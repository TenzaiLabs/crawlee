from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app import cli, main


@pytest.mark.asyncio
async def test_api_docs_are_tenzai_branded():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        schema_response = await client.get("/openapi.json")
        docs_response = await client.get("/docs")
        redoc_response = await client.get("/redoc")

    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert schema["info"]["title"] == "Tenzai Crawler"
    assert schema["info"]["x-logo"] == {"url": "/static/tenzai-logo.svg", "altText": "Tenzai"}

    assert docs_response.status_code == 200
    assert "Tenzai Crawler API docs" in docs_response.text
    assert "/static/tenzai-favicon-48.png" in docs_response.text
    assert "/static/tenzai-logo.svg" in docs_response.text

    assert redoc_response.status_code == 200
    assert "Tenzai Crawler ReDoc" in redoc_response.text
    assert "/static/tenzai-favicon-48.png" in redoc_response.text
    assert "/static/tenzai-logo.svg" in redoc_response.text

    assert (Path(main.STATIC_DIR) / "tenzai-favicon-48.png").is_file()
    assert (Path(main.STATIC_DIR) / "tenzai-logo.svg").is_file()


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


@pytest.mark.asyncio
async def test_get_job_returns_generated_exclusions(app, monkeypatch: pytest.MonkeyPatch):
    exclusions = {
        "auth_blocked_url_count": 1,
        "auth_applied_blocked_url_count": 1,
        "auth_ignored_blocked_url_count": 0,
        "auth_dynamic_patterns": ["/logout(?:$|[/?#])"],
        "auth_discovered_url_count": 1,
        "auth_discovered_urls": ["https://example.com/app/dashboard"],
        "extra_seed_urls": ["https://example.com/app/dashboard"],
        "effective_patterns": ["logout", "/logout(?:$|[/?#])"],
    }
    await main.db.execute(
        """
        INSERT INTO jobs (
            job_id,
            status,
            target_url,
            scope_config,
            auth_config,
            error,
            created_at,
            finished_at,
            generated_exclusions
        )
        VALUES (?, 'completed', ?, NULL, NULL, NULL, datetime('now'), ?, ?)
        """,
        (
            "job-exclusions-1",
            "https://example.com",
            "2026-02-23T12:00:00+00:00",
            main.db.dumps_json(exclusions),
        ),
    )

    monkeypatch.setattr(
        main.parser,
        "parse_log",
        lambda job_id, target_url: {"entries": [], "tree": {"children": {}, "pages": []}},
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/jobs/job-exclusions-1")

    assert response.status_code == 200
    assert response.json()["generated_exclusions"] == exclusions
