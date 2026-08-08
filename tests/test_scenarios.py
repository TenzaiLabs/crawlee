from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
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


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@asynccontextmanager
async def _crawler_server(tmp_path: Path) -> AsyncIterator[str]:
    port = _reserve_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "crawler-server.log"
    log_file = log_path.open("wb")
    environment = os.environ.copy()
    environment.update(
        {
            "CRAWLER_HOST": "127.0.0.1",
            "CRAWLER_PORT": str(port),
            "CRAWLER_DB_PATH": str(tmp_path / "jobs.db"),
            "CRAWLER_LOG_DIR": str(tmp_path / "logs"),
        }
    )
    process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "tenzai-crawler-server",
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=log_file,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=2) as client:
            for _ in range(120):
                if process.returncode is not None:
                    log_file.flush()
                    raise RuntimeError(
                        f"crawler server exited with {process.returncode}:\n"
                        f"{log_path.read_text(errors='replace')}"
                    )
                try:
                    response = await client.get("/openapi.json")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError("crawler server readiness timed out")
        yield base_url
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        log_file.close()


@pytest.mark.asyncio
async def test_happy_path_crawl_succeeds(tmp_path: Path):
    if os.getenv("RUN_E2E") != "1":
        pytest.skip("Set RUN_E2E=1 to run end-to-end crawl scenario")
    if shutil.which("katana") is None:
        pytest.skip("End-to-end crawl requires the katana binary")
    async with _local_testsite() as target_url, _crawler_server(tmp_path) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as client:
            response = await client.post(
                "/jobs",
                json={
                    "target_url": target_url,
                    "scope_config": {
                        "max_depth": 2,
                        "crawl_duration": "20s",
                    },
                    "discovery": {"enabled": False},
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
