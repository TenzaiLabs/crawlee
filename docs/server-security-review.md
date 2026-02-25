# Server-Side Security Review (Non-AI)

## Scope and Threat Model

- Scope: server/API/runtime risks in this repository (`app/`, `k8s/`).
- Excluded: prompt-injection/model-behavior AI security concerns.
- Threat model used here: service may be reachable by users other than a single trusted operator.

## Executive Summary

The current implementation is acceptable for a strictly trusted internal operator workflow, but has multiple trivial/high-impact risks if exposed to broader network access.

Top issues are:
- No API authentication/authorization.
- SSRF-capable crawl/auth entry points with no target/network allowlist.
- Debug introspection endpoints that can leak in-memory state when enabled.
- Plaintext persistence of potentially sensitive `auth_config` content.
- Lack of resource guardrails on user-controlled crawl parameters.

## Findings

## Critical

1. Unauthenticated job-control API.
- Evidence: all endpoints are exposed without auth middleware/dependencies in `app/main.py`.
- Impact: any reachable client can create, inspect, and cancel jobs.
- References:
  - `app/main.py:117-198`
  - `app/main.py:149-172`
  - `app/main.py:175-198`
- Recommendation:
  - Add API authentication (bearer token or mTLS) and reject anonymous requests.
  - Add authorization policy for debug and cancel operations.

2. SSRF / internal network probing via unbounded target URLs.
- Evidence: user-provided `target_url` and `login_url` are used directly for crawl/auth navigation.
- Impact: requests to loopback, link-local, RFC1918, cloud metadata endpoints, and internal services.
- References:
  - `app/main.py:117-146`
  - `app/auth_agent.py:461-463`
  - `app/auth_agent.py:518`
  - `app/crawler.py:67-69`
- Recommendation:
  - Enforce URL policy: scheme allowlist (`http/https`), host allowlist/denylist, block private/link-local/loopback ranges.
  - Add egress controls at Kubernetes/network layer in addition to app checks.

## High

1. Sensitive data can be persisted in plaintext in `auth_config`.
- Evidence: request `auth_config` is inserted directly into SQLite as JSON; only convention discourages plaintext.
- Impact: credentials/tokens can end up at-rest in `/data/jobs.db` if clients send literal values.
- References:
  - `app/main.py:132-143`
  - `app/db.py:40-49`
- Recommendation:
  - Validate and reject plaintext credential fields where feasible.
  - Require secret references (`{{env:...}}`) for credential-like keys.
  - Add optional at-rest encryption if plaintext cannot be fully prevented.

2. Debug endpoints can leak internals and in-memory secrets when enabled.
- Evidence: `/debug/tasks` and `/debug/jobs/{job_id}/stack` return coroutine repr/stack frames.
- Impact: stack traces can expose sensitive runtime details and internal topology.
- References:
  - `app/main.py:76-115`
- Recommendation:
  - Keep disabled by default (already true) and additionally gate behind strong auth.
  - Redact stack payloads or restrict to admin-only environments.

3. Unbounded/weakly bounded user inputs can drive local DoS.
- Evidence: user controls crawl knobs (`concurrency`, `parallelism`, `crawl_duration`, etc.) with no upper bounds.
- Impact: excessive CPU/memory/FD usage and service starvation.
- References:
  - `app/crawler.py:99-109`
  - `app/crawler.py:150-169`
- Recommendation:
  - Add explicit upper bounds in `validate_scope_config` for numeric fields.
  - Apply per-job hard ceilings independent of request values.

## Medium

1. Process command logging may leak sensitive header values.
- Evidence: subprocess command is logged as joined argv; crawl headers are passed as `-H` args.
- Impact: cookie/authorization headers can appear in logs if command logging is enabled in production.
- References:
  - `app/process.py:45`
  - `app/crawler.py:136-137`
- Recommendation:
  - Redact `-H` argument values in logs.
  - Consider logging command shape only (flags present) not raw values.

2. `run_safe_subprocess` inherits ambient environment by default.
- Evidence: `create_subprocess_exec(..., env=None)` inherits parent env unless override supplied.
- Impact: child tools receive all service env vars; broadens blast radius if tool or crash dumps leak env.
- References:
  - `app/process.py:46-55`
- Recommendation:
  - Provide minimal allowlisted env to subprocesses.
  - Pass only required variables (`PATH`, locale, proxy vars if needed).

3. Kubernetes manifest lacks explicit security hardening defaults.
- Evidence: statefulset has no `securityContext`, resource limits, or network policy references.
- Impact: weaker container/runtime isolation and harder multi-tenant safety.
- References:
  - `k8s/statefulset.yaml:15-24`
- Recommendation:
  - Add `runAsNonRoot`, drop capabilities, readOnlyRootFilesystem where possible.
  - Add CPU/memory limits and complementary `NetworkPolicy`.

## Low

1. Potential log-forging via unsanitized user-originated values in logs.
- Evidence: values like URL and some exceptions are logged directly.
- Impact: noisy/ambiguous logs, lower forensic reliability.
- References:
  - `app/main.py:119`
  - `app/orchestrator.py:121`
- Recommendation:
  - Normalize/control-character-strip high-risk logged values.

## Prioritized Remediation Plan

1. Add API authentication/authorization in `app/main.py`.
2. Add strict URL/network egress policy checks for `target_url`/`login_url`.
3. Enforce `auth_config` secret-reference policy (reject plaintext secrets).
4. Add bounds for scope_config numerical controls.
5. Redact secrets in command/log output and minimize subprocess env.
6. Harden Kubernetes deployment defaults.

## Notes for Current Intended Deployment

If this stays a trusted-operator internal tool behind strong cluster/network boundaries, risk is materially reduced. If externalized beyond trusted users, the Critical/High items should be treated as blockers.
