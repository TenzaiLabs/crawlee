from __future__ import annotations

import os
from collections import Counter

import httpx
import pytest
from playwright.async_api import async_playwright

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1",
    reason="browser fixture E2E requires RUN_E2E=1 and the testsite stack",
)

HARNESS_TOKEN = os.environ.get("TEST_HARNESS_TOKEN", "")


async def _reset(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.post(
            "/_test/reset",
            headers={"X-Test-Harness-Token": HARNESS_TOKEN},
        )
        response.raise_for_status()


async def _ledger(base_url: str, run_id: str) -> list[dict[str, str]]:
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.get(
            f"/_test/ledger/{run_id}",
            headers={"X-Test-Harness-Token": HARNESS_TOKEN},
        )
        response.raise_for_status()
        return response.json()["entries"]


def _route_counts(entries: list[dict[str, str]]) -> Counter[tuple[str, str]]:
    return Counter((entry["method"], entry["route"]) for entry in entries)


def _assert_no_forbidden(entries: list[dict[str, str]]) -> None:
    forbidden = [entry for entry in entries if entry["classification"] == "forbidden"]
    assert forbidden == []


@pytest.mark.asyncio
async def test_site_b_authenticated_wizards_are_browser_reachable_and_safe() -> None:
    assert HARNESS_TOKEN, "TEST_HARNESS_TOKEN must be set for fixture E2E"
    base_url = "http://localhost:8002"
    run_id = "fixture-site-b"
    await _reset(base_url)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(extra_http_headers={"X-Crawler-Test-Run": run_id})
        page = await context.new_page()
        await page.goto(f"{base_url}/login")
        await page.get_by_label("Username").fill("demo")
        await page.get_by_label("Password").fill("password")
        await page.get_by_role("button", name="Login").click()
        await page.get_by_role("button", name="Open workflow center").click()
        await page.get_by_role("button", name="Show workflow tools").click()
        await page.get_by_role("tab", name="Onboarding").click()
        await page.get_by_role("button", name="Validate onboarding").click()
        await page.get_by_role("button", name="Preview onboarding draft").click()
        await page.get_by_text("harbor-onboarding-draft: preview").wait_for()
        await page.get_by_role("tab", name="Settings validation").click()
        await page.get_by_role("button", name="Validate settings").click()
        await page.get_by_text("Settings are valid and were not saved.").wait_for()
        await browser.close()

    entries = await _ledger(base_url, run_id)
    counts = _route_counts(entries)
    assert counts[("POST", "/api/onboarding/validate")] == 1
    assert counts[("POST", "/api/onboarding/preview")] == 1
    assert counts[("POST", "/api/settings/validate")] == 1
    _assert_no_forbidden(entries)


@pytest.mark.asyncio
async def test_site_e_adversarial_controls_are_bounded_and_safe() -> None:
    assert HARNESS_TOKEN, "TEST_HARNESS_TOKEN must be set for fixture E2E"
    base_url = "http://localhost:8005"
    run_id = "fixture-site-e"
    await _reset(base_url)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(extra_http_headers={"X-Crawler-Test-Run": run_id})
        page = await context.new_page()
        await page.goto(f"{base_url}/gauntlet")
        await page.get_by_role("button", name="Load detail").first.click()
        await page.get_by_text("Runtime-only crawl detail").wait_for()
        await page.get_by_role("button", name="Review preferences").click()
        await page.get_by_role("button", name="Save preferences").click()
        await browser.close()

    entries = await _ledger(base_url, run_id)
    counts = _route_counts(entries)
    assert counts[("GET", "/api/gauntlet/details")] == 1
    _assert_no_forbidden(entries)


@pytest.mark.asyncio
async def test_site_f_spa_wizards_shadow_dom_and_modal_are_reachable_and_safe() -> None:
    assert HARNESS_TOKEN, "TEST_HARNESS_TOKEN must be set for fixture E2E"
    base_url = "http://localhost:8006"
    run_id = "fixture-site-f"
    await _reset(base_url)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(extra_http_headers={"X-Crawler-Test-Run": run_id})
        page = await context.new_page()
        await page.goto(base_url)
        await page.get_by_role("link", name="overview").click()
        await page.get_by_role("button", name="Load audit snapshot").click()
        await page.get_by_text("3 audit records are available.").wait_for()
        await page.get_by_role("link", name="projects").click()
        await page.get_by_role("button", name="Open project filters").click()
        await page.get_by_role("button", name="Search projects").click()
        await page.get_by_role("button", name="Inspect Aurora").click()
        await page.get_by_text("SLA 24h; region eu-west.").wait_for()
        await page.get_by_role("button", name="Close details").click()
        await page.get_by_role("link", name="reports 2026").click()
        await page.get_by_role("button", name="Build coverage report").click()
        await page.get_by_role("button", name="Validate report scope").click()
        await page.get_by_role("button", name="Preview draft report").click()
        await page.get_by_text("Draft draft-q2-coverage is in preview state.").wait_for()
        await browser.close()

    entries = await _ledger(base_url, run_id)
    counts = _route_counts(entries)
    assert counts[("GET", "/api/shadow/audit")] == 1
    assert counts[("POST", "/api/projects/search")] == 1
    assert counts[("GET", "/api/projects/details/project-aurora")] == 1
    assert counts[("POST", "/api/reports/validate")] == 1
    assert counts[("POST", "/api/reports/preview")] == 1
    _assert_no_forbidden(entries)
