from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import auth_agent, orchestrator


@pytest.mark.asyncio
async def test_run_auth_if_needed_returns_dynamic_exclusion_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_authenticate(
        target_url: str,
        auth_config: dict,
        cancel_event: asyncio.Event,
    ) -> auth_agent.AuthResult:
        assert target_url == "https://example.com"
        assert "_proxify_log_path" not in auth_config
        assert not cancel_event.is_set()
        return auth_agent.AuthResult(
            headers=["Authorization: Bearer token"],
            cookies=[{"name": "session", "value": "abc"}],
            landing_url="https://example.com/app/dashboard",
            blocked_urls=["/logout", "https://evil.test/delete"],
            discovered_urls=[
                "https://example.com/projects/alpha",
                "https://example.com/app/dashboard",
            ],
        )

    monkeypatch.setattr(orchestrator.auth_agent, "authenticate", fake_authenticate)

    auth_context = await orchestrator._run_auth_if_needed(
        "job-1",
        "https://example.com",
        {"login_url": "https://example.com/login"},
        ["Cookie: session=abc"],
        True,
        asyncio.Event(),
    )

    assert auth_context.headers == ["Cookie: session=abc", "Authorization: Bearer token"]
    assert auth_context.landing_url == "https://example.com/app/dashboard"
    assert auth_context.extra_seed_urls == [
        "https://example.com/app/dashboard",
        "https://example.com/projects/alpha",
    ]
    assert auth_context.discovered_urls == [
        "https://example.com/projects/alpha",
        "https://example.com/app/dashboard",
    ]
    assert auth_context.dynamic_exclude_patterns == ["/logout(?:$|[/?#])"]
    assert auth_context.auth_blocked_url_count == 2
    assert auth_context.auth_applied_blocked_url_count == 1
    assert auth_context.auth_ignored_blocked_url_count == 1


def test_build_generated_exclusions_payload() -> None:
    auth_context = orchestrator.CrawlAuthContext(
        headers=["Cookie: session=abc"],
        extra_seed_urls=["https://example.com/app/dashboard"],
        discovered_urls=["https://example.com/app/dashboard"],
        dynamic_exclude_patterns=["/logout(?:$|[/?#])"],
        auth_blocked_url_count=1,
        auth_applied_blocked_url_count=1,
        auth_ignored_blocked_url_count=0,
    )
    config = orchestrator.crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={"exclude_filters": ["/admin"]},
        dynamic_exclude_patterns=auth_context.dynamic_exclude_patterns,
    )

    assert orchestrator.build_generated_exclusions_payload(config, auth_context) == {
        "auth_blocked_url_count": 1,
        "auth_applied_blocked_url_count": 1,
        "auth_ignored_blocked_url_count": 0,
        "auth_dynamic_patterns": ["/logout(?:$|[/?#])"],
        "auth_discovered_url_count": 1,
        "auth_discovered_urls": ["https://example.com/app/dashboard"],
        "extra_seed_urls": ["https://example.com/app/dashboard"],
        "effective_patterns": [
            *orchestrator.crawler.DEFAULT_EXCLUSION_PATTERNS,
            "/admin",
            "/logout(?:$|[/?#])",
        ],
    }


def test_extract_manual_headers_resolves_env_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_COOKIE", "session=abc")

    assert orchestrator._extract_manual_headers({"headers": ["Cookie: {{env:APP_COOKIE}}"]}) == [
        "Cookie: session=abc"
    ]


@pytest.mark.asyncio
async def test_run_auth_if_needed_returns_manual_header_context() -> None:
    auth_context = await orchestrator._run_auth_if_needed(
        "job-1",
        "https://example.com",
        {"headers": ["Cookie: session=abc"]},
        ["Cookie: session=abc"],
        False,
        asyncio.Event(),
    )

    assert auth_context == orchestrator.CrawlAuthContext(headers=["Cookie: session=abc"])


