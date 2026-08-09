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
        assert cancel_payload["cancellation_status"] == "completed"

        final_payload = await cli.get_job(client, job_id)
        assert final_payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_job_exposes_persisted_checkpoint_sitemap(app):
    sitemap = {
        "entries": [{"method": "GET", "url": "https://example.com/revealed"}],
        "tree": {"children": {}, "pages": []},
        "discovery": {
            "outcome": "interrupted",
            "rounds": 1,
            "new_entry_count": 1,
            "state_count": 2,
            "workflow_count": 1,
            "stop_reason": "cancelled_after_checkpoint",
        },
    }
    serialized, entry_count, size_bytes = main.orchestrator.serialize_sitemap(sitemap)
    await main.db.execute(
        """
        INSERT INTO jobs (
            job_id, status, target_url, discovery_config, created_at, finished_at,
            sitemap, discovery_result, result_entry_count, result_size_bytes, crawl_evidence
        )
        VALUES (?, 'cancelled', ?, ?, datetime('now'), datetime('now'), ?, ?, ?, ?, ?)
        """,
        (
            "cancelled-with-checkpoint",
            "https://example.com",
            main.DiscoveryConfig().model_dump_json(),
            serialized,
            main.db.dumps_json(sitemap["discovery"]),
            entry_count,
            size_bytes,
            main.db.dumps_json(
                {
                    "completeness": "partial",
                    "warnings": ["Katana pure-headless baseline had not completed"],
                }
            ),
        ),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/jobs/cancelled-with-checkpoint")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "cancelled"
    assert payload["sitemap"] == sitemap
    assert payload["result_metadata"] == {
        "entry_count": entry_count,
        "size_bytes": size_bytes,
        "completeness": "partial",
        "warnings": ["Katana pure-headless baseline had not completed"],
    }


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
async def test_discovery_config_defaults_is_persisted_and_returned(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/jobs", json={"target_url": "https://example.com"})
        response = await client.get(f"/jobs/{created.json()['job_id']}")

    assert created.status_code == 201
    assert response.status_code == 200
    assert response.json()["discovery"] == {
        "enabled": True,
        "max_rounds": 3,
        "max_actions": 100,
        "max_llm_pages": 25,
    }


@pytest.mark.asyncio
async def test_submitted_plaintext_auth_config_is_persisted_and_returned(app):
    auth_config = {
        "login_url": "https://example.com/login",
        "credentials": {"email": "operator@example.com", "password": "plain-secret"},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={"target_url": "https://example.com", "auth_config": auth_config},
        )
        response = await client.get(f"/jobs/{created.json()['job_id']}")

    assert created.status_code == 201
    assert response.status_code == 200
    assert response.json()["auth_config"] == auth_config


@pytest.mark.asyncio
async def test_discovery_config_accepts_lower_limits_and_rejects_server_cap_excess(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/jobs",
            json={
                "target_url": "https://example.com",
                "discovery": {
                    "enabled": False,
                    "max_rounds": 1,
                    "max_actions": 5,
                    "max_llm_pages": 2,
                },
            },
        )
        accepted_job = await client.get(f"/jobs/{accepted.json()['job_id']}")
        too_many_rounds = await client.post(
            "/jobs",
            json={
                "target_url": "https://example.com",
                "discovery": {"max_rounds": 4},
            },
        )
        unknown_field = await client.post(
            "/jobs",
            json={
                "target_url": "https://example.com",
                "discovery": {"model": "operator-selected"},
            },
        )

    assert accepted.status_code == 201
    assert accepted_job.json()["discovery"] == {
        "enabled": False,
        "max_rounds": 1,
        "max_actions": 5,
        "max_llm_pages": 2,
    }
    assert too_many_rounds.status_code == 422
    assert unknown_field.status_code == 422


@pytest.mark.asyncio
async def test_list_jobs(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await cli.list_jobs(client)
        assert listing["jobs"] == []
        assert listing["total"] == 0
        assert listing["limit"] == 50
        assert listing["offset"] == 0

        await cli.create_job(client, "https://example.com")
        await cli.create_job(client, "https://example.org")

        listing = await cli.list_jobs(client)
        assert len(listing["jobs"]) == 2
        assert listing["total"] == 2
        assert listing["jobs"][0]["target_url"].rstrip("/") == "https://example.org"
        assert listing["jobs"][1]["target_url"].rstrip("/") == "https://example.com"
        assert listing["jobs"][0]["queue_position"] == 2
        assert listing["jobs"][1]["queue_position"] == 1
        assert listing["jobs"][0]["duration_seconds"] >= 0
        assert "auth_config" not in listing["jobs"][0]
        assert "sitemap" not in listing["jobs"][0]


@pytest.mark.asyncio
async def test_list_jobs_includes_terminal_history(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_payload = await cli.create_job(client, "https://example.com")
        job_id = create_payload["job_id"]
        await cli.cancel_job(client, job_id)

        listing = await cli.list_jobs(client)
        assert len(listing["jobs"]) == 1
        assert listing["total"] == 1
        assert listing["jobs"][0]["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server_owned_key,value",
    [
        ("headless", False),
        ("cdp_url", "ws://127.0.0.1:9222/devtools/browser/abc"),
        ("chrome_ws_url", "ws://127.0.0.1:9222/devtools/browser/abc"),
        ("system_chrome", True),
        ("system_chrome_path", "/usr/bin/chromium"),
        ("no_incognito", True),
        ("max_pages", 500),
    ],
)
async def test_scope_config_rejects_server_owned_browser_options(app, server_owned_key: str, value):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "target_url": "https://example.com",
                "scope_config": {server_owned_key: value},
            },
        )
        assert response.status_code == 422
        assert "Unknown scope_config keys" in response.text


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
async def test_get_job_reads_persisted_sitemap_without_reparsing(
    app, monkeypatch: pytest.MonkeyPatch
):
    sitemap = {
        "entries": [{"url": "https://example.com"}],
        "tree": {"children": {}, "pages": []},
    }
    serialized, entry_count, size_bytes = main.orchestrator.serialize_sitemap(sitemap)
    await main.db.execute(
        """
        INSERT INTO jobs (
            job_id, status, target_url, scope_config, auth_config, error, created_at, finished_at,
            sitemap, result_entry_count, result_size_bytes
        )
        VALUES (?, 'completed', ?, NULL, NULL, NULL, datetime('now'), ?, ?, ?, ?)
        """,
        (
            "job-result-1",
            "https://example.com",
            "2026-02-23T12:00:00+00:00",
            serialized,
            entry_count,
            size_bytes,
        ),
    )

    monkeypatch.setattr(
        main.parser,
        "parse_katana_log",
        lambda *args, **kwargs: pytest.fail("persisted results must not be reparsed"),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/jobs/job-result-1")
        second = await client.get("/jobs/job-result-1")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["sitemap"]["entries"][0]["url"] == "https://example.com"
    assert second.json()["sitemap"]["entries"][0]["url"] == "https://example.com"
    assert first.json()["result_metadata"] == {
        "entry_count": 1,
        "size_bytes": size_bytes,
        "completeness": "complete",
        "warnings": [],
    }


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
    empty_sitemap = main.db.dumps_json({"entries": [], "tree": {"children": {}, "pages": []}})
    assert empty_sitemap is not None
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
            generated_exclusions,
            sitemap,
            result_entry_count,
            result_size_bytes
        )
        VALUES (?, 'completed', ?, NULL, NULL, NULL, datetime('now'), ?, ?, ?, 0, ?)
        """,
        (
            "job-exclusions-1",
            "https://example.com",
            "2026-02-23T12:00:00+00:00",
            main.db.dumps_json(exclusions),
            empty_sitemap,
            len(empty_sitemap.encode()),
        ),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/jobs/job-exclusions-1")

    assert response.status_code == 200
    assert response.json()["generated_exclusions"] == exclusions


@pytest.mark.asyncio
async def test_list_jobs_filters_and_paginates_history(app):
    for index, status_value in enumerate(("completed", "failed", "completed")):
        await main.db.execute(
            """
            INSERT INTO jobs (
                job_id, status, target_url, error, created_at, finished_at,
                result_entry_count, result_size_bytes
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                f"job-{index}",
                status_value,
                f"https://example.com/{index}",
                "2026-07-21T12:00:00+00:00",
                "2026-07-21T12:01:00+00:00",
                index if status_value == "completed" else None,
                100 + index if status_value == "completed" else None,
            ),
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/jobs",
            params={"status": "completed", "limit": 1, "offset": 1},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["limit"] == 1
    assert payload["offset"] == 1
    assert [job["job_id"] for job in payload["jobs"]] == ["job-0"]
    assert payload["jobs"][0]["result_metadata"] == {
        "entry_count": 0,
        "size_bytes": 100,
        "completeness": "complete",
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_completed_job_does_not_backfill_from_legacy_logs(app) -> None:
    await main.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at, finished_at)
        VALUES (?, 'completed', ?, datetime('now'), datetime('now'))
        """,
        ("legacy-job", "https://example.com"),
    )
    legacy_log = Path(main.db.LOG_DIR) / "legacy-job.jsonl"
    legacy_log.write_text(
        '{"request":{"method":"GET","url":"https://example.com/"}}\n',
        encoding="utf-8",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/jobs/legacy-job")

    assert response.status_code == 500
    assert response.json()["detail"] == "Terminal job result is unavailable"


@pytest.mark.asyncio
async def test_get_completed_job_without_result_returns_clear_error(app):
    await main.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at, finished_at)
        VALUES (?, 'completed', ?, datetime('now'), datetime('now'))
        """,
        ("missing-result", "https://example.com"),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/jobs/missing-result")

    assert response.status_code == 500
    assert response.json()["detail"] == "Terminal job result is unavailable"


@pytest.mark.asyncio
async def test_get_completed_job_with_invalid_persisted_shape_returns_error(app):
    await main.db.execute(
        """
        INSERT INTO jobs (job_id, status, target_url, created_at, finished_at, sitemap)
        VALUES (?, 'completed', ?, datetime('now'), datetime('now'), ?)
        """,
        ("invalid-result", "https://example.com", "{}"),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/jobs/invalid-result")

    assert response.status_code == 500
    assert response.json()["detail"] == "Persisted job result is corrupt"
