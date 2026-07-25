from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scripts import qualification_artifacts
from scripts import run_browser_discovery_qualification as qualification


def test_baseline_identities_exclude_guided_and_enrichment_epochs() -> None:
    payload = {
        "evidence": {
            "known_files": {
                "documents": [{"url": "https://example.com/robots.txt"}],
            },
            "katana": {
                "standard": {
                    "records": [
                        {"method": "GET", "url": "https://example.com/baseline"},
                    ]
                },
                "pure_headless": {"records": []},
            },
            "browser": {
                "requests": [
                    {
                        "method": "POST",
                        "url": "https://example.com/auth",
                        "epoch": "authentication",
                    },
                    {
                        "method": "POST",
                        "url": "https://example.com/api/workflow",
                        "epoch": "browser-discovery-1",
                    },
                    {
                        "method": "GET",
                        "url": "https://example.com/enriched",
                        "epoch": "katana-pure-headless-discovery-1",
                    },
                ]
            },
        }
    }

    assert qualification._baseline_identities(payload) == {
        ("GET", "https://example.com/robots.txt"),
        ("GET", "https://example.com/baseline"),
        ("POST", "https://example.com/auth"),
    }


def test_final_identities_and_path_identity_preserve_method_and_query() -> None:
    payload = {
        "sitemap": {
            "entries": [
                {
                    "method": "POST",
                    "url": "https://example.com/api/search?kind=active",
                },
                {"method": "GET", "url": "https://example.com/"},
            ],
            "tree": {"children": {}, "pages": []},
        }
    }

    identities = qualification._final_identities(payload)

    assert identities == {
        ("POST", "https://example.com/api/search?kind=active"),
        ("GET", "https://example.com/"),
    }
    assert {qualification._path_identity(method, url) for method, url in identities} == {
        ("POST", "/api/search?kind=active"),
        ("GET", "/"),
    }


def test_prepare_server_home_stages_verified_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = tmp_path / "model.json"
    model.write_bytes(b"model")
    browser_cache = tmp_path / "ms-playwright"
    browser_cache.mkdir()
    katana = tmp_path / "katana"
    katana.write_bytes(b"katana")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_cache))
    monkeypatch.setattr(
        qualification_artifacts,
        "EXPECTED_DIT_MODEL_SHA256",
        qualification._sha256(model),
    )
    monkeypatch.setattr(
        qualification_artifacts,
        "EXPECTED_KATANA_SHA256",
        qualification._sha256(katana),
    )

    home, resolved_model, resolved_katana = qualification.prepare_server_home(
        tmp_path / "run",
        model,
        katana,
    )

    staged = home / ".dit" / "model.json"
    assert staged.is_symlink()
    assert staged.resolve() == model.resolve()
    assert (home / ".cache" / "ms-playwright").resolve() == browser_cache.resolve()
    assert (home / "bin" / "katana").resolve() == katana.resolve()
    assert resolved_model == model.resolve()
    assert resolved_katana == katana.resolve()


def test_prepare_server_home_rejects_wrong_model(tmp_path: Path) -> None:
    model = tmp_path / "model.json"
    model.write_bytes(b"wrong")

    with pytest.raises(ValueError, match="checksum mismatch"):
        qualification.prepare_server_home(tmp_path / "run", model, None)


def test_discovery_request_sequence_is_phase_specific_and_ordered() -> None:
    payload = {
        "evidence": {
            "browser": {
                "requests": [
                    {"method": "GET", "url": "https://example.com/baseline-only"},
                ]
            },
            "discovery": {
                "rounds": [
                    {
                        "requests": [
                            {"method": "POST", "url": "https://example.com/validate"},
                            {"method": "GET", "url": "https://example.com/noise"},
                        ]
                    },
                    {
                        "requests": [
                            {"method": "POST", "url": "https://example.com/preview"},
                        ]
                    },
                ]
            },
        }
    }

    observed = qualification._discovery_request_sequence(payload)

    assert observed == [
        ("POST", "/validate"),
        ("GET", "/noise"),
        ("POST", "/preview"),
    ]
    assert qualification._is_subsequence(
        [("POST", "/validate"), ("POST", "/preview")],
        observed,
    )
    assert not qualification._is_subsequence(
        [("POST", "/preview"), ("POST", "/validate")],
        observed,
    )


def test_live_qualification_requires_runtime_verified_workflow_for_sequences() -> None:
    result = cast(
        qualification.QualificationResult,
        SimpleNamespace(
            status="completed",
            discovery_outcome="fixpoint",
            browser_only_found=1,
            browser_only_total=1,
            discovery_browser_only_found=1,
            required_sequences_found=1,
            required_sequences_total=1,
            discovery_workflow_count=0,
            baseline_subset_preserved=True,
            isolation_violations=[],
            persisted_result_verified=True,
        ),
    )

    assert not qualification._passed(result)
    result.discovery_workflow_count = 1
    assert qualification._passed(result)
