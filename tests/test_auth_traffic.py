from __future__ import annotations

from app.auth_traffic import AuthTrafficCapture


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
        request = _FakeRequest(
            "https://example.com/api/me",
            headers={"authorization": "Bearer tok123"},
        )
        capture._on_request(request)
        assert capture.captured_headers == ["Authorization: Bearer tok123"]

    def test_captures_csrf_header(self):
        capture = AuthTrafficCapture("https://example.com")
        request = _FakeRequest(
            "https://example.com/api/login",
            headers={"x-csrf-token": "abc"},
        )
        capture._on_request(request)
        assert capture.captured_headers == ["X-CSRF-Token: abc"]

    def test_ignores_out_of_scope(self):
        capture = AuthTrafficCapture("https://example.com")
        request = _FakeRequest(
            "https://evil.com/steal",
            headers={"authorization": "Bearer secret"},
        )
        capture._on_request(request)
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
        request = _FakeRequest(
            "https://api.example.com/token",
            headers={"authorization": "Bearer sub"},
        )
        capture._on_request(request)
        assert capture.captured_headers == ["Authorization: Bearer sub"]
