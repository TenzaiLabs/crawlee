# Contributing

Tenzai Crawler is an async-native FastAPI service for trusted operators. Contributions should keep
the service small, explicit, and safe around auth, crawling, subprocesses, and persisted artifacts.

## Development Setup

Install dependencies:

```bash
uv sync --extra test --extra dev
```

Run the API locally:

```bash
uv run tenzai-crawler-server
```

Run the usual checks:

```bash
uv run pre-commit run -a
uv run pytest -q
uv run ruff check app tests scripts --fix
uv run ruff format app tests scripts
uv run ty check
```

Install the hooks once with:

```bash
uv run pre-commit install
```

The E2E scenario suite is opt-in and requires `katana` and `proxify` in `PATH`:

```bash
RUN_E2E=1 uv run pytest -q tests/test_scenarios.py
```

## Engineering Rules

- Keep the service async-native; do not add blocking I/O to the event loop.
- Preserve the single-job constraint because Proxify binds `127.0.0.1:8888`.
- Keep the orchestrator as the hub for auth decisions, proxy lifecycle, crawl execution, and job
  status transitions.
- Keep crawler code auth-agnostic. It should consume headers, target URL, scope, seed URLs, and
  exclusions, not auth internals.
- Add focused tests for new behavior. Use deterministic unit tests by default and opt-in E2E only
  where external binaries or live fixtures are required.
- Put substantial design, validation, and security writeups in `docs/`.

## Auth and Secret Handling

Do not commit secrets, cookies, bearer tokens, captured credentials, private crawl logs, or
plaintext `auth_config` values.

Use environment references such as:

```json
{
  "credentials": {
    "email": "{{env:APP_EMAIL}}",
    "password": "{{env:APP_PASSWORD}}"
  }
}
```

Auth guardrails:

- AI auth runs only when `auth_config` has `credentials` or `login_url`.
- Header-only auth is manual-header mode and must not trigger AI auth.
- Keep these interfaces stable unless a change is explicitly coordinated:
  - `authenticate(target_url, auth_config, cancel_event) -> AuthResult`
  - `resolve_secrets(auth_config) -> dict`
  - `needs_auth(auth_config) -> bool`

## Testsites and Generated Artifacts

The `testsites/` fixtures are deterministic coverage for crawler behavior. Prefer fixture-backed
tests over tiny ad hoc servers when the existing fixtures can prove the behavior.

Generated comparison outputs should not be committed unless the change explicitly asks for report
artifacts:

- `docs/testsite-comparison-report.md`
- `docs/testsite-comparison-results.json`

## Docs Site

The GitHub Pages site is served from `docs/`.

- `docs/index.html` is the overview page.
- `docs/docs.html` is the usage and architecture docs page.
- Shared styling lives in `docs/assets/site.css`.

Keep Pages workflow changes conservative:

- Use least-privilege job permissions.
- Pin GitHub Actions to immutable commit SHAs.
- Use `persist-credentials: false` for checkout unless a job truly needs to push.
- Do not introduce external runtime CDN dependencies when a small vendored asset is practical.

## Pull Request Checklist

Before opening or merging a PR:

- Confirm the change does not persist plaintext secrets.
- Confirm auth mode behavior is unchanged or explicitly tested.
- Confirm dangerous-path and auth-recorded exclusions still work when touching crawler behavior.
- Run `uv run pre-commit run -a`, plus focused tests for the touched code.
- Keep generated reports, logs, DB files, screenshots, and local artifacts out of the commit unless
  they are the requested deliverable.
