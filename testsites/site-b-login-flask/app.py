from __future__ import annotations

import os
from functools import wraps

from flask import Flask, redirect, render_template, request, session, url_for
from markupsafe import escape

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")

VALID_USER = {"username": "demo", "password": "password"}


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
