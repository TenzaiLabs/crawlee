from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.qualification_artifacts import prepare_server_home, sha256_file
from scripts.run_auth_agent_tests import AuthAgentSiteCase
from scripts.run_crawler_auth_tests import _build_scope_config, _poll_job, _site_name_for_case
from scripts.run_phase0_baseline import (
    LOCAL_TARGETS_PATH,
    ROOT,
    ServerProcess,
    Target,
    _auth_config_for_run,
    _crawl_entries,
    _isolation_violations,
    _load_manifest,
    _manifest_entries,
    _prepare_target,
    _read_ledger,
    _sitemap_digest,
    _start_server,
    _stop_server,
    _wait_for_server,
    _wait_for_target,
)
from scripts.run_testsite_comparison import _canonical_cases

REPORT_PATH = ROOT / "docs" / "browser-guided-discovery-qualification.md"
JSON_PATH = ROOT / "docs" / "browser-guided-discovery-qualification.json"
QUALIFICATION_TARGETS = {
    "site-b-login-flask",
    "site-e-crawl-trap-ruby",
    "site-f-spa-deno",
}
_sha256 = sha256_file


@dataclass
class QualificationResult:
    target: str
    repetition: int
    job_id: str | None
    status: str | None
    error: str | None
    entry_count: int
    baseline_entry_count: int
    baseline_subset_preserved: bool
    browser_only_found: int
    browser_only_total: int
    missing_browser_only: list[str]
    discovery_browser_only_found: int
    missing_discovery_browser_only: list[str]
    required_sequences_found: int
    required_sequences_total: int
    missing_required_sequences: list[list[str]]
    ledger_required_count: int
    ledger_destructive_count: int
    isolation_violations: list[str]
    discovery_outcome: str | None
    discovery_rounds: int
    discovery_new_entry_count: int
    discovery_state_count: int
    discovery_workflow_count: int
    discovery_stop_reason: str | None
    sitemap_sha256: str | None
    persisted_result_verified: bool = False


def _identity(method: Any, url: Any) -> tuple[str, str] | None:
    if not isinstance(url, str) or not url:
        return None
    return str(method or "GET").upper(), url


def _baseline_identities(payload: dict[str, Any]) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        return identities

    known_files = evidence.get("known_files")
    documents = known_files.get("documents") if isinstance(known_files, dict) else None
    if isinstance(documents, list):
        for document in documents:
            if isinstance(document, dict):
                value = _identity("GET", document.get("url"))
                if value is not None:
                    identities.add(value)

    katana = evidence.get("katana")
    if isinstance(katana, dict):
        for lane in katana.values():
            records = lane.get("records") if isinstance(lane, dict) else None
            if not isinstance(records, list):
                continue
            for record in records:
                if isinstance(record, dict):
                    value = _identity(record.get("method"), record.get("url"))
                    if value is not None:
                        identities.add(value)

    browser = evidence.get("browser")
    requests = browser.get("requests") if isinstance(browser, dict) else None
    if isinstance(requests, list):
        for request in requests:
            if not isinstance(request, dict):
                continue
            epoch = str(request.get("epoch") or "")
            if "discovery-" in epoch:
                continue
            value = _identity(request.get("method"), request.get("url"))
            if value is not None:
                identities.add(value)
    return identities


def _final_identities(payload: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        value
        for entry in _crawl_entries(payload)
        if (value := _identity(entry.get("method"), entry.get("url"))) is not None
    }


def _path_identity(method: str, url: str) -> tuple[str, str]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return method, path


def _discovery_request_sequence(payload: dict[str, Any]) -> list[tuple[str, str]]:
    evidence = payload.get("evidence")
    discovery = evidence.get("discovery") if isinstance(evidence, dict) else None
    rounds = discovery.get("rounds") if isinstance(discovery, dict) else None
    observed: list[tuple[str, str]] = []
    if not isinstance(rounds, list):
        return observed
    for round_record in rounds:
        requests = round_record.get("requests") if isinstance(round_record, dict) else None
        if not isinstance(requests, list):
            continue
        for request in requests:
            if not isinstance(request, dict):
                continue
            identity = _identity(request.get("method"), request.get("url"))
            if identity is not None:
                observed.append(_path_identity(*identity))
    return observed


