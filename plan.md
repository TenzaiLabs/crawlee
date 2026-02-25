# Crawler Implementation Plan

## Overview

An async-native, AI-driven web crawler deployed as a single-replica Kubernetes StatefulSet.
It solves two hard problems: **automated authentication** (LLM + Playwright) and **safe crawling** (strict scoping + regex exclusions).

Stack: Python 3.14, FastAPI, aiosqlite, Playwright, Katana, Proxify.

---

## Phase 1: Foundation & State Management

**Goal:** Stable API on K8s with persistent storage and crash recovery.

### 1.1 — Project Scaffold & Dockerfile ✅
- Python project structure:
  ```
  app/
    main.py          # FastAPI entrypoint
    db.py            # aiosqlite helpers
    models.py        # Pydantic schemas
    orchestrator.py  # Job runner (async background task)
    process.py       # Subprocess management
    proxy.py         # Proxify lifecycle
    crawler.py       # Katana lifecycle
    auth_agent.py    # Playwright + LLM auth
    parser.py        # JSONL → sitemap
    cli.py           # CLI helper (added beyond plan)
    scope_config.py  # Scope validation (added beyond plan)
  ```
- `Dockerfile`: Python 3.14 + Playwright browsers + Katana binary + Proxify binary.
- `uv project dependencies`: fastapi, uvicorn, aiosqlite, playwright, llm (https://llm.datasette.io/en/stable/).

### 1.2 — Kubernetes Manifests ✅
- `k8s/statefulset.yaml` — replicas: 1, PVC at `/data`.
- `k8s/service.yaml` — ClusterIP exposing port 8000.

### 1.3 — Database & API ✅
- SQLite at `/data/jobs.db`. Schema:
  ```sql
  CREATE TABLE jobs (
      job_id TEXT PRIMARY KEY,
      status TEXT NOT NULL DEFAULT 'pending',
      target_url TEXT NOT NULL,
      scope_config TEXT,        -- JSON
      auth_config TEXT,         -- JSON (env var refs only, no plaintext secrets)
      error TEXT,
      created_at TEXT NOT NULL,
      finished_at TEXT
  );
  ```
- Valid statuses: `pending → authenticating → crawling → processing → completed | failed | failed_interrupted | cancelled`.
- Endpoints:
  - `POST /jobs` — create job, return job_id.
  - `GET /jobs/{id}` — return job status + result (sitemap) if completed.
  - `POST /jobs/{id}/cancel` — request graceful cancellation of a running job.

### 1.4 — Crash Recovery ("Zombie Killer") ✅
- FastAPI `lifespan` handler on startup:
  ```sql
  UPDATE jobs SET status = 'failed_interrupted'
  WHERE status IN ('authenticating', 'crawling', 'processing');
  ```
- Ensures no stale "running" jobs after pod restart.

### 1.5 — Job Concurrency Guard ✅
- Since Proxify binds `:8888`, only one job can run at a time.
- API rejects `POST /jobs` with `409 Conflict` if any job is in an active state.
- Uses an asyncio Lock to prevent race conditions between concurrent `POST /jobs` requests.

**Deliverable:** Deployed pod that persists data across `kubectl delete pod`. API accepts/returns jobs.

---

## Phase 2: Proxy & Process Layer

**Goal:** Reliable subprocess management and HTTP traffic capture.

### 2.1 — Async Process Wrapper (`process.py`) ✅
- `run_safe_subprocess(cmd, timeout, on_output)`:
  - Spawns via `asyncio.create_subprocess_exec` with `start_new_session=True`.
  - Sends SIGTERM/SIGKILL to process group on stall, cancel, or task cancellation.
  - Stall detection: if no stdout/stderr for N seconds, kill.
  - Returns exit code + captured output.
  - Also supports `cancel_event` and `stop_event` for external termination.

### 2.2 — Proxify Integration (`proxy.py`) ✅
- Start: spawn `proxify -http-addr 127.0.0.1:8888 -output /data/logs/{job_id}.jsonl`.
- Stop: send SIGTERM, wait up to 5s, then SIGKILL.
- Health check: confirm port 8888 is listening before proceeding (TCP connect loop).

### 2.3 — JSONL Parser (`parser.py`) ✅
- Read `/data/logs/{job_id}.jsonl` (also reads `.katana` log as secondary source).
- Extract: method, URL, status code, content-type, timestamp.
- Deduplicate by (method, URL).
- Output: structured sitemap dict (grouped by path hierarchy).
- Filters out CONNECT requests and out-of-scope hostnames.
- Pin Proxify version and document expected JSONL schema.

**Deliverable:** Submit a URL → Proxify captures traffic → API returns parsed sitemap.

---

## Phase 3: Crawler Integration

**Goal:** Katana integration with strict safety rules.

### 3.1 — Katana Runner (`crawler.py`) ✅
- Spawn: `katana -u <url> -proxy http://127.0.0.1:8888 -silent -jsonl`.
- Pass auth via `-H "Cookie: ..."` and `-H "Authorization: ..."` flags.
- Wire up stall detection (60s no output → kill).
- Respect `max-depth` and page-count limits from `scope_config`.

#### 3.1.1 — Command Builder ✅
- `build_katana_command()` accepts `CrawlConfig` (target_url, scope config, headers).
- Normalizes header inputs into repeated `-H` flags.
- Defaults: depth=5, rate_limit=10. Overridable via `scope_config`.
- Also supports: concurrency, parallelism, crawl_duration, headless mode, system chrome, CDP URL, request timeout, crawl scope, and custom exclusion filters.

#### 3.1.2 — Process Runner ✅
- `run_crawl()` executes Katana via `run_safe_subprocess` with 60s stall timeout.
- Streams output lines into a deque buffer (last 20 lines) for debugging.
- Maps non-zero exit codes to `RuntimeError` with captured output tail.
- Supports `max_pages` via a `stop_event` that terminates Katana when the limit is reached.

#### 3.1.3 — Output Wiring ✅
- Katana outputs JSONL to stdout; lines starting with `{` are written to a `.katana` sidecar log.
- Parser reads both the Proxify JSONL and the `.katana` log for completeness.
- Output is compatible with the parser expectations.

### 3.2 — Safety / Scoping ✅
- **FQDN scope:** pass via `-fs dn` (same domain) by default; configurable via `scope_config.field_scope`.
- **Exclusion regex:** build from defaults + user config:
  ```
  Default: logout|signout|log-out|sign-out|delete|remove|unsubscribe|deactivate
  ```
  Pass via `-crawl-out-scope` (pipe-delimited). Supports `exclude_filters` list and `exclude_regex` string from `scope_config`.
- **Rate limiting:** add `-rl` flag (default: 10 req/s). `robots.txt` handling relies on Katana's default behavior.

### 3.3 — Job Lifecycle (in `orchestrator.py`) ✅
1. Set status → `crawling`.
2. Start Proxify.
3. Wait for proxy health check.
4. Start Katana (with headers from `auth_config` if present).
5. Monitor Katana (stall/cancellation checks via `cancel_event`).
6. Katana terminates (or is killed on cancel/stall).
7. Stop Proxify (in `finally` block — always cleaned up).
8. Set status → `processing`.
9. Parse JSONL → sitemap.
10. Set status → `completed` (or `failed` on exception, `cancelled` on cancel).

**Deliverable:** Full unauthenticated crawl pipeline. Submit URL → get sitemap of discovered pages.

---

## Phase 4: AI-Driven Authentication

**Goal:** LLM-powered login before crawling, with orchestrator-managed handoff into crawl headers.

### 4.0 — Current Status Snapshot
- Overall status: **implemented and passing current tests**, with **known gaps** before calling this production-hardened.
- Core architecture is in place: orchestrator is the hub, auth agent returns `AuthResult`, crawler remains auth-agnostic.
- Validation completed in this environment:
  - `uv run pytest -q` → `19 passed, 1 skipped`.
  - `uv run python testsites/verify_sites.py` → all 6 local sites passed.
  - `RUN_E2E=1` scenario required longer polling, and test polling was extended to `120 * 0.5s`.

### 4.1 — Secret Resolution (`auth_agent.py`) ✅
- Implemented `resolve_secrets(auth_config)` with recursive substitution for `{{env:VAR}}` and `{{totp:VAR}}` in nested dict/list/string values.
- Missing env vars raise `ValueError`.
- `pyotp` integration is implemented and dependency added.
- Secrets are resolved in memory by orchestrator before browser launch; no persistence logic was added for resolved values.

### 4.2 — Playwright Browser Setup (`auth_agent.py`) ✅
- Implemented async Playwright Chromium launch with proxied traffic via `http://127.0.0.1:8888`.
- Navigates to `login_url` when provided, otherwise falls back to `target_url`.
- Orchestrator starts Proxify and waits for health before auth execution.

### 4.3 — LLM Agent Loop (`auth_agent.py`) ⚠️
- Implemented `llm.get_async_model()` using `CRAWLER_AUTH_MODEL` (default `gpt-5-mini`).
- Implemented tool-based chain flow with tools: `click`, `type_text`, `select_option`, `wait`, `get_page_state`, `done`.
- Implemented retry with backoff (up to 3 attempts) around chain execution.
- Implemented max-step enforcement through `after_call` callback and `AuthenticationError` when exceeded.
- Implemented cancel checks in callbacks and preflight.
- Implemented provider-aware API key discovery for OpenAI/Anthropic/OpenRouter/Gemini/Vertex env conventions and explicit model key assignment.
- Known gaps:
  - Cancel is not checked at the beginning of every tool function body; it is checked in callbacks and pre-auth.
  - `success_indicator` is passed to the model prompt but not independently evaluated in code.

### 4.4 — DOM State Extraction (`auth_agent.py`) ✅
- Implemented page-state extraction via `page.evaluate()` with visibility filtering.
- Output includes URL, title, visible inputs/selects/buttons/links, and truncated visible text.
- Implemented formatting helper output suitable for prompt injection.

### 4.5 — Session Extraction & Handoff (`auth_agent.py`) ⚠️
- Implemented cookie extraction and formatting to `Cookie: ...` header.
- Implemented Proxify log scan for `Authorization` headers, with target-host filtering and deduplication.
- Added scan-time logging at debug/info/warn levels to improve observability of extraction behavior.
- Returns `AuthResult(cookies, headers)`.
- Known gap:
  - Authorization extraction is heuristic (tail scan, schema-tolerant parsing) and may miss edge log shapes.

### 4.6 — Updated Orchestrator Flow (`orchestrator.py`) ✅
- Implemented `needs_auth(auth_config)` gate:
  - `credentials` or `login_url` present → run auth flow.
  - header-only config → skip auth flow, preserve manual-header mode.
- Implemented status transitions:
  - `pending -> authenticating -> crawling -> processing -> completed` when auth is needed.
  - `pending -> crawling -> processing -> completed` otherwise.
- Implemented merge behavior:
  - manual headers + auth-returned headers merged into `CrawlConfig.headers`.
- Proxify log path is passed to auth agent ephemerally via in-memory config field.

### 4.7 — `auth_config` Support Status ⚠️
- Working fields: `headers`, `login_url`, `credentials`, `instructions`, `success_indicator`, `max_steps`.
- Behavior matches the intended loose/unstructured credentials contract.
- Known gap:
  - No strict schema validation was added beyond runtime type checks/coercion.

### 4.8 — Component Interfaces ✅
- Implemented signatures and interaction model:
  - `authenticate(target_url, auth_config, cancel_event) -> AuthResult`.
  - `resolve_secrets(auth_config) -> dict`.
  - `needs_auth(auth_config) -> bool`.
- Crawler remains unaware of authentication internals and only receives headers.

### 4.9 — Error Handling Status ⚠️
- Implemented:
  - Tool-level Playwright errors are returned as tool output strings (allowing model retries).
  - Max-step overflow raises `AuthenticationError`.
  - LLM call retries with exponential backoff and terminal failure wrapping.
  - Browser/context/playwright cleanup in `finally`.
- Known gap:
  - `success_indicator` is not used as a hard success/failure check, so completion still depends on model/tool flow heuristics.

### 4.10 — Dependencies & Runtime Config ⚠️
- Implemented:
  - `pyotp` added in `pyproject.toml`.
  - Existing Playwright and `llm` deps retained.
- Not yet implemented:
  - Explicit `OPENAI_API_KEY` wiring in Kubernetes manifest (`k8s/statefulset.yaml`) remains to be added.

### 4.11 — Testing & Validation Status ⚠️
- Implemented unit coverage:
  - `needs_auth` behavior.
  - env + missing-env + TOTP secret resolution.
  - DOM-state formatting.
- Runtime validation performed:
  - Full test suite: `19 passed, 1 skipped`.
  - Local sites verification: all 6 test sites passed via fallback script.
  - E2E scenario stability improved by extending polling window to ~60s.
  - Site verification script currently emits Python `SyntaxWarning` for `return` in a `finally` block; execution succeeded, but the helper script should be cleaned up.
- Missing from the original target:
  - Dedicated integration test that mocks deterministic LLM tool calls for auth completion and cookie/header assertions.

**Phase 4 deliverable status:** **Mostly achieved**, but not considered fully hardened. Primary follow-ups are explicit success-indicator enforcement, auth integration-test depth, and K8s env wiring.

---

## Phase 5: Testing & Hardening

### Test Target Sites (self-hosted, containerized)
| Site | Purpose | Key Assertion |
|------|---------|---------------|
| A — Static HTML | Basic crawl | All 3-5 pages discovered |
| B — Simple Login | Form auth | Session extracted, authed pages found |
| C — Registration | "Do no harm" | Registration form is NOT submitted |
| D — Complex Auth | Multi-button login | Correct "Sign In" button chosen over "Register" |
| E — Crawl Trap | Infinite calendar links | Crawl terminates within page/depth limits |
| F — SPA | JS-rendered content | Client-rendered links discovered via Katana's headless mode |

### Integration Tests
- Job lifecycle: pending → completed happy path.
- Crash recovery: kill pod mid-crawl → restart → job marked `failed_interrupted`.
- Cancellation: cancel mid-crawl → processes cleaned up → status `cancelled`.
- Concurrent job rejection: second POST while job running → 409.
- Scope enforcement: crawler never requests URLs outside target FQDN.
- Exclusion enforcement: crawler never hits logout/delete URLs.

---

## Open Questions
1. **LLM provider:** Which model/API for the auth agent? (OpenAI, Anthropic, local?)
Answer: use an LLM middleware but the first integration can b OpenAI using the env var OPENAI_API_KEY
2. **MFA support:** TOTP is mentioned — need to define the TOTP secret injection mechanism.
Use the python TOTP libraries
3. **Observability:** Should we add structured logging / metrics, or keep it simple for v1?
Answer: keep it simple
4. **Multi-job support (future):** Dynamic proxy port allocation would unblock this.
Single job for now
