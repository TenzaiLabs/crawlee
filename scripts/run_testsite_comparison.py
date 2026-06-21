from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import os
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.run_auth_agent_tests import AuthAgentSiteCase, build_cases
from scripts.run_crawler_auth_tests import (
    _blocked_paths_for_case,
    _build_scope_config,
    _entry_is_successful_same_origin,
    _entry_matches_path,
    _entry_matches_probe,
    _import_main_app,
    _poll_job,
    _site_name_for_case,
    _stop_orchestrator_tasks,
)

REPORT_PATH = Path("docs/testsite-comparison-report.md")
JSON_PATH = Path("docs/testsite-comparison-results.json")


@dataclass(frozen=True)
class Variant:
    key: str
    title: str
    auth_agent_enabled: bool
    safety_guards_enabled: bool


@dataclass
class ComparisonResult:
    run: int
    site: str
    case_name: str
    original_mode: str
    variant: str
    passed: bool
    access_ok: bool
    safe_ok: bool
    status: str | None = None
    job_id: str | None = None
    entry_count: int = 0
    matched_urls: list[str] | None = None
    blocked_hits: list[str] | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0


VARIANTS = [
    Variant(
        key="full",
        title="Full crawler",
        auth_agent_enabled=True,
        safety_guards_enabled=True,
    ),
    Variant(
        key="no_safety",
        title="Crawler with no safety guards",
        auth_agent_enabled=True,
        safety_guards_enabled=False,
    ),
    Variant(
        key="no_auth_no_safety",
        title="Crawler with no auth agent and no safety guards",
        auth_agent_enabled=False,
        safety_guards_enabled=False,
    ),
]


def _canonical_cases(*, gateway: bool) -> list[AuthAgentSiteCase]:
    by_site: dict[str, AuthAgentSiteCase] = {}
    for case in build_cases(gateway=gateway):
        if case.mode == "llm_no_auth":
            continue
        site = _site_name_for_case(case)
        if site is None:
            continue
        existing = by_site.get(site)
        if existing is None or case.expected_blocked_paths:
            by_site[site] = case
    return [by_site[site] for site in sorted(by_site)]


def _case_for_variant(case: AuthAgentSiteCase, variant: Variant) -> AuthAgentSiteCase:
    if variant.auth_agent_enabled or case.mode != "llm":
        return case
    return replace(case, auth_config=None)


def _configure_runtime_environment(args: argparse.Namespace, temp_dir: Path) -> None:
    os.environ["CRAWLER_DB_PATH"] = str(temp_dir / "jobs.db")
    os.environ["CRAWLER_LOG_DIR"] = str(temp_dir / "logs")
    os.environ["CRAWLER_SUBPROCESS_TIMEOUT"] = str(args.subprocess_timeout)
    os.environ["CRAWLER_AUTH_ATTEMPTS"] = str(args.auth_attempts)


def _disable_safety_guards() -> None:
    crawler = importlib.import_module("app.crawler")
    crawler.DEFAULT_EXCLUSION_PATTERNS = []

    def blocked_urls_to_exclude_patterns(
        blocked_urls: list[str] | None,
        *,
        target_url: str,
        base_url: str | None = None,
    ) -> list[str]:
        return []

    exclude_converter_name = "blocked_urls_to_exclude_patterns"
    setattr(crawler, exclude_converter_name, blocked_urls_to_exclude_patterns)


def _entry_count(sitemap: dict[str, Any] | None) -> int:
    entries = sitemap.get("entries", []) if isinstance(sitemap, dict) else []
    return len(entries) if isinstance(entries, list) else 0


def _sitemap_entries(sitemap: dict[str, Any] | None) -> list[dict[str, Any]]:
    entries = sitemap.get("entries", []) if isinstance(sitemap, dict) else []
    return [entry for entry in entries if isinstance(entry, dict)]


def _blocked_hits(case: AuthAgentSiteCase, entries: list[dict[str, Any]]) -> list[str]:
    blocked_paths = [
        *tuple(getattr(case, "expected_blocked_paths", ())),
        *_blocked_paths_for_case(case),
    ]
    hits: list[str] = []
    for entry in entries:
        url = entry.get("url")
        if not isinstance(url, str):
            continue
        if any(_entry_matches_path(entry, case.target_url, path) for path in blocked_paths):
            if url not in hits:
                hits.append(url)
    return hits


