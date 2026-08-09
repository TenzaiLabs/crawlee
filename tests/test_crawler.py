from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import crawler
from app.process import ProcessMemoryBudget, SubprocessResult


def _flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_standard_command_assigns_static_analysis_and_classification_flags() -> None:
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        extra_seed_urls=["https://example.com/seed", "https://example.com/seed"],
    )

    command = crawler.build_standard_katana_command(
        config,
        terminal_summary_path="/tmp/standard-terminal.json",
    )

    assert command.count("-u") == 3
    assert all(flag in command for flag in ("-jc", "-jsl", "-fx", "-kb", "-td"))
    assert all(flag in command for flag in ("-fsu", "-fst", "-mrs", "-duc"))
    assert _flag_value(command, "-fst") == "10"
    assert _flag_value(command, "-mrs") == str(5 * 1024 * 1024)
    assert _flag_value(command, "-ct") == "10m"
    assert _flag_value(command, "-terminal-summary") == "/tmp/standard-terminal.json"
    assert all(
        flag not in command
        for flag in ("-cwu", "-xhr", "-hybrid", "-known-files", "-kf", "-fpt", "-iqp")
    )
    assert _flag_value(command, "-crawl-out-scope") == "|".join(
        crawler.DEFAULT_EXCLUSION_PATTERNS
    )


def test_standard_command_honors_explicit_scope_and_client_limits_only() -> None:
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={
            "max_depth": 2,
            "rate_limit": 5,
            "concurrency": 3,
            "parallelism": 2,
            "exclude_filters": ["/admin", "/admin"],
            "exclude_regex": "/danger-zone",
            "crawl_scope": "example\\.com$",
            "crawl_duration": "90s",
            "timeout": 20,
        },
        headers=["Cookie: session=abc"],
    )

    command = crawler.build_katana_command(
        config,
        lane="standard",
        terminal_summary_path="terminal.json",
    )

    assert _flag_value(command, "-d") == "2"
    assert _flag_value(command, "-rl") == "5"
    assert _flag_value(command, "-c") == "3"
    assert _flag_value(command, "-p") == "2"
    assert _flag_value(command, "-cs") == r"example\.com$"
    assert _flag_value(command, "-ct") == "90s"
    assert _flag_value(command, "-timeout") == "20"
    assert _flag_value(command, "-crawl-out-scope") == "|".join(
        [*crawler.DEFAULT_EXCLUSION_PATTERNS, "/admin", "/danger-zone"]
    )
    assert _flag_value(command, "-H") == "Cookie: session=abc"
    assert crawler.build_exclusion_patterns(config) == [
        *crawler.DEFAULT_EXCLUSION_PATTERNS,
        "/admin",
        "/danger-zone",
    ]


def test_blocked_urls_to_exclude_patterns_normalizes_safe_same_scope_urls() -> None:
    patterns = crawler.blocked_urls_to_exclude_patterns(
        [
            "https://example.com/logout?next=/",
            "/account/delete/",
            "/account/delete",
            "javascript:alert(1)",
            "mailto:test@example.com",
            "https://evil.test/logout",
            "https://example.com/#logout",
        ],
        target_url="https://example.com",
        base_url="https://example.com/app/dashboard",
    )

    assert patterns == [
        "/logout(?:$|[/?#])",
        "/account/delete(?:$|[/?#])",
    ]


def test_katana_commands_merge_and_deduplicate_dynamic_exclusions() -> None:
    dynamic_pattern = "/account/delete(?:$|[/?#])"
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={
            "exclude_filters": ["/admin", "/admin"],
            "exclude_regex": "/danger-zone",
        },
        dynamic_exclude_patterns=[dynamic_pattern, "/admin"],
        cdp_url="ws://127.0.0.1:9222/devtools/browser/abc",
    )
    expected = [
        *crawler.DEFAULT_EXCLUSION_PATTERNS,
        "/admin",
        "/danger-zone",
        dynamic_pattern,
    ]

    assert crawler.build_exclusion_patterns(config) == expected
    standard = crawler.build_standard_katana_command(
        config,
        terminal_summary_path="standard-terminal.json",
    )
    pure_headless = crawler.build_pure_headless_katana_command(
        config,
        terminal_summary_path="pure-headless-terminal.json",
    )
    assert _flag_value(standard, "-crawl-out-scope") == "|".join(expected)
    assert _flag_value(pure_headless, "-crawl-out-scope") == "|".join(expected)


def test_pure_headless_command_uses_shared_chrome_and_browser_flags() -> None:
    config = crawler.CrawlConfig(
        target_url="https://example.com",
        cdp_url="ws://127.0.0.1:9222/devtools/browser/abc",
    )

    command = crawler.build_pure_headless_katana_command(
        config,
        terminal_summary_path="terminal.json",
    )

    assert _flag_value(command, "-cwu") == config.cdp_url
    assert _flag_value(command, "-p") == "1"
    assert _flag_value(command, "-pls") == "domcontentloaded"
    assert _flag_value(command, "-dwt") == "2"
    assert _flag_value(command, "-mfc") == "0"
    assert _flag_value(command, "-crawl-out-scope") == "|".join(
        crawler.DEFAULT_EXCLUSION_PATTERNS
    )
    assert all(flag in command for flag in ("-xhr", "-kb", "-fsu", "-fst"))
    assert all(
        flag not in command for flag in ("-jc", "-jsl", "-fx", "-td", "-mrs", "-rl", "-known-files")
    )


