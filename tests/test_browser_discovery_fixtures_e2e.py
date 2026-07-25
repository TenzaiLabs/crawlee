from __future__ import annotations

import asyncio
import os
from collections import Counter
from dataclasses import dataclass

import httpx
import pytest
from playwright.async_api import async_playwright

from app import browser_discovery
from app.browser_session import BrowserSession

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1",
    reason="browser fixture E2E requires RUN_E2E=1 and the testsite stack",
)

HARNESS_TOKEN = os.environ.get("TEST_HARNESS_TOKEN", "")


@dataclass(frozen=True)
class _DiscoveryCase:
    case_id: str
    base_url: str
    candidate_paths: list[str]
    actions: list[browser_discovery.ScriptedAction]
    required: set[tuple[str, str]]
    login: bool = False


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


async def _run_production_discovery_round(
    *,
    case_id: str,
    base_url: str,
    candidate_paths: list[str],
    scripted_actions: list[browser_discovery.ScriptedAction],
    login: bool = False,
) -> browser_discovery.DiscoveryRoundResult:
    session = BrowserSession(f"scripted-discovery-{case_id}")
    await session.start()
    try:
        context = await session.connect_playwright(
            headers=[f"X-Crawler-Test-Run: scripted-discovery-{case_id}"],
            target_url=base_url,
            epoch="scripted-discovery-auth",
        )
        if login:
            page = await context.new_page()
            await page.goto(f"{base_url}/login")
            await page.get_by_label("Username").fill("demo")
            await page.get_by_label("Password").fill("password")
            await page.get_by_role("button", name="Login").click()
            await page.close()
        assert session.observer is not None
        session.observer.set_epoch("scripted-browser-discovery")
        return await session.guard(
            browser_discovery.run_discovery_round(
                context=context,
                target_url=base_url,
                candidates=[
                    browser_discovery.DiscoveryCandidate(
                        url=f"{base_url}{path}",
                        score=100,
                        reason="fixture",
                    )
                    for path in candidate_paths
                ],
                observer=session.observer,
                adapter=browser_discovery.ScriptedDiscoveryAdapter(scripted_actions),
                cancel_event=asyncio.Event(),
                max_actions=40,
                max_pages=10,
                max_states=40,
                processed_states=set(),
            )
        )
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_production_discovery_interface_reaches_all_browser_only_fixture_requests() -> None:
    assert HARNESS_TOKEN, "TEST_HARNESS_TOKEN must be set for fixture E2E"
    cases = [
        _DiscoveryCase(
            case_id="site-b",
            base_url="http://localhost:8002",
            candidate_paths=["/dashboard"],
            login=True,
            actions=[
                browser_discovery.ScriptedAction("Open workflow center"),
                browser_discovery.ScriptedAction("Show workflow tools"),
                browser_discovery.ScriptedAction("Onboarding"),
                browser_discovery.ScriptedAction("Validate onboarding"),
                browser_discovery.ScriptedAction("Preview onboarding draft"),
                browser_discovery.ScriptedAction("Settings validation"),
                browser_discovery.ScriptedAction("Validate settings"),
            ],
            required={
                ("POST", "/api/onboarding/validate"),
                ("POST", "/api/onboarding/preview"),
                ("POST", "/api/settings/validate"),
            },
        ),
        _DiscoveryCase(
            case_id="site-e",
            base_url="http://localhost:8005",
            candidate_paths=["/gauntlet"],
            actions=[
                browser_discovery.ScriptedAction(
                    "guided_mode",
                    kind=browser_discovery.DiscoveryActionKind.select,
                    value="bounded",
                ),
                browser_discovery.ScriptedAction("Load guided detail"),
                browser_discovery.ScriptedAction("Review preferences"),
                browser_discovery.ScriptedAction("Save preferences"),
            ],
            required={("GET", "/api/gauntlet/guided-details")},
        ),
        _DiscoveryCase(
            case_id="site-f",
            base_url="http://localhost:8006",
            candidate_paths=["/app/overview", "/app/projects", "/app/reports/2026"],
            actions=[
                browser_discovery.ScriptedAction("Load audit snapshot"),
                browser_discovery.ScriptedAction("Open project filters"),
                browser_discovery.ScriptedAction("Search projects"),
                browser_discovery.ScriptedAction("Inspect Aurora"),
                browser_discovery.ScriptedAction("Build coverage report"),
                browser_discovery.ScriptedAction("Validate report scope"),
                browser_discovery.ScriptedAction("Preview draft report"),
            ],
            required={
                ("GET", "/api/shadow/audit"),
                ("POST", "/api/projects/search"),
                ("GET", "/api/projects/details/project-aurora"),
                ("POST", "/api/reports/validate"),
                ("POST", "/api/reports/preview"),
            },
        ),
    ]

    for case in cases:
        await _reset(case.base_url)
        result = await _run_production_discovery_round(
            case_id=case.case_id,
            base_url=case.base_url,
            candidate_paths=case.candidate_paths,
            scripted_actions=case.actions,
            login=case.login,
        )
        entries = await _ledger(case.base_url, f"scripted-discovery-{case.case_id}")
        observed = set(_route_counts(entries))
        assert case.required <= observed, {
            "missing": sorted(case.required - observed),
            "actions": result.actions,
            "diagnostics": result.diagnostics,
            "ledger": entries,
        }
        assert result.state_count > 0
        _assert_no_forbidden(entries)


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
        await page.get_by_label("Detail mode").select_option("bounded")
        await page.get_by_role("button", name="Load guided detail").click()
        await (
            page.locator("#guided-detail-result")
            .get_by_text(
                "Guided runtime detail",
                exact=True,
            )
            .wait_for()
        )
        await page.get_by_role("button", name="Review preferences").click()
        await page.get_by_role("button", name="Save preferences").click()
        await browser.close()

    entries = await _ledger(base_url, run_id)
    counts = _route_counts(entries)
    assert counts[("GET", "/api/gauntlet/guided-details")] == 1
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


