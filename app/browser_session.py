from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import urlparse

import httpx
from playwright._impl._api_structures import SetCookieParam
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from websockets.asyncio.client import connect

from .cdp_observer import PassiveCDPObserver
from .process import ProcessMemoryBudget, ProcessMemoryLimitExceeded
from .settings import CRAWLER_BROWSER_STARTUP_TIMEOUT_SECONDS, CRAWLER_JOB_MEMORY_LIMIT_BYTES

T = TypeVar("T")


class BrowserSessionError(RuntimeError):
    pass


class BrowserDisconnectedError(BrowserSessionError):
    pass


def _chrome_launch_command(executable: str, profile_dir: Path) -> list[str]:
    return [
        executable,
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--ignore-certificate-errors",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "about:blank",
    ]


def _header_mapping(headers: list[str] | None) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for header in headers or []:
        name, separator, value = str(header).partition(":")
        if not separator or not name.strip():
            continue
        if name.strip().lower() == "cookie":
            continue
        mapped[name.strip()] = value.strip()
    return mapped


def _cookie_header_values(headers: list[str] | None) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for header in headers or []:
        name, separator, value = str(header).partition(":")
        if not separator or name.strip().lower() != "cookie":
            continue
        for item in value.split(";"):
            cookie_name, cookie_separator, cookie_value = item.strip().partition("=")
            if cookie_separator and cookie_name:
                values.append((cookie_name.strip(), cookie_value.strip()))
    return values


def _cookie_url(target_url: str) -> str:
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("target_url must be an absolute HTTP(S) URL")
    return f"{parsed.scheme}://{parsed.netloc}/"


async def _wait_for_cdp(
    profile_dir: Path,
    process: asyncio.subprocess.Process,
) -> tuple[str, str]:
    active_port_path = profile_dir / "DevToolsActivePort"
    deadline = asyncio.get_running_loop().time() + CRAWLER_BROWSER_STARTUP_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise BrowserSessionError(
                f"Chrome exited before CDP readiness with code {process.returncode}"
            )
        if active_port_path.is_file():
            lines = await asyncio.to_thread(active_port_path.read_text)
            parts = lines.splitlines()
            if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                port = parts[0].strip()
                websocket_url = f"ws://127.0.0.1:{port}{parts[1].strip()}"
                try:
                    async with httpx.AsyncClient(timeout=2) as client:
                        response = await client.get(f"http://127.0.0.1:{port}/json/version")
                        response.raise_for_status()
                        product = str(response.json().get("Browser", "unknown"))
                except httpx.HTTPError, ValueError:
                    await asyncio.sleep(0.1)
                    continue
                return websocket_url, product
        await asyncio.sleep(0.1)
    raise BrowserSessionError("Chrome did not publish a ready CDP endpoint")


