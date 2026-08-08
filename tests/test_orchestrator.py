from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from app import auth_agent, cdp_observer, orchestrator


@pytest.mark.asyncio
async def test_run_auth_if_needed_does_not_turn_agent_hints_into_crawl_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_authenticate(
        target_url: str,
        auth_config: dict,
        cancel_event: asyncio.Event,
        *,
        context,
    ) -> auth_agent.AuthResult:
        assert target_url == "https://example.com"
        assert auth_config == {"login_url": "https://example.com/login"}
        assert not cancel_event.is_set()
        assert context == "shared-context"
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
        browser_context="shared-context",
    )

    assert auth_context.headers == ["Cookie: session=abc", "Authorization: Bearer token"]
    assert auth_context.landing_url == "https://example.com/app/dashboard"
    assert auth_context.extra_seed_urls == [
        "https://example.com/app/dashboard",
        "https://example.com/projects/alpha",
        "https://example.com/app/dashboard",
    ]
    assert auth_context.discovered_urls == [
        "https://example.com/projects/alpha",
        "https://example.com/app/dashboard",
    ]
    assert auth_context.auth_blocked_url_count == 2


def test_build_generated_exclusions_payload() -> None:
    auth_context = orchestrator.CrawlAuthContext(
        headers=["Cookie: session=abc"],
        extra_seed_urls=["https://example.com/app/dashboard"],
        discovered_urls=["https://example.com/app/dashboard"],
        auth_blocked_url_count=1,
    )
    config = orchestrator.crawler.CrawlConfig(
        target_url="https://example.com",
        scope_config={"exclude_filters": ["/admin"]},
    )

    assert orchestrator.build_generated_exclusions_payload(config, auth_context) == {
        "auth_blocked_url_count": 1,
        "auth_applied_blocked_url_count": 0,
        "auth_ignored_blocked_url_count": 1,
        "auth_dynamic_patterns": [],
        "auth_discovered_url_count": 1,
        "auth_discovered_urls": ["https://example.com/app/dashboard"],
        "extra_seed_urls": ["https://example.com/app/dashboard"],
        "effective_patterns": ["/admin"],
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
async def test_discovery_completion_persists_result_and_metadata_atomically(app) -> None:
    baseline = {"entries": [], "tree": {"children": {}, "pages": []}}
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at, baseline_sitemap)
        VALUES (?, 'processing', ?, datetime('now'), ?)
        """,
        (
            "job-complete",
            "https://example.com",
            orchestrator.serialize_sitemap(baseline)[0],
        ),
    )
    sitemap = {
        "entries": [{"method": "GET", "url": "https://example.com"}],
        "tree": {"children": {}, "pages": []},
    }
    result = orchestrator.DiscoveryResult(
        outcome=orchestrator.DiscoveryOutcome.fixpoint,
        rounds=1,
        new_entry_count=1,
        state_count=2,
        workflow_count=1,
        stop_reason="complete_round_added_nothing",
    )

    assert await orchestrator.complete_discovery_job(
        "job-complete",
        sitemap,
        {"schema_version": 1},
        result,
        asyncio.Event(),
    )

    row = await orchestrator.db.fetch_one("SELECT * FROM jobs WHERE job_id = ?", ("job-complete",))
    assert row is not None
    assert row["status"] == "completed"
    assert row["finished_at"] is not None
    assert row["result_entry_count"] == 1
    assert row["result_size_bytes"] == len(row["sitemap"].encode("utf-8"))
    persisted = cast(dict, orchestrator.db.loads_json(row["sitemap"]))
    assert persisted["entries"] == sitemap["entries"]
    assert persisted["discovery"] == result.model_dump(mode="json")


@pytest.mark.asyncio
async def test_discovery_completion_does_not_overwrite_cancelled_state(app) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at, finished_at)
        VALUES (?, 'cancelled', ?, datetime('now'), datetime('now'))
        """,
        ("job-cancelled", "https://example.com"),
    )

    assert (
        await orchestrator.complete_discovery_job(
            "job-cancelled",
            {"entries": [], "tree": {"children": {}, "pages": []}},
            {},
            orchestrator.DiscoveryResult(
                outcome=orchestrator.DiscoveryOutcome.fixpoint,
                rounds=0,
                new_entry_count=0,
                state_count=0,
                workflow_count=0,
                stop_reason="complete_round_added_nothing",
            ),
            asyncio.Event(),
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
async def test_discovery_completion_marks_partial_outcome_in_evidence(app) -> None:
    baseline = {"entries": [], "tree": {"children": {}, "pages": []}}
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at, baseline_sitemap)
        VALUES (?, 'processing', ?, datetime('now'), ?)
        """,
        (
            "job-partial-discovery",
            "https://example.com",
            orchestrator.serialize_sitemap(baseline)[0],
        ),
    )
    result = orchestrator.DiscoveryResult(
        outcome=orchestrator.DiscoveryOutcome.partial_failure,
        rounds=1,
        new_entry_count=0,
        state_count=3,
        workflow_count=1,
        stop_reason="model_decision_failed",
    )

    assert await orchestrator.complete_discovery_job(
        "job-partial-discovery",
        baseline,
        {"schema_version": 2, "completeness": "complete", "warnings": []},
        result,
        asyncio.Event(),
    )

    row = await orchestrator.db.fetch_one(
        "SELECT crawl_evidence FROM jobs WHERE job_id = ?",
        ("job-partial-discovery",),
    )
    assert row is not None
    evidence = orchestrator.db.loads_json(row["crawl_evidence"])
    assert evidence is not None
    assert evidence["completeness"] == "partial"
    assert evidence["warnings"] == [
        "Discovery ended before fixpoint: model_decision_failed"
    ]


@pytest.mark.asyncio
async def test_completion_loses_to_requested_cancellation(app) -> None:
    baseline = {"entries": [], "tree": {"children": {}, "pages": []}}
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at, baseline_sitemap)
        VALUES (?, 'processing', ?, datetime('now'), ?)
        """,
        (
            "job-cancel-race",
            "https://example.com",
            orchestrator.serialize_sitemap(baseline)[0],
        ),
    )
    cancel_event = asyncio.Event()
    cancel_event.set()

    assert (
        await orchestrator.complete_discovery_job(
            "job-cancel-race",
            {"entries": [], "tree": {"children": {}, "pages": []}},
            {},
            orchestrator.DiscoveryResult(
                outcome=orchestrator.DiscoveryOutcome.fixpoint,
                rounds=0,
                new_entry_count=0,
                state_count=0,
                workflow_count=0,
                stop_reason="complete_round_added_nothing",
            ),
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
async def test_baseline_checkpoint_finalizes_with_discovery_metadata(app) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at)
        VALUES (?, 'crawling', ?, datetime('now'))
        """,
        ("job-checkpoint", "https://example.com"),
    )
    baseline = {
        "entries": [{"method": "GET", "url": "https://example.com"}],
        "tree": {"children": {}, "pages": []},
    }
    evidence = {"schema_version": 1, "records": [{"lane": "standard", "count": 1}]}

    assert await orchestrator.persist_baseline_checkpoint("job-checkpoint", baseline, evidence)
    assert await orchestrator.transition_job_status(
        "job-checkpoint",
        {orchestrator.JobStatus.crawling},
        orchestrator.JobStatus.discovering,
    )
    result = orchestrator.DiscoveryResult(
        outcome=orchestrator.DiscoveryOutcome.budget_exhausted,
        rounds=1,
        new_entry_count=0,
        state_count=2,
        workflow_count=0,
        stop_reason="action_budget",
    )
    assert await orchestrator.finalize_baseline_checkpoint(
        "job-checkpoint",
        result,
        expected_statuses={orchestrator.JobStatus.discovering},
    )

    row = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-checkpoint",)
    )
    assert row is not None
    assert row["status"] == "completed"
    finalized_evidence = orchestrator.db.loads_json(row["crawl_evidence"])
    assert finalized_evidence is not None
    assert finalized_evidence["completeness"] == "partial"
    assert finalized_evidence["warnings"] == [
        "Discovery ended before fixpoint: action_budget"
    ]
    sitemap = orchestrator.db.loads_json(row["sitemap"])
    assert sitemap is not None
    assert sitemap["entries"] == baseline["entries"]
    assert sitemap["discovery"]["outcome"] == "budget_exhausted"
    persisted_result = orchestrator.db.loads_json(row["discovery_result"])
    assert persisted_result is not None
    assert persisted_result["stop_reason"] == "action_budget"


@pytest.mark.asyncio
async def test_discovery_checkpoint_preserves_baseline_and_partial_progress(app) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at)
        VALUES (?, 'crawling', ?, datetime('now'))
        """,
        ("job-discovery-checkpoint", "https://example.com"),
    )
    baseline = orchestrator.parser.build_sitemap([{"method": "GET", "url": "https://example.com"}])
    partial = orchestrator.parser.build_sitemap(
        [
            {"method": "GET", "url": "https://example.com"},
            {"method": "POST", "url": "https://example.com/api/generated"},
        ]
    )
    baseline_evidence = {"schema_version": 2, "phase": "baseline"}
    discovery_evidence = {"schema_version": 2, "phase": "discovery"}
    progress = {
        "rounds": 2,
        "new_entry_count": 1,
        "state_count": 4,
        "workflow_count": 1,
    }

    assert await orchestrator.persist_baseline_checkpoint(
        "job-discovery-checkpoint", baseline, baseline_evidence
    )
    assert await orchestrator.transition_job_status(
        "job-discovery-checkpoint",
        {orchestrator.JobStatus.crawling},
        orchestrator.JobStatus.discovering,
    )
    assert await orchestrator.persist_discovery_checkpoint(
        "job-discovery-checkpoint", partial, discovery_evidence, progress
    )

    row = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-discovery-checkpoint",)
    )
    assert row is not None
    assert orchestrator.db.loads_json(row["baseline_sitemap"]) == baseline
    assert orchestrator.db.loads_json(row["discovery_checkpoint_sitemap"]) == partial
    assert orchestrator.db.loads_json(row["discovery_checkpoint_progress"]) == progress
    assert orchestrator.db.loads_json(row["crawl_evidence"]) == discovery_evidence

    failure = orchestrator.DiscoveryResult(
        outcome=orchestrator.DiscoveryOutcome.partial_failure,
        rounds=0,
        new_entry_count=0,
        state_count=0,
        workflow_count=0,
        stop_reason="browser_discovery_failed_after_baseline",
    )
    assert await orchestrator.finalize_baseline_checkpoint(
        "job-discovery-checkpoint",
        failure,
        expected_statuses={orchestrator.JobStatus.discovering},
        error="enrichment failed",
    )

    row = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-discovery-checkpoint",)
    )
    assert row is not None
    sitemap = orchestrator.db.loads_json(row["sitemap"])
    assert sitemap is not None
    assert sitemap["entries"] == partial["entries"]
    assert sitemap["discovery"] == {
        "outcome": "partial_failure",
        **progress,
        "stop_reason": "browser_discovery_failed_after_baseline",
    }
    assert orchestrator.db.loads_json(row["discovery_result"]) == sitemap["discovery"]


