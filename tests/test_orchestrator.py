from __future__ import annotations

import asyncio

import pytest

from app import auth_agent, orchestrator


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
        assert "_proxify_log_path" not in auth_config
        assert not cancel_event.is_set()
        return auth_agent.AuthResult(
            headers=["Authorization: Bearer token"],
            cookies=[{"name": "session", "value": "abc"}],
            landing_url="https://example.com/app/dashboard",
            blocked_urls=["/logout", "https://evil.test/delete"],
        )

    monkeypatch.setattr(orchestrator.auth_agent, "authenticate", fake_authenticate)

    auth_context = await orchestrator._run_auth_if_needed(
        "job-1",
        "https://example.com",
        {"login_url": "https://example.com/login"},
        ["Cookie: session=abc"],
        True,
        asyncio.Event(),
    )

    assert auth_context.headers == ["Cookie: session=abc", "Authorization: Bearer token"]
    assert auth_context.landing_url == "https://example.com/app/dashboard"
    assert auth_context.extra_seed_urls == ["https://example.com/app/dashboard"]
    assert auth_context.dynamic_exclude_patterns == ["/logout(?:$|[/?#])"]


@pytest.mark.asyncio
async def test_run_auth_if_needed_returns_manual_header_context() -> None:
    auth_context = await orchestrator._run_auth_if_needed(
        "job-1",
        "https://example.com",
        {"headers": ["Cookie: session=abc"]},
        ["Cookie: session=abc"],
        False,
        asyncio.Event(),
    )

    assert auth_context == orchestrator.CrawlAuthContext(headers=["Cookie: session=abc"])
