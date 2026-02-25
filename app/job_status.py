from __future__ import annotations

from .models import JobStatus

ACTIVE_JOB_STATUSES = {
    JobStatus.queued.value,
    JobStatus.pending.value,
    JobStatus.authenticating.value,
    JobStatus.crawling.value,
    JobStatus.processing.value,
}

TERMINAL_JOB_STATUSES = {
    JobStatus.completed.value,
    JobStatus.failed.value,
    JobStatus.failed_interrupted.value,
    JobStatus.cancelled.value,
}

INTERRUPTED_JOB_STATUSES = {
    JobStatus.queued.value,
    JobStatus.authenticating.value,
    JobStatus.crawling.value,
    JobStatus.processing.value,
}
