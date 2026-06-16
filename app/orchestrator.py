from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from . import auth_agent, crawler, db, parser, proxy
from .job_status import ACTIVE_JOB_STATUSES, TERMINAL_JOB_STATUSES
from .log_records import sanitize_log_file
from .models import JobStatus

logger = logging.getLogger(__name__)

_job_tasks: dict[str, asyncio.Task[None]] = {}
_cancel_events: dict[str, asyncio.Event] = {}
_queue: asyncio.Queue[str] = asyncio.Queue()
_drainer_task: asyncio.Task[None] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_auth_config(raw_auth_config: Any) -> dict[str, Any]:
    auth_config = raw_auth_config if isinstance(raw_auth_config, dict) else {}
    return dict(auth_config)


def _extract_manual_headers(auth_config: dict[str, Any]) -> list[str]:
    headers = auth_config.get("headers")
    if not isinstance(headers, list):
        return []
    return [str(header) for header in headers]


async def _cancel_if_requested(job_id: str, cancel_event: asyncio.Event) -> bool:
    if not cancel_event.is_set():
        return False
    await update_job_status(job_id, JobStatus.cancelled)
    return True


async def _run_auth_if_needed(
    job_id: str,
    target_url: str,
    auth_config: dict[str, Any],
    base_headers: list[str],
    proxy_process: proxy.ProxyProcess,
    cancel_event: asyncio.Event,
) -> tuple[list[str], str | None, list[str]]:
    merged_headers = list(base_headers)
    if not auth_agent.needs_auth(auth_config):
        return merged_headers, None, []

    logger.info("Running authentication for job_id=%s", job_id)
    resolved_config = auth_agent.resolve_secrets(auth_config)
    # Hand the Proxify log path to the auth agent without persisting it.
    resolved_config["_proxify_log_path"] = proxy_process.log_path
    auth_result = await auth_agent.authenticate(target_url, resolved_config, cancel_event)
    merged_headers.extend(auth_result.headers)
    dynamic_exclude_patterns = crawler.blocked_urls_to_exclude_patterns(
        auth_result.blocked_urls,
        target_url=target_url,
        base_url=auth_result.landing_url or target_url,
    )
    if dynamic_exclude_patterns:
        logger.info(
            "Auth produced %d dynamic crawl exclusion pattern(s)",
            len(dynamic_exclude_patterns),
        )
    return merged_headers, auth_result.landing_url, dynamic_exclude_patterns


async def has_active_job() -> bool:
    placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
    query = f"SELECT COUNT(1) as count FROM jobs WHERE status IN ({placeholders})"
    row = await db.fetch_one(query, tuple(ACTIVE_JOB_STATUSES))
    logger.debug("Checked for active jobs")
    return bool(row and row["count"])


async def update_job_status(job_id: str, status: JobStatus, error: str | None = None) -> None:
    logger.info("Updating job status job_id=%s status=%s", job_id, status.value)
    finished_at = _now() if status.value in TERMINAL_JOB_STATUSES else None
    await db.execute(
        """
        UPDATE jobs
        SET status = ?, error = ?, finished_at = ?
        WHERE job_id = ?
        """,
        (status.value, error, finished_at, job_id),
    )