def _matched_urls(case: AuthAgentSiteCase, entries: list[dict[str, Any]]) -> list[str]:
    if case.mode == "public":
        matches = [
            entry.get("url")
            for entry in entries
            if _entry_is_successful_same_origin(entry, case.target_url)
        ]
    else:
        matches = [
            entry.get("url")
            for entry in entries
            if _entry_matches_probe(entry, case.target_url, case.probe_path)
        ]
    return [url for url in matches if isinstance(url, str)]


def _classify_result(
    case: AuthAgentSiteCase,
    variant: Variant,
    payload: dict[str, Any],
    *,
    run_index: int,
    job_id: str,
    elapsed_seconds: float,
) -> ComparisonResult:
    sitemap = payload.get("sitemap")
    entries = _sitemap_entries(sitemap)
    matched_urls = _matched_urls(case, entries)
    blocked_hits = _blocked_hits(case, entries)
    status = payload.get("status")
    completed = status == "completed"
    access_ok = completed and bool(matched_urls)
    safe_ok = completed and not blocked_hits

    error = payload.get("error")
    if completed and not access_ok:
        error = f"probe path was not crawled: {case.probe_path}"
    if completed and access_ok and blocked_hits:
        error = "blocked URL was crawled"

    site = _site_name_for_case(case) or case.name
    return ComparisonResult(
        run=run_index,
        site=site,
        case_name=case.name,
        original_mode=case.mode,
        variant=variant.key,
        passed=access_ok and safe_ok,
        access_ok=access_ok,
        safe_ok=safe_ok,
        status=str(status) if status is not None else None,
        job_id=job_id,
        entry_count=_entry_count(sitemap),
        matched_urls=matched_urls[:5],
        blocked_hits=blocked_hits[:5],
        error=error,
        elapsed_seconds=elapsed_seconds,
    )


async def _run_case(
    client: httpx.AsyncClient,
    case: AuthAgentSiteCase,
    variant: Variant,
    *,
    run_index: int,
    scope_config: dict[str, Any],
    poll_interval: float,
    job_timeout: float,
    cancel_timeout: float,
) -> ComparisonResult:
    started_at = time.monotonic()
    variant_case = _case_for_variant(case, variant)
    site = _site_name_for_case(case) or case.name
    try:
        response = await client.post(
            "/jobs",
            json={
                "target_url": variant_case.target_url,
                "scope_config": scope_config,
                "auth_config": variant_case.auth_config,
            },
        )
        response.raise_for_status()
        job_id = response.json()["job_id"]
        payload = await _poll_job(
            client,
            job_id,
            poll_interval=poll_interval,
            timeout=job_timeout,
            cancel_timeout=cancel_timeout,
        )
        return _classify_result(
            case,
            variant,
            payload,
            run_index=run_index,
            job_id=job_id,
            elapsed_seconds=time.monotonic() - started_at,
        )
    except Exception as exc:
        return ComparisonResult(
            run=run_index,
            site=site,
            case_name=case.name,
            original_mode=case.mode,
            variant=variant.key,
            passed=False,
            access_ok=False,
            safe_ok=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_seconds=time.monotonic() - started_at,
        )


async def _run_variant(
    cases: list[AuthAgentSiteCase],
    variant: Variant,
    args: argparse.Namespace,
    *,
    run_index: int,
) -> list[ComparisonResult]:
    temp_root = tempfile.TemporaryDirectory(
        prefix=f"crawler-comparison-{variant.key}-run-{run_index}-"
    )
    temp_dir = Path(temp_root.name)
    try:
        _configure_runtime_environment(args, temp_dir)
        main = _import_main_app()
        if not variant.safety_guards_enabled:
            _disable_safety_guards()
        scope_config = _build_scope_config(args)
        results: list[ComparisonResult] = []
        transport = httpx.ASGITransport(app=main.app)
        async with main.lifespan(main.app):
            try:
                await main.db.execute("DELETE FROM jobs")
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                    timeout=args.job_timeout + 5,
                ) as client:
                    for case in cases:
                        print(
                            f"comparison run={run_index} {variant.key} {case.name} ...",
                            flush=True,
                        )
                        result = await _run_case(
                            client,
                            case,
                            variant,
                            run_index=run_index,
                            scope_config=scope_config,
                            poll_interval=args.poll_interval,
                            job_timeout=args.job_timeout,
                            cancel_timeout=args.cancel_timeout,
                        )
                        status = "PASS" if result.passed else "FAIL"
                        detail = result.error or (
                            f"entries={result.entry_count} matched={result.matched_urls}"
                        )
                        print(
                            f"  {status} status={result.status} "
                            f"elapsed={result.elapsed_seconds:.1f}s {detail}",
                            flush=True,
                        )
                        results.append(result)
            finally:
                await _stop_orchestrator_tasks(main)
        return results
    finally:
        temp_root.cleanup()


