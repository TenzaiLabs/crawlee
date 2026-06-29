from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from . import auth_agent, crawler, db, parser, proxy
from .common import sanitize_log_value
from .job_status import ACTIVE_JOB_STATUSES, TERMINAL_JOB_STATUSES
from .log_records import sanitize_log_file
from .models import JobStatus

logger = logging.getLogger(__name__)

_job_tasks: dict[str, asyncio.Task[None]] = {}
_cancel_events: dict[str, asyncio.Event] = {}
_queue: asyncio.Queue[str] = asyncio.Queue()
_drainer_task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class CrawlAuthContext:
    headers: list[str]
    landing_url: str | None = None
    extra_seed_urls: list[str] = field(default_factory=list)
    discovered_urls: list[str] = field(default_factory=list)
    dynamic_exclude_patterns: list[str] = field(default_factory=list)
    auth_blocked_url_count: int = 0
    auth_applied_blocked_url_count: int = 0
    auth_ignored_blocked_url_count: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_auth_config(raw_auth_config: Any) -> dict[str, Any]:
    auth_config = raw_auth_config if isinstance(raw_auth_config, dict) else {}
    return dict(auth_config)


def _extract_manual_headers(auth_config: dict[str, Any]) -> list[str]:
    headers = auth_config.get("headers")
    if not isinstance(headers, list):
        return []
    resolved = auth_agent.resolve_secrets({"headers": headers})
    resolved_headers = resolved.get("headers")
    if not isinstance(resolved_headers, list):
        return []
    return [str(header) for header in resolved_headers]


def _same_url(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    left_path = left_parsed.path.rstrip("/") or "/"
    right_path = right_parsed.path.rstrip("/") or "/"
    return (
        left_parsed.scheme.lower(),
        left_parsed.netloc.lower(),
        left_path,
        left_parsed.query,
    ) == (
        right_parsed.scheme.lower(),
        right_parsed.netloc.lower(),
        right_path,
        right_parsed.query,
    )


def _merge_extra_seed_urls(
    *,
    target_url: str,
    landing_url: str | None,
    discovered_urls: list[str],
) -> list[str]:
    candidates: list[str] = []
    if landing_url:
        candidates.append(landing_url)
    candidates.extend(discovered_urls)
    seeds: list[str] = []
    for candidate in candidates:
        url = str(candidate).strip()
        if not url or _same_url(url, target_url):
            continue
        if any(_same_url(url, existing) for existing in seeds):
            continue
        seeds.append(url)
    return seeds


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
    should_auth: bool,
    cancel_event: asyncio.Event,
) -> CrawlAuthContext:
    merged_headers = list(base_headers)
    if not should_auth:
        return CrawlAuthContext(headers=merged_headers)

    logger.info("Running authentication for job_id=%s", sanitize_log_value(job_id))
    resolved_config = auth_agent.resolve_secrets(auth_config)
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
    auth_blocked_url_count = len(auth_result.blocked_urls)
    auth_applied_blocked_url_count = len(dynamic_exclude_patterns)
    auth_ignored_blocked_url_count = max(
        0,
        auth_blocked_url_count - auth_applied_blocked_url_count,
    )

    extra_seed_urls = _merge_extra_seed_urls(
        target_url=target_url,
        landing_url=auth_result.landing_url,
        discovered_urls=auth_result.discovered_urls,
    )
    if auth_result.discovered_urls:
        logger.info(
            "Auth discovered %d same-origin crawl seed URL(s)",
            len(auth_result.discovered_urls),
        )

    return CrawlAuthContext(
        headers=merged_headers,
        landing_url=auth_result.landing_url,
        extra_seed_urls=extra_seed_urls,
        discovered_urls=auth_result.discovered_urls,
        dynamic_exclude_patterns=dynamic_exclude_patterns,
        auth_blocked_url_count=auth_blocked_url_count,
        auth_applied_blocked_url_count=auth_applied_blocked_url_count,
        auth_ignored_blocked_url_count=auth_ignored_blocked_url_count,
    )


async def has_active_job() -> bool:
    placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
    query = f"SELECT COUNT(1) as count FROM jobs WHERE status IN ({placeholders})"
    row = await db.fetch_one(query, tuple(ACTIVE_JOB_STATUSES))
    logger.debug("Checked for active jobs")
    return bool(row and row["count"])


async def update_job_status(job_id: str, status: JobStatus, error: str | None = None) -> None:
    logger.info(
        "Updating job status job_id=%s status=%s",
        sanitize_log_value(job_id),
        sanitize_log_value(status.value),
    )
    finished_at = _now() if status.value in TERMINAL_JOB_STATUSES else None
    await db.execute(
        """
        UPDATE jobs
        SET status = ?, error = ?, finished_at = ?
        WHERE job_id = ?
        """,
        (status.value, error, finished_at, job_id),
    )


def build_generated_exclusions_payload(
    config: crawler.CrawlConfig,
    auth_context: CrawlAuthContext,
) -> dict[str, Any]:
    return {
        "auth_blocked_url_count": auth_context.auth_blocked_url_count,
        "auth_applied_blocked_url_count": auth_context.auth_applied_blocked_url_count,
        "auth_ignored_blocked_url_count": auth_context.auth_ignored_blocked_url_count,
        "auth_dynamic_patterns": list(auth_context.dynamic_exclude_patterns),
        "auth_discovered_url_count": len(auth_context.discovered_urls),
        "auth_discovered_urls": list(auth_context.discovered_urls),
        "extra_seed_urls": list(auth_context.extra_seed_urls),
        "effective_patterns": crawler.build_exclusion_patterns(config),
    }


