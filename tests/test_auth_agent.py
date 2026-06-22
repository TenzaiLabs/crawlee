from __future__ import annotations

from typing import cast

import pytest

from app import auth_agent, auth_browser


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
    assert prepared.probe_url is None


def test_prepare_auth_config_coerces_max_steps():
    prepared = auth_agent._prepare_auth_config(
        "https://example.com",
        {
            "login_url": "https://example.com/login",
            "max_steps": "12",
        },
    )

    assert prepared.login_url == "https://example.com/login"
    assert prepared.max_steps == 12


def test_prepare_auth_config_accepts_probe_url():
    prepared = auth_agent._prepare_auth_config(
        "https://example.com",
        {
            "login_url": "https://example.com/login",
            "probe_url": " /app/dashboard ",
        },
    )

    assert prepared.probe_url == "/app/dashboard"


def test_auth_prompt_includes_probe_url() -> None:
    prompt = auth_browser.build_user_prompt(
        page_state="Login form",
        target_url="https://example.com",
        credentials={},
        instructions=None,
        success_indicator=None,
        probe_url="/app/dashboard",
    )

    assert "Protected probe URL for verification: /app/dashboard" in prompt


def test_explicit_probe_candidates_cover_target_login_current_and_trailing_slash() -> None:
    assert auth_agent._explicit_probe_candidates(
        "dashboard",
        target_url="https://example.com/",
        login_url="https://example.com/login",
        current_url="https://example.com/app/current",
    ) == [
        "https://example.com/dashboard",
        "https://example.com/dashboard/",
        "https://example.com/login/dashboard",
        "https://example.com/login/dashboard/",
        "https://example.com/app/current/dashboard",
        "https://example.com/app/current/dashboard/",
    ]


def test_explicit_probe_candidates_keep_absolute_and_root_relative_same_origin() -> None:
    assert auth_agent._explicit_probe_candidates(
        "/dashboard",
        target_url="https://example.com/base",
        login_url="https://example.com/login",
        current_url="https://example.com/login",
    ) == [
        "https://example.com/dashboard",
        "https://example.com/dashboard/",
    ]
    assert auth_agent._explicit_probe_candidates(
        "https://example.com/app/dashboard?tab=home#section",
        target_url="https://example.com/",
        login_url="https://example.com/login",
        current_url="https://example.com/login",
    ) == ["https://example.com/app/dashboard?tab=home"]
    assert (
        auth_agent._explicit_probe_candidates(
            "https://evil.example.com/dashboard",
            target_url="https://example.com/",
            login_url="https://example.com/login",
            current_url="https://example.com/login",
        )
        == []
    )


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


@pytest.mark.asyncio
async def test_build_tools_records_blocked_urls() -> None:
    class FakePage:
        url = "https://example.com/app/dashboard"

    blocked_urls: list[str] = []
    tools = auth_agent._build_tools(FakePage(), blocked_urls)
    record_blocked_url = next(tool for tool in tools if tool.__name__ == "record_blocked_url")

    assert await record_blocked_url("/logout", "sign out link") == "recorded blocked URL"
    assert await record_blocked_url("delete", "destructive action") == "recorded blocked URL"
    assert await record_blocked_url("/logout", "duplicate") == "blocked URL already recorded"
    assert await record_blocked_url("", "empty") == "ignored empty blocked URL"

    assert blocked_urls == [
        "https://example.com/logout",
        "https://example.com/app/delete",
    ]


def test_discovered_url_candidate_filters_unsafe_and_cross_origin_links() -> None:
    assert (
        auth_agent._discovered_url_candidate("/conference/2026#top", "https://example.com/")
        == "https://example.com/conference/2026"
    )
    assert auth_agent._discovered_url_candidate("/User/Login", "https://example.com/") is None
    assert auth_agent._discovered_url_candidate("/logout", "https://example.com/") is None
    assert (
        auth_agent._discovered_url_candidate(
            "https://other.example.com/app", "https://example.com/"
        )
        is None
    )


@pytest.mark.asyncio
async def test_discovered_urls_from_open_pages_returns_safe_same_origin_links() -> None:
    class FakeController:
        async def collect_link_items(self) -> list[dict[str, str]]:
            return [
                {"href": "https://example.com/projects/alpha", "text": "Project Alpha"},
                {"href": "https://example.com/projects/alpha#section", "text": "duplicate"},
                {"href": "https://example.com/User/Logout", "text": "Logout"},
                {"href": "https://docs.example.com/help", "text": "External Docs"},
            ]

    urls = await auth_agent._discovered_urls_from_open_pages(
        cast(auth_agent._AuthBrowserController, FakeController()),
        target_url="https://example.com/",
    )

    assert urls == ["https://example.com/projects/alpha"]


