# Crawler

Crawler is an async-native FastAPI service that crawls websites through Katana + Proxify, stores job state in SQLite, and optionally performs pre-crawl login with Playwright + LLM tool-calling.

The service executes one job at a time because Proxify binds `127.0.0.1:8888`. Additional jobs are accepted and queued, then drained serially by a background worker.

## Requirements

- Python `3.14`
- `uv`
- `katana` and `proxify` in `PATH`

## Setup For Usage

Install runtime dependencies:

```bash
uv sync --extra test
```

Run the API:

```bash
uv run crawler-server
```

The server binds `0.0.0.0:8000` by default. Override with `CRAWLER_HOST` and `CRAWLER_PORT`.

The service validates `katana` and `proxify` at startup and exits if either is missing.

## Setup For Development

Install full dev dependencies:

```bash
uv sync --extra test --extra dev
```

Run checks:

```bash
uv run pytest -q
uv run ruff check app tests --fix
uv run ruff format app tests
uv run ty check
```

Optional E2E scenario:

```bash
RUN_E2E=1 uv run pytest -q tests/test_scenarios.py
```

## Quick API Usage

Create a crawl job:

```bash
curl -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"target_url":"https://example.com"}'
```

List active and queued jobs:

```bash
curl http://localhost:8000/jobs
```

Get job status/result:

```bash
curl http://localhost:8000/jobs/<job_id>
```

Cancel a job:

```bash
curl -X POST http://localhost:8000/jobs/<job_id>/cancel
```

## Quick CLI Usage

The CLI is a wrapper over the same API endpoints.

Basic commands:

```bash
uv run crawler create https://example.com
uv run crawler list
uv run crawler status <job_id>
uv run crawler cancel <job_id>
```

Global options:

- `--base-url` (default `http://localhost:8000`)
- `--timeout` (default `30` seconds)

Pass `scope_config` via `--scope-config-json` / `--scope-config-file`, or use shorthand flags like `--headless` and `--cdp-url`. When combined, flags override matching keys.

## Authentication Usage

### Manual-header mode

Use header-only `auth_config` to skip AI auth:

```json
{
  "target_url": "https://example.com",
  "auth_config": {
    "headers": [
      "Authorization: Bearer $TOKEN",
      "Cookie: session=abc"
    ]
  }
}
```

### AI-auth mode

Use `credentials` and/or `login_url` to run auth before crawl:

```json
{
  "target_url": "https://example.com",
  "auth_config": {
    "login_url": "https://example.com/login",
    "credentials": {
      "email": "{{env:APP_EMAIL}}",
      "password": "{{env:APP_PASSWORD}}"
    },
    "instructions": "Login and stop once the dashboard is visible.",
    "success_indicator": "Dashboard"
  }
}
```

- AI auth runs only when `credentials` or `login_url` is present; header-only config never triggers it.
- Sensitive credential fields must use `{{env:VAR}}` / `{{totp:VAR}}`; plaintext secrets are rejected.
- `auth_config.api_key` is rejected; use `api_key_env` instead.

From the CLI, use `--auth-header` for manual headers or `--auth-config-json` / `--auth-config-file` for full config. `--auth-login-url` sets the login URL. Flags override matching keys when combined with JSON/file config.

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `CRAWLER_HOST` | `0.0.0.0` | Bind address |
| `CRAWLER_PORT` | `8000` | Bind port |
| `CRAWLER_DB_PATH` | `/data/jobs.db` | SQLite database path |
| `CRAWLER_LOG_DIR` | `/data/logs` | Log output directory |
| `CRAWLER_AUTH_MODEL` | `gpt-5-mini` | LLM model for AI auth |
| `CRAWLER_SUBPROCESS_TIMEOUT` | `60` | Subprocess timeout (seconds) |
| `CRAWLER_ENABLE_DEBUG_ENDPOINTS` | off | Set `1` to enable debug routes |

## Output

Completed jobs expose a `sitemap` on `GET /jobs/<job_id>` with:

- **`entries`** — flat list of observed HTTP requests (`method`, `url`, `status`, `content_type`, `timestamp`), deduplicated by `(method, url)`, scoped to the target domain.
- **`tree`** — the same entries organized into a path-segment hierarchy (`children`, `pages`) for tree-style rendering.

Log artifacts are written to `$CRAWLER_LOG_DIR`: `{job_id}.jsonl` (Proxify) and `{job_id}.jsonl.katana` (Katana sidecar).

## Deployment

Apply Kubernetes manifests:

```bash
kubectl apply -f k8s/statefulset.yaml
kubectl apply -f k8s/service.yaml
```

## Known Limitations

- Authorization extraction from Proxify logs is heuristic and may miss unusual record shapes.
- `success_indicator` is prompt context and not a hard success gate.
- Cancellation checks run in preflight/callbacks, not at the top of every tool function body.
- Kubernetes manifests do not include explicit LLM provider env wiring by default.

## Security Posture

Crawler is designed for trusted operators. If exposed broadly, add API authentication/authorization and egress controls.
