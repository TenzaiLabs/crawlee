from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from .common import coerce_int, is_host_in_scope, open_text_writer
from .process import run_safe_subprocess
from .scope_config import _coerce_bool, validate_scope_config
from .settings import CRAWLER_SUBPROCESS_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

DEFAULT_EXCLUSION_PATTERNS = [
    "logout",
    "signout",
    "log-out",
    "sign-out",
    "delete",
    "remove",
    "unsubscribe",
    "deactivate",
]


@dataclass
class CrawlConfig:
    target_url: str
    scope_config: dict | None = None
    headers: list[str] | None = None
    extra_seed_urls: list[str] | None = None
    dynamic_exclude_patterns: list[str] | None = None


def _unique_patterns(patterns: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for pattern in patterns:
        if pattern and pattern not in seen:
            seen.add(pattern)
            ordered.append(pattern)
    return ordered


def blocked_urls_to_exclude_patterns(
    blocked_urls: list[str] | None,
    *,
    target_url: str,
    base_url: str | None = None,
) -> list[str]:
    """Convert URL hints into safe Katana out-of-scope regex snippets."""
    if not blocked_urls:
        return []

    patterns: list[str] = []
    base = base_url or target_url
    for blocked_url in blocked_urls:
        url_text = str(blocked_url).strip()
        if not url_text or len(url_text) > 2048:
            continue

        parsed = urlparse(urljoin(base, url_text))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if not is_host_in_scope(parsed.hostname, target_url):
            continue

        path = parsed.path.rstrip("/")
        if not path:
            continue

        patterns.append(f"{re.escape(path)}(?:$|[/?#])")

    return _unique_patterns(patterns)


def build_exclusion_patterns(config: CrawlConfig) -> list[str]:
    scope_config = config.scope_config or {}
    extra_filters = scope_config.get("exclude_filters")
    filters: list[str] = DEFAULT_EXCLUSION_PATTERNS.copy()
    if isinstance(extra_filters, list):
        filters.extend(str(item) for item in extra_filters if item)

    exclude_regex = scope_config.get("exclude_regex")
    if exclude_regex:
        filters.append(str(exclude_regex))

    if config.dynamic_exclude_patterns:
        filters.extend(str(pattern) for pattern in config.dynamic_exclude_patterns if pattern)

    return _unique_patterns(filters)


def build_katana_command(
    config: CrawlConfig,
    proxy_url: str = "http://127.0.0.1:8888",
) -> list[str]:
    scope_config = config.scope_config or {}
    validate_scope_config(scope_config)

    depth = coerce_int(scope_config.get("max_depth"), 5)
    rate_limit = coerce_int(scope_config.get("rate_limit"), 10)
    field_scope = scope_config.get("field_scope", "rdn")
    if not isinstance(field_scope, str) or not field_scope:
        field_scope = "rdn"

    command = [
        "katana",
        "-u",
        config.target_url,
        "-proxy",
        proxy_url,
        "-silent",
        "-jsonl",
        "-known-files",
        "all",
        "-jc",
        "-jsl",
        "-no-color",
        "-verbose",
        "-fs",
        field_scope,
        "-d",
        str(depth),
        "-rl",
        str(rate_limit),
    ]

    for seed_url in config.extra_seed_urls or []:
        command.extend(["-u", seed_url])

    crawl_scope = scope_config.get("crawl_scope")
    if not crawl_scope:
        # For IP-based targets, rdn/dn field-scope doesn't constrain crawling
        # properly — katana follows every external link it discovers.
        # Auto-add a -cs regex so only URLs matching the target IP are crawled.
        parsed = urlparse(config.target_url)
        host = parsed.hostname or ""
        try:
            ipaddress.ip_address(host)
            crawl_scope = re.escape(host)
        except ValueError:
            pass
    if crawl_scope:
        command.extend(["-cs", str(crawl_scope)])

    filters = build_exclusion_patterns(config)
    if filters:
        command.extend(["-crawl-out-scope", "|".join(filters)])

    concurrency = scope_config.get("concurrency")
    if concurrency is not None:
        command.extend(["-c", str(coerce_int(concurrency, 10))])

    parallelism = scope_config.get("parallelism")
    if parallelism is not None:
        command.extend(["-p", str(coerce_int(parallelism, 10))])

    crawl_duration = scope_config.get("crawl_duration")
    if crawl_duration:
        command.extend(["-ct", str(crawl_duration)])

    headless = _coerce_bool(scope_config.get("headless", True))
    cdp_url = scope_config.get("cdp_url") or scope_config.get("chrome_ws_url")
    if isinstance(cdp_url, str) and cdp_url:
        # Reuse an existing Chrome instance instead of starting a new one.
        # Katana flag name: -cwu, -chrome-ws-url.
        command.extend(["-cwu", cdp_url])

    if headless:
        command.append("-hybrid")
        command.extend(
            [
                "-headless-options",
                f"proxy-server={proxy_url},proxy-bypass-list=<-loopback>",
            ]
        )

    if _coerce_bool(scope_config.get("system_chrome")):
        command.append("-system-chrome")

    system_chrome_path = scope_config.get("system_chrome_path")
    if isinstance(system_chrome_path, str) and system_chrome_path:
        command.extend(["-system-chrome-path", system_chrome_path])

    request_timeout = scope_config.get("timeout")
    if request_timeout is not None:
        command.extend(["-timeout", str(coerce_int(request_timeout, 10))])

    for header in config.headers or []:
        command.extend(["-H", header])

    logger.debug("Built katana command for target_url=%s", config.target_url)
    return command


async def run_crawl(
    config: CrawlConfig,
    cancel_event: asyncio.Event | None = None,
    log_path: str | None = None,
) -> None:
    logger.info("Starting crawl for target_url=%s", config.target_url)
    scope_config = config.scope_config or {}
    max_pages = coerce_int(scope_config.get("max_pages"), 0)
    stop_event = asyncio.Event() if max_pages > 0 else None
    page_count = 0
    recent_output: deque[str] = deque(maxlen=20)
    katana_log = log_path + ".katana" if log_path else None
    log_file = open_text_writer(katana_log) if katana_log else None

    async def _on_output(line: str) -> None:
        nonlocal page_count
        stripped = line.strip()
        recent_output.append(stripped)
        if log_file and stripped.startswith("{"):
            log_file.write(stripped + "\n")
            log_file.flush()
        if stop_event is None:
            return
        page_count += 1
        if page_count >= max_pages:
            stop_event.set()
            logger.info("Reached max_pages=%d, stopping katana crawl", max_pages)

    headless = _coerce_bool(scope_config.get("headless", True))
    env: dict[str, str] | None = None
    if headless:
        env = {
            "HTTP_PROXY": "http://127.0.0.1:8888",
            "HTTPS_PROXY": "http://127.0.0.1:8888",
            "NO_PROXY": "",
        }

    stderr_log = log_path + ".stderr" if log_path else None
    try:
        result = await run_safe_subprocess(
            build_katana_command(config),
            timeout=CRAWLER_SUBPROCESS_TIMEOUT_SECONDS,
            on_output=_on_output,
            cancel_event=cancel_event,
            stop_event=stop_event,
            env=env,
            stderr_path=stderr_log,
        )
    finally:
        if log_file:
            log_file.close()

    if cancel_event is not None and cancel_event.is_set():
        logger.info("Crawl ended due to cancellation for target_url=%s", config.target_url)
        return
    if stop_event is not None and stop_event.is_set():
        logger.info(
            "Crawl ended because max_pages limit was hit for target_url=%s",
            config.target_url,
        )
        return
    if result.exit_code != 0:
        output_tail = "\n".join(recent_output).strip()
        detail = output_tail if output_tail else result.output.strip()
        logger.warning(
            "Katana exited non-zero code=%d target_url=%s",
            result.exit_code,
            config.target_url,
        )
        raise RuntimeError(f"Katana exited with code {result.exit_code}: {detail}")
    logger.info("Crawl finished successfully for target_url=%s", config.target_url)
