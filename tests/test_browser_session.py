from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from app import browser_session
from app.process import ProcessMemoryLimitExceeded


def test_chrome_launch_ignores_certificate_errors(tmp_path: Path) -> None:
    command = browser_session._chrome_launch_command("/opt/chrome", tmp_path / "profile")

    assert command[0] == "/opt/chrome"
    assert "--ignore-certificate-errors" in command
    assert f"--user-data-dir={tmp_path / 'profile'}" in command


def test_header_and_cookie_parsing_keeps_cookie_out_of_extra_headers() -> None:
    headers = [
        "Authorization: Bearer token",
        "Cookie: session=abc; preference=compact",
        "X-Empty:",
        "invalid",
    ]

    assert browser_session._header_mapping(headers) == {
        "Authorization": "Bearer token",
        "X-Empty": "",
    }
    assert browser_session._cookie_header_values(headers) == [
        ("session", "abc"),
        ("preference", "compact"),
    ]
    assert browser_session._cookie_url("https://app.example.test/path") == (
        "https://app.example.test/"
    )


@pytest.mark.asyncio
async def test_guard_cancels_work_when_observer_disconnects() -> None:
    class FakeObserver:
        def __init__(self) -> None:
            self.disconnected = asyncio.Event()

    class FakeProcess:
        returncode = None

    session = browser_session.BrowserSession("job-guard")
    cast(Any, session).process = FakeProcess()
    observer = FakeObserver()
    cast(Any, session).observer = observer
    work_cancelled = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            work_cancelled.set()

    task = asyncio.create_task(session.guard(work()))
    await asyncio.sleep(0)
    observer.disconnected.set()

    with pytest.raises(browser_session.BrowserDisconnectedError):
        await task
    assert work_cancelled.is_set()


@pytest.mark.asyncio
async def test_guard_cancels_work_when_shared_memory_budget_is_exceeded() -> None:
    class FakeObserver:
        def __init__(self) -> None:
            self.disconnected = asyncio.Event()

    class FakeProcess:
        returncode = None

    session = browser_session.BrowserSession("job-memory-guard")
    cast(Any, session).process = FakeProcess()
    cast(Any, session).observer = FakeObserver()
    session.memory_budget.observed_bytes = session.memory_budget.limit_bytes + 1
    work_cancelled = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            work_cancelled.set()

    task = asyncio.create_task(session.guard(work()))
    await asyncio.sleep(0)
    session.memory_budget.exceeded.set()

    with pytest.raises(ProcessMemoryLimitExceeded, match="memory ceiling exceeded"):
        await task
    assert work_cancelled.is_set()


@pytest.mark.asyncio
async def test_disconnect_playwright_closes_pages_without_closing_chrome() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self, pages: list[FakePage]) -> None:
            self.pages = pages

    class FakePlaywright:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class FakeBrowser:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    pages = [FakePage(), FakePage()]
    playwright = FakePlaywright()
    browser = FakeBrowser()
    session = browser_session.BrowserSession("job-handoff")
    cast(Any, session)._context = FakeContext(pages)
    cast(Any, session)._playwright = playwright
    cast(Any, session)._browser = browser

    await session.disconnect_playwright()

    assert all(page.closed for page in pages)
    assert playwright.stopped is True
    assert browser.closed is False
    assert session._context is None


@pytest.mark.asyncio
async def test_stop_removes_only_the_job_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "state").write_text("ephemeral")
    neighbor = tmp_path / "neighbor"
    neighbor.mkdir()

    session = browser_session.BrowserSession("job-cleanup")
    session.profile_dir = profile

    await session.stop()

    assert not profile.exists()
    assert neighbor.is_dir()
