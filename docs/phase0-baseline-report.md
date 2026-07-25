# Step 6 Dual-Pass Katana Baseline

Generated: `2026-07-23T18:10:41.888364+00:00`

This is the proxyless dual-pass baseline from a real `uv run tenzai-crawler-server` process, exercised only through its HTTP API. Every job uses bounded known-file discovery, standard Katana, shared-Chrome pure-headless Katana, and passive CDP evidence; guided LLM discovery is disabled.

## Run summary

- Completed safely: 22/24 targets.
- Sitemap entries: 382.
- Browser-only fixture controls found: 1/10.
- Lane-specific capability markers found: 10/10.
- Persisted results verified after server restart: 24/24.
- Cross-job isolation violations: 0.
- Declared destructive or session-ending markers observed: 2.
- Fixture-ledger entries: 235 (18 required, 0 destructive markers).
- CrawlGround: 44/59 controls (75%); scored controls: `buttons.01-html-button`, `buttons.02-div-onclick`, `dynamic-content.01-fetch-injected`, `dynamic-content.02-fetch-injected`, `dynamic-content.03-intersection-observer`, `dynamic-content.04-details-toggle`, `dynamic-content.05-sse-injected`, `dynamic-content.06-websocket-injected`, `dynamic-content.07-load-more`, `dynamic-content.08-template-element`, `dynamic-content.09-infinite-scroll`, `dynamic-content.10-localstorage`, `forms.05-fetch-post`, `forms.08-formaction-override`, `forms.09-js-form-submit`, `forms.11-fetch-get`, `frames.01-iframe-srcdoc`, `frames.02-shadow-dom`, `frames.04-shadow-dom-closed`, `js-events.01-mouseover-injected`, `js-events.02-onclick-prevent-default`, `js-events.03-focus-injected`, `js-events.04-css-hover-dropdown`, `js-events.05-multilevel-dropdown`, `js-events.06-dialog-modal`, `js-events.07-dblclick`, `js-events.08-keydown-enter`, `js-events.09-contextmenu`, `js-events.10-tab-panel`, `js-events.11-popover`, `links.01-anchor-href`, `links.02-js-built-href`, `links.04-svg-anchor`, `links.05-target-blank`, `links.06-javascript-href`, `navigation.01-meta-refresh`, `navigation.02-window-location-timeout`, `navigation.03-history-push-state`, `navigation.04-hash-routing`, `navigation.05-window-replace`, `navigation.06-http-redirect`, `navigation.08-window-open`, `navigation.09-history-replace-state`, `other.01-bot-detection-globals`.

| Target | Ready | Crawl | Auth mode | Entries | Expected | Browser-only | Lane markers | Ledger | Persisted | Isolation | Destructive | Seconds |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| `site-a-static` | yes | `completed` | `public` | 16 | 11/11 | 0/0 | 0/0 | 0 | yes | 0 | 1 | 69.0 |
| `site-b-login-flask` | yes | `completed` | `llm` | 13 | 7/7 | 1/4 | 0/0 | 83 | yes | 0 | 0 | 247.0 |
| `site-c-registration-express` | yes | `completed` | `public` | 11 | 8/8 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 91.1 |
| `site-d-complex-auth-go` | yes | `completed` | `llm` | 10 | 6/6 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 187.1 |
| `site-e-crawl-trap-ruby` | yes | `completed` | `public` | 12 | 8/11 | 0/1 | 0/0 | 41 | yes | 0 | 0 | 93.5 |
| `site-f-spa-deno` | yes | `completed` | `public` | 13 | 8/8 | 0/5 | 0/0 | 25 | yes | 0 | 0 | 55.4 |
| `site-g-discovery-lanes` | yes | `completed` | `manual_headers` | 36 | 9/9 | 0/0 | 10/10 | 86 | yes | 0 | 0 | 108.2 |
| `auth-a-simple-form` | yes | `completed` | `llm` | 12 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 241.5 |
| `auth-b-http-basic` | yes | `completed` | `manual_headers` | 11 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 1 | 55.8 |
| `auth-c-complex-form` | yes | `completed` | `llm` | 11 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 224.0 |
| `auth-d-interactive-captcha` | yes | `completed` | `llm` | 11 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 225.4 |
| `auth-e-delay-login` | yes | `completed` | `llm` | 11 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 221.3 |
| `auth-f-ocr-captcha` | yes | `completed` | `llm` | 12 | 8/8 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 222.8 |
| `auth-g-multi-step` | yes | `completed` | `llm` | 13 | 8/8 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 232.9 |
| `auth-h-new-window` | yes | `completed` | `llm` | 12 | 8/8 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 281.8 |
| `auth-i-iframe` | yes | `completed` | `llm` | 12 | 8/8 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 198.0 |
| `auth-j-xsrf-token` | yes | `completed` | `llm` | 11 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 223.8 |
| `auth-k-dynamic-fields` | yes | `completed` | `llm` | 11 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 225.9 |
| `auth-l-security-question` | yes | `completed` | `llm` | 11 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 277.8 |
| `auth-m-totp-mfa` | yes | `completed` | `llm` | 11 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 225.4 |
| `auth-o-bearer-token` | yes | `completed` | `manual_headers` | 10 | 7/7 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 55.9 |
| `crawlground` | yes | `failed` | `public` | 0 | 0/1 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 622.1 |
| `juice-shop` | yes | `completed` | `public` | 112 | 2/3 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 55.4 |
| `parabank` | yes | `failed` | `public` | 0 | 0/4 | 0/0 | 0/0 | 0 | yes | 0 | 0 | 645.3 |

## Errors and observed destructive markers

- `site-a-static`: ['GET /workspace/deleted.html']
- `auth-b-http-basic`: ['GET /logout']
- `crawlground`: Katana did not exhaust every input queue (1 incomplete)
- `parabank`: Katana pure-headless: Katana did not exhaust every input queue (1 incomplete)

## Missing expected endpoints

- `site-e-crawl-trap-ruby`: `GET /calendar/2026/01/02`, `GET /calendar/2026/01/31`, `GET /calendar/2026/02/02`
- `crawlground`: `GET /`
- `juice-shop`: `GET /rest/products/search`
- `parabank`: `GET /parabank/`, `GET /parabank/about.htm`, `GET /parabank/index.htm`, `GET /parabank/services.htm`

Machine-readable results: `docs/phase0-baseline-results.json`
