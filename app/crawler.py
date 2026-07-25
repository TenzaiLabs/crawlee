from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from .common import coerce_int, open_text_reader, open_text_writer
from .log_records import sanitize_record
from .process import ProcessMemoryBudget, run_safe_subprocess
from .scope_config import validate_scope_config
from .settings import (
    CRAWLER_KATANA_PROCESS_TIMEOUT_SECONDS,
    CRAWLER_SUBPROCESS_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

DEFAULT_CRAWL_DURATION = "10m"
KATANA_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
KATANA_SIMILARITY_THRESHOLD = 10
PURE_HEADLESS_PAGE_LOAD_STRATEGY = "domcontentloaded"
PURE_HEADLESS_DOM_WAIT_SECONDS = 2

KatanaLane = Literal["standard", "pure-headless"]
KatanaCompletion = Literal["complete", "partial"]
_COMPLETED_TERMINAL_REASONS = {"queue_exhausted", "crawl_timeout", "input_failure"}


class KatanaRunError(RuntimeError):
    pass


class KatanaProcessDeadlineExceeded(KatanaRunError):
    pass


class KatanaTerminalSummaryError(KatanaRunError):
    pass


@dataclass(frozen=True)
class CrawlConfig:
    target_url: str
    scope_config: dict[str, Any] | None = None
    headers: list[str] | None = None
    extra_seed_urls: list[str] | None = None
    cdp_url: str | None = None


@dataclass(frozen=True)
class KatanaRunResult:
    lane: KatanaLane
    terminal_summary: dict[str, Any]
    outcome: KatanaCompletion = "complete"

    def evidence(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "outcome": self.outcome,
            "terminal_summary": self.terminal_summary,
        }


def _unique_patterns(patterns: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for pattern in patterns:
        if pattern and pattern not in seen:
            seen.add(pattern)
            ordered.append(pattern)
    return ordered


def build_exclusion_patterns(config: CrawlConfig) -> list[str]:
    """Return only caller-supplied Katana filters.

    The server does not infer destructive routes or convert authentication-agent
    observations into crawl policy.
    """

    scope_config = config.scope_config or {}
    filters: list[str] = []
    extra_filters = scope_config.get("exclude_filters")
    if isinstance(extra_filters, list):
        filters.extend(str(item) for item in extra_filters if item)
    exclude_regex = scope_config.get("exclude_regex")
    if exclude_regex:
        filters.append(str(exclude_regex))
    return _unique_patterns(filters)


def crawl_inputs(config: CrawlConfig) -> list[str]:
    return [config.target_url, *(config.extra_seed_urls or [])]


def _crawl_scope(config: CrawlConfig) -> str | None:
    scope_config = config.scope_config or {}
    configured = scope_config.get("crawl_scope")
    if configured:
        return str(configured)
    host = urlparse(config.target_url).hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return re.escape(host)


def _base_katana_command(
    config: CrawlConfig,
    *,
    terminal_summary_path: str,
) -> list[str]:
    scope_config = config.scope_config or {}
    validate_scope_config(scope_config)
    depth = coerce_int(scope_config.get("max_depth"), 5)
    field_scope = scope_config.get("field_scope", "rdn")
    if not isinstance(field_scope, str) or not field_scope:
        field_scope = "rdn"

    command = ["katana"]
    for seed_url in crawl_inputs(config):
        command.extend(["-u", seed_url])
    command.extend(
        [
            "-silent",
            "-jsonl",
            "-no-color",
            "-duc",
            "-fs",
            field_scope,
            "-d",
            str(depth),
            "-ct",
            str(scope_config.get("crawl_duration", DEFAULT_CRAWL_DURATION)),
            "-terminal-summary",
            terminal_summary_path,
        ]
    )

    crawl_scope = _crawl_scope(config)
    if crawl_scope:
        command.extend(["-cs", crawl_scope])
    exclusions = build_exclusion_patterns(config)
    if exclusions:
        command.extend(["-crawl-out-scope", "|".join(exclusions)])
    request_timeout = scope_config.get("timeout")
    if request_timeout is not None:
        command.extend(["-timeout", str(coerce_int(request_timeout, 10))])
    for header in config.headers or []:
        command.extend(["-H", header])
    return command


def build_standard_katana_command(
    config: CrawlConfig,
    *,
    terminal_summary_path: str,
) -> list[str]:
    command = _base_katana_command(config, terminal_summary_path=terminal_summary_path)
    command.extend(
        [
            "-jc",
            "-jsl",
            "-fx",
            "-kb",
            "-td",
            "-fsu",
            "-fst",
            str(KATANA_SIMILARITY_THRESHOLD),
            "-mrs",
            str(KATANA_MAX_RESPONSE_BYTES),
        ]
    )
    scope_config = config.scope_config or {}
    command.extend(["-rl", str(coerce_int(scope_config.get("rate_limit"), 10))])
    concurrency = scope_config.get("concurrency")
    if concurrency is not None:
        command.extend(["-c", str(coerce_int(concurrency, 10))])
    parallelism = scope_config.get("parallelism")
    if parallelism is not None:
        command.extend(["-p", str(coerce_int(parallelism, 10))])
    return command


def build_pure_headless_katana_command(
    config: CrawlConfig,
    *,
    terminal_summary_path: str,
) -> list[str]:
    if not config.cdp_url:
        raise ValueError("pure-headless Katana requires the server-owned CDP endpoint")
    command = _base_katana_command(config, terminal_summary_path=terminal_summary_path)
    command.extend(
        [
            "-cwu",
            config.cdp_url,
            "-p",
            "1",
            "-xhr",
            "-kb",
            "-fsu",
            "-fst",
            str(KATANA_SIMILARITY_THRESHOLD),
            "-mfc",
            "0",
            "-pls",
            PURE_HEADLESS_PAGE_LOAD_STRATEGY,
            "-dwt",
            str(PURE_HEADLESS_DOM_WAIT_SECONDS),
        ]
    )
    return command


def build_katana_command(
    config: CrawlConfig,
    *,
    lane: KatanaLane = "standard",
    terminal_summary_path: str = "katana-terminal-summary.json",
) -> list[str]:
    if lane == "standard":
        return build_standard_katana_command(
            config,
            terminal_summary_path=terminal_summary_path,
        )
    return build_pure_headless_katana_command(
        config,
        terminal_summary_path=terminal_summary_path,
    )


def _load_terminal_summary(path: str, expected_inputs: list[str]) -> dict[str, Any]:
    if not os.path.exists(path):
        raise KatanaTerminalSummaryError("Katana did not write its terminal summary")
    try:
        with open_text_reader(path) as handle:
            summary = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise KatanaTerminalSummaryError("Katana terminal summary is not valid JSON") from exc
    if not isinstance(summary, dict):
        raise KatanaTerminalSummaryError("Katana terminal summary must be an object")
    if summary.get("schema_version") != 1 or summary.get("status") != "completed":
        raise KatanaTerminalSummaryError("Katana terminal summary is not completed schema v1")
    inputs = summary.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise KatanaTerminalSummaryError("Katana terminal summary has no input records")
    actual_inputs: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict) or not isinstance(item.get("input"), str):
            raise KatanaTerminalSummaryError("Katana terminal summary has an invalid input record")
        input_url = item["input"]
        actual_inputs.add(input_url)
        reason = item.get("reason")
        if reason not in _COMPLETED_TERMINAL_REASONS:
            raise KatanaTerminalSummaryError(
                f"Katana terminal summary has unknown input reason {reason!r}"
            )
    expected = set(expected_inputs)
    if actual_inputs != expected:
        raise KatanaTerminalSummaryError(
            "Katana terminal input set differs from the submitted seed batch"
        )
    return summary


def _terminal_outcome(summary: dict[str, Any]) -> KatanaCompletion:
    inputs = summary["inputs"]
    return "complete" if all(item["reason"] == "queue_exhausted" for item in inputs) else "partial"


async def run_crawl(
    config: CrawlConfig,
    *,
    lane: KatanaLane = "standard",
    output_path: str,
    terminal_summary_path: str | None = None,
    cancel_event: asyncio.Event | None = None,
    memory_budget: ProcessMemoryBudget | None = None,
) -> KatanaRunResult:
    logger.info("Starting %s crawl for target_url=%s", lane, config.target_url)
    terminal_path = terminal_summary_path or f"{output_path}.terminal.json"
    Path(terminal_path).unlink(missing_ok=True)
    malformed_json_lines = 0
    log_file = open_text_writer(output_path)

    async def _on_output(line: str) -> None:
        nonlocal malformed_json_lines
        stripped = line.strip()
        if not stripped.startswith("{"):
            return
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            malformed_json_lines += 1
            return
        if not isinstance(record, dict):
            malformed_json_lines += 1
            return
        log_file.write(json.dumps(sanitize_record(record), ensure_ascii=False) + "\n")
        log_file.flush()

    try:
        try:
            async with asyncio.timeout(CRAWLER_KATANA_PROCESS_TIMEOUT_SECONDS):
                result = await run_safe_subprocess(
                    build_katana_command(
                        config,
                        lane=lane,
                        terminal_summary_path=terminal_path,
                    ),
                    timeout=CRAWLER_SUBPROCESS_TIMEOUT_SECONDS,
                    on_output=_on_output,
                    cancel_event=cancel_event,
                    memory_budget=memory_budget,
                    diagnostic_tail_bytes=0,
                )
        except TimeoutError as exc:
            raise KatanaProcessDeadlineExceeded(
                f"Katana {lane} process exceeded its wall-clock deadline"
            ) from exc
    finally:
        log_file.close()

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError
    if result.exit_code != 0:
        raise KatanaRunError(f"Katana {lane} exited with code {result.exit_code}")
    if malformed_json_lines:
        raise KatanaRunError(
            f"Katana {lane} emitted {malformed_json_lines} malformed JSON record(s)"
        )
    try:
        terminal_summary = _load_terminal_summary(terminal_path, crawl_inputs(config))
    except KatanaTerminalSummaryError as exc:
        raise KatanaTerminalSummaryError(f"Katana {lane}: {exc}") from exc
    outcome = _terminal_outcome(terminal_summary)
    logger.info(
        "Crawl finished for target_url=%s lane=%s outcome=%s",
        config.target_url,
        lane,
        outcome,
    )
    return KatanaRunResult(
        lane=lane,
        terminal_summary=terminal_summary,
        outcome=outcome,
    )