async def run_job(job_id: str, cancel_event: asyncio.Event) -> None:
    proxy_process: proxy.ProxyProcess | None = None
    logger.info("Starting job runner for job_id=%s", job_id)
    try:
        row = await db.fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if row is None:
            logger.warning("Job row missing during run_job startup job_id=%s", job_id)
            return

        # Check if already cancelled while queued.
        if row["status"] in TERMINAL_JOB_STATUSES:
            logger.info("Job already in terminal state, skipping job_id=%s", job_id)
            return

        auth_config = _normalize_auth_config(db.loads_json(row["auth_config"]) or {})
        manual_headers = _extract_manual_headers(auth_config)
        should_auth = auth_agent.needs_auth(auth_config)
        logger.debug("Job %s auth phase required=%s", job_id, should_auth)

        next_status = JobStatus.authenticating if should_auth else JobStatus.crawling
        logger.info("Job %s transitioning queued -> %s", job_id, next_status.value)
        await update_job_status(job_id, next_status)
        if await _cancel_if_requested(job_id, cancel_event):
            return

        proxy_process = await proxy.start_proxy(job_id)
        await proxy.wait_for_proxy(proxy_process)
        logger.info("Proxy started for job_id=%s", job_id)
        if await _cancel_if_requested(job_id, cancel_event):
            return

        await proxy.check_target_connectivity(row["target_url"])

        merged_headers, landing_url, dynamic_exclude_patterns = await _run_auth_if_needed(
            job_id,
            row["target_url"],
            auth_config,
            manual_headers,
            proxy_process,
            cancel_event,
        )
        if await _cancel_if_requested(job_id, cancel_event):
            return

        if should_auth:
            logger.info("Job %s transitioning authenticating -> crawling", job_id)
            await update_job_status(job_id, JobStatus.crawling)

        extra_seed_urls: list[str] | None = None
        if landing_url and landing_url != row["target_url"]:
            extra_seed_urls = [landing_url]

        logger.info(
            "Crawl config: headers=%d landing_url=%s extra_seeds=%s dynamic_exclusions=%d",
            len(merged_headers),
            landing_url,
            extra_seed_urls,
            len(dynamic_exclude_patterns),
        )
        await crawler.run_crawl(
            crawler.CrawlConfig(
                target_url=row["target_url"],
                scope_config=db.loads_json(row["scope_config"]),
                headers=merged_headers or None,
                extra_seed_urls=extra_seed_urls,
                dynamic_exclude_patterns=dynamic_exclude_patterns or None,
            ),
            cancel_event=cancel_event,
            log_path=proxy_process.log_path,
        )
        if await _cancel_if_requested(job_id, cancel_event):
            return

        logger.info("Job %s transitioning crawling -> processing", job_id)
        await update_job_status(job_id, JobStatus.processing)
        await asyncio.to_thread(parser.parse_log, job_id, row["target_url"])
        logger.info("Job %s transitioning processing -> completed", job_id)
        await update_job_status(job_id, JobStatus.completed)
        logger.info("Job completed successfully job_id=%s", job_id)
    except asyncio.CancelledError:
        logger.warning("Job task cancelled job_id=%s", job_id)
        await update_job_status(job_id, JobStatus.cancelled)
    except Exception as exc:  # pragma: no cover - safeguard
        logger.warning("Job failed job_id=%s error=%s", job_id, exc)
        if cancel_event.is_set():
            await update_job_status(job_id, JobStatus.cancelled)
        else:
            await update_job_status(job_id, JobStatus.failed, str(exc))
    finally:
        if proxy_process is not None:
            await proxy.stop_proxy(proxy_process)
            await asyncio.to_thread(sanitize_log_file, proxy_process.log_path)
        logger.debug("Job runner cleanup finished for job_id=%s", job_id)


async def _drain_queue() -> None:
    logger.info("Queue drainer started")
    while True:
        logger.info("Queue drainer waiting for next job (queue_size=%d)", _queue.qsize())
        job_id = await _queue.get()
        logger.info("Queue drainer picked up job_id=%s (remaining=%d)", job_id, _queue.qsize())
        cancel_event = asyncio.Event()
        _cancel_events[job_id] = cancel_event
        task = asyncio.create_task(run_job(job_id, cancel_event))
        _job_tasks[job_id] = task

        def _cleanup(_: asyncio.Task[None], _job_id: str = job_id) -> None:
            _cancel_events.pop(_job_id, None)
            _job_tasks.pop(_job_id, None)
            logger.debug("Cleaned up in-memory job task state job_id=%s", _job_id)

        task.add_done_callback(_cleanup)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            _queue.task_done()
        logger.info("Queue drainer finished job_id=%s", job_id)


def start_drainer() -> None:
    global _drainer_task
    if _drainer_task is None or _drainer_task.done():
        _drainer_task = asyncio.create_task(_drain_queue())
        logger.info("Started queue drainer task")


def enqueue_job(job_id: str) -> None:
    _queue.put_nowait(job_id)
    logger.info("Enqueued job job_id=%s queue_size=%d", job_id, _queue.qsize())


async def request_cancel(job_id: str) -> bool:
    event = _cancel_events.get(job_id)
    if event is None:
        logger.warning("Cancel requested for non-running job_id=%s", job_id)
        return False
    event.set()
    logger.info("Set cancellation event for job_id=%s", job_id)
    return True


def get_job_task(job_id: str) -> asyncio.Task[None] | None:
    return _job_tasks.get(job_id)
