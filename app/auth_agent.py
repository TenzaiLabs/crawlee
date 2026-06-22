from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

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
    headers: list[str]
    cookies: list[dict[str, Any]] = field(default_factory=list)
    landing_url: str | None = None
    blocked_urls: list[str] = field(default_factory=list)


@dataclass
class _PreparedAuthConfig:
    login_url: str
    credentials_payload: dict[str, Any]
    instructions: str | None
    success_indicator: str | None
    max_steps: int


@dataclass
class _ConfiguredModel:
    model: Any
    chain_key: str | None


@dataclass
class _VerificationResult:
    success: bool
    landing_url: str | None = None
    reason: str | None = None


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

    return _PreparedAuthConfig(
        login_url=login_url,
        credentials_payload=credentials_payload,
        instructions=instructions,
        success_indicator=success_indicator,
        max_steps=max_steps,
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


_REF_ATTR = "data-crawler-auth-ref"
_AUTH_ELEMENT_SELECTOR = (
    "input:not([type='hidden']), textarea, select, button, a[href], [role='button'], "
    "[onclick], input[type='submit'], input[type='button']"
)
_LOGIN_PATH_MARKERS = (
    "login",
    "log-in",
    "signin",
    "sign-in",
    "authenticate",
    "authentication",
    "session",
)
_AUTHENTICATED_LINK_MARKERS = (
    "admin",
    "app",
    "audit",
    "billing",
    "console",
    "dashboard",
    "account",
    "members",
    "portal",
    "profile",
    "projects",
    "reports",
    "settings",
    "team",
    "workspace",
)
_AUTH_UNSAFE_LINK_MARKERS = (
    "delete",
    "disable",
    "logout",
    "remove",
    "signout",
    "sign-out",
    "unsubscribe",
)


def _url_origin(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_same_origin(url: str, base_url: str) -> bool:
    return _url_origin(url) == _url_origin(base_url)


def _is_login_like_url(url: str) -> bool:
    parsed = urlparse(url)
    haystack = f"{parsed.path}?{parsed.query}".lower()
    return any(marker in haystack for marker in _LOGIN_PATH_MARKERS)


def _is_target_root(url: str, target_url: str) -> bool:
    if not _is_same_origin(url, target_url):
        return False
    parsed_url = urlparse(url)
    parsed_target = urlparse(target_url)
    url_path = parsed_url.path.rstrip("/") or "/"
    target_path = parsed_target.path.rstrip("/") or "/"
    return url_path == target_path == "/" and not parsed_url.query


def _is_unsafe_auth_url(url: str) -> bool:
    parsed = urlparse(url)
    haystack = f"{parsed.path}?{parsed.query}".lower()
    return any(marker in haystack for marker in _AUTH_UNSAFE_LINK_MARKERS)


def _looks_like_authenticated_url(url: str) -> bool:
    parsed = urlparse(url)
    haystack = f"{parsed.path}?{parsed.query}".lower()
    if _is_login_like_url(url) or _is_unsafe_auth_url(url):
        return False
    return any(marker in haystack for marker in _AUTHENTICATED_LINK_MARKERS)


def _dedupe_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        clean_url = str(url).strip()
        if not clean_url or clean_url in seen:
            continue
        seen.add(clean_url)
        deduped.append(clean_url)
    return deduped


def _totp_from_secret(secret: str) -> str:
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover
        raise AuthenticationError("pyotp is required for TOTP authentication") from exc
    return pyotp.TOTP(secret).now()


async def _safe_visible_text(page: Any, *, limit: int = 8000) -> str:
    try:
        text = await page.evaluate(
            "(limit) => (document.body ? document.body.innerText : '').slice(0, limit)",
            limit,
        )
    except PlaywrightTimeoutError, PlaywrightError:
        return ""
    return text if isinstance(text, str) else ""


class _AuthBrowserController:
    """LLM-facing Playwright browser controller with page/frame element refs."""

    def __init__(self, context: Any, page: Any, target_url: str) -> None:
        self.context = context
        self.active_page = page
        self.target_url = target_url
        self._element_refs: dict[str, tuple[Any, Any]] = {}

    @property
    def current_url(self) -> str:
        try:
            return str(self.active_page.url or "")
        except Exception:
            return ""

    def _open_pages(self) -> list[Any]:
        return [page for page in self.context.pages if not page.is_closed()]

    def _set_active_page(self, page: Any | None = None) -> None:
        if page is not None and not page.is_closed():
            self.active_page = page
            return
        pages = self._open_pages()
        if pages:
            self.active_page = pages[-1]

    async def _settle(self, page: Any | None = None) -> None:
        selected_page = page or self.active_page
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            await selected_page.wait_for_load_state("domcontentloaded", timeout=5_000)
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            await selected_page.wait_for_load_state("load", timeout=2_000)
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            await selected_page.wait_for_timeout(250)

    async def get_page_state(self, *, text_limit: int = 4000) -> str:
        self._set_active_page()
        self._element_refs.clear()

        lines: list[str] = [
            "Browser state:",
            "Use element refs like p0f0e3 with click/type_text/select_option.",
        ]
        pages = self._open_pages()
        if not pages:
            return "Browser state: no open pages"

        per_frame_text_limit = max(500, text_limit // max(1, len(pages)))
        for page_index, page in enumerate(pages):
            active = " active" if page == self.active_page else ""
            title = ""
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                title = await page.title()
            lines.append(f"Page p{page_index}{active}: url={page.url} title={title}")

            for frame_index, frame in enumerate(page.frames):
                frame_ref = f"p{page_index}f{frame_index}"
                payload = await self._frame_state(frame, frame_ref, per_frame_text_limit)
                if payload is None:
                    lines.append(f"Frame {frame_ref}: url={frame.url} inaccessible")
                    continue

                frame_label = "main" if frame == page.main_frame else "child"
                lines.append(
                    f"Frame {frame_ref} {frame_label}: url={payload['url']} "
                    f"title={payload['title']}"
                )

                elements = payload.get("elements")
                if isinstance(elements, list) and elements:
                    lines.append("Elements:")
                    for item in elements:
                        if not isinstance(item, dict):
                            continue
                        ref = str(item.get("ref") or "")
                        if not ref:
                            continue
                        self._element_refs[ref] = (page, frame)
                        rendered = self._render_element(item)
                        if rendered:
                            lines.append(f"- {rendered}")

                text = payload.get("text")
                if isinstance(text, str) and text.strip():
                    lines.append("VisibleText:")
                    lines.append(text)

        return "\n".join(lines).strip()

    async def _frame_state(self, frame: Any, frame_ref: str, text_limit: int) -> dict | None:
        js = r"""
        ({selector, refAttr, frameRef, textLimit}) => {
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (!style) return false;
            if (
              style.display === 'none' ||
              style.visibility === 'hidden' ||
              style.opacity === '0'
            ) return false;
            if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };
          const attr = (el, name) => (el && el.getAttribute) ? el.getAttribute(name) : '';
          const safeText = (s) => (s || '').toString().replace(/\s+/g, ' ').trim();
          const labelText = (el) => {
            const labels = el.labels
              ? Array.from(el.labels).map(label => safeText(label.innerText))
              : [];
            const closest = el.closest ? el.closest('label') : null;
            if (closest) labels.push(safeText(closest.innerText));
            const aria = attr(el, 'aria-label');
            if (aria) labels.push(aria);
            return labels.filter(Boolean).join(' | ').slice(0, 160);
          };

          const elements = Array.from(document.querySelectorAll(selector))
            .filter(isVisible)
            .slice(0, 120)
            .map((el, idx) => {
              const ref = `${frameRef}e${idx}`;
              el.setAttribute(refAttr, ref);
              const tag = el.tagName.toLowerCase();
              const options = tag === 'select'
                ? Array.from(el.options).slice(0, 20).map(option => ({
                    value: option.value,
                    text: safeText(option.text),
                    selected: option.selected,
                  }))
                : [];
              return {
                ref,
                tag,
                type: attr(el, 'type'),
                role: attr(el, 'role'),
                name: attr(el, 'name'),
                id: attr(el, 'id'),
                placeholder: attr(el, 'placeholder'),
                label: labelText(el),
                text: safeText(el.innerText || el.value || ''),
                href: attr(el, 'href'),
                options,
              };
            });

          return {
            url: location.href,
            title: document.title,
            elements,
            text: safeText(document.body ? document.body.innerText : '').slice(0, textLimit),
          };
        }
        """
        try:
            payload = await frame.evaluate(
                js,
                {
                    "selector": _AUTH_ELEMENT_SELECTOR,
                    "refAttr": _REF_ATTR,
                    "frameRef": frame_ref,
                    "textLimit": text_limit,
                },
            )
        except PlaywrightTimeoutError, PlaywrightError:
            return None
        return payload if isinstance(payload, dict) else None

    def _render_element(self, item: dict[str, Any]) -> str:
        parts = [f"{item.get('ref')}: {item.get('tag', '')}"]
        for key in ("type", "role", "name", "id", "placeholder", "label", "text", "href"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(f"{key}={value[:180]}")
        options = item.get("options")
        if isinstance(options, list) and options:
            rendered_options: list[str] = []
            for option in options:
                if not isinstance(option, dict):
                    continue
                value = str(option.get("value") or "")
                text = str(option.get("text") or "")
                rendered_options.append(f"{value}:{text}" if value != text else text)
            if rendered_options:
                parts.append(f"options={rendered_options}")
        return " ".join(parts)

    async def _locator_for_target(self, target: str) -> tuple[Any, Any]:
        target_text = str(target).strip()
        if target_text in self._element_refs:
            page, frame = self._element_refs[target_text]
            selector = f'[{_REF_ATTR}="{target_text}"]'
            return page, frame.locator(selector).first
        return self.active_page, self.active_page.locator(target_text).first

    async def click(self, target: str) -> str:
        page, locator = await self._locator_for_target(target)
        new_page_task = asyncio.create_task(self.context.wait_for_event("page", timeout=3_000))
        try:
            await locator.click(timeout=10_000)
        except Exception:
            new_page_task.cancel()
            with contextlib.suppress(Exception):
                await new_page_task
            raise

        new_page = None
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError, asyncio.CancelledError):
            new_page = await new_page_task
        self._set_active_page(new_page or page)
        await self._settle(self.active_page)
        return f"clicked {target}; active_url={self.current_url}"

    async def type_text(self, target: str, text: str) -> str:
        page, locator = await self._locator_for_target(target)
        await locator.click(timeout=10_000)
        await locator.fill(str(text), timeout=10_000)
        self._set_active_page(page)
        await self._settle(page)
        return f"typed into {target}"

    async def select_option(self, target: str, value: str) -> str:
        page, locator = await self._locator_for_target(target)
        selected_value = str(value)
        try:
            await locator.select_option(value=selected_value, timeout=10_000)
        except PlaywrightTimeoutError, PlaywrightError:
            await locator.select_option(label=selected_value, timeout=10_000)
        self._set_active_page(page)
        await self._settle(page)
        return f"selected option {value} on {target}"

    async def press_key(self, key: str, target: str | None = None) -> str:
        if target:
            page, locator = await self._locator_for_target(target)
            await locator.press(str(key), timeout=10_000)
            self._set_active_page(page)
        else:
            await self.active_page.keyboard.press(str(key))
        await self._settle(self.active_page)
        return f"pressed {key}"

    async def navigate(self, url: str) -> str:
        base_url = self.current_url or self.target_url
        destination = urljoin(base_url, str(url).strip())
        response = await self.active_page.goto(destination, wait_until="domcontentloaded")
        await self._settle(self.active_page)
        status = response.status if response is not None else "unknown"
        return f"navigated to {self.current_url}; status={status}"

    async def switch_page(self, page_ref: str) -> str:
        ref = str(page_ref).strip().lower()
        if not ref.startswith("p"):
            return "page_ref must look like p0, p1, etc."
        try:
            page_index = int(ref[1:])
        except ValueError:
            return "page_ref must look like p0, p1, etc."
        pages = self._open_pages()
        if page_index < 0 or page_index >= len(pages):
            return f"page {page_ref} is not open"
        self.active_page = pages[page_index]
        await self._settle(self.active_page)
        return f"active page is {page_ref}; active_url={self.current_url}"

    async def collect_link_items(self) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        for page in self._open_pages():
            for frame in page.frames:
                with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                    payload = await frame.evaluate(
                        r"""
                        () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                          href: a.href,
                          text: (a.innerText || a.getAttribute('aria-label') || '')
                            .replace(/\s+/g, ' ')
                            .trim(),
                        }))
                        """
                    )
                    if isinstance(payload, list):
                        for item in payload:
                            if not isinstance(item, dict):
                                continue
                            href = item.get("href")
                            if not isinstance(href, str) or not href.strip():
                                continue
                            text = item.get("text")
                            links.append(
                                {
                                    "href": href.strip(),
                                    "text": text.strip() if isinstance(text, str) else "",
                                }
                            )

        seen: set[str] = set()
        deduped: list[dict[str, str]] = []
        for item in links:
            href = item["href"]
            if href in seen:
                continue
            seen.add(href)
            deduped.append(item)
        return deduped

    async def collect_links(self) -> list[str]:
        return [item["href"] for item in await self.collect_link_items()]


async def _success_indicator_matches(controller: _AuthBrowserController, indicator: str) -> bool:
    indicator_text = str(indicator).strip()
    if not indicator_text:
        return False

    for page in controller._open_pages():
        for frame in page.frames:
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                if await frame.locator(indicator_text).first.is_visible(timeout=500):
                    controller._set_active_page(page)
                    return True

        visible_text = (await _safe_visible_text(page)).lower()
        if indicator_text.lower() in visible_text:
            controller._set_active_page(page)
            return True
    return False


def _link_item_is_auth_candidate(item: dict[str, str], target_url: str) -> bool:
    href = item.get("href", "")
    if not _is_same_origin(href, target_url):
        return False
    if _is_login_like_url(href) or _is_unsafe_auth_url(href):
        return False

    parsed = urlparse(href)
    haystack = f"{parsed.path}?{parsed.query} {item.get('text', '')}".lower()
    return any(marker in haystack for marker in _AUTHENTICATED_LINK_MARKERS)


def _strong_current_url_candidate(
    current_url: str,
    *,
    target_url: str,
    login_url: str,
    requires_auth_evidence: bool,
) -> str | None:
    if not current_url or not _is_same_origin(current_url, target_url):
        return None
    if _is_login_like_url(current_url) or _is_unsafe_auth_url(current_url):
        return None
    if current_url == login_url:
        return None
    if not requires_auth_evidence:
        return current_url
    if _is_target_root(current_url, target_url):
        return None
    return current_url


async def _link_candidates_from_open_pages(
    controller: _AuthBrowserController,
    *,
    target_url: str,
) -> list[str]:
    return [
        item["href"]
        for item in await controller.collect_link_items()
        if _link_item_is_auth_candidate(item, target_url)
    ]


async def _verification_candidates(
    controller: _AuthBrowserController,
    *,
    target_url: str,
    login_url: str,
    requires_auth_evidence: bool,
    probe_url: str | None = None,
) -> list[str]:
    candidates: list[str] = []

    if probe_url:
        resolved_probe_url = urljoin(controller.current_url or target_url, probe_url)
        probe_candidate = _strong_current_url_candidate(
            resolved_probe_url,
            target_url=target_url,
            login_url=login_url,
            requires_auth_evidence=requires_auth_evidence,
        )
        if probe_candidate:
            candidates.append(probe_candidate)

    login_query = parse_qs(urlparse(login_url).query)
    for value in login_query.get("next", []):
        candidates.append(urljoin(login_url, value))

    candidates.extend(await _link_candidates_from_open_pages(controller, target_url=target_url))

    current_candidate = _strong_current_url_candidate(
        controller.current_url,
        target_url=target_url,
        login_url=login_url,
        requires_auth_evidence=requires_auth_evidence,
    )
    if current_candidate:
        candidates.append(current_candidate)

    if requires_auth_evidence:
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            await controller.active_page.goto(target_url, wait_until="domcontentloaded")
            await controller._settle(controller.active_page)
        candidates.extend(await _link_candidates_from_open_pages(controller, target_url=target_url))
    elif not candidates:
        candidates.append(target_url)

    return _dedupe_urls(candidates)


async def _verify_authenticated(
    controller: _AuthBrowserController,
    *,
    target_url: str,
    login_url: str,
    success_indicator: str | None,
    requires_auth_evidence: bool,
    probe_url: str | None = None,
) -> _VerificationResult:
    if success_indicator and await _success_indicator_matches(controller, success_indicator):
        return _VerificationResult(success=True, landing_url=controller.current_url)

    candidates = await _verification_candidates(
        controller,
        target_url=target_url,
        login_url=login_url,
        requires_auth_evidence=requires_auth_evidence,
        probe_url=probe_url,
    )
    if not candidates:
        return _VerificationResult(success=False, reason="no authenticated probe URLs available")

    failures: list[str] = []
    for candidate in candidates[:20]:
        try:
            response = await controller.active_page.goto(candidate, wait_until="domcontentloaded")
            await controller._settle(controller.active_page)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            failures.append(f"{candidate}: navigation error {exc}")
            continue

        final_url = controller.current_url
        status = response.status if response is not None else None
        if status in {401, 403} or (status is not None and status >= 400):
            failures.append(f"{candidate}: status={status} final={final_url}")
            continue
        if _is_login_like_url(final_url):
            failures.append(f"{candidate}: redirected to login final={final_url}")
            continue
        if not _is_same_origin(final_url, target_url):
            failures.append(f"{candidate}: out-of-origin final={final_url}")
            continue

        if success_indicator and not await _success_indicator_matches(
            controller,
            success_indicator,
        ):
            failures.append(f"{candidate}: success indicator not visible")
            continue

        return _VerificationResult(success=True, landing_url=final_url)

    reason = "; ".join(failures[:5]) if failures else "all probes failed"
    return _VerificationResult(success=False, reason=reason)


async def _collect_auth_result(
    *,
    context: Any,
    page: Any,
    login_url: str,
    traffic_capture: auth_traffic.AuthTrafficCapture,
    blocked_urls: list[str] | None = None,
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

    if traffic_capture.redirect_chain:
        logger.debug(
            "Auth redirect chain (%d hops): %s",
            len(traffic_capture.redirect_chain),
            traffic_capture.redirect_chain,
        )

    landing_url = _extract_landing_url(page, login_url)
    logger.info(
        "Auth result: %d header(s), landing_url=%s blocked_urls=%d",
        len(headers),
        landing_url,
        len(blocked_urls or []),
    )
    return AuthResult(
        headers=_dedupe_headers(headers),
        cookies=cookies,
        landing_url=landing_url,
        blocked_urls=blocked_urls or [],
    )


def _build_tool_hooks(
    *,
    cancel_event: asyncio.Event,
    max_steps: int,
) -> tuple[Any, Any]:
    tool_calls = 0

    async def _before_call(_: Any, __: Any) -> None:
        if cancel_event.is_set():
            raise asyncio.CancelledError

    async def _after_call(tool: Any, __: Any, result: Any) -> None:
        nonlocal tool_calls
        tool_calls += 1
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if tool_calls > max_steps:
            raise AuthenticationError(f"Authentication max_steps exceeded ({max_steps})")
        if getattr(tool, "name", None) == "done" and str(result).startswith("verified:"):
            raise _AuthenticationComplete

    return _before_call, _after_call


def _build_tools(
    browser: Any,
    blocked_urls: list[str] | None = None,
    *,
    target_url: str | None = None,
    login_url: str | None = None,
    success_indicator: str | None = None,
    credentials: dict[str, Any] | None = None,
) -> list[Any]:
    blocked_url_sink = blocked_urls if blocked_urls is not None else []
    seen_blocked_urls: set[str] = set(blocked_url_sink)
    controller = browser if isinstance(browser, _AuthBrowserController) else None
    credential_values = credentials or {}

    async def click(target: str) -> str:
        """Click an element by ref from get_page_state, or by CSS selector as fallback."""
        try:
            if controller is not None:
                return await controller.click(target)
            await browser.locator(target).first.click(timeout=10_000)
            return f"clicked {target}"
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error clicking {target}: {exc}"

    async def type_text(target: str, text: str) -> str:
        """Fill text into an input by ref from get_page_state, or CSS selector as fallback."""
        try:
            if controller is not None:
                return await controller.type_text(target, text)
            locator = browser.locator(target).first
            await locator.click(timeout=10_000)
            await locator.fill(str(text), timeout=10_000)
            return f"typed into {target}"
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error typing into {target}: {exc}"

    async def select_option(target: str, value: str) -> str:
        """Select an option from a dropdown by ref from get_page_state."""
        try:
            if controller is not None:
                return await controller.select_option(target, value)
            await browser.locator(target).first.select_option(value=str(value), timeout=10_000)
            return f"selected option {value} on {target}"
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error selecting option on {target}: {exc}"

    async def press_key(key: str, target: str = "") -> str:
        """Press a keyboard key, optionally focused on an element ref."""
        try:
            if controller is not None:
                return await controller.press_key(key, target or None)
            if target:
                await browser.locator(target).first.press(str(key), timeout=10_000)
            else:
                await browser.keyboard.press(str(key))
            return f"pressed {key}"
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error pressing {key}: {exc}"

    async def navigate(url: str) -> str:
        """Navigate the active browser page to an absolute or relative URL."""
        try:
            if controller is not None:
                return await controller.navigate(url)
            response = await browser.goto(str(url), wait_until="domcontentloaded")
            status = response.status if response is not None else "unknown"
            return f"navigated to {getattr(browser, 'url', '')}; status={status}"
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error navigating to {url}: {exc}"

    async def switch_page(page_ref: str) -> str:
        """Switch active page/tab by page ref, for example p0 or p1."""
        if controller is None:
            return "page switching is unavailable"
        return await controller.switch_page(page_ref)

    async def wait(milliseconds: int) -> str:
        """Wait for a specified number of milliseconds."""
        ms = coerce_int(milliseconds, 500)
        if controller is not None:
            await controller.active_page.wait_for_timeout(ms)
        else:
            await browser.wait_for_timeout(ms)
        return f"waited {ms}ms"

    async def get_page_state() -> str:
        """Get current pages, frames, visible text, and element refs for interaction."""
        try:
            if controller is not None:
                return await controller.get_page_state()
            return await extract_page_state(browser)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return f"error extracting page state: {exc}"

    async def verify_authentication(probe_url: str = "") -> str:
        """Probe whether the current browser context can access authenticated content."""
        if controller is None or not target_url or not login_url:
            return "verification unavailable"
        result = await _verify_authenticated(
            controller,
            target_url=target_url,
            login_url=login_url,
            success_indicator=success_indicator,
            requires_auth_evidence=bool(credential_values),
            probe_url=str(probe_url).strip() or None,
        )
        if result.success:
            return f"verified: authenticated landing_url={result.landing_url}"
        return f"not verified: {result.reason or 'authentication did not reach protected content'}"

    async def get_totp_code(secret_or_key: str = "") -> str:
        """Generate a current TOTP code from a seed or a credential key such as totp_secret."""
        requested = str(secret_or_key).strip()
        secret = ""
        source = requested or "default"

        if requested and requested in credential_values:
            secret = str(credential_values[requested])
        elif requested:
            secret = requested
        else:
            for key in ("totp_secret", "mfa_secret", "otp_secret"):
                value = credential_values.get(key)
                if value:
                    secret = str(value)
                    source = key
                    break

        if not secret:
            return "error generating TOTP: no secret or credential key was provided"

        try:
            code = _totp_from_secret(secret)
        except Exception as exc:
            return f"error generating TOTP: {exc}"
        return f"TOTP code from {source}: {code}"

    async def done() -> str:
        """Finish only after authentication verification succeeds."""
        return await verify_authentication()

    async def record_blocked_url(url: str, reason: str = "") -> str:
        """Record a URL that the crawler should avoid after authentication."""
        url_text = str(url).strip()
        if not url_text:
            return "ignored empty blocked URL"
        if len(url_text) > 2048:
            return "ignored blocked URL because it is too long"

        try:
            if controller is not None:
                base_url = controller.current_url
            else:
                base_url = str(getattr(browser, "url", "") or "")
        except Exception:
            base_url = ""
        resolved_url = urljoin(base_url, url_text) if base_url else url_text
        if resolved_url in seen_blocked_urls:
            return "blocked URL already recorded"

        seen_blocked_urls.add(resolved_url)
        blocked_url_sink.append(resolved_url)
        reason_text = str(reason).strip()
        if reason_text:
            logger.info("Auth agent recorded blocked URL hint reason=%s", reason_text[:200])
        else:
            logger.info("Auth agent recorded blocked URL hint")
        return "recorded blocked URL"

    return [
        click,
        type_text,
        select_option,
        press_key,
        navigate,
        switch_page,
        wait,
        get_page_state,
        verify_authentication,
        get_totp_code,
        record_blocked_url,
        done,
    ]


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

    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        # Auth browsing is direct. Proxify stays in the Katana crawl path; routing
        # login traffic through it has caused body/protocol corruption on forms.
        traffic_capture = auth_traffic.AuthTrafficCapture(target_url)
        traffic_capture.attach(page)
        context.on("page", traffic_capture.attach)

        await page.goto(prepared.login_url, wait_until="domcontentloaded")
        controller = _AuthBrowserController(context, page, target_url)
        page_state = await controller.get_page_state()
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
        blocked_urls: list[str] = []
        tools = _build_tools(
            controller,
            blocked_urls,
            target_url=target_url,
            login_url=prepared.login_url,
            success_indicator=prepared.success_indicator,
            credentials=prepared.credentials_payload,
        )
        await _run_auth_with_retries(
            model=configured_model.model,
            user_prompt=user_prompt,
            max_steps=prepared.max_steps,
            chain_key=configured_model.chain_key,
            tools=tools,
            before_call=before_call,
            after_call=after_call,
        )
        verification = await _verify_authenticated(
            controller,
            target_url=target_url,
            login_url=prepared.login_url,
            success_indicator=prepared.success_indicator,
            requires_auth_evidence=bool(prepared.credentials_payload),
        )
        if not verification.success:
            raise AuthenticationError(
                "Authentication verification failed: "
                f"{verification.reason or 'protected content was not reachable'}"
            )
        return await _collect_auth_result(
            context=context,
            page=controller.active_page,
            login_url=prepared.login_url,
            traffic_capture=traffic_capture,
            blocked_urls=blocked_urls,
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
