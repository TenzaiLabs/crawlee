from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import BaseServer
from threading import Thread

import httpx
import pytest


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@asynccontextmanager
async def _local_testsite() -> AsyncIterator[str]:
    root = Path(__file__).resolve().parents[1] / "testsites" / "site-a-static" / "html"

    class Handler(_QuietStaticHandler):
        def __init__(
            self,
            request,
            client_address: tuple[str, int],
            server: BaseServer,
        ) -> None:
            super().__init__(request, client_address, server, directory=str(root))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = str(server.server_address[0])
        port = int(server.server_address[1])
        yield f"http://{host}:{port}"
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_happy_path_crawl_succeeds(app_with_orchestrator):
    if os.getenv("RUN_E2E") != "1":
        pytest.skip("Set RUN_E2E=1 to run end-to-end crawl scenario")
    if shutil.which("katana") is None or shutil.which("proxify") is None:
        pytest.skip("End-to-end crawl requires katana and proxify binaries")
    async with _local_testsite() as target_url:
        transport = httpx.ASGITransport(app=app_with_orchestrator)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/jobs",
                json={
                    "target_url": target_url,
                    "scope_config": {
                        "headless": False,
                        "max_depth": 2,
                        "max_pages": 25,
                        "crawl_duration": "20s",
                    },
                },
            )
            response.raise_for_status()
            job_id = response.json()["job_id"]

            payload = {}
            for _ in range(120):
                status_response = await client.get(f"/jobs/{job_id}")
                status_response.raise_for_status()
                payload = status_response.json()
                if payload["status"] in {
                    "completed",
                    "failed",
                    "failed_interrupted",
                    "cancelled",
                }:
                    break
                await asyncio.sleep(0.5)

            assert payload["status"] == "completed"
            entries = payload["sitemap"]["entries"]
            assert any(entry["url"].endswith("/about.html") for entry in entries)
