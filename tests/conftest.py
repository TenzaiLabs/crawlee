from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import pytest_asyncio


@pytest_asyncio.fixture()
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("CRAWLER_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("CRAWLER_LOG_DIR", str(tmp_path / "logs"))

    from app import db, main

    importlib.reload(db)
    importlib.reload(main)
    monkeypatch.setattr(main.orchestrator, "enqueue_job", lambda job_id: None)
    monkeypatch.setattr(main.orchestrator, "start_drainer", lambda: None)
    monkeypatch.setattr(main.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    async with main.lifespan(main.app):
        await main.db.execute("DELETE FROM jobs")
        yield main.app


@pytest_asyncio.fixture()
async def app_with_orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("CRAWLER_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("CRAWLER_LOG_DIR", str(tmp_path / "logs"))

    from app import db, main

    importlib.reload(db)
    importlib.reload(main)
    monkeypatch.setattr(main.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    async with main.lifespan(main.app):
        await main.db.execute("DELETE FROM jobs")
        yield main.app
