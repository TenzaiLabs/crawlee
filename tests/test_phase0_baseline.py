from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts import run_phase0_baseline


def test_local_target_manifest_is_complete_and_unique() -> None:
    targets = run_phase0_baseline._load_manifest(
        run_phase0_baseline.LOCAL_TARGETS_PATH,
        required=True,
    )

    assert len(targets) == 21
    assert len({target.name for target in targets}) == 21
    assert all(target.kind == "repository" for target in targets)
    assert all(target.sitemap is not None for target in targets)
    assert all(target.reset == "container-restart" for target in targets)


def test_manifest_entry_sets_separate_baseline_browser_only_and_blocked() -> None:
    target = next(
        target
        for target in run_phase0_baseline._load_manifest(
            run_phase0_baseline.LOCAL_TARGETS_PATH,
            required=True,
        )
        if target.name == "site-f-spa-deno"
    )

    baseline = run_phase0_baseline._manifest_entries(target, "entries")
    browser_only = run_phase0_baseline._manifest_entries(target, "browser_only_entries")
    blocked = run_phase0_baseline._manifest_entries(target, "blocked_entries")

    assert ("GET", "/api/links") in baseline
    assert ("POST", "/api/projects/search") in browser_only
    assert ("POST", "/api/actions/delete") in blocked
    assert not baseline & browser_only


def test_discovery_lane_manifest_declares_each_evidence_lane() -> None:
    target = next(
        target
        for target in run_phase0_baseline._load_manifest(
            run_phase0_baseline.LOCAL_TARGETS_PATH,
            required=True,
        )
        if target.name == "site-g-discovery-lanes"
    )

    markers = run_phase0_baseline._capability_markers(target)

    assert {marker.lane for marker in markers} >= {
        "standard-jc",
        "standard-jsl",
        "standard-fx",
        "pure-headless",
        "passive-cdp",
        "known-files",
        "browser-handoff",
        "header-propagation",
    }
    assert run_phase0_baseline._ledger_config(target) == {
        "run_header": "X-Crawler-Test-Run",
        "url_template": "http://localhost:8007/_test/ledger/{run_id}",
        "harness_header": "X-Test-Harness-Token",
    }
    assert target.auth_config == {"headers": ["X-Discovery-Token: {{env:TEST_HARNESS_TOKEN}}"]}


def test_capability_results_are_grouped_by_lane() -> None:
    target = next(
        target
        for target in run_phase0_baseline._load_manifest(
            run_phase0_baseline.LOCAL_TARGETS_PATH,
            required=True,
        )
        if target.name == "site-g-discovery-lanes"
    )

    total, found, by_lane = run_phase0_baseline._capability_results(
        target,
        {
            ("GET", "/api/js/regex-marker.do"),
            ("GET", "/rendered/only"),
        },
    )

    assert total == 11
    assert found == 2
    assert by_lane["standard-jc"] == "1/1"
    assert by_lane["pure-headless"] == "1/3"


def test_capability_results_use_structured_form_evidence() -> None:
    target = next(
        target
        for target in run_phase0_baseline._load_manifest(
            run_phase0_baseline.LOCAL_TARGETS_PATH,
            required=True,
        )
        if target.name == "site-g-discovery-lanes"
    )

    total, found, by_lane = run_phase0_baseline._capability_results(
        target,
        set(),
        {("POST", "/api/form/preview")},
    )

    assert total == 11
    assert found == 1
    assert by_lane["standard-fx"] == "1/1"