def _required_request_sequences(target: Target) -> list[list[tuple[str, str]]]:
    if target.sitemap is None:
        return []
    payload = json.loads(target.sitemap.read_text())
    discovery = payload.get("browser_discovery") if isinstance(payload, dict) else None
    raw_sequences = (
        discovery.get("required_request_sequences") if isinstance(discovery, dict) else None
    )
    sequences: list[list[tuple[str, str]]] = []
    if not isinstance(raw_sequences, list):
        return sequences
    for raw_sequence in raw_sequences:
        if not isinstance(raw_sequence, list):
            continue
        sequence: list[tuple[str, str]] = []
        for item in raw_sequence:
            if not isinstance(item, str) or " " not in item:
                sequence = []
                break
            method, path = item.split(" ", 1)
            sequence.append((method.upper(), path))
        if sequence:
            sequences.append(sequence)
    return sequences


def _is_subsequence(
    required: list[tuple[str, str]],
    observed: list[tuple[str, str]],
) -> bool:
    if not required:
        return True
    index = 0
    for identity in observed:
        if identity == required[index]:
            index += 1
            if index == len(required):
                return True
    return False


def _passed(result: QualificationResult) -> bool:
    return (
        result.status == "completed"
        and result.discovery_outcome == "fixpoint"
        and result.browser_only_found == result.browser_only_total
        and result.discovery_browser_only_found == result.browser_only_total
        and result.required_sequences_found == result.required_sequences_total
        and (result.required_sequences_total == 0 or result.discovery_workflow_count > 0)
        and result.baseline_subset_preserved
        and not result.isolation_violations
        and result.persisted_result_verified
    )


