from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime

from flask import Flask, Response, jsonify, make_response, redirect, request

app = Flask(__name__)

HARNESS_TOKEN = os.environ.get("TEST_HARNESS_TOKEN")
HEADER_TOKEN = os.environ.get("DISCOVERY_FIXTURE_HEADER_TOKEN")
LEDGERS: dict[str, list[dict[str, str]]] = defaultdict(list)

REQUIRED_ROUTES = {
    "/api/js/regex-marker.do",
    "/api/js/jsluice-marker",
    "/api/form/preview",
    "/api/runtime/xhr",
    "/api/observer/frame",
    "/api/observer/popup",
    "/api/observer/service-worker",
    "/api/observer/worker",
    "/rendered/only",
    "/handoff",
    "/header-only",
    "/subdomain-header-only",
    "/subdomain-known-file-marker",
    "/seed/one/child",
    "/seed/two/child",
}
DESTRUCTIVE_ROUTES = {"/api/destructive"}


@app.before_request
def record_request() -> None:
    if request.path.startswith("/_test/"):
        return
    run_id = request.headers.get("X-Crawler-Test-Run")
    if not run_id:
        return
    classification = "allowed-background"
    if request.path in REQUIRED_ROUTES:
        classification = "required"
    elif request.path in DESTRUCTIVE_ROUTES:
        classification = "destructive-marker"
    LEDGERS[run_id].append(
        {
            "method": request.method,
            "route": request.path.rstrip("/") or "/",
            "timestamp": datetime.now(UTC).isoformat(),
            "classification": classification,
        }
    )


