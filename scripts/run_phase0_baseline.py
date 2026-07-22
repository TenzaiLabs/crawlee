from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse

import httpx

from scripts.run_auth_agent_tests import AuthAgentSiteCase
from scripts.run_crawler_auth_tests import (
    _build_scope_config,
    _poll_job,
    _site_name_for_case,
)
from scripts.run_testsite_comparison import _canonical_cases

ROOT = Path(__file__).resolve().parent.parent
LOCAL_TARGETS_PATH = ROOT / "testsites" / "targets.json"
EXTERNAL_TARGETS_PATH = ROOT / "testsites" / "external" / "targets.json"
REPORT_PATH = ROOT / "docs" / "phase0-baseline-report.md"
JSON_PATH = ROOT / "docs" / "phase0-baseline-results.json"


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    seed_url: str
    health_url: str
    health_status: int
    reset: str | dict[str, Any]
    auth_reference: str | None
    allowed_origins: tuple[str, ...]
    sitemap: Path | None
    version: str
    score_setup: dict[str, Any] | None = None
    score_results_url: str | None = None
    expected_entries: tuple[tuple[str, str], ...] = ()


@dataclass
class BaselineResult:
    target: str
    kind: str
    version: str
    ready: bool
    health_status: int | None
    crawl_status: str | None
    auth_mode: str
    entry_count: int
    expected_entry_count: int
    expected_entries_found: int
    browser_only_entry_count: int
    browser_only_entries_found: int
    blocked_hits: list[str]
    methods: dict[str, int]
    status_codes: dict[str, int]
    sample_paths: list[str]
    elapsed_seconds: float
    score: dict[str, Any] | None = None
    error: str | None = None
    job_id: str | None = None
    sitemap_sha256: str | None = None
    persisted_result_verified: bool = False
    isolation_violations: list[str] = field(default_factory=list)


@dataclass
class ServerProcess:
    process: asyncio.subprocess.Process
    base_url: str
    log_path: Path
    log_file: BinaryIO


def _load_manifest(path: Path, *, required: bool) -> list[Target]:
    if not path.exists():
        if required:
            raise ValueError(f"required target manifest is missing: {path}")
        return []
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1 or not isinstance(data.get("targets"), list):
        raise ValueError(f"invalid target manifest: {path}")

    targets: list[Target] = []
    required_fields = {
        "name",
        "kind",
        "seed_url",
        "health_url",
        "health_status",
        "reset",
        "auth_reference",
        "allowed_origins",
        "sitemap",
        "version",
    }
    for raw in data["targets"]:
        if not isinstance(raw, dict):
            raise ValueError(f"target entry is not an object in {path}")
        missing = sorted(required_fields - raw.keys())
        if missing:
            raise ValueError(f"target {raw.get('name', '<unknown>')} missing fields: {missing}")
        sitemap_value = raw.get("sitemap")
        sitemap = ROOT / sitemap_value if isinstance(sitemap_value, str) else None
        target = Target(
            name=str(raw["name"]),
            kind=str(raw["kind"]),
            seed_url=str(raw["seed_url"]),
            health_url=str(raw["health_url"]),
            health_status=int(raw["health_status"]),
            reset=raw["reset"],
            auth_reference=(
                str(raw["auth_reference"]) if raw["auth_reference"] is not None else None
            ),
            allowed_origins=tuple(str(value) for value in raw["allowed_origins"]),
            sitemap=sitemap,
            version=str(raw["version"]),
            score_setup=raw.get("score_setup"),
            score_results_url=raw.get("score_results_url"),
            expected_entries=tuple(
                (str(entry.get("method", "GET")).upper(), str(entry["path"]))
                for entry in raw.get("expected_entries", [])
                if isinstance(entry, dict) and isinstance(entry.get("path"), str)
            ),
        )
        _validate_target(target)
        targets.append(target)
    return targets


