from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

import llm
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from . import auth_browser, auth_log_extract, auth_model, auth_secrets, auth_traffic
from .common import coerce_int
from .settings import (
    CRAWLER_AUTH_ATTEMPTS,
    CRAWLER_AUTH_MAX_STEPS_DEFAULT,
    CRAWLER_AUTH_RETRY_BASE_SECONDS,
)

logger = logging.getLogger(__name__)


@dataclass
class AuthResult:
    cookies: list[dict]
    headers: list[str]
    landing_url: str | None = None


@dataclass
class _PreparedAuthConfig:
    login_url: str
    credentials_payload: dict[str, Any]
    instructions: str | None
    success_indicator: str | None
    max_steps: int
    proxify_log_path: str | None


@dataclass
class _ConfiguredModel:
    model: Any
    chain_key: str | None


class AuthenticationError(RuntimeError):
    pass


class _AuthenticationComplete(Exception):
    """Internal control-flow exception raised when the model calls done()."""


def needs_auth(auth_config: dict | None) -> bool:
    if not isinstance(auth_config, dict):
        return False
    login_url = auth_config.get("login_url")
    if isinstance(login_url, str) and login_url.strip():
        return True
    credentials = auth_config.get("credentials")
    return isinstance(credentials, dict) and bool(credentials)


def resolve_secrets(auth_config: dict) -> dict:
    return auth_secrets.resolve_secrets(auth_config)


def _resolve_model_and_api_key(
    auth_config: dict[str, Any],
) -> tuple[str, str | None, str, tuple[str, ...]]:
    return auth_model.resolve_model_and_api_key(auth_config)


async def extract_authorization_headers(
    log_path: str | None,
    target_url: str,
    *,
    max_bytes: int = 1_000_000,
) -> list[str]:
    logger.info("Starting authorization header scan")
    headers = await auth_log_extract.extract_authorization_headers_from_log(
        log_path,
        target_url,
        max_bytes=max_bytes,
    )
    if headers:
        logger.info("Authorization scan found %d header(s)", len(headers))
    else:
        logger.warning("Authorization scan completed with no headers found")
    return headers


async def extract_page_state(page: Any, *, text_limit: int = 4000) -> str:
    return await auth_browser.extract_page_state(page, text_limit=text_limit)


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_headers: list[str] = []
    for header in headers:
        if header in seen:
            continue
        seen.add(header)
        unique_headers.append(header)
    return unique_headers


def _prepare_auth_config(target_url: str, auth_config: dict[str, Any]) -> _PreparedAuthConfig:
    login_url = auth_config.get("login_url")
    if not isinstance(login_url, str) or not login_url.strip():
        login_url = target_url

    credentials = auth_config.get("credentials")
    credentials_payload: dict[str, Any] = {}
    if isinstance(credentials, dict):
        credentials_payload = {str(k): v for k, v in credentials.items()}

    instructions = auth_config.get("instructions")
    if not isinstance(instructions, str):
        instructions = None

    success_indicator = auth_config.get("success_indicator")
    if not isinstance(success_indicator, str):
        success_indicator = None

    max_steps = coerce_int(auth_config.get("max_steps"), CRAWLER_AUTH_MAX_STEPS_DEFAULT)
    if max_steps <= 0:
        max_steps = CRAWLER_AUTH_MAX_STEPS_DEFAULT

    proxify_log_path = auth_config.get("_proxify_log_path") or auth_config.get("proxify_log_path")
    if not isinstance(proxify_log_path, str):
        proxify_log_path = None

    return _PreparedAuthConfig(
        login_url=login_url,
        credentials_payload=credentials_payload,
        instructions=instructions,
        success_indicator=success_indicator,
        max_steps=max_steps,
        proxify_log_path=proxify_log_path,
    )


