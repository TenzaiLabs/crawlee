from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import json
import logging
import os
import shutil
import signal
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from . import db, orchestrator, parser  # noqa: E402
from .auth_config import AuthConfigValidationError, validate_auth_config  # noqa: E402
from .common import sanitize_log_value  # noqa: E402
from .job_status import INTERRUPTED_JOB_STATUSES, TERMINAL_JOB_STATUSES  # noqa: E402
from .models import (  # noqa: E402
    CancellationStatus,
    JobCancelResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JobResponse,
    JobResultMetadata,
    JobStatus,
    JobSummary,
)
from .scope_config import ScopeConfigValidationError, validate_scope_config  # noqa: E402

logger = logging.getLogger(__name__)

APP_TITLE = "Tenzai Crawler"
APP_DESCRIPTION = (
    "Async-native Tenzai crawling service for operator-led application reconnaissance, "
    "authenticated crawl setup, and sitemap extraction."
)
APP_VERSION = "0.1.0"
STATIC_DIR = Path(__file__).resolve().parent / "static"
OPENAPI_URL = "/openapi.json"
FAVICON_URL = "/static/tenzai-favicon-48.png"
LOGO_URL = "/static/tenzai-logo.svg"

def _result_metadata(row: Any) -> JobResultMetadata | None:
    entry_count = row["result_entry_count"]
    size_bytes = row["result_size_bytes"]
    if entry_count is None or size_bytes is None:
        return None
    return JobResultMetadata(entry_count=entry_count, size_bytes=size_bytes)


def _duration_seconds(created_at: str, finished_at: str | None) -> float | None:
    try:
        started = datetime.fromisoformat(created_at)
        finished = datetime.fromisoformat(finished_at) if finished_at else datetime.now(UTC)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=UTC)
    return max(0.0, round((finished - started).total_seconds(), 3))


async def _queue_position(row: Any) -> int | None:
    if row["status"] != JobStatus.queued.value:
        return None
    position = await db.fetch_one(
        """
        SELECT COUNT(1) AS position
        FROM jobs
        WHERE status = ?
          AND (created_at < ? OR (created_at = ? AND rowid <= ?))
        """,
        (JobStatus.queued.value, row["created_at"], row["created_at"], row["rowid"]),
    )
    return int(position["position"]) if position is not None else None


def _decode_persisted_sitemap(job_id: str, serialized: str) -> dict[str, Any]:
    try:
        sitemap = json.loads(serialized)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("Persisted sitemap is corrupt for job_id=%s", sanitize_log_value(job_id))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Completed job result is corrupt",
        ) from exc
    try:
        return parser.validate_sitemap(sitemap)
    except parser.CrawlArtifactsCorruptError as exc:
        logger.error(
            "Persisted sitemap has invalid shape for job_id=%s",
            sanitize_log_value(job_id),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Completed job result is corrupt",
        ) from exc


