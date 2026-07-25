from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import parser


def test_parse_katana_log_dedupes_and_builds_tree(tmp_path: Path):
    log_path = tmp_path / "job-1.jsonl"
    entries = [
        {
            "request": {"method": "GET", "url": "https://example.com/a"},
            "response": {"status": 200, "headers": {"content-type": "text/html"}},
            "timestamp": "2024-01-01T00:00:00Z",
        },
        {
            "request": {"method": "GET", "url": "https://example.com/a"},
            "response": {"status": 200, "headers": {"content-type": "text/html"}},
            "timestamp": "2024-01-01T00:00:01Z",
        },
        {
            "request": {"method": "POST", "url": "https://example.com/a/b"},
            "response": {"status": 201, "headers": {"content-type": "application/json"}},
            "timestamp": "2024-01-01T00:00:02Z",
        },
    ]

    with log_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")

    sitemap = parser.parse_katana_log(str(log_path))
    assert len(sitemap["entries"]) == 2
    assert "a" in sitemap["tree"]["children"]


def test_parse_katana_log_keeps_response_status_over_request_only_duplicate(
    tmp_path: Path,
):
    log_path = tmp_path / "job-1.jsonl"
    entries = [
        {
            "request": {
                "method": "GET",
                "endpoint": "https://example.com/app/overview",
            },
            "response": {
                "status_code": 200,
                "headers": {"Content-Type": "text/html; charset=utf-8"},
            },
            "timestamp": "2024-01-01T00:00:00Z",
        },
        {
            "request": {
                "method": "GET",
                "endpoint": "https://example.com/app/overview",
                "custom_fields": {"email": ["ops@example.test"]},
            },
            "timestamp": "2024-01-01T00:00:01Z",
        },
    ]

    with log_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")

    sitemap = parser.parse_katana_log(str(log_path), "https://example.com")

    assert sitemap["entries"] == [
        {
            "method": "GET",
            "url": "https://example.com/app/overview",
            "status": 200,
            "content_type": "text/html; charset=utf-8",
            "timestamp": "2024-01-01T00:00:00Z",
        }
    ]


def test_parse_katana_log_does_not_merge_adjacent_legacy_artifacts(tmp_path: Path):
    log_path = tmp_path / "job-1.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "request": {
                    "method": "GET",
                    "endpoint": "https://example.com/direct-katana",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "job-1.jsonl.katana").write_text(
        json.dumps(
            {
                "request": {
                    "method": "GET",
                    "endpoint": "https://example.com/legacy-sidecar",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    sitemap = parser.parse_katana_log(str(log_path), "https://example.com")

    assert [entry["url"] for entry in sitemap["entries"]] == ["https://example.com/direct-katana"]


def test_parse_katana_log_rejects_missing_artifact(tmp_path: Path):
    with pytest.raises(parser.CrawlArtifactsMissingError, match="artifact not found"):
        parser.parse_katana_log(
            str(tmp_path / "missing-job.jsonl"),
            "https://example.com",
        )


def test_parse_katana_log_rejects_corrupt_artifact(tmp_path: Path):
    log_path = tmp_path / "corrupt-job.jsonl"
    log_path.write_text("not-json\nstill-not-json\n", encoding="utf-8")

    with pytest.raises(parser.CrawlArtifactsCorruptError, match="no valid JSON"):
        parser.parse_katana_log(
            str(log_path),
            "https://example.com",
        )


def test_parse_katana_evidence_retains_lane_and_candidate_features(tmp_path: Path) -> None:
    log_path = tmp_path / "standard.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "request": {
                    "method": "GET",
                    "endpoint": "https://example.com/login",
                    "tag": "js",
                    "attribute": "regex",
                    "source": "https://example.com/app.js",
                },
                "response": {
                    "status_code": 200,
                    "knowledgebase": {"PageType": "login"},
                    "technologies": ["Example Framework"],
                    "forms": [{"method": "POST", "action": "/session"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert parser.parse_katana_evidence(
        str(log_path), lane="standard", target_url="https://example.com"
    ) == [
        {
            "lane": "standard",
            "method": "GET",
            "url": "https://example.com/login",
            "status": 200,
            "content_type": None,
            "timestamp": None,
            "tag": "js",
            "attribute": "regex",
            "source": "https://example.com/app.js",
            "features": {
                "knowledgebase": {"PageType": "login"},
                "technologies": ["Example Framework"],
                "forms": [{"method": "POST", "action": "/session"}],
            },
        }
    ]


def test_merge_baseline_uses_exact_method_url_and_join_safe_cdp_responses() -> None:
    standard = parser.build_sitemap(
        [
            {
                "method": "GET",
                "url": "https://example.com/shared",
                "status": None,
                "content_type": None,
                "timestamp": "standard-time",
            },
            {
                "method": "POST",
                "url": "https://example.com/shared",
                "status": 202,
                "content_type": "application/json",
                "timestamp": None,
            },
        ]
    )
    pure = parser.build_sitemap(
        [
            {
                "method": "GET",
                "url": "https://example.com/shared",
                "status": 200,
                "content_type": "text/html",
                "timestamp": "pure-time",
            }
        ]
    )
    browser = {
        "requests": [
            {
                "method": "GET",
                "url": "https://example.com/runtime-a",
                "session_id": "session-a",
                "request_id": "1",
                "observed_at": "request-a",
            },
            {
                "method": "GET",
                "url": "https://example.com/runtime-b",
                "session_id": "session-b",
                "request_id": "1",
                "observed_at": "request-b",
            },
            {
                "method": "GET",
                "url": "data:text/plain,ignored",
                "session_id": "session-a",
                "request_id": "2",
                "observed_at": "ignored",
            },
        ],
        "responses": [
            {
                "url": "https://example.com/runtime-a",
                "session_id": "session-a",
                "request_id": "1",
                "status": 201,
                "mime_type": "application/json",
            },
            {
                "url": "https://example.com/runtime-b",
                "session_id": "session-b",
                "request_id": "1",
                "status": 204,
                "mime_type": "text/plain",
            },
        ],
    }
    known_files = {
        "documents": [
            {
                "url": "https://example.com/sitemap.xml",
                "status": 200,
                "content_type": "application/xml",
                "observed_at": "known-time",
            },
            {
                "url": "https://evil.test/sitemap.xml",
                "status": 200,
                "content_type": "application/xml",
                "observed_at": "ignored",
            },
        ]
    }

    sitemap = parser.merge_baseline_sitemap(
        target_url="https://example.com",
        katana_sitemaps=[standard, pure],
        browser_evidence=browser,
        known_file_evidence=known_files,
    )

    assert sitemap["entries"] == [
        {
            "method": "GET",
            "url": "https://example.com/shared",
            "status": 200,
            "content_type": "text/html",
            "timestamp": "standard-time",
        },
        {
            "method": "POST",
            "url": "https://example.com/shared",
            "status": 202,
            "content_type": "application/json",
            "timestamp": None,
        },
        {
            "method": "GET",
            "url": "https://example.com/sitemap.xml",
            "status": 200,
            "content_type": "application/xml",
            "timestamp": "known-time",
        },
        {
            "method": "GET",
            "url": "https://example.com/runtime-a",
            "status": 201,
            "content_type": "application/json",
            "timestamp": "request-a",
        },
        {
            "method": "GET",
            "url": "https://example.com/runtime-b",
            "status": 204,
            "content_type": "text/plain",
            "timestamp": "request-b",
        },
    ]
