# Shared Browser Lifecycle Checkpoint

Generated: `2026-07-23`

## Result

The production orchestrator now owns one ephemeral Chrome profile, loopback CDP
endpoint, passive observer, and sequence of Playwright/Katana ownership epochs
for each job. Authentication receives the existing Playwright context and no
longer launches or closes a private browser.

Two controlled jobs were submitted through the real HTTP server with the exact
`v1.6.1-tenzai.1` Katana binary from the qualified crawler image:

| Job | Budget | Result | Evidence |
| --- | --- | --- | --- |
| `dbca1920-cc9d-4af8-a6aa-565351eb302d` | 2 minutes | completed, 18 sitemap entries | runtime XHR, rendered navigation, handoff, same-origin header, frame and worker requests, and both serial-seed children; result retrieved after a server restart |
| `b34004fd-9951-4316-8461-61db2f5ca895` | 75 seconds | completed, 17 sitemap entries | `http://child.localhost:8007/subdomain-header-only` was discovered and the fixture access log recorded HTTP 200, proving the non-cookie auth header reached an in-scope subdomain |

The direct browser fixture oracle also verified HTTP 200 and rendered success
text on the subdomain page. After both jobs, no job Chrome, Katana process, or
job profile directory remained.

The first run exercised:

```text
real POST /jobs
  -> orchestrator-owned Chrome + passive CDP observer
  -> Playwright manual-header/bootstrap epoch
  -> close pages and disconnect Playwright
  -> Katana -cwu -p 1 on the same Chrome
  -> close leftover page targets
  -> reconnect Playwright, open and navigate a fresh page
  -> stop Chrome and delete the profile
  -> persist sitemap
  -> restart server and retrieve the completed job
```

## Verification

- `uv run pytest -q`: 156 passed, 7 skipped.
- `uv run ruff check app tests scripts`: passed.
- `uv run ruff format --check app tests scripts`: passed.
- `uv run ty check`: passed.
- Opt-in site-G browser fixture: 1 passed.

This is the Step 4 lifecycle checkpoint, not the dual-pass baseline. At this
checkpoint the real-server crawl uses the shared pure-headless pass. Step 5
adds persistence and recovery contracts; Step 6 replaces the temporary
single-pass baseline with the final standard-plus-pure-headless stage and
persists passive-CDP provenance.
