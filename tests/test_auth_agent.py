from __future__ import annotations

import json
import logging

import pytest

from app import auth_agent


def test_needs_auth():
    assert auth_agent.needs_auth(None) is False
    assert auth_agent.needs_auth({}) is False
    assert auth_agent.needs_auth({"headers": ["Cookie: a=b"]}) is False
    assert auth_agent.needs_auth({"login_url": "https://example.com/login"}) is True
    assert auth_agent.needs_auth({"credentials": {"email": "a"}}) is True
    assert auth_agent.needs_auth({"credentials": {}}) is False


def test_prepare_auth_config_defaults_to_target_url():
    prepared = auth_agent._prepare_auth_config("https://example.com", {"credentials": {"u": "a"}})

    assert prepared.login_url == "https://example.com"
    assert prepared.credentials_payload == {"u": "a"}
    assert prepared.instructions is None
    assert prepared.success_indicator is None


def test_prepare_auth_config_coerces_max_steps_and_log_path():
    prepared = auth_agent._prepare_auth_config(
        "https://example.com",
        {
            "login_url": "https://example.com/login",
            "max_steps": "12",
            "_proxify_log_path": "/tmp/proxify.log",
        },
    )

    assert prepared.login_url == "https://example.com/login"
    assert prepared.max_steps == 12
    assert prepared.proxify_log_path == "/tmp/proxify.log"


def test_resolve_secrets_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_EMAIL", "user@example.com")
    monkeypatch.setenv("APP_PASS", "s3cr3t")

    resolved = auth_agent.resolve_secrets(
        {
            "credentials": {
                "email": "{{env:APP_EMAIL}}",
                "password": "{{env:APP_PASS}}",
            },
            "instructions": "Login as {{env:APP_EMAIL}}",
        }
    )

    assert resolved["credentials"]["email"] == "user@example.com"
    assert resolved["credentials"]["password"] == "s3cr3t"
    assert resolved["instructions"] == "Login as user@example.com"


def test_resolve_secrets_missing_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(ValueError, match="MISSING_VAR"):
        auth_agent.resolve_secrets({"credentials": {"email": "{{env:MISSING_VAR}}"}})


def test_resolve_secrets_totp(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

    import pyotp

    monkeypatch.setattr(pyotp.TOTP, "now", lambda self: "123456")
    resolved = auth_agent.resolve_secrets({"credentials": {"otp": "{{totp:APP_TOTP_SECRET}}"}})
    assert resolved["credentials"]["otp"] == "123456"


@pytest.mark.asyncio
async def test_extract_page_state_formats_payload():
    class FakePage:
        async def evaluate(self, _: str, __: int):
            return {
                "url": "https://example.com/login",
                "title": "Login",
                "inputs": [
                    {
                        "tag": "input",
                        "type": "email",
                        "name": "email",
                        "id": "email",
                        "placeholder": "Email",
                        "value": "",
                    }
                ],
                "selects": [],
                "buttons": [{"tag": "button", "type": "submit", "id": "submit", "text": "Sign in"}],
                "links": [{"text": "Forgot password", "href": "/forgot"}],
                "text": "Please sign in",
            }

    state = await auth_agent.extract_page_state(FakePage())
    assert "URL: https://example.com/login" in state
    assert "Title: Login" in state
    assert "Inputs:" in state
    assert "Buttons:" in state
    assert "Links:" in state
    assert "VisibleText:" in state


def test_resolve_model_and_api_key_default_openai(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CRAWLER_AUTH_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    model_id, key, provider, candidates = auth_agent._resolve_model_and_api_key({})

    assert model_id == "gpt-5-mini"
    assert key == "openai-key"
    assert provider == "openai"
    assert candidates == ("OPENAI_API_KEY",)


def test_resolve_model_and_api_key_anthropic(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CRAWLER_AUTH_MODEL", "claude-3-7-sonnet")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)

    model_id, key, provider, candidates = auth_agent._resolve_model_and_api_key({})

    assert model_id == "claude-3-7-sonnet"
    assert key == "anthropic-key"
    assert provider == "anthropic"
    assert candidates == ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")


def test_resolve_model_and_api_key_api_key_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CRAWLER_AUTH_MODEL", "gemini-2.5-pro")
    monkeypatch.setenv("MY_CUSTOM_LLM_KEY", "custom-key")

    model_id, key, provider, candidates = auth_agent._resolve_model_and_api_key(
        {"api_key_env": "MY_CUSTOM_LLM_KEY"}
    )

    assert model_id == "gemini-2.5-pro"
    assert key == "custom-key"
    assert provider == "gemini"
    assert candidates == ("MY_CUSTOM_LLM_KEY",)


@pytest.mark.asyncio
async def test_extract_authorization_headers_logs_warning_when_not_found(
    tmp_path, caplog: pytest.LogCaptureFixture
):
    log_path = tmp_path / "proxify.jsonl"
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {"request": {"url": "https://example.com/home", "headers": {"X-Test": "1"}}}
                ),
                json.dumps(
                    {
                        "request": {
                            "url": "https://example.com/profile",
                            "headers": {"Accept": "*/*"},
                        }
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    caplog.set_level(logging.DEBUG, logger="app.auth_agent")
    headers = await auth_agent.extract_authorization_headers(str(log_path), "https://example.com")

    assert headers == []
    assert "Starting authorization header scan" in caplog.text
    assert "Authorization scan completed with no headers found" in caplog.text


@pytest.mark.asyncio
async def test_extract_authorization_headers_logs_info_when_found(
    tmp_path, caplog: pytest.LogCaptureFixture
):
    log_path = tmp_path / "proxify.jsonl"
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {"request": {"url": "https://example.com/home", "headers": {"X-Test": "1"}}}
                ),
                json.dumps(
                    {
                        "request": {
                            "url": "https://example.com/api/me",
                            "headers": {"Authorization": "Bearer token123"},
                        }
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    caplog.set_level(logging.DEBUG, logger="app.auth_agent")
    headers = await auth_agent.extract_authorization_headers(str(log_path), "https://example.com")

    assert headers == ["Authorization: Bearer token123"]
    assert "Authorization scan found 1 header(s)" in caplog.text