async def update_job_generated_exclusions(job_id: str, exclusions: dict[str, Any]) -> None:
    logger.info("Persisting generated exclusions job_id=%s", sanitize_log_value(job_id))
    await db.execute(
        """
        UPDATE jobs
        SET generated_exclusions = ?
        WHERE job_id = ?
        """,
        (db.dumps_json(exclusions), job_id),
    )


async def run_job(job_id: str, cancel_event: asyncio.Event) -> None:
    proxy_process: proxy.ProxyProcess | None = None
    log_job_id = sanitize_log_value(job_id)
    logger.info("Starting job runner for job_id=%s", log_job_id)
    try:
        row = await db.fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if row is None:
            logger.warning("Job row missing during run_job startup job_id=%s", log_job_id)
            return

        # Check if already cancelled while queued.
        if row["status"] in TERMINAL_JOB_STATUSES:
            logger.info("Job already in terminal state, skipping job_id=%s", log_job_id)
            return

        auth_config = _normalize_auth_config(db.loads_json(row["auth_config"]) or {})
        manual_headers = _extract_manual_headers(auth_config)
        should_auth = auth_agent.needs_auth(auth_config)
        logger.debug("Job %s auth phase required=%s", log_job_id, should_auth)

        next_status = JobStatus.authenticating if should_auth else JobStatus.crawling
        logger.info(
            "Job %s transitioning queued -> %s",
            log_job_id,
            sanitize_log_value(next_status.value),
        )
        await update_job_status(job_id, next_status)
        if await _cancel_if_requested(job_id, cancel_event):
            return

        proxy_process = await proxy.start_proxy(job_id)
        await proxy.wait_for_proxy(proxy_process)
        logger.info("Proxy started for job_id=%s", log_job_id)
        if await _cancel_if_requested(job_id, cancel_event):
            return

        await proxy.check_target_connectivity(row["target_url"])

        auth_context = await _run_auth_if_needed(
            job_id,
            row["target_url"],
            auth_config,
            manual_headers,
            should_auth,
            cancel_event,
        )
        if await _cancel_if_requested(job_id, cancel_event):
            return

        if should_auth:
            logger.info("Job %s transitioning authenticating -> crawling", log_job_id)
            await update_job_status(job_id, JobStatus.crawling)

        logger.info(
            "Crawl config: headers=%d landing_url=%s extra_seeds=%s dynamic_exclusions=%d",
            len(auth_context.headers),
            sanitize_log_value(auth_context.landing_url),
            sanitize_log_value(auth_context.extra_seed_urls or None),
            len(auth_context.dynamic_exclude_patterns),
        )
        crawl_config = crawler.CrawlConfig(
            target_url=row["target_url"],
            scope_config=db.loads_json(row["scope_config"]),
            headers=auth_context.headers or None,
            extra_seed_urls=auth_context.extra_seed_urls or None,
            dynamic_exclude_patterns=auth_context.dynamic_exclude_patterns or None,
        )
        await update_job_generated_exclusions(
            job_id,
            build_generated_exclusions_payload(crawl_config, auth_context),
        )
        await crawler.run_crawl(
            crawl_config,
            cancel_event=cancel_event,
            log_path=proxy_process.log_path,
        )
        if await _cancel_if_requested(job_id, cancel_event):
            return

        logger.info("Job %s transitioning crawling -> processing", log_job_id)
        await update_job_status(job_id, JobStatus.processing)
        await asyncio.to_thread(parser.parse_log, job_id, row["target_url"])
        logger.info("Job %s transitioning processing -> completed", log_job_id)
        await update_job_status(job_id, JobStatus.completed)
        logger.info("Job completed successfully job_id=%s", log_job_id)
    except asyncio.CancelledError:
        logger.warning("Job task cancelled job_id=%s", log_job_id)
        await update_job_status(job_id, JobStatus.cancelled)
    except Exception as exc:  # pragma: no cover - safeguard
        logger.warning("Job failed job_id=%s error=%s", log_job_id, sanitize_log_value(exc))
        if cancel_event.is_set():
            await update_job_status(job_id, JobStatus.cancelled)
        else:
            await update_job_status(job_id, JobStatus.failed, str(exc))
    finally:
        if proxy_process is not None:
            await proxy.stop_proxy(proxy_process)
            await asyncio.to_thread(sanitize_log_file, proxy_process.log_path)
            await asyncio.to_thread(sanitize_log_file, proxy_process.log_path + ".katana")
        logger.debug("Job runner cleanup finished for job_id=%s", log_job_id)


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
            logger.debug(
                "Cleaned up in-memory job task state job_id=%s",
                sanitize_log_value(_job_id),
            )

        task.add_done_callback(_cleanup)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            _queue.task_done()
        logger.info("Queue drainer finished job_id=%s", sanitize_log_value(job_id))


def start_drainer() -> None:
    global _drainer_task
    if _drainer_task is None or _drainer_task.done():
        _drainer_task = asyncio.create_task(_drain_queue())
        logger.info("Started queue drainer task")


def enqueue_job(job_id: str) -> None:
    _queue.put_nowait(job_id)
    logger.info("Enqueued job job_id=%s queue_size=%d", sanitize_log_value(job_id), _queue.qsize())


async def request_cancel(job_id: str) -> bool:
    event = _cancel_events.get(job_id)
    if event is None:
        logger.warning("Cancel requested for non-running job_id=%s", sanitize_log_value(job_id))
        return False
    event.set()
    logger.info("Set cancellation event for job_id=%s", sanitize_log_value(job_id))
    return True


def get_job_task(job_id: str) -> asyncio.Task[None] | None:
    return _job_tasks.get(job_id)
