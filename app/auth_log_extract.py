from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import urlparse

from .common import is_host_in_scope, redact_header_value
from .log_records import extract_authorization_headers, extract_request_url
from .settings import CRAWLER_AUTH_SCAN_MAX_BYTES

logger = logging.getLogger(__name__)


def format_cookie_header(cookies: list[dict]) -> str | None:
    pairs: list[str] = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs) if pairs else None


async def extract_authorization_headers_from_log(
    log_path: str | None,
    target_url: str,
    *,
    max_bytes: int = CRAWLER_AUTH_SCAN_MAX_BYTES,
) -> list[str]:
    if not log_path:
        logger.info("Skipping authorization header extraction: no log path provided")
        return []
    if not os.path.exists(log_path):
        logger.warning(
            "Skipping authorization header extraction: log path does not exist: %s", log_path
        )
        return []

    logger.info("Starting authorization header scan for %s (max_bytes=%d)", log_path, max_bytes)

    def _scan() -> list[str]:
        try:
            with open(log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(size - max_bytes, 0), os.SEEK_SET)
                chunk = handle.read()
        except OSError:
            logger.warning("Failed to read authorization scan log: %s", log_path, exc_info=True)
            return []

        text = chunk.decode("utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip()]
        logger.debug("Authorization scan loaded %d non-empty JSONL lines", len(lines))
        if size > max_bytes and lines:
            lines = lines[1:]
            logger.debug("Authorization scan dropped first partial line due to max_bytes window")

        found: list[str] = []
        seen: set[str] = set()
        scanned = 0
        for line in reversed(lines):
            scanned += 1
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON line during authorization scan")
                continue

            if not isinstance(data, dict):
                continue

            url = extract_request_url(data)
            if not url:
                logger.debug("Skipping record without request URL")
                continue
            host = urlparse(url).hostname
            if not is_host_in_scope(host, target_url):
                logger.debug("Skipping out-of-scope host during authorization scan: %s", host)
                continue

            for header in extract_authorization_headers(data):
                if header not in seen:
                    seen.add(header)
                    found.append(header)

            if found:
                logger.info(
                    "Authorization scan found %d header(s) after %d line(s)", len(found), scanned
                )
                break

        if not found:
            logger.warning(
                "Authorization scan completed with no headers found after %d line(s)", scanned
            )
        else:
            for header in found:
                logger.debug("Authorization header detected: %s", redact_header_value(header))
        return list(reversed(found))

    return await asyncio.to_thread(_scan)
