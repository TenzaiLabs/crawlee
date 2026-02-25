from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def test_parse_log_dedupes_and_builds_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    monkeypatch.setenv("CRAWLER_LOG_DIR", str(log_dir))

    from app import db, parser

    importlib.reload(db)
    importlib.reload(parser)

    log_path = log_dir / "job-1.jsonl"
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

    sitemap = parser.parse_log("job-1")
    assert len(sitemap["entries"]) == 2
    assert "a" in sitemap["tree"]["children"]
