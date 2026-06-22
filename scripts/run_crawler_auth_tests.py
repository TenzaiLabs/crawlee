from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

TERMINAL_STATUSES = {"completed", "failed", "failed_interrupted", "cancelled"}
DEFAULT_MODES = ["llm_no_auth", "manual_headers", "llm"]
TESTSITES_DIR = Path(__file__).resolve().parent.parent / "testsites"


@dataclass
class CrawlerSiteResult:
    name: str
    mode: str
    passed: bool
    status: str | None = None
    job_id: str | None = None
    entry_count: int = 0
    matched_urls: list[str] | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0


def _configure_runtime_environment(args: argparse.Namespace, temp_dir: Path) -> None:
    db_path = Path(args.db_path) if args.db_path else temp_dir / "jobs.db"
    log_dir = Path(args.log_dir) if args.log_dir else temp_dir / "logs"

    os.environ["CRAWLER_DB_PATH"] = str(db_path)
    os.environ["CRAWLER_LOG_DIR"] = str(log_dir)
    os.environ["CRAWLER_SUBPROCESS_TIMEOUT"] = str(args.subprocess_timeout)

    # Keep auth attempts deterministic for the runner: failures should surface as test failures
    # rather than stretching each case across repeated full LLM/browser attempts.
    if args.auth_attempts is not None:
        os.environ["CRAWLER_AUTH_ATTEMPTS"] = str(args.auth_attempts)


def _import_main_app() -> Any:
    reload_order = [
        "app.settings",
        "app.db",
        "app.parser",
        "app.process",
        "app.proxy",
        "app.crawler",
        "app.orchestrator",
        "app.main",
    ]
    for module_name in reload_order:
        module = sys.modules.get(module_name)
        if module is not None:
            importlib.reload(module)
    return importlib.import_module("app.main")


def _select_cases(cases: list[Any], *, names: list[str], modes: list[str]) -> list[Any]:
    selected = cases
    if names:
        wanted_names = set(names)
        selected = [case for case in selected if case.name in wanted_names]
    if modes:
        wanted_modes = set(modes)
        selected = [case for case in selected if case.mode in wanted_modes]
    return selected


def _build_scope_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "max_depth": args.max_depth,
        "max_pages": args.max_pages,
        "rate_limit": args.rate_limit,
        "concurrency": args.concurrency,
        "parallelism": args.parallelism,
        "crawl_duration": args.crawl_duration,
        "timeout": args.request_timeout,
        "headless": args.headless,
    }


def _same_origin(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.scheme == right_parsed.scheme
        and left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or _default_port(left_parsed.scheme))
        == (right_parsed.port or _default_port(right_parsed.scheme))
    )


def _default_port(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _entry_matches_probe(entry: dict[str, Any], target_url: str, probe_path: str) -> bool:
    url = entry.get("url")
    if not isinstance(url, str):
        return False

    probe_url = urljoin(target_url.rstrip("/") + "/", probe_path.lstrip("/"))
    if not _same_origin(url, probe_url):
        return False

    parsed_url = urlparse(url)
    parsed_probe = urlparse(probe_url)
    if parsed_url.path.rstrip("/") != parsed_probe.path.rstrip("/"):
        return False

    status = entry.get("status")
    return isinstance(status, int) and 200 <= status < 300


def _entry_matches_path(entry: dict[str, Any], target_url: str, path: str) -> bool:
    url = entry.get("url")
    if not isinstance(url, str):
        return False

    expected_url = urljoin(target_url.rstrip("/") + "/", path.lstrip("/"))
    if not _same_origin(url, expected_url):
        return False

    parsed_url = urlparse(url)
    parsed_expected = urlparse(expected_url)
    return parsed_url.path.rstrip("/") == parsed_expected.path.rstrip("/")


def _site_name_for_case(case: Any) -> str | None:
    case_name = getattr(case, "name", None)
    if not isinstance(case_name, str):
        return None
    site_names = sorted(
        (path.parent.name for path in TESTSITES_DIR.glob("*/sitemap.json")),
        key=len,
        reverse=True,
    )
    for site_name in site_names:
        if case_name == site_name or case_name.startswith(f"{site_name}-"):
            return site_name
    return None


def _blocked_paths_for_case(case: Any) -> tuple[str, ...]:
    site_name = _site_name_for_case(case)
    if site_name is None:
        return ()
    sitemap_path = TESTSITES_DIR / site_name / "sitemap.json"
    try:
        data = json.loads(sitemap_path.read_text())
    except OSError, json.JSONDecodeError:
        return ()

    paths: list[str] = []
    for entry in data.get("blocked_entries", []):
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str):
            continue
        path = urlparse(url).path
        if path and path not in paths:
            paths.append(path)
    return tuple(paths)