async def _run_one(
    *,
    app_client: httpx.AsyncClient,
    health_client: httpx.AsyncClient,
    target: Target,
    case: AuthAgentSiteCase,
    repetition: int,
    scope_config: dict[str, Any],
    args: argparse.Namespace,
    harness_token: str,
) -> QualificationResult:
    run_id = f"live-discovery-{target.name}-{repetition}-{secrets.token_hex(5)}"
    job_id: str | None = None
    try:
        await _prepare_target(health_client, target, harness_token)
        await _wait_for_target(health_client, target)
        response = await app_client.post(
            "/jobs",
            json={
                "target_url": target.seed_url,
                "scope_config": scope_config,
                "auth_config": _auth_config_for_run(target, case, run_id),
                "discovery": {
                    "enabled": True,
                    "max_rounds": args.max_rounds,
                    "max_actions": args.max_actions,
                    "max_llm_pages": args.max_llm_pages,
                },
            },
        )
        response.raise_for_status()
        job_id = str(response.json()["job_id"])
        payload = await _poll_job(
            app_client,
            job_id,
            poll_interval=args.poll_interval,
            timeout=args.job_timeout,
            cancel_timeout=args.cancel_timeout,
        )
        final_identities = _final_identities(payload)
        final_paths = {_path_identity(method, url) for method, url in final_identities}
        baseline_identities = _baseline_identities(payload)
        browser_only = _manifest_entries(target, "browser_only_entries")
        discovery_requests = _discovery_request_sequence(payload)
        discovery_request_set = set(discovery_requests)
        missing_browser_only = sorted(
            f"{method} {path}" for method, path in browser_only - final_paths
        )
        missing_discovery_browser_only = sorted(
            f"{method} {path}" for method, path in browser_only - discovery_request_set
        )
        required_sequences = _required_request_sequences(target)
        missing_required_sequences = [
            [f"{method} {path}" for method, path in sequence]
            for sequence in required_sequences
            if not _is_subsequence(sequence, discovery_requests)
        ]
        ledger = await _read_ledger(health_client, target, run_id, harness_token)
        discovery = payload.get("discovery_result")
        if not isinstance(discovery, dict):
            discovery = {}
        return QualificationResult(
            target=target.name,
            repetition=repetition,
            job_id=job_id,
            status=str(payload.get("status")) if payload.get("status") else None,
            error=str(payload.get("error")) if payload.get("error") else None,
            entry_count=len(final_identities),
            baseline_entry_count=len(baseline_identities),
            baseline_subset_preserved=baseline_identities <= final_identities,
            browser_only_found=len(browser_only & final_paths),
            browser_only_total=len(browser_only),
            missing_browser_only=missing_browser_only,
            discovery_browser_only_found=len(browser_only & discovery_request_set),
            missing_discovery_browser_only=missing_discovery_browser_only,
            required_sequences_found=len(required_sequences) - len(missing_required_sequences),
            required_sequences_total=len(required_sequences),
            missing_required_sequences=missing_required_sequences,
            ledger_required_count=sum(
                entry.get("classification") == "required" for entry in ledger
            ),
            ledger_destructive_count=sum(
                entry.get("classification") in {"forbidden", "destructive-marker"}
                for entry in ledger
            ),
            isolation_violations=_isolation_violations(
                target,
                payload,
                _crawl_entries(payload),
            ),
            discovery_outcome=(str(discovery.get("outcome")) if discovery.get("outcome") else None),
            discovery_rounds=int(discovery.get("rounds") or 0),
            discovery_new_entry_count=int(discovery.get("new_entry_count") or 0),
            discovery_state_count=int(discovery.get("state_count") or 0),
            discovery_workflow_count=int(discovery.get("workflow_count") or 0),
            discovery_stop_reason=(
                str(discovery.get("stop_reason")) if discovery.get("stop_reason") else None
            ),
            sitemap_sha256=_sitemap_digest(payload),
        )
    except Exception as exc:
        return QualificationResult(
            target=target.name,
            repetition=repetition,
            job_id=job_id,
            status=None,
            error=f"{type(exc).__name__}: {exc}",
            entry_count=0,
            baseline_entry_count=0,
            baseline_subset_preserved=False,
            browser_only_found=0,
            browser_only_total=len(_manifest_entries(target, "browser_only_entries")),
            missing_browser_only=sorted(
                f"{method} {path}"
                for method, path in _manifest_entries(target, "browser_only_entries")
            ),
            discovery_browser_only_found=0,
            missing_discovery_browser_only=sorted(
                f"{method} {path}"
                for method, path in _manifest_entries(target, "browser_only_entries")
            ),
            required_sequences_found=0,
            required_sequences_total=len(_required_request_sequences(target)),
            missing_required_sequences=[
                [f"{method} {path}" for method, path in sequence]
                for sequence in _required_request_sequences(target)
            ],
            ledger_required_count=0,
            ledger_destructive_count=0,
            isolation_violations=[],
            discovery_outcome=None,
            discovery_rounds=0,
            discovery_new_entry_count=0,
            discovery_state_count=0,
            discovery_workflow_count=0,
            discovery_stop_reason=None,
            sitemap_sha256=None,
        )


async def _verify_persistence(
    client: httpx.AsyncClient,
    results: list[QualificationResult],
) -> None:
    response = await client.get("/jobs", params={"limit": 250})
    response.raise_for_status()
    listed = {
        str(item.get("job_id"))
        for item in response.json().get("jobs", [])
        if isinstance(item, dict)
    }
    for result in results:
        if result.job_id is None or result.job_id not in listed:
            continue
        response = await client.get(f"/jobs/{result.job_id}")
        response.raise_for_status()
        payload = response.json()
        result.persisted_result_verified = (
            payload.get("status") == result.status
            and _sitemap_digest(payload) == result.sitemap_sha256
        )


