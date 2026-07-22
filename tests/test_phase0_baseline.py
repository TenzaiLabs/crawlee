from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts import run_phase0_baseline


def test_local_target_manifest_is_complete_and_unique() -> None:
    targets = run_phase0_baseline._load_manifest(
        run_phase0_baseline.LOCAL_TARGETS_PATH,
        required=True,
    )

    assert len(targets) == 20
    assert len({target.name for target in targets}) == 20
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