def _entry_is_successful_same_origin(entry: dict[str, Any], target_url: str) -> bool:
    url = entry.get("url")
    status = entry.get("status")
    return (
        isinstance(url, str)
        and isinstance(status, int)
        and _same_origin(url, target_url)
        and 200 <= status < 400
    )


def _validate_sitemap(
    case: Any,
    sitemap: dict[str, Any] | None,
) -> tuple[bool, list[str], str | None]:
    entries = sitemap.get("entries", []) if isinstance(sitemap, dict) else []
    if not isinstance(entries, list):
        return False, [], "sitemap entries are missing or malformed"

    excluded_paths = _unique_strings(
        [
            *tuple(getattr(case, "expected_blocked_paths", ())),
            *_blocked_paths_for_case(case),
        ]
    )
    crawled_excluded_urls = [
        entry.get("url")
        for entry in entries
        if isinstance(entry, dict)
        for excluded_path in excluded_paths
        if _entry_matches_path(entry, case.target_url, excluded_path)
    ]
    crawled_excluded = [url for url in crawled_excluded_urls if isinstance(url, str)]
    if crawled_excluded:
        return (
            False,
            crawled_excluded[:5],
            ("blocked URL was crawled: " + ", ".join(crawled_excluded[:5])),
        )

    if case.mode == "llm_no_auth":
        matched = [
            entry.get("url")
            for entry in entries
            if isinstance(entry, dict) and _entry_is_successful_same_origin(entry, case.target_url)
        ]
        urls = [url for url in matched if isinstance(url, str)]
        if urls:
            return True, urls[:5], None
        return False, [], "no successful same-origin pages were crawled"

    matched = [
        entry.get("url")
        for entry in entries
        if isinstance(entry, dict) and _entry_matches_probe(entry, case.target_url, case.probe_path)
    ]
    urls = [url for url in matched if isinstance(url, str)]
    if urls:
        return True, urls[:5], None
    return False, [], f"protected probe path was not crawled: {case.probe_path}"