def test_observed_form_identities_reads_katana_evidence() -> None:
    payload = {
        "evidence": {
            "katana": {
                "standard": {
                    "records": [
                        {
                            "features": {
                                "forms": [
                                    {
                                        "method": "post",
                                        "action": "http://localhost:8007/api/form/preview",
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }
    }

    assert run_phase0_baseline._observed_form_identities(payload) == {("POST", "/api/form/preview")}


def test_run_header_is_added_without_mutating_manifest_auth_config() -> None:
    target = next(
        target
        for target in run_phase0_baseline._load_manifest(
            run_phase0_baseline.LOCAL_TARGETS_PATH,
            required=True,
        )
        if target.name == "site-g-discovery-lanes"
    )
    case = run_phase0_baseline._generic_case(target)

    resolved = run_phase0_baseline._auth_config_for_run(target, case, "run-123")

    assert resolved == {
        "headers": [
            "X-Discovery-Token: {{env:TEST_HARNESS_TOKEN}}",
            "X-Crawler-Test-Run: run-123",
        ]
    }
    assert target.auth_config == {"headers": ["X-Discovery-Token: {{env:TEST_HARNESS_TOKEN}}"]}


def test_canonical_case_map_uses_base_site_name_for_dynamic_case() -> None:
    cases = {
        run_phase0_baseline._site_name_for_case(case) or case.name: case
        for case in run_phase0_baseline._canonical_cases(gateway=False)
    }

    assert cases["auth-a-simple-form"].mode == "llm"


def test_external_manifest_rejects_floating_versions(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 1,
        "targets": [
            {
                "name": "external",
                "kind": "external",
                "seed_url": "http://localhost:9999",
                "health_url": "http://localhost:9999/",
                "health_status": 200,
                "reset": "container-restart",
                "auth_reference": None,
                "allowed_origins": ["http://localhost:9999"],
                "sitemap": None,
                "version": "latest",
            }
        ],
    }
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="floating version"):
        run_phase0_baseline._load_manifest(path, required=True)


def test_external_manifest_rejects_out_of_scope_lifecycle_url(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 1,
        "targets": [
            {
                "name": "external",
                "kind": "external",
                "seed_url": "http://localhost:9999",
                "health_url": "http://localhost:9999/",
                "health_status": 200,
                "reset": {"method": "POST", "url": "http://example.com/reset"},
                "auth_reference": None,
                "allowed_origins": ["http://localhost:9999"],
                "sitemap": None,
                "version": "v1.0.0",
            }
        ],
    }
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="reset origin is not allowlisted"):
        run_phase0_baseline._load_manifest(path, required=True)


def test_external_expected_entries_are_loaded_without_a_sitemap() -> None:
    targets = run_phase0_baseline._load_manifest(
        run_phase0_baseline.EXTERNAL_TARGETS_PATH,
        required=True,
    )
    juice_shop = next(target for target in targets if target.name == "juice-shop")

    assert run_phase0_baseline._manifest_entries(juice_shop, "entries") == {
        ("GET", "/"),
        ("GET", "/rest/admin/application-version"),
        ("GET", "/rest/products/search"),
    }


def test_cross_job_isolation_rejects_entries_from_another_origin() -> None:
    target = run_phase0_baseline.Target(
        name="one",
        kind="repository",
        seed_url="http://localhost:8001",
        health_url="http://localhost:8001/",
        health_status=200,
        reset="container-restart",
        auth_reference=None,
        allowed_origins=("http://localhost:8001",),
        sitemap=None,
        version="repository-worktree",
    )

    violations = run_phase0_baseline._isolation_violations(
        target,
        {"target_url": target.seed_url},
        [{"url": "http://localhost:8002/from-previous-job"}],
    )

    assert violations == ["out-of-target entry http://localhost:8002/from-previous-job"]


def test_cross_job_isolation_accepts_equivalent_root_url() -> None:
    target = run_phase0_baseline.Target(
        name="one",
        kind="repository",
        seed_url="http://localhost:8001",
        health_url="http://localhost:8001/",
        health_status=200,
        reset="container-restart",
        auth_reference=None,
        allowed_origins=("http://localhost:8001",),
        sitemap=None,
        version="repository-worktree",
    )

    assert (
        run_phase0_baseline._isolation_violations(
            target,
            {"target_url": "http://localhost:8001/"},
            [{"url": "http://localhost:8001/page"}],
        )
        == []
    )


def test_server_environment_is_isolated_to_temporary_storage(
    tmp_path: Path,
) -> None:
    args = argparse.Namespace(subprocess_timeout=60, auth_attempts=1)

    env = run_phase0_baseline._server_environment(args, tmp_path, 18765)

    assert env["CRAWLER_DB_PATH"] == str(tmp_path / "jobs.db")
    assert env["CRAWLER_LOG_DIR"] == str(tmp_path / "logs")
    assert env["CRAWLER_HOST"] == "127.0.0.1"
    assert env["CRAWLER_PORT"] == "18765"


def test_server_environment_receives_ephemeral_harness_token(tmp_path: Path) -> None:
    args = argparse.Namespace(subprocess_timeout=60, auth_attempts=1)

    env = run_phase0_baseline._server_environment(args, tmp_path, 18765, "runtime-ref")

    assert env["TEST_HARNESS_TOKEN"] == "runtime-ref"


def test_server_environment_can_isolate_home(tmp_path: Path) -> None:
    args = argparse.Namespace(subprocess_timeout=60, auth_attempts=1)
    home_dir = tmp_path / "home"
    (home_dir / "bin").mkdir(parents=True)

    env = run_phase0_baseline._server_environment(
        args,
        tmp_path,
        18765,
        home_dir=home_dir,
    )

    assert env["HOME"] == str(home_dir)
    assert env["PATH"].split(os.pathsep)[0] == str(home_dir / "bin")


def test_report_identifies_exact_missing_expected_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.md"
    json_path = tmp_path / "results.json"
    monkeypatch.setattr(run_phase0_baseline, "ROOT", tmp_path)
    monkeypatch.setattr(run_phase0_baseline, "REPORT_PATH", report_path)
    monkeypatch.setattr(run_phase0_baseline, "JSON_PATH", json_path)
    result = run_phase0_baseline.BaselineResult(
        target="fixture",
        kind="repository",
        version="test",
        ready=True,
        health_status=200,
        crawl_status="completed",
        auth_mode="public",
        entry_count=1,
        expected_entry_count=2,
        expected_entries_found=1,
        browser_only_entry_count=0,
        browser_only_entries_found=0,
        blocked_hits=[],
        methods={"GET": 1},
        status_codes={"200": 1},
        sample_paths=["/"],
        elapsed_seconds=1.0,
        missing_expected_entries=["GET /missing"],
    )
    args = argparse.Namespace(
        local_only=True,
        max_depth=3,
        crawl_duration="5m",
        discovery_enabled=False,
        dit_model=tmp_path / "model.json",
        katana_binary=tmp_path / "katana",
        report_path=report_path,
        json_path=json_path,
    )
    args.dit_model.write_bytes(b"model")
    args.katana_binary.write_bytes(b"katana")

    run_phase0_baseline._write_reports([result], args)

    payload = json.loads(json_path.read_text())
    assert payload["configuration"]["capture_source"].startswith("known-files+")
    assert payload["results"][0]["missing_expected_entries"] == ["GET /missing"]
    assert "- `fixture`: `GET /missing`" in report_path.read_text()


def test_required_request_sequences_are_scored_in_ledger_order() -> None:
    target = next(
        target
        for target in run_phase0_baseline._load_manifest(
            run_phase0_baseline.LOCAL_TARGETS_PATH,
            required=True,
        )
        if target.name == "site-f-spa-deno"
    )
    ledger = [
        {"method": "POST", "route": "/api/projects/search"},
        {"method": "GET", "route": "/unrelated"},
        {"method": "GET", "route": "/api/projects/details/project-aurora"},
        {"method": "POST", "route": "/api/reports/validate"},
        {"method": "POST", "route": "/api/reports/preview"},
    ]

    assert run_phase0_baseline._request_sequence_results(target, ledger) == (2, 2)


def test_qualification_gates_controlled_targets_but_not_public_canary_outcomes() -> None:
    controlled = run_phase0_baseline.BaselineResult(
        target="controlled",
        kind="repository",
        version="test",
        ready=True,
        health_status=200,
        crawl_status="completed",
        auth_mode="public",
        entry_count=1,
        expected_entry_count=0,
        expected_entries_found=0,
        browser_only_entry_count=1,
        browser_only_entries_found=1,
        blocked_hits=[],
        methods={"GET": 1},
        status_codes={"200": 1},
        sample_paths=["/"],
        elapsed_seconds=1.0,
        persisted_result_verified=True,
        discovery_outcome="fixpoint",
        capability_marker_count=1,
        capability_markers_found=1,
        request_sequence_count=1,
        request_sequences_found=1,
        discovery_workflow_count=1,
    )
    public_canary = replace(
        controlled,
        target="public-canary",
        kind="external",
        crawl_status="failed",
        discovery_outcome=None,
        browser_only_entries_found=0,
        capability_markers_found=0,
        request_sequences_found=0,
    )

    assert not run_phase0_baseline._qualification_failures(
        [controlled, public_canary],
        discovery_enabled=True,
    )
    failures = run_phase0_baseline._qualification_failures(
        [replace(controlled, request_sequences_found=0)],
        discovery_enabled=True,
    )
    assert failures == ["controlled: request sequences 0/1"]

    failures = run_phase0_baseline._qualification_failures(
        [replace(controlled, discovery_workflow_count=0)],
        discovery_enabled=True,
    )
    assert failures == ["controlled: no runtime-verified discovery workflow"]


@pytest.mark.asyncio
async def test_external_reset_script_name_cannot_escape_script_directory() -> None:
    target = run_phase0_baseline.Target(
        name="external",
        kind="external",
        seed_url="http://localhost:9999",
        health_url="http://localhost:9999/",
        health_status=200,
        reset="external-recreate:../reset.sh",
        auth_reference=None,
        allowed_origins=("http://localhost:9999",),
        sitemap=None,
        version="sha256:abc",
        score_setup=None,
        score_results_url=None,
    )

    with pytest.raises(ValueError, match="unsafe reset script"):
        await run_phase0_baseline._prepare_target(AsyncMock(), target)


@pytest.mark.asyncio
async def test_repository_reset_propagates_ephemeral_harness_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_env: dict[str, str] = {}

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    def fake_run(*args, **kwargs):
        observed_env.update(kwargs["env"])
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(run_phase0_baseline.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(run_phase0_baseline.subprocess, "run", fake_run)
    target = run_phase0_baseline.Target(
        name="site-g-discovery-lanes",
        kind="repository",
        seed_url="http://localhost:8007",
        health_url="http://localhost:8007/",
        health_status=200,
        reset="container-restart",
        auth_reference=None,
        allowed_origins=("http://localhost:8007",),
        sitemap=None,
        version="repository-worktree",
    )

    await run_phase0_baseline._prepare_target(AsyncMock(), target, "ephemeral-ref")

    assert observed_env["TEST_HARNESS_TOKEN"] == "ephemeral-ref"