def page(title: str, body: str, *, scripts: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>{title}</title></head>
  <body>
    <h1>{title}</h1>
    {body}
    {scripts}
  </body>
</html>"""


@app.get("/")
def index() -> Response:
    subdomain_header_url = f"{request.scheme}://child.{request.host}/subdomain-header-only"
    body = f"""
    <nav>
      <a href="/static-form">Static form</a>
      <a href="/handoff">Browser handoff</a>
      <a href="/header-only">Header-only page</a>
      <a href="{subdomain_header_url}">Subdomain header-only page</a>
      <a href="/seed/one">Seed one</a>
      <a href="/seed/two">Seed two</a>
      <a href="/perpetual">Perpetual traffic</a>
    </nav>
    <button id="runtime-xhr" type="button">Load runtime XHR marker</button>
    <button id="rendered-navigation" type="button">Open rendered-only page</button>
    <button id="observer-popup" type="button">Open observer popup</button>
    <button id="observer-frame" type="button">Load observer frame</button>
    <button id="observer-worker" type="button">Start observer worker</button>
    <button id="observer-service-worker" type="button">Start observer service worker</button>
    <p id="runtime-result" aria-live="polite"></p>
    <p id="observer-worker-result" aria-live="polite"></p>
    <p id="observer-service-worker-result" aria-live="polite"></p>
    <div id="observer-frame-host"></div>
    """
    scripts = """
    <script src="/assets/regex-marker.js"></script>
    <script src="/assets/jsluice-marker.js"></script>
    <script>
      localStorage.setItem("discovery-lane-state", "ready");
      document.getElementById("runtime-xhr").addEventListener("click", async () => {
        const path = ["", "api", "runtime", "xhr"].join("/");
        const response = await fetch(path);
        const data = await response.json();
        document.getElementById("runtime-result").textContent = data.marker;
      });
      document.getElementById("rendered-navigation").addEventListener("click", () => {
        const path = ["", "rendered", "only"].join("/");
        window.location.assign(path);
      });
      document.getElementById("observer-popup").addEventListener("click", () => {
        window.open("/observer/popup", "observer-popup");
      });
      document.getElementById("observer-frame").addEventListener("click", () => {
        const frame = document.createElement("iframe");
        frame.title = "Observer frame";
        frame.src = "/observer/frame";
        document.getElementById("observer-frame-host").replaceChildren(frame);
      });
      document.getElementById("observer-worker").addEventListener("click", () => {
        const worker = new Worker("/assets/observer-worker.js");
        worker.addEventListener("message", (event) => {
          if (event.data === "ready") {
            worker.postMessage("probe");
            return;
          }
          document.getElementById("observer-worker-result").textContent = event.data;
          worker.terminate();
        });
      });
      navigator.serviceWorker?.addEventListener("message", (event) => {
        document.getElementById("observer-service-worker-result").textContent = event.data;
      });
      document.getElementById("observer-service-worker").addEventListener("click", async () => {
        const registration = await navigator.serviceWorker.register(
          "/observer-service-worker.js"
        );
        await navigator.serviceWorker.ready;
        registration.active.postMessage("probe");
      });
    </script>
    """
    response = make_response(page("Discovery Lane Matrix", body, scripts=scripts))
    response.set_cookie("discovery_lane_session", "active", httponly=True, samesite="Lax")
    return response


@app.get("/assets/regex-marker.js")
def regex_marker_script() -> Response:
    return Response(
        "window.regexLaneMarker = '/api/js/regex-marker.do';\n",
        content_type="text/javascript",
    )


@app.get("/assets/jsluice-marker.js")
def jsluice_marker_script() -> Response:
    return Response(
        "window.jsluiceLaneMarker = function () {\n"
        '  document.location = "/api/js/jsluice-marker";\n'
        "};\n",
        content_type="text/javascript",
    )


@app.get("/api/js/regex-marker.do")
def regex_marker() -> dict[str, str]:
    return {"marker": "js-regex"}


@app.get("/api/js/jsluice-marker")
def jsluice_marker() -> dict[str, str]:
    return {"marker": "jsluice"}


@app.route("/static-form", methods=["GET"])
def static_form() -> str:
    return page(
        "Static Form Extraction",
        """
        <form method="post" action="/api/form/preview">
          <label>Query <input name="query" value="fixture"></label>
          <button type="submit">Preview</button>
        </form>
        <form method="post" action="/api/destructive">
          <button type="submit">Destructive marker</button>
        </form>
        """,
    )


@app.post("/api/form/preview")
def form_preview() -> dict[str, str]:
    return {"marker": "form-preview", "query": request.form.get("query", "")}


@app.post("/api/destructive")
def destructive_marker() -> dict[str, object]:
    return {"changed": False, "marker": "destructive-observed"}


@app.get("/api/runtime/xhr")
def runtime_xhr() -> dict[str, str]:
    return {"marker": "runtime-xhr"}


@app.get("/observer/popup")
def observer_popup() -> str:
    return page(
        "Observer Popup",
        '<p id="observer-popup-result" aria-live="polite"></p>',
        scripts="""
        <script>
          fetch("/api/observer/popup")
            .then((response) => response.json())
            .then((data) => {
              document.getElementById("observer-popup-result").textContent = data.marker;
            });
        </script>
        """,
    )


@app.get("/observer/frame")
def observer_frame() -> str:
    return page(
        "Observer Frame",
        '<p id="observer-frame-result" aria-live="polite"></p>',
        scripts="""
        <script>
          fetch("/api/observer/frame")
            .then((response) => response.json())
            .then((data) => {
              document.getElementById("observer-frame-result").textContent = data.marker;
            });
        </script>
        """,
    )


@app.get("/assets/observer-worker.js")
def observer_worker_script() -> Response:
    return Response(
        'postMessage("ready");\n'
        'self.addEventListener("message", () => {\n'
        '  fetch("/api/observer/worker")\n'
        "    .then((response) => response.json())\n"
        "    .then((data) => postMessage(data.marker));\n"
        "});\n",
        content_type="text/javascript",
    )


@app.get("/observer-service-worker.js")
def observer_service_worker_script() -> Response:
    return Response(
        'self.addEventListener("install", () => self.skipWaiting());\n'
        'self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));\n'
        'self.addEventListener("message", (event) => {\n'
        '  event.waitUntil(fetch("/api/observer/service-worker")\n'
        "    .then((response) => response.json())\n"
        "    .then((data) => event.source.postMessage(data.marker)));\n"
        "});\n",
        content_type="text/javascript",
    )


@app.get("/api/observer/<surface>")
def observer_api(surface: str) -> tuple[dict[str, str], int] | dict[str, str]:
    if surface not in {"popup", "frame", "worker", "service-worker"}:
        return {"error": "not-found"}, 404
    return {"marker": f"observer-{surface}"}


@app.get("/rendered/only")
def rendered_only() -> str:
    return page("Rendered-only Navigation", "<p>Browser action marker reached.</p>")


@app.get("/handoff")
def handoff() -> str:
    cookie_state = (
        "present"
        if request.cookies.get("discovery_lane_session") == "active"
        else "missing"
    )
    return page(
        "Browser Handoff",
        f'<p id="cookie-state">cookie:{cookie_state}</p><p id="storage-state"></p>',
        scripts="""
        <script>
          document.getElementById("storage-state").textContent =
            `localStorage:${localStorage.getItem("discovery-lane-state") || "missing"}`;
        </script>
        """,
    )


@app.get("/header-only")
def header_only() -> tuple[str, int] | str:
    if not HEADER_TOKEN or request.headers.get("X-Discovery-Token") != HEADER_TOKEN:
        return "Missing discovery fixture header", 401
    return page("Header-only Marker", "<p>Scoped header accepted.</p>")


@app.get("/subdomain-header-only")
def subdomain_header_only() -> tuple[str, int] | str:
    if not HEADER_TOKEN or request.headers.get("X-Discovery-Token") != HEADER_TOKEN:
        return "Missing discovery fixture header", 401
    return page("Subdomain Header Marker", "<p>Scoped subdomain header accepted.</p>")


@app.get("/seed/<seed>")
def seed(seed: str) -> tuple[str, int] | str:
    if seed not in {"one", "two"}:
        return "Not Found", 404
    return page(f"Seed {seed}", f'<a href="/seed/{seed}/child">Seed {seed} child</a>')


@app.get("/seed/<seed>/child")
def seed_child(seed: str) -> tuple[str, int] | str:
    if seed not in {"one", "two"}:
        return "Not Found", 404
    return page(f"Seed {seed} Child", f"<p>Serial seed marker {seed}.</p>")


@app.get("/perpetual")
def perpetual() -> str:
    return page(
        "Perpetual Browser Traffic",
        '<p id="poll-state">Polling is active.</p>',
        scripts="""
        <script>
          setInterval(() => fetch(["", "api", "perpetual", "poll"].join("/")), 500);
        </script>
        """,
    )


@app.get("/api/perpetual/poll")
def perpetual_poll() -> dict[str, str]:
    return {"state": "unchanged"}


@app.get("/robots.txt")
def robots() -> tuple[str, int] | Response:
    if request.host.startswith("child."):
        if not HEADER_TOKEN or request.headers.get("X-Discovery-Token") != HEADER_TOKEN:
            return "Missing discovery fixture header", 401
        return Response(
            f"User-agent: *\nSitemap: {request.scheme}://{request.host}/subdomain-sitemap.xml\n",
            content_type="text/plain",
        )
    return Response(
        "User-agent: *\n"
        "Disallow: /robots-marker\n"
        "Sitemap: /sitemap.xml\n"
        "Sitemap: /sitemaps/redirect.xml\n",
        content_type="text/plain",
    )


@app.get("/subdomain-sitemap.xml")
def subdomain_sitemap() -> tuple[str, int] | Response:
    if not HEADER_TOKEN or request.headers.get("X-Discovery-Token") != HEADER_TOKEN:
        return "Missing discovery fixture header", 401
    return Response(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{request.scheme}://{request.host}/subdomain-known-file-marker</loc></url>"
        "</urlset>",
        content_type="application/xml",
    )


@app.get("/subdomain-known-file-marker")
def subdomain_known_file_marker() -> tuple[str, int] | str:
    if not HEADER_TOKEN or request.headers.get("X-Discovery-Token") != HEADER_TOKEN:
        return "Missing discovery fixture header", 401
    return page("Subdomain Known-file Marker", "<p>Scoped subdomain sitemap reached.</p>")


@app.get("/robots-marker")
def robots_marker() -> str:
    return page("Robots Marker", "<p>robots.txt entry reached.</p>")


@app.get("/sitemap.xml")
def sitemap_index() -> Response:
    return Response(
        """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>http://localhost:8007/sitemaps/pages.xml</loc></sitemap>
  <sitemap><loc>http://localhost:8007/sitemaps/cycle-a.xml</loc></sitemap>
  <sitemap><loc>http://localhost:8007/sitemaps/malformed.xml</loc></sitemap>
  <sitemap><loc>http://localhost:8007/sitemaps/oversized.xml</loc></sitemap>
</sitemapindex>""",
        content_type="application/xml",
    )


@app.get("/sitemaps/pages.xml")
def sitemap_pages() -> Response:
    return Response(
        """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://localhost:8007/known-file-marker</loc></url>
</urlset>""",
        content_type="application/xml",
    )


@app.get("/known-file-marker")
def known_file_marker() -> str:
    return page("Known-file Marker", "<p>Nested sitemap entry reached.</p>")


@app.get("/sitemaps/redirect.xml")
def sitemap_redirect() -> Response:
    return redirect("/sitemaps/pages.xml", code=302)


@app.get("/sitemaps/cycle-a.xml")
def sitemap_cycle_a() -> Response:
    return Response(
        """<sitemapindex><sitemap><loc>http://localhost:8007/sitemaps/cycle-b.xml</loc></sitemap></sitemapindex>""",
        content_type="application/xml",
    )


