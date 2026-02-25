from __future__ import annotations

import httpx
import pytest

from app import cli, main


@pytest.mark.asyncio
async def test_create_get_cancel_flow(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_payload = await cli.create_job(client, "https://example.com")
        job_id = create_payload["job_id"]

        status_payload = await cli.get_job(client, job_id)
        assert status_payload["status"] == "queued"
        assert status_payload["target_url"].rstrip("/") == "https://example.com"

        cancel_payload = await cli.cancel_job(client, job_id)
        assert cancel_payload["status"] == "cancelled"

        final_payload = await cli.get_job(client, job_id)
        assert final_payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_multiple_jobs_queued(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp1 = await client.post("/jobs", json={"target_url": "https://example.com"})
        assert resp1.status_code == 201

        resp2 = await client.post("/jobs", json={"target_url": "https://example.org"})
        assert resp2.status_code == 201

        assert resp1.json()["job_id"] != resp2.json()["job_id"]


@pytest.mark.asyncio
async def test_list_jobs(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await cli.list_jobs(client)
        assert listing["jobs"] == []

        await cli.create_job(client, "https://example.com")
        await cli.create_job(client, "https://example.org")

        listing = await cli.list_jobs(client)
        assert len(listing["jobs"]) == 2
        assert listing["jobs"][0]["status"] == "queued"
        assert listing["jobs"][1]["status"] == "queued"


@pytest.mark.asyncio
async def test_list_jobs_excludes_terminal(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_payload = await cli.create_job(client, "https://example.com")
        job_id = create_payload["job_id"]
        await cli.cancel_job(client, job_id)

        listing = await cli.list_jobs(client)
        assert len(listing["jobs"]) == 0


@pytest.mark.asyncio
async def test_scope_config_requires_headless(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "target_url": "https://example.com",
                "scope_config": {
                    "headless": False,
                    "cdp_url": "ws://127.0.0.1:9222/devtools/browser/abc",
                },
            },
        )
        assert response.status_code == 422
        assert "headless" in response.text


@pytest.mark.asyncio
async def test_scope_config_cdp_url_mutually_exclusive(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "target_url": "https://example.com",
                "scope_config": {
                    "headless": True,
                    "cdp_url": "ws://127.0.0.1:9222/devtools/browser/abc",
                    "system_chrome": True,
                },
            },
        )
        assert response.status_code == 422
        assert "mutually" in response.text


@pytest.mark.asyncio
async def test_scope_config_rejects_unknown_keys(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "target_url": "https://example.com",
                "scope_config": {"unknown_option": True},
            },
        )
        assert response.status_code == 422
        assert "Unknown scope_config keys" in response.text


@pytest.mark.asyncio
async def test_auth_config_rejects_disallowed_api_key_field(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "target_url": "https://example.com",
                "auth_config": {
                    "api_key": "plaintext-secret",
                    "login_url": "https://example.com/login",
                },
            },
        )
        assert response.status_code == 422
        assert "auth_config.api_key" in response.text


@pytest.mark.asyncio
async def test_get_job_uses_completed_sitemap_cache(app, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "CRAWLER_COMPLETED_SITEMAP_CACHE_ENABLED", True)
    monkeypatch.setattr(main, "CRAWLER_COMPLETED_SITEMAP_CACHE_MAX_ENTRIES", 16)
    async with main._completed_sitemap_cache_lock:
        main._completed_sitemap_cache.clear()

    await main.db.execute(
        """
        INSERT INTO jobs (
            job_id, status, target_url, scope_config, auth_config, error, created_at, finished_at
        )
        VALUES (?, 'completed', ?, NULL, NULL, NULL, datetime('now'), ?)
        """,
        ("job-cache-1", "https://example.com", "2026-02-23T12:00:00+00:00"),
    )

    calls = 0

    def _fake_parse_log(job_id: str, target_url: str):
        nonlocal calls
        calls += 1
        assert job_id == "job-cache-1"
        assert target_url == "https://example.com"
        return {"entries": [{"url": "https://example.com"}], "tree": {"children": {}, "pages": []}}

    monkeypatch.setattr(main.parser, "parse_log", _fake_parse_log)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/jobs/job-cache-1")
        second = await client.get("/jobs/job-cache-1")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["sitemap"]["entries"][0]["url"] == "https://example.com"
    assert second.json()["sitemap"]["entries"][0]["url"] == "https://example.com"
    assert calls == 1
