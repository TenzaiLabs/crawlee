# Test Target Websites

Twenty standalone target sites for crawler testing: six original mixed-stack fixtures and fourteen authentication-focused fixtures based on the scenarios listed at `authenticationtest.com`.

## Run All Sites + Nginx Gateway

```bash
cd testsites
docker compose up --build
```

Gateway ports (all served by the Nginx container):
- Site A (static): http://localhost:9101
- Site B (login Flask): http://localhost:9102
- Site C (registration Express): http://localhost:9103
- Site D (complex auth Go): http://localhost:9104
- Site E (crawl trap Sinatra): http://localhost:9105
- Site F (SPA Deno): http://localhost:9106
- Auth A (simple form): http://localhost:9201
- Auth B (HTTP basic): http://localhost:9202
- Auth C (complex form): http://localhost:9203
- Auth D (interactive auth): http://localhost:9204
- Auth E (delayed login): http://localhost:9205
- Auth F (OCR captcha): http://localhost:9206
- Auth G (multi-step login): http://localhost:9207
- Auth H (new-window login): http://localhost:9208
- Auth I (iframe login): http://localhost:9209
- Auth J (XSRF token): http://localhost:9210
- Auth K (dynamic field names): http://localhost:9211
- Auth L (security question): http://localhost:9212
- Auth M (TOTP MFA): http://localhost:9213
- Auth O (bearer token): http://localhost:9215

Direct container ports (for standalone access):
- Site A: http://localhost:8001
- Site B: http://localhost:8002
- Site C: http://localhost:8003
- Site D: http://localhost:8004
- Site E: http://localhost:8005
- Site F: http://localhost:8006
- Auth A: http://localhost:8101
- Auth B: http://localhost:8102
- Auth C: http://localhost:8103
- Auth D: http://localhost:8104
- Auth E: http://localhost:8105
- Auth F: http://localhost:8106
- Auth G: http://localhost:8107
- Auth H: http://localhost:8108
- Auth I: http://localhost:8109
- Auth J: http://localhost:8110
- Auth K: http://localhost:8111
- Auth L: http://localhost:8112
- Auth M: http://localhost:8113
- Auth O: http://localhost:8115

## Run An Individual Site

Each site has its own Dockerfile. Example:

```bash
cd testsites/site-b-login-flask
docker build -t crawler-site-b .
docker run --rm -p 8002:8000 crawler-site-b
```

## Site Notes

- Site B credentials: `demo` / `password`.
- Site D credentials: `admin` / `swordfish` and includes two submit buttons.
- Site C registration POST returns `409` to discourage submission.
- Site E generates infinite calendar links to test crawl depth limits.
- Site F inserts navigation links after loading `/api/links` in the browser.

## Authentication Pattern Coverage

Each auth site includes a login surface plus five post-login pages:
- `/app/overview`
- `/app/projects`
- `/app/billing`
- `/app/reports`
- `/app/audit`

Pattern mapping (all ideas listed on `https://authenticationtest.com/`):
- `auth-a-simple-form`: Simple Form Authentication.
- `auth-b-http-basic`: Basic HTTP/NTLM Authentication equivalent via HTTP Basic credentials (`user` / `pass`).
- `auth-c-complex-form`: Complex Form Authentication with extra tenant/region fields.
- `auth-d-interactive-captcha`: Interactive Authentication with challenge code (`588357`).
- `auth-e-delay-login`: Delayed login challenge.
- `auth-f-ocr-captcha`: OCR challenge emulation (code `4319`).
- `auth-g-multi-step`: Multi-Page Challenge (`/login` then `/login/step2`).
- `auth-h-new-window`: New Window Challenge (`/popup-login`).
- `auth-i-iframe`: IFrame login challenge (`/frame-login`).
- `auth-j-xsrf-token`: Cross-Site Request Forgery token challenge.
- `auth-k-dynamic-fields`: Dynamic Field Names challenge.
- `auth-l-security-question`: Security Question challenge (answer `42`).
- `auth-m-totp-mfa`: TOTP MFA challenge (seed helper at `/totp-seed`, demo code `123456`).
- `auth-o-bearer-token`: Bearer Token pattern (`Authorization: Bearer t0k3nId`).

Fixture credentials format:
- E-mail: `<site-name>@auth.local`
- Password: `pa$$w0rd`

## Verify Outside Containers

Run the local verifier to launch each site and check every URL in each `sitemap.json`:

```bash
cd testsites
python verify_sites.py
```

Each site has a `sitemap.json` file containing expected crawl output for direct ports. If you route through gateway ports, update the base URLs accordingly.
