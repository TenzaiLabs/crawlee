from __future__ import annotations

import argparse

from scripts import run_external_site_comparison


def test_normalize_url_sorts_query_and_removes_fragment() -> None:
    assert (
        run_external_site_comparison._normalize_url("HTTPS://Example.COM/path/?b=2&a=1#section")
        == "https://example.com/path?a=1&b=2"
    )


def test_sitemap_urls_keeps_successful_unique_urls_only() -> None:
    payload = {
        "sitemap": {
            "entries": [
                {"url": "https://example.com/a#frag", "status": 200},
                {"url": "https://example.com/a", "status": 200},
                {"url": "https://example.com/b", "status": 302},
                {"url": "https://example.com/c", "status": 404},
                {"url": None, "status": 200},
            ]
        }
    }

    assert run_external_site_comparison._sitemap_urls(payload) == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_comparison_rows_detect_exact_sitemap_match() -> None:
    results = [
        run_external_site_comparison.ExternalResult(
            site="public",
            variant="no_auth",
            target_url="https://example.com",
            status="completed",
            passed=True,
            elapsed_seconds=1.0,
            entry_count=2,
            sitemap_hash="",
            urls=["https://example.com/a", "https://example.com/b"],
        ),
        run_external_site_comparison.ExternalResult(
            site="public",
            variant="auth_agent",
            target_url="https://example.com",
            status="completed",
            passed=True,
            elapsed_seconds=1.0,
            entry_count=2,
            sitemap_hash="",
            urls=["https://example.com/b", "https://example.com/a"],
        ),
    ]

    assert run_external_site_comparison._comparison_rows(results) == [
        {
            "site": "public",
            "no_auth_count": 2,
            "auth_agent_count": 2,
            "identical": True,
            "only_no_auth": [],
            "only_auth_agent": [],
        }
    ]


def test_cmt_auth_config_uses_env_template_for_password() -> None:
    config = run_external_site_comparison._cmt_auth_config()

    assert config["credentials"]["email"] == "acepace@gmail.com"
    assert config["credentials"]["password"] == "{{env:CMT_PASSWORD}}"
    assert config["probe_url"] == "/Conference/Recent"


def test_prepare_artifact_dir_preserves_logs_and_records_paths(tmp_path) -> None:
    artifact_dir = tmp_path / "external-artifacts"
    args = argparse.Namespace(artifact_dir=str(artifact_dir))

    result = run_external_site_comparison._prepare_artifact_dir(args)

    assert result == artifact_dir
    assert artifact_dir.is_dir()
    assert args.artifact_dir == str(artifact_dir)
    assert args.db_path == str(artifact_dir / "jobs.db")
    assert args.log_dir == str(artifact_dir / "logs")


def test_job_log_paths_are_derived_from_job_id(tmp_path) -> None:
    paths = run_external_site_comparison._job_log_paths(tmp_path, "job-123")

    assert paths == {
        "log_path": str(tmp_path / "job-123.jsonl"),
        "katana_log_path": str(tmp_path / "job-123.jsonl.katana"),
        "stderr_log_path": str(tmp_path / "job-123.jsonl.stderr"),
    }


def test_write_report_includes_generated_exclusions(tmp_path) -> None:
    result = run_external_site_comparison.ExternalResult(
        site="cmt",
        variant="auth_agent",
        target_url="https://cmtint.research.microsoft.com/",
        status="completed",
        passed=True,
        elapsed_seconds=1.0,
        entry_count=1,
        sitemap_hash="hash",
        urls=["https://cmtint.research.microsoft.com/Conference/Recent"],
        generated_exclusions={
            "auth_blocked_url_count": 0,
            "auth_discovered_url_count": 1,
            "auth_dynamic_patterns": [],
            "auth_discovered_urls": ["https://example.com/projects/alpha"],
            "extra_seed_urls": ["https://example.com/projects/alpha"],
            "effective_patterns": ["logout", "delete"],
        },
    )

    report = tmp_path / "report.md"
    args = argparse.Namespace(
        crawl_duration="60s",
        max_depth=3,
        max_pages=100,
        headless=True,
        artifact_dir="/tmp/crawler-external-comparison-test",
        db_path="/tmp/crawler-external-comparison-test/jobs.db",
        log_dir="/tmp/crawler-external-comparison-test/logs",
        cdp_url=None,
        manual_cookie_from_cdp=False,
        manual_cookie_env="CRAWLER_MANUAL_COOKIE",
        manual_auth_header=[],
        no_incognito=False,
    )
    run_external_site_comparison.write_report(report, [result], args)

    text = report.read_text(encoding="utf-8")
    assert "Artifact directory: `/tmp/crawler-external-comparison-test`" in text
    assert "Job DB: `/tmp/crawler-external-comparison-test/jobs.db`" in text
    assert "Log directory: `/tmp/crawler-external-comparison-test/logs`" in text
    assert "generated exclusions" in text
    assert "auth blocked URL count: `0`" in text
    assert "auth discovered URL count: `1`" in text
    assert "https://example.com/projects/alpha" in text
    assert "- `logout`" in text
    assert "- `delete`" in text
