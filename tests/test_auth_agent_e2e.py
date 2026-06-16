from __future__ import annotations

import os

import pytest

from scripts.run_auth_agent_tests import build_cases, run_cases


@pytest.mark.asyncio
async def test_auth_agent_against_local_testsites() -> None:
    if os.getenv("RUN_AUTH_AGENT_E2E") != "1":
        pytest.skip("Set RUN_AUTH_AGENT_E2E=1 to run auth-agent testsite checks")

    modes = set(filter(None, os.getenv("RUN_AUTH_AGENT_E2E_MODES", "").split(",")))
    names = set(filter(None, os.getenv("RUN_AUTH_AGENT_E2E_CASES", "").split(",")))
    cases = build_cases(gateway=os.getenv("RUN_AUTHSITES_GATEWAY") == "1")
    if modes:
        cases = [case for case in cases if case.mode in modes]
    if names:
        cases = [case for case in cases if case.name in names]

    results = await run_cases(cases, timeout=30.0)
    failures = [result for result in results if not result.passed]

    assert not failures, [failure.__dict__ for failure in failures]
