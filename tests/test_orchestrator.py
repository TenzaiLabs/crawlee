from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from app import auth_agent, orchestrator, proxy


@pytest.mark.asyncio
async def test_run_auth_if_needed_returns_dynamic_exclusion_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_authenticate(
        target_url: str,
        auth_config: dict,
        cancel_event: asyncio.Event,
    ) -> auth_agent.AuthResult:
        assert target_url == "https://example.com"
        assert auth_config["_proxify_log_path"] == "/tmp/proxify.jsonl"
        assert not cancel_event.is_set()
        return auth_agent.AuthResult(
            cookies=[],
            headers=["Authorization: Bearer token"],
            landing_url="https://example.com/app/dashboard",
            blocked_urls=["/logout", "https://evil.test/delete"],
        )

    monkeypatch.setattr(orchestrator.auth_agent, "authenticate", fake_authenticate)

    headers, landing_url, dynamic_exclude_patterns = await orchestrator._run_auth_if_needed(
        "job-1",
        "https://example.com",
        {"login_url": "https://example.com/login"},
        ["Cookie: session=abc"],
        cast(proxy.ProxyProcess, SimpleNamespace(log_path="/tmp/proxify.jsonl")),
        asyncio.Event(),
    )

    assert headers == ["Cookie: session=abc", "Authorization: Bearer token"]
    assert landing_url == "https://example.com/app/dashboard"
    assert dynamic_exclude_patterns == ["/logout(?:$|[/?#])"]
