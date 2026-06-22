# Testsite Crawler Comparison Report

Generated: `2026-06-21T23:36:05+03:00`

## Run Configuration

- Sites: `20` canonical fixtures from `testsites/`.
- Runs per selected site/variant: `5`.
- Crawl duration: `25s`.
- Max depth/pages: `3` / `80`.
- Headless Katana hybrid mode: `False`.
- Safety guards: default dangerous-path exclusions plus auth-recorded blocked URLs.
- No-auth-agent variant keeps manual `Authorization` headers because header-only auth does not invoke the AI auth agent.

## Summary

| Variant | Jobs | PASS | Access OK | Safe OK | Unsafe | No access | Failed jobs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Full crawler | 100 | 98 | 98 | 98 | 0 | 0 | 2 |

## Improvement Readout

- Full crawler median job time was `55.9s`.
- Full crawler repeated-result reliability was `19/20` sites over `5` runs.

## Speed

| Variant | Jobs | Average | Median | Min | Max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full crawler | 100 | 54.7s | 55.9s | 35.4s | 187.0s |

## Reliability

| Variant | Runs | Stable sites | Reliability | Unstable sites |
| --- | ---: | ---: | ---: | --- |
| Full crawler | 5 | 19/20 | 95.0% | `auth-m-totp-mfa` |

## Matrix

| Site | Mode | Full crawler |
| --- | --- | --- |
| `auth-a-simple-form` | `llm` | `PASS (5/5)` |
| `auth-b-http-basic` | `manual_headers` | `PASS (5/5)` |
| `auth-c-complex-form` | `llm` | `PASS (5/5)` |
| `auth-d-interactive-captcha` | `llm` | `PASS (5/5)` |
| `auth-e-delay-login` | `llm` | `PASS (5/5)` |
| `auth-f-ocr-captcha` | `llm` | `PASS (5/5)` |
| `auth-g-multi-step` | `llm` | `PASS (5/5)` |
| `auth-h-new-window` | `llm` | `PASS (5/5)` |
| `auth-i-iframe` | `llm` | `PASS (5/5)` |
| `auth-j-xsrf-token` | `llm` | `PASS (5/5)` |
| `auth-k-dynamic-fields` | `llm` | `PASS (5/5)` |
| `auth-l-security-question` | `llm` | `PASS (5/5)` |
| `auth-m-totp-mfa` | `llm` | `FLAKY: PASS 3, FAIL 2` |
| `auth-o-bearer-token` | `manual_headers` | `PASS (5/5)` |
| `site-a-static` | `public` | `PASS (5/5)` |
| `site-b-login-flask` | `llm` | `PASS (5/5)` |
| `site-c-registration-express` | `public` | `PASS (5/5)` |
| `site-d-complex-auth-go` | `llm` | `PASS (5/5)` |
| `site-e-crawl-trap-ruby` | `public` | `PASS (5/5)` |
| `site-f-spa-deno` | `public` | `PASS (5/5)` |

## Failures And Unsafe Hits

| Variant | Site | Status | Entries | Detail |
| --- | --- | --- | ---: | --- |
| `full` run `1` | `auth-m-totp-mfa` | `cancelled` | 0 | timed out after 180.0s |
| `full` run `3` | `auth-m-totp-mfa` | `cancelled` | 0 | timed out after 180.0s |