@pytest.mark.asyncio
async def test_invalid_discovery_checkpoint_falls_back_to_baseline(app) -> None:
    baseline = orchestrator.parser.build_sitemap([{"method": "GET", "url": "https://example.com"}])
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (
            job_id, status, target_url, created_at, baseline_sitemap,
            discovery_checkpoint_sitemap, discovery_checkpoint_progress
        )
        VALUES (?, 'discovering', ?, datetime('now'), ?, ?, ?)
        """,
        (
            "job-corrupt-discovery-checkpoint",
            "https://example.com",
            orchestrator.db.dumps_json(baseline),
            "{not-json",
            orchestrator.db.dumps_json(
                {
                    "rounds": 2,
                    "new_entry_count": 3,
                    "state_count": 4,
                    "workflow_count": 1,
                }
            ),
        ),
    )
    result = orchestrator.DiscoveryResult(
        outcome=orchestrator.DiscoveryOutcome.interrupted,
        rounds=0,
        new_entry_count=0,
        state_count=0,
        workflow_count=0,
        stop_reason="server_restarted_after_discovery_checkpoint",
    )

    assert await orchestrator.finalize_baseline_checkpoint(
        "job-corrupt-discovery-checkpoint",
        result,
        expected_statuses={orchestrator.JobStatus.discovering},
    )

    row = await orchestrator.db.fetch_one(
        "SELECT sitemap, discovery_result FROM jobs WHERE job_id = ?",
        ("job-corrupt-discovery-checkpoint",),
    )
    assert row is not None
    sitemap = orchestrator.db.loads_json(row["sitemap"])
    assert sitemap is not None
    assert sitemap["entries"] == baseline["entries"]
    assert sitemap["discovery"]["new_entry_count"] == 0


@pytest.mark.asyncio
async def test_recovery_finalizes_valid_checkpoint_and_fails_pre_checkpoint_job(app) -> None:
    baseline = orchestrator.db.dumps_json({"entries": [], "tree": {"children": {}, "pages": []}})
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at, baseline_sitemap)
        VALUES (?, 'discovering', ?, datetime('now'), ?)
        """,
        ("job-recover", "https://example.com", baseline),
    )
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at)
        VALUES (?, 'crawling', ?, datetime('now'))
        """,
        ("job-no-checkpoint", "https://example.org"),
    )

    await orchestrator.recover_interrupted_jobs()

    recovered = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-recover",)
    )
    failed = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-no-checkpoint",)
    )
    assert recovered is not None
    assert recovered["status"] == "completed"
    recovered_sitemap = orchestrator.db.loads_json(recovered["sitemap"])
    assert recovered_sitemap is not None
    assert recovered_sitemap["discovery"] == {
        "outcome": "interrupted",
        "rounds": 0,
        "new_entry_count": 0,
        "state_count": 0,
        "workflow_count": 0,
        "stop_reason": "server_restarted_after_baseline_checkpoint",
    }
    assert failed is not None
    assert failed["status"] == "failed_interrupted"
    assert failed["finished_at"] is not None


@pytest.mark.asyncio
async def test_recovery_finalizes_latest_discovery_checkpoint(app) -> None:
    baseline = orchestrator.parser.build_sitemap([])
    partial = orchestrator.parser.build_sitemap(
        [{"method": "GET", "url": "https://example.com/from-browser"}]
    )
    progress = {
        "rounds": 1,
        "new_entry_count": 1,
        "state_count": 2,
        "workflow_count": 1,
    }
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (
            job_id, status, target_url, created_at, baseline_sitemap,
            discovery_checkpoint_sitemap, discovery_checkpoint_progress
        )
        VALUES (?, 'discovering', ?, datetime('now'), ?, ?, ?)
        """,
        (
            "job-recover-discovery",
            "https://example.com",
            orchestrator.db.dumps_json(baseline),
            orchestrator.db.dumps_json(partial),
            orchestrator.db.dumps_json(progress),
        ),
    )

    await orchestrator.recover_interrupted_jobs()

    row = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-recover-discovery",)
    )
    assert row is not None
    assert row["status"] == "completed"
    sitemap = orchestrator.db.loads_json(row["sitemap"])
    assert sitemap is not None
    assert sitemap["entries"] == partial["entries"]
    assert sitemap["discovery"] == {
        "outcome": "interrupted",
        **progress,
        "stop_reason": "server_restarted_after_discovery_checkpoint",
    }


