# Phase 0 Current Crawler Baseline

Generated: `2026-07-22T19:38:27.342418+00:00`

This is the pre-browser-discovery result from a real `uv run tenzai-crawler-server` process, exercised only through its HTTP API. Browser-only coverage is expected to remain low or zero.

## Run summary

- Completed safely: 23/23 targets.
- Sitemap entries: 494.
- Browser-only fixture controls found: 0/10.
- Persisted results verified after server restart: 23/23.
- Cross-job isolation violations: 0.
- Blocked-route hits: 0.
- CrawlGround: 32/59 controls (54%); scored controls: `forms.09-js-form-submit`, `forms.11-fetch-get`, `forms.12-xhr-post`, `frames.04-shadow-dom-closed`, `js-events.01-mouseover-injected`, `js-events.02-onclick-prevent-default`, `js-events.03-focus-injected`, `js-events.04-css-hover-dropdown`, `js-events.05-multilevel-dropdown`, `js-events.06-dialog-modal`, `js-events.07-dblclick`, `js-events.08-keydown-enter`, `js-events.09-contextmenu`, `js-events.10-tab-panel`, `js-events.11-popover`, `js-events.12-pointer-events`, `js-events.13-long-press`, `js-events.14-custom-event`, `links.01-anchor-href`, `links.02-js-built-href`, `links.04-svg-anchor`, `links.05-target-blank`, `links.06-javascript-href`, `navigation.01-meta-refresh`, `navigation.02-window-location-timeout`, `navigation.03-history-push-state`, `navigation.04-hash-routing`, `navigation.05-window-replace`, `navigation.06-http-redirect`, `navigation.08-window-open`, `navigation.09-history-replace-state`, `other.01-bot-detection-globals`.

| Target | Ready | Crawl | Auth mode | Entries | Expected | Browser-only | Persisted | Isolation | Blocked | Seconds |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| `site-a-static` | yes | `completed` | `public` | 13 | 11/11 | 0/0 | yes | 0 | 0 | 104.2 |
| `site-b-login-flask` | yes | `completed` | `llm` | 6 | 3/7 | 0/4 | yes | 0 | 0 | 103.2 |
| `site-c-registration-express` | yes | `completed` | `public` | 9 | 8/8 | 0/0 | yes | 0 | 0 | 91.8 |
| `site-d-complex-auth-go` | yes | `completed` | `llm` | 4 | 3/6 | 0/0 | yes | 0 | 0 | 48.1 |
| `site-e-crawl-trap-ruby` | yes | `completed` | `public` | 14 | 11/11 | 0/1 | yes | 0 | 0 | 97.8 |
| `site-f-spa-deno` | yes | `completed` | `public` | 10 | 7/8 | 0/5 | yes | 0 | 0 | 92.1 |
| `auth-a-simple-form` | yes | `completed` | `llm` | 13 | 6/7 | 0/0 | yes | 0 | 0 | 122.3 |
| `auth-b-http-basic` | yes | `completed` | `manual_headers` | 8 | 7/7 | 0/0 | yes | 0 | 0 | 203.8 |
| `auth-c-complex-form` | yes | `completed` | `llm` | 14 | 6/7 | 0/0 | yes | 0 | 0 | 119.7 |
| `auth-d-interactive-captcha` | yes | `completed` | `llm` | 12 | 6/7 | 0/0 | yes | 0 | 0 | 122.2 |
| `auth-e-delay-login` | yes | `completed` | `llm` | 11 | 6/7 | 0/0 | yes | 0 | 0 | 191.9 |
| `auth-f-ocr-captcha` | yes | `completed` | `llm` | 14 | 6/8 | 0/0 | yes | 0 | 0 | 135.4 |
| `auth-g-multi-step` | yes | `completed` | `llm` | 15 | 6/8 | 0/0 | yes | 0 | 0 | 140.7 |
| `auth-h-new-window` | yes | `completed` | `llm` | 13 | 6/8 | 0/0 | yes | 0 | 0 | 139.7 |
| `auth-i-iframe` | yes | `completed` | `llm` | 13 | 6/8 | 0/0 | yes | 0 | 0 | 135.2 |
| `auth-j-xsrf-token` | yes | `completed` | `llm` | 10 | 6/7 | 0/0 | yes | 0 | 0 | 142.1 |
| `auth-k-dynamic-fields` | yes | `completed` | `llm` | 14 | 6/7 | 0/0 | yes | 0 | 0 | 129.2 |
| `auth-l-security-question` | yes | `completed` | `llm` | 15 | 6/7 | 0/0 | yes | 0 | 0 | 123.2 |
| `auth-m-totp-mfa` | yes | `completed` | `llm` | 12 | 6/7 | 0/0 | yes | 0 | 0 | 153.4 |
| `auth-o-bearer-token` | yes | `completed` | `manual_headers` | 8 | 7/7 | 0/0 | yes | 0 | 0 | 184.2 |
| `crawlground` | yes | `completed` | `public` | 80 | 1/1 | 0/0 | yes | 0 | 0 | 269.1 |
| `juice-shop` | yes | `completed` | `public` | 93 | 2/3 | 0/0 | yes | 0 | 0 | 316.9 |
| `parabank` | yes | `completed` | `public` | 93 | 1/4 | 0/0 | yes | 0 | 0 | 203.8 |

## Errors and safety findings

No crawler failures or blocked-route hits were observed.

Machine-readable results: `docs/phase0-baseline-results.json`
