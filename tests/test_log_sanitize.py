from __future__ import annotations

import json
from pathlib import Path

from app.log_records import (
    _BODY_PLACEHOLDER,
    _SECRET_PLACEHOLDER,
    sanitize_log_file,
    sanitize_record,
)


def _make_record(
    resp_content_type: str | None = None,
    resp_body: str = "some body",
    resp_raw: str | None = None,
    req_body: str | None = None,
) -> dict:
    resp: dict = {}
    if resp_content_type:
        resp["header"] = {"content-type": resp_content_type}
    if resp_raw is None:
        ct_line = f"Content-Type: {resp_content_type}\r\n" if resp_content_type else ""
        resp["raw"] = f"HTTP/1.1 200 OK\r\n{ct_line}\r\n\r\n{resp_body}"
    else:
        resp["raw"] = resp_raw
    resp["body"] = resp_body

    req: dict = {"method": "GET", "url": "http://example.com"}
    if req_body is not None:
        req["body"] = req_body
    return {"request": req, "response": resp}


def test_textual_content_types_preserved():
    for ct in ("text/html", "text/css", "application/json", "application/xml"):
        rec = _make_record(resp_content_type=ct)
        result = sanitize_record(rec)
        assert result["response"]["body"] == "some body"


def test_non_textual_body_stripped():
    for ct in ("image/jpeg", "image/png", "application/octet-stream", "font/woff2"):
        rec = _make_record(resp_content_type=ct)
        result = sanitize_record(rec)
        assert result["response"]["body"] == _BODY_PLACEHOLDER
        assert _BODY_PLACEHOLDER in result["response"]["raw"]
        assert "some body" not in result["response"]["raw"]


def test_content_type_with_charset():
    rec = _make_record(resp_content_type="application/json; charset=utf-8")
    result = sanitize_record(rec)
    assert result["response"]["body"] == "some body"


def test_unknown_content_type_preserved():
    rec = _make_record(resp_content_type=None)
    result = sanitize_record(rec)
    assert result["response"]["body"] == "some body"


def test_request_body_stripped_for_non_textual():
    rec = {
        "request": {
            "method": "POST",
            "url": "http://example.com",
            "header": {"content-type": "application/octet-stream"},
            "body": "binary data here",
        },
        "response": {"raw": "HTTP/1.1 200 OK\r\n\r\nok"},
    }
    result = sanitize_record(rec)
    assert result["request"]["body"] == _BODY_PLACEHOLDER


def test_sensitive_headers_redacted_from_header_maps_and_raw_blocks():
    rec = {
        "request": {
            "method": "GET",
            "url": "http://example.com",
            "header": {
                "Cookie": "session=abc",
                "Authorization": "Bearer secret",
                "Accept": "text/html",
            },
            "raw": (
                "GET / HTTP/1.1\r\n"
                "Host: example.com\r\n"
                "Cookie: session=abc\r\n"
                "Authorization: Bearer secret\r\n"
                "\r\n"
            ),
        },
        "response": {
            "header": {"Set-Cookie": "next=secret", "Content-Type": "text/html"},
            "body": "ok",
            "raw": "HTTP/1.1 200 OK\r\nSet-Cookie: next=secret\r\n\r\nok",
        },
    }

    result = sanitize_record(rec)

    assert result["request"]["header"]["Cookie"] == _SECRET_PLACEHOLDER
    assert result["request"]["header"]["Authorization"] == _SECRET_PLACEHOLDER
    assert result["request"]["header"]["Accept"] == "text/html"
    assert "Cookie: [redacted]" in result["request"]["raw"]
    assert "Authorization: [redacted]" in result["request"]["raw"]
    assert "session=abc" not in result["request"]["raw"]
    assert result["response"]["header"]["Set-Cookie"] == _SECRET_PLACEHOLDER
    assert "Set-Cookie: [redacted]" in result["response"]["raw"]
    assert "next=secret" not in result["response"]["raw"]


def test_sanitize_log_file(tmp_path: Path):
    log = tmp_path / "test.jsonl"
    records = [
        _make_record(resp_content_type="text/html", resp_body="<html>hi</html>"),
        _make_record(resp_content_type="image/png", resp_body="\x89PNG binary data"),
    ]
    with log.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    sanitize_log_file(str(log))

    with log.open() as f:
        lines = [json.loads(line) for line in f if line.strip()]

    assert lines[0]["response"]["body"] == "<html>hi</html>"
    assert lines[1]["response"]["body"] == _BODY_PLACEHOLDER


def test_sanitize_log_file_missing_is_noop(tmp_path: Path):
    sanitize_log_file(str(tmp_path / "does_not_exist.jsonl"))