@pytest.mark.asyncio
async def test_job_time_budget_finalizes_latest_discovery_checkpoint(
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = orchestrator.parser.build_sitemap([])
    partial = orchestrator.parser.build_sitemap(
        [{"method": "GET", "url": "https://example.com/from-browser"}]
    )
    progress = {
        "rounds": 1,
        "new_entry_count": 1,
        "state_count": 2,
        "workflow_count": 1,
    }
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (
            job_id, status, target_url, created_at, baseline_sitemap,
            discovery_checkpoint_sitemap, discovery_checkpoint_progress
        )
        VALUES (?, 'discovering', ?, datetime('now'), ?, ?, ?)
        """,
        (
            "job-time-budget-checkpoint",
            "https://example.com",
            orchestrator.db.dumps_json(baseline),
            orchestrator.db.dumps_json(partial),
            orchestrator.db.dumps_json(progress),
        ),
    )
    monkeypatch.setattr(orchestrator, "CRAWLER_JOB_TIMEOUT_SECONDS", 60.0)

    await orchestrator.finalize_job_time_budget("job-time-budget-checkpoint")

    row = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-time-budget-checkpoint",)
    )
    assert row is not None
    assert row["status"] == "completed"
    assert row["error"] == "Job exceeded its 60-second deadline"
    sitemap = orchestrator.db.loads_json(row["sitemap"])
    assert sitemap is not None
    assert sitemap["entries"] == partial["entries"]
    assert sitemap["discovery"] == {
        "outcome": "budget_exhausted",
        **progress,
        "stop_reason": "job_time_budget",
    }


@pytest.mark.asyncio
async def test_run_job_enforces_wall_clock_deadline_before_checkpoint(
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = False

    class HangingBrowserSession:
        def __init__(self, job_id: str) -> None:
            assert job_id == "job-time-budget-pre-checkpoint"

        async def start(self) -> None:
            await asyncio.Event().wait()

        async def stop(self) -> None:
            nonlocal stopped
            stopped = True

    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at)
        VALUES (?, 'queued', ?, datetime('now'))
        """,
        ("job-time-budget-pre-checkpoint", "https://example.com"),
    )
    monkeypatch.setattr(orchestrator.browser_session, "BrowserSession", HangingBrowserSession)
    monkeypatch.setattr(orchestrator, "CRAWLER_JOB_TIMEOUT_SECONDS", 0.1)

    await orchestrator.run_job("job-time-budget-pre-checkpoint", asyncio.Event())

    row = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-time-budget-pre-checkpoint",)
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == ("Job exceeded its 0.1-second deadline before a valid checkpoint")
    assert stopped is True


