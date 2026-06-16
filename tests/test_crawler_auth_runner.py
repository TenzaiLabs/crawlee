from __future__ import annotations

from types import SimpleNamespace

from scripts import run_crawler_auth_tests


def test_entry_matches_probe_requires_same_origin_and_2xx() -> None:
    entry = {"url": "http://localhost:8101/app/overview", "status": 200}

    assert run_crawler_auth_tests._entry_matches_probe(
        entry,
        "http://localhost:8101",
        "/app/overview",
    )
    assert not run_crawler_auth_tests._entry_matches_probe(
        {"url": "http://localhost:8102/app/overview", "status": 200},
        "http://localhost:8101",
        "/app/overview",
    )
    assert not run_crawler_auth_tests._entry_matches_probe(
        {"url": "http://localhost:8101/app/overview", "status": 302},
        "http://localhost:8101",
        "/app/overview",
    )


def test_validate_sitemap_requires_protected_probe_for_auth_cases() -> None:
    case = SimpleNamespace(
        mode="llm",
        target_url="http://localhost:8101",
        probe_path="/app/overview",
    )

    passed, urls, error = run_crawler_auth_tests._validate_sitemap(
        case,
        {
            "entries": [
                {"url": "http://localhost:8101/login", "status": 200},
                {"url": "http://localhost:8101/app/overview", "status": 200},
            ]
        },
    )

    assert passed
    assert urls == ["http://localhost:8101/app/overview"]
    assert error is None


def test_validate_sitemap_allows_successful_same_origin_for_no_auth_sanity() -> None:
    case = SimpleNamespace(
        mode="llm_no_auth",
        target_url="http://localhost:8001",
        probe_path="/",
    )

    passed, urls, error = run_crawler_auth_tests._validate_sitemap(
        case,
        {"entries": [{"url": "http://localhost:8001/workspace.html", "status": 200}]},
    )

    assert passed
    assert urls == ["http://localhost:8001/workspace.html"]
    assert error is None
