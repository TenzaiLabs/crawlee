from __future__ import annotations

import os
import secrets
import time
from functools import wraps

from flask import Flask, Response, redirect, render_template_string, request, session, url_for
from markupsafe import escape

PATTERN_KEY = "auth-b-http-basic"
PATTERN_TITLE = "Basic HTTP Authentication"

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
                <a href="/login">Login</a>
                {{ logout_button|safe }}
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
        logout_button=logout_button_html(),
    )


def is_authenticated() -> bool:
    auth = request.authorization
    return bool(auth and auth.username == "user" and auth.password == "pass")


def basic_auth_challenge() -> Response:
    response = Response("Unauthorized", status=401)
    response.headers["WWW-Authenticate"] = 'Basic realm="Crawler Testsite"'
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return basic_auth_challenge()
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
    if is_authenticated():
        return redirect(request.args.get("next") or "/app/overview")
    return basic_auth_challenge()


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


@app.route("/logout")
def logout() -> Response:
    session.clear()
    return redirect("/")


def logout_button_html() -> str:
    if not is_authenticated():
        return ""
    return """
        <form method="get" action="/logout" style="display:inline; margin-left: 0.5rem;">
            <button type="submit">Logout</button>
        </form>
        <a href="/logout" style="margin-left: 0.5rem;">Legacy logout link</a>
    """


def action_forms(page_slug: str, page_label: str) -> str:
    entry_id = f"{page_slug}-entry-001"
    return f"""
        <section aria-label="Workspace actions">
            <h2>Workspace actions</h2>
            <p>Use these forms to simulate normal create, modify, and delete workflows for {page_label}.</p>
            <form method="post" action="/app/actions/create">
                <input type="hidden" name="source" value="{page_slug}">
                <label>Title <input name="title" value="New {page_label} entry"></label><br>
                <label>Owner <input name="owner" value="ops@example.test"></label><br>
                <button type="submit">Create entry</button>
            </form>
            <form method="post" action="/app/actions/update">
                <input type="hidden" name="source" value="{page_slug}">
                <label>Entry ID <input name="entry_id" value="{entry_id}"></label><br>
                <label>Status <select name="status"><option>Active</option><option>Paused</option><option>Needs review</option></select></label><br>
                <label>Note <input name="note" value="Updated by test operator"></label><br>
                <button type="submit">Update entry</button>
            </form>
            <form method="post" action="/app/actions/delete">
                <input type="hidden" name="source" value="{page_slug}">
                <label>Entry ID <input name="entry_id" value="{entry_id}"></label><br>
                <button type="submit">Delete entry</button>
            </form>
        </section>
    """


def action_result(action: str, details: str) -> str:
    body = f"""
        <p>Mock {escape(action)} request accepted.</p>
        <p>{details}</p>
        <p>No persistent data was changed by this test fixture.</p>
        <p><a href="/app/overview">Return to overview</a></p>
    """
    return render_page("Action Recorded", body)


@app.post("/app/actions/create")
@login_required
def create_entry() -> str:
    title = escape(request.form.get("title", "Untitled entry").strip() or "Untitled entry")
    owner = escape(request.form.get("owner", "unassigned").strip() or "unassigned")
    source = escape(request.form.get("source", "workspace").strip() or "workspace")
    return action_result("create", f"Created {title} for {owner} from {source}.")


@app.post("/app/actions/update")
@login_required
def update_entry() -> str:
    entry_id = escape(request.form.get("entry_id", "entry-001").strip() or "entry-001")
    status = escape(request.form.get("status", "Active").strip() or "Active")
    note = escape(request.form.get("note", "No note").strip() or "No note")
    return action_result("update", f"Updated {entry_id} to {status}. Note: {note}.")


@app.post("/app/actions/delete")
@login_required
def delete_entry() -> str:
    entry_id = escape(request.form.get("entry_id", "entry-001").strip() or "entry-001")
    source = escape(request.form.get("source", "workspace").strip() or "workspace")
    return action_result("delete", f"Marked {entry_id} from {source} for deletion review.")


for slug, label in APP_PAGES:
    endpoint = f"app_{slug}"

    def make_view(page_slug: str, page_label: str):
        @app.route(f"/app/{page_slug}", endpoint=f"app_{page_slug}")
        @login_required
        def view_page() -> str:
            body = (
                f"<p>{page_label} content for {PATTERN_TITLE}.</p>"
                + action_forms(page_slug, page_label)
            )
            return render_page(page_label, body)

        return view_page

    make_view(slug, label)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