def _write_report(results: list[QualificationResult], args: argparse.Namespace) -> None:
    generated_at = datetime.now(UTC).isoformat()
    passed = [result for result in results if _passed(result)]
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "configuration": {
            "execution_boundary": "uv-run-server-http-api",
            "repetitions": args.repetitions,
            "max_rounds": args.max_rounds,
            "max_actions": args.max_actions,
            "max_llm_pages": args.max_llm_pages,
            "dit_model_sha256": _sha256(args.dit_model),
            "katana_sha256": _sha256(args.katana_binary),
        },
        "summary": {
            "runs": len(results),
            "passed": len(passed),
            "browser_only_found": sum(result.browser_only_found for result in results),
            "browser_only_declared": sum(result.browser_only_total for result in results),
            "browser_only_observed_during_discovery": sum(
                result.discovery_browser_only_found for result in results
            ),
            "required_sequences_found": sum(result.required_sequences_found for result in results),
            "required_sequences_declared": sum(
                result.required_sequences_total for result in results
            ),
            "ledger_required": sum(result.ledger_required_count for result in results),
            "ledger_destructive": sum(result.ledger_destructive_count for result in results),
            "verified_workflows": sum(result.discovery_workflow_count for result in results),
            "persisted": sum(result.persisted_result_verified for result in results),
        },
        "results": [asdict(result) for result in results],
    }
    args.json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Browser-Guided Discovery Qualification",
        "",
        f"Generated: `{generated_at}`",
        "",
        (
            "Every row used a real `uv run tenzai-crawler-server`, HTTP job submission, "
            "target reset and ledger, persisted API retrieval after server restart, and the "
            "server-owned live discovery model."
        ),
        "",
        f"- Passed: {len(passed)}/{len(results)} runs.",
        (
            f"- Browser-only endpoints: {payload['summary']['browser_only_found']}/"
            f"{payload['summary']['browser_only_declared']}."
        ),
        (
            "- Browser-only endpoints observed in guided-browser traffic: "
            f"{payload['summary']['browser_only_observed_during_discovery']}/"
            f"{payload['summary']['browser_only_declared']}."
        ),
        (
            "- Required guided request sequences: "
            f"{payload['summary']['required_sequences_found']}/"
            f"{payload['summary']['required_sequences_declared']}."
        ),
        f"- Required ledger requests: {payload['summary']['ledger_required']}.",
        f"- Destructive ledger requests observed: {payload['summary']['ledger_destructive']}.",
        f"- Runtime-verified workflows: {payload['summary']['verified_workflows']}.",
        f"- Persisted results verified: {payload['summary']['persisted']}/{len(results)}.",
        "",
        "| Target | Run | Status | Outcome | Entries | New | Browser-only | Guided | "
        "Sequences | States | Workflows | Baseline kept | Persisted | Destructive |",
        "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
        "--- | --- | ---: |",
    ]
    for result in results:
        lines.append(
            f"| `{result.target}` | {result.repetition} | `{result.status or '-'}` | "
            f"`{result.discovery_outcome or '-'}` | {result.entry_count} | "
            f"{result.discovery_new_entry_count} | "
            f"{result.browser_only_found}/{result.browser_only_total} | "
            f"{result.discovery_browser_only_found}/{result.browser_only_total} | "
            f"{result.required_sequences_found}/{result.required_sequences_total} | "
            f"{result.discovery_state_count} | "
            f"{result.discovery_workflow_count} | "
            f"{'yes' if result.baseline_subset_preserved else 'no'} | "
            f"{'yes' if result.persisted_result_verified else 'no'} | "
            f"{result.ledger_destructive_count} |"
        )
    failures = [result for result in results if result not in passed]
    lines.extend(["", "## Gaps", ""])
    if not failures:
        lines.append("No qualification gaps.")
    for result in failures:
        details = [
            *result.missing_browser_only,
            *result.missing_discovery_browser_only,
            *[" -> ".join(sequence) for sequence in result.missing_required_sequences],
        ]
        detail = result.error or ", ".join(details) or "gate failed"
        lines.append(f"- `{result.target}` run {result.repetition}: {detail}")
    try:
        json_display = args.json_path.relative_to(ROOT)
    except ValueError:
        json_display = args.json_path
    lines.extend(["", f"Machine-readable results: `{json_display}`", ""])
    args.report_path.write_text("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qualify live browser-guided discovery.")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--max-actions", type=int, default=100)
    parser.add_argument("--max-llm-pages", type=int, default=25)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--rate-limit", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--parallelism", type=int, default=10)
    parser.add_argument("--crawl-duration", default="10m")
    parser.add_argument("--request-timeout", type=int, default=10)
    parser.add_argument("--subprocess-timeout", type=int, default=720)
    parser.add_argument("--auth-attempts", type=int, default=1)
    parser.add_argument("--job-timeout", type=float, default=3600)
    parser.add_argument("--cancel-timeout", type=float, default=30)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--api-request-timeout", type=float, default=30)
    parser.add_argument("--server-readiness-timeout", type=float, default=60)
    parser.add_argument("--server-shutdown-timeout", type=float, default=30)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    parser.add_argument("--json-path", type=Path, default=JSON_PATH)
    parser.add_argument(
        "--dit-model",
        type=Path,
        help="Pinned DIT classifier model used by Katana's -kb flag.",
    )
    parser.add_argument(
        "--katana-binary",
        type=Path,
        help="Qualified v1.6.1-tenzai.2 binary used by the crawler server.",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Keep the temporary database, crawler logs, and server logs after the run.",
    )
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    targets = [
        target
        for target in _load_manifest(LOCAL_TARGETS_PATH, required=True)
        if target.name in QUALIFICATION_TARGETS and (not args.case or target.name in set(args.case))
    ]
    if not targets:
        raise ValueError("no browser-discovery qualification targets selected")
    harness_token = os.environ.get("TEST_HARNESS_TOKEN")
    if not harness_token:
        raise ValueError("TEST_HARNESS_TOKEN is required for fixture ledgers")

    cases = {
        _site_name_for_case(case) or case.name: case for case in _canonical_cases(gateway=False)
    }
    scope_config = _build_scope_config(args)
    temp_root = tempfile.TemporaryDirectory(
        prefix="crawler-browser-discovery-",
        delete=not args.keep_artifacts,
    )
    temp_dir = Path(temp_root.name)
    server_home, args.dit_model, args.katana_binary = prepare_server_home(
        temp_dir,
        args.dit_model,
        args.katana_binary,
    )
    server: ServerProcess | None = None
    results: list[QualificationResult] = []
    try:
        server = await _start_server(args, temp_dir, harness_token, home_dir=server_home)
        await _wait_for_server(server, timeout=args.server_readiness_timeout)
        async with (
            httpx.AsyncClient(base_url=server.base_url, timeout=args.api_request_timeout) as app,
            httpx.AsyncClient(timeout=30, follow_redirects=True) as health,
        ):
            for repetition in range(1, args.repetitions + 1):
                for target in targets:
                    print(f"discovery {target.name} repetition={repetition} ...", flush=True)
                    result = await _run_one(
                        app_client=app,
                        health_client=health,
                        target=target,
                        case=cases[target.name],
                        repetition=repetition,
                        scope_config=scope_config,
                        args=args,
                        harness_token=harness_token,
                    )
                    print(
                        f"  status={result.status} outcome={result.discovery_outcome} "
                        f"browser={result.browser_only_found}/{result.browser_only_total} "
                        f"guided={result.discovery_browser_only_found}/"
                        f"{result.browser_only_total} "
                        f"sequences={result.required_sequences_found}/"
                        f"{result.required_sequences_total} "
                        f"new={result.discovery_new_entry_count} "
                        f"destructive={result.ledger_destructive_count}"
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
        ) as client:
            await _verify_persistence(client, results)
        await _stop_server(server, timeout=args.server_shutdown_timeout)
        server = None
        _write_report(results, args)
        passed = [result for result in results if _passed(result)]
        print(f"discovery qualification: {len(passed)}/{len(results)} passed", flush=True)
        return 0 if len(passed) == len(results) else 1
    finally:
        if server is not None:
            with contextlib.suppress(Exception):
                await _stop_server(server, timeout=args.server_shutdown_timeout)
        if args.keep_artifacts:
            print(f"kept qualification artifacts: {temp_dir}", flush=True)
        else:
            temp_root.cleanup()


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
