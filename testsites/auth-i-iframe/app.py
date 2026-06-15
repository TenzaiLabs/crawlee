from __future__ import annotations

import os
import secrets
import time
from functools import wraps

from flask import Flask, Response, redirect, render_template_string, request, session, url_for

PATTERN_KEY = "auth-i-iframe"
PATTERN_TITLE = "IFrame Authentication Challenge"

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
    return handle_iframe_login()


def login_form_html() -> str:
    extras = ""
    return (
        "<form method='post'>"
        "<label>E-Mail <input name='email'></label><br>"
        "<label>Password <input name='password' type='password'></label><br>"
        f"{extras}"
        "<button type='submit'>Sign In</button></form>"
    )


def handle_login_submit() -> bool:
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()

    if email != VALID_EMAIL or password != VALID_PASSWORD:
        return False
    return True


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
