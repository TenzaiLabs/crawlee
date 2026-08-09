# Tenzai Crawler

[![CI](https://github.com/TenzaiLabs/crawlee/actions/workflows/ci.yml/badge.svg)](https://github.com/TenzaiLabs/crawlee/actions/workflows/ci.yml)
[![Pages](https://github.com/TenzaiLabs/crawlee/actions/workflows/pages.yml/badge.svg)](https://github.com/TenzaiLabs/crawlee/actions/workflows/pages.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-00a8bf)](https://tenzailabs.github.io/crawlee/)
[![Python](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-see%20LICENSE.md-lightgrey)](LICENSE.md)
[![Security](https://img.shields.io/badge/security-policy-00a8bf)](SECURITY.md)

Tenzai Crawler is an async-native FastAPI service for crawling websites with Katana. It stores job state in SQLite, writes per-lane Katana JSONL artifacts, can authenticate through an operator-supplied header or Playwright-driven LLM flow, and uses browser-guided discovery to expose workflows that an ordinary crawl misses.

The service runs as a single-job worker. Jobs are accepted through the API, stored as queued records, and drained serially.

Read the [concept overview](docs/concept.html) or the DEFCON presentation,
[Improving crawling with an LLM (PDF)](docs/ImprovingCrawlingWithAnLLM.pdf), for
the thesis behind the design.

## Architecture

- **API**: FastAPI endpoints create, inspect, list, and cancel crawl jobs.
- **Job store**: SQLite stores target URLs, status, scope config, auth config, errors, and timestamps.
- **Orchestrator**: owns job status transitions, a job-scoped Chromium profile, bounded known-file discovery, both Katana lanes, browser-guided discovery, checkpoints, and cleanup.
- **Authentication**: header-only configs are passed directly to the crawler; credential or login-url configs run the LLM auth agent before crawl.
- **Crawler**: standard Katana handles static/JavaScript/form extraction; pure-headless Katana reuses the job Chrome through CDP for rendered behavior. The crawler remains auth-agnostic.
- **Browser discovery**: Playwright opens fresh pages after Katana,
  deterministically explores distinct same-document hash routes, lets the LLM
  exercise remaining workflow controls, and returns stable seeds to another
  two-lane Katana stage.
- **Parser**: controlled-fetch, Katana, and passive-CDP evidence is aggregated into a completed-job sitemap by exact `(method, URL)` identity.

Target certificate verification is intentionally disabled across controlled
fetches, Katana, and the shared Chrome so self-signed test applications remain
crawlable.

## Requirements

- Python `3.14`
- `uv`
- The source-pinned `v1.6.1`-derived Katana build in `PATH` (the container image installs and verifies the qualified artifact)

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

The service validates `katana` at startup and exits if it is missing.

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

List current and previously completed jobs (newest first):

```bash
curl http://localhost:8000/jobs
```

Filter or paginate job history:

```bash
curl 'http://localhost:8000/jobs?status=completed&limit=25&offset=0'
```

Get job status/result:

```bash
curl http://localhost:8000/jobs/<job_id>
```

Cancel a job:

```bash
curl -X POST http://localhost:8000/jobs/<job_id>/cancel
```

## CLI Usage

The CLI is an HTTP client for the same API endpoints. Start
`uv run tenzai-crawler-server` first; every CLI command prints its response as
JSON. Creating a job returns immediately with a `job_id` rather than waiting for
the crawl to finish.

### Connect to the server

Show the available commands and options:

```bash
uv run tenzai-crawler --help
uv run tenzai-crawler create --help
```

The global options must appear before the subcommand:

- `--base-url` selects the API server (default `http://localhost:8000`).
- `--timeout` controls each CLI-to-API request (default `30` seconds). It does
  not change the crawl or whole-job deadline.

For example, query a remote crawler server with a 60-second HTTP timeout:

```bash
uv run tenzai-crawler \
  --base-url http://crawler.example:8000 \
  --timeout 60 \
  list
```

The previous `crawler` and `crawler-server` entry points remain available as
compatibility aliases.

### Create a crawl job

Create a job with the default scope and browser-guided discovery settings:

```bash
uv run tenzai-crawler create https://example.com
```

The response contains the identifier used by the other commands:

```json
{
  "job_id": "<job_id>"
}
```

Pass per-job Katana limits as inline JSON:

```bash
uv run tenzai-crawler create https://example.com \
  --scope-config-json \
  '{"max_depth":4,"crawl_duration":"5m","exclude_filters":["/logout"]}'
```

For reusable configuration, put the same JSON object in a file:

```bash
uv run tenzai-crawler create https://example.com \
  --scope-config-file ./scope.json
```

`--scope-config-json` and `--scope-config-file` are mutually exclusive. Valid
scope keys are `max_depth`, `rate_limit`, `crawl_scope`, `exclude_filters`,
`exclude_regex`, `field_scope`, `concurrency`, `parallelism`,
`crawl_duration`, and `timeout`. Chrome and CDP settings are server-owned.

### Add authentication

For manual-header mode, repeat `--auth-header` as needed. Environment templates
are resolved by the server, so the referenced variables must exist in the
server process environment:

```bash
uv run tenzai-crawler create https://example.com \
  --auth-header 'Authorization: Bearer {{env:APP_TOKEN}}' \
  --auth-header 'X-Tenant: example'
```

For AI-auth mode, save a full configuration such as this as `auth.json`:

```json
{
  "login_url": "https://example.com/login",
  "credentials": {
    "email": "{{env:APP_EMAIL}}",
    "password": "{{env:APP_PASSWORD}}"
  },
  "instructions": "Sign in and stop when the dashboard is visible.",
  "success_indicator": "Dashboard"
}
```

Then create the job:

```bash
uv run tenzai-crawler create https://example.com \
  --auth-config-file ./auth.json
```

`--auth-config-json` accepts the same object inline. `--auth-login-url` and
`--auth-header` override the corresponding values supplied through JSON or a
file. Header-only configuration does not invoke the AI auth agent.

### Control browser-guided discovery

Discovery is enabled by default with server-capped limits of 3 rounds, 100
actions, and 25 LLM-selected pages. Lower those per-job budgets with:

```bash
uv run tenzai-crawler create https://example.com \
  --discovery-max-rounds 2 \
  --discovery-max-actions 40 \
  --discovery-max-llm-pages 10
```

Run only the deterministic two-lane Katana baseline with:

```bash
uv run tenzai-crawler create https://example.com --disable-discovery
```

### Inspect and cancel jobs

Use the `job_id` returned by `create`:

```bash
uv run tenzai-crawler status <job_id>
uv run tenzai-crawler list
uv run tenzai-crawler list --status completed --limit 25 --offset 0
uv run tenzai-crawler cancel <job_id>
```

`status` returns the persisted sitemap when a job completes. A cancelled job
also returns a sitemap if it reached a valid baseline or discovery checkpoint.
The service runs one job at a time, so `list` and `status` expose queue positions
for waiting jobs.

## Docs Website

The static docs website lives in `docs/`:

- `docs/index.html` — one-page overview.
- `docs/docs.html` — simple usage and architecture docs.

The GitHub Pages workflow publishes that directory from `main`; after Pages is enabled for the repository, the site is available at `https://tenzailabs.github.io/crawlee/`.

## Project Policies

- See `SECURITY.md` for vulnerability reporting, security boundaries, and disclosure expectations.
- See `CONTRIBUTING.md` for local development, test, docs, and pull request expectations.

## Authentication Usage

Authentication is optional. `auth_config` controls which mode runs.

### Manual-header mode

Use header-only `auth_config` when the operator already has headers or cookies. This mode does not run the LLM auth agent.

The service persists the submitted `auth_config` and returns it from the job-status API. Use
environment references when the job record should not contain the resolved credential, and protect
the jobs database and API as sensitive if plaintext values are submitted.

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
- Auth browsing is direct through Playwright; Katana also connects to targets directly.
- The auth agent must verify access to authenticated content before returning a session.
  Hash/history routes are preserved, and credentialed auth is compared with a
  temporary clean context in the same Chrome instance. A public `200`, cookies,
  storage, or a changed URL alone is not sufficient evidence.
- Cookies, captured auth headers, and the authenticated landing URL are passed to Katana.
- URLs the auth agent considers unsafe may be recorded as evidence, but are not converted into Katana exclusions. Operator-supplied scope exclusions remain explicit inputs.
- Secret templates `{{env:VAR}}`, `{{totp:VAR}}`, and `{{totp_seed:SECRET}}` are resolved only in memory before auth.
- `auth_config.api_key` is rejected; use `api_key_env` instead.

From the CLI, use `--auth-header` for manual headers or `--auth-config-json` / `--auth-config-file` for full config. `--auth-login-url` sets the login URL. Flags override matching keys when combined with JSON/file config.

## Manual Browser-Discovery Testing

The browser-guided workflow is ready for manual testing against the repository
fixtures. The authoritative automated qualification uses the same deployed
boundary described here: start external targets, run a real
`tenzai-crawler-server`, submit through HTTP, poll the persisted job, and inspect
the returned sitemap. It does not use an in-process FastAPI harness.

Reproduce the repeated live-model qualification with
`uv run python -m scripts.run_browser_discovery_qualification`. Its generated
report and JSON result are local artifacts and are not versioned.

### 1. Check prerequisites

Make sure `katana` and Docker are available:

```bash
katana -version
docker version
```

On Windows with WSL, if `docker` is missing, enable the current distribution in
Docker Desktop under **Settings → Resources → WSL Integration**, then reopen the
shell.

Export the provider API key required by the configured auth and discovery
models. Keep the value in the shell environment; do not put it in a job payload
or configuration file.

### 2. Start the fixture websites

From the repository root, generate an ephemeral fixture token and start all 21
repository targets:

```bash
export TEST_HARNESS_TOKEN="$(openssl rand -hex 16)"
docker compose -f testsites/docker-compose.yml up -d --build
```

The most useful browser-discovery targets are:

| Fixture | URL | What it exercises |
| --- | --- | --- |
| Site B | `http://localhost:8002` | LLM authentication and multi-step workflow discovery |
| Site E | `http://localhost:8005` | Crawl-trap resistance and bounded guided interaction |
| Site F | `http://localhost:8006` | SPA controls, runtime requests, modal state, and report preview |
| Site G | `http://localhost:8007` | Both Katana lanes, CDP evidence, scoped headers, subdomains, and known files |

The complete port and credential roster is in
[`testsites/README.md`](testsites/README.md).

### 3. Start the real crawler server

Use temporary runtime paths so manual results do not modify `/data` or mix with
another run. Site B uses deterministic, non-production fixture credentials;
the job stores only their environment references.

```bash
export MANUAL_CRAWLER_DIR="$(mktemp -d)"
export CRAWLER_DB_PATH="$MANUAL_CRAWLER_DIR/jobs.db"
export CRAWLER_LOG_DIR="$MANUAL_CRAWLER_DIR/logs"
export SITE_B_USERNAME=demo
export SITE_B_PASSWORD=password
mkdir -p "$CRAWLER_LOG_DIR"

uv run tenzai-crawler-server
```

Leave this terminal running. The server should bind to
`http://localhost:8000` and validate Katana during startup.

### 4. Submit an authenticated Site B job

In another terminal, submit the job through the CLI. The CLI uses the public
`POST /jobs` API, so this still exercises the deployed HTTP boundary:

```bash
uv run tenzai-crawler create http://localhost:8002 \
  --auth-config-json \
  '{"login_url":"http://localhost:8002/login","credentials":{"username":"{{env:SITE_B_USERNAME}}","password":"{{env:SITE_B_PASSWORD}}"},"instructions":"Sign in and stop when the dashboard is visible.","success_indicator":"Dashboard"}' \
  --discovery-max-rounds 3 \
  --discovery-max-actions 100 \
  --discovery-max-llm-pages 25
```

Copy the returned `job_id`, then poll the persisted job:

```bash
uv run tenzai-crawler status <job_id>
```

The normal lifecycle is:

```text
queued → authenticating → crawling → discovering → processing → completed
```

The two baseline Katana lanes can take several minutes. Only one job runs at a
time; additional jobs remain queued.

### 5. Check the result

For Site B, the completed sitemap should include these browser-only results:

```text
GET  /workflow-center
POST /api/onboarding/validate
POST /api/onboarding/preview
POST /api/settings/validate
```

The response's `sitemap.discovery` object should report a terminal outcome,
nonzero state and workflow counts, and the stop reason. The response evidence
should show both baseline Katana lanes and the browser-guided rounds. Raw lane
artifacts and terminal summaries are written under `$CRAWLER_LOG_DIR`.

Repeat the same `create` command without auth options for Sites E and F. Their
declared browser-only endpoints, required request sequences, and manual
interaction descriptions are in:

- [`testsites/site-e-crawl-trap-ruby/sitemap.json`](testsites/site-e-crawl-trap-ruby/sitemap.json)
- [`testsites/site-f-spa-deno/sitemap.json`](testsites/site-f-spa-deno/sitemap.json)

For Site G, pass its scoped fixture header by reference:

```bash
uv run tenzai-crawler create http://localhost:8007 \
  --auth-header 'X-Discovery-Token: {{env:TEST_HARNESS_TOKEN}}'
```

Header-only mode does not invoke the authentication agent.

### 6. Reset or stop

Restart a fixture before repeating a manual scenario:

```bash
docker compose -f testsites/docker-compose.yml restart site-b
```

Stop the crawler with `Ctrl-C`. Stop all fixture websites when finished:

```bash
docker compose -f testsites/docker-compose.yml down
```

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
uv run python -m scripts.run_crawler_auth_tests --crawl-duration 5m --job-timeout 600
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
| `CRAWLER_AUTH_NAVIGATION_ATTEMPTS` | `3` | Attempts for transient failures while opening the authentication entry page |
| `CRAWLER_AUTH_NAVIGATION_RETRY_BASE_SECONDS` | `1` | Initial backoff for authentication entry navigation retries; doubles after each failure |
| `CRAWLER_AUTH_MAX_STEPS` | `85` | Default max tool-calling steps for auth |
| `CRAWLER_AUTH_TIMEOUT_SECONDS` | `300` | Total AI-auth deadline, including retries and verification |
| `CRAWLER_DISCOVERY_MODEL` | `gpt-5.4-mini` | LLM model for browser-guided workflow decisions |
| `CRAWLER_DISCOVERY_MAX_MODEL_TURNS` | `40` | Hard model-turn budget for browser discovery |
| `CRAWLER_DISCOVERY_MAX_STATES` | `120` | Hard unique UI-state budget for browser discovery |
| `CRAWLER_DISCOVERY_TIMEOUT_SECONDS` | `300` | Total browser-guided action/model deadline |
| `CRAWLER_DISCOVERY_ACTION_SETTLE_TIMEOUT_SECONDS` | `15` | Maximum settling budget after navigation or a browser action |
| `CRAWLER_JOB_TIMEOUT_SECONDS` | `3600` | Whole-job wall-clock deadline across auth, baseline, discovery, and finalization |
| `CRAWLER_JOB_MEMORY_LIMIT_BYTES` | `2147483648` | Shared RSS ceiling for the job Chrome and active Katana process groups |
| `CRAWLER_SUBPROCESS_TIMEOUT` | `720` | Subprocess inactivity timeout (seconds) |
| `CRAWLER_KATANA_PROCESS_TIMEOUT_SECONDS` | `720` | Katana wall deadline, after which usable output is retained as a partial result |
| `CRAWLER_ENABLE_DEBUG_ENDPOINTS` | off | Set `1` to enable debug routes |

## Output

Completed jobs, and cancelled jobs that reached a valid checkpoint, expose a
`sitemap` on `GET /jobs/<job_id>` with:

- **`entries`** — flat list of observed HTTP requests (`method`, `url`, `status`, `content_type`, `timestamp`), aggregated by exact `(method, url)` identity and scoped to the target domain.
- **`tree`** — the same entries organized into a path-segment hierarchy (`children`, `pages`) for tree-style rendering.
- **`discovery`** — the terminal discovery outcome, rounds, additions, state/workflow counts, and stop reason.

The baseline writes `$CRAWLER_LOG_DIR/{job_id}.standard.jsonl` and `{job_id}.pure-headless.jsonl`; each enrichment round writes matching `discovery-{round}` artifacts. Every Katana JSONL artifact has an adjacent atomic `.terminal.json` summary that proves the result of every input seed.

Katana can emit both response-bearing records and request-only records for the same URL. Completed sitemaps preserve response status and content type when duplicate records are normalized.

The standard lane is checkpointed before pure-headless starts, and the merged
dual-pass baseline is checkpointed before guided discovery. Validated partial
additions are checkpointed during discovery, so cancellation, deadline,
memory-budget, and restart recovery can finalize the latest valid sitemap. A
Katana `crawl_timeout` with valid output is returned as a completed partial
result instead of discarding the sitemap; `result_metadata.completeness` is
`partial` and `result_metadata.warnings` identifies the affected lane and seed.
A partial lane cannot establish a discovery fixpoint. Discovery outcomes that
stop before a fixpoint (`budget_exhausted`, `partial_failure`, or `interrupted`)
also force `result_metadata.completeness` to `partial` and add a warning with
the discovery stop reason. A cancelled job keeps status `cancelled` while
exposing any finalized checkpoint; cancellation before the baseline has no
sitemap. Terminal result reads use only the persisted result and never reparse
historical logs.

## Known Limitations

- Authorization extraction from browser traffic is heuristic and may miss unusual record shapes.
- The LLM auth agent depends on the configured model, the quality of page accessibility data, and the supplied operator instructions for unusual flows.
- TOTP is supported through an explicit auth-agent tool; other out-of-band MFA methods require additional tooling or operator-specific instructions.
- Cancellation checks run in preflight/callbacks and long subprocess boundaries, not at the top of every tool function body.
## Security Posture

Tenzai Crawler is designed for trusted operators. If exposed broadly, add API authentication/authorization and egress controls.
