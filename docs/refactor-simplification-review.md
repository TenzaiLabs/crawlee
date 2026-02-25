# Refactor and Simplification Review (Current State)

## Scope

This review reflects the current `app/` and `k8s/` state, with emphasis on simplification opportunities that remain after the Phase 4 architecture work.

## Current Snapshot

### Completed since the previous survey

The following high-value cleanups are now in place:

- Shared helper extraction into `app/common.py` (`coerce_int`, hostname scope helpers, redaction helpers, file-open wrappers).
- Status set centralization into `app/job_status.py` (`ACTIVE_JOB_STATUSES`, `TERMINAL_JOB_STATUSES`, `INTERRUPTED_JOB_STATUSES`).
- Orchestrator helper extraction in `app/orchestrator.py` (`_normalize_auth_config`, `_extract_manual_headers`, `_run_auth_if_needed`, `_cancel_if_requested`).
- Auth module decomposition into focused units:
  - `app/auth_secrets.py`
  - `app/auth_model.py`
  - `app/auth_browser.py`
  - `app/auth_log_extract.py`
  - `app/auth_traffic.py`
- Process-policy constant centralization in `app/settings.py`.
- `SubprocessResult` converted to a dataclass (`app/process.py`).
- Shared record parsing introduced via `app/log_records.py` and consumed by parser/auth log extraction.

These changes materially reduced drift risk while preserving Phase 4 boundaries.

## High-Impact Opportunities Still Open

1. Formalize auth success criteria beyond tool-loop completion.
- Current behavior: `success_indicator` is prompt-only context and not a hard pass/fail gate.
- Risk: model may call `done()` before the intended post-login state is truly reached.
- Recommendation: add code-level post-check(s), for example:
  - verify `success_indicator` text/selector in final page state, or
  - require at least one in-scope authenticated signal (cookie/auth header/profile endpoint).

2. Add stronger input policy for user-driven crawl/auth configuration.
- Current `scope_config` validation only enforces headless/browser-flag constraints.
- Numeric controls (`max_depth`, `rate_limit`, `concurrency`, `parallelism`, `crawl_duration`) do not have explicit upper bounds in validation.
- `auth_config` remains intentionally loose (dict), but there is no policy enforcement for suspicious/plaintext credential-like values.
- Recommendation: add bounded validation policy in `scope_config.py` and a lightweight auth-config policy layer in orchestrator/auth without breaking loose-schema compatibility.

3. Decompose `authenticate()` into smaller testable units.
- `app/auth_agent.py::authenticate` still combines model setup, browser lifecycle, route wiring, tool definitions, chain execution, retries, extraction, and packaging.
- Recommendation: extract helper units:
  - `_build_toolset(page, cancel_event, max_steps)`
  - `_run_auth_chain(...)`
  - `_collect_auth_result(...)`
  while keeping the public auth interface unchanged.

4. Move cancel checks closer to tool bodies for faster cooperative interruption.
- Current cancellation is checked preflight and in chain callbacks.
- Known caveat: checks are not at the top of every tool body (`click`, `type_text`, `select_option`, `wait`, `get_page_state`).
- Recommendation: add early `cancel_event` checks in each tool function to reduce wasted actions when cancellation races with tool calls.

## Medium-Impact Opportunities

1. Reduce repeated cancellation checkpoints in `run_job()`.
- `_cancel_if_requested()` improved readability, but the orchestration path still performs many sequential checks.
- Recommendation: adopt a small phase-runner abstraction (phase step + cancel guard) to make control flow less repetitive and easier to audit.

2. Clarify and tighten persistence boundaries for sensitive auth material.
- Current behavior correctly avoids persisting resolved secrets and only stores request-supplied `auth_config` JSON.
- Recommendation: add explicit validation/warnings for plaintext credential fields at API boundary; optionally reject known credential keys unless templated (`{{env:...}}`, `{{totp:...}}`) in strict mode.

3. Optional response caching for completed sitemap reads.
- `GET /jobs/{id}` reparses logs for completed jobs.
- Recommendation: consider caching parsed sitemap in-memory for short intervals to reduce repeated disk reads on polling-heavy clients.

4. Kubernetes hardening defaults remain minimal.
- `k8s/statefulset.yaml` currently has core deployment shape but no explicit security context/resource policy/env wiring.
- Recommendation: add security context, resource requests/limits, and explicit env configuration examples for auth model/provider keys.

## Low-Impact Cleanup

1. Centralize auth prompt text constants.
- `auth_browser.py` is already cleaner; further extracting reusable prompt fragments would simplify experimentation/testing.

2. Add focused tests for final auth caveats.
- Existing tests cover many foundations (scope validation, auth header extraction logs, traffic capture, subprocess edge cases).
- Missing: deterministic integration test for tool-loop completion + post-auth success assertions and cancel-during-tool semantics.

## Suggested Refactor Order (From Here)

1. Add auth success post-check policy (`success_indicator` hard gate and/or authenticated-signal checks).
2. Add bounded validation for `scope_config` and optional strict auth-config policy.
3. Split `authenticate()` internals into smaller helper units.
4. Add tool-body cancellation checks + targeted tests.
5. Apply K8s hardening/env wiring updates.

## Guardrails to Preserve During Refactor

- Orchestrator remains the single hub for status/auth/crawl orchestration.
- AI auth runs only when `credentials` or `login_url` is present.
- Header-only `auth_config` remains manual-header mode and never triggers AI auth.
- Auth interfaces remain stable:
  - `authenticate(target_url, auth_config, cancel_event) -> AuthResult`
  - `resolve_secrets(auth_config) -> dict`
  - `needs_auth(auth_config) -> bool`
- Crawler remains auth-agnostic and accepts headers only.
