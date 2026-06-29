from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from scripts.run_crawler_auth_tests import (
    _build_scope_config,
    _import_main_app,
    _poll_job,
    _stop_orchestrator_tasks,
)

REPORT_PATH = Path("docs/external-site-comparison-report.md")
JSON_PATH = Path("docs/external-site-comparison-results.json")


@dataclass(frozen=True)
class ExternalCase:
    site: str
    variant: str
    target_url: str
    auth_config: dict[str, Any] | None


@dataclass
class ExternalResult:
    site: str
    variant: str
    target_url: str
    status: str | None
    passed: bool
    elapsed_seconds: float
    entry_count: int
    sitemap_hash: str
    urls: list[str]
    generated_exclusions: dict[str, Any] | None = None
    job_id: str | None = None
    error: str | None = None
    log_path: str | None = None
    katana_log_path: str | None = None
    stderr_log_path: str | None = None


def _public_auth_config(target_url: str) -> dict[str, Any]:
    return {
        "login_url": target_url,
        "instructions": (
            "This is expected to be a public site with no login form. "
            "Do not create an account or enter credentials. If the page is accessible, "
            "verify access and finish."
        ),
        "max_steps": 35,
    }


def _cmt_auth_config() -> dict[str, Any]:
    return {
        "login_url": "https://cmtint.research.microsoft.com/",
        "probe_url": "/Conference/Recent",
        "credentials": {
            "email": "acepace@gmail.com",
            "password": "{{env:CMT_PASSWORD}}",
        },
        "instructions": (
            "Log in to Microsoft CMT using the provided email and password. "
            "If there are multiple account or sign-in options, choose email/password login. "
            "After login, verify that the authenticated CMT workspace or conference list "
            "is visible using the protected probe URL."
        ),
        "max_steps": 140,
    }


async def _cookie_header_from_cdp(cdp_url: str, target_url: str) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        cookie_values: dict[str, str] = {}
        for context in browser.contexts:
            for cookie in await context.cookies([target_url]):
                name = cookie.get("name")
                value = cookie.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    cookie_values[name] = value
        if not cookie_values:
            raise RuntimeError(f"No cookies available from CDP for {target_url}")
        return "; ".join(f"{name}={value}" for name, value in sorted(cookie_values.items()))


async def _prepare_manual_cookie_header(
    args: argparse.Namespace,
    cases: list[ExternalCase],
) -> None:
    if not args.manual_cookie_from_cdp:
        return
    if not args.cdp_url:
        raise ValueError("--manual-cookie-from-cdp requires --cdp-url")
    if not any(case.variant == "no_auth" for case in cases):
        return
    target_url = next(case.target_url for case in cases if case.variant == "no_auth")
    os.environ[args.manual_cookie_env] = await _cookie_header_from_cdp(args.cdp_url, target_url)


def _manual_no_auth_config(args: argparse.Namespace) -> dict[str, Any] | None:
    headers: list[str] = []
    if args.manual_cookie_from_cdp:
        headers.append(f"Cookie: {{{{env:{args.manual_cookie_env}}}}}")
    if args.manual_auth_header:
        headers.extend(args.manual_auth_header)
    return {"headers": headers} if headers else None


def _apply_manual_no_auth_config(
    cases: list[ExternalCase],
    args: argparse.Namespace,
) -> list[ExternalCase]:
    auth_config = _manual_no_auth_config(args)
    if auth_config is None:
        return cases
    return [
        ExternalCase(case.site, case.variant, case.target_url, auth_config)
        if case.variant == "no_auth"
        else case
        for case in cases
    ]


