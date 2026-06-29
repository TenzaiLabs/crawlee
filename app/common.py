from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse


def coerce_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return fallback


def allowed_hostnames(target_url: str | None) -> tuple[set[str], str] | None:
    if not target_url:
        return None
    parsed = urlparse(target_url)
    if not parsed.hostname:
        return None
    root_host = parsed.hostname
    if root_host.startswith("www."):
        root_host = root_host[4:]
    return {root_host, f"www.{root_host}"}, root_host


def is_host_in_scope(host: str | None, target_url: str | None) -> bool:
    allowed = allowed_hostnames(target_url)
    if allowed is None:
        return True
    if not host:
        return True
    allowed_hosts, root_host = allowed
    return host in allowed_hosts or host.endswith(f".{root_host}")


_SECRET_KV_PATTERN = re.compile(r"(?i)\b(api[_-]?key|token|password|secret)(\s*[=:]\s*)([^\s,;]+)")
_HEADER_PATTERN = re.compile(r"(?i)^(authorization|cookie):\s*(.+)$")


def sanitize_log_value(value: object) -> str:
    text = redact_sensitive_text(str(value))
    return text.replace("\r\n", "\\r\\n").replace("\r", "\\r").replace("\n", "\\n")


def redact_sensitive_text(value: str) -> str:
    def _replace_match(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    return _SECRET_KV_PATTERN.sub(_replace_match, value)


def redact_header_value(value: str) -> str:
    match = _HEADER_PATTERN.match(value.strip())
    if not match:
        return redact_sensitive_text(value)
    return f"{match.group(1)}: [REDACTED]"


def redact_command(parts: Iterable[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for part in parts:
        piece = str(part)
        if redact_next:
            redacted.append(redact_header_value(piece))
            redact_next = False
            continue
        if piece in {"-H", "--header"}:
            redacted.append(piece)
            redact_next = True
            continue
        redacted.append(redact_sensitive_text(piece))
    return " ".join(redacted)


def open_text_reader(path: str):
    return open(path, encoding="utf-8")


def open_text_writer(path: str):
    return open(path, "w", encoding="utf-8")
