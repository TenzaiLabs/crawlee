from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_browser_capability_spike as spike


def test_katana_commands_keep_standard_and_pure_capabilities_separate(tmp_path: Path) -> None:
    headers = {"X-Discovery-Token": "runtime-value"}
    standard = spike._standard_command(
        "katana",
        "http://example.test",
        tmp_path / "s",
        tmp_path / "standard-terminal",
        headers,
        terminal_summary_supported=True,
    )
    pure = spike._pure_headless_command(
        "katana",
        "http://example.test",
        "ws://127.0.0.1/devtools/browser/id",
        tmp_path / "p",
        tmp_path / "terminal",
        headers,
        terminal_summary_supported=True,
    )

    assert all(flag in standard for flag in ("-jc", "-jsl", "-fx", "-td", "-kb", "-kf"))
    assert standard[standard.index("-s") + 1] == "breadth-first"
    assert standard[standard.index("-terminal-summary") + 1] == str(tmp_path / "standard-terminal")
    assert all(flag in pure for flag in ("-cwu", "-xhr", "-iqp", "-fsu", "-fst"))
    assert "-kf" not in pure
    assert "-fpt" not in pure
    assert pure[pure.index("-u") + 1].endswith("/header-only")
    assert pure[pure.index("-terminal-summary") + 1] == str(tmp_path / "terminal")


def test_jsonl_helpers_read_response_metadata() -> None:
    records = [
        {
            "request": {"method": "get", "endpoint": "https://example.test/marker"},
            "response": {
                "status_code": 200,
                "body": "ok",
                "knowledgebase": {"PageType": "landing"},
            },
        }
    ]

    assert spike._has_endpoint(records, "GET", "/marker", status=200)
    assert spike._contains_field(records, "knowledgebase")
    assert spike._max_body_size(records) == 2
    assert spike._record_summaries(records) == [
        {"method": "GET", "url": "https://example.test/marker", "status": 200}
    ]


def test_passive_observer_records_network_events_without_fetch_interception() -> None:
    observer = spike.PassiveCDPObserver("ws://unused")
    observer._sessions["worker-session"] = {
        "type": "worker",
        "url": "https://example.test/worker.js",
        "target_id": "target",
    }

    observer._handle_event(
        {
            "method": "Network.requestWillBeSent",
            "sessionId": "worker-session",
            "params": {
                "request": {"method": "GET", "url": "https://example.test/api/worker"},
                "type": "Fetch",
            },
        }
    )
    observer._handle_event(
        {
            "method": "Network.responseReceived",
            "sessionId": "worker-session",
            "params": {"response": {"url": "https://example.test/api/worker", "status": 200}},
        }
    )

    assert observer.observations == [
        spike.NetworkObservation(
            method="GET",
            url="https://example.test/api/worker",
            target_type="worker",
            target_url="https://example.test/worker.js",
            frame_id=None,
            loader_id=None,
            resource_type="Fetch",
        )
    ]
    assert observer.responses == [
        spike.NetworkResponseObservation(
            url="https://example.test/api/worker",
            status=200,
            target_type="worker",
        )
    ]


def test_capability_report_does_not_persist_header_values_or_process_tails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(spike, "ROOT", tmp_path)
    run = spike.KatanaRun(
        command=["katana", "-H", "Authorization: plaintext-secret"],
        exit_code=0,
        elapsed_seconds=1.0,
        records=[
            {
                "request": {"method": "GET", "endpoint": "https://example.test/"},
                "response": {"status_code": 200},
            }
        ],
        stderr_tail="plaintext-secret",
        stdout_tail="plaintext-secret",
        artifact_sha256="artifact",
    )
    result = spike.SpikeResult(
        generated_at="2026-07-23T00:00:00+00:00",
        base_url="https://example.test",
        katana_version="v1.6.1-tenzai.1",
        katana_sha256="binary",
        dit_model_sha256="model",
        chrome_product="Chrome/test",
        checks=[spike.CapabilityCheck("check", "lane", True, "evidence")],
        standard=run,
        pure_headless=run,
    )
    report_path = tmp_path / "report.md"
    results_path = tmp_path / "results.json"

    spike._write_reports(result, report_path, results_path)

    persisted = results_path.read_text() + report_path.read_text()
    assert "plaintext-secret" not in persisted
    assert "Authorization: {{runtime}}" in persisted
    assert json.loads(results_path.read_text())["standard"]["record_summaries"][0] == {
        "method": "GET",
        "status": 200,
        "url": "https://example.test/",
    }