def _unique_strings(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return tuple(unique)


async def _poll_job(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    poll_interval: float,
    timeout: float,
    cancel_timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/jobs/{job_id}")
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") in TERMINAL_STATUSES:
            return payload
        await asyncio.sleep(poll_interval)

    with contextlib.suppress(Exception):
        await client.post(f"/jobs/{job_id}/cancel")
    cancel_deadline = time.monotonic() + cancel_timeout
    while time.monotonic() < cancel_deadline:
        with contextlib.suppress(Exception):
            response = await client.get(f"/jobs/{job_id}")
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") in TERMINAL_STATUSES:
                payload["error"] = f"timed out after {timeout:.1f}s"
                return payload
        await asyncio.sleep(poll_interval)

    payload["status"] = payload.get("status") or "timeout"
    payload["error"] = f"timed out after {timeout:.1f}s"
    return payload


async def run_case(
    client: httpx.AsyncClient,
    case: Any,
    *,
    scope_config: dict[str, Any],
    poll_interval: float,
    job_timeout: float,
    cancel_timeout: float,
) -> CrawlerSiteResult:
    started_at = time.monotonic()
    try:
        response = await client.post(
            "/jobs",
            json={
                "target_url": case.target_url,
                "scope_config": scope_config,
                "auth_config": case.auth_config,
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
        status = payload.get("status")
        sitemap = payload.get("sitemap")
        entries = sitemap.get("entries", []) if isinstance(sitemap, dict) else []
        entry_count = len(entries) if isinstance(entries, list) else 0

        if status != "completed":
            return CrawlerSiteResult(
                name=case.name,
                mode=case.mode,
                passed=False,
                status=str(status),
                job_id=job_id,
                entry_count=entry_count,
                error=payload.get("error") or f"job finished with status={status}",
                elapsed_seconds=time.monotonic() - started_at,
            )

        passed, matched_urls, validation_error = _validate_sitemap(case, sitemap)
        return CrawlerSiteResult(
            name=case.name,
            mode=case.mode,
            passed=passed,
            status=str(status),
            job_id=job_id,
            entry_count=entry_count,
            matched_urls=matched_urls,
            error=validation_error,
            elapsed_seconds=time.monotonic() - started_at,
        )
    except Exception as exc:
        return CrawlerSiteResult(
            name=case.name,
            mode=case.mode,
            passed=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_seconds=time.monotonic() - started_at,
        )


async def run_cases(
    cases: list[Any],
    *,
    app: Any,
    scope_config: dict[str, Any],
    poll_interval: float,
    job_timeout: float,
    cancel_timeout: float,
) -> list[CrawlerSiteResult]:
    results: list[CrawlerSiteResult] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        timeout=job_timeout + 5,
    ) as client:
        for case in cases:
            print(f"crawler-auth-test {case.name} ({case.mode}) ...", flush=True)
            result = await run_case(
                client,
                case,
                scope_config=scope_config,
                poll_interval=poll_interval,
                job_timeout=job_timeout,
                cancel_timeout=cancel_timeout,
            )
            status = "PASS" if result.passed else "FAIL"
            detail = result.error or f"entries={result.entry_count} matched={result.matched_urls}"
            print(
                f"  {status} job={result.job_id} status={result.status} "
                f"elapsed={result.elapsed_seconds:.1f}s {detail}",
                flush=True,
            )
            results.append(result)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run full crawler auth jobs against local test websites.",
    )
    parser.add_argument("--gateway", action="store_true", help="Use nginx gateway ports 9101-9215.")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run one named case. Repeatable.",
    )
    parser.add_argument(
        "--mode",
        action="append",
        choices=["public", "llm_no_auth", "manual_headers", "llm"],
        default=[],
        help=(
            "Run cases by mode. Repeatable. Defaults to llm_no_auth, manual_headers, and llm "
            "when no --case is provided."
        ),
    )
    parser.add_argument("--db-path", help="SQLite DB path. Defaults to a temp directory.")
    parser.add_argument("--log-dir", help="Crawler log directory. Defaults to a temp directory.")
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--rate-limit", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--parallelism", type=int, default=10)
    parser.add_argument("--crawl-duration", default="25s")
    parser.add_argument("--request-timeout", type=int, default=10)
    parser.add_argument("--subprocess-timeout", type=int, default=45)
    parser.add_argument("--auth-attempts", type=int, default=1)
    parser.add_argument("--job-timeout", type=float, default=120.0)
    parser.add_argument("--cancel-timeout", type=float, default=20.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Enable Katana hybrid/headless crawling. Disabled by default for fixture speed.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    return parser.parse_args()


async def _stop_orchestrator_tasks(main: Any) -> None:
    orchestrator = main.orchestrator
    for event in list(getattr(orchestrator, "_cancel_events", {}).values()):
        event.set()

    tasks = list(getattr(orchestrator, "_job_tasks", {}).values())
    drainer = getattr(orchestrator, "_drainer_task", None)
    if drainer is not None:
        tasks.append(drainer)

    for task in tasks:
        if not task.done():
            task.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def async_main() -> int:
    args = parse_args()
    temp_root = tempfile.TemporaryDirectory(prefix="crawler-auth-e2e-")
    temp_dir = Path(temp_root.name)
    try:
        _configure_runtime_environment(args, temp_dir)

        from scripts.run_auth_agent_tests import build_cases

        modes = args.mode if args.mode else ([] if args.case else DEFAULT_MODES)
        cases = _select_cases(build_cases(gateway=args.gateway), names=args.case, modes=modes)
        if not cases:
            print("No cases selected.")
            return 2

        main = _import_main_app()
        scope_config = _build_scope_config(args)
        async with main.lifespan(main.app):
            try:
                await main.db.execute("DELETE FROM jobs")
                results = await run_cases(
                    cases,
                    app=main.app,
                    scope_config=scope_config,
                    poll_interval=args.poll_interval,
                    job_timeout=args.job_timeout,
                    cancel_timeout=args.cancel_timeout,
                )
            finally:
                await _stop_orchestrator_tasks(main)

        failed = [result for result in results if not result.passed]
        if args.json:
            print(json.dumps([result.__dict__ for result in results], indent=2, sort_keys=True))
        print(
            f"crawler-auth-test summary: {len(results) - len(failed)} passed, {len(failed)} failed"
        )
        return 1 if failed else 0
    finally:
        temp_root.cleanup()


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