def _validate_target(target: Target) -> None:
    seed_origin = _origin(target.seed_url)
    if seed_origin not in target.allowed_origins:
        raise ValueError(f"target {target.name} seed origin is not allowlisted")
    if _origin(target.health_url) not in target.allowed_origins:
        raise ValueError(f"target {target.name} health origin is not allowlisted")
    lifecycle_urls: list[tuple[str, str]] = []
    if isinstance(target.reset, dict):
        lifecycle_urls.append(("reset", str(target.reset.get("url", ""))))
    if target.score_setup is not None:
        lifecycle_urls.append(("score setup", str(target.score_setup.get("url", ""))))
    if target.score_results_url is not None:
        lifecycle_urls.append(("score results", target.score_results_url))
    for label, url in lifecycle_urls:
        if _origin(url) not in target.allowed_origins:
            raise ValueError(f"target {target.name} {label} origin is not allowlisted")
    if target.sitemap is not None and not target.sitemap.is_file():
        raise ValueError(f"target {target.name} sitemap is missing: {target.sitemap}")
    if target.kind != "repository":
        lowered = target.version.lower()
        if any(floating in lowered for floating in ("latest", "main", "master")):
            raise ValueError(
                f"external target {target.name} uses floating version {target.version}"
            )


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _path(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _same_url(left: str, right: str) -> bool:
    return _origin(left) == _origin(right) and _path(left) == _path(right)


def _manifest_entries(target: Target, field: str) -> set[tuple[str, str]]:
    if target.sitemap is None:
        return set(target.expected_entries) if field == "entries" else set()
    data = json.loads(target.sitemap.read_text())
    if field == "browser_only_entries":
        raw_entries = data.get("browser_discovery", {}).get(field, [])
        return {
            (str(entry.get("method", "GET")).upper(), str(entry["path"]))
            for entry in raw_entries
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
    raw_entries = data.get(field, [])
    return {
        (str(entry.get("method", "GET")).upper(), _path(str(entry["url"])))
        for entry in raw_entries
        if isinstance(entry, dict) and isinstance(entry.get("url"), str)
    }


def _crawl_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    sitemap = payload.get("sitemap")
    entries = sitemap.get("entries", []) if isinstance(sitemap, dict) else []
    return [entry for entry in entries if isinstance(entry, dict)]


def _observed_identities(entries: list[dict[str, Any]]) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for entry in entries:
        url = entry.get("url")
        if isinstance(url, str):
            identities.add((str(entry.get("method", "GET")).upper(), _path(url)))
    return identities


def _sitemap_digest(payload: dict[str, Any]) -> str | None:
    sitemap = payload.get("sitemap")
    if not isinstance(sitemap, dict):
        return None
    encoded = json.dumps(sitemap, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _isolation_violations(
    target: Target,
    payload: dict[str, Any],
    entries: list[dict[str, Any]],
) -> list[str]:
    violations: set[str] = set()
    payload_target = payload.get("target_url")
    if not isinstance(payload_target, str) or not _same_url(payload_target, target.seed_url):
        violations.add(f"job target_url changed to {payload.get('target_url')!r}")
    for entry in entries:
        url = entry.get("url")
        if isinstance(url, str) and _origin(url) not in target.allowed_origins:
            violations.add(f"out-of-target entry {url}")
    return sorted(violations)


async def _request_from_spec(client: httpx.AsyncClient, spec: dict[str, Any]) -> httpx.Response:
    method = str(spec.get("method", "GET")).upper()
    url = str(spec["url"])
    headers = {str(key): str(value) for key, value in spec.get("headers", {}).items()}
    return await client.request(method, url, headers=headers, json=spec.get("json"))


def _ensure_response_origin(target: Target, response: httpx.Response, label: str) -> None:
    if _origin(str(response.url)) not in target.allowed_origins:
        raise RuntimeError(f"target {target.name} {label} redirected outside its allowlist")


async def _prepare_target(client: httpx.AsyncClient, target: Target) -> None:
    if isinstance(target.reset, dict):
        response = await _request_from_spec(client, target.reset)
        _ensure_response_origin(target, response, "reset")
        response.raise_for_status()
    elif target.reset.startswith("external-recreate:"):
        script_name = target.reset.removeprefix("external-recreate:")
        if Path(script_name).name != script_name or not script_name.endswith(".sh"):
            raise ValueError(f"target {target.name} has unsafe reset script name")
        script = ROOT / "testsites" / "external" / "scripts" / script_name
        if not script.is_file():
            raise ValueError(f"target {target.name} reset script is missing: {script}")
        result = await asyncio.to_thread(
            subprocess.run,
            [str(script)],
            cwd=script.parent.parent,
            text=True,
            capture_output=True,
            timeout=240,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"target {target.name} reset failed: {detail}")
    elif target.kind == "repository" and target.reset == "container-restart":
        service_name = "-".join(target.name.split("-")[:2])
        if not service_name or not service_name.replace("-", "").isalnum():
            raise ValueError(f"target {target.name} has unsafe Compose service name")
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "compose",
                "up",
                "-d",
                "--force-recreate",
                "--no-deps",
                service_name,
            ],
            cwd=ROOT / "testsites",
            text=True,
            capture_output=True,
            timeout=180,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"target {target.name} reset failed: {detail}")
    else:
        raise ValueError(f"target {target.name} has unsupported reset operation")
    if target.score_setup:
        response = await _request_from_spec(client, target.score_setup)
        _ensure_response_origin(target, response, "score setup")
        response.raise_for_status()


async def _check_health(client: httpx.AsyncClient, target: Target) -> int:
    response = await client.get(target.health_url, follow_redirects=True)
    _ensure_response_origin(target, response, "health check")
    if response.status_code != target.health_status:
        raise RuntimeError(
            f"health returned {response.status_code}, expected {target.health_status}"
        )
    return response.status_code


async def _wait_for_target(
    client: httpx.AsyncClient,
    target: Target,
    *,
    timeout: float = 180.0,
) -> int:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await _check_health(client, target)
        except (httpx.HTTPError, RuntimeError) as exc:
            last_error = exc
            await asyncio.sleep(1)
    raise RuntimeError(f"target did not become ready: {last_error}")


def _generic_case(target: Target) -> AuthAgentSiteCase:
    return AuthAgentSiteCase(
        name=target.name,
        target_url=target.seed_url,
        auth_config=None,
        probe_path="/",
        mode="public",
    )


async def _crawl_one(
    client: httpx.AsyncClient,
    target: Target,
    case: AuthAgentSiteCase,
    *,
    scope_config: dict[str, Any],
    poll_interval: float,
    job_timeout: float,
    cancel_timeout: float,
    health_client: httpx.AsyncClient,
) -> BaselineResult:
    started = time.monotonic()
    health_status: int | None = None
    job_id: str | None = None
    try:
        await _prepare_target(health_client, target)
        health_status = await _wait_for_target(health_client, target)
        response = await client.post(
            "/jobs",
            json={
                "target_url": target.seed_url,
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
        entries = _crawl_entries(payload)
        observed = _observed_identities(entries)
        expected = _manifest_entries(target, "entries")
        browser_only = _manifest_entries(target, "browser_only_entries")
        blocked = _manifest_entries(target, "blocked_entries")
        blocked_hits = sorted(f"{method} {path}" for method, path in observed & blocked)
        isolation_violations = _isolation_violations(target, payload, entries)
        methods = Counter(str(entry.get("method", "GET")).upper() for entry in entries)
        status_codes = Counter(
            str(entry["status"]) for entry in entries if isinstance(entry.get("status"), int)
        )
        score: dict[str, Any] | None = None
        if target.score_results_url:
            score_response = await health_client.get(target.score_results_url)
            _ensure_response_origin(target, score_response, "score results")
            score_response.raise_for_status()
            score_payload = score_response.json()
            score = score_payload if isinstance(score_payload, dict) else {"results": score_payload}
        status = str(payload.get("status")) if payload.get("status") is not None else None
        error = payload.get("error")
        if blocked_hits:
            error = f"blocked routes were observed: {', '.join(blocked_hits)}"
        if isolation_violations:
            error = f"cross-job isolation failed: {'; '.join(isolation_violations)}"
        return BaselineResult(
            target=target.name,
            kind=target.kind,
            version=target.version,
            ready=True,
            health_status=health_status,
            crawl_status=status,
            auth_mode=case.mode,
            entry_count=len(entries),
            expected_entry_count=len(expected),
            expected_entries_found=len(observed & expected),
            browser_only_entry_count=len(browser_only),
            browser_only_entries_found=len(observed & browser_only),
            blocked_hits=blocked_hits,
            methods=dict(sorted(methods.items())),
            status_codes=dict(sorted(status_codes.items())),
            sample_paths=sorted(
                {_path(str(entry["url"])) for entry in entries if entry.get("url")}
            )[:12],
            elapsed_seconds=round(time.monotonic() - started, 3),
            score=score,
            error=str(error) if error else None,
            job_id=job_id,
            sitemap_sha256=_sitemap_digest(payload),
            isolation_violations=isolation_violations,
        )
    except Exception as exc:
        return BaselineResult(
            target=target.name,
            kind=target.kind,
            version=target.version,
            ready=False,
            health_status=health_status,
            crawl_status=None,
            auth_mode=case.mode,
            entry_count=0,
            expected_entry_count=len(_manifest_entries(target, "entries")),
            expected_entries_found=0,
            browser_only_entry_count=len(_manifest_entries(target, "browser_only_entries")),
            browser_only_entries_found=0,
            blocked_hits=[],
            methods={},
            status_codes={},
            sample_paths=[],
            elapsed_seconds=round(time.monotonic() - started, 3),
            error=f"{type(exc).__name__}: {exc}",
            job_id=job_id,
        )


def _reserve_server_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _server_environment(args: argparse.Namespace, temp_dir: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "CRAWLER_DB_PATH": str(temp_dir / "jobs.db"),
            "CRAWLER_LOG_DIR": str(temp_dir / "logs"),
            "CRAWLER_SUBPROCESS_TIMEOUT": str(args.subprocess_timeout),
            "CRAWLER_AUTH_ATTEMPTS": str(args.auth_attempts),
            "CRAWLER_HOST": "127.0.0.1",
            "CRAWLER_PORT": str(port),
        }
    )
    return env


def _server_log_tail(server: ServerProcess, *, lines: int = 80) -> str:
    with contextlib.suppress(OSError):
        return "\n".join(server.log_path.read_text(errors="replace").splitlines()[-lines:])
    return "server log unavailable"


async def _start_server(args: argparse.Namespace, temp_dir: Path) -> ServerProcess:
    port = _reserve_server_port()
    log_path = temp_dir / f"server-{port}.log"
    log_file = log_path.open("wb")
    try:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "tenzai-crawler-server",
            cwd=ROOT,
            env=_server_environment(args, temp_dir, port),
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        log_file.close()
        raise
    return ServerProcess(
        process=process,
        base_url=f"http://127.0.0.1:{port}",
        log_path=log_path,
        log_file=log_file,
    )


async def _wait_for_server(server: ServerProcess, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=server.base_url, timeout=2) as client:
        while time.monotonic() < deadline:
            if server.process.returncode is not None:
                raise RuntimeError(
                    f"crawler server exited with {server.process.returncode}:\n"
                    f"{_server_log_tail(server)}"
                )
            try:
                response = await client.get("/openapi.json")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    raise RuntimeError(f"crawler server readiness timed out:\n{_server_log_tail(server)}")


async def _stop_server(server: ServerProcess, *, timeout: float) -> None:
    forced = False
    try:
        if server.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(server.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(server.process.wait(), timeout=timeout)
            except TimeoutError:
                forced = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(server.process.pid, signal.SIGKILL)
                await server.process.wait()
    finally:
        server.log_file.close()
    log_tail = _server_log_tail(server)
    clean_uvicorn_shutdown = (
        "Application shutdown complete." in log_tail and "Finished server process" in log_tail
    )
    if forced or not clean_uvicorn_shutdown or server.process.returncode not in {0, 143}:
        raise RuntimeError(
            f"crawler server did not shut down cleanly ({server.process.returncode}):\n{log_tail}"
        )


async def _verify_persisted_results(
    client: httpx.AsyncClient,
    results: list[BaselineResult],
    targets: list[Target],
) -> None:
    target_urls = {target.name: target.seed_url for target in targets}
    expected_ids = {result.job_id for result in results if result.job_id is not None}
    response = await client.get("/jobs", params={"limit": 250})
    response.raise_for_status()
    listing = response.json()
    listed_ids = {job.get("job_id") for job in listing.get("jobs", []) if isinstance(job, dict)}
    if not expected_ids.issubset(listed_ids):
        missing = sorted(expected_ids - listed_ids)
        raise RuntimeError(f"persisted job listing is missing job IDs: {missing}")

    for result in results:
        if result.job_id is None:
            continue
        response = await client.get(f"/jobs/{result.job_id}")
        response.raise_for_status()
        payload = response.json()
        digest = _sitemap_digest(payload)
        payload_target = payload.get("target_url")
        if not isinstance(payload_target, str) or not _same_url(
            payload_target, target_urls[result.target]
        ):
            result.error = "persisted job target_url changed after server restart"
            continue
        if payload.get("status") != result.crawl_status:
            result.error = "persisted job status changed after server restart"
            continue
        if result.sitemap_sha256 != digest:
            result.error = "persisted sitemap changed after server restart"
            continue
        result.persisted_result_verified = True


def _compose(args: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=900,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {detail}")


def _start_targets(*, include_external: bool) -> None:
    env = os.environ.copy()
    _compose(["up", "-d", "--build", "--wait"], cwd=ROOT / "testsites", env=env)
    if include_external:
        _compose(["up", "-d", "--wait"], cwd=ROOT / "testsites" / "external", env=env)


def _stop_targets(*, include_external: bool) -> None:
    env = os.environ.copy()
    if include_external:
        _compose(["down"], cwd=ROOT / "testsites" / "external", env=env)
    _compose(["down"], cwd=ROOT / "testsites", env=env)


def _write_reports(results: list[BaselineResult], args: argparse.Namespace) -> None:
    generated_at = datetime.now(UTC).isoformat()
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "configuration": {
            "execution_boundary": "uv-run-server-http-api",
            "include_external": not args.local_only,
            "headless_katana": args.headless,
            "max_depth": args.max_depth,
            "max_pages": args.max_pages,
            "crawl_duration": args.crawl_duration,
        },
        "summary": {
            "targets": len(results),
            "ready": sum(result.ready for result in results),
            "completed": sum(result.crawl_status == "completed" for result in results),
            "entries": sum(result.entry_count for result in results),
            "browser_only_found": sum(result.browser_only_entries_found for result in results),
            "browser_only_declared": sum(result.browser_only_entry_count for result in results),
            "blocked_hits": sum(len(result.blocked_hits) for result in results),
            "persisted_results_verified": sum(
                result.persisted_result_verified for result in results
            ),
            "isolation_violations": sum(len(result.isolation_violations) for result in results),
        },
        "results": [asdict(result) for result in results],
    }
    JSON_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    summary = payload["summary"]
    crawlground = next((result for result in results if result.target == "crawlground"), None)
    crawlground_line: str | None = None
    if crawlground is not None and isinstance(crawlground.score, dict):
        score_summary = crawlground.score.get("summary")
        score_tests = crawlground.score.get("tests")
        if isinstance(score_summary, dict) and isinstance(score_tests, list):
            tool_scores = score_summary.get("tools", [])
            katana_score = next(
                (
                    item
                    for item in tool_scores
                    if isinstance(item, dict) and item.get("name") == "katana-baseline"
                ),
                None,
            )
            scored_ids = sorted(
                str(test["id"])
                for test in score_tests
                if isinstance(test, dict)
                and isinstance(test.get("tools"), dict)
                and isinstance(test["tools"].get("katana-baseline"), dict)
                and test["tools"]["katana-baseline"].get("scored") is True
                and isinstance(test.get("id"), str)
            )
            if isinstance(katana_score, dict):
                scored = int(katana_score.get("scored", 0))
                total = int(score_summary.get("total", 0))
                scored_percent = int(katana_score.get("scoredPercent", 0))
                controls = ", ".join(f"`{control}`" for control in scored_ids) or "none"
                crawlground_line = (
                    f"- CrawlGround: {scored}/{total} controls ({scored_percent}%); "
                    f"scored controls: {controls}."
                )

    lines = [
        "# Phase 0 Current Crawler Baseline",
        "",
        f"Generated: `{generated_at}`",
        "",
        (
            "This is the pre-browser-discovery result from a real `uv run "
            "tenzai-crawler-server` process, exercised only through its HTTP API. "
            "Browser-only coverage is expected to remain low or zero."
        ),
        "",
        "## Run summary",
        "",
        f"- Completed safely: {summary['completed']}/{summary['targets']} targets.",
        f"- Sitemap entries: {summary['entries']}.",
        (
            f"- Browser-only fixture controls found: {summary['browser_only_found']}/"
            f"{summary['browser_only_declared']}."
        ),
        (
            f"- Persisted results verified after server restart: "
            f"{summary['persisted_results_verified']}/{summary['targets']}."
        ),
        f"- Cross-job isolation violations: {summary['isolation_violations']}.",
        f"- Blocked-route hits: {summary['blocked_hits']}.",
    ]
    if crawlground_line is not None:
        lines.append(crawlground_line)
    lines.extend(
        [
            "",
            (
                "| Target | Ready | Crawl | Auth mode | Entries | Expected | Browser-only | "
                "Persisted | Isolation | Blocked | Seconds |"
            ),
            "| --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |",
        ]
    )
    for result in results:
        lines.append(
            f"| `{result.target}` | {'yes' if result.ready else 'no'} | "
            f"`{result.crawl_status or '-'}` | `{result.auth_mode}` | {result.entry_count} | "
            f"{result.expected_entries_found}/{result.expected_entry_count} | "
            f"{result.browser_only_entries_found}/{result.browser_only_entry_count} | "
            f"{'yes' if result.persisted_result_verified else 'no'} | "
            f"{len(result.isolation_violations)} | {len(result.blocked_hits)} | "
            f"{result.elapsed_seconds:.1f} |"
        )
    lines.extend(["", "## Errors and safety findings", ""])
    findings = [result for result in results if result.error or result.blocked_hits]
    if findings:
        for result in findings:
            lines.append(f"- `{result.target}`: {result.error or result.blocked_hits}")
    else:
        lines.append("No crawler failures or blocked-route hits were observed.")
    lines.extend(["", f"Machine-readable results: `{JSON_PATH.relative_to(ROOT)}`", ""])
    REPORT_PATH.write_text("\n".join(lines))


def _merge_existing_results(results: list[BaselineResult]) -> list[BaselineResult]:
    if not JSON_PATH.is_file():
        return results
    existing_payload = json.loads(JSON_PATH.read_text())
    merged: dict[str, BaselineResult] = {}
    for raw in existing_payload.get("results", []):
        if isinstance(raw, dict):
            existing = BaselineResult(**raw)
            merged[existing.target] = existing
    for result in results:
        merged[result.target] = result
    return list(merged.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start, verify, and baseline every controlled Phase 0 crawl target.",
    )
    parser.add_argument("--local-only", action="store_true", help="Exclude external targets.")
    parser.add_argument("--manage-targets", action="store_true", help="Start targets before run.")
    parser.add_argument(
        "--stop-targets", action="store_true", help="Stop managed targets after run."
    )
    parser.add_argument(
        "--merge-existing",
        action="store_true",
        help="Replace selected rows in the existing report instead of discarding it.",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Katana hybrid mode (default: enabled; use --no-headless to disable).",
    )
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--rate-limit", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--parallelism", type=int, default=10)
    parser.add_argument("--crawl-duration", default="5m")
    parser.add_argument("--request-timeout", type=int, default=10)
    parser.add_argument("--subprocess-timeout", type=int, default=360)
    parser.add_argument("--auth-attempts", type=int, default=1)
    parser.add_argument("--job-timeout", type=float, default=600.0)
    parser.add_argument("--cancel-timeout", type=float, default=20.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--api-request-timeout", type=float, default=30.0)
    parser.add_argument("--server-readiness-timeout", type=float, default=30.0)
    parser.add_argument("--server-shutdown-timeout", type=float, default=20.0)
    parser.add_argument("--case", action="append", default=[], help="Run only named targets.")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    include_external = not args.local_only
    targets = _load_manifest(LOCAL_TARGETS_PATH, required=True)
    targets.extend(_load_manifest(EXTERNAL_TARGETS_PATH, required=include_external))
    if args.case:
        selected = set(args.case)
        targets = [target for target in targets if target.name in selected]
    if not targets:
        raise ValueError("no Phase 0 targets selected")

    temp_root = tempfile.TemporaryDirectory(prefix="crawler-phase0-")
    temp_dir = Path(temp_root.name)
    targets_started = False
    server: ServerProcess | None = None
    try:
        if args.manage_targets:
            _start_targets(include_external=include_external)
            targets_started = True
        cases = {
            _site_name_for_case(case) or case.name: case for case in _canonical_cases(gateway=False)
        }
        scope_config = _build_scope_config(args)
        results: list[BaselineResult] = []
        server = await _start_server(args, temp_dir)
        await _wait_for_server(server, timeout=args.server_readiness_timeout)
        async with (
            httpx.AsyncClient(
                base_url=server.base_url,
                timeout=args.api_request_timeout,
            ) as app_client,
            httpx.AsyncClient(timeout=20, follow_redirects=True) as health_client,
        ):
            for target in targets:
                case = cases.get(target.name, _generic_case(target))
                print(f"phase0 {target.name} ({case.mode}) ...", flush=True)
                result = await _crawl_one(
                    app_client,
                    target,
                    case,
                    scope_config=scope_config,
                    poll_interval=args.poll_interval,
                    job_timeout=args.job_timeout,
                    cancel_timeout=args.cancel_timeout,
                    health_client=health_client,
                )
                print(
                    f"  ready={result.ready} status={result.crawl_status} "
                    f"entries={result.entry_count} "
                    f"expected={result.expected_entries_found}/{result.expected_entry_count} "
                    f"browser-only={result.browser_only_entries_found}/"
                    f"{result.browser_only_entry_count} "
                    f"isolation={len(result.isolation_violations)} "
                    f"blocked={len(result.blocked_hits)} elapsed={result.elapsed_seconds:.1f}s"
                    + (f" error={result.error}" if result.error else ""),
                    flush=True,
                )
                results.append(result)

        await _stop_server(server, timeout=args.server_shutdown_timeout)
        server = None
        server = await _start_server(args, temp_dir)
        await _wait_for_server(server, timeout=args.server_readiness_timeout)
        async with httpx.AsyncClient(
            base_url=server.base_url,
            timeout=args.api_request_timeout,
        ) as verification_client:
            await _verify_persisted_results(verification_client, results, targets)
        await _stop_server(server, timeout=args.server_shutdown_timeout)
        server = None

        if args.merge_existing:
            results = _merge_existing_results(results)
        _write_reports(results, args)
        failed = [
            result
            for result in results
            if (
                not result.ready
                or result.crawl_status != "completed"
                or result.blocked_hits
                or result.isolation_violations
                or not result.persisted_result_verified
            )
        ]
        print(
            f"phase0 summary: {len(results) - len(failed)}/{len(results)} "
            "targets completed safely; "
            f"report={REPORT_PATH.relative_to(ROOT)}",
            flush=True,
        )
        return 1 if failed else 0
    finally:
        if server is not None:
            with contextlib.suppress(Exception):
                await _stop_server(server, timeout=args.server_shutdown_timeout)
        if targets_started and args.stop_targets:
            with contextlib.suppress(Exception):
                _stop_targets(include_external=include_external)
        temp_root.cleanup()


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
