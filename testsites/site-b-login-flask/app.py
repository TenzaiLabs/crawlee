from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime
from functools import wraps

from flask import Flask, redirect, render_template, request, session, url_for
from markupsafe import escape

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")

VALID_USER = {"username": "demo", "password": "password"}
HARNESS_TOKEN = os.environ.get("TEST_HARNESS_TOKEN")
LEDGERS: dict[str, list[dict[str, str]]] = defaultdict(list)
REQUIRED_ROUTES = {
    "/api/onboarding/validate",
    "/api/onboarding/preview",
    "/api/settings/validate",
}
FORBIDDEN_ROUTES = {
    "/logout",
    "/actions/delete",
    "/api/invitations/send",
    "/api/security/password",
    "/api/account/close",
}


@app.before_request
def record_test_request():
    if request.path.startswith("/_test/"):
        return None
    run_id = request.headers.get("X-Crawler-Test-Run")
    if not run_id:
        return None
    if request.path in FORBIDDEN_ROUTES:
        classification = "forbidden"
    elif request.path in REQUIRED_ROUTES:
        classification = "required"
    else:
        classification = "allowed-background"
    LEDGERS[run_id].append(
        {
            "method": request.method,
            "route": request.path.rstrip("/") or "/",
            "timestamp": datetime.now(UTC).isoformat(),
            "classification": classification,
        }
    )
    return None


def harness_authorized() -> bool:
    return bool(HARNESS_TOKEN) and request.headers.get("X-Test-Harness-Token") == HARNESS_TOKEN


@app.get("/_test/ledger/<run_id>")
def test_ledger(run_id: str):
    if not harness_authorized():
        return "Not Found", 404
    return {"run_id": run_id, "entries": LEDGERS.get(run_id, [])}


@app.post("/_test/reset")
def test_reset():
    if not harness_authorized():
        return "Not Found", 404
    LEDGERS.clear()
    return "", 204


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/public")
def public():
    return render_template("public.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == VALID_USER["username"] and password == VALID_USER["password"]:
            session["user"] = username
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html", error=None)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


def action_details(action: str) -> dict[str, str]:
    entry_id = escape(request.form.get("entry_id", "account-101").strip() or "account-101")
    title = escape(request.form.get("title", "Harbor workspace entry").strip() or "Harbor workspace entry")
    status = escape(request.form.get("status", "Active").strip() or "Active")
    owner = escape(request.form.get("owner", "ops@example.test").strip() or "ops@example.test")
    if action == "created":
        summary = f"Created {title} for {owner}."
    elif action == "updated":
        summary = f"Updated {entry_id} to {status}."
    else:
        summary = f"Marked {entry_id} for deletion review."
    return {"action": action, "summary": summary}


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html")


@app.route("/reports")
@login_required
def reports():
    return render_template("reports.html")


@app.route("/actions")
@login_required
def actions():
    return render_template("actions.html")


@app.get("/workflow-center")
@login_required
def workflow_center():
    return render_template("workflow_center.html")


@app.post("/api/onboarding/validate")
@login_required
def validate_onboarding():
    return {"valid": True, "next": "preview"}


@app.post("/api/onboarding/preview")
@login_required
def preview_onboarding():
    return {"draft_id": "harbor-onboarding-draft", "state": "preview"}


@app.post("/api/settings/validate")
@login_required
def validate_settings():
    return {"valid": True, "state": "not-saved"}


@app.post("/api/invitations/send")
@login_required
def send_invitation():
    return {"sent": False, "fixture": True}


@app.post("/api/security/password")
@login_required
def change_password():
    return {"changed": False, "fixture": True}


@app.post("/api/account/close")
@login_required
def close_account():
    return {"closed": False, "fixture": True}


@app.post("/actions/create")
@login_required
def create_action():
    return render_template("action_result.html", **action_details("created"))


@app.post("/actions/update")
@login_required
def update_action():
    return render_template("action_result.html", **action_details("updated"))


@app.post("/actions/delete")
@login_required
def delete_action():
    return render_template("action_result.html", **action_details("deleted"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
