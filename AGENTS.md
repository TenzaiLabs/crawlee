# Agent Guidelines

This repository is an async-native FastAPI crawler service that runs as a single-replica StatefulSet.

## Runtime + Platform

- Python: 3.14
- Dependency management: `uv` (do not add `requirements.txt`)
- Data paths:
  - DB: `/data/jobs.db` (`CRAWLER_DB_PATH`)
  - Logs: `/data/logs` (`CRAWLER_LOG_DIR`)
- Required binaries in `PATH`: `katana`, `proxify`

## Local Workflow

- Install deps: `uv sync --extra test --extra dev`
- Run server: `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Run tests: `uv run pytest -q`
- Lint/format:
  - `uv run ruff check app tests --fix`
  - `uv run ruff format app tests`
- Type-check: `uv run ty check`
- Pre-commit: `uv run pre-commit run -a`

### End-to-End Test

- E2E is opt-in: `RUN_E2E=1 uv run pytest -q tests/test_scenarios.py`
- Requires `katana` + `proxify` in `PATH`

## Core Engineering Rules

- Keep the service async-native; avoid blocking I/O on the event loop
- Preserve the single-job constraint (proxy binds `127.0.0.1:8888`).
- Never persist plaintext secrets; only store env-var references like `{{env:VAR_NAME}}`
- Keep logging simple (no metrics/structured logging for v1)

## Auth Architecture Guardrails

- Orchestrator is the hub: it decides auth vs crawl flow and owns status transitions
- Run AI auth only when `auth_config` has `credentials` or `login_url`
- Header-only `auth_config` is manual-header mode and must not trigger AI auth
- Keep auth interfaces stable:
  - `authenticate(target_url, auth_config, cancel_event) -> AuthResult`
  - `resolve_secrets(auth_config) -> dict`
  - `needs_auth(auth_config) -> bool`
- Keep crawler auth-agnostic: it only accepts headers and must not depend on auth internals

## Deployment + Security

- Treat as trusted-operator tooling by default; if exposed broadly, add API auth + egress controls
- Keep debug endpoints off unless explicitly needed (`CRAWLER_ENABLE_DEBUG_ENDPOINTS=1`)
- Prefer env-var secret references over plaintext `auth_config` values

## Code Quality Expectations

- Prefer small modules and explicit, testable functions
- Add unit tests for new behavior; avoid brittle network-dependent tests
- When integrating Katana/Proxify/Playwright, validate flags against pinned versions
- Put substantial engineering/security writeups in `docs/`
