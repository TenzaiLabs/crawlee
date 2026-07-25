from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import json
import os
import secrets
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

from scripts.qualification_artifacts import prepare_server_home, sha256_file
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
    auth_config: dict[str, Any] | None = None


@dataclass(frozen=True)
class CapabilityMarker:
    marker_id: str
    lane: str
    method: str
    path: str
    evidence: str


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
    capability_marker_count: int = 0
    capability_markers_found: int = 0
    capability_by_lane: dict[str, str] = field(default_factory=dict)
    ledger_entry_count: int = 0
    ledger_required_count: int = 0
    ledger_destructive_count: int = 0
    missing_expected_entries: list[str] = field(default_factory=list)
    discovery_outcome: str | None = None
    discovery_rounds: int = 0
    discovery_new_entry_count: int = 0
    discovery_state_count: int = 0
    discovery_workflow_count: int = 0
    discovery_stop_reason: str | None = None
    request_sequence_count: int = 0
    request_sequences_found: int = 0


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
            auth_config=(
                copy.deepcopy(raw["auth_config"])
                if isinstance(raw.get("auth_config"), dict)
                else None
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


def _sitemap_metadata(target: Target) -> dict[str, Any]:
    if target.sitemap is None:
        return {}
    data = json.loads(target.sitemap.read_text())
    return data if isinstance(data, dict) else {}


def _capability_markers(target: Target) -> tuple[CapabilityMarker, ...]:
    raw_markers = _sitemap_metadata(target).get("capability_markers", [])
    markers: list[CapabilityMarker] = []
    for raw in raw_markers:
        if not isinstance(raw, dict):
            continue
        if not all(isinstance(raw.get(key), str) for key in ("id", "lane", "path")):
            continue
        markers.append(
            CapabilityMarker(
                marker_id=str(raw["id"]),
                lane=str(raw["lane"]),
                method=str(raw.get("method", "GET")).upper(),
                path=str(raw["path"]),
                evidence=str(raw.get("evidence", "endpoint")),
            )
        )
    return tuple(markers)


def _required_request_sequences(target: Target) -> tuple[tuple[str, ...], ...]:
    browser_discovery = _sitemap_metadata(target).get("browser_discovery", {})
    if not isinstance(browser_discovery, dict):
        return ()
    raw_sequences = browser_discovery.get("required_request_sequences", [])
    if not isinstance(raw_sequences, list):
        return ()
    sequences: list[tuple[str, ...]] = []
    for raw_sequence in raw_sequences:
        if not isinstance(raw_sequence, list):
            continue
        sequence = tuple(str(item).strip() for item in raw_sequence if str(item).strip())
        if sequence:
            sequences.append(sequence)
    return tuple(sequences)


def _request_sequence_results(
    target: Target,
    ledger_entries: list[dict[str, Any]],
) -> tuple[int, int]:
    sequences = _required_request_sequences(target)
    observed = [
        f"{str(entry.get('method', 'GET')).upper()} {str(entry.get('route', '')).strip()}"
        for entry in ledger_entries
        if isinstance(entry, dict) and str(entry.get("route", "")).strip()
    ]

    def contains_in_order(sequence: tuple[str, ...]) -> bool:
        position = 0
        for request_key in observed:
            if request_key == sequence[position]:
                position += 1
                if position == len(sequence):
                    return True
        return False

    return len(sequences), sum(contains_in_order(sequence) for sequence in sequences)


def _ledger_config(target: Target) -> dict[str, str] | None:
    raw = _sitemap_metadata(target).get("ledger")
    if not isinstance(raw, dict):
        return None
    required = ("run_header", "url_template", "harness_header")
    if not all(isinstance(raw.get(key), str) and raw[key] for key in required):
        raise ValueError(f"target {target.name} has an invalid ledger configuration")
    return {key: str(raw[key]) for key in required}


def _capability_results(
    target: Target,
    observed: set[tuple[str, str]],
    form_observed: set[tuple[str, str]] | None = None,
) -> tuple[int, int, dict[str, str]]:
    markers = _capability_markers(target)
    form_identities = form_observed or set()

    def marker_found(marker: CapabilityMarker) -> bool:
        identities = form_identities if marker.evidence == "form" else observed
        return (marker.method, marker.path) in identities

    lane_totals: Counter[str] = Counter(marker.lane for marker in markers)
    lane_found: Counter[str] = Counter(marker.lane for marker in markers if marker_found(marker))
    by_lane = {lane: f"{lane_found[lane]}/{total}" for lane, total in sorted(lane_totals.items())}
    found = sum(marker_found(marker) for marker in markers)
    return len(markers), found, by_lane


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


def _observed_form_identities(payload: dict[str, Any]) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    evidence = payload.get("evidence")
    katana = evidence.get("katana") if isinstance(evidence, dict) else None
    if not isinstance(katana, dict):
        return identities
    for lane in katana.values():
        records = lane.get("records") if isinstance(lane, dict) else None
        if not isinstance(records, list):
            continue
        for record in records:
            features = record.get("features") if isinstance(record, dict) else None
            if not isinstance(features, dict):
                continue
            forms = features.get("forms") or features.get("form")
            if isinstance(forms, dict):
                forms = [forms]
            if not isinstance(forms, list):
                continue
            for form in forms:
                if not isinstance(form, dict) or not isinstance(form.get("action"), str):
                    continue
                identities.add(
                    (
                        str(form.get("method", "GET")).upper(),
                        _path(form["action"]),
                    )
                )
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


async def _prepare_target(
    client: httpx.AsyncClient,
    target: Target,
    harness_token: str | None = None,
) -> None:
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
        compose_env = os.environ.copy()
        if harness_token:
            compose_env["TEST_HARNESS_TOKEN"] = harness_token
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
            env=compose_env,
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
        auth_config=copy.deepcopy(target.auth_config),
        probe_path="/",
        mode="manual_headers" if target.auth_config else "public",
    )


