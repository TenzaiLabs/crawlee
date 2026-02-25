from __future__ import annotations

import os
import secrets
import time
from functools import wraps

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for

PATTERN_KEY = "auth-d-interactive-captcha"
PATTERN_TITLE = "Interactive Authentication"

VALID_EMAIL = f"{PATTERN_KEY}@auth.local"
VALID_PASSWORD = "pa$$w0rd"

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
    links = "".join([f'<li><a href="/app/{slug}">{label}</a></li>' for slug, label in APP_PAGES])
    return render_template_string(
        """
        <!doctype html>
        <html>
        <head><meta charset="utf-8"><title>{{ title }}</title></head>
        <body>
            <h1>{{ title }}</h1>
            <p><strong>Pattern:</strong> {{ pattern }}</p>
            <nav>
                <a href="/">Home</a> |
                <a href="/login">Login</a> |
                <a href="/logout">Logout</a>
            </nav>
            <hr>
            {{ body|safe }}
            <h2>Post-login pages</h2>
            <ul>{{ links|safe }}</ul>
        </body>
        </html>
        """,
        title=title,
        pattern=PATTERN_TITLE,
        body=body,
        links=links,
    )


def is_authenticated() -> bool:
    if session.get("auth"):
        return True
    if PATTERN_KEY == "auth-b-http-basic":
        auth = request.authorization
        if auth and auth.username == "user" and auth.password == "pass":
            return True
    if PATTERN_KEY == "auth-o-bearer-token":
        header = request.headers.get("Authorization", "")
        if header == "Bearer t0k3nId":
            return True
    return False


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/")
def home() -> str:
    details = {
        "auth-a-simple-form": "Simple username/password form.",
        "auth-b-http-basic": "HTTP Basic auth (user/pass) plus optional form login.",
        "auth-c-complex-form": "Form with tenant, region, and remember-me fields.",
        "auth-d-interactive-captcha": "Interactive challenge code must be entered.",
        "auth-e-delay-login": "Server intentionally delays successful login response.",
        "auth-f-ocr-captcha": "OCR-style text challenge field required.",
        "auth-g-multi-step": "Username and password submitted across two steps.",
        "auth-h-new-window": "Authentication form opens in a new window route.",
        "auth-i-iframe": "Login form is embedded in an iframe route.",
        "auth-j-xsrf-token": "Login requires anti-CSRF token and cookie-backed session state.",
        "auth-k-dynamic-fields": "Credential input names are dynamic on every page load.",
        "auth-l-security-question": "Login requires a case-insensitive security answer.",
        "auth-m-totp-mfa": "Password + MFA code required. Seed exposed via helper endpoint.",
        "auth-n-session-hijack": "Session takeover path accepts issued session transfer token.",
        "auth-o-bearer-token": "Bearer token auth unlocks access (Authorization header).",
    }
    body = (
        f"<p>{details[PATTERN_KEY]}</p>"
        f"<p>Credentials: <code>{VALID_EMAIL}</code> / <code>{VALID_PASSWORD}</code></p>"
        "<p><a href='/login'>Proceed to login</a></p>"
    )
    return render_page("Authentication Pattern Fixture", body)


@app.route("/login", methods=["GET", "POST"])
def login() -> str | Response:
    if PATTERN_KEY == "auth-g-multi-step":
        return handle_multi_step_login()
    if PATTERN_KEY == "auth-h-new-window":
        return handle_new_window_login()
    if PATTERN_KEY == "auth-i-iframe":
        return handle_iframe_login()

    message = ""
    if request.method == "POST":
        if handle_login_submit():
            session["auth"] = True
            return redirect(request.args.get("next") or "/app/overview")
        message = "<p style='color:red'>Authentication failed</p>"

    form_html = login_form_html()
    return render_page("Login", message + form_html)


def login_form_html() -> str:
    if PATTERN_KEY == "auth-k-dynamic-fields":
        email_field = f"email_{secrets.token_hex(2)}"
        password_field = f"password_{secrets.token_hex(2)}"
        session["dynamic_email_field"] = email_field
        session["dynamic_password_field"] = password_field
        return (
            "<form method='post'>"
            f"<label>{email_field}<input name='{email_field}'></label><br>"
            f"<label>{password_field}<input name='{password_field}' type='password'></label><br>"
            "<button type='submit'>Sign In</button></form>"
        )

    extras = ""
    if PATTERN_KEY == "auth-c-complex-form":
        extras = (
            "<label>Tenant <input name='tenant' value='north'></label><br>"
            "<label>Region <select name='region'><option>us-east</option></select></label><br>"
            "<label><input type='checkbox' name='remember'>Remember me</label><br>"
        )
    elif PATTERN_KEY == "auth-d-interactive-captcha":
        extras = "<label>Challenge Code (588357) <input name='challenge'></label><br>"
    elif PATTERN_KEY == "auth-f-ocr-captcha":
        extras = "<p>OCR Challenge Text: <strong>4319</strong></p><label>Captcha <input name='captcha'></label><br>"
    elif PATTERN_KEY == "auth-j-xsrf-token":
        token = secrets.token_hex(8)
        session["csrf_token"] = token
        extras = f"<input type='hidden' name='csrf' value='{token}'>"
    elif PATTERN_KEY == "auth-l-security-question":
        extras = "<label>Security answer (favorite color?) <input name='security'></label><br>"
    elif PATTERN_KEY == "auth-m-totp-mfa":
        extras = "<p>MFA seed endpoint: <a href='/totp-seed'>/totp-seed</a></p><label>MFA code <input name='mfa'></label><br>"
    elif PATTERN_KEY == "auth-n-session-hijack":
        extras = "<p>After login, call <code>/issue-session-token</code> and replay via <code>/hijack/&lt;token&gt;</code>.</p>"
    elif PATTERN_KEY == "auth-o-bearer-token":
        extras = "<label>Bearer token <input name='api_token' placeholder='t0k3nId'></label><br>"
    elif PATTERN_KEY == "auth-b-http-basic":
        extras = "<p>Alternative auth: send <code>Authorization: Basic dXNlcjpwYXNz</code> to protected pages.</p>"
    elif PATTERN_KEY == "auth-e-delay-login":
        extras = "<p>This login intentionally sleeps before completing.</p>"

    return (
        "<form method='post'>"
        "<label>E-Mail <input name='email'></label><br>"
        "<label>Password <input name='password' type='password'></label><br>"
        f"{extras}"
        "<button type='submit'>Sign In</button></form>"
    )


