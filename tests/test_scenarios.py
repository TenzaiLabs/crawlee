from __future__ import annotations

import asyncio
import os
import shutil

import httpx
import pytest


@pytest.mark.asyncio
async def test_happy_path_crawl_succeeds(app_with_orchestrator):
    if os.getenv("RUN_E2E") != "1":
        pytest.skip("Set RUN_E2E=1 to run end-to-end crawl scenario")
    if shutil.which("katana") is None or shutil.which("proxify") is None:
        pytest.skip("End-to-end crawl requires katana and proxify binaries")
    transport = httpx.ASGITransport(app=app_with_orchestrator)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/jobs", json={"target_url": "https://example.com"})
        response.raise_for_status()
        job_id = response.json()["job_id"]

        payload = {}
        for _ in range(120):
            status_response = await client.get(f"/jobs/{job_id}")
            status_response.raise_for_status()
            payload = status_response.json()
            if payload["status"] in {"completed", "failed", "failed_interrupted", "cancelled"}:
                break
            await asyncio.sleep(0.5)

        assert payload["status"] == "completed"