@pytest.mark.asyncio
async def test_build_tools_generates_totp_from_credential_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTotp:
        def __init__(self, secret: str) -> None:
            self.secret = secret

        def now(self) -> str:
            assert self.secret == "JBSWY3DPEHPK3PXP"
            return "654321"

    import pyotp

    monkeypatch.setattr(pyotp, "TOTP", FakeTotp)

    tools = auth_agent._build_tools(
        object(),
        credentials={"totp_secret": "JBSWY3DPEHPK3PXP"},
    )
    get_totp_code = next(tool for tool in tools if tool.__name__ == "get_totp_code")

    assert await get_totp_code("totp_secret") == "TOTP code from totp_secret: 654321"


@pytest.mark.asyncio
async def test_build_tools_remembers_successful_configured_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        url = "https://example.com/app"

    async def fake_verify(*args, **kwargs):
        assert kwargs["probe_url"] == "/app/dashboard"
        return auth_agent._VerificationResult(
            success=True,
            landing_url="https://example.com/app/dashboard",
        )

    monkeypatch.setattr(auth_agent, "_verify_authenticated", fake_verify)
    memory = auth_agent._VerificationMemory()
    controller = auth_agent._AuthBrowserController(None, FakePage(), "https://example.com/")
    tools = auth_agent._build_tools(
        controller,
        target_url="https://example.com/",
        login_url="https://example.com/login",
        configured_probe_url="/app/dashboard",
        credentials={"email": "user@example.com"},
        verification_memory=memory,
    )
    verify_authentication = next(tool for tool in tools if tool.__name__ == "verify_authentication")

    assert await verify_authentication() == (
        "verified: authenticated landing_url=https://example.com/app/dashboard"
    )
    assert memory.probe_url == "/app/dashboard"
    assert memory.result is not None
    assert memory.result.landing_url == "https://example.com/app/dashboard"


@pytest.mark.asyncio
async def test_verification_candidates_reject_public_root_for_credentialed_auth() -> None:
    class FakeResponse:
        status = 200

    class FakePage:
        def __init__(self, controller) -> None:
            self.controller = controller

        async def goto(self, url: str, *, wait_until: str):
            self.controller.current_url = url
            return FakeResponse()

    class FakeController:
        def __init__(self) -> None:
            self.current_url = "https://example.com/"
            self.active_page = FakePage(self)

        async def _settle(self, page) -> None:
            return None

        async def collect_link_items(self) -> list[dict[str, str]]:
            return [{"href": "https://example.com/about", "text": "About"}]

    controller = FakeController()

    candidates = await auth_agent._verification_candidates(
        cast(auth_agent._AuthBrowserController, controller),
        target_url="https://example.com/",
        login_url="https://example.com/login",
        requires_auth_evidence=True,
    )

    assert candidates == []


@pytest.mark.asyncio
async def test_verification_candidates_allow_public_root_for_no_auth_sanity() -> None:
    class FakeController:
        current_url = "https://example.com/"

        async def collect_link_items(self) -> list[dict[str, str]]:
            return []

    candidates = await auth_agent._verification_candidates(
        cast(auth_agent._AuthBrowserController, FakeController()),
        target_url="https://example.com/",
        login_url="https://example.com/",
        requires_auth_evidence=False,
    )

    assert candidates == ["https://example.com/"]


@pytest.mark.asyncio
async def test_verification_candidates_discover_authenticated_links_after_target_visit() -> None:
    class FakeResponse:
        status = 200

    class FakePage:
        def __init__(self, controller) -> None:
            self.controller = controller

        async def goto(self, url: str, *, wait_until: str):
            self.controller.current_url = url
            return FakeResponse()

    class FakeController:
        def __init__(self) -> None:
            self.current_url = "https://example.com/popup-login"
            self.active_page = FakePage(self)

        async def _settle(self, page) -> None:
            return None

        async def collect_link_items(self) -> list[dict[str, str]]:
            if self.current_url == "https://example.com/":
                return [{"href": "https://example.com/workspace", "text": "Workspace"}]
            return []

    controller = FakeController()

    candidates = await auth_agent._verification_candidates(
        cast(auth_agent._AuthBrowserController, controller),
        target_url="https://example.com/",
        login_url="https://example.com/login",
        requires_auth_evidence=True,
    )

    assert candidates == ["https://example.com/workspace"]


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