def _configure_model(auth_config: dict[str, Any]) -> _ConfiguredModel:
    model_id, key, provider, key_candidates = _resolve_model_and_api_key(auth_config)
    model = llm.get_async_model(model_id)

    chain_key: str | None = None
    if key:
        with contextlib.suppress(Exception):
            model.key = key
        if getattr(model, "key", None) != key:
            chain_key = key
        logger.info("Configured auth model=%s provider=%s using API key source", model_id, provider)
    else:
        if key_candidates:
            logger.warning(
                "No API key found for provider=%s model=%s. Checked: %s",
                provider,
                model_id,
                ", ".join(key_candidates),
            )
        else:
            logger.warning("No API key configured for provider=%s model=%s", provider, model_id)

    return _ConfiguredModel(model=model, chain_key=chain_key)


def _extract_landing_url(page: Any, login_url: str) -> str | None:
    try:
        current_url = page.url
        if current_url and current_url != login_url:
            return current_url
    except Exception:
        return None
    return None


async def _collect_auth_result(
    *,
    context: Any,
    page: Any,
    login_url: str,
    traffic_capture: auth_traffic.AuthTrafficCapture,
    proxify_log_path: str | None,
    target_url: str,
) -> AuthResult:
    cookies_raw = await context.cookies() if context is not None else []
    cookies = [dict(cookie) for cookie in cookies_raw]
    logger.info("Auth completed: captured %d cookie(s)", len(cookies))

    headers: list[str] = []
    cookie_header = auth_log_extract.format_cookie_header(cookies)
    if cookie_header:
        headers.append(f"Cookie: {cookie_header}")

    browser_headers = traffic_capture.captured_headers
    if browser_headers:
        logger.info("Browser traffic capture found %d auth header(s)", len(browser_headers))
    headers.extend(browser_headers)

    headers.extend(await extract_authorization_headers(proxify_log_path, target_url))

    if traffic_capture.redirect_chain:
        logger.debug(
            "Auth redirect chain (%d hops): %s",
            len(traffic_capture.redirect_chain),
            traffic_capture.redirect_chain,
        )

    landing_url = _extract_landing_url(page, login_url)
    logger.info("Auth result: %d header(s), landing_url=%s", len(headers), landing_url)
    return AuthResult(cookies=cookies, headers=_dedupe_headers(headers), landing_url=landing_url)


def _build_tool_hooks(
    *,
    cancel_event: asyncio.Event,
    max_steps: int,
) -> tuple[Any, Any]:
    tool_calls = 0

    async def _before_call(_: Any, __: Any) -> None:
        if cancel_event.is_set():
            raise asyncio.CancelledError

    async def _after_call(tool: Any, __: Any, ___: Any) -> None:
        nonlocal tool_calls
        tool_calls += 1
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if tool_calls > max_steps:
            raise AuthenticationError(f"Authentication max_steps exceeded ({max_steps})")
        if getattr(tool, "name", None) == "done":
            raise _AuthenticationComplete

    return _before_call, _after_call


def _build_tools(page: Any) -> list[Any]:
    async def click(selector: str) -> str:
        """Click an element matching the CSS selector."""
        try:
            await page.locator(selector).first.click(timeout=10_000)
            return f"clicked {selector}"
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error clicking {selector}: {exc}"

    async def type_text(selector: str, text: str) -> str:
        """Type text into an input field matching the CSS selector."""
        try:
            locator = page.locator(selector).first
            await locator.click(timeout=10_000)
            await locator.fill(str(text), timeout=10_000)
            return f"typed into {selector}"
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error typing into {selector}: {exc}"

    async def select_option(selector: str, value: str) -> str:
        """Select an option from a dropdown by value."""
        try:
            await page.locator(selector).first.select_option(value=str(value), timeout=10_000)
            return f"selected option {value} on {selector}"
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error selecting option on {selector}: {exc}"

    async def wait(milliseconds: int) -> str:
        """Wait for a specified number of milliseconds."""
        ms = coerce_int(milliseconds, 500)
        await page.wait_for_timeout(ms)
        return f"waited {ms}ms"

    async def get_page_state() -> str:
        """Get simplified DOM state: visible text, forms, inputs, buttons, links."""
        try:
            return await extract_page_state(page)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error extracting page state: {exc}"

    async def done() -> str:
        """Signal that authentication is complete."""
        return "done"

    return [click, type_text, select_option, wait, get_page_state, done]


