from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class JobStatus(StrEnum):
    queued = "queued"
    pending = "pending"
    authenticating = "authenticating"
    crawling = "crawling"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    failed_interrupted = "failed_interrupted"
    cancelled = "cancelled"


class CancellationStatus(StrEnum):
    requested = "requested"
    completed = "completed"
    not_needed = "not_needed"


class JobCreateRequest(BaseModel):
    target_url: HttpUrl
    scope_config: dict[str, Any] | None = None
    auth_config: dict[str, Any] | None = None


class JobResultMetadata(BaseModel):
    entry_count: int
    size_bytes: int


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    target_url: HttpUrl
    scope_config: dict[str, Any] | None = None
    auth_config: dict[str, Any] | None = None
    error: str | None = None
    created_at: str
    finished_at: str | None = None
    duration_seconds: float | None = None
    queue_position: int | None = None
    generated_exclusions: dict[str, Any] | None = None
    result_metadata: JobResultMetadata | None = None
    sitemap: dict[str, Any] | None = None


class JobCreateResponse(BaseModel):
    job_id: str = Field(..., description="Unique job identifier")


class JobSummary(BaseModel):
    job_id: str
    status: JobStatus
    target_url: HttpUrl
    error: str | None = None
    created_at: str
    finished_at: str | None = None
    duration_seconds: float | None = None
    queue_position: int | None = None
    result_metadata: JobResultMetadata | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
    total: int
    limit: int
    offset: int


class JobCancelResponse(BaseModel):
    job_id: str
    status: JobStatus
    cancellation_status: CancellationStatus