async def _read_completed_sitemap(row: Any) -> tuple[dict[str, Any], JobResultMetadata]:
    job_id = row["job_id"]
    serialized = row["sitemap"]
    if serialized is not None:
        sitemap = _decode_persisted_sitemap(job_id, serialized)
        metadata = _result_metadata(row)
        if metadata is None:
            _, entry_count, size_bytes = orchestrator.serialize_sitemap(sitemap)
            metadata = JobResultMetadata(entry_count=entry_count, size_bytes=size_bytes)
        return sitemap, metadata

    try:
        sitemap = await asyncio.to_thread(
            parser.parse_log,
            job_id,
            row["target_url"],
            require_artifacts=True,
        )
    except (parser.CrawlArtifactsMissingError, parser.CrawlArtifactsCorruptError) as exc:
        logger.error("Completed legacy job has unavailable result artifacts job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Completed job result is unavailable"
                if isinstance(exc, parser.CrawlArtifactsMissingError)
                else "Completed job result is corrupt"
            ),
        ) from exc

    serialized, entry_count, size_bytes = orchestrator.serialize_sitemap(sitemap)
    await db.execute_rowcount(
        """
        UPDATE jobs
        SET sitemap = ?, result_entry_count = ?, result_size_bytes = ?
        WHERE job_id = ? AND status = ? AND sitemap IS NULL
        """,
        (serialized, entry_count, size_bytes, job_id, JobStatus.completed.value),
    )
    persisted = await db.fetch_one(
        "SELECT sitemap, result_entry_count, result_size_bytes FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    if persisted is None or persisted["sitemap"] is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Completed job result could not be persisted",
        )
    return (
        _decode_persisted_sitemap(job_id, persisted["sitemap"]),
        JobResultMetadata(
            entry_count=persisted["result_entry_count"],
            size_bytes=persisted["result_size_bytes"],
        ),
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting crawler service lifespan")
    if os.getenv("CRAWLER_ENABLE_FAULTHANDLER") == "1":
        # Useful for debugging stuck jobs in production-like environments where ptrace/strace
        # is disallowed. `kill -USR2 <pid>` will dump all Python stacks to stderr.
        faulthandler.enable()
        with contextlib.suppress(RuntimeError, ValueError):
            faulthandler.register(signal.SIGUSR2, all_threads=True)
        logger.debug("Faulthandler enabled with SIGUSR2 stack dump support")

    missing = [binary for binary in ("katana", "proxify") if shutil.which(binary) is None]
    if missing:
        missing_list = ", ".join(missing)
        logger.warning("Missing required binaries in PATH: %s", missing_list)
        raise RuntimeError(f"Missing required binaries in PATH: {missing_list}")

    # Ensure the asyncio child watcher is attached to the *running* loop.
    #
    # If some library (or the runtime) created a default loop earlier, the global child
    # watcher can end up attached to a different loop than the one uvicorn is running.
    # In that case, subprocess completion callbacks never fire and `await process.wait()`
    # can hang forever.
    #
    # Ref: https://github.com/python/cpython/issues/79802
    with contextlib.suppress(NotImplementedError, RuntimeError, AttributeError):
        get_child_watcher = getattr(asyncio, "get_child_watcher", None)
        if callable(get_child_watcher):
            get_child_watcher().attach_loop(asyncio.get_running_loop())
            logger.debug("Attached asyncio child watcher to running loop")

    await db.init_db()
    logger.info("Database initialized")

    interrupted = "', '".join(INTERRUPTED_JOB_STATUSES)
    await db.execute(
        f"""
        UPDATE jobs
        SET status = '{JobStatus.failed_interrupted.value}'
        WHERE status IN ('{interrupted}')
        """
    )
    logger.info("Marked interrupted in-flight jobs as failed_interrupted")
    orchestrator.start_drainer()
    yield
    logger.info("Shutting down crawler service lifespan")


app = FastAPI(
    lifespan=lifespan,
    title=APP_TITLE,
    description=APP_DESCRIPTION,
    version=APP_VERSION,
    openapi_url=OPENAPI_URL,
    docs_url=None,
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=APP_TITLE,
        version=APP_VERSION,
        description=APP_DESCRIPTION,
        routes=app.routes,
    )
    schema["info"]["x-logo"] = {"url": LOGO_URL, "altText": "Tenzai"}
    app.openapi_schema = schema
    return app.openapi_schema


app.__dict__["openapi"] = custom_openapi


def _with_tenzai_docs_brand(response: HTMLResponse, product_label: str) -> HTMLResponse:
    body = bytes(response.body).decode("utf-8")
    style = """
    <style>
      .tenzai-docs-header {
        align-items: center;
        background: #232325;
        border-bottom: 1px solid #3a3a3d;
        box-sizing: border-box;
        color: #ffffff;
        display: flex;
        gap: 18px;
        min-height: 64px;
        padding: 14px 32px;
      }
      .tenzai-docs-header img {
        height: 32px;
        width: auto;
      }
      .tenzai-docs-header span {
        border-left: 1px solid #686868;
        color: #88ffff;
        font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        font-size: 15px;
        font-weight: 600;
        line-height: 1;
        padding-left: 18px;
      }
      @media (max-width: 640px) {
        .tenzai-docs-header {
          padding: 12px 18px;
        }
        .tenzai-docs-header span {
          font-size: 13px;
        }
      }
    </style>
    """
    header = (
        '<header class="tenzai-docs-header">'
        f'<img src="{LOGO_URL}" alt="Tenzai logo">'
        f"<span>{product_label}</span>"
        "</header>"
    )
    body = body.replace("</head>", f"{style}</head>")
    body = body.replace("<body>", f"<body>{header}", 1)
    return HTMLResponse(content=body, status_code=response.status_code)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html() -> HTMLResponse:
    response = get_swagger_ui_html(
        openapi_url=OPENAPI_URL,
        title=f"{APP_TITLE} API docs",
        swagger_favicon_url=FAVICON_URL,
    )
    return _with_tenzai_docs_brand(response, "Crawler API")


@app.get("/redoc", include_in_schema=False)
async def custom_redoc_html() -> HTMLResponse:
    response = get_redoc_html(
        openapi_url=OPENAPI_URL,
        title=f"{APP_TITLE} ReDoc",
        redoc_favicon_url=FAVICON_URL,
    )
    return _with_tenzai_docs_brand(response, "Crawler API")


if os.getenv("CRAWLER_ENABLE_DEBUG_ENDPOINTS") == "1":

    @app.get("/debug/tasks")
    async def debug_tasks() -> dict:
        tasks_payload: list[dict] = []
        for task in asyncio.all_tasks():
            frames = task.get_stack(limit=25)
            stack: list[str] = []
            for frame in frames:
                stack.extend(traceback.format_stack(frame, limit=25))
            tasks_payload.append(
                {
                    "id": id(task),
                    "name": task.get_name(),
                    "done": task.done(),
                    "cancelled": task.cancelled(),
                    "coro": repr(task.get_coro()),
                    "stack": stack,
                }
            )
        return {"tasks": tasks_payload}

    @app.get("/debug/jobs/{job_id}/stack")
    async def debug_job_stack(job_id: str) -> dict:
        task = orchestrator.get_job_task(job_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No in-memory task for job",
            )
        frames = task.get_stack(limit=25)
        stack: list[str] = []
        for frame in frames:
            stack.extend(traceback.format_stack(frame, limit=25))
        return {
            "id": id(task),
            "name": task.get_name(),
            "done": task.done(),
            "cancelled": task.cancelled(),
            "coro": repr(task.get_coro()),
            "stack": stack,
        }


@app.post("/jobs", response_model=JobCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreateRequest) -> JobCreateResponse:
    logger.info(
        "Received create job request for target_url=%s",
        sanitize_log_value(payload.target_url),
    )

    try:
        validate_scope_config(payload.scope_config)
        validate_auth_config(payload.auth_config)
    except ScopeConfigValidationError as exc:
        logger.warning(
            "Rejected job creation with invalid scope_config: %s",
            sanitize_log_value(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except AuthConfigValidationError as exc:
        logger.warning(
            "Rejected job creation with invalid auth_config: %s",
            sanitize_log_value(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    job_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO jobs (
            job_id,
            status,
            target_url,
            scope_config,
            auth_config,
            error,
            created_at,
            finished_at
        )
        VALUES (?, 'queued', ?, ?, ?, NULL, datetime('now'), NULL)
        """,
        (
            job_id,
            str(payload.target_url),
            db.dumps_json(payload.scope_config),
            db.dumps_json(payload.auth_config),
        ),
    )
    orchestrator.enqueue_job(job_id)
    logger.info("Created job job_id=%s", job_id)
    return JobCreateResponse(job_id=job_id)


@app.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=250)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    logger.debug("Listing jobs status=%s limit=%d offset=%d", status_filter, limit, offset)
    where = " WHERE j.status = ?" if status_filter is not None else ""
    params: tuple[Any, ...] = (status_filter.value,) if status_filter is not None else ()
    async with db.connect() as conn:
        await conn.execute("BEGIN")
        count_cursor = await conn.execute(f"SELECT COUNT(1) FROM jobs j{where}", params)
        count_row = await count_cursor.fetchone()
        await count_cursor.close()
        rows_cursor = await conn.execute(
            f"""
            SELECT j.job_id, j.status, j.target_url, j.error, j.created_at, j.finished_at,
                   j.result_entry_count, j.result_size_bytes,
                   CASE WHEN j.status = 'queued' THEN (
                       SELECT COUNT(1)
                       FROM jobs q
                       WHERE q.status = 'queued'
                         AND (
                             q.created_at < j.created_at
                             OR (q.created_at = j.created_at AND q.rowid <= j.rowid)
                         )
                   ) END AS queue_position
            FROM jobs j{where}
            ORDER BY j.created_at DESC, j.rowid DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        rows = await rows_cursor.fetchall()
        await rows_cursor.close()
    total = int(count_row[0]) if count_row is not None else 0
    jobs = [
        JobSummary(
            job_id=row["job_id"],
            status=JobStatus(row["status"]),
            target_url=row["target_url"],
            error=row["error"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            duration_seconds=_duration_seconds(row["created_at"], row["finished_at"]),
            queue_position=row["queue_position"],
            result_metadata=_result_metadata(row),
        )
        for row in rows
    ]
    return JobListResponse(jobs=jobs, total=total, limit=limit, offset=offset)


@app.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    log_job_id = sanitize_log_value(job_id)
    logger.debug("Fetching job status for job_id=%s", log_job_id)
    row = await db.fetch_one("SELECT rowid, * FROM jobs WHERE job_id = ?", (job_id,))
    if row is None:
        logger.warning("Job lookup failed, not found: job_id=%s", log_job_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    sitemap = None
    result_metadata = _result_metadata(row)
    if row["status"] == JobStatus.completed.value:
        logger.debug("Job %s is completed, reading persisted sitemap", log_job_id)
        sitemap, result_metadata = await _read_completed_sitemap(row)

    return JobResponse(
        job_id=row["job_id"],
        status=JobStatus(row["status"]),
        target_url=row["target_url"],
        scope_config=db.loads_json(row["scope_config"]),
        auth_config=db.loads_json(row["auth_config"]),
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        duration_seconds=_duration_seconds(row["created_at"], row["finished_at"]),
        queue_position=await _queue_position(row),
        generated_exclusions=db.loads_json(row["generated_exclusions"]),
        result_metadata=result_metadata,
        sitemap=sitemap,
    )


@app.post("/jobs/{job_id}/cancel", response_model=JobCancelResponse)
async def cancel_job(job_id: str) -> JobCancelResponse:
    log_job_id = sanitize_log_value(job_id)
    logger.info("Received cancel request for job_id=%s", log_job_id)
    row = await db.fetch_one("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
    if row is None:
        logger.warning("Cancel request failed, job not found: job_id=%s", log_job_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    status_value = row["status"]
    if status_value in TERMINAL_JOB_STATUSES:
        cancellation_status = (
            CancellationStatus.completed
            if status_value == JobStatus.cancelled.value
            else CancellationStatus.not_needed
        )
        return JobCancelResponse(
            job_id=job_id,
            status=JobStatus(status_value),
            cancellation_status=cancellation_status,
        )

    if status_value in {JobStatus.queued.value, JobStatus.pending.value}:
        if await orchestrator.cancel_queued_job(job_id):
            logger.info(
                "Cancelled %s job job_id=%s",
                sanitize_log_value(status_value),
                log_job_id,
            )
            return JobCancelResponse(
                job_id=job_id,
                status=JobStatus.cancelled,
                cancellation_status=CancellationStatus.completed,
            )
        row = await db.fetch_one("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        status_value = row["status"]
        if status_value in TERMINAL_JOB_STATUSES:
            cancellation_status = (
                CancellationStatus.completed
                if status_value == JobStatus.cancelled.value
                else CancellationStatus.not_needed
            )
            return JobCancelResponse(
                job_id=job_id,
                status=JobStatus(status_value),
                cancellation_status=cancellation_status,
            )

    requested = await orchestrator.request_cancel(job_id)
    if not requested:
        row = await db.fetch_one("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
        if row is not None and row["status"] in TERMINAL_JOB_STATUSES:
            terminal_status = JobStatus(row["status"])
            return JobCancelResponse(
                job_id=job_id,
                status=terminal_status,
                cancellation_status=(
                    CancellationStatus.completed
                    if terminal_status is JobStatus.cancelled
                    else CancellationStatus.not_needed
                ),
            )
        logger.warning(
            "Cancel request had no in-memory runner for job_id=%s",
            log_job_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job runner is unavailable; cancellation was not recorded",
        )

    return JobCancelResponse(
        job_id=job_id,
        status=JobStatus(status_value),
        cancellation_status=CancellationStatus.requested,
    )


def cli() -> None:
    """Entrypoint for the ``crawler-server`` console script."""
    import uvicorn

    host = os.environ.get("CRAWLER_HOST", "0.0.0.0")
    port = int(os.environ.get("CRAWLER_PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port)
