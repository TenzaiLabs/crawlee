from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from . import (
    auth_agent,
    browser_discovery,
    browser_session,
    crawler,
    db,
    discovery_model,
    job_persistence,
    known_files,
    parser,
)
from .common import sanitize_log_value
from .job_status import ACTIVE_JOB_STATUSES, INTERRUPTED_JOB_STATUSES, TERMINAL_JOB_STATUSES
from .log_records import sanitize_log_file
from .models import DiscoveryConfig, DiscoveryOutcome, DiscoveryResult, JobStatus
from .process import ProcessMemoryLimitExceeded
from .settings import (
    CRAWLER_DISCOVERY_MAX_STATES,
    CRAWLER_DISCOVERY_TIMEOUT_SECONDS,
    CRAWLER_JOB_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

_job_tasks: dict[str, asyncio.Task[None]] = {}
_cancel_events: dict[str, asyncio.Event] = {}
_state_locks: dict[str, asyncio.Lock] = {}
_queue: asyncio.Queue[str] = asyncio.Queue()
_drainer_task: asyncio.Task[None] | None = None
_job_persistence = job_persistence.JobPersistence(_state_locks)


@dataclass(frozen=True)
class CrawlAuthContext:
    headers: list[str]
    cookies: list[dict[str, Any]] = field(default_factory=list)
    landing_url: str | None = None
    extra_seed_urls: list[str] = field(default_factory=list)
    discovered_urls: list[str] = field(default_factory=list)
    auth_blocked_url_count: int = 0


class _JobCancellationRequested(Exception):
    pass


@dataclass(frozen=True)
class BrowserGuidedDiscoveryExecution:
    sitemap: dict[str, Any]
    evidence: dict[str, Any]
    result: DiscoveryResult


@dataclass(frozen=True)
class KatanaLaneExecution:
    lane: crawler.KatanaLane
    run: crawler.KatanaRunResult
    sitemap: dict[str, Any]
    records: list[dict[str, Any]]
    known_file_result: known_files.KnownFileResult
    new_known_file_seeds: list[str]


@dataclass(frozen=True)
class BaselinePhaseExecution:
    sitemap: dict[str, Any]
    evidence: dict[str, Any]
    known_file_result: known_files.KnownFileResult
    katana_sitemaps: list[dict[str, Any]]
    katana_records: list[dict[str, Any]]
    pending_known_file_seeds: list[str]
    katana_partial: bool


DiscoveryCheckpointWriter = Callable[
    [str, dict[str, Any], dict[str, Any], dict[str, int]],
    Awaitable[bool],
]


def _normalize_auth_config(raw_auth_config: Any) -> dict[str, Any]:
    auth_config = raw_auth_config if isinstance(raw_auth_config, dict) else {}
    return dict(auth_config)


def _extract_manual_headers(auth_config: dict[str, Any]) -> list[str]:
    headers = auth_config.get("headers")
    if not isinstance(headers, list):
        return []
    resolved = auth_agent.resolve_secrets({"headers": headers})
    resolved_headers = resolved.get("headers")
    if not isinstance(resolved_headers, list):
        return []
    return [str(header) for header in resolved_headers]


def _merge_extra_seed_urls(
    *,
    target_url: str,
    landing_url: str | None,
    discovered_urls: list[str],
) -> list[str]:
    candidates: list[str] = []
    if landing_url:
        candidates.append(landing_url)
    candidates.extend(discovered_urls)
    del target_url
    return [url for candidate in candidates if (url := str(candidate).strip())]


def _raise_if_cancel_requested(cancel_event: asyncio.Event) -> None:
    if cancel_event.is_set():
        raise _JobCancellationRequested


async def _run_auth_if_needed(
    job_id: str,
    target_url: str,
    auth_config: dict[str, Any],
    base_headers: list[str],
    should_auth: bool,
    cancel_event: asyncio.Event,
    *,
    browser_context: Any | None = None,
) -> CrawlAuthContext:
    merged_headers = list(base_headers)
    if not should_auth:
        return CrawlAuthContext(headers=merged_headers)

    logger.info("Running authentication for job_id=%s", sanitize_log_value(job_id))
    if browser_context is None:
        raise RuntimeError("AI authentication requires the orchestrator-owned browser context")
    resolved_config = auth_agent.resolve_secrets(auth_config)
    auth_result = await auth_agent.authenticate(
        target_url,
        resolved_config,
        cancel_event,
        context=browser_context,
    )
    merged_headers.extend(auth_result.headers)
    auth_blocked_url_count = len(auth_result.blocked_urls)

    extra_seed_urls = _merge_extra_seed_urls(
        target_url=target_url,
        landing_url=auth_result.landing_url,
        discovered_urls=auth_result.discovered_urls,
    )
    if auth_result.discovered_urls:
        logger.info(
            "Auth discovered %d same-origin crawl seed URL(s)",
            len(auth_result.discovered_urls),
        )

    return CrawlAuthContext(
        headers=merged_headers,
        cookies=auth_result.cookies,
        landing_url=auth_result.landing_url,
        extra_seed_urls=extra_seed_urls,
        discovered_urls=auth_result.discovered_urls,
        auth_blocked_url_count=auth_blocked_url_count,
    )


async def has_active_job() -> bool:
    placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
    query = f"SELECT COUNT(1) as count FROM jobs WHERE status IN ({placeholders})"
    row = await db.fetch_one(query, tuple(ACTIVE_JOB_STATUSES))
    logger.debug("Checked for active jobs")
    return bool(row and row["count"])


async def update_job_status(job_id: str, status: JobStatus, error: str | None = None) -> None:
    await _job_persistence.update_status(job_id, status, error)


async def transition_job_status(
    job_id: str,
    from_statuses: set[JobStatus],
    to_status: JobStatus,
) -> bool:
    return await _job_persistence.transition(job_id, from_statuses, to_status)


async def cancel_queued_job(job_id: str) -> bool:
    return await _job_persistence.cancel_queued(job_id)


def build_generated_exclusions_payload(
    config: crawler.CrawlConfig,
    auth_context: CrawlAuthContext,
) -> dict[str, Any]:
    return {
        "auth_blocked_url_count": auth_context.auth_blocked_url_count,
        "auth_applied_blocked_url_count": 0,
        "auth_ignored_blocked_url_count": auth_context.auth_blocked_url_count,
        "auth_dynamic_patterns": [],
        "auth_discovered_url_count": len(auth_context.discovered_urls),
        "auth_discovered_urls": list(auth_context.discovered_urls),
        "extra_seed_urls": list(auth_context.extra_seed_urls),
        "effective_patterns": crawler.build_exclusion_patterns(config),
    }


async def update_job_generated_exclusions(job_id: str, exclusions: dict[str, Any]) -> None:
    logger.info("Persisting generated exclusions job_id=%s", sanitize_log_value(job_id))
    await db.execute(
        """
        UPDATE jobs
        SET generated_exclusions = ?
        WHERE job_id = ?
        """,
        (db.dumps_json(exclusions), job_id),
    )


def serialize_sitemap(sitemap: dict[str, Any]) -> tuple[str, int, int]:
    return job_persistence.serialize_sitemap(sitemap)


async def complete_discovery_job(
    job_id: str,
    sitemap: dict[str, Any],
    evidence: dict[str, Any],
    result: DiscoveryResult,
    cancel_event: asyncio.Event,
) -> bool:
    return await _job_persistence.complete(
        job_id,
        sitemap,
        evidence,
        result,
        cancel_event,
    )


async def persist_baseline_checkpoint(
    job_id: str,
    sitemap: dict[str, Any],
    evidence: dict[str, Any],
) -> bool:
    return await _job_persistence.publish_baseline(job_id, sitemap, evidence)


async def persist_discovery_checkpoint(
    job_id: str,
    sitemap: dict[str, Any],
    evidence: dict[str, Any],
    progress: dict[str, int],
) -> bool:
    return await _job_persistence.publish_discovery_checkpoint(
        job_id,
        sitemap,
        evidence,
        progress,
    )


def build_browser_evidence(
    job_browser: browser_session.BrowserSession,
) -> dict[str, Any]:
    observer = job_browser.observer
    if observer is None:
        return {"schema_version": 1, "requests": [], "responses": [], "diagnostics": []}
    return {
        "schema_version": 1,
        "requests": [asdict(request) for request in observer.requests],
        "responses": [asdict(response) for response in observer.responses],
        "diagnostics": list(getattr(observer, "diagnostics", ())),
    }


def build_baseline_evidence(
    *,
    known_file_result: known_files.KnownFileResult,
    standard_run: crawler.KatanaRunResult,
    standard_records: list[dict[str, Any]],
    pure_headless_run: crawler.KatanaRunResult | None,
    pure_headless_records: list[dict[str, Any]] | None,
    browser_evidence: dict[str, Any],
) -> dict[str, Any]:
    katana_evidence: dict[str, Any] = {
        "standard": {
            **standard_run.evidence(),
            "records": standard_records,
        },
    }
    runs = [standard_run]
    warnings = _katana_run_warnings(standard_run, phase="baseline")
    if pure_headless_run is None:
        warnings.append("Katana pure-headless baseline had not completed")
    else:
        runs.append(pure_headless_run)
        warnings.extend(_katana_run_warnings(pure_headless_run, phase="baseline"))
        katana_evidence["pure_headless"] = {
            **pure_headless_run.evidence(),
            "records": pure_headless_records or [],
        }
    completeness = (
        "complete"
        if pure_headless_run is not None and all(run.outcome == "complete" for run in runs)
        else "partial"
    )
    return {
        "schema_version": 2,
        "completeness": completeness,
        "warnings": warnings,
        "known_files": known_file_result.evidence(),
        "katana": katana_evidence,
        "browser": browser_evidence,
    }


def _katana_run_warnings(
    run: crawler.KatanaRunResult,
    *,
    phase: str,
) -> list[str]:
    warnings: list[str] = []
    if run.termination_reason is not None:
        warnings.append(f"Katana {run.lane} {phase} ended with {run.termination_reason}")
    if run.terminal_summary is None:
        return warnings
    inputs = run.terminal_summary.get("inputs")
    if not isinstance(inputs, list):
        return warnings
    for item in inputs:
        if not isinstance(item, dict) or item.get("reason") == "queue_exhausted":
            continue
        warnings.append(
            f"Katana {run.lane} {phase} ended with {item.get('reason', 'unknown')} "
            f"for {item.get('input', 'unknown input')}"
        )
    return warnings


def build_discovery_adapter() -> browser_discovery.DiscoveryAdapter:
    """Return the server-owned live model adapter."""

    return discovery_model.build_live_discovery_adapter()


def _entry_keys(sitemap: dict[str, Any]) -> set[tuple[str, str]]:
    parser.validate_sitemap(sitemap)
    return {
        (str(entry.get("method") or "").upper(), str(entry.get("url") or ""))
        for entry in sitemap["entries"]
    }


def _sitemap_origins(target_url: str, sitemap: dict[str, Any]) -> list[str]:
    parser.validate_sitemap(sitemap)
    urls = [
        str(entry["url"])
        for entry in sitemap["entries"]
        if isinstance(entry, dict) and isinstance(entry.get("url"), str)
    ]
    return known_files.in_scope_origins(target_url, urls)


async def run_katana_lane(
    *,
    target_url: str,
    scope_config: dict[str, Any] | None,
    auth_context: CrawlAuthContext,
    job_browser: browser_session.BrowserSession,
    known_file_result: known_files.KnownFileResult,
    lane: crawler.KatanaLane,
    epoch: str,
    evidence_lane: str,
    log_path: str,
    seed_urls: list[str],
    cancel_event: asyncio.Event,
) -> KatanaLaneExecution:
    """Run one explicit Katana lane and return its parsed, typed artifacts."""

    cdp_url = await job_browser.begin_katana_epoch(epoch)
    config = crawler.CrawlConfig(
        target_url=target_url,
        scope_config=scope_config,
        headers=auth_context.headers or None,
        extra_seed_urls=[*auth_context.extra_seed_urls, *seed_urls] or None,
        cdp_url=cdp_url if lane == "pure-headless" else None,
    )
    run = await job_browser.guard(
        crawler.run_crawl(
            config,
            lane=lane,
            cancel_event=cancel_event,
            output_path=log_path,
            memory_budget=job_browser.memory_budget,
        )
    )
    _raise_if_cancel_requested(cancel_event)
    sitemap = await asyncio.to_thread(parser.parse_katana_log, log_path, target_url)
    records = await asyncio.to_thread(
        parser.parse_katana_evidence,
        log_path,
        lane=evidence_lane,
        target_url=target_url,
    )
    known_seed_count = len(known_file_result.seeds)
    expanded_known_files = await job_browser.guard(
        known_files.discover_known_files(
            target_url,
            headers=auth_context.headers or None,
            origins=_sitemap_origins(target_url, sitemap),
            cancel_event=cancel_event,
            existing_result=known_file_result,
        )
    )
    _raise_if_cancel_requested(cancel_event)
    return KatanaLaneExecution(
        lane=lane,
        run=run,
        sitemap=sitemap,
        records=records,
        known_file_result=expanded_known_files,
        new_known_file_seeds=expanded_known_files.seeds[known_seed_count:],
    )


async def run_baseline_phase(
    *,
    job_id: str,
    target_url: str,
    scope_config: dict[str, Any] | None,
    auth_context: CrawlAuthContext,
    job_browser: browser_session.BrowserSession,
    cancel_event: asyncio.Event,
    standard_log_path: str,
    pure_headless_log_path: str,
) -> BaselinePhaseExecution:
    """Run known-file discovery and both baseline Katana lanes as one phase."""

    known_file_result = await job_browser.guard(
        known_files.discover_known_files(
            target_url,
            headers=auth_context.headers or None,
            cancel_event=cancel_event,
        )
    )
    _raise_if_cancel_requested(cancel_event)
    standard_config = crawler.CrawlConfig(
        target_url=target_url,
        scope_config=scope_config,
        headers=auth_context.headers or None,
        extra_seed_urls=[*auth_context.extra_seed_urls, *known_file_result.seeds] or None,
    )
    await update_job_generated_exclusions(
        job_id,
        build_generated_exclusions_payload(standard_config, auth_context),
    )
    standard = await run_katana_lane(
        target_url=target_url,
        scope_config=scope_config,
        auth_context=auth_context,
        job_browser=job_browser,
        known_file_result=known_file_result,
        lane="standard",
        epoch="katana-standard-baseline",
        evidence_lane="standard",
        log_path=standard_log_path,
        seed_urls=known_file_result.seeds,
        cancel_event=cancel_event,
    )
    standard_browser_evidence = build_browser_evidence(job_browser)
    standard_sitemap = parser.merge_baseline_sitemap(
        target_url=target_url,
        katana_sitemaps=[standard.sitemap],
        browser_evidence=standard_browser_evidence,
        known_file_evidence=standard.known_file_result.evidence(),
    )
    standard_evidence = build_baseline_evidence(
        known_file_result=standard.known_file_result,
        standard_run=standard.run,
        standard_records=standard.records,
        pure_headless_run=None,
        pure_headless_records=None,
        browser_evidence=standard_browser_evidence,
    )
    if standard_sitemap["entries"] and not await _job_persistence.publish_baseline(
        job_id,
        standard_sitemap,
        standard_evidence,
    ):
        raise RuntimeError("Job could not persist its standard-lane checkpoint")
    known_seed_count_before_pure = len(standard.known_file_result.seeds)
    pure_headless = await run_katana_lane(
        target_url=target_url,
        scope_config=scope_config,
        auth_context=auth_context,
        job_browser=job_browser,
        known_file_result=standard.known_file_result,
        lane="pure-headless",
        epoch="katana-pure-headless-baseline",
        evidence_lane="pure-headless",
        log_path=pure_headless_log_path,
        seed_urls=standard.known_file_result.seeds,
        cancel_event=cancel_event,
    )
    await job_browser.close_leftover_page_targets()
    post_katana_context = await job_browser.connect_playwright(epoch="post-katana")
    post_katana_page = await post_katana_context.new_page()
    await job_browser.guard(
        post_katana_page.goto(
            auth_context.landing_url or target_url,
            wait_until="domcontentloaded",
        )
    )
    await post_katana_page.close()
    await job_browser.disconnect_playwright()

    browser_evidence = build_browser_evidence(job_browser)
    katana_sitemaps = [standard.sitemap, pure_headless.sitemap]
    sitemap = parser.merge_baseline_sitemap(
        target_url=target_url,
        katana_sitemaps=katana_sitemaps,
        browser_evidence=browser_evidence,
        known_file_evidence=pure_headless.known_file_result.evidence(),
    )
    evidence = build_baseline_evidence(
        known_file_result=pure_headless.known_file_result,
        standard_run=standard.run,
        standard_records=standard.records,
        pure_headless_run=pure_headless.run,
        pure_headless_records=pure_headless.records,
        browser_evidence=browser_evidence,
    )
    if not sitemap["entries"] and (
        standard.run.outcome == "partial" or pure_headless.run.outcome == "partial"
    ):
        raise crawler.KatanaRunError("Katana baseline produced no usable sitemap entries")
    if not await _job_persistence.publish_baseline(job_id, sitemap, evidence):
        raise RuntimeError("Job could not persist its baseline checkpoint")
    if not await _job_persistence.transition(
        job_id,
        {JobStatus.crawling},
        JobStatus.discovering,
    ):
        raise RuntimeError("Job could not transition from crawling to discovering")
    return BaselinePhaseExecution(
        sitemap=sitemap,
        evidence=evidence,
        known_file_result=pure_headless.known_file_result,
        katana_sitemaps=katana_sitemaps,
        katana_records=[*standard.records, *pure_headless.records],
        pending_known_file_seeds=pure_headless.known_file_result.seeds[
            known_seed_count_before_pure:
        ],
        katana_partial=(
            standard.run.outcome == "partial" or pure_headless.run.outcome == "partial"
        ),
    )


async def run_browser_guided_discovery(
    *,
    job_id: str,
    target_url: str,
    scope_config: dict[str, Any] | None,
    auth_context: CrawlAuthContext,
    discovery_config: DiscoveryConfig,
    job_browser: browser_session.BrowserSession,
    known_file_result: known_files.KnownFileResult,
    baseline_sitemap: dict[str, Any],
    baseline_evidence: dict[str, Any],
    katana_sitemaps: list[dict[str, Any]],
    katana_records: list[dict[str, Any]],
    cancel_event: asyncio.Event,
    katana_log_paths: list[str],
    checkpoint_writer: DiscoveryCheckpointWriter | None = None,
    pending_known_file_seeds: list[str] | None = None,
    katana_partial: bool = False,
) -> BrowserGuidedDiscoveryExecution:
    adapter = build_discovery_adapter()
    processed_states: set[tuple[str, str]] = set()
    current_sitemap = baseline_sitemap
    baseline_keys = _entry_keys(baseline_sitemap)
    discovery_evidence: dict[str, Any] = {"schema_version": 1, "rounds": []}
    enrichment_evidence: list[dict[str, Any]] = []
    total_actions = 0
    total_llm_pages = 0
    total_states = 0
    total_workflows = 0
    completed_rounds = 0
    outcome = DiscoveryOutcome.budget_exhausted
    stop_reason = "max_rounds"
    deadline = asyncio.get_running_loop().time() + CRAWLER_DISCOVERY_TIMEOUT_SECONDS
    queued_known_file_seeds = list(pending_known_file_seeds or [])
    katana_budget_exhausted = katana_partial
    crawl_warnings = list(baseline_evidence.get("warnings", []))

    def checkpoint_evidence() -> dict[str, Any]:
        current = dict(baseline_evidence)
        current["completeness"] = "partial" if katana_budget_exhausted else "complete"
        current["warnings"] = list(crawl_warnings)
        current["browser"] = build_browser_evidence(job_browser)
        current["discovery"] = discovery_evidence
        current["katana_enrichment"] = enrichment_evidence
        return current

    async def persist_current_checkpoint() -> None:
        if checkpoint_writer is None:
            return
        progress = {
            "rounds": completed_rounds,
            "new_entry_count": len(_entry_keys(current_sitemap) - baseline_keys),
            "state_count": total_states,
            "workflow_count": total_workflows,
        }
        if not await checkpoint_writer(
            job_id,
            current_sitemap,
            checkpoint_evidence(),
            progress,
        ):
            raise RuntimeError("Job could not persist its discovery checkpoint")

    for round_number in range(1, discovery_config.max_rounds + 1):
        _raise_if_cancel_requested(cancel_event)
        remaining_actions = discovery_config.max_actions - total_actions
        remaining_llm_pages = discovery_config.max_llm_pages - total_llm_pages
        remaining_states = CRAWLER_DISCOVERY_MAX_STATES - total_states
        if remaining_actions <= 0:
            stop_reason = "action_budget"
            break
        if remaining_states <= 0:
            stop_reason = "state_budget"
            break
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            stop_reason = "time_budget"
            break

        candidates = browser_discovery.select_candidates(
            target_url=target_url,
            landing_url=auth_context.landing_url,
            katana_records=katana_records,
        )
        set_discovery_context = getattr(adapter, "set_discovery_context", None)
        if callable(set_discovery_context):
            set_discovery_context(
                known_endpoints=[
                    f"{method} {url}" for method, url in sorted(_entry_keys(current_sitemap))
                ],
                remaining_budgets={
                    "actions": remaining_actions,
                    "llm_pages": max(0, remaining_llm_pages),
                    "states": remaining_states,
                    "seconds": max(0, round(remaining_seconds, 3)),
                },
            )
        context = await job_browser.connect_playwright(epoch=f"browser-discovery-{round_number}")
        try:
            try:
                async with asyncio.timeout(remaining_seconds):
                    round_result = await job_browser.guard(
                        browser_discovery.run_discovery_round(
                            context=context,
                            target_url=target_url,
                            candidates=candidates,
                            observer=job_browser.observer,
                            adapter=adapter,
                            cancel_event=cancel_event,
                            max_actions=remaining_actions,
                            max_llm_pages=max(0, remaining_llm_pages),
                            max_states=remaining_states,
                            processed_states=processed_states,
                            known_get_urls={
                                url
                                for method, url in _entry_keys(current_sitemap)
                                if method == "GET"
                            },
                        )
                    )
            except TimeoutError:
                stop_reason = "time_budget"
                break
        finally:
            await job_browser.disconnect_playwright()

        completed_rounds += 1
        total_actions += round_result.action_count
        total_llm_pages += round_result.llm_page_count
        total_states += round_result.state_count
        total_workflows += round_result.workflow_count
        before_round_keys = _entry_keys(current_sitemap)
        browser_evidence = build_browser_evidence(job_browser)
        current_sitemap = parser.merge_baseline_sitemap(
            target_url=target_url,
            katana_sitemaps=katana_sitemaps,
            browser_evidence=browser_evidence,
            known_file_evidence=known_file_result.evidence(),
        )
        current_keys = _entry_keys(current_sitemap)
        new_seed_urls = [*queued_known_file_seeds, *round_result.stable_get_seeds]
        queued_known_file_seeds.clear()

        round_record: dict[str, Any] = {
            "round": round_number,
            "candidate_count": round_result.candidate_count,
            "processed_page_count": round_result.processed_pages,
            "llm_page_count": round_result.llm_page_count,
            "state_count": round_result.state_count,
            "action_count": round_result.action_count,
            "workflow_count": round_result.workflow_count,
            "states": round_result.states,
            "actions": round_result.actions,
            "diagnostics": round_result.diagnostics,
            "stable_get_seeds": list(round_result.stable_get_seeds),
            "new_seed_count": len(new_seed_urls),
            "model_budget_exhausted": round_result.model_budget_exhausted,
            "model_failure_count": round_result.model_failure_count,
            "requests": round_result.request_evidence,
            "responses": round_result.response_evidence,
        }
        round_record["new_entry_count"] = len(current_keys - before_round_keys)
        discovery_evidence["rounds"].append(round_record)
        await persist_current_checkpoint()

        if new_seed_urls:
            lane_runs: dict[str, Any] = {}
            enrichment_round = {"round": round_number, "lanes": lane_runs}
            enrichment_evidence.append(enrichment_round)
            enrichment_seed_urls = list(new_seed_urls)
            for lane in ("standard", "pure-headless"):
                log_path = os.path.join(
                    db.LOG_DIR,
                    f"{job_id}.discovery-{round_number}.{lane}.jsonl",
                )
                katana_log_paths.append(log_path)
                lane_execution = await run_katana_lane(
                    target_url=target_url,
                    scope_config=scope_config,
                    auth_context=auth_context,
                    job_browser=job_browser,
                    known_file_result=known_file_result,
                    lane=lane,
                    epoch=f"katana-{lane}-discovery-{round_number}",
                    evidence_lane=f"{lane}-discovery-{round_number}",
                    log_path=log_path,
                    seed_urls=enrichment_seed_urls,
                    cancel_event=cancel_event,
                )
                katana_sitemaps.append(lane_execution.sitemap)
                katana_records.extend(lane_execution.records)
                lane_runs[lane] = {
                    **lane_execution.run.evidence(),
                    "records": lane_execution.records,
                }
                lane_warnings = _katana_run_warnings(
                    lane_execution.run,
                    phase=f"discovery round {round_number}",
                )
                if lane_warnings:
                    katana_budget_exhausted = True
                    crawl_warnings.extend(lane_warnings)
                known_file_result = lane_execution.known_file_result
                enrichment_seed_urls.extend(lane_execution.new_known_file_seeds)
                queued_known_file_seeds.extend(lane_execution.new_known_file_seeds)
                browser_evidence = build_browser_evidence(job_browser)
                current_sitemap = parser.merge_baseline_sitemap(
                    target_url=target_url,
                    katana_sitemaps=katana_sitemaps,
                    browser_evidence=browser_evidence,
                    known_file_evidence=known_file_result.evidence(),
                )
                current_keys = _entry_keys(current_sitemap)
                round_record["new_entry_count"] = len(current_keys - before_round_keys)
                await persist_current_checkpoint()
            await job_browser.close_leftover_page_targets()
            browser_evidence = build_browser_evidence(job_browser)
            current_sitemap = parser.merge_baseline_sitemap(
                target_url=target_url,
                katana_sitemaps=katana_sitemaps,
                browser_evidence=browser_evidence,
                known_file_evidence=known_file_result.evidence(),
            )
            current_keys = _entry_keys(current_sitemap)

        round_record["new_entry_count"] = len(current_keys - before_round_keys)
        await persist_current_checkpoint()
        if round_result.model_failure_count:
            outcome = DiscoveryOutcome.partial_failure
            stop_reason = "model_decision_failed"
            break
        if round_result.model_budget_exhausted:
            stop_reason = "model_turn_budget"
            break
        if round_result.llm_page_budget_exhausted:
            stop_reason = "llm_page_budget"
            break
        if (
            round_result.state_count == 0
            and round_result.action_count == 0
            and not (current_keys - before_round_keys)
        ):
            outcome = DiscoveryOutcome.fixpoint
            stop_reason = "complete_round_added_nothing"
            break
        if round_result.budget_exhausted:
            if total_actions >= discovery_config.max_actions:
                stop_reason = "action_budget"
            elif total_states >= CRAWLER_DISCOVERY_MAX_STATES:
                stop_reason = "state_budget"
            else:
                stop_reason = "llm_page_budget"
            break

    if outcome == DiscoveryOutcome.fixpoint and katana_budget_exhausted:
        outcome = DiscoveryOutcome.budget_exhausted
        stop_reason = "katana_crawl_budget"
    result = DiscoveryResult(
        outcome=outcome,
        rounds=completed_rounds,
        new_entry_count=len(_entry_keys(current_sitemap) - baseline_keys),
        state_count=total_states,
        workflow_count=total_workflows,
        stop_reason=stop_reason,
    )
    final_evidence = job_persistence.evidence_for_discovery_result(
        checkpoint_evidence(),
        result,
    )
    return BrowserGuidedDiscoveryExecution(
        sitemap=current_sitemap,
        evidence=final_evidence,
        result=result,
    )


async def finalize_baseline_checkpoint(
    job_id: str,
    result: DiscoveryResult,
    *,
    expected_statuses: set[JobStatus],
    error: str | None = None,
    final_status: JobStatus = JobStatus.completed,
) -> bool:
    return await _job_persistence.finalize_from_latest_checkpoint(
        job_id,
        result,
        expected_statuses=expected_statuses,
        error=error,
        final_status=final_status,
    )


async def finalize_operator_cancellation(job_id: str) -> None:
    """Publish the newest valid checkpoint while retaining cancelled job status."""

    row = await db.fetch_one(
        "SELECT status, baseline_sitemap FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    if row is None or row["status"] in TERMINAL_JOB_STATUSES:
        return
    status = JobStatus(row["status"])
    if row["baseline_sitemap"] is not None:
        result = DiscoveryResult(
            outcome=DiscoveryOutcome.interrupted,
            rounds=0,
            new_entry_count=0,
            state_count=0,
            workflow_count=0,
            stop_reason="cancelled_after_checkpoint",
        )
        try:
            if await _job_persistence.finalize_from_latest_checkpoint(
                job_id,
                result,
                expected_statuses={status},
                final_status=JobStatus.cancelled,
            ):
                return
        except (RuntimeError, parser.CrawlArtifactsCorruptError) as exc:
            logger.warning(
                "Could not finalize cancelled job checkpoint job_id=%s error=%s",
                sanitize_log_value(job_id),
                sanitize_log_value(exc),
            )
    await _job_persistence.update_status(job_id, JobStatus.cancelled)


async def recover_interrupted_jobs() -> None:
    placeholders = ",".join("?" for _ in INTERRUPTED_JOB_STATUSES)
    rows = await db.fetch_all(
        f"""
        SELECT job_id, status, baseline_sitemap, discovery_checkpoint_sitemap
        FROM jobs
        WHERE status IN ({placeholders})
        """,
        tuple(INTERRUPTED_JOB_STATUSES),
    )
    for row in rows:
        job_id = str(row["job_id"])
        status = JobStatus(row["status"])
        if row["baseline_sitemap"] is not None:
            result = DiscoveryResult(
                outcome=DiscoveryOutcome.interrupted,
                rounds=0,
                new_entry_count=0,
                state_count=0,
                workflow_count=0,
                stop_reason=(
                    "server_restarted_after_discovery_checkpoint"
                    if row["discovery_checkpoint_sitemap"] is not None
                    else "server_restarted_after_baseline_checkpoint"
                ),
            )
            try:
                if await _job_persistence.finalize_from_latest_checkpoint(
                    job_id,
                    result,
                    expected_statuses={status},
                ):
                    logger.info(
                        "Finalized interrupted job from baseline checkpoint job_id=%s",
                        sanitize_log_value(job_id),
                    )
                    continue
            except (RuntimeError, parser.CrawlArtifactsCorruptError) as exc:
                logger.warning(
                    "Could not recover baseline checkpoint job_id=%s error=%s",
                    sanitize_log_value(job_id),
                    sanitize_log_value(exc),
                )
        await _job_persistence.fail_if_status(
            job_id,
            status,
            JobStatus.failed_interrupted,
            "Server restarted before a valid baseline checkpoint",
        )
        logger.info("Marked interrupted job as failed_interrupted job_id=%s", job_id)


async def finalize_job_time_budget(job_id: str) -> None:
    row = await db.fetch_one(
        "SELECT status, baseline_sitemap FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    if row is None or row["status"] in TERMINAL_JOB_STATUSES:
        return
    status = JobStatus(row["status"])
    error = f"Job exceeded its {CRAWLER_JOB_TIMEOUT_SECONDS:g}-second deadline"
    if row["baseline_sitemap"] is not None:
        result = DiscoveryResult(
            outcome=DiscoveryOutcome.budget_exhausted,
            rounds=0,
            new_entry_count=0,
            state_count=0,
            workflow_count=0,
            stop_reason="job_time_budget",
        )
        try:
            if await _job_persistence.finalize_from_latest_checkpoint(
                job_id,
                result,
                expected_statuses={status},
                error=error,
            ):
                return
        except (RuntimeError, parser.CrawlArtifactsCorruptError) as exc:
            logger.warning(
                "Could not finalize timed-out job checkpoint job_id=%s error=%s",
                sanitize_log_value(job_id),
                sanitize_log_value(exc),
            )
    await _job_persistence.update_status(
        job_id,
        JobStatus.failed,
        f"{error} before a valid checkpoint",
    )


async def run_job(job_id: str, cancel_event: asyncio.Event) -> None:
    cancellation_requested = False
    deadline_expired = asyncio.Event()
    job_browser: browser_session.BrowserSession | None = None
    log_job_id = sanitize_log_value(job_id)
    standard_log_path = os.path.join(db.LOG_DIR, f"{job_id}.standard.jsonl")
    pure_headless_log_path = os.path.join(db.LOG_DIR, f"{job_id}.pure-headless.jsonl")
    katana_log_paths = [standard_log_path, pure_headless_log_path]
    owner_task = asyncio.current_task()
    assert owner_task is not None

    async def cancel_at_job_deadline() -> None:
        await asyncio.sleep(CRAWLER_JOB_TIMEOUT_SECONDS)
        deadline_expired.set()
        owner_task.cancel()

    deadline_task = asyncio.create_task(cancel_at_job_deadline())
    logger.info("Starting job runner for job_id=%s", log_job_id)
    try:
        row = await db.fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if row is None:
            logger.warning("Job row missing during run_job startup job_id=%s", log_job_id)
            return

        # Check if already cancelled while queued.
        if row["status"] in TERMINAL_JOB_STATUSES:
            logger.info("Job already in terminal state, skipping job_id=%s", log_job_id)
            return

        auth_config = _normalize_auth_config(db.loads_json(row["auth_config"]) or {})
        manual_headers = _extract_manual_headers(auth_config)
        should_auth = auth_agent.needs_auth(auth_config)
        logger.debug("Job %s auth phase required=%s", log_job_id, should_auth)

        next_status = JobStatus.authenticating if should_auth else JobStatus.crawling
        logger.info(
            "Job %s transitioning queued -> %s",
            log_job_id,
            sanitize_log_value(next_status.value),
        )
        claimed = await _job_persistence.claim_job(job_id, next_status)
        if not claimed:
            logger.info(
                "Job was no longer queued when runner tried to claim it job_id=%s",
                log_job_id,
            )
            return
        _raise_if_cancel_requested(cancel_event)

        job_browser = browser_session.BrowserSession(job_id)
        await job_browser.start()
        browser_context = await job_browser.connect_playwright(
            headers=manual_headers,
            target_url=row["target_url"],
            epoch="authentication" if should_auth else "bootstrap",
        )

        auth_context = await job_browser.guard(
            _run_auth_if_needed(
                job_id,
                row["target_url"],
                auth_config,
                manual_headers,
                should_auth,
                cancel_event,
                browser_context=browser_context,
            )
        )
        await job_browser.configure_auth(
            headers=auth_context.headers,
            cookies=auth_context.cookies,
            target_url=row["target_url"],
        )
        _raise_if_cancel_requested(cancel_event)

        if should_auth:
            logger.info("Job %s transitioning authenticating -> crawling", log_job_id)
            if not await _job_persistence.transition(
                job_id,
                {JobStatus.authenticating},
                JobStatus.crawling,
            ):
                raise RuntimeError("Job could not transition from authenticating to crawling")

        logger.info(
            "Crawl config: headers=%d landing_url=%s extra_seeds=%s",
            len(auth_context.headers),
            sanitize_log_value(auth_context.landing_url),
            sanitize_log_value(auth_context.extra_seed_urls or None),
        )
        baseline = await run_baseline_phase(
            job_id=job_id,
            target_url=row["target_url"],
            scope_config=db.loads_json(row["scope_config"]),
            auth_context=auth_context,
            job_browser=job_browser,
            cancel_event=cancel_event,
            standard_log_path=standard_log_path,
            pure_headless_log_path=pure_headless_log_path,
        )

        discovery_config = DiscoveryConfig.model_validate_json(row["discovery_config"])
        if not discovery_config.enabled:
            disabled_result = DiscoveryResult(
                outcome=DiscoveryOutcome.disabled,
                rounds=0,
                new_entry_count=0,
                state_count=0,
                workflow_count=0,
                stop_reason="disabled_by_request",
            )
            if not await _job_persistence.finalize_from_latest_checkpoint(
                job_id,
                disabled_result,
                expected_statuses={JobStatus.discovering},
            ):
                raise RuntimeError("Job could not finalize its disabled discovery checkpoint")
            logger.info("Job completed with discovery disabled job_id=%s", log_job_id)
            return

        try:
            execution = await run_browser_guided_discovery(
                job_id=job_id,
                target_url=row["target_url"],
                scope_config=db.loads_json(row["scope_config"]),
                auth_context=auth_context,
                discovery_config=discovery_config,
                job_browser=job_browser,
                known_file_result=baseline.known_file_result,
                baseline_sitemap=baseline.sitemap,
                baseline_evidence=baseline.evidence,
                katana_sitemaps=baseline.katana_sitemaps,
                katana_records=baseline.katana_records,
                cancel_event=cancel_event,
                katana_log_paths=katana_log_paths,
                checkpoint_writer=_job_persistence.publish_discovery_checkpoint,
                pending_known_file_seeds=baseline.pending_known_file_seeds,
                katana_partial=baseline.katana_partial,
            )
        except asyncio.CancelledError, _JobCancellationRequested:
            raise
        except Exception as exc:
            stop_reason = (
                "job_memory_budget"
                if isinstance(exc, ProcessMemoryLimitExceeded)
                else "browser_discovery_failed_after_baseline"
            )
            partial_result = DiscoveryResult(
                outcome=DiscoveryOutcome.partial_failure,
                rounds=0,
                new_entry_count=0,
                state_count=0,
                workflow_count=0,
                stop_reason=stop_reason,
            )
            if not await _job_persistence.finalize_from_latest_checkpoint(
                job_id,
                partial_result,
                expected_statuses={JobStatus.discovering},
                error=f"Browser discovery failed: {sanitize_log_value(exc)}",
            ):
                raise RuntimeError("Job could not finalize its partial discovery result") from exc
            logger.warning(
                "Job completed from baseline after discovery failure job_id=%s error=%s",
                log_job_id,
                sanitize_log_value(exc),
            )
            return
        _raise_if_cancel_requested(cancel_event)
        logger.info("Job %s transitioning discovering -> processing", log_job_id)
        if not await _job_persistence.transition(
            job_id,
            {JobStatus.discovering},
            JobStatus.processing,
        ):
            raise RuntimeError("Job could not transition from crawling to processing")
        _raise_if_cancel_requested(cancel_event)
        logger.info("Job %s transitioning processing -> completed", log_job_id)
        if not await _job_persistence.complete(
            job_id,
            execution.sitemap,
            execution.evidence,
            execution.result,
            cancel_event,
        ):
            if cancel_event.is_set():
                raise _JobCancellationRequested
            raise RuntimeError("Job could not transition from processing to completed")
        logger.info("Job completed successfully job_id=%s", log_job_id)
    except _JobCancellationRequested:
        cancellation_requested = True
        logger.info("Job cancellation reached a safe checkpoint job_id=%s", log_job_id)
    except asyncio.CancelledError:
        if deadline_expired.is_set() and not cancel_event.is_set():
            logger.warning("Job reached its wall-clock deadline job_id=%s", log_job_id)
            await finalize_job_time_budget(job_id)
        elif cancel_event.is_set():
            logger.info("Operator cancellation stopped job task job_id=%s", log_job_id)
            cancellation_requested = True
        else:
            logger.warning(
                "Job task interrupted without operator cancellation job_id=%s", log_job_id
            )
    except Exception as exc:  # pragma: no cover - safeguard
        logger.warning("Job failed job_id=%s error=%s", log_job_id, sanitize_log_value(exc))
        if cancel_event.is_set():
            cancellation_requested = True
        else:
            await _job_persistence.update_status(job_id, JobStatus.failed, str(exc))
    finally:
        deadline_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await deadline_task
        if job_browser is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await job_browser.stop()
        await asyncio.gather(
            *(asyncio.to_thread(sanitize_log_file, path) for path in katana_log_paths)
        )
        if cancellation_requested:
            await finalize_operator_cancellation(job_id)
        logger.debug("Job runner cleanup finished for job_id=%s", log_job_id)


async def _drain_queue() -> None:
    logger.info("Queue drainer started")
    while True:
        logger.info("Queue drainer waiting for next job (queue_size=%d)", _queue.qsize())
        job_id = await _queue.get()
        logger.info("Queue drainer picked up job_id=%s (remaining=%d)", job_id, _queue.qsize())
        cancel_event = asyncio.Event()
        _cancel_events[job_id] = cancel_event
        _state_locks[job_id] = asyncio.Lock()
        task = asyncio.create_task(run_job(job_id, cancel_event))
        _job_tasks[job_id] = task

        def _cleanup(_: asyncio.Task[None], _job_id: str = job_id) -> None:
            _cancel_events.pop(_job_id, None)
            _job_tasks.pop(_job_id, None)
            _state_locks.pop(_job_id, None)
            logger.debug(
                "Cleaned up in-memory job task state job_id=%s",
                sanitize_log_value(_job_id),
            )

        task.add_done_callback(_cleanup)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            _queue.task_done()
        logger.info("Queue drainer finished job_id=%s", sanitize_log_value(job_id))


def start_drainer() -> None:
    global _drainer_task
    if _drainer_task is None or _drainer_task.done():
        _drainer_task = asyncio.create_task(_drain_queue())
        logger.info("Started queue drainer task")


def enqueue_job(job_id: str) -> None:
    _queue.put_nowait(job_id)
    logger.info("Enqueued job job_id=%s queue_size=%d", sanitize_log_value(job_id), _queue.qsize())


async def request_cancel(job_id: str) -> bool:
    event = _cancel_events.get(job_id)
    if event is None:
        logger.warning("Cancel requested for non-running job_id=%s", sanitize_log_value(job_id))
        return False
    lock = _state_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        row = await db.fetch_one("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
        if row is None or row["status"] in TERMINAL_JOB_STATUSES:
            return False
        event.set()
        logger.info("Set cancellation event for job_id=%s", sanitize_log_value(job_id))
        return True


def get_job_task(job_id: str) -> asyncio.Task[None] | None:
    return _job_tasks.get(job_id)
