# Agent Guidelines

Python 3.14 async FastAPI service managed with `uv`.

## Verify

```bash
uv run pytest -q
uv run ruff check app tests scripts
uv run ty check
```

E2E is opt-in (`RUN_E2E=1`) and requires `katana` in `PATH`.

## Guardrails

- Keep I/O async and preserve the single-job design.
- Treat persisted `auth_config` values and job-status responses as sensitive. Environment
  references such as `{{env:VAR_NAME}}` are recommended, but plaintext values are supported.
- The orchestrator owns auth/crawl flow and status transitions.
- Run AI auth only for `auth_config` containing `credentials` or `login_url`; headers alone are manual-header mode.
- Keep the crawler auth-agnostic: it accepts headers, not auth internals.
- Add deterministic unit tests for behavior changes; avoid network-dependent tests.

Runtime data defaults to `/data/jobs.db` and `/data/logs`.
