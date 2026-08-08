# Security Policy

Tenzai Crawler is trusted-operator tooling for authenticated application crawling. It drives
Playwright and Katana against operator-selected targets, so security reports should
focus on ways the service can cross trust boundaries, expose secrets, or perform unsafe crawl
behavior despite its configuration.

## Supported Versions

Security fixes are handled on `main`. If release branches or versioned packages are introduced,
this policy should be updated with the supported version range.

## Reporting a Vulnerability

Do not open a public issue for a vulnerability.

Use GitHub private vulnerability reporting for this repository when available. Include:

- A clear description of the issue and affected component.
- Reproduction steps with the smallest target or fixture that demonstrates the issue.
- Expected vs. actual behavior.
- Any logs or screenshots needed to understand impact, with secrets redacted.
- Whether the issue requires debug endpoints, operator credentials, or a network position outside
  the service.

If private vulnerability reporting is not available, contact the maintainers through the
repository owner's normal private security channel before disclosing details publicly.

## Security Boundaries

The service is designed for trusted operators. If deployed for broad or untrusted access, add API
authentication, authorization, and egress controls before exposing it.

Important boundaries:

- The orchestrator owns job state, auth decisions, crawl lifecycle, and status transitions.
- AI auth only runs when `auth_config` includes `credentials` or `login_url`.
- Header-only `auth_config` is manual-header mode and must not trigger AI auth.
- The crawler remains auth-agnostic and accepts only target, headers, scope, seed URLs, and
  exclusion patterns.
- The submitted `auth_config` is persisted and returned by the job API. Environment templates such
  as `{{env:APP_PASSWORD}}` are recommended so resolved values remain in memory. Protect the jobs
  database and API as sensitive when operators submit plaintext values.
- Debug endpoints must stay disabled unless explicitly needed.

## In Scope

Examples of security issues worth reporting:

- Secret leakage outside the documented `auth_config` job record and job-status response, including
  leakage through logs, command output, crawl evidence, generated reports, or docs.
- Auth-boundary bypasses where header-only mode triggers AI auth or AI auth runs without a login
  URL or credentials.
- Crawl behavior that violates operator-supplied scope or explicit exclusion configuration. The
  runtime intentionally does not infer or block destructive-looking routes.
- SSRF or egress-control bypasses in `target_url`, `login_url`, redirects, or
  extra seed URLs.
- Cancellation or process-lifecycle bugs that leave Katana or browser processes running.
- Debug endpoint exposure or sensitive task/stack leakage.
- GitHub Actions changes that broaden permissions, use mutable third-party actions, or expose
  secrets to untrusted code paths.

## Out of Scope

These are usually not security vulnerabilities by themselves:

- Reports against local deterministic `testsites/` fixtures with no impact on the service.
- Failed crawls caused by missing `katana`, browser binaries, or model credentials.
- LLM prompt quality concerns without a concrete security boundary bypass.
- Denial of service from an operator intentionally submitting expensive crawl jobs in a trusted
  single-user deployment.

## Disclosure Expectations

Give maintainers reasonable time to investigate and remediate before public disclosure. Avoid
sharing exploit details, target credentials, captured cookies, bearer tokens, or private crawl
logs in public channels.
