# Testsite Crawler Comparison Report

Generated: `2026-06-17T13:04:58+03:00`

## Run Configuration

- Sites: `20` canonical fixtures from `testsites/`.
- Crawl duration: `25s`.
- Max depth/pages: `3` / `80`.
- Headless Katana hybrid mode: `False`.
- Safety guards: default dangerous-path exclusions plus auth-recorded blocked URLs.
- No-auth-agent variant keeps manual `Authorization` headers because header-only auth does not invoke the AI auth agent.

## Summary

| Variant | PASS | Access OK | Safe OK | Unsafe | No access | Failed jobs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Full crawler | 20 | 20 | 20 | 0 | 0 | 0 |
| Crawler with no safety guards | 19 | 20 | 19 | 1 | 0 | 0 |
| Crawler with no auth agent and no safety guards | 6 | 6 | 20 | 0 | 14 | 0 |

## Improvement Readout

- Full crawler passed `20/20` sites, compared with `19/20` with safety disabled and `6/20` with both auth agent and safety disabled.
- Safety guards reduced unsafe successful crawls from `1` to `0`.
- The auth agent increased access-ok crawls from `6` to `20`.

## Matrix

| Site | Mode | Full crawler | No safety guards | No auth agent + no safety |
| --- | --- | --- | --- | --- |
| `auth-a-simple-form` | `llm` | `PASS` | `UNSAFE (1 blocked)` | `NO ACCESS` |
| `auth-b-http-basic` | `manual_headers` | `PASS` | `PASS` | `PASS` |
| `auth-c-complex-form` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-d-interactive-captcha` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-e-delay-login` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-f-ocr-captcha` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-g-multi-step` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-h-new-window` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-i-iframe` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-j-xsrf-token` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-k-dynamic-fields` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-l-security-question` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-m-totp-mfa` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `auth-o-bearer-token` | `manual_headers` | `PASS` | `PASS` | `PASS` |
| `site-a-static` | `public` | `PASS` | `PASS` | `PASS` |
| `site-b-login-flask` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `site-c-registration-express` | `public` | `PASS` | `PASS` | `PASS` |
| `site-d-complex-auth-go` | `llm` | `PASS` | `PASS` | `NO ACCESS` |
| `site-e-crawl-trap-ruby` | `public` | `PASS` | `PASS` | `PASS` |
| `site-f-spa-deno` | `public` | `PASS` | `PASS` | `PASS` |

## Failures And Unsafe Hits

| Variant | Site | Status | Entries | Detail |
| --- | --- | --- | ---: | --- |
| `no_safety` | `auth-a-simple-form` | `completed` | 11 | http://localhost:8101/app/danger/close-account |
| `no_auth_no_safety` | `auth-a-simple-form` | `completed` | 4 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-c-complex-form` | `completed` | 3 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-d-interactive-captcha` | `completed` | 5 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-e-delay-login` | `completed` | 3 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-f-ocr-captcha` | `completed` | 4 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-g-multi-step` | `completed` | 4 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-h-new-window` | `completed` | 4 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-i-iframe` | `completed` | 4 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-j-xsrf-token` | `completed` | 3 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-k-dynamic-fields` | `completed` | 3 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-l-security-question` | `completed` | 5 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `auth-m-totp-mfa` | `completed` | 3 | probe path was not crawled: /app/overview |
| `no_auth_no_safety` | `site-b-login-flask` | `completed` | 4 | probe path was not crawled: /dashboard |
| `no_auth_no_safety` | `site-d-complex-auth-go` | `completed` | 4 | probe path was not crawled: /app |