async def _stop_process(process: asyncio.subprocess.Process, *, timeout: float = 10) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def _browser_cdp_call(
    websocket_url: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with connect(websocket_url, max_size=4 * 1024 * 1024) as connection:
        await connection.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        async for raw_message in connection:
            message = json.loads(raw_message)
            if not isinstance(message, dict) or message.get("id") != 1:
                continue
            error = message.get("error")
            if isinstance(error, dict):
                raise BrowserSessionError(str(error.get("message", error)))
            result = message.get("result")
            return result if isinstance(result, dict) else {}
    raise BrowserDisconnectedError("Chrome disconnected during a CDP command")


class BrowserSession:
    """Own one job-scoped Chrome and sequential Playwright/Katana epochs."""

    def __init__(self, job_id: str) -> None:
        safe_job_id = re.sub(r"[^a-zA-Z0-9_.-]", "-", job_id)[:48] or "job"
        self.job_id = job_id
        self._profile_prefix = f"tenzai-crawler-{safe_job_id}-"
        self.profile_dir: Path | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.websocket_url: str | None = None
        self.chrome_product: str | None = None
        self.observer: PassiveCDPObserver | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._headers: list[str] = []
        self._target_url: str | None = None
        self._stopped = False
        self.memory_budget = ProcessMemoryBudget(CRAWLER_JOB_MEMORY_LIMIT_BYTES)

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise BrowserSessionError("Playwright is not connected to the job browser")
        return self._context

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if self.process is not None:
            raise BrowserSessionError("job browser is already started")
        self.profile_dir = Path(tempfile.mkdtemp(prefix=self._profile_prefix))
        locator = await async_playwright().start()
        try:
            executable = locator.chromium.executable_path
        finally:
            await locator.stop()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *_chrome_launch_command(executable, self.profile_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self.memory_budget.register(self.process.pid)
            await self.memory_budget.start()
            self.websocket_url, self.chrome_product = await _wait_for_cdp(
                self.profile_dir,
                self.process,
            )
            self.observer = PassiveCDPObserver(self.websocket_url)
            await self.observer.start()
        except BaseException:
            await self.stop()
            raise

    async def connect_playwright(
        self,
        *,
        headers: list[str] | None = None,
        target_url: str | None = None,
        epoch: str,
    ) -> BrowserContext:
        self.ensure_alive()
        if self._playwright is not None:
            raise BrowserSessionError("Playwright already owns the job browser")
        if headers is not None:
            self._headers = list(headers)
        if target_url is not None:
            self._target_url = target_url
        assert self.websocket_url is not None
        if self.observer is not None:
            self.observer.set_epoch(epoch)
        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.connect_over_cdp(self.websocket_url)
            if not browser.contexts:
                raise BrowserSessionError("Chrome CDP connection has no default context")
            context = browser.contexts[0]
            await context.set_extra_http_headers(_header_mapping(self._headers))
            if self._target_url is not None:
                manual_cookies: list[SetCookieParam] = [
                    {"name": name, "value": value, "url": _cookie_url(self._target_url)}
                    for name, value in _cookie_header_values(self._headers)
                ]
                if manual_cookies:
                    await context.add_cookies(manual_cookies)
        except BaseException:
            await playwright.stop()
            raise
        self._playwright = playwright
        self._browser = browser
        self._context = context
        return context

    async def configure_auth(
        self,
        *,
        headers: list[str],
        cookies: list[dict[str, Any]],
        target_url: str,
    ) -> None:
        self._headers = list(headers)
        self._target_url = target_url
        context = self.context
        await context.set_extra_http_headers(_header_mapping(headers))
        if cookies:
            await context.add_cookies(cast("list[SetCookieParam]", cookies))

    async def close_playwright_pages(self) -> None:
        if self._context is None:
            return
        for page in tuple(self._context.pages):
            if page.is_closed():
                continue
            with contextlib.suppress(Exception):
                await page.close()

    async def disconnect_playwright(self) -> None:
        await self.close_playwright_pages()
        playwright = self._playwright
        self._context = None
        self._browser = None
        self._playwright = None
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()

    async def begin_katana_epoch(self, epoch: str) -> str:
        await self.disconnect_playwright()
        self.ensure_alive()
        if self.observer is not None:
            self.observer.set_epoch(epoch)
        assert self.websocket_url is not None
        return self.websocket_url

    async def close_leftover_page_targets(self) -> None:
        self.ensure_alive()
        assert self.websocket_url is not None
        targets = await _browser_cdp_call(self.websocket_url, "Target.getTargets")
        for target in targets.get("targetInfos", []):
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            target_id = target.get("targetId")
            if isinstance(target_id, str) and target_id:
                with contextlib.suppress(BrowserSessionError):
                    await _browser_cdp_call(
                        self.websocket_url,
                        "Target.closeTarget",
                        {"targetId": target_id},
                    )

    async def guard(self, awaitable: Awaitable[T]) -> T:
        if self.observer is None:
            raise BrowserSessionError("job browser observer is not started")
        self.ensure_alive()
        work = asyncio.ensure_future(awaitable)
        disconnected = asyncio.create_task(self.observer.disconnected.wait())
        memory_exceeded = asyncio.create_task(self.memory_budget.exceeded.wait())
        try:
            done, _ = await asyncio.wait(
                {work, disconnected, memory_exceeded},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if memory_exceeded in done:
                work.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await work
                raise ProcessMemoryLimitExceeded(self.memory_budget.failure_message)
            if disconnected in done:
                work.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await work
                raise BrowserDisconnectedError("job Chrome or passive CDP observer disconnected")
            return await work
        finally:
            disconnected.cancel()
            memory_exceeded.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await disconnected
            with contextlib.suppress(asyncio.CancelledError):
                await memory_exceeded

    def ensure_alive(self) -> None:
        self.memory_budget.raise_if_exceeded()
        if self._stopped or self.process is None or self.process.returncode is not None:
            raise BrowserDisconnectedError("job Chrome is not running")
        if self.observer is not None and self.observer.disconnected.is_set():
            raise BrowserDisconnectedError("passive CDP observer is disconnected")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        await self.disconnect_playwright()
        if self.observer is not None:
            await self.observer.stop()
        await self.memory_budget.stop()
        if self.process is not None:
            self.memory_budget.unregister(self.process.pid)
            await _stop_process(self.process)
        if self.profile_dir is not None:
            await asyncio.to_thread(shutil.rmtree, self.profile_dir, True)
        self.observer = None
        self.process = None
        self.websocket_url = None
        self.chrome_product = None
