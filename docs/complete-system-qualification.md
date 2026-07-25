# Step 9 Complete-System Qualification

Generated: `2026-07-23T20:38:09.581898+00:00`

This is the complete proxyless qualification from real `uv run tenzai-crawler-server` processes, exercised only through the HTTP API. The report combines the full sequential inventory run with targeted reruns of repaired controlled fixtures. Every job uses bounded known-file discovery, standard Katana, shared-Chrome pure-headless Katana, passive CDP evidence, and guided LLM discovery.

## Run summary

- Completed safely: 22/24 targets.
- Controlled release fixtures completed: 21/21.
- Observation-only public canaries completed: 1/3.
- Controlled qualification gate: PASS.
- Sitemap entries: 625.
- Browser-only fixture controls found: 10/10.
- Lane-specific capability markers found: 11/11.
- Required request sequences found: 5/5.
- Persisted results verified after server restart: 24/24.
- Cross-job isolation violations: 0.
- Discovery fixpoints: 21/24 with 237 new entries.
- Declared destructive or session-ending markers observed: 3.
- Fixture-ledger entries: 709 (108 required, 0 destructive markers).
- CrawlGround: 44/59 controls (75%); scored controls: `buttons.01-html-button`, `buttons.02-div-onclick`, `dynamic-content.01-fetch-injected`, `dynamic-content.02-fetch-injected`, `dynamic-content.03-intersection-observer`, `dynamic-content.04-details-toggle`, `dynamic-content.05-sse-injected`, `dynamic-content.06-websocket-injected`, `dynamic-content.07-load-more`, `dynamic-content.08-template-element`, `dynamic-content.09-infinite-scroll`, `dynamic-content.10-localstorage`, `forms.05-fetch-post`, `forms.08-formaction-override`, `forms.09-js-form-submit`, `forms.11-fetch-get`, `frames.01-iframe-srcdoc`, `frames.02-shadow-dom`, `frames.04-shadow-dom-closed`, `js-events.01-mouseover-injected`, `js-events.02-onclick-prevent-default`, `js-events.03-focus-injected`, `js-events.04-css-hover-dropdown`, `js-events.05-multilevel-dropdown`, `js-events.06-dialog-modal`, `js-events.07-dblclick`, `js-events.08-keydown-enter`, `js-events.09-contextmenu`, `js-events.10-tab-panel`, `js-events.11-popover`, `links.01-anchor-href`, `links.02-js-built-href`, `links.04-svg-anchor`, `links.05-target-blank`, `links.06-javascript-href`, `navigation.01-meta-refresh`, `navigation.02-window-location-timeout`, `navigation.03-history-push-state`, `navigation.04-hash-routing`, `navigation.05-window-replace`, `navigation.06-http-redirect`, `navigation.08-window-open`, `navigation.09-history-replace-state`, `other.01-bot-detection-globals`.

