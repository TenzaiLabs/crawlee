from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from functools import wraps

from flask import Flask, Response, redirect, render_template_string, request, session, url_for

PATTERN_KEY = "auth-m-totp-mfa"
PATTERN_TITLE = "TOTP MFA Challenge"

VALID_EMAIL = f"{PATTERN_KEY}@auth.local"
VALID_PASSWORD = "pa$$w0rd"
TOTP_SECRET = "I65VU7K5ZQL7WB4E"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "auth-pattern-dev-secret")

APP_PAGES = [
    ("overview", "Overview"),
    ("projects", "Projects"),
    ("billing", "Billing"),
    ("reports", "Reports"),
    ("audit", "Audit"),
]


def render_page(title: str, body: str) -> str:
    links = ""
    if is_authenticated():
        app_links = "".join([f'<li><a href="/app/{slug}">{label}</a></li>' for slug, label in APP_PAGES])
        links = f"<h2>Workspace</h2><ul>{app_links}</ul>"
    return render_template_string(
        """
        <!doctype html>
        <html>
        <head><meta charset="utf-8"><title>{{ title }}</title></head>
        <body>
            <h1>{{ title }}</h1>
            <nav>
                <a href="/">Home</a> |
                <a href="/login">Login</a> |
                <a href="/logout">Logout</a>
            </nav>
            <hr>
            {{ body|safe }}
            {{ links|safe }}
        </body>
        </html>
        """,
        title=title,
        pattern=PATTERN_TITLE,
        body=body,
        links=links,
    )


def is_authenticated() -> bool:
    return bool(session.get("auth"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/")
def home() -> str:
    body = (
        "<p>Sign in to access the application workspace.</p>"
        "<p><a href='/login'>Continue</a></p>"
    )
    return render_page("Authentication Pattern Fixture", body)


@app.route("/login", methods=["GET", "POST"])
def login() -> str | Response:
    message = ""
    if request.method == "POST":
        if handle_login_submit():
            session["auth"] = True
            return redirect(request.args.get("next") or "/app/overview")
        message = "<p style='color:red'>Authentication failed</p>"

    form_html = login_form_html()
    return render_page("Login", message + form_html)


def login_form_html() -> str:
    extras = "<label>MFA code <input name='mfa' autocomplete='one-time-code'></label><br>"
    return (
        "<form method='post'>"
        "<label>E-Mail <input name='email'></label><br>"
        "<label>Password <input name='password' type='password'></label><br>"
        f"{extras}"
        "<button type='submit'>Sign In</button></form>"
    )


def totp_code(secret: str, timestamp: int | None = None) -> str:
    counter = int((time.time() if timestamp is None else timestamp) // 30)
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def is_valid_totp(code: str) -> bool:
    candidate = code.strip()
    if not candidate.isdigit() or len(candidate) != 6:
        return False
    now = int(time.time())
    return any(candidate == totp_code(TOTP_SECRET, now + offset * 30) for offset in (-1, 0, 1))


def handle_login_submit() -> bool:
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()

    if email != VALID_EMAIL or password != VALID_PASSWORD:
        return False

    if not is_valid_totp(request.form.get("mfa", "")):
        return False
    return True


@app.route("/logout")
def logout() -> Response:
    session.clear()
    return redirect("/")


for slug, label in APP_PAGES:
    endpoint = f"app_{slug}"

    def make_view(page_slug: str, page_label: str):
        @app.route(f"/app/{page_slug}", endpoint=f"app_{page_slug}")
        @login_required
        def view_page() -> str:
            body = f"<p>{page_label} content for {PATTERN_TITLE}.</p>"
            return render_page(page_label, body)

        return view_page

    make_view(slug, label)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