def _auth_config_for_run(
    target: Target,
    case: AuthAgentSiteCase,
    run_id: str | None,
) -> dict[str, Any] | None:
    source = case.auth_config if case.auth_config is not None else target.auth_config
    auth_config = copy.deepcopy(source) if source is not None else None
    ledger = _ledger_config(target)
    if ledger is None or run_id is None:
        return auth_config
    if auth_config is None:
        auth_config = {}
    headers = auth_config.setdefault("headers", [])
    if not isinstance(headers, list):
        raise ValueError(f"target {target.name} auth headers must be a list")
    headers.append(f"{ledger['run_header']}: {run_id}")
    return auth_config


async def _read_ledger(
    client: httpx.AsyncClient,
    target: Target,
    run_id: str | None,
    harness_token: str | None,
) -> list[dict[str, Any]]:
    ledger = _ledger_config(target)
    if ledger is None:
        return []
    if run_id is None or not harness_token:
        raise RuntimeError(f"target {target.name} requires TEST_HARNESS_TOKEN for its ledger")
    url = ledger["url_template"].format(run_id=run_id)
    if _origin(url) not in target.allowed_origins:
        raise ValueError(f"target {target.name} ledger origin is not allowlisted")
    response = await client.get(url, headers={ledger["harness_header"]: harness_token})
    _ensure_response_origin(target, response, "ledger")
    response.raise_for_status()
    payload = response.json()
    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    return [entry for entry in entries if isinstance(entry, dict)]


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
    harness_token: str | None,
    discovery_enabled: bool = False,
) -> BaselineResult:
    started = time.monotonic()
    health_status: int | None = None
    job_id: str | None = None
    run_id = secrets.token_hex(12) if _ledger_config(target) is not None else None
    try:
        await _prepare_target(health_client, target, harness_token)
        health_status = await _wait_for_target(health_client, target)
        response = await client.post(
            "/jobs",
            json={
                "target_url": target.seed_url,
                "scope_config": scope_config,
                "auth_config": _auth_config_for_run(target, case, run_id),
                "discovery": {"enabled": discovery_enabled},
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
        observed_forms = _observed_form_identities(payload)
        expected = _manifest_entries(target, "entries")
        browser_only = _manifest_entries(target, "browser_only_entries")
        blocked = _manifest_entries(target, "blocked_entries")
        blocked_hits = sorted(f"{method} {path}" for method, path in observed & blocked)
        capability_count, capability_found, capability_by_lane = _capability_results(
            target,
            observed,
            observed_forms,
        )
        ledger_entries = await _read_ledger(
            health_client,
            target,
            run_id,
            harness_token,
        )
        ledger_required = sum(entry.get("classification") == "required" for entry in ledger_entries)
        ledger_destructive = sum(
            entry.get("classification") in {"forbidden", "destructive-marker"}
            for entry in ledger_entries
        )
        request_sequence_count, request_sequences_found = _request_sequence_results(
            target,
            ledger_entries,
        )
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
        discovery = payload.get("discovery_result")
        if not isinstance(discovery, dict):
            discovery = {}
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
            capability_marker_count=capability_count,
            capability_markers_found=capability_found,
            capability_by_lane=capability_by_lane,
            ledger_entry_count=len(ledger_entries),
            ledger_required_count=ledger_required,
            ledger_destructive_count=ledger_destructive,
            missing_expected_entries=sorted(
                f"{method} {path}" for method, path in expected - observed
            ),
            discovery_outcome=(
                str(discovery["outcome"]) if discovery.get("outcome") is not None else None
            ),
            discovery_rounds=int(discovery.get("rounds") or 0),
            discovery_new_entry_count=int(discovery.get("new_entry_count") or 0),
            discovery_state_count=int(discovery.get("state_count") or 0),
            discovery_workflow_count=int(discovery.get("workflow_count") or 0),
            discovery_stop_reason=(
                str(discovery["stop_reason"]) if discovery.get("stop_reason") is not None else None
            ),
            request_sequence_count=request_sequence_count,
            request_sequences_found=request_sequences_found,
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
            missing_expected_entries=sorted(
                f"{method} {path}" for method, path in _manifest_entries(target, "entries")
            ),
        )


def _reserve_server_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _server_environment(
    args: argparse.Namespace,
    temp_dir: Path,
    port: int,
    harness_token: str | None = None,
    *,
    home_dir: Path | None = None,
) -> dict[str, str]:
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
    if harness_token:
        env["TEST_HARNESS_TOKEN"] = harness_token
    if home_dir is not None:
        env["HOME"] = str(home_dir)
        private_bin = home_dir / "bin"
        if private_bin.is_dir():
            env["PATH"] = f"{private_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def _server_log_tail(server: ServerProcess, *, lines: int = 80) -> str:
    with contextlib.suppress(OSError):
        return "\n".join(server.log_path.read_text(errors="replace").splitlines()[-lines:])
    return "server log unavailable"


async def _start_server(
    args: argparse.Namespace,
    temp_dir: Path,
    harness_token: str | None = None,
    *,
    home_dir: Path | None = None,
) -> ServerProcess:
    port = _reserve_server_port()
    log_path = temp_dir / f"server-{port}.log"
    log_file = log_path.open("wb")
    try:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "tenzai-crawler-server",
            cwd=ROOT,
            env=_server_environment(
                args,
                temp_dir,
                port,
                harness_token,
                home_dir=home_dir,
            ),
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


def _start_targets(*, include_external: bool, harness_token: str | None = None) -> None:
    env = os.environ.copy()
    if harness_token:
        env["TEST_HARNESS_TOKEN"] = harness_token
    _compose(["up", "-d", "--build", "--wait"], cwd=ROOT / "testsites", env=env)
    if include_external:
        _compose(["up", "-d", "--wait"], cwd=ROOT / "testsites" / "external", env=env)


def _stop_targets(*, include_external: bool) -> None:
    env = os.environ.copy()
    if include_external:
        _compose(["down"], cwd=ROOT / "testsites" / "external", env=env)
    _compose(["down"], cwd=ROOT / "testsites", env=env)


def _qualification_failures(
    results: list[BaselineResult],
    *,
    discovery_enabled: bool,
) -> list[str]:
    failures: list[str] = []
    for result in results:
        if not result.ready:
            failures.append(f"{result.target}: target was not ready")
        if result.isolation_violations:
            failures.append(f"{result.target}: cross-job isolation violation")
        if not result.persisted_result_verified:
            failures.append(f"{result.target}: persisted result was not verified")

        # Public websites are observation-only canaries. Their crawl outcome is
        # reported, but only repository-controlled fixtures are release gates.
        if result.kind != "repository":
            continue
        if result.crawl_status != "completed":
            failures.append(
                f"{result.target}: controlled crawl status was {result.crawl_status or 'missing'}"
            )
            continue
        if not discovery_enabled:
            continue
        if result.discovery_outcome != "fixpoint":
            failures.append(
                f"{result.target}: controlled discovery outcome was "
                f"{result.discovery_outcome or 'missing'}"
            )
        if result.browser_only_entries_found != result.browser_only_entry_count:
            failures.append(
                f"{result.target}: browser-only endpoints "
                f"{result.browser_only_entries_found}/{result.browser_only_entry_count}"
            )
        if result.capability_markers_found != result.capability_marker_count:
            failures.append(
                f"{result.target}: capability markers "
                f"{result.capability_markers_found}/{result.capability_marker_count}"
            )
        if result.request_sequences_found != result.request_sequence_count:
            failures.append(
                f"{result.target}: request sequences "
                f"{result.request_sequences_found}/{result.request_sequence_count}"
            )
        if result.request_sequence_count and result.discovery_workflow_count < 1:
            failures.append(f"{result.target}: no runtime-verified discovery workflow")
    return failures


def _write_reports(results: list[BaselineResult], args: argparse.Namespace) -> None:
    generated_at = datetime.now(UTC).isoformat()
    qualification_failures = _qualification_failures(
        results,
        discovery_enabled=args.discovery_enabled,
    )
    controlled = [result for result in results if result.kind == "repository"]
    public_canaries = [result for result in results if result.kind != "repository"]
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "configuration": {
            "execution_boundary": "uv-run-server-http-api",
            "capture_source": "known-files+standard-katana+pure-headless-katana+passive-cdp",
            "include_external": any(result.kind != "repository" for result in results),
            "merged_existing_results": bool(getattr(args, "merge_existing", False)),
            "discovery_enabled": args.discovery_enabled,
            "max_depth": args.max_depth,
            "crawl_duration": args.crawl_duration,
            "dit_model_sha256": sha256_file(args.dit_model),
            "katana_sha256": sha256_file(args.katana_binary),
        },
        "summary": {
            "targets": len(results),
            "ready": sum(result.ready for result in results),
            "completed": sum(result.crawl_status == "completed" for result in results),
            "controlled_targets": len(controlled),
            "controlled_completed": sum(
                result.crawl_status == "completed" for result in controlled
            ),
            "public_canaries": len(public_canaries),
            "public_canaries_completed": sum(
                result.crawl_status == "completed" for result in public_canaries
            ),
            "qualification_passed": not qualification_failures,
            "qualification_failures": qualification_failures,
            "entries": sum(result.entry_count for result in results),
            "browser_only_found": sum(result.browser_only_entries_found for result in results),
            "browser_only_declared": sum(result.browser_only_entry_count for result in results),
            "capability_markers_found": sum(result.capability_markers_found for result in results),
            "capability_markers_declared": sum(
                result.capability_marker_count for result in results
            ),
            "blocked_hits": sum(len(result.blocked_hits) for result in results),
            "ledger_entries": sum(result.ledger_entry_count for result in results),
            "ledger_required": sum(result.ledger_required_count for result in results),
            "ledger_destructive": sum(result.ledger_destructive_count for result in results),
            "request_sequences_found": sum(result.request_sequences_found for result in results),
            "request_sequences_declared": sum(result.request_sequence_count for result in results),
            "persisted_results_verified": sum(
                result.persisted_result_verified for result in results
            ),
            "isolation_violations": sum(len(result.isolation_violations) for result in results),
            "discovery_fixpoints": sum(
                result.discovery_outcome == "fixpoint" for result in results
            ),
            "discovery_new_entries": sum(result.discovery_new_entry_count for result in results),
            "discovery_workflows": sum(result.discovery_workflow_count for result in results),
        },
        "results": [asdict(result) for result in results],
    }
    args.json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

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
        (
            "# Step 9 Complete-System Qualification"
            if args.discovery_enabled
            else "# Step 6 Dual-Pass Katana Baseline"
        ),
        "",
        f"Generated: `{generated_at}`",
        "",
        (
            "This is the complete proxyless qualification"
            if args.discovery_enabled
            else "This is the proxyless dual-pass baseline"
        )
        + (
            " from a real `uv run tenzai-crawler-server` process, exercised only through "
            "its HTTP API. Every job uses bounded known-file discovery, standard Katana, "
            "shared-Chrome pure-headless Katana, and passive CDP evidence; guided LLM "
            f"discovery is {'enabled' if args.discovery_enabled else 'disabled'}."
        ),
        "",
        "## Run summary",
        "",
        f"- Completed safely: {summary['completed']}/{summary['targets']} targets.",
        (
            f"- Controlled release fixtures completed: "
            f"{summary['controlled_completed']}/{summary['controlled_targets']}."
        ),
        (
            f"- Observation-only public canaries completed: "
            f"{summary['public_canaries_completed']}/{summary['public_canaries']}."
        ),
        (
            "- Controlled qualification gate: "
            f"{'PASS' if summary['qualification_passed'] else 'FAIL'}."
        ),
        f"- Sitemap entries: {summary['entries']}.",
        (
            f"- Browser-only fixture controls found: {summary['browser_only_found']}/"
            f"{summary['browser_only_declared']}."
        ),
        (
            f"- Lane-specific capability markers found: "
            f"{summary['capability_markers_found']}/"
            f"{summary['capability_markers_declared']}."
        ),
        (
            f"- Required request sequences found: "
            f"{summary['request_sequences_found']}/"
            f"{summary['request_sequences_declared']}."
        ),
        (
            f"- Persisted results verified after server restart: "
            f"{summary['persisted_results_verified']}/{summary['targets']}."
        ),
        f"- Cross-job isolation violations: {summary['isolation_violations']}.",
        (
            f"- Discovery fixpoints: {summary['discovery_fixpoints']}/{summary['targets']} "
            f"with {summary['discovery_new_entries']} new entries."
        ),
        (f"- Declared destructive or session-ending markers observed: {summary['blocked_hits']}."),
        (
            f"- Fixture-ledger entries: {summary['ledger_entries']} "
            f"({summary['ledger_required']} required, "
            f"{summary['ledger_destructive']} destructive markers)."
        ),
    ]
    if crawlground_line is not None:
        lines.append(crawlground_line)
    lines.extend(
        [
            "",
            (
                "| Target | Ready | Crawl | Auth mode | Entries | Expected | Browser-only | "
                "Lane markers | Sequences | Outcome | New | Ledger | Persisted | Isolation | "
                "Destructive | Seconds |"
            ),
            (
                "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | "
                "---: | --- | ---: | ---: | ---: |"
            ),
        ]
    )
    for result in results:
        lines.append(
            f"| `{result.target}` | {'yes' if result.ready else 'no'} | "
            f"`{result.crawl_status or '-'}` | `{result.auth_mode}` | {result.entry_count} | "
            f"{result.expected_entries_found}/{result.expected_entry_count} | "
            f"{result.browser_only_entries_found}/{result.browser_only_entry_count} | "
            f"{result.capability_markers_found}/{result.capability_marker_count} | "
            f"{result.request_sequences_found}/{result.request_sequence_count} | "
            f"`{result.discovery_outcome or '-'}` | "
            f"{result.discovery_new_entry_count} | "
            f"{result.ledger_entry_count} | "
            f"{'yes' if result.persisted_result_verified else 'no'} | "
            f"{len(result.isolation_violations)} | "
            f"{max(len(result.blocked_hits), result.ledger_destructive_count)} | "
            f"{result.elapsed_seconds:.1f} |"
        )
    lines.extend(["", "## Errors and observed destructive markers", ""])
    findings = [
        result
        for result in results
        if result.error or result.blocked_hits or result.ledger_destructive_count
    ]
    if findings:
        for result in findings:
            detail = result.error or result.blocked_hits
            if not detail:
                detail = f"ledger destructive markers: {result.ledger_destructive_count}"
            lines.append(f"- `{result.target}`: {detail}")
    else:
        lines.append("No crawler failures or destructive-route markers were observed.")
    lines.extend(["", "## Missing expected endpoints", ""])
    missing_expected = [result for result in results if result.missing_expected_entries]
    if missing_expected:
        for result in missing_expected:
            entries = ", ".join(f"`{entry}`" for entry in result.missing_expected_entries)
            lines.append(f"- `{result.target}`: {entries}")
    else:
        lines.append("Every declared expected endpoint was observed.")
    lines.extend(["", "## Controlled qualification gate", ""])
    if qualification_failures:
        lines.extend(f"- {failure}" for failure in qualification_failures)
    else:
        lines.append(
            "PASS. Every repository-controlled fixture satisfied readiness, completion, "
            "persistence, isolation, discovery-fixpoint, endpoint, capability-marker, "
            "and request-sequence gates. Public canaries are reported but non-blocking."
        )
    try:
        json_display = args.json_path.relative_to(ROOT)
    except ValueError:
        json_display = args.json_path
    lines.extend(["", f"Machine-readable results: `{json_display}`", ""])
    args.report_path.write_text("\n".join(lines))