def test_pure_headless_command_requires_internal_cdp_endpoint() -> None:
    with pytest.raises(ValueError, match="server-owned CDP"):
        crawler.build_pure_headless_katana_command(
            crawler.CrawlConfig(target_url="https://example.com"),
            terminal_summary_path="terminal.json",
        )


def test_ip_target_gets_explicit_katana_scope() -> None:
    command = crawler.build_standard_katana_command(
        crawler.CrawlConfig(target_url="http://127.0.0.1:8000"),
        terminal_summary_path="terminal.json",
    )

    assert _flag_value(command, "-cs") == r"127\.0\.0\.1"


def _write_terminal(command: list[str], *, reason: str = "queue_exhausted") -> None:
    inputs = [command[index + 1] for index, value in enumerate(command) if value == "-u"]
    terminal_path = Path(_flag_value(command, "-terminal-summary"))
    terminal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "generated_at": "2026-07-23T00:00:00Z",
                "inputs": [
                    {"input": input_url, "reason": reason} for input_url in sorted(set(inputs))
                ],
            }
        )
    )


@pytest.mark.asyncio
async def test_run_crawl_sanitizes_artifact_and_requires_queue_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = ProcessMemoryBudget(1024)

    async def fake_run_safe_subprocess(
        command,
        *,
        timeout,
        on_output=None,
        cancel_event=None,
        memory_budget=None,
        diagnostic_tail_bytes=None,
    ):
        assert timeout > 0
        assert memory_budget is budget
        assert diagnostic_tail_bytes == 0
        assert on_output is not None
        await on_output(
            json.dumps(
                {
                    "request": {
                        "method": "GET",
                        "endpoint": "https://example.com/",
                        "raw": (
                            "GET / HTTP/1.1\r\nHost: example.com\r\nCookie: session=abc\r\n\r\n"
                        ),
                    },
                    "response": {"status_code": 200, "headers": {}, "body": "ok"},
                }
            )
        )
        _write_terminal(command)
        return SubprocessResult(exit_code=0, output="")

    monkeypatch.setattr(crawler, "run_safe_subprocess", fake_run_safe_subprocess)
    log_path = tmp_path / "job.jsonl"

    result = await crawler.run_crawl(
        crawler.CrawlConfig(target_url="https://example.com"),
        lane="standard",
        output_path=str(log_path),
        memory_budget=budget,
    )

    artifact = log_path.read_text()
    assert "Cookie: [redacted]" in artifact
    assert "session=abc" not in artifact
    assert result.lane == "standard"
    assert result.terminal_summary is not None
    assert result.terminal_summary["inputs"][0]["reason"] == "queue_exhausted"


@pytest.mark.asyncio
async def test_run_crawl_returns_partial_result_for_budget_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_safe_subprocess(command, **_kwargs):
        _write_terminal(command, reason="crawl_timeout")
        return SubprocessResult(exit_code=0, output="")

    monkeypatch.setattr(crawler, "run_safe_subprocess", fake_run_safe_subprocess)

    result = await crawler.run_crawl(
        crawler.CrawlConfig(target_url="https://example.com"),
        output_path=str(tmp_path / "job.jsonl"),
    )

    assert result.outcome == "partial"
    assert result.terminal_summary is not None
    assert result.terminal_summary["inputs"][0]["reason"] == "crawl_timeout"


@pytest.mark.asyncio
async def test_run_crawl_rejects_unknown_terminal_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_safe_subprocess(command, **_kwargs):
        _write_terminal(command, reason="unexpected_reason")
        return SubprocessResult(exit_code=0, output="")

    monkeypatch.setattr(crawler, "run_safe_subprocess", fake_run_safe_subprocess)

    with pytest.raises(
        crawler.KatanaTerminalSummaryError,
        match="unknown input reason",
    ):
        await crawler.run_crawl(
            crawler.CrawlConfig(target_url="https://example.com"),
            output_path=str(tmp_path / "job.jsonl"),
        )


@pytest.mark.asyncio
async def test_run_crawl_identifies_lane_when_terminal_summary_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_safe_subprocess(_command, **_kwargs):
        return SubprocessResult(exit_code=0, output="")

    monkeypatch.setattr(crawler, "run_safe_subprocess", fake_run_safe_subprocess)

    with pytest.raises(
        crawler.KatanaTerminalSummaryError,
        match="Katana standard: Katana did not write its terminal summary",
    ):
        await crawler.run_crawl(
            crawler.CrawlConfig(target_url="https://example.com"),
            lane="standard",
            output_path=str(tmp_path / "job.jsonl"),
        )


@pytest.mark.asyncio
async def test_run_crawl_enforces_process_wall_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stalled_process(*_args, **_kwargs):
        await _kwargs["on_output"](
            '{"request":{"method":"GET","endpoint":"https://example.com/partial"}}\n'
        )
        await asyncio.sleep(60)
        return SubprocessResult(exit_code=0, output="")

    monkeypatch.setattr(crawler, "run_safe_subprocess", stalled_process)
    monkeypatch.setattr(crawler, "CRAWLER_KATANA_PROCESS_TIMEOUT_SECONDS", 0.01)

    result = await crawler.run_crawl(
        crawler.CrawlConfig(target_url="https://example.com"),
        output_path=str(tmp_path / "job.jsonl"),
    )

    assert result.outcome == "partial"
    assert result.terminal_summary is None
    assert result.termination_reason == "process_deadline"
    assert result.evidence() == {
        "lane": "standard",
        "outcome": "partial",
        "terminal_summary": None,
        "termination_reason": "process_deadline",
    }
    assert "https://example.com/partial" in (tmp_path / "job.jsonl").read_text()
