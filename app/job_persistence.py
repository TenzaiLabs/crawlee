from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from . import db, parser
from .common import sanitize_log_value
from .job_status import TERMINAL_JOB_STATUSES
from .models import DiscoveryResult, JobStatus

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def serialize_sitemap(sitemap: dict[str, Any]) -> tuple[str, int, int]:
    parser.validate_sitemap(sitemap)
    serialized = json.dumps(sitemap, separators=(",", ":"), ensure_ascii=False)
    entries = sitemap.get("entries")
    entry_count = len(entries) if isinstance(entries, list) else 0
    return serialized, entry_count, len(serialized.encode("utf-8"))


def _checkpoint_result_sitemap(
    checkpoint_sitemap: str,
    result: DiscoveryResult,
) -> dict[str, Any]:
    try:
        sitemap = json.loads(checkpoint_sitemap)
    except (json.JSONDecodeError, TypeError) as exc:
        raise parser.CrawlArtifactsCorruptError("Checkpoint is not valid JSON") from exc
    parser.validate_sitemap(sitemap)
    finalized = dict(sitemap)
    finalized["discovery"] = result.model_dump(mode="json")
    return finalized


def _result_with_checkpoint_progress(
    result: DiscoveryResult,
    serialized_progress: str | None,
) -> DiscoveryResult:
    if serialized_progress is None:
        return result
    try:
        progress = json.loads(serialized_progress)
        if not isinstance(progress, dict):
            raise TypeError("checkpoint progress must be an object")
        allowed_fields = {"rounds", "new_entry_count", "state_count", "workflow_count"}
        values = result.model_dump(mode="python")
        values.update({key: progress[key] for key in allowed_fields if key in progress})
        return DiscoveryResult.model_validate(values)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            "Ignoring invalid discovery checkpoint progress error=%s",
            sanitize_log_value(exc),
        )
        return result


