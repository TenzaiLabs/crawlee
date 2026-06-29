from __future__ import annotations

import asyncio
import contextlib
import copy
import faulthandler
import logging
import os
import shutil
import signal
import traceback
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from . import db, orchestrator, parser  # noqa: E402
from .auth_config import AuthConfigValidationError, validate_auth_config  # noqa: E402
from .common import sanitize_log_value  # noqa: E402
from .job_status import (  # noqa: E402
    ACTIVE_JOB_STATUSES,
    INTERRUPTED_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
)
from .models import (  # noqa: E402
    JobCancelResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JobResponse,
    JobStatus,
)
from .scope_config import ScopeConfigValidationError, validate_scope_config  # noqa: E402
from .settings import (  # noqa: E402
    CRAWLER_COMPLETED_SITEMAP_CACHE_ENABLED,
    CRAWLER_COMPLETED_SITEMAP_CACHE_MAX_ENTRIES,
)

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

_completed_sitemap_cache: OrderedDict[tuple[str, str | None], dict[str, Any]] = OrderedDict()
_completed_sitemap_cache_lock = asyncio.Lock()


async def _read_completed_sitemap(
    job_id: str,
    target_url: str,
    finished_at: str | None,
) -> dict[str, Any]:
    if not CRAWLER_COMPLETED_SITEMAP_CACHE_ENABLED:
        return await asyncio.to_thread(parser.parse_log, job_id, target_url)

    cache_key = (job_id, finished_at)
    async with _completed_sitemap_cache_lock:
        cached = _completed_sitemap_cache.get(cache_key)
        if cached is not None:
            _completed_sitemap_cache.move_to_end(cache_key)
            logger.debug("Sitemap cache hit for completed job_id=%s", sanitize_log_value(job_id))
            return copy.deepcopy(cached)

    sitemap = await asyncio.to_thread(parser.parse_log, job_id, target_url)
    async with _completed_sitemap_cache_lock:
        _completed_sitemap_cache[cache_key] = sitemap
        _completed_sitemap_cache.move_to_end(cache_key)
        while len(_completed_sitemap_cache) > CRAWLER_COMPLETED_SITEMAP_CACHE_MAX_ENTRIES:
            _completed_sitemap_cache.popitem(last=False)
    return copy.deepcopy(sitemap)


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
    async with _completed_sitemap_cache_lock:
        _completed_sitemap_cache.clear()
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
async def list_jobs() -> JobListResponse:
    logger.debug("Listing active jobs")
    placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
    rows = await db.fetch_all(
        f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
        tuple(ACTIVE_JOB_STATUSES),
    )
    jobs = [
        JobResponse(
            job_id=row["job_id"],
            status=JobStatus(row["status"]),
            target_url=row["target_url"],
            scope_config=db.loads_json(row["scope_config"]),
            auth_config=db.loads_json(row["auth_config"]),
            error=row["error"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            generated_exclusions=db.loads_json(row["generated_exclusions"]),
        )
        for row in rows
    ]
    return JobListResponse(jobs=jobs)


@app.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    log_job_id = sanitize_log_value(job_id)
    logger.debug("Fetching job status for job_id=%s", log_job_id)
    row = await db.fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if row is None:
        logger.warning("Job lookup failed, not found: job_id=%s", log_job_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    sitemap = None
    if row["status"] == JobStatus.completed.value:
        logger.debug("Job %s is completed, reading sitemap", log_job_id)
        sitemap = await _read_completed_sitemap(job_id, row["target_url"], row["finished_at"])

    return JobResponse(
        job_id=row["job_id"],
        status=JobStatus(row["status"]),
        target_url=row["target_url"],
        scope_config=db.loads_json(row["scope_config"]),
        auth_config=db.loads_json(row["auth_config"]),
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        generated_exclusions=db.loads_json(row["generated_exclusions"]),
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
        return JobCancelResponse(job_id=job_id, status=JobStatus(status_value))

    if status_value in {JobStatus.queued.value, JobStatus.pending.value}:
        await orchestrator.update_job_status(job_id, JobStatus.cancelled)
        logger.info(
            "Cancelled %s job job_id=%s",
            sanitize_log_value(status_value),
            log_job_id,
        )
        return JobCancelResponse(job_id=job_id, status=JobStatus.cancelled)

    requested = await orchestrator.request_cancel(job_id)
    if not requested:
        await orchestrator.update_job_status(job_id, JobStatus.cancelled)
        logger.warning(
            "Cancel request had no in-memory event, force-marked cancelled for job_id=%s",
            log_job_id,
        )
        return JobCancelResponse(job_id=job_id, status=JobStatus.cancelled)

    return JobCancelResponse(job_id=job_id, status=JobStatus(status_value))


def cli() -> None:
    """Entrypoint for the ``crawler-server`` console script."""
    import uvicorn

    host = os.environ.get("CRAWLER_HOST", "0.0.0.0")
    port = int(os.environ.get("CRAWLER_PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port)