@pytest.mark.asyncio
async def test_site_g_discovery_lanes_are_browser_reachable_and_bounded() -> None:
    assert HARNESS_TOKEN, "TEST_HARNESS_TOKEN must be set for fixture E2E"
    base_url = "http://localhost:8007"
    run_id = "fixture-site-g"
    await _reset(base_url)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            extra_http_headers={
                "X-Crawler-Test-Run": run_id,
                "X-Discovery-Token": HARNESS_TOKEN,
            }
        )
        page = await context.new_page()

        await page.goto(base_url)
        await page.get_by_role("button", name="Load runtime XHR marker").click()
        await page.get_by_text("runtime-xhr").wait_for()

        async with context.expect_page() as popup_info:
            await page.get_by_role("button", name="Open observer popup").click()
        popup = await popup_info.value
        await popup.get_by_text("observer-popup").wait_for()
        await popup.close()

        await page.get_by_role("button", name="Load observer frame").click()
        await (
            page.frame_locator('iframe[title="Observer frame"]')
            .get_by_text("observer-frame")
            .wait_for()
        )

        await page.get_by_role("button", name="Start observer worker").click()
        await page.get_by_text("observer-worker").wait_for()

        await page.get_by_role("button", name="Start observer service worker").click()
        await page.get_by_text("observer-service-worker").wait_for()

        await page.get_by_role("button", name="Open rendered-only page").click()
        await page.get_by_text("Browser action marker reached.").wait_for()
        await page.close()

        page = await context.new_page()
        await page.goto(f"{base_url}/handoff")
        await page.get_by_text("cookie:present").wait_for()
        await page.get_by_text("localStorage:ready").wait_for()

        response = await page.goto(f"{base_url}/header-only")
        assert response is not None
        assert response.status == 200

        response = await page.goto("http://child.localhost:8007/subdomain-header-only")
        assert response is not None
        assert response.status == 200
        await page.get_by_text("Scoped subdomain header accepted.").wait_for()

        await page.goto(f"{base_url}/seed/one")
        await page.get_by_role("link", name="Seed one child").click()
        await page.get_by_text("Serial seed marker one.").wait_for()
        await page.goto(f"{base_url}/seed/two")
        await page.get_by_role("link", name="Seed two child").click()
        await page.get_by_text("Serial seed marker two.").wait_for()

        async with page.expect_response(
            lambda candidate: candidate.url.endswith("/api/perpetual/poll")
        ):
            await page.goto(f"{base_url}/perpetual")
        await page.close()
        await browser.close()

    entries = await _ledger(base_url, run_id)
    counts = _route_counts(entries)
    assert counts[("GET", "/api/runtime/xhr")] == 1
    assert counts[("GET", "/api/observer/popup")] == 1
    assert counts[("GET", "/api/observer/frame")] == 1
    assert counts[("GET", "/api/observer/worker")] == 1
    assert counts[("GET", "/api/observer/service-worker")] == 1
    assert counts[("GET", "/rendered/only")] == 1
    assert counts[("GET", "/handoff")] == 1
    assert counts[("GET", "/header-only")] == 1
    assert counts[("GET", "/subdomain-header-only")] == 1
    assert counts[("GET", "/seed/one/child")] == 1
    assert counts[("GET", "/seed/two/child")] == 1
    assert counts[("GET", "/api/perpetual/poll")] >= 1
    destructive = [entry for entry in entries if entry["classification"] == "destructive-marker"]
    assert destructive == []
