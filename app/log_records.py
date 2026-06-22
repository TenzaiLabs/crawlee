from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Content types considered textual (matched as prefixes after lowercasing and
# stripping parameters like charset).
_TEXTUAL_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/x-www-form-urlencoded",
)

_BODY_PLACEHOLDER = "[non-text body removed]"


def _is_textual_content_type(content_type: str | None) -> bool:
    if not content_type:
        return True
    ct = content_type.lower().split(";")[0].strip()
    return any(ct.startswith(p) for p in _TEXTUAL_PREFIXES)


def _content_type_from_headers(section: dict[str, Any]) -> str | None:
    headers = section.get("headers")
    if isinstance(headers, dict):
        ct = headers.get("content-type")
        if ct:
            return str(ct)
    header = section.get("header")
    if isinstance(header, dict):
        ct = header.get("content-type") or header.get("Content-Type")
        if ct:
            return str(ct)
    raw = section.get("raw")
    if isinstance(raw, str):
        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped:
                break
            if stripped.lower().startswith("content-type:"):
                return stripped.split(":", 1)[1].strip()
    return None


def _strip_body_from_raw(raw: str) -> str:
    for sep in ("\r\n\r\n", "\n\n"):
        idx = raw.find(sep)
        if idx != -1:
            return raw[: idx + len(sep)] + _BODY_PLACEHOLDER
    return raw


def _sanitize_section(section: dict[str, Any]) -> dict[str, Any]:
    ct = _content_type_from_headers(section)
    if _is_textual_content_type(ct):
        return section
    section = dict(section)
    if "body" in section:
        section["body"] = _BODY_PLACEHOLDER
    if "raw" in section:
        section["raw"] = _strip_body_from_raw(section["raw"])
    return section


def sanitize_record(data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data)
    for key in ("request", "response"):
        section = data.get(key)
        if isinstance(section, dict):
            data[key] = _sanitize_section(section)
    return data


def sanitize_log_file(log_path: str) -> None:
    if not os.path.exists(log_path):
        return
    tmp_path = log_path + ".tmp"
    changed = 0
    try:
        with (
            open(log_path, encoding="utf-8", errors="replace") as infile,
            open(tmp_path, "w", encoding="utf-8") as outfile,
        ):
            for line in infile:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    outfile.write(line)
                    continue
                if isinstance(data, dict):
                    sanitized = sanitize_record(data)
                    if sanitized is not data:
                        changed += 1
                    outfile.write(json.dumps(sanitized) + "\n")
                else:
                    outfile.write(line)
        os.replace(tmp_path, log_path)
        logger.info("Sanitized log file %s (%d records processed)", log_path, changed)
    except Exception:
        logger.warning("Failed to sanitize log file %s", log_path, exc_info=True)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _header_value_case_insensitive(headers: dict[str, Any], name: str) -> Any:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return value
    return None


def extract_request_url(data: dict[str, Any]) -> str | None:
    request = _as_dict(data.get("request"))
    url = request.get("endpoint") or request.get("url") or data.get("url")
    if not url:
        return None
    return str(url)


def extract_request_headers(data: dict[str, Any]) -> dict[str, Any]:
    request = _as_dict(data.get("request"))
    headers = request.get("headers") if isinstance(request.get("headers"), dict) else None
    if headers is None:
        headers = request.get("header") if isinstance(request.get("header"), dict) else None
    if headers is None:
        headers = data.get("headers") if isinstance(data.get("headers"), dict) else None
    return headers if isinstance(headers, dict) else {}


def normalize_request_record(data: dict[str, Any]) -> dict[str, Any] | None:
    request = _as_dict(data.get("request"))
    response = _as_dict(data.get("response"))

    request_header = request.get("header", {}) if isinstance(request.get("header"), dict) else {}
    response_header = response.get("header", {}) if isinstance(response.get("header"), dict) else {}

    method = request.get("method") or request_header.get("method") or data.get("method")
    url = extract_request_url(data)
    status = response.get("status_code") or response.get("status") or data.get("status")
    if status is None:
        raw = response.get("raw")
        if isinstance(raw, str) and raw.startswith("HTTP/"):
            parts = raw.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])

    response_headers = response.get("headers")
    content_type = (
        _header_value_case_insensitive(response_headers, "content-type")
        if isinstance(response_headers, dict)
        else None
    )
    if content_type is None:
        content_type = response_header.get("content-type") or response_header.get("Content-Type")

    timestamp = data.get("timestamp") or data.get("time")

    if not method or not url:
        return None

    return {
        "method": str(method),
        "url": str(url),
        "status": status,
        "content_type": content_type,
        "timestamp": timestamp,
        "headers": extract_request_headers(data),
    }