def _cell(result: ComparisonResult) -> str:
    if result.passed:
        return "PASS"
    if result.access_ok and not result.safe_ok:
        return f"UNSAFE ({len(result.blocked_hits or [])} blocked)"
    if result.status == "completed":
        return "NO ACCESS"
    return "FAIL"


def _selected_variants(args: argparse.Namespace) -> list[Variant]:
    wanted = set(args.variant)
    if not wanted:
        return VARIANTS
    return [variant for variant in VARIANTS if variant.key in wanted]


def _outcome(result: ComparisonResult) -> str:
    if result.passed:
        return "PASS"
    if result.access_ok and not result.safe_ok:
        return "UNSAFE"
    if result.status == "completed":
        return "NO ACCESS"
    return "FAIL"


def _matrix_cell(results: list[ComparisonResult]) -> str:
    if not results:
        return "NOT RUN"
    cells = [_cell(result) for result in results]
    counts = Counter(cells)
    if len(counts) == 1:
        cell = cells[0]
        return f"{cell} ({len(results)}/{len(results)})" if len(results) > 1 else cell
    parts = [f"{cell} {count}" for cell, count in counts.most_common()]
    return "FLAKY: " + ", ".join(parts)


def _summary(
    results: list[ComparisonResult],
    variants: list[Variant] | None = None,
) -> dict[str, dict[str, int]]:
    variants = variants or VARIANTS
    summary: dict[str, dict[str, int]] = {}
    for variant in variants:
        subset = [result for result in results if result.variant == variant.key]
        summary[variant.key] = {
            "jobs": len(subset),
            "passed": sum(1 for result in subset if result.passed),
            "access_ok": sum(1 for result in subset if result.access_ok),
            "safe_ok": sum(1 for result in subset if result.safe_ok),
            "unsafe": sum(1 for result in subset if result.access_ok and not result.safe_ok),
            "no_access": sum(
                1 for result in subset if result.status == "completed" and not result.access_ok
            ),
            "failed_jobs": sum(
                1 for result in subset if result.status is not None and result.status != "completed"
            ),
        }
    return summary


def _speed_summary(
    results: list[ComparisonResult],
    variants: list[Variant],
) -> dict[str, dict[str, float | int]]:
    summary: dict[str, dict[str, float | int]] = {}
    for variant in variants:
        values = [result.elapsed_seconds for result in results if result.variant == variant.key]
        if not values:
            summary[variant.key] = {
                "jobs": 0,
                "avg_seconds": 0.0,
                "median_seconds": 0.0,
                "min_seconds": 0.0,
                "max_seconds": 0.0,
            }
            continue
        summary[variant.key] = {
            "jobs": len(values),
            "avg_seconds": statistics.fmean(values),
            "median_seconds": statistics.median(values),
            "min_seconds": min(values),
            "max_seconds": max(values),
        }
    return summary


def _reliability_summary(
    cases: list[AuthAgentSiteCase],
    results: list[ComparisonResult],
    variants: list[Variant],
    *,
    expected_runs: int,
) -> dict[str, dict[str, Any]]:
    site_names = [_site_name_for_case(case) or case.name for case in cases]
    grouped: dict[tuple[str, str], list[ComparisonResult]] = defaultdict(list)
    for result in results:
        grouped[(result.variant, result.site)].append(result)

    summary: dict[str, dict[str, Any]] = {}
    for variant in variants:
        stable_sites = 0
        unstable_sites: list[str] = []
        incomplete_sites: list[str] = []
        for site in site_names:
            site_results = grouped[(variant.key, site)]
            outcomes = {_outcome(result) for result in site_results}
            complete = len(site_results) == expected_runs
            stable = complete and len(outcomes) == 1
            if stable:
                stable_sites += 1
            else:
                unstable_sites.append(site)
                if not complete:
                    incomplete_sites.append(site)

        total_sites = len(site_names)
        summary[variant.key] = {
            "runs": expected_runs,
            "sites": total_sites,
            "stable_sites": stable_sites,
            "reliability_percent": (stable_sites / total_sites * 100) if total_sites else 0.0,
            "unstable_sites": unstable_sites,
            "incomplete_sites": incomplete_sites,
        }
    return summary


