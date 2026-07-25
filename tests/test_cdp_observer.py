from __future__ import annotations

import pytest

from app import cdp_observer


def test_passive_observer_records_request_and_response_metadata_by_epoch() -> None:
    observer = cdp_observer.PassiveCDPObserver("ws://unused")
    observer.set_epoch("katana-baseline")
    observer._sessions["worker-session"] = {
        "type": "worker",
        "url": "https://example.test/worker.js",
        "target_id": "worker-target",
    }

    observer._handle_event(
        {
            "method": "Network.requestWillBeSent",
            "sessionId": "worker-session",
            "params": {
                "requestId": "request-1",
                "request": {"method": "post", "url": "https://example.test/api/worker"},
                "frameId": "frame-1",
                "loaderId": "loader-1",
                "type": "Fetch",
                "initiator": {
                    "type": "script",
                    "stack": {
                        "callFrames": [
                            {"url": "https://example.test/worker.js"},
                        ]
                    },
                },
            },
        }
    )
    observer._handle_event(
        {
            "method": "Network.responseReceived",
            "sessionId": "worker-session",
            "params": {
                "requestId": "request-1",
                "type": "Fetch",
                "response": {
                    "url": "https://example.test/api/worker",
                    "status": 201.0,
                    "mimeType": "application/json",
                },
            },
        }
    )

    assert len(observer.requests) == 1
    request = observer.requests[0]
    assert request.method == "POST"
    assert request.session_id == "worker-session"
    assert request.target_type == "worker"
    assert request.target_url == "https://example.test/worker.js"
    assert request.request_id == "request-1"
    assert request.frame_id == "frame-1"
    assert request.loader_id == "loader-1"
    assert request.resource_type == "Fetch"
    assert request.initiator_type == "script"
    assert request.initiator_url == "https://example.test/worker.js"
    assert request.epoch == "katana-baseline"
    assert request.observed_at

    assert len(observer.responses) == 1
    response = observer.responses[0]
    assert response.status == 201
    assert response.session_id == "worker-session"
    assert response.target_type == "worker"
    assert response.request_id == "request-1"
    assert response.resource_type == "Fetch"
    assert response.mime_type == "application/json"
    assert response.epoch == "katana-baseline"
    assert response.observed_at


def test_passive_observer_ignores_non_network_events_and_rejects_empty_epoch() -> None:
    observer = cdp_observer.PassiveCDPObserver("ws://unused")
    observer._handle_event({"method": "Runtime.consoleAPICalled", "params": {}})

    assert observer.requests == []
    assert observer.responses == []

    try:
        observer.set_epoch("  ")
    except ValueError as exc:
        assert "must not be empty" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("empty epoch was accepted")


@pytest.mark.asyncio
async def test_passive_observer_enables_sessions_without_pausing_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = cdp_observer.PassiveCDPObserver("ws://unused")
    observer._sessions["session-1"] = {
        "type": "worker",
        "url": "https://example.test/worker.js",
        "target_id": "target-1",
    }
    calls: list[tuple[str, dict | None, str | None]] = []

    async def send(method, params=None, *, session_id=None):
        calls.append((method, params, session_id))
        return {}

    monkeypatch.setattr(observer, "_send", send)

    await observer._enable_session("session-1")

    assert calls == [
        (
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            "session-1",
        ),
        ("Network.enable", None, "session-1"),
    ]
    assert observer.diagnostics == [
        {
            "kind": "initial-network-observation-gap",
            "session_id": "session-1",
            "target_type": "worker",
            "target_url": "https://example.test/worker.js",
            "reason": "target_was_not_paused_before_network_enable",
        }
    ]