async def _run_auth_chain(
    *,
    model: Any,
    user_prompt: str,
    max_steps: int,
    chain_key: str | None,
    tools: list[Any],
    before_call: Any,
    after_call: Any,
) -> None:
    conversation = model.conversation()
    chain_kwargs: dict[str, Any] = {
        "prompt": user_prompt,
        "system": auth_browser.SYSTEM_PROMPT,
        "tools": tools,
        "stream": False,
        "before_call": before_call,
        "after_call": after_call,
        "chain_limit": max_steps + 5,
    }
    if chain_key:
        chain_kwargs["key"] = chain_key
    chain = conversation.chain(**chain_kwargs)
    await chain.text()


async def _run_auth_with_retries(
    *,
    model: Any,
    user_prompt: str,
    max_steps: int,
    chain_key: str | None,
    tools: list[Any],
    before_call: Any,
    after_call: Any,
) -> None:
    for attempt in range(CRAWLER_AUTH_ATTEMPTS):
        try:
            await _run_auth_chain(
                model=model,
                user_prompt=user_prompt,
                max_steps=max_steps,
                chain_key=chain_key,
                tools=tools,
                before_call=before_call,
                after_call=after_call,
            )
            break
        except _AuthenticationComplete:
            break
        except AuthenticationError, asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt >= CRAWLER_AUTH_ATTEMPTS - 1:
                raise AuthenticationError(f"LLM authentication failed: {exc}") from exc
            await asyncio.sleep(CRAWLER_AUTH_RETRY_BASE_SECONDS * (2**attempt))


async def authenticate(
    target_url: str, auth_config: dict, cancel_event: asyncio.Event
) -> AuthResult:
    if not isinstance(auth_config, dict):
        raise TypeError("auth_config must be a dict")
    if cancel_event.is_set():
        raise asyncio.CancelledError

    prepared = _prepare_auth_config(target_url, auth_config)
    configured_model = _configure_model(auth_config)

    proxy_url = "http://127.0.0.1:8888"

    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            proxy={"server": proxy_url},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # Option 3 – capture auth headers / SAML traffic in-browser.
        traffic_capture = auth_traffic.AuthTrafficCapture(target_url)
        traffic_capture.attach(page)

        # Option 2 – bypass the proxy for POST/PUT/PATCH so Proxify cannot
        # corrupt the request body (see docs/proxify-bug-report.md).
        async def _proxy_bypass(route) -> None:
            await auth_traffic.proxy_bypass_route_handler(route, context)

        await page.route("**/*", _proxy_bypass)

        await page.goto(prepared.login_url, wait_until="domcontentloaded")
        page_state = await extract_page_state(page)
        user_prompt = auth_browser.build_user_prompt(
            page_state=page_state,
            target_url=target_url,
            credentials=prepared.credentials_payload,
            instructions=prepared.instructions,
            success_indicator=prepared.success_indicator,
        )

        before_call, after_call = _build_tool_hooks(
            cancel_event=cancel_event,
            max_steps=prepared.max_steps,
        )
        tools = _build_tools(page)
        await _run_auth_with_retries(
            model=configured_model.model,
            user_prompt=user_prompt,
            max_steps=prepared.max_steps,
            chain_key=configured_model.chain_key,
            tools=tools,
            before_call=before_call,
            after_call=after_call,
        )
        return await _collect_auth_result(
            context=context,
            page=page,
            login_url=prepared.login_url,
            traffic_capture=traffic_capture,
            proxify_log_path=prepared.proxify_log_path,
            target_url=target_url,
        )
    finally:
        with contextlib.suppress(Exception):
            if context is not None:
                await context.close()
        with contextlib.suppress(Exception):
            if browser is not None:
                await browser.close()
        with contextlib.suppress(Exception):
            await playwright.stop()