def _write_json(path: Path, results: list[ComparisonResult], args: argparse.Namespace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    variants = _selected_variants(args)
    cases = _canonical_cases(gateway=args.gateway)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {
            "gateway": args.gateway,
            "crawl_duration": args.crawl_duration,
            "max_depth": args.max_depth,
            "max_pages": args.max_pages,
            "headless": args.headless,
            "runs": args.runs,
            "variants": [variant.key for variant in variants],
            "manual_headers_retained_without_auth_agent": True,
        },
        "summary": _summary(results, variants),
        "speed": _speed_summary(results, variants),
        "reliability": _reliability_summary(
            cases,
            results,
            variants,
            expected_runs=args.runs,
        ),
        "results": [result.__dict__ for result in results],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_report(
    path: Path,
    cases: list[AuthAgentSiteCase],
    results: list[ComparisonResult],
    args: argparse.Namespace,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    variants = _selected_variants(args)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    by_variant_site: dict[tuple[str, str], list[ComparisonResult]] = defaultdict(list)
    for result in results:
        by_variant_site[(result.variant, result.site)].append(result)
    summary = _summary(results, variants)
    speed = _speed_summary(results, variants)
    reliability = _reliability_summary(cases, results, variants, expected_runs=args.runs)
    total_sites = len(cases)
    lines = [
        "# Testsite Crawler Comparison Report",
        "",
        f"Generated: `{generated_at}`",
        "",
        "## Run Configuration",
        "",
        f"- Sites: `{total_sites}` canonical fixtures from `testsites/`.",
        f"- Runs per selected site/variant: `{args.runs}`.",
        f"- Crawl duration: `{args.crawl_duration}`.",
        f"- Max depth/pages: `{args.max_depth}` / `{args.max_pages}`.",
        f"- Headless Katana hybrid mode: `{args.headless}`.",
        "- Safety guards: default dangerous-path exclusions plus auth-recorded blocked URLs.",
        (
            "- No-auth-agent variant keeps manual `Authorization` headers because header-only "
            "auth does not invoke the AI auth agent."
        ),
        "",
        "## Summary",
        "",
        "| Variant | Jobs | PASS | Access OK | Safe OK | Unsafe | No access | Failed jobs |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in variants:
        item = summary[variant.key]
        lines.append(
            f"| {variant.title} | {item['jobs']} | {item['passed']} | {item['access_ok']} | "
            f"{item['safe_ok']} | {item['unsafe']} | {item['no_access']} | "
            f"{item['failed_jobs']} |"
        )

    full = summary.get("full")
    no_safety = summary.get("no_safety")
    no_auth = summary.get("no_auth_no_safety")
    readout: list[str] = []
    if full and no_safety and no_auth:
        readout.extend(
            [
                (
                    f"- Full crawler passed `{full['passed']}` jobs, compared with "
                    f"`{no_safety['passed']}` with safety disabled and "
                    f"`{no_auth['passed']}` with both auth agent and safety disabled."
                ),
                (
                    f"- Safety guards reduced unsafe successful crawls from "
                    f"`{no_safety['unsafe']}` to `{full['unsafe']}`."
                ),
                (
                    f"- The auth agent increased access-ok crawls from "
                    f"`{no_auth['access_ok']}` to `{full['access_ok']}`."
                ),
            ]
        )
    if "full" in speed:
        full_speed = speed["full"]
        readout.append(f"- Full crawler median job time was `{full_speed['median_seconds']:.1f}s`.")
    if "full" in reliability:
        full_reliability = reliability["full"]
        readout.append(
            f"- Full crawler repeated-result reliability was "
            f"`{full_reliability['stable_sites']}/{full_reliability['sites']}` sites "
            f"over `{full_reliability['runs']}` runs."
        )

    lines.extend(
        [
            "",
            "## Improvement Readout",
            "",
            *(readout or ["- No selected variants produced summary data."]),
            "",
            "## Speed",
            "",
            "| Variant | Jobs | Average | Median | Min | Max |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for variant in variants:
        item = speed[variant.key]
        lines.append(
            f"| {variant.title} | {item['jobs']} | {item['avg_seconds']:.1f}s | "
            f"{item['median_seconds']:.1f}s | {item['min_seconds']:.1f}s | "
            f"{item['max_seconds']:.1f}s |"
        )

    lines.extend(
        [
            "",
            "## Reliability",
            "",
            "| Variant | Runs | Stable sites | Reliability | Unstable sites |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for variant in variants:
        item = reliability[variant.key]
        unstable = ", ".join(f"`{site}`" for site in item["unstable_sites"]) or "-"
        lines.append(
            f"| {variant.title} | {item['runs']} | {item['stable_sites']}/{item['sites']} | "
            f"{item['reliability_percent']:.1f}% | {unstable} |"
        )

    lines.extend(
        [
            "",
            "## Matrix",
            "",
            "| Site | Mode | " + " | ".join(variant.title for variant in variants) + " |",
            "| --- | --- | " + " | ".join("---" for _ in variants) + " |",
        ]
    )
    for case in cases:
        site = _site_name_for_case(case) or case.name
        row = [
            site,
            case.mode,
            *[_matrix_cell(by_variant_site[(variant.key, site)]) for variant in variants],
        ]
        lines.append("| " + " | ".join(f"`{item}`" for item in row) + " |")

    lines.extend(["", "## Failures And Unsafe Hits", ""])
    notable = [result for result in results if not result.passed]
    if not notable:
        lines.append("No failures or unsafe hits were observed.")
    else:
        lines.append("| Variant | Site | Status | Entries | Detail |")
        lines.append("| --- | --- | --- | ---: | --- |")
        for result in notable:
            detail = result.error or ""
            if result.blocked_hits:
                detail = "; ".join(result.blocked_hits)
            lines.append(
                f"| `{result.variant}` run `{result.run}` | `{result.site}` | "
                f"`{result.status}` | {result.entry_count} | {detail} |"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare full crawler, no-safety, and no-auth/no-safety testsite runs.",
    )
    parser.add_argument("--gateway", action="store_true", help="Use nginx gateway ports 9101-9215.")
    parser.add_argument(
        "--variant",
        action="append",
        choices=[variant.key for variant in VARIANTS],
        default=[],
        help="Run one comparison variant. Repeatable. Defaults to all variants.",
    )
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument("--json-output", default=str(JSON_PATH))
    parser.add_argument("--runs", type=int, default=1, help="Repeat each selected site/variant.")
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--rate-limit", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--parallelism", type=int, default=10)
    parser.add_argument("--crawl-duration", default="25s")
    parser.add_argument("--request-timeout", type=int, default=10)
    parser.add_argument("--subprocess-timeout", type=int, default=60)
    parser.add_argument("--auth-attempts", type=int, default=1)
    parser.add_argument("--job-timeout", type=float, default=180.0)
    parser.add_argument("--cancel-timeout", type=float, default=20.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Enable Katana hybrid/headless crawling for every variant.",
    )
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    if args.runs < 1:
        print("--runs must be at least 1")
        return 2
    cases = _canonical_cases(gateway=args.gateway)
    if not cases:
        print("No canonical testsite cases selected.")
        return 2
    variants = _selected_variants(args)

    results: list[ComparisonResult] = []
    for run_index in range(1, args.runs + 1):
        for variant in variants:
            results.extend(await _run_variant(cases, variant, args, run_index=run_index))

    _write_json(Path(args.json_output), results, args)
    _write_report(Path(args.report), cases, results, args)
    failed_jobs = [
        result for result in results if result.status is not None and result.status != "completed"
    ]
    print(f"Wrote {args.report}")
    print(f"Wrote {args.json_output}")
    return 1 if failed_jobs else 0


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(async_main()))
    raise SystemExit(130)


if __name__ == "__main__":
    main()
