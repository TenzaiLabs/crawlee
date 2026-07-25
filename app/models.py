from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class JobStatus(StrEnum):
    queued = "queued"
    pending = "pending"
    authenticating = "authenticating"
    crawling = "crawling"
    discovering = "discovering"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    failed_interrupted = "failed_interrupted"
    cancelled = "cancelled"


class CancellationStatus(StrEnum):
    requested = "requested"
    completed = "completed"
    not_needed = "not_needed"


class DiscoveryOutcome(StrEnum):
    fixpoint = "fixpoint"
    disabled = "disabled"
    budget_exhausted = "budget_exhausted"
    partial_failure = "partial_failure"
    interrupted = "interrupted"


class ResultCompleteness(StrEnum):
    complete = "complete"
    partial = "partial"


class DiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_rounds: int = Field(default=3, ge=1, le=3)
    max_actions: int = Field(default=100, ge=1, le=100)
    max_llm_pages: int = Field(default=25, ge=1, le=25)


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: DiscoveryOutcome
    rounds: int = Field(ge=0)
    new_entry_count: int = Field(ge=0)
    state_count: int = Field(ge=0)
    workflow_count: int = Field(ge=0)
    stop_reason: str


class JobCreateRequest(BaseModel):
    target_url: HttpUrl
    scope_config: dict[str, Any] | None = None
    auth_config: dict[str, Any] | None = None
    discovery: DiscoveryConfig | None = None


class JobResultMetadata(BaseModel):
    entry_count: int
    size_bytes: int
    completeness: ResultCompleteness = ResultCompleteness.complete
    warnings: list[str] = Field(default_factory=list)


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    target_url: HttpUrl
    scope_config: dict[str, Any] | None = None
    auth_config: dict[str, Any] | None = None
    discovery: DiscoveryConfig
    error: str | None = None
    created_at: str
    finished_at: str | None = None
    duration_seconds: float | None = None
    queue_position: int | None = None
    generated_exclusions: dict[str, Any] | None = None
    discovery_result: DiscoveryResult | None = None
    evidence: dict[str, Any] | None = None
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