class JobPersistence:
    """Own all compare-and-set transitions and result checkpoint publication."""

    def __init__(self, state_locks: dict[str, asyncio.Lock]) -> None:
        self._state_locks = state_locks

    async def update_status(
        self,
        job_id: str,
        status: JobStatus,
        error: str | None = None,
    ) -> None:
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

    async def transition(
        self,
        job_id: str,
        from_statuses: set[JobStatus],
        to_status: JobStatus,
    ) -> bool:
        placeholders = ",".join("?" for _ in from_statuses)
        updated = await db.execute_rowcount(
            f"UPDATE jobs SET status = ?, error = NULL, finished_at = NULL "
            f"WHERE job_id = ? AND status IN ({placeholders})",
            (to_status.value, job_id, *(status.value for status in from_statuses)),
        )
        return updated == 1

    async def claim_job(self, job_id: str, to_status: JobStatus) -> bool:
        return await self.transition(
            job_id,
            {JobStatus.queued, JobStatus.pending},
            to_status,
        )

    async def cancel_queued(self, job_id: str) -> bool:
        updated = await db.execute_rowcount(
            """
            UPDATE jobs
            SET status = ?, error = NULL, finished_at = ?
            WHERE job_id = ? AND status IN (?, ?)
            """,
            (
                JobStatus.cancelled.value,
                _now(),
                job_id,
                JobStatus.queued.value,
                JobStatus.pending.value,
            ),
        )
        return updated == 1

    async def fail_if_status(
        self,
        job_id: str,
        from_status: JobStatus,
        to_status: JobStatus,
        error: str,
    ) -> bool:
        updated = await db.execute_rowcount(
            """
            UPDATE jobs
            SET status = ?, error = ?, finished_at = ?
            WHERE job_id = ? AND status = ?
            """,
            (to_status.value, error, _now(), job_id, from_status.value),
        )
        return updated == 1

    async def publish_baseline(
        self,
        job_id: str,
        sitemap: dict[str, Any],
        evidence: dict[str, Any],
    ) -> bool:
        serialized, entry_count, _ = serialize_sitemap(sitemap)
        logger.info(
            "Persisting baseline checkpoint job_id=%s entries=%d",
            sanitize_log_value(job_id),
            entry_count,
        )
        updated = await db.execute_rowcount(
            """
            UPDATE jobs
            SET baseline_sitemap = ?, crawl_evidence = ?
            WHERE job_id = ? AND status = ?
            """,
            (
                serialized,
                db.dumps_json(evidence),
                job_id,
                JobStatus.crawling.value,
            ),
        )
        return updated == 1

    async def publish_discovery_checkpoint(
        self,
        job_id: str,
        sitemap: dict[str, Any],
        evidence: dict[str, Any],
        progress: dict[str, int],
    ) -> bool:
        serialized, entry_count, _ = serialize_sitemap(sitemap)
        logger.info(
            "Persisting discovery checkpoint job_id=%s entries=%d rounds=%d",
            sanitize_log_value(job_id),
            entry_count,
            progress["rounds"],
        )
        updated = await db.execute_rowcount(
            """
            UPDATE jobs
            SET discovery_checkpoint_sitemap = ?,
                discovery_checkpoint_progress = ?,
                crawl_evidence = ?
            WHERE job_id = ? AND status = ? AND baseline_sitemap IS NOT NULL
            """,
            (
                serialized,
                db.dumps_json(progress),
                db.dumps_json(evidence),
                job_id,
                JobStatus.discovering.value,
            ),
        )
        return updated == 1

    async def complete(
        self,
        job_id: str,
        sitemap: dict[str, Any],
        evidence: dict[str, Any],
        result: DiscoveryResult,
        cancel_event: asyncio.Event,
    ) -> bool:
        finalized = dict(sitemap)
        finalized["discovery"] = result.model_dump(mode="json")
        serialized, entry_count, size_bytes = serialize_sitemap(finalized)
        lock = self._state_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            if cancel_event.is_set():
                return False
            updated = await db.execute_rowcount(
                """
                UPDATE jobs
                SET sitemap = ?,
                    crawl_evidence = ?,
                    discovery_result = ?,
                    result_entry_count = ?,
                    result_size_bytes = ?,
                    status = ?,
                    error = NULL,
                    finished_at = ?
                WHERE job_id = ? AND status = ? AND baseline_sitemap IS NOT NULL
                """,
                (
                    serialized,
                    db.dumps_json(evidence),
                    result.model_dump_json(),
                    entry_count,
                    size_bytes,
                    JobStatus.completed.value,
                    _now(),
                    job_id,
                    JobStatus.processing.value,
                ),
            )
        return updated == 1

    async def finalize_from_latest_checkpoint(
        self,
        job_id: str,
        result: DiscoveryResult,
        *,
        expected_statuses: set[JobStatus],
        error: str | None = None,
        final_status: JobStatus = JobStatus.completed,
    ) -> bool:
        if final_status not in {JobStatus.completed, JobStatus.cancelled}:
            raise ValueError("Checkpoint final status must be completed or cancelled")
        row = await db.fetch_one(
            """
            SELECT baseline_sitemap,
                   discovery_checkpoint_sitemap,
                   discovery_checkpoint_progress
            FROM jobs
            WHERE job_id = ?
            """,
            (job_id,),
        )
        if row is None or row["baseline_sitemap"] is None:
            raise RuntimeError("Job has no baseline checkpoint")
        checkpoint_result = _result_with_checkpoint_progress(
            result,
            row["discovery_checkpoint_progress"],
        )
        checkpoint_sitemap = row["discovery_checkpoint_sitemap"]
        if checkpoint_sitemap is not None:
            try:
                sitemap = _checkpoint_result_sitemap(checkpoint_sitemap, checkpoint_result)
            except parser.CrawlArtifactsCorruptError as exc:
                logger.warning(
                    "Ignoring invalid discovery checkpoint sitemap job_id=%s error=%s",
                    sanitize_log_value(job_id),
                    sanitize_log_value(exc),
                )
                checkpoint_result = result
                sitemap = _checkpoint_result_sitemap(row["baseline_sitemap"], result)
        else:
            sitemap = _checkpoint_result_sitemap(row["baseline_sitemap"], result)
        serialized, entry_count, size_bytes = serialize_sitemap(sitemap)
        placeholders = ",".join("?" for _ in expected_statuses)
        updated = await db.execute_rowcount(
            f"""
            UPDATE jobs
            SET sitemap = ?,
                discovery_result = ?,
                result_entry_count = ?,
                result_size_bytes = ?,
                status = ?,
                error = ?,
                finished_at = ?
            WHERE job_id = ?
              AND baseline_sitemap IS NOT NULL
              AND status IN ({placeholders})
            """,
            (
                serialized,
                checkpoint_result.model_dump_json(),
                entry_count,
                size_bytes,
                final_status.value,
                error,
                _now(),
                job_id,
                *(status.value for status in expected_statuses),
            ),
        )
        return updated == 1