@pytest.mark.asyncio
async def test_partial_checkpoint_finalization_records_error_and_cannot_overwrite_cancel(
    app,
) -> None:
    baseline = orchestrator.db.dumps_json({"entries": [], "tree": {"children": {}, "pages": []}})
    for job_id in ("job-partial", "job-checkpoint-cancelled"):
        await orchestrator.db.execute(
            """
            INSERT INTO jobs (job_id, status, target_url, created_at, baseline_sitemap)
            VALUES (?, 'discovering', ?, datetime('now'), ?)
            """,
            (job_id, "https://example.com", baseline),
        )
    await orchestrator.update_job_status(
        "job-checkpoint-cancelled", orchestrator.JobStatus.cancelled
    )
    result = orchestrator.DiscoveryResult(
        outcome=orchestrator.DiscoveryOutcome.partial_failure,
        rounds=1,
        new_entry_count=2,
        state_count=3,
        workflow_count=1,
        stop_reason="katana_enrichment_failed",
    )

    assert await orchestrator.finalize_baseline_checkpoint(
        "job-partial",
        result,
        expected_statuses={orchestrator.JobStatus.discovering},
        error="enrichment failed after checkpoint",
    )
    assert not await orchestrator.finalize_baseline_checkpoint(
        "job-checkpoint-cancelled",
        result,
        expected_statuses={orchestrator.JobStatus.discovering},
    )

    partial = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-partial",)
    )
    cancelled = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-checkpoint-cancelled",)
    )
    assert partial is not None
    assert partial["status"] == "completed"
    assert partial["error"] == "enrichment failed after checkpoint"
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["sitemap"] is None