def _merge_existing_results(
    results: list[BaselineResult],
    json_path: Path,
) -> list[BaselineResult]:
    if not json_path.is_file():
        return results
    existing_payload = json.loads(json_path.read_text())
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
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--rate-limit", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--parallelism", type=int, default=10)
    parser.add_argument("--crawl-duration", default="10m")
    parser.add_argument("--request-timeout", type=int, default=10)
    parser.add_argument("--subprocess-timeout", type=int, default=720)
    parser.add_argument("--auth-attempts", type=int, default=1)
    parser.add_argument("--job-timeout", type=float, default=600.0)
    parser.add_argument("--cancel-timeout", type=float, default=20.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--api-request-timeout", type=float, default=30.0)
    parser.add_argument("--server-readiness-timeout", type=float, default=30.0)
    parser.add_argument("--server-shutdown-timeout", type=float, default=20.0)
    parser.add_argument("--case", action="append", default=[], help="Run only named targets.")
    parser.add_argument("--discovery-enabled", action="store_true")
    parser.add_argument("--dit-model", type=Path)
    parser.add_argument("--katana-binary", type=Path)
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--json-path", type=Path)
    parser.add_argument("--keep-artifacts", action="store_true")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    if args.report_path is None:
        args.report_path = (
            ROOT / "docs" / "complete-system-qualification.md"
            if args.discovery_enabled
            else REPORT_PATH
        )
    if args.json_path is None:
        args.json_path = (
            ROOT / "docs" / "complete-system-qualification.json"
            if args.discovery_enabled
            else JSON_PATH
        )
    include_external = not args.local_only
    targets = _load_manifest(LOCAL_TARGETS_PATH, required=True)
    targets.extend(_load_manifest(EXTERNAL_TARGETS_PATH, required=include_external))
    if args.case:
        selected = set(args.case)
        targets = [target for target in targets if target.name in selected]
    if not targets:
        raise ValueError("no Phase 0 targets selected")

    ledger_required = any(_ledger_config(target) is not None for target in targets)
    harness_token = os.environ.get("TEST_HARNESS_TOKEN")
    if ledger_required and not harness_token:
        if not args.manage_targets:
            raise ValueError(
                "TEST_HARNESS_TOKEN is required when using an already-running ledger fixture"
            )
        harness_token = secrets.token_urlsafe(32)

    temp_root = tempfile.TemporaryDirectory(
        prefix="crawler-phase0-",
        delete=not args.keep_artifacts,
    )
    temp_dir = Path(temp_root.name)
    server_home, args.dit_model, args.katana_binary = prepare_server_home(
        temp_dir,
        args.dit_model,
        args.katana_binary,
    )
    targets_started = False
    server: ServerProcess | None = None
    try:
        if args.manage_targets:
            _start_targets(
                include_external=include_external,
                harness_token=harness_token,
            )
            targets_started = True
        cases = {
            _site_name_for_case(case) or case.name: case for case in _canonical_cases(gateway=False)
        }
        scope_config = _build_scope_config(args)
        results: list[BaselineResult] = []
        server = await _start_server(args, temp_dir, harness_token, home_dir=server_home)
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
                    harness_token=harness_token,
                    discovery_enabled=args.discovery_enabled,
                )
                print(
                    f"  ready={result.ready} status={result.crawl_status} "
                    f"entries={result.entry_count} "
                    f"expected={result.expected_entries_found}/{result.expected_entry_count} "
                    f"browser-only={result.browser_only_entries_found}/"
                    f"{result.browser_only_entry_count} "
                    f"isolation={len(result.isolation_violations)} "
                    f"markers={len(result.blocked_hits)} elapsed={result.elapsed_seconds:.1f}s"
                    + (f" error={result.error}" if result.error else ""),
                    flush=True,
                )
                results.append(result)

        await _stop_server(server, timeout=args.server_shutdown_timeout)
        server = None
        server = await _start_server(args, temp_dir, harness_token, home_dir=server_home)
        await _wait_for_server(server, timeout=args.server_readiness_timeout)
        async with httpx.AsyncClient(
            base_url=server.base_url,
            timeout=args.api_request_timeout,
        ) as verification_client:
            await _verify_persisted_results(verification_client, results, targets)
        await _stop_server(server, timeout=args.server_shutdown_timeout)
        server = None

        if args.merge_existing:
            results = _merge_existing_results(results, args.json_path)
        _write_reports(results, args)
        qualification_failures = _qualification_failures(
            results,
            discovery_enabled=args.discovery_enabled,
        )
        completed = sum(result.crawl_status == "completed" for result in results)
        print(
            f"phase0 summary: {completed}/{len(results)} targets completed; "
            f"controlled qualification={'PASS' if not qualification_failures else 'FAIL'}; "
            f"report={args.report_path}",
            flush=True,
        )
        return 1 if qualification_failures else 0
    finally:
        if server is not None:
            with contextlib.suppress(Exception):
                await _stop_server(server, timeout=args.server_shutdown_timeout)
        if targets_started and args.stop_targets:
            with contextlib.suppress(Exception):
                _stop_targets(include_external=include_external)
        if args.keep_artifacts:
            print(f"kept phase qualification artifacts: {temp_dir}", flush=True)
        else:
            temp_root.cleanup()


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