@app.get("/sitemaps/cycle-b.xml")
def sitemap_cycle_b() -> Response:
    return Response(
        """<sitemapindex><sitemap><loc>http://localhost:8007/sitemaps/cycle-a.xml</loc></sitemap></sitemapindex>""",
        content_type="application/xml",
    )


@app.get("/sitemaps/malformed.xml")
def sitemap_malformed() -> Response:
    return Response("<urlset><url><loc>broken", content_type="application/xml")


@app.get("/sitemaps/oversized.xml")
def sitemap_oversized() -> Response:
    prefix = "<urlset><url><loc>http://localhost:8007/oversized-marker</loc></url>"
    body = prefix + (" " * (5 * 1024 * 1024)) + "</urlset>"
    return Response(body, content_type="application/xml")


@app.get("/_test/ledger/<run_id>")
def test_ledger(run_id: str) -> tuple[str, int] | Response:
    if not HARNESS_TOKEN or request.headers.get("X-Test-Harness-Token") != HARNESS_TOKEN:
        return "Not Found", 404
    return jsonify({"run_id": run_id, "entries": LEDGERS.get(run_id, [])})


@app.post("/_test/reset")
def test_reset() -> tuple[str, int]:
    if not HARNESS_TOKEN or request.headers.get("X-Test-Harness-Token") != HARNESS_TOKEN:
        return "Not Found", 404
    LEDGERS.clear()
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
