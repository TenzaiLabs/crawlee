from __future__ import annotations

import pytest

from app.auth_traffic import (
    AuthTrafficCapture,
    _parse_set_cookie,
    proxy_bypass_route_handler,
)

# ---------------------------------------------------------------------------
# _parse_set_cookie
# ---------------------------------------------------------------------------


class TestParseSetCookie:
    def test_simple(self):
        cookie = _parse_set_cookie("session=abc123", "https://example.com/login")
        assert cookie is not None
        assert cookie["name"] == "session"
        assert cookie["value"] == "abc123"
        assert cookie["domain"] == "example.com"
        assert cookie["path"] == "/"

    def test_full_attributes(self):
        header = "id=xyz; Domain=.example.com; Path=/app; Secure; HttpOnly; SameSite=Lax"
        cookie = _parse_set_cookie(header, "https://example.com/login")
        assert cookie is not None
        assert cookie["name"] == "id"
        assert cookie["value"] == "xyz"
        assert cookie["domain"] == "example.com"
        assert cookie["path"] == "/app"
        assert cookie["secure"] is True
        assert cookie["httpOnly"] is True
        assert cookie["sameSite"] == "Lax"

    def test_max_age(self):
        cookie = _parse_set_cookie("tok=v; Max-Age=3600", "https://example.com/")
        assert cookie is not None
        assert "expires" in cookie
        assert cookie["expires"] > 0

    def test_empty_returns_none(self):
        assert _parse_set_cookie("", "https://example.com") is None

    def test_no_equals_returns_none(self):
        assert _parse_set_cookie("malformed", "https://example.com") is None

    def test_value_with_equals(self):
        cookie = _parse_set_cookie("token=abc=def==", "https://example.com/")
        assert cookie is not None
        assert cookie["name"] == "token"
        assert cookie["value"] == "abc=def=="


# ---------------------------------------------------------------------------
# AuthTrafficCapture
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, url: str, method: str = "GET", headers: dict | None = None):
        self.url = url
        self.method = method
        self.headers = headers or {}


class _FakeResponse:
    def __init__(self, url: str, status: int, headers: dict | None = None):
        self.url = url
        self.status = status
        self.headers = headers or {}


class TestAuthTrafficCapture:
    def test_captures_authorization_header(self):
        capture = AuthTrafficCapture("https://example.com")
        req = _FakeRequest(
            "https://example.com/api/me",
            headers={"authorization": "Bearer tok123"},
        )
        capture._on_request(req)
        assert capture.captured_headers == ["Authorization: Bearer tok123"]

    def test_captures_csrf_header(self):
        capture = AuthTrafficCapture("https://example.com")
        req = _FakeRequest(
            "https://example.com/api/login",
            headers={"x-csrf-token": "abc"},
        )
        capture._on_request(req)
        assert capture.captured_headers == ["X-CSRF-Token: abc"]

    def test_ignores_out_of_scope(self):
        capture = AuthTrafficCapture("https://example.com")
        req = _FakeRequest(
            "https://evil.com/steal",
            headers={"authorization": "Bearer secret"},
        )
        capture._on_request(req)
        assert capture.captured_headers == []

    def test_deduplicates_headers(self):
        capture = AuthTrafficCapture("https://example.com")
        for _ in range(3):
            capture._on_request(
                _FakeRequest(
                    "https://example.com/api",
                    headers={"authorization": "Bearer same"},
                )
            )
        assert len(capture.captured_headers) == 1

    def test_tracks_redirects(self):
        capture = AuthTrafficCapture("https://example.com")
        capture._on_response(
            _FakeResponse(
                "https://example.com/login",
                302,
                headers={"location": "https://idp.example.com/saml"},
            )
        )
        assert len(capture.redirect_chain) == 1
        assert capture.redirect_chain[0]["status"] == 302
        assert capture.redirect_chain[0]["to"] == "https://idp.example.com/saml"

    def test_records_request_log(self):
        capture = AuthTrafficCapture("https://example.com")
        capture._on_request(_FakeRequest("https://example.com/login", "POST"))
        capture._on_request(_FakeRequest("https://example.com/dashboard", "GET"))
        assert len(capture.request_log) == 2
        assert capture.request_log[0]["method"] == "POST"

    def test_allows_subdomain_in_scope(self):
        capture = AuthTrafficCapture("https://example.com")
        req = _FakeRequest(
            "https://api.example.com/token",
            headers={"authorization": "Bearer sub"},
        )
        capture._on_request(req)
        assert capture.captured_headers == ["Authorization: Bearer sub"]


# ---------------------------------------------------------------------------
# proxy_bypass_route_handler
# ---------------------------------------------------------------------------


class _FakeRoute:
    def __init__(self, request: _FakeRouteRequest):
        self.request = request
        self.continued = False
        self.fulfilled = False
        self.fetch_called = False
        self._fetch_response = None

    async def continue_(self):
        self.continued = True

    async def fetch(self, **kwargs):
        self.fetch_called = True
        return self._fetch_response

    async def fulfill(self, *, response=None):
        self.fulfilled = True


class _FakeRouteRequest:
    def __init__(self, method: str, url: str = "https://example.com/login"):
        self.method = method
        self.url = url

    async def all_headers(self):
        return {"cookie": "session=abc", "content-type": "application/x-www-form-urlencoded"}


class _FakeContext:
    def __init__(self):
        self.added_cookies: list[list[dict]] = []

    async def add_cookies(self, cookies):
        self.added_cookies.append(cookies)


class _FakeHeaderEntry:
    def __init__(self, name: str, value: str):
        self.name = name
        self.value = value

    def __getitem__(self, key):
        return {"name": self.name, "value": self.value}[key]


class _FakeFetchResponse:
    def __init__(self, set_cookies: list[str] | None = None):
        self._set_cookies = set_cookies or []

    async def headers_array(self):
        result = []
        for sc in self._set_cookies:
            result.append({"name": "set-cookie", "value": sc})
        return result


@pytest.mark.asyncio
async def test_route_handler_continues_get():
    req = _FakeRouteRequest("GET")
    route = _FakeRoute(req)
    await proxy_bypass_route_handler(route, _FakeContext())
    assert route.continued is True
    assert route.fetch_called is False


@pytest.mark.asyncio
async def test_route_handler_bypasses_post():
    req = _FakeRouteRequest("POST")
    route = _FakeRoute(req)
    route._fetch_response = _FakeFetchResponse()
    ctx = _FakeContext()
    await proxy_bypass_route_handler(route, ctx)
    assert route.fetch_called is True
    assert route.fulfilled is True
    assert route.continued is False


@pytest.mark.asyncio
async def test_route_handler_syncs_cookies():
    req = _FakeRouteRequest("POST")
    route = _FakeRoute(req)
    route._fetch_response = _FakeFetchResponse(set_cookies=["session=new123; Path=/; HttpOnly"])
    ctx = _FakeContext()
    await proxy_bypass_route_handler(route, ctx)
    assert len(ctx.added_cookies) == 1
    cookie = ctx.added_cookies[0][0]
    assert cookie["name"] == "session"
    assert cookie["value"] == "new123"


@pytest.mark.asyncio
async def test_route_handler_falls_back_on_error():
    req = _FakeRouteRequest("POST")
    route = _FakeRoute(req)
    route._fetch_response = None  # fetch() returns None → headers_array() will fail
    ctx = _FakeContext()
    await proxy_bypass_route_handler(route, ctx)
    # Should fall back to continue_()
    assert route.continued is True
