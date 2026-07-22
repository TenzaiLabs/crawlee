from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import db


@pytest.mark.asyncio
async def test_init_db_migrates_legacy_jobs_schema_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                target_url TEXT NOT NULL,
                scope_config TEXT,
                auth_config TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )

    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    monkeypatch.setattr(db, "LOG_DIR", str(tmp_path / "logs"))

    await db.init_db()
    await db.init_db()

    async with db.connect() as conn:
        cursor = await conn.execute("PRAGMA table_info(jobs)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()

    assert {
        "generated_exclusions",
        "sitemap",
        "result_entry_count",
        "result_size_bytes",
    } <= columns
