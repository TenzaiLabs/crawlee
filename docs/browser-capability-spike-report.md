# Browser, CDP, and Katana Capability Spike

Generated: `2026-07-23T13:12:33.564563+00:00`

- Result: 24/24 checks passed.
- Katana: `[INF] Current version: v1.6.1-tenzai.1`.
- Katana binary SHA-256: `e88b5e90cb4e6305de02f52abe2ca69ce6a45cb3275860a88e432eff612f229e`.
- DIT model SHA-256: `eba238b3093ff7aa4772ce17536bc313cb955428a6aa87dae41695a2dede6e59`.
- Chrome: `Chrome/149.0.7827.55`.
- Requests were observed through CDP only; the observer did not enable Fetch or continue, fail, rewrite, or block requests.

| Check | Lane | Result | Evidence |
| --- | --- | --- | --- |
| `playwright-katana-playwright-handoff` | `browser-handoff` | pass | fresh Playwright page retained the cookie and origin localStorage after -cwu |
| `passive-cdp-popup` | `passive-cdp` | pass | observed GET /api/observer/popup from CDP target type page |
| `passive-cdp-frame` | `passive-cdp` | pass | observed GET /api/observer/frame from CDP target type page |
| `passive-cdp-worker` | `passive-cdp` | pass | observed GET /api/observer/worker from CDP target type worker |
| `passive-cdp-service_worker` | `passive-cdp` | pass | observed GET /api/observer/service-worker from CDP target type service_worker |
| `standard-js-crawl` | `standard` | pass | -jc emitted the regex marker endpoint |
| `standard-jsluice` | `standard` | pass | -jsl emitted the concatenated JavaScript marker endpoint |
| `standard-form-extraction` | `standard` | pass | -fx emitted structured form metadata |
| `standard-header` | `standard` | pass | -H reached the header-protected page with status 200 |
| `standard-known-files` | `accepted-but-ineffective` | pass | -kf fetched the nested sitemap document but did not enqueue its URL marker |
| `standard-tech-detect` | `standard` | pass | -td emitted technology metadata |
| `standard-knowledge-base` | `standard` | pass | -kb emitted page classification metadata |
| `standard-page-type-filter` | `standard` | pass | -fpt parked ran with the pinned classifier model and retained non-parked pages |
| `standard-breadth-first-strategy` | `standard` | pass | the standard engine accepted the explicit breadth-first strategy |
| `standard-max-response-size` | `standard` | pass | no standard response body exceeded the configured 5 MiB reader limit |
| `standard-terminal-summary` | `standard` | pass | source-pinned Katana reported queue_exhausted for the standard input |
| `pure-headless-serial-seed-one` | `pure-headless` | pass | -cwu -p 1 emitted the one seed child |
| `pure-headless-serial-seed-two` | `pure-headless` | pass | -cwu -p 1 emitted the two seed child |
| `pure-headless-header` | `pure-headless` | pass | passive CDP observed status 200 for the -H-protected pure-headless input; Katana retained only its request-only record |
| `pure-headless-terminal-summary` | `pure-headless` | pass | source-pinned Katana emitted per-input machine-readable terminal reasons |
| `pure-headless-runtime-xhr` | `pure-headless` | pass | -xhr emitted the runtime fetch marker |
| `pure-headless-filtering-flags` | `pure-headless` | pass | -iqp and -fsu -fst 10 completed with useful output |
| `pure-headless-known-files-separate` | `accepted-but-ineffective` | pass | -kf is intentionally confined to the standard lane because pure headless does not consume it |
| `pure-headless-max-response-size` | `accepted-but-ineffective` | pass | -mrs is accepted by pure headless but does not bound CDP response materialization |

## Interpretation

Every capability required for the production shared-browser lifecycle passed.

The Chrome process remained alive across Playwright disconnect, Katana `-cwu`, and a fresh Playwright connection. The post-Katana page observed the original cookie and origin `localStorage`; no tab, DOM, `sessionStorage`, or partial wizard state was retained as a requirement.

Machine-readable results: `docs/browser-capability-spike-results.json`