| Target | Ready | Crawl | Auth mode | Entries | Expected | Browser-only | Lane markers | Sequences | Outcome | New | Ledger | Persisted | Isolation | Destructive | Seconds |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | ---: | ---: |
| `site-a-static` | yes | `completed` | `public` | 16 | 11/11 | 0/0 | 0/0 | 0/0 | `fixpoint` | 0 | 0 | yes | 0 | 1 | 86.6 |
| `site-b-login-flask` | yes | `completed` | `llm` | 16 | 7/7 | 4/4 | 0/0 | 2/2 | `fixpoint` | 3 | 111 | yes | 0 | 0 | 280.2 |
| `site-c-registration-express` | yes | `completed` | `public` | 12 | 8/8 | 0/0 | 0/0 | 0/0 | `fixpoint` | 1 | 0 | yes | 0 | 0 | 103.8 |
| `site-d-complex-auth-go` | yes | `completed` | `llm` | 10 | 6/6 | 0/0 | 0/0 | 0/0 | `fixpoint` | 0 | 0 | yes | 0 | 0 | 200.7 |
| `site-e-crawl-trap-ruby` | yes | `completed` | `public` | 13 | 8/11 | 1/1 | 0/0 | 1/1 | `fixpoint` | 1 | 108 | yes | 0 | 0 | 202.4 |
| `site-f-spa-deno` | yes | `completed` | `public` | 18 | 8/8 | 5/5 | 0/0 | 2/2 | `fixpoint` | 5 | 144 | yes | 0 | 0 | 140.6 |
| `site-g-discovery-lanes` | yes | `completed` | `manual_headers` | 41 | 9/9 | 0/0 | 11/11 | 0/0 | `fixpoint` | 1 | 346 | yes | 0 | 0 | 397.4 |
| `auth-a-simple-form` | yes | `completed` | `llm` | 12 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 0 | 0 | yes | 0 | 0 | 279.4 |
| `auth-b-http-basic` | yes | `completed` | `manual_headers` | 11 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 0 | 0 | yes | 0 | 1 | 73.5 |
| `auth-c-complex-form` | yes | `completed` | `llm` | 12 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 1 | 0 | yes | 0 | 0 | 285.5 |
| `auth-d-interactive-captcha` | yes | `completed` | `llm` | 12 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 1 | 0 | yes | 0 | 0 | 252.2 |
| `auth-e-delay-login` | yes | `completed` | `llm` | 12 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 1 | 0 | yes | 0 | 0 | 247.4 |
| `auth-f-ocr-captcha` | yes | `completed` | `llm` | 13 | 8/8 | 0/0 | 0/0 | 0/0 | `fixpoint` | 1 | 0 | yes | 0 | 0 | 256.1 |
| `auth-g-multi-step` | yes | `completed` | `llm` | 14 | 8/8 | 0/0 | 0/0 | 0/0 | `fixpoint` | 1 | 0 | yes | 0 | 0 | 259.7 |
| `auth-h-new-window` | yes | `completed` | `llm` | 14 | 8/8 | 0/0 | 0/0 | 0/0 | `fixpoint` | 2 | 0 | yes | 0 | 1 | 322.6 |
| `auth-i-iframe` | yes | `completed` | `llm` | 12 | 8/8 | 0/0 | 0/0 | 0/0 | `fixpoint` | 0 | 0 | yes | 0 | 0 | 226.2 |
| `auth-j-xsrf-token` | yes | `completed` | `llm` | 13 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 2 | 0 | yes | 0 | 0 | 248.9 |
| `auth-k-dynamic-fields` | yes | `completed` | `llm` | 11 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 0 | 0 | yes | 0 | 0 | 253.3 |
| `auth-l-security-question` | yes | `completed` | `llm` | 12 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 1 | 0 | yes | 0 | 0 | 298.8 |
| `auth-m-totp-mfa` | yes | `completed` | `llm` | 11 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 0 | 0 | yes | 0 | 0 | 255.1 |
| `auth-o-bearer-token` | yes | `completed` | `manual_headers` | 10 | 7/7 | 0/0 | 0/0 | 0/0 | `fixpoint` | 0 | 0 | yes | 0 | 0 | 76.8 |
| `crawlground` | yes | `failed` | `public` | 0 | 0/1 | 0/0 | 0/0 | 0/0 | `-` | 0 | 0 | yes | 0 | 0 | 620.5 |
| `juice-shop` | yes | `completed` | `public` | 330 | 2/3 | 0/0 | 0/0 | 0/0 | `budget_exhausted` | 216 | 0 | yes | 0 | 0 | 370.8 |
| `parabank` | yes | `failed` | `public` | 0 | 0/4 | 0/0 | 0/0 | 0/0 | `-` | 0 | 0 | yes | 0 | 0 | 643.8 |

## Errors and observed destructive markers

- `site-a-static`: ['GET /workspace/deleted.html']
- `auth-b-http-basic`: ['GET /logout']
- `auth-h-new-window`: ['GET /logout']
- `crawlground`: Katana pure-headless: Katana did not exhaust every input queue (1 incomplete)
- `parabank`: Katana pure-headless: Katana did not exhaust every input queue (1 incomplete)

## Missing expected endpoints

- `site-e-crawl-trap-ruby`: `GET /calendar/2026/01/02`, `GET /calendar/2026/01/31`, `GET /calendar/2026/02/02`
- `crawlground`: `GET /`
- `juice-shop`: `GET /rest/products/search`
- `parabank`: `GET /parabank/`, `GET /parabank/about.htm`, `GET /parabank/index.htm`, `GET /parabank/services.htm`

## Controlled qualification gate

PASS. Every repository-controlled fixture satisfied readiness, completion, persistence, isolation, discovery-fixpoint, endpoint, capability-marker, and request-sequence gates. Public canaries are reported but non-blocking.

Machine-readable results: `docs/complete-system-qualification.json`