@pytest.mark.asyncio
async def test_operator_cancellation_publishes_latest_discovery_checkpoint(app) -> None:
    baseline = {
        "entries": [{"method": "GET", "url": "https://example.com"}],
        "tree": {"children": {}, "pages": []},
    }
    partial = {
        "entries": [
            {"method": "GET", "url": "https://example.com"},
            {"method": "GET", "url": "https://example.com/revealed"},
        ],
        "tree": {"children": {}, "pages": []},
    }
    progress = {
        "rounds": 1,
        "new_entry_count": 1,
        "state_count": 2,
        "workflow_count": 1,
    }
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (
            job_id, status, target_url, created_at, baseline_sitemap,
            discovery_checkpoint_sitemap, discovery_checkpoint_progress, crawl_evidence
        )
        VALUES (?, 'discovering', ?, datetime('now'), ?, ?, ?, ?)
        """,
        (
            "job-cancel-checkpoint",
            "https://example.com",
            orchestrator.db.dumps_json(baseline),
            orchestrator.db.dumps_json(partial),
            orchestrator.db.dumps_json(progress),
            orchestrator.db.dumps_json({"schema_version": 2, "rounds": [1]}),
        ),
    )

    await orchestrator.finalize_operator_cancellation("job-cancel-checkpoint")

    row = await orchestrator.db.fetch_one(
        "SELECT * FROM jobs WHERE job_id = ?", ("job-cancel-checkpoint",)
    )
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["finished_at"] is not None
    assert row["result_entry_count"] == 2
    sitemap = orchestrator.db.loads_json(row["sitemap"])
    assert sitemap is not None
    assert sitemap["entries"] == partial["entries"]
    assert sitemap["discovery"] == {
        "outcome": "interrupted",
        "rounds": 1,
        "new_entry_count": 1,
        "state_count": 2,
        "workflow_count": 1,
        "stop_reason": "cancelled_after_checkpoint",
    }
    assert orchestrator.db.loads_json(row["discovery_result"]) == sitemap["discovery"]


@pytest.mark.asyncio
async def test_operator_cancellation_before_checkpoint_has_no_sitemap(app) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at)
        VALUES (?, 'crawling', ?, datetime('now'))
        """,
        ("job-cancel-pre-checkpoint", "https://example.com"),
    )

    await orchestrator.finalize_operator_cancellation("job-cancel-pre-checkpoint")

    row = await orchestrator.db.fetch_one(
        "SELECT status, sitemap, discovery_result FROM jobs WHERE job_id = ?",
        ("job-cancel-pre-checkpoint",),
    )
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["sitemap"] is None
    assert row["discovery_result"] is None


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
async def test_run_job_persists_dual_pass_baseline_checkpoint(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    await orchestrator.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, discovery_config, created_at)
        VALUES (?, 'queued', ?, ?, datetime('now'))
        """,
        (
            "job-run",
            "https://example.com",
            '{"enabled":false,"max_rounds":3,"max_actions":100,"max_llm_pages":25}',
        ),
    )

    lifecycle: list[str] = []

    class FakePage:
        async def goto(self, url: str, *, wait_until: str):
            assert url == "https://example.com"
            assert wait_until == "domcontentloaded"
            lifecycle.append("post-page-navigated")

        async def close(self) -> None:
            lifecycle.append("post-page-closed")

    class FakeContext:
        async def new_page(self) -> FakePage:
            lifecycle.append("post-page-opened")
            return FakePage()

    class FakeBrowserSession:
        def __init__(self, job_id: str) -> None:
            assert job_id == "job-run"
            self.observer = None
            self.memory_budget = None

        async def start(self) -> None:
            lifecycle.append("started")

        async def connect_playwright(self, **kwargs) -> FakeContext:
            lifecycle.append(f"playwright:{kwargs['epoch']}")
            return FakeContext()

        async def guard(self, awaitable):
            return await awaitable

        async def configure_auth(self, **kwargs) -> None:
            lifecycle.append("auth-configured")

        async def begin_katana_epoch(self, epoch: str) -> str:
            lifecycle.append("playwright-disconnected")
            lifecycle.append(f"katana:{epoch}")
            return "ws://127.0.0.1:9222/devtools/browser/job-run"

        async def close_leftover_page_targets(self) -> None:
            lifecycle.append("targets-closed")

        async def disconnect_playwright(self) -> None:
            lifecycle.append("playwright-disconnected")

        async def stop(self) -> None:
            lifecycle.append("stopped")

    async def discover_known_files(*args, **kwargs):
        assert args == ("https://example.com",)
        assert kwargs["headers"] is None
        if kwargs.get("existing_result") is not None:
            assert kwargs["origins"] == ["https://example.com"]
            return kwargs["existing_result"]
        lifecycle.append("known-files")
        return orchestrator.known_files.KnownFileResult(
            seeds=["https://example.com/from-sitemap"],
            documents=[],
            diagnostics=[],
            attempts=2,
            origins=["https://example.com"],
        )

    async def run_crawl(config, **kwargs):
        lane = kwargs["lane"]
        assert kwargs["output_path"].endswith(f"/job-run.{lane}.jsonl")
        if lane == "standard":
            assert config.cdp_url is None
            assert config.extra_seed_urls == ["https://example.com/from-sitemap"]
        else:
            assert config.cdp_url is not None
            assert config.cdp_url.endswith("/job-run")
            assert config.extra_seed_urls == ["https://example.com/from-sitemap"]
            row = await orchestrator.db.fetch_one(
                "SELECT baseline_sitemap FROM jobs WHERE job_id = ?",
                ("job-run",),
            )
            assert row is not None and row["baseline_sitemap"] is not None
            lifecycle.append("standard-checkpoint-observed")
        lifecycle.append(f"crawl:{lane}")
        return orchestrator.crawler.KatanaRunResult(
            lane=lane,
            terminal_summary={
                "schema_version": 1,
                "status": "completed",
                "inputs": [{"input": config.target_url, "reason": "queue_exhausted"}],
            },
        )

    monkeypatch.setattr(orchestrator.browser_session, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(orchestrator.known_files, "discover_known_files", discover_known_files)
    monkeypatch.setattr(orchestrator.crawler, "run_crawl", run_crawl)
    monkeypatch.setattr(orchestrator, "sanitize_log_file", lambda path: None)
    calls: list[str] = []
    sitemap = orchestrator.parser.build_sitemap(
        [
            {
                "method": "GET",
                "url": "https://example.com/",
                "status": 200,
                "content_type": "text/html",
                "timestamp": None,
            }
        ]
    )

    def parse_lane(*args, **kwargs):
        path = str(args[0])
        calls.append(path)
        assert path.endswith(("/job-run.standard.jsonl", "/job-run.pure-headless.jsonl"))
        assert args[1] == "https://example.com"
        assert kwargs == {}
        return sitemap

    monkeypatch.setattr(orchestrator.parser, "parse_katana_log", parse_lane)
    monkeypatch.setattr(orchestrator.parser, "parse_katana_evidence", lambda *a, **kw: [])

    await orchestrator.run_job("job-run", asyncio.Event())

    row = await orchestrator.db.fetch_one("SELECT * FROM jobs WHERE job_id = ?", ("job-run",))
    assert row is not None
    assert row["status"] == "completed"
    completed_sitemap = orchestrator.db.loads_json(row["sitemap"])
    assert completed_sitemap is not None
    assert completed_sitemap["entries"] == sitemap["entries"]
    assert completed_sitemap["discovery"]["outcome"] == "disabled"
    assert len(calls) == 2
    assert orchestrator.db.loads_json(row["baseline_sitemap"]) == sitemap
    crawl_evidence = orchestrator.db.loads_json(row["crawl_evidence"])
    assert crawl_evidence is not None
    assert crawl_evidence["schema_version"] == 2
    assert lifecycle == [
        "started",
        "playwright:bootstrap",
        "auth-configured",
        "known-files",
        "playwright-disconnected",
        "katana:katana-standard-baseline",
        "crawl:standard",
        "playwright-disconnected",
        "katana:katana-pure-headless-baseline",
        "standard-checkpoint-observed",
        "crawl:pure-headless",
        "targets-closed",
        "playwright:post-katana",
        "post-page-opened",
        "post-page-navigated",
        "post-page-closed",
        "playwright-disconnected",
        "stopped",
    ]


@pytest.mark.asyncio
async def test_browser_guided_discovery_enriches_new_get_seeds_to_fixpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle: list[str] = []

    class FakeBrowserSession:
        observer = None
        memory_budget = None

        async def connect_playwright(self, *, epoch: str):
            lifecycle.append(f"playwright:{epoch}")
            return object()

        async def disconnect_playwright(self) -> None:
            lifecycle.append("playwright-disconnected")

        async def guard(self, awaitable):
            return await awaitable

        async def begin_katana_epoch(self, epoch: str) -> str:
            lifecycle.append(f"katana:{epoch}")
            return "ws://127.0.0.1:9222/devtools/browser/job"

        async def close_leftover_page_targets(self) -> None:
            lifecycle.append("targets-closed")

    rounds = [
        orchestrator.browser_discovery.DiscoveryRoundResult(
            states=[{"fingerprint": "state-1"}],
            actions=[{"kind": "click"}],
            stable_get_seeds=[
                "https://example.com/api/runtime",
                "https://example.com/api/runtime",
            ],
            processed_pages=1,
            action_count=1,
            state_count=1,
        ),
        orchestrator.browser_discovery.DiscoveryRoundResult(),
    ]

    async def run_round(**kwargs):
        assert kwargs["max_actions"] in {99, 100}
        return rounds.pop(0)

    crawl_configs = []

    async def run_crawl(config, **kwargs):
        crawl_configs.append((config, kwargs["lane"]))
        return orchestrator.crawler.KatanaRunResult(
            lane=kwargs["lane"],
            terminal_summary={
                "schema_version": 1,
                "status": "completed",
                "inputs": [{"input": config.target_url, "reason": "queue_exhausted"}],
            },
        )

    runtime_sitemap = orchestrator.parser.build_sitemap(
        [
            {
                "method": "GET",
                "url": "https://example.com/api/runtime",
                "status": 200,
                "content_type": "application/json",
                "timestamp": None,
            }
        ]
    )
    monkeypatch.setattr(orchestrator.browser_discovery, "run_discovery_round", run_round)
    monkeypatch.setattr(orchestrator.crawler, "run_crawl", run_crawl)
    monkeypatch.setattr(orchestrator.parser, "parse_katana_log", lambda *a, **kw: runtime_sitemap)
    monkeypatch.setattr(orchestrator.parser, "parse_katana_evidence", lambda *a, **kw: [])

    baseline = orchestrator.parser.build_sitemap([])
    log_paths: list[str] = []
    execution = await orchestrator.run_browser_guided_discovery(
        job_id="job",
        target_url="https://example.com",
        scope_config=None,
        auth_context=orchestrator.CrawlAuthContext(headers=[]),
        discovery_config=orchestrator.DiscoveryConfig(),
        job_browser=cast(orchestrator.browser_session.BrowserSession, FakeBrowserSession()),
        known_file_result=orchestrator.known_files.KnownFileResult(
            seeds=[],
            documents=[],
            diagnostics=[],
            attempts=0,
            origins=["https://example.com"],
        ),
        baseline_sitemap=baseline,
        baseline_evidence={"schema_version": 2},
        katana_sitemaps=[baseline],
        katana_records=[],
        cancel_event=asyncio.Event(),
        katana_log_paths=log_paths,
    )

    assert execution.result.outcome == orchestrator.DiscoveryOutcome.fixpoint
    assert execution.result.rounds == 2
    assert execution.result.new_entry_count == 1
    assert [entry["url"] for entry in execution.sitemap["entries"]] == [
        "https://example.com/api/runtime"
    ]
    assert len(crawl_configs) == 2
    assert crawl_configs[0][0].extra_seed_urls == [
        "https://example.com/api/runtime",
        "https://example.com/api/runtime",
    ]
    assert len(log_paths) == 2
    assert lifecycle == [
        "playwright:browser-discovery-1",
        "playwright-disconnected",
        "katana:katana-standard-discovery-1",
        "katana:katana-pure-headless-discovery-1",
        "targets-closed",
        "playwright:browser-discovery-2",
        "playwright-disconnected",
    ]


def test_baseline_evidence_marks_timed_out_lane_partial() -> None:
    standard = orchestrator.crawler.KatanaRunResult(
        lane="standard",
        terminal_summary={
            "schema_version": 1,
            "status": "completed",
            "inputs": [{"input": "https://example.com", "reason": "queue_exhausted"}],
        },
    )
    pure_headless = orchestrator.crawler.KatanaRunResult(
        lane="pure-headless",
        terminal_summary={
            "schema_version": 1,
            "status": "completed",
            "inputs": [{"input": "https://example.com", "reason": "crawl_timeout"}],
        },
        outcome="partial",
    )

    evidence = orchestrator.build_baseline_evidence(
        known_file_result=orchestrator.known_files.KnownFileResult(
            seeds=[], documents=[], diagnostics=[], attempts=0
        ),
        standard_run=standard,
        standard_records=[],
        pure_headless_run=pure_headless,
        pure_headless_records=[],
        browser_evidence={"schema_version": 1},
    )

    assert evidence["completeness"] == "partial"
    assert evidence["katana"]["pure_headless"]["outcome"] == "partial"
    assert evidence["warnings"] == [
        "Katana pure-headless baseline ended with crawl_timeout for https://example.com"
    ]


def test_baseline_evidence_marks_process_deadline_partial() -> None:
    standard = orchestrator.crawler.KatanaRunResult(
        lane="standard",
        terminal_summary={
            "schema_version": 1,
            "status": "completed",
            "inputs": [{"input": "https://example.com", "reason": "queue_exhausted"}],
        },
    )
    pure_headless = orchestrator.crawler.KatanaRunResult(
        lane="pure-headless",
        terminal_summary=None,
        outcome="partial",
        termination_reason="process_deadline",
    )

    evidence = orchestrator.build_baseline_evidence(
        known_file_result=orchestrator.known_files.KnownFileResult(
            seeds=[], documents=[], diagnostics=[], attempts=0
        ),
        standard_run=standard,
        standard_records=[],
        pure_headless_run=pure_headless,
        pure_headless_records=[{"url": "https://example.com/partial"}],
        browser_evidence={"schema_version": 1},
    )

    assert evidence["completeness"] == "partial"
    assert evidence["katana"]["pure_headless"] == {
        "lane": "pure-headless",
        "outcome": "partial",
        "terminal_summary": None,
        "termination_reason": "process_deadline",
        "records": [{"url": "https://example.com/partial"}],
    }
    assert evidence["warnings"] == [
        "Katana pure-headless baseline ended with process_deadline"
    ]


@pytest.mark.asyncio
async def test_discovery_does_not_claim_fixpoint_after_partial_katana(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBrowserSession:
        observer = None

        async def connect_playwright(self, *, epoch: str):
            assert epoch == "browser-discovery-1"
            return object()

        async def disconnect_playwright(self) -> None:
            return None

        async def guard(self, awaitable):
            return await awaitable

    async def empty_round(**_kwargs):
        return orchestrator.browser_discovery.DiscoveryRoundResult()

    monkeypatch.setattr(
        orchestrator,
        "build_discovery_adapter",
        orchestrator.browser_discovery.NullDiscoveryAdapter,
    )
    monkeypatch.setattr(orchestrator.browser_discovery, "run_discovery_round", empty_round)
    baseline = orchestrator.parser.build_sitemap([])

    execution = await orchestrator.run_browser_guided_discovery(
        job_id="job-partial-katana",
        target_url="https://example.com",
        scope_config=None,
        auth_context=orchestrator.CrawlAuthContext(headers=[]),
        discovery_config=orchestrator.DiscoveryConfig(),
        job_browser=cast(
            orchestrator.browser_session.BrowserSession,
            FakeBrowserSession(),
        ),
        known_file_result=orchestrator.known_files.KnownFileResult(
            seeds=[], documents=[], diagnostics=[], attempts=0
        ),
        baseline_sitemap=baseline,
        baseline_evidence={
            "schema_version": 2,
            "completeness": "partial",
            "warnings": ["Katana pure-headless baseline reached crawl_timeout"],
        },
        katana_sitemaps=[baseline],
        katana_records=[],
        cancel_event=asyncio.Event(),
        katana_log_paths=[],
        katana_partial=True,
    )

    assert execution.result.outcome == orchestrator.DiscoveryOutcome.budget_exhausted
    assert execution.result.stop_reason == "katana_crawl_budget"
    assert execution.evidence["completeness"] == "partial"


@pytest.mark.asyncio
async def test_browser_guided_discovery_checkpoints_before_enrichment_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_url = "https://example.com/api/runtime"
    observed_network = SimpleNamespace(
        requests=[
            cdp_observer.NetworkRequest(
                method="GET",
                url=runtime_url,
                session_id="session",
                target_type="page",
                target_url="https://example.com",
                request_id="request",
                frame_id="frame",
                loader_id="loader",
                resource_type="Fetch",
                initiator_type="script",
                initiator_url="https://example.com/app.js",
                epoch="browser-discovery-1",
                observed_at="2026-07-23T00:00:00+00:00",
            )
        ],
        responses=[],
    )

    class FakeBrowserSession:
        observer = observed_network
        memory_budget = None

        async def connect_playwright(self, *, epoch: str):
            assert epoch == "browser-discovery-1"
            return object()

        async def disconnect_playwright(self) -> None:
            return None

        async def guard(self, awaitable):
            return await awaitable

        async def begin_katana_epoch(self, epoch: str) -> str:
            assert epoch == "katana-standard-discovery-1"
            return "ws://127.0.0.1:9222/devtools/browser/job"

    async def run_round(**kwargs):
        return orchestrator.browser_discovery.DiscoveryRoundResult(
            states=[{"fingerprint": "state-1"}],
            actions=[{"kind": "click"}],
            stable_get_seeds=[runtime_url],
            processed_pages=1,
            action_count=1,
            state_count=1,
        )

    async def fail_enrichment(*args, **kwargs):
        raise RuntimeError("enrichment failed")

    checkpoints: list[tuple[dict, dict, dict]] = []

    async def checkpoint_writer(job_id, sitemap, evidence, progress):
        assert job_id == "job-partial-enrichment"
        checkpoints.append((sitemap, evidence, progress))
        return True

    monkeypatch.setattr(
        orchestrator,
        "build_discovery_adapter",
        orchestrator.browser_discovery.NullDiscoveryAdapter,
    )
    monkeypatch.setattr(orchestrator.browser_discovery, "run_discovery_round", run_round)
    monkeypatch.setattr(orchestrator.crawler, "run_crawl", fail_enrichment)
    baseline = orchestrator.parser.build_sitemap([])

    with pytest.raises(RuntimeError, match="enrichment failed"):
        await orchestrator.run_browser_guided_discovery(
            job_id="job-partial-enrichment",
            target_url="https://example.com",
            scope_config=None,
            auth_context=orchestrator.CrawlAuthContext(headers=[]),
            discovery_config=orchestrator.DiscoveryConfig(),
            job_browser=cast(
                orchestrator.browser_session.BrowserSession,
                FakeBrowserSession(),
            ),
            known_file_result=orchestrator.known_files.KnownFileResult(
                seeds=[], documents=[], diagnostics=[], attempts=0
            ),
            baseline_sitemap=baseline,
            baseline_evidence={"schema_version": 2},
            katana_sitemaps=[baseline],
            katana_records=[],
            cancel_event=asyncio.Event(),
            katana_log_paths=[],
            checkpoint_writer=checkpoint_writer,
        )

    assert len(checkpoints) == 1
    sitemap, evidence, progress = checkpoints[0]
    assert [(entry["method"], entry["url"]) for entry in sitemap["entries"]] == [
        ("GET", runtime_url)
    ]
    assert evidence["discovery"]["rounds"][0]["new_entry_count"] == 1
    assert progress == {
        "rounds": 1,
        "new_entry_count": 1,
        "state_count": 1,
        "workflow_count": 0,
    }


@pytest.mark.asyncio
async def test_browser_guided_discovery_stops_at_server_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBrowserSession:
        observer = None

        async def connect_playwright(self, *, epoch: str):
            return object()

        async def disconnect_playwright(self) -> None:
            return None

        async def guard(self, awaitable):
            return await awaitable

    async def hanging_round(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        orchestrator,
        "build_discovery_adapter",
        orchestrator.browser_discovery.NullDiscoveryAdapter,
    )
    monkeypatch.setattr(orchestrator, "CRAWLER_DISCOVERY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(orchestrator.browser_discovery, "run_discovery_round", hanging_round)
    baseline = orchestrator.parser.build_sitemap([])

    execution = await orchestrator.run_browser_guided_discovery(
        job_id="job-timeout",
        target_url="https://example.com",
        scope_config=None,
        auth_context=orchestrator.CrawlAuthContext(headers=[]),
        discovery_config=orchestrator.DiscoveryConfig(),
        job_browser=cast(orchestrator.browser_session.BrowserSession, FakeBrowserSession()),
        known_file_result=orchestrator.known_files.KnownFileResult(
            seeds=[], documents=[], diagnostics=[], attempts=0
        ),
        baseline_sitemap=baseline,
        baseline_evidence={"schema_version": 2},
        katana_sitemaps=[baseline],
        katana_records=[],
        cancel_event=asyncio.Event(),
        katana_log_paths=[],
    )

    assert execution.result.outcome == orchestrator.DiscoveryOutcome.budget_exhausted
    assert execution.result.rounds == 0
    assert execution.result.stop_reason == "time_budget"
