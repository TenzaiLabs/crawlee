"""In-browser traffic capture for authentication flows.

Hooks into Playwright page events to capture Authorization headers,
cookies, and SAML/SSO traffic without depending on the proxy.  Also
provides a route handler that bypasses the proxy for requests that
carry a body (POST/PUT/PATCH) to work around the Proxify body-drop bug
while still routing GETs through the proxy for logging.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from .common import is_host_in_scope, redact_header_value

logger = logging.getLogger(__name__)

# Request header names (lowercase) that carry auth-relevant information.
_AUTH_REQUEST_HEADERS = (
    "authorization",
    "x-csrf-token",
    "x-xsrf-token",
)

# Canonical casing for the headers we emit.
_HEADER_CANONICAL: dict[str, str] = {
    "authorization": "Authorization",
    "x-csrf-token": "X-CSRF-Token",
    "x-xsrf-token": "X-XSRF-Token",
}

# URL path substrings that signal SAML / SSO / OAuth traffic.
_SSO_URL_MARKERS = ("saml", "sso", "adfs", "oauth", "openid", "auth/realms", "cas/login")

# Methods whose body Proxify corrupts during forwarding.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


# ---------------------------------------------------------------------------
# Set-Cookie parser (minimal, for syncing cookies back into the context)
# ---------------------------------------------------------------------------


def _parse_set_cookie(header_value: str, request_url: str) -> dict | None:
    """Parse a ``Set-Cookie`` header into a Playwright-compatible cookie dict.

    Returns *None* when the header cannot be meaningfully parsed.
    """
    if not header_value:
        return None

    parts = [p.strip() for p in header_value.split(";")]
    if not parts:
        return None

    # First part is name=value.
    eq_idx = parts[0].find("=")
    if eq_idx < 1:
        return None

    name = parts[0][:eq_idx]
    value = parts[0][eq_idx + 1 :]

    parsed_url = urlparse(request_url)
    cookie: dict = {
        "name": name,
        "value": value,
        "domain": parsed_url.hostname or "",
        "path": "/",
    }

    for part in parts[1:]:
        if "=" in part:
            attr_name, _, attr_value = part.partition("=")
            attr_name = attr_name.strip().lower()
            attr_value = attr_value.strip()
            if attr_name == "domain":
                cookie["domain"] = attr_value.lstrip(".")
            elif attr_name == "path":
                cookie["path"] = attr_value
            elif attr_name == "samesite":
                canonical = attr_value.capitalize()
                if canonical in ("Strict", "Lax", "None"):
                    cookie["sameSite"] = canonical
            elif attr_name == "max-age":
                try:
                    cookie["expires"] = int(time.time()) + int(attr_value)
                except ValueError:
                    pass
        else:
            lower = part.lower()
            if lower == "secure":
                cookie["secure"] = True
            elif lower == "httponly":
                cookie["httpOnly"] = True

    return cookie


# ---------------------------------------------------------------------------
# Route handler: bypass proxy for body-carrying requests
# ---------------------------------------------------------------------------


async def proxy_bypass_route_handler(route, context) -> None:  # noqa: ANN001
    """Playwright route handler that sends POST/PUT/PATCH directly.

    GET/HEAD/OPTIONS requests are forwarded via ``route.continue_()`` so they
    still flow through the configured proxy (Proxify) for traffic logging.

    For body-carrying methods the request is replayed with ``route.fetch()``
    (which bypasses the browser proxy) and the response — including any
    ``Set-Cookie`` headers — is synced back into *context*.
    """
    request = route.request
    if request.method not in _BODY_METHODS:
        await route.continue_()
        return

    try:
        # Replay the request directly (not through the proxy).
        # all_headers() includes Cookie and other browser-added headers.
        all_headers = await request.all_headers()
        response = await route.fetch(
            headers=all_headers,
            max_redirects=0,  # let the browser follow redirects normally
        )

        # Sync Set-Cookie headers from the direct response into the browser
        # context so subsequent requests carry the new cookies.
        for header in await response.headers_array():
            if header["name"].lower() == "set-cookie":
                cookie = _parse_set_cookie(header["value"], request.url)
                if cookie:
                    try:
                        await context.add_cookies([cookie])
                    except Exception:
                        logger.debug(
                            "Failed to sync Set-Cookie back to context: %s",
                            header["value"][:80],
                        )

        await route.fulfill(response=response)
    except Exception as exc:
        # Fallback: let it go through the proxy (body may be corrupted but
        # at least the request isn't lost).
        logger.debug("POST proxy-bypass failed, falling back to proxy: %s", exc)
        await route.continue_()


# ---------------------------------------------------------------------------
# In-browser traffic capture
# ---------------------------------------------------------------------------


class AuthTrafficCapture:
    """Captures auth-relevant traffic from Playwright page network events.

    Attach an instance to a page *before* navigating to the login URL.  It
    records:

    * ``Authorization``, ``X-CSRF-Token``, etc. headers on outbound requests
      to in-scope hosts.
    * SAML / SSO / OAuth redirect chains for debugging.
    * All request/response pairs during the auth flow (method, url, status).
    """

    def __init__(self, target_url: str) -> None:
        self._target_url = target_url
        self._auth_headers: list[str] = []
        self._seen_headers: set[str] = set()
        self._redirect_chain: list[dict] = []
        self._request_log: list[dict] = []

    # -- public API ----------------------------------------------------------

    def attach(self, page) -> None:  # noqa: ANN001
        """Register request / response listeners on *page*."""
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        logger.debug("AuthTrafficCapture attached to page")

    @property
    def captured_headers(self) -> list[str]:
        """De-duplicated auth headers captured from browser traffic."""
        return list(self._auth_headers)

    @property
    def redirect_chain(self) -> list[dict]:
        """Redirect hops observed during the auth flow (for debugging)."""
        return list(self._redirect_chain)

    @property
    def request_log(self) -> list[dict]:
        """Chronological list of all request/response pairs."""
        return list(self._request_log)

    # -- internals -----------------------------------------------------------

    def _is_in_scope(self, url: str) -> bool:
        host = urlparse(url).hostname
        return is_host_in_scope(host, self._target_url)

    def _on_request(self, request) -> None:  # noqa: ANN001
        url = request.url
        method = request.method
        headers = request.headers  # lowercase keys, sync property

        self._request_log.append({"method": method, "url": url})

        # Capture auth-relevant headers on in-scope requests.
        if self._is_in_scope(url):
            for hdr_name in _AUTH_REQUEST_HEADERS:
                value = headers.get(hdr_name)
                if not value:
                    continue
                canonical = _HEADER_CANONICAL.get(hdr_name, hdr_name)
                formatted = f"{canonical}: {value}"
                if formatted not in self._seen_headers:
                    self._seen_headers.add(formatted)
                    self._auth_headers.append(formatted)
                    logger.debug(
                        "Captured auth header from browser: %s -> %s",
                        url,
                        redact_header_value(formatted),
                    )

        # Log SAML / SSO traffic regardless of scope (IdPs are cross-origin).
        if any(m in url.lower() for m in _SSO_URL_MARKERS):
            logger.debug("SSO/SAML request: %s %s", method, url)

    def _on_response(self, response) -> None:  # noqa: ANN001
        url = response.url
        status = response.status

        # Track redirect hops.
        if status in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            self._redirect_chain.append({"from": url, "to": location, "status": status})
            logger.debug("Auth redirect: %d %s -> %s", status, url, location)

        # Log SSO/SAML responses.
        if any(m in url.lower() for m in _SSO_URL_MARKERS):
            logger.debug("SSO/SAML response: %d %s", status, url)