def handle_login_submit() -> bool:
    if PATTERN_KEY == "auth-k-dynamic-fields":
        email = request.form.get(session.get("dynamic_email_field", ""), "").strip()
        password = request.form.get(session.get("dynamic_password_field", ""), "").strip()
    else:
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

    if email != VALID_EMAIL or password != VALID_PASSWORD:
        return False

    if PATTERN_KEY == "auth-c-complex-form":
        if request.form.get("tenant", "").strip() != "north" or request.form.get("region", "").strip() != "us-east":
            return False
    elif PATTERN_KEY == "auth-d-interactive-captcha":
        if request.form.get("challenge", "").strip() != "588357":
            return False
    elif PATTERN_KEY == "auth-e-delay-login":
        time.sleep(2)
    elif PATTERN_KEY == "auth-f-ocr-captcha":
        if request.form.get("captcha", "").strip() != "4319":
            return False
    elif PATTERN_KEY == "auth-j-xsrf-token":
        if request.form.get("csrf") != session.get("csrf_token"):
            return False
    elif PATTERN_KEY == "auth-l-security-question":
        if request.form.get("security", "").strip().lower() != "blue":
            return False
    elif PATTERN_KEY == "auth-m-totp-mfa":
        if request.form.get("mfa", "").strip() != "123456":
            return False
    elif PATTERN_KEY == "auth-o-bearer-token":
        if request.form.get("api_token", "").strip() != "t0k3nId":
            return False
    return True


def handle_multi_step_login() -> str | Response:
    message = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        if email == VALID_EMAIL:
            session["multi_step_email"] = email
            return redirect("/login/step2")
        message = "<p style='color:red'>Unknown e-mail</p>"
    form = "<form method='post'><label>E-Mail <input name='email'></label><button type='submit'>Next</button></form>"
    return render_page("Multi-Step Login - Step 1", message + form)


@app.route("/login/step2", methods=["GET", "POST"])
def login_step2() -> str | Response:
    if PATTERN_KEY != "auth-g-multi-step":
        return redirect("/login")

    message = ""
    if request.method == "POST":
        if session.get("multi_step_email") == VALID_EMAIL and request.form.get("password", "").strip() == VALID_PASSWORD:
            session["auth"] = True
            session.pop("multi_step_email", None)
            return redirect("/app/overview")
        message = "<p style='color:red'>Invalid password</p>"

    form = "<form method='post'><label>Password <input type='password' name='password'></label><button type='submit'>Sign In</button></form>"
    return render_page("Multi-Step Login - Step 2", message + form)


def handle_new_window_login() -> str:
    return render_page(
        "New Window Login",
        "<p>Launch auth popup:</p><p><a href='/popup-login' target='_blank'>Open Login Window</a></p>",
    )


@app.route("/popup-login", methods=["GET", "POST"])
def popup_login() -> str | Response:
    if PATTERN_KEY != "auth-h-new-window":
        return redirect("/login")

    message = ""
    if request.method == "POST" and handle_login_submit():
        session["auth"] = True
        return render_page("Popup Login Complete", "<p>Authenticated. You can close this window.</p>")
    elif request.method == "POST":
        message = "<p style='color:red'>Authentication failed</p>"

    return render_page("Popup Login", message + login_form_html())


def handle_iframe_login() -> str:
    return render_page("IFrame Login", "<iframe src='/frame-login' title='iframe-login' width='700' height='380'></iframe>")


@app.route("/frame-login", methods=["GET", "POST"])
def frame_login() -> str | Response:
    if PATTERN_KEY != "auth-i-iframe":
        return redirect("/login")

    if request.method == "POST" and handle_login_submit():
        session["auth"] = True
        return render_page("IFrame Login Complete", "<p>Authenticated from iframe.</p>")

    message = "<p style='color:red'>Authentication failed</p>" if request.method == "POST" else ""
    return render_page("IFrame Login Form", message + login_form_html())


@app.route("/totp-seed")
def totp_seed() -> Response:
    if PATTERN_KEY != "auth-m-totp-mfa":
        return jsonify({"enabled": False})
    return jsonify({"seed": "I65VU7K5ZQL7WB4E"})


@app.route("/issue-session-token")
@login_required
def issue_session_token() -> Response:
    token = secrets.token_hex(12)
    session["transfer_token"] = token
    return jsonify({"token": token, "use": f"/hijack/{token}"})


@app.route("/hijack/<token>")
def hijack(token: str) -> Response:
    if PATTERN_KEY != "auth-n-session-hijack":
        return redirect("/login")
    if token and token == session.get("transfer_token"):
        session["auth"] = True
        return redirect("/app/overview")
    return redirect("/login")


@app.route("/token-login")
def token_login() -> Response:
    if PATTERN_KEY != "auth-o-bearer-token":
        return redirect("/login")
    if request.headers.get("Authorization", "") == "Bearer t0k3nId":
        session["auth"] = True
        return redirect("/app/overview")
    return Response("Missing bearer token", status=401)


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
