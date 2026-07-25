"""In-browser traffic capture for authentication flows.

Hooks into Playwright page events to capture auth-relevant headers and
SAML/SSO traffic directly from the authentication browser.
"""

from __future__ import annotations

import logging
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