def build_cases(
    *,
    include_cmt: bool,
    sites: list[str] | None = None,
    variants: list[str] | None = None,
) -> list[ExternalCase]:
    public_targets = [
        ("quotes-to-scrape", "https://quotes.toscrape.com/"),
        ("webscraper-ecommerce-static", "https://webscraper.io/test-sites/e-commerce/static"),
        ("acepace", "https://acepace.net/"),
    ]
    cases: list[ExternalCase] = []
    for site, target_url in public_targets:
        cases.append(ExternalCase(site, "no_auth", target_url, None))
        cases.append(ExternalCase(site, "auth_agent", target_url, _public_auth_config(target_url)))

    if include_cmt:
        cases.append(
            ExternalCase(
                "cmt",
                "no_auth",
                "https://cmtint.research.microsoft.com/",
                None,
            )
        )
        cases.append(
            ExternalCase(
                "cmt",
                "auth_agent",
                "https://cmtint.research.microsoft.com/",
                _cmt_auth_config(),
            )
        )
    if sites:
        wanted_sites = set(sites)
        cases = [case for case in cases if case.site in wanted_sites]
    if variants:
        wanted_variants = set(variants)
        cases = [case for case in cases if case.variant in wanted_variants]
    return cases


def _configure_runtime_environment(args: argparse.Namespace, temp_dir: Path) -> None:
    temp_dir.mkdir(parents=True, exist_ok=True)
    (temp_dir / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["CRAWLER_DB_PATH"] = str(temp_dir / "jobs.db")
    os.environ["CRAWLER_LOG_DIR"] = str(temp_dir / "logs")
    os.environ["CRAWLER_SUBPROCESS_TIMEOUT"] = str(args.subprocess_timeout)
    os.environ["CRAWLER_AUTH_ATTEMPTS"] = str(args.auth_attempts)


def _prepare_artifact_dir(args: argparse.Namespace) -> Path:
    if args.artifact_dir:
        artifact_dir = Path(args.artifact_dir).expanduser()
        artifact_dir.mkdir(parents=True, exist_ok=True)
    else:
        artifact_dir = Path(tempfile.mkdtemp(prefix="crawler-external-comparison-"))
    args.artifact_dir = str(artifact_dir)
    args.db_path = str(artifact_dir / "jobs.db")
    args.log_dir = str(artifact_dir / "logs")
    return artifact_dir


def _job_log_paths(log_dir: Path, job_id: str | None) -> dict[str, str | None]:
    if not job_id:
        return {
            "log_path": None,
            "katana_log_path": None,
            "stderr_log_path": None,
        }
    log_path = log_dir / f"{job_id}.jsonl"
    return {
        "log_path": str(log_path),
        "katana_log_path": str(log_path) + ".katana",
        "stderr_log_path": str(log_path) + ".stderr",
    }


def _normalize_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _sitemap_urls(payload: dict[str, Any]) -> list[str]:
    sitemap = payload.get("sitemap")
    entries = sitemap.get("entries", []) if isinstance(sitemap, dict) else []
    urls: set[str] = set()
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        status = entry.get("status")
        if isinstance(url, str) and isinstance(status, int) and 200 <= status < 400:
            urls.add(_normalize_url(url))
    return sorted(urls)


def _hash_urls(urls: list[str]) -> str:
    digest = hashlib.sha256()
    for url in urls:
        digest.update(url.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


async def _run_case(
    client: httpx.AsyncClient,
    case: ExternalCase,
    *,
    log_dir: Path,
    scope_config: dict[str, Any],
    poll_interval: float,
    job_timeout: float,
    cancel_timeout: float,
) -> ExternalResult:
    started_at = time.monotonic()
    job_id: str | None = None
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
        urls = _sitemap_urls(payload)
        error = payload.get("error")
        if status == "completed" and not urls:
            error = "completed with empty sitemap"
        return ExternalResult(
            site=case.site,
            variant=case.variant,
            target_url=case.target_url,
            status=str(status) if status is not None else None,
            passed=status == "completed" and bool(urls),
            elapsed_seconds=time.monotonic() - started_at,
            entry_count=len(urls),
            sitemap_hash=_hash_urls(urls),
            urls=urls,
            generated_exclusions=payload.get("generated_exclusions")
            if isinstance(payload.get("generated_exclusions"), dict)
            else None,
            job_id=job_id,
            error=error,
            **_job_log_paths(log_dir, job_id),
        )
    except Exception as exc:
        return ExternalResult(
            site=case.site,
            variant=case.variant,
            target_url=case.target_url,
            status=None,
            passed=False,
            elapsed_seconds=time.monotonic() - started_at,
            entry_count=0,
            sitemap_hash=_hash_urls([]),
            urls=[],
            generated_exclusions=None,
            job_id=job_id,
            error=f"{type(exc).__name__}: {exc}",
            **_job_log_paths(log_dir, job_id),
        )


async def run_cases(cases: list[ExternalCase], args: argparse.Namespace) -> list[ExternalResult]:
    artifact_dir = _prepare_artifact_dir(args)
    log_dir = artifact_dir / "logs"
    print(f"Preserving artifacts in {artifact_dir}", flush=True)
    _configure_runtime_environment(args, artifact_dir)
    main = _import_main_app()
    scope_config = _build_scope_config(args)
    if args.cdp_url:
        scope_config["cdp_url"] = args.cdp_url
    if args.no_incognito:
        scope_config["no_incognito"] = True
    results: list[ExternalResult] = []
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
                    print(f"external {case.site} {case.variant} ...", flush=True)
                    result = await _run_case(
                        client,
                        case,
                        log_dir=log_dir,
                        scope_config=scope_config,
                        poll_interval=args.poll_interval,
                        job_timeout=args.job_timeout,
                        cancel_timeout=args.cancel_timeout,
                    )
                    status = "PASS" if result.passed else "FAIL"
                    detail = result.error or (
                        f"urls={result.entry_count} hash={result.sitemap_hash[:12]}"
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


def _site_pairs(results: list[ExternalResult]) -> dict[str, dict[str, ExternalResult]]:
    pairs: dict[str, dict[str, ExternalResult]] = {}
    for result in results:
        pairs.setdefault(result.site, {})[result.variant] = result
    return pairs


def _comparison_rows(results: list[ExternalResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for site, variants in sorted(_site_pairs(results).items()):
        no_auth = variants.get("no_auth")
        auth = variants.get("auth_agent")
        no_auth_urls = set(no_auth.urls) if no_auth else set()
        auth_urls = set(auth.urls) if auth else set()
        rows.append(
            {
                "site": site,
                "no_auth_count": len(no_auth_urls),
                "auth_agent_count": len(auth_urls),
                "identical": no_auth_urls == auth_urls and bool(no_auth_urls),
                "only_no_auth": sorted(no_auth_urls - auth_urls),
                "only_auth_agent": sorted(auth_urls - no_auth_urls),
            }
        )
    return rows


def write_json(path: Path, results: list[ExternalResult], args: argparse.Namespace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {
            "crawl_duration": args.crawl_duration,
            "max_depth": args.max_depth,
            "max_pages": args.max_pages,
            "headless": args.headless,
            "include_cmt": args.include_cmt,
            "artifact_dir": getattr(args, "artifact_dir", None),
            "db_path": getattr(args, "db_path", None),
            "log_dir": getattr(args, "log_dir", None),
            "cdp_url": args.cdp_url,
            "manual_cookie_from_cdp": args.manual_cookie_from_cdp,
            "manual_cookie_env": args.manual_cookie_env if args.manual_cookie_from_cdp else None,
            "manual_auth_header_count": len(args.manual_auth_header or []),
            "no_incognito": args.no_incognito,
        },
        "comparisons": _comparison_rows(results),
        "results": [result.__dict__ for result in results],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _status_text(result: ExternalResult | None) -> str:
    if result is None:
        return "not run"
    if result.passed:
        return f"completed, {result.entry_count} URLs, {result.elapsed_seconds:.1f}s"
    return f"{result.status or 'error'}: {result.error or 'failed'}"


def write_report(path: Path, results: list[ExternalResult], args: argparse.Namespace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    pairs = _site_pairs(results)
    rows = _comparison_rows(results)
    lines = [
        "# External Site Auth Comparison Report",
        "",
        f"Generated: `{generated_at}`",
        "",
        "## Run Configuration",
        "",
        f"- Crawl duration: `{args.crawl_duration}`.",
        f"- Max depth/pages: `{args.max_depth}` / `{args.max_pages}`.",
        f"- Headless Katana hybrid mode: `{args.headless}`.",
        f"- Artifact directory: `{getattr(args, 'artifact_dir', None)}`.",
        f"- Job DB: `{getattr(args, 'db_path', None)}`.",
        f"- Log directory: `{getattr(args, 'log_dir', None)}`.",
        f"- CDP URL: `{args.cdp_url}`.",
        f"- Manual no-auth cookie from CDP: `{args.manual_cookie_from_cdp}`.",
        f"- Manual no-auth header count: `{len(args.manual_auth_header or [])}`.",
        f"- Katana no-incognito: `{args.no_incognito}`.",
        (
            "- Public auth-agent runs use `login_url` only and instructions to finish if "
            "no login exists."
        ),
        (
            "- CMT credentials were supplied through environment variables and are not "
            "written to this report."
        ),
        "",
        "## Exact Sitemap Comparison",
        "",
        "| Site | No auth | Auth agent | Exact same sitemap? | Only no-auth | Only auth-agent |",
        "| --- | --- | --- | --- | ---: | ---: |",
    ]
    for row in rows:
        variants = pairs[row["site"]]
        lines.append(
            f"| `{row['site']}` | {_status_text(variants.get('no_auth'))} | "
            f"{_status_text(variants.get('auth_agent'))} | "
            f"{'yes' if row['identical'] else 'no'} | "
            f"{len(row['only_no_auth'])} | {len(row['only_auth_agent'])} |"
        )

    lines.extend(["", "## URLs", ""])
    for site in sorted(pairs):
        lines.append(f"### {site}")
        for variant in ("no_auth", "auth_agent"):
            result = pairs[site].get(variant)
            lines.append("")
            lines.append(f"#### {variant}")
            if result is None:
                lines.append("- not run")
                continue
            lines.append(f"- status: `{result.status}`")
            lines.append(f"- hash: `{result.sitemap_hash}`")
            lines.append(f"- url count: `{result.entry_count}`")
            if result.error:
                lines.append(f"- error: `{result.error}`")
            if result.log_path:
                lines.append(f"- proxify log: `{result.log_path}`")
            if result.katana_log_path:
                lines.append(f"- katana log: `{result.katana_log_path}`")
            if result.stderr_log_path:
                lines.append(f"- stderr log: `{result.stderr_log_path}`")
            if result.generated_exclusions:
                lines.append("- generated exclusions:")
                auth_count = result.generated_exclusions.get("auth_blocked_url_count")
                lines.append(f"  - auth blocked URL count: `{auth_count}`")
                discovered_count = result.generated_exclusions.get("auth_discovered_url_count")
                if discovered_count is not None:
                    lines.append(f"  - auth discovered URL count: `{discovered_count}`")
                dynamic_patterns = result.generated_exclusions.get("auth_dynamic_patterns")
                if isinstance(dynamic_patterns, list) and dynamic_patterns:
                    lines.append("  - auth dynamic patterns:")
                    for pattern in dynamic_patterns:
                        lines.append(f"    - `{pattern}`")
                discovered_urls = result.generated_exclusions.get("auth_discovered_urls")
                if isinstance(discovered_urls, list) and discovered_urls:
                    lines.append("  - auth discovered URLs:")
                    for url in discovered_urls:
                        lines.append(f"    - {url}")
                extra_seed_urls = result.generated_exclusions.get("extra_seed_urls")
                if isinstance(extra_seed_urls, list) and extra_seed_urls:
                    lines.append("  - extra seed URLs:")
                    for url in extra_seed_urls:
                        lines.append(f"    - {url}")
                effective_patterns = result.generated_exclusions.get("effective_patterns")
                if isinstance(effective_patterns, list) and effective_patterns:
                    lines.append("  - effective patterns:")
                    for pattern in effective_patterns:
                        lines.append(f"    - `{pattern}`")
            for url in result.urls:
                lines.append(f"- {url}")
            if not result.urls:
                lines.append("- no URLs")
        row = next(item for item in rows if item["site"] == site)
        if row["only_no_auth"] or row["only_auth_agent"]:
            lines.append("")
            lines.append("#### Delta")
            if row["only_no_auth"]:
                lines.append("")
                lines.append("Only no-auth:")
                for url in row["only_no_auth"]:
                    lines.append(f"- {url}")
            if row["only_auth_agent"]:
                lines.append("")
                lines.append("Only auth-agent:")
                for url in row["only_auth_agent"]:
                    lines.append(f"- {url}")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare external crawls with and without auth.")
    parser.add_argument("--include-cmt", action="store_true")
    parser.add_argument(
        "--site",
        action="append",
        choices=[
            "quotes-to-scrape",
            "webscraper-ecommerce-static",
            "acepace",
            "cmt",
        ],
        default=[],
        help="Run one site. Repeatable. Defaults to public sites, plus CMT with --include-cmt.",
    )
    parser.add_argument(
        "--variant",
        action="append",
        choices=["no_auth", "auth_agent"],
        default=[],
        help="Run one variant. Repeatable. Defaults to both variants.",
    )
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument("--json-output", default=str(JSON_PATH))
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--rate-limit", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--parallelism", type=int, default=5)
    parser.add_argument("--crawl-duration", default="20s")
    parser.add_argument("--request-timeout", type=int, default=15)
    parser.add_argument("--subprocess-timeout", type=int, default=90)
    parser.add_argument("--auth-attempts", type=int, default=1)
    parser.add_argument("--job-timeout", type=float, default=240.0)
    parser.add_argument("--cancel-timeout", type=float, default=20.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--cdp-url",
        help="CDP WebSocket URL for Katana and optional manual cookie extraction.",
    )
    parser.add_argument(
        "--manual-cookie-from-cdp",
        action="store_true",
        help=(
            "Read same-site cookies from --cdp-url and pass them to no_auth as "
            "a header-only auth_config using an env-var reference."
        ),
    )
    parser.add_argument(
        "--manual-cookie-env",
        default="CRAWLER_MANUAL_COOKIE",
        help="Environment variable name used to hand CDP cookies to manual no_auth mode.",
    )
    parser.add_argument(
        "--manual-auth-header",
        action="append",
        default=[],
        help=(
            "Header string to pass to no_auth as header-only auth_config. "
            "Use env templates for secrets, e.g. 'Cookie: {{env:CMT_COOKIE}}'."
        ),
    )
    parser.add_argument(
        "--no-incognito",
        action="store_true",
        help="Pass Katana -no-incognito in headless mode, useful with --cdp-url.",
    )
    parser.add_argument(
        "--artifact-dir",
        help=(
            "Directory for the jobs DB and logs. Defaults to a preserved "
            "/tmp/crawler-external-comparison-* directory."
        ),
    )
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    needs_cmt_password = (
        args.include_cmt
        and (not args.site or "cmt" in args.site)
        and (not args.variant or "auth_agent" in args.variant)
    )
    if needs_cmt_password and not os.environ.get("CMT_PASSWORD"):
        print("CMT_PASSWORD is required when --include-cmt is set")
        return 2
    cases = build_cases(
        include_cmt=args.include_cmt,
        sites=args.site,
        variants=args.variant,
    )
    await _prepare_manual_cookie_header(args, cases)
    cases = _apply_manual_no_auth_config(cases, args)
    if not cases:
        print("No external cases selected.")
        return 2
    results = await run_cases(cases, args)
    write_json(Path(args.json_output), results, args)
    write_report(Path(args.report), results, args)
    failed = [result for result in results if not result.passed]
    print(f"Wrote {args.report}")
    print(f"Wrote {args.json_output}")
    return 1 if failed else 0


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(async_main()))
    raise SystemExit(130)


if __name__ == "__main__":
    main()
