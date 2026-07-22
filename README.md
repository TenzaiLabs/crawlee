# Tenzai Crawler

[![CI](https://github.com/TenzaiLabs/crawlee/actions/workflows/ci.yml/badge.svg)](https://github.com/TenzaiLabs/crawlee/actions/workflows/ci.yml)
[![Pages](https://github.com/TenzaiLabs/crawlee/actions/workflows/pages.yml/badge.svg)](https://github.com/TenzaiLabs/crawlee/actions/workflows/pages.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-00a8bf)](https://tenzailabs.github.io/crawlee/)
[![Python](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-see%20LICENSE.md-lightgrey)](LICENSE.md)
[![Security](https://img.shields.io/badge/security-policy-00a8bf)](SECURITY.md)

Tenzai Crawler is an async-native FastAPI service for crawling websites with Katana and Proxify. It stores job state in SQLite, writes request logs to disk, and can perform pre-crawl authentication with either operator-supplied headers or a Playwright-driven LLM auth agent.

The service runs as a single-job worker. Jobs are accepted through the API, stored as queued records, and drained serially because Proxify binds the fixed local proxy address `127.0.0.1:8888`.

## Architecture

- **API**: FastAPI endpoints create, inspect, list, and cancel crawl jobs.
- **Job store**: SQLite stores target URLs, status, scope config, auth config, errors, and timestamps.
- **Orchestrator**: owns job status transitions, starts/stops Proxify, runs authentication when required, invokes Katana, and processes completed logs.
- **Authentication**: header-only configs are passed directly to the crawler; credential or login-url configs run the LLM auth agent before crawl.
- **Crawler**: Katana receives target URLs, authenticated headers, extra seed URLs, scope limits, and exclusion patterns. The crawler remains auth-agnostic.
- **Parser**: Proxify and Katana JSONL logs are normalized into a completed-job sitemap.

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
uv run tenzai-crawler-server
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
uv run pre-commit run -a
uv run pytest -q
uv run ruff check app tests scripts --fix
uv run ruff format app tests scripts
uv run ty check
```

Install the commit hooks locally with `uv run pre-commit install`.

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
uv run tenzai-crawler create https://example.com
uv run tenzai-crawler list
uv run tenzai-crawler status <job_id>
uv run tenzai-crawler cancel <job_id>
```

The previous `crawler` and `crawler-server` entry points remain available as compatibility aliases.

## Docs Website

The static docs website lives in `docs/`:

- `docs/index.html` — one-page overview.
- `docs/docs.html` — simple usage and architecture docs.

The GitHub Pages workflow publishes that directory from `main`; after Pages is enabled for the repository, the site is available at `https://tenzailabs.github.io/crawlee/`.

## Project Policies

- See `SECURITY.md` for vulnerability reporting, security boundaries, and disclosure expectations.
- See `CONTRIBUTING.md` for local development, test, docs, and pull request expectations.

Global options:

- `--base-url` (default `http://localhost:8000`)
- `--timeout` (default `30` seconds)

Pass `scope_config` via `--scope-config-json` / `--scope-config-file`, or use shorthand flags like `--headless` and `--cdp-url`. When combined, flags override matching keys.

## Authentication Usage

Authentication is optional. `auth_config` controls which mode runs.

### Manual-header mode

Use header-only `auth_config` when the operator already has headers or cookies. This mode does not run the LLM auth agent.

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

Use `credentials` and/or `login_url` when the service should log in before crawling. The auth agent uses Playwright browser controls exposed as structured tools, including page/frame element refs, popup handling, iframe interaction, explicit authentication verification, blocked URL recording, and TOTP code generation.

```json
{
  "target_url": "https://example.com",
  "auth_config": {
    "login_url": "https://example.com/login",
    "credentials": {
      "email": "{{env:APP_EMAIL}}",
      "password": "{{env:APP_PASSWORD}}",
      "totp_secret": "{{env:APP_TOTP_SECRET}}"
    },
    "instructions": "Login and stop once the dashboard is visible. If MFA is requested, use get_totp_code(\"totp_secret\").",
    "success_indicator": "Dashboard"
  }
}
```

- AI auth runs only when `credentials` or `login_url` is present; header-only config never triggers it.
- Auth browsing is direct through Playwright; Proxify is used for the Katana crawl path.
- The auth agent must verify access to authenticated content before returning a session.
- Cookies, captured auth headers, and the authenticated landing URL are passed to Katana.
- Unsafe URLs detected during auth, such as logout or destructive actions, can be recorded and converted into Katana exclusion patterns.
- Secret templates `{{env:VAR}}`, `{{totp:VAR}}`, and `{{totp_seed:SECRET}}` are resolved only in memory before auth.
- `auth_config.api_key` is rejected; use `api_key_env` instead.

From the CLI, use `--auth-header` for manual headers or `--auth-config-json` / `--auth-config-file` for full config. `--auth-login-url` sets the login URL. Flags override matching keys when combined with JSON/file config.

## Local Auth Testsites

The `testsites/` stack provides local fixtures for public sites, manual-header auth, and LLM-driven auth flows:

- Basic form login
- Complex and dynamic forms
- Multi-step login
- Popup/new-window login
- Iframe login
- XSRF token login
- Delay-after-submit login
- Challenge/captcha-style login with supplied answers
- Security question login
- TOTP/MFA login
- HTTP Basic and Bearer-token header auth

Start the fixtures:

```bash
cd testsites
docker compose up -d --build
```

Run the standalone auth agent against the fixtures:

```bash
uv run python -m scripts.run_auth_agent_tests --timeout 30
```

Run full crawler jobs against the auth fixtures:

```bash
uv run python -m scripts.run_crawler_auth_tests --crawl-duration 25s --job-timeout 180
```

Stop the fixtures:

```bash
cd testsites
docker compose down
```

The crawler auth runner uses temporary DB/log paths by default. Use `--case`, `--mode`, `--gateway`, `--db-path`, and `--log-dir` to narrow or persist a run.

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `CRAWLER_HOST` | `0.0.0.0` | Bind address |
| `CRAWLER_PORT` | `8000` | Bind port |
| `CRAWLER_DB_PATH` | `/data/jobs.db` | SQLite database path |
| `CRAWLER_LOG_DIR` | `/data/logs` | Log output directory |
| `CRAWLER_AUTH_MODEL` | `gpt-5.4-nano` | LLM model for AI auth |
| `CRAWLER_AUTH_ATTEMPTS` | `3` | Auth retry attempts |
| `CRAWLER_AUTH_MAX_STEPS` | `85` | Default max tool-calling steps for auth |
| `CRAWLER_SUBPROCESS_TIMEOUT` | `60` | Subprocess timeout (seconds) |
| `CRAWLER_ENABLE_DEBUG_ENDPOINTS` | off | Set `1` to enable debug routes |

## Output

Completed jobs expose a `sitemap` on `GET /jobs/<job_id>` with:

- **`entries`** — flat list of observed HTTP requests (`method`, `url`, `status`, `content_type`, `timestamp`), deduplicated by `(method, url)`, scoped to the target domain.
- **`tree`** — the same entries organized into a path-segment hierarchy (`children`, `pages`) for tree-style rendering.

Log artifacts are written to `$CRAWLER_LOG_DIR`: `{job_id}.jsonl` (Proxify) and `{job_id}.jsonl.katana` (Katana sidecar).

Katana can emit both response-bearing records and request-only records for the same URL. Completed sitemaps preserve response status and content type when duplicate records are normalized.

## Deployment

Apply Kubernetes manifests:

```bash
kubectl apply -f k8s/statefulset.yaml
kubectl apply -f k8s/service.yaml
```

## Known Limitations

- Authorization extraction from browser/proxy traffic is heuristic and may miss unusual record shapes.
- The LLM auth agent depends on the configured model, the quality of page accessibility data, and the supplied operator instructions for unusual flows.
- TOTP is supported through an explicit auth-agent tool; other out-of-band MFA methods require additional tooling or operator-specific instructions.
- Cancellation checks run in preflight/callbacks and long subprocess boundaries, not at the top of every tool function body.
- Kubernetes manifests do not include explicit LLM provider env wiring by default.

## Security Posture

Tenzai Crawler is designed for trusted operators. If exposed broadly, add API authentication/authorization and egress controls.
