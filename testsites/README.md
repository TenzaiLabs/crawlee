# Test Target Websites

`testsites/` contains 20 small, deterministic websites for crawler testing:
6 general fixtures and 14 authentication fixtures. Each site has a
`sitemap.json` that defines the expected crawlable GET surface.

Authenticated sites expose logout controls after login. The sites also include
small create, update, and delete workflows that return realistic confirmation
pages without persisting business data.

## Run

Start all sites:

```bash
cd testsites
docker compose up --build
```

Run one site:

```bash
cd testsites/site-b-login-flask
docker build -t crawler-site-b .
docker run --rm -p 8002:8000 crawler-site-b
```

Verify all `sitemap.json` entries:

```bash
cd testsites
python verify_sites.py
```

The verifier uses direct ports. The Docker Compose gateway also exposes the
same sites on the `910x` and `920x` port ranges.

## Ports

| Site | Direct | Gateway |
| --- | --- | --- |
| `site-a-static` | `8001` | `9101` |
| `site-b-login-flask` | `8002` | `9102` |
| `site-c-registration-express` | `8003` | `9103` |
| `site-d-complex-auth-go` | `8004` | `9104` |
| `site-e-crawl-trap-ruby` | `8005` | `9105` |
| `site-f-spa-deno` | `8006` | `9106` |
| `auth-a-simple-form` | `8101` | `9201` |
| `auth-b-http-basic` | `8102` | `9202` |
| `auth-c-complex-form` | `8103` | `9203` |
| `auth-d-interactive-captcha` | `8104` | `9204` |
| `auth-e-delay-login` | `8105` | `9205` |
| `auth-f-ocr-captcha` | `8106` | `9206` |
| `auth-g-multi-step` | `8107` | `9207` |
| `auth-h-new-window` | `8108` | `9208` |
| `auth-i-iframe` | `8109` | `9209` |
| `auth-j-xsrf-token` | `8110` | `9210` |
| `auth-k-dynamic-fields` | `8111` | `9211` |
| `auth-l-security-question` | `8112` | `9212` |
| `auth-m-totp-mfa` | `8113` | `9213` |
| `auth-o-bearer-token` | `8115` | `9215` |

Use `http://localhost:<port>` for either direct or gateway access.

## General Fixtures

- `site-a-static`: Static HTML with ordinary internal links and a public
  workspace action page.
- `site-b-login-flask`: Flask form login with private dashboard, settings,
  reports, action pages, and logout.
- `site-c-registration-express`: Express registration fixture. Registration
  POST returns `409`; `/workspace` exposes public mock action forms.
- `site-d-complex-auth-go`: Go form login with sign-in and registration buttons
  on the same form, protected action pages, and logout.
- `site-e-crawl-trap-ruby`: Sinatra calendar crawl trap with unbounded date
  links and a bounded public workspace page.
- `site-f-spa-deno`: Deno SPA that loads links from `/api/links` and exposes
  client-rendered action forms with server-side POST fallbacks.

## Auth Fixtures

Common app pages after authentication:

- `/app/overview`
- `/app/projects`
- `/app/billing`
- `/app/reports`
- `/app/audit`

Each authenticated app page includes a logout button and create, update, and
delete forms. The form handlers live under `/app/actions/...` and do not persist
data.

Common credentials:

- E-mail: `<site-name>@auth.local`
- Password: `pa$$w0rd`

Additional auth config:

| Fixture | Additional config |
| --- | --- |
| `auth-b-http-basic` | `Authorization: Basic dXNlcjpwYXNz` |
| `auth-d-interactive-captcha` | Challenge code `588357` |
| `auth-f-ocr-captcha` | CAPTCHA code `4319`; the page shows only an image |
| `auth-l-security-question` | Security answer `42` |
| `auth-m-totp-mfa` | TOTP seed `I65VU7K5ZQL7WB4E` |
| `auth-o-bearer-token` | `Authorization: Bearer t0k3nId` |

Auth roster:

- `auth-a-simple-form`: Standard e-mail and password form login.
- `auth-b-http-basic`: HTTP Basic challenge using `401` and
  `WWW-Authenticate`.
- `auth-c-complex-form`: Form login with tenant and region fields.
- `auth-d-interactive-captcha`: Form login with a challenge-code field.
- `auth-e-delay-login`: Form login with a delayed successful response.
- `auth-f-ocr-captcha`: Form login with OCR CAPTCHA image and input.
- `auth-g-multi-step`: Two-page login flow.
- `auth-h-new-window`: Login form on a popup route.
- `auth-i-iframe`: Login form embedded in an iframe route.
- `auth-j-xsrf-token`: Form login with a session-bound anti-CSRF token.
- `auth-k-dynamic-fields`: Credential field names change on each load.
- `auth-l-security-question`: Form login with a security-answer field.
- `auth-m-totp-mfa`: Form login with TOTP MFA validation.
- `auth-o-bearer-token`: Header-only bearer-token access.

## Missing Auth Coverage

The current roster does not cover:

- OAuth, OpenID Connect, social login, and external IdP redirects.
- SAML SSO.
- WebAuthn, passkeys, and hardware security keys.
- Device-code, QR-code, and push-approval flows.
- Digest auth, NTLM, Kerberos, and SPNEGO.
- Client certificate and mTLS auth.
- Password reset, invite acceptance, and account recovery.
- Risk-based or step-up auth.
- Rate-limit, lockout, and anti-bruteforce behavior.
- Session refresh, rotation, token expiry, and refresh-token flows.
- Cross-domain auth, callback, and cookie flows across multiple hosts.

## Missing Website Coverage

The current roster also does not cover these non-auth website patterns:

- Search pages with filters, sorting, and paginated results.
- Catalogs with category pages, next/previous pagination, and duplicate URL
  pressure.
- Faceted navigation with many filter combinations and canonical URLs.
- Infinite scroll and lazy-loaded content.
- JavaScript-heavy pages where the main content comes from JSON or XHR APIs.
- Safe public form workflows such as contact, quote, feedback, and support
  forms.
- File and document libraries with PDFs, CSVs, images, and downloadable assets.
- Redirect-heavy sites with canonicalization, trailing-slash handling, and
  redirect chains.
- Error-state sites with intentional `404`, `410`, `429`, `500`, and soft-404
  pages.
- Sites with `robots.txt`, XML sitemaps, disallowed paths, and canonical tags.
- Cookie or consent banners that affect visible content.
- Multi-language sites with locale prefixes and `hreflang`.
- Multi-host or cross-subdomain sites.
- Media-heavy pages with video, responsive images, and lazy images.
- Data dashboards with sortable tables, expandable rows, tabs, and accordions.
- Hash-route SPAs using `/#/route` navigation.
- Web components and shadow DOM.
- Pages with links or forms inside menus, hidden navigation, and ARIA-driven
  controls.
- Session or cookie personalization without authentication.
- Deterministic throttle and rate-limit behavior.
