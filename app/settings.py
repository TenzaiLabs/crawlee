from __future__ import annotations

import os

from .common import coerce_int


def _env_int(name: str, fallback: int, *, minimum: int | None = None) -> int:
    value = coerce_int(os.getenv(name), fallback)
    if minimum is not None and value < minimum:
        return fallback
    return value


def _env_float(name: str, fallback: float, *, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else fallback
    except ValueError:
        return fallback
    if minimum is not None and value < minimum:
        return fallback
    return value


CRAWLER_SUBPROCESS_TIMEOUT_SECONDS = _env_int("CRAWLER_SUBPROCESS_TIMEOUT", 720, minimum=1)
CRAWLER_SUBPROCESS_POLL_INTERVAL_SECONDS = _env_float(
    "CRAWLER_SUBPROCESS_POLL_INTERVAL", 0.5, minimum=0.1
)
CRAWLER_SUBPROCESS_GRACE_SECONDS = _env_float("CRAWLER_SUBPROCESS_GRACE_SECONDS", 0.5, minimum=0.1)
CRAWLER_KATANA_PROCESS_TIMEOUT_SECONDS = _env_float(
    "CRAWLER_KATANA_PROCESS_TIMEOUT_SECONDS", 720.0, minimum=1.0
)
CRAWLER_BROWSER_STARTUP_TIMEOUT_SECONDS = _env_float(
    "CRAWLER_BROWSER_STARTUP_TIMEOUT_SECONDS", 20.0, minimum=0.1
)

CRAWLER_AUTH_ATTEMPTS = _env_int("CRAWLER_AUTH_ATTEMPTS", 3, minimum=1)
CRAWLER_AUTH_TIMEOUT_SECONDS = _env_float("CRAWLER_AUTH_TIMEOUT_SECONDS", 300.0, minimum=0.1)
CRAWLER_AUTH_RETRY_BASE_SECONDS = _env_float("CRAWLER_AUTH_RETRY_BASE_SECONDS", 1.0, minimum=0.1)
CRAWLER_AUTH_MAX_STEPS_DEFAULT = _env_int("CRAWLER_AUTH_MAX_STEPS", 85, minimum=1)
CRAWLER_AUTH_SCAN_MAX_BYTES = _env_int("CRAWLER_AUTH_SCAN_MAX_BYTES", 1_000_000, minimum=1)
CRAWLER_DISCOVERY_MAX_MODEL_TURNS = _env_int("CRAWLER_DISCOVERY_MAX_MODEL_TURNS", 40, minimum=1)
CRAWLER_DISCOVERY_MAX_STATES = _env_int("CRAWLER_DISCOVERY_MAX_STATES", 120, minimum=1)
CRAWLER_DISCOVERY_TIMEOUT_SECONDS = _env_float(
    "CRAWLER_DISCOVERY_TIMEOUT_SECONDS", 300.0, minimum=1.0
)
CRAWLER_JOB_TIMEOUT_SECONDS = _env_float("CRAWLER_JOB_TIMEOUT_SECONDS", 3600.0, minimum=1.0)
CRAWLER_JOB_MEMORY_LIMIT_BYTES = _env_int(
    "CRAWLER_JOB_MEMORY_LIMIT_BYTES",
    2 * 1024 * 1024 * 1024,
    minimum=1,
)