@pytest.mark.asyncio
async def test_complete_job_persists_result_and_metadata_atomically(app) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at)
        VALUES (?, 'processing', ?, datetime('now'))
        """,
        ("job-complete", "https://example.com"),
    )
    sitemap = {
        "entries": [{"method": "GET", "url": "https://example.com"}],
        "tree": {"children": {}, "pages": []},
    }

    assert await orchestrator.complete_job("job-complete", sitemap) is True

    row = await orchestrator.db.fetch_one("SELECT * FROM jobs WHERE job_id = ?", ("job-complete",))
    assert row is not None
    assert row["status"] == "completed"
    assert row["finished_at"] is not None
    assert row["result_entry_count"] == 1
    assert row["result_size_bytes"] == len(row["sitemap"].encode("utf-8"))
    assert orchestrator.db.loads_json(row["sitemap"]) == sitemap


@pytest.mark.asyncio
async def test_complete_job_does_not_overwrite_cancelled_state(app) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at, finished_at)
        VALUES (?, 'cancelled', ?, datetime('now'), datetime('now'))
        """,
        ("job-cancelled", "https://example.com"),
    )

    assert (
        await orchestrator.complete_job(
            "job-cancelled",
            {"entries": [], "tree": {"children": {}, "pages": []}},
        )
        is False
    )
    row = await orchestrator.db.fetch_one(
        "SELECT status, sitemap FROM jobs WHERE job_id = ?", ("job-cancelled",)
    )
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["sitemap"] is None


@pytest.mark.asyncio
async def test_completion_loses_to_requested_cancellation(app) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at)
        VALUES (?, 'processing', ?, datetime('now'))
        """,
        ("job-cancel-race", "https://example.com"),
    )
    cancel_event = asyncio.Event()
    cancel_event.set()

    assert (
        await orchestrator.complete_job(
            "job-cancel-race",
            {"entries": [], "tree": {"children": {}, "pages": []}},
            cancel_event,
        )
        is False
    )
    row = await orchestrator.db.fetch_one(
        "SELECT status, sitemap FROM jobs WHERE job_id = ?",
        ("job-cancel-race",),
    )
    assert row is not None
    assert row["status"] == "processing"
    assert row["sitemap"] is None


@pytest.mark.asyncio
async def test_queued_cancellation_prevents_runner_claim(app) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at)
        VALUES (?, 'queued', ?, datetime('now'))
        """,
        ("job-queued-cancel", "https://example.com"),
    )

    assert await orchestrator.cancel_queued_job("job-queued-cancel") is True
    assert (
        await orchestrator.transition_job_status(
            "job-queued-cancel",
            {orchestrator.JobStatus.queued},
            orchestrator.JobStatus.crawling,
        )
        is False
    )
    row = await orchestrator.db.fetch_one(
        "SELECT status, finished_at FROM jobs WHERE job_id = ?",
        ("job-queued-cancel",),
    )
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["finished_at"] is not None


@pytest.mark.asyncio
async def test_run_job_persists_parsed_sitemap_once(app, monkeypatch: pytest.MonkeyPatch) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at)
        VALUES (?, 'queued', ?, datetime('now'))
        """,
        ("job-run", "https://example.com"),
    )
    proxy_process = SimpleNamespace(log_path="/tmp/job-run.jsonl")

    async def noop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(
        orchestrator.proxy,
        "start_proxy",
        lambda job_id: _async_value(proxy_process),
    )
    monkeypatch.setattr(orchestrator.proxy, "wait_for_proxy", noop)
    monkeypatch.setattr(orchestrator.proxy, "check_target_connectivity", noop)
    monkeypatch.setattr(orchestrator.proxy, "stop_proxy", noop)
    monkeypatch.setattr(orchestrator.crawler, "run_crawl", noop)
    monkeypatch.setattr(orchestrator, "sanitize_log_file", lambda path: None)
    calls = 0
    sitemap = {"entries": [], "tree": {"children": {}, "pages": []}}

    def parse_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["require_artifacts"] is True
        return sitemap

    monkeypatch.setattr(orchestrator.parser, "parse_log", parse_once)

    await orchestrator.run_job("job-run", asyncio.Event())

    row = await orchestrator.db.fetch_one("SELECT * FROM jobs WHERE job_id = ?", ("job-run",))
    assert row is not None
    assert row["status"] == "completed"
    assert orchestrator.db.loads_json(row["sitemap"]) == sitemap
    assert calls == 1


async def _async_value(value):
    return value
