from __future__ import annotations

from scripts import run_testsite_comparison
from scripts.run_auth_agent_tests import AuthAgentSiteCase


def test_canonical_cases_select_one_case_per_fixture() -> None:
    cases = run_testsite_comparison._canonical_cases(gateway=False)
    sites = [run_testsite_comparison._site_name_for_case(case) for case in cases]

    assert len(cases) == 20
    assert len(set(sites)) == 20
    assert "site-a-static-llm-no-auth" not in [case.name for case in cases]
    assert "auth-a-simple-form-dynamic-exclusion" in [case.name for case in cases]


def test_no_auth_agent_variant_drops_only_llm_auth_config() -> None:
    llm_case = AuthAgentSiteCase(
        "auth-a-simple-form",
        "http://localhost:8101",
        {"login_url": "http://localhost:8101/login", "credentials": {"email": "x"}},
        "/app/overview",
        "llm",
    )
    manual_case = AuthAgentSiteCase(
        "auth-b-http-basic",
        "http://localhost:8102",
        {"headers": ["Authorization: Basic dXNlcjpwYXNz"]},
        "/app/overview",
        "manual_headers",
    )
    variant = run_testsite_comparison.VARIANTS[2]

    assert run_testsite_comparison._case_for_variant(llm_case, variant).auth_config is None
    assert (
        run_testsite_comparison._case_for_variant(manual_case, variant).auth_config
        == manual_case.auth_config
    )


def test_classify_result_marks_unsafe_when_blocked_url_is_crawled() -> None:
    case = AuthAgentSiteCase(
        "auth-a-simple-form-dynamic-exclusion",
        "http://localhost:8101",
        None,
        "/app/overview",
        "llm",
        ("/app/danger/close-account",),
    )
    payload = {
        "status": "completed",
        "sitemap": {
            "entries": [
                {"url": "http://localhost:8101/app/overview", "status": 200},
                {"url": "http://localhost:8101/app/danger/close-account", "status": 200},
            ]
        },
    }

    result = run_testsite_comparison._classify_result(
        case,
        run_testsite_comparison.VARIANTS[1],
        payload,
        run_index=1,
        job_id="job-1",
        elapsed_seconds=1.0,
    )

    assert not result.passed
    assert result.access_ok
    assert not result.safe_ok
    assert result.error == "blocked URL was crawled"


def test_summary_counts_no_access_completed_jobs() -> None:
    results = [
        run_testsite_comparison.ComparisonResult(
            run=1,
            site="site-a-static",
            case_name="site-a-static",
            original_mode="public",
            variant="full",
            passed=True,
            access_ok=True,
            safe_ok=True,
            status="completed",
        ),
        run_testsite_comparison.ComparisonResult(
            run=1,
            site="auth-a-simple-form",
            case_name="auth-a-simple-form",
            original_mode="llm",
            variant="full",
            passed=False,
            access_ok=False,
            safe_ok=True,
            status="completed",
        ),
    ]
    for variant in run_testsite_comparison.VARIANTS[1:]:
        results.append(
            run_testsite_comparison.ComparisonResult(
                run=1,
                site="site-a-static",
                case_name="site-a-static",
                original_mode="public",
                variant=variant.key,
                passed=False,
                access_ok=False,
                safe_ok=False,
                status="failed",
            )
        )

    summary = run_testsite_comparison._summary(results)

    assert summary["full"] == {
        "jobs": 2,
        "passed": 1,
        "access_ok": 1,
        "safe_ok": 2,
        "unsafe": 0,
        "no_access": 1,
        "failed_jobs": 0,
    }


def test_matrix_cell_marks_flaky_repeated_outcomes() -> None:
    results = [
        run_testsite_comparison.ComparisonResult(
            run=1,
            site="site-a-static",
            case_name="site-a-static",
            original_mode="public",
            variant="full",
            passed=True,
            access_ok=True,
            safe_ok=True,
            status="completed",
        ),
        run_testsite_comparison.ComparisonResult(
            run=2,
            site="site-a-static",
            case_name="site-a-static",
            original_mode="public",
            variant="full",
            passed=False,
            access_ok=False,
            safe_ok=True,
            status="completed",
        ),
    ]

    assert run_testsite_comparison._matrix_cell(results) == "FLAKY: PASS 1, NO ACCESS 1"


def test_reliability_summary_counts_stable_sites_across_runs() -> None:
    cases = [
        AuthAgentSiteCase("site-a-static", "http://localhost:8001", None, "/", "public"),
        AuthAgentSiteCase(
            "auth-a-simple-form",
            "http://localhost:8101",
            None,
            "/app/overview",
            "llm",
        ),
    ]
    results = [
        run_testsite_comparison.ComparisonResult(
            run=1,
            site="site-a-static",
            case_name="site-a-static",
            original_mode="public",
            variant="full",
            passed=True,
            access_ok=True,
            safe_ok=True,
            status="completed",
        ),
        run_testsite_comparison.ComparisonResult(
            run=2,
            site="site-a-static",
            case_name="site-a-static",
            original_mode="public",
            variant="full",
            passed=True,
            access_ok=True,
            safe_ok=True,
            status="completed",
        ),
        run_testsite_comparison.ComparisonResult(
            run=1,
            site="auth-a-simple-form",
            case_name="auth-a-simple-form",
            original_mode="llm",
            variant="full",
            passed=True,
            access_ok=True,
            safe_ok=True,
            status="completed",
        ),
        run_testsite_comparison.ComparisonResult(
            run=2,
            site="auth-a-simple-form",
            case_name="auth-a-simple-form",
            original_mode="llm",
            variant="full",
            passed=False,
            access_ok=False,
            safe_ok=True,
            status="completed",
        ),
    ]

    summary = run_testsite_comparison._reliability_summary(
        cases,
        results,
        [run_testsite_comparison.VARIANTS[0]],
        expected_runs=2,
    )

    assert summary["full"]["stable_sites"] == 1
    assert summary["full"]["reliability_percent"] == 50.0
    assert summary["full"]["unstable_sites"] == ["auth-a-simple-form"]
