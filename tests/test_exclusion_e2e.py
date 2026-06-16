from __future__ import annotations

import contextlib
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator
from urllib.parse import urlparse

import pytest

from app import crawler

ROOT = Path(__file__).resolve().parents[1]
TESTSITES_ROOT = ROOT / "testsites"
TESTSITES_PYTHON = TESTSITES_ROOT / ".venv" / "bin" / "python"


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@dataclass(frozen=True)
class FullSiteExclusionCase:
    name: str
    target_url: str
    headers: tuple[str, ...]
    expected_path: str
    blocked_paths: set[str]


@contextmanager
def static_site(root: Path) -> Iterator[str]:
    class Handler(QuietStaticHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = b""
            if process.stdout is not None:
                output = process.stdout.read()
            raise RuntimeError(
                f"fixture process exited early with code {process.returncode}: "
                f"{output.decode('utf-8', errors='replace')}"
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for fixture port {port}")


@contextmanager
def flask_site(site_dir: Path) -> Iterator[str]:
    python = TESTSITES_PYTHON if TESTSITES_PYTHON.exists() else Path(sys.executable)
    port = _free_port()
    env = os.environ.copy()
    env["PORT"] = str(port)
    process = subprocess.Popen(
        [str(python), "app.py"],
        cwd=site_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        _wait_for_port(port, process)
        yield f"http://127.0.0.1:{port}"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def _katana_paths(
    target_url: str,
    *,
    headers: tuple[str, ...] = (),
    exclusions: list[str],
) -> set[str]:
    command = [
        "katana",
        "-u",
        target_url,
        "-silent",
        "-jsonl",
        "-known-files",
        "all",
        "-no-color",
        "-verbose",
        "-fs",
        "rdn",
        "-d",
        "3",
        "-rl",
        "20",
    ]
    if exclusions:
        command.extend(["-crawl-out-scope", "|".join(exclusions)])
    for header in headers:
        command.extend(["-H", header])

    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr

    paths: set[str] = set()
    for line in result.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        request = data.get("request")
        if not isinstance(request, dict):
            continue
        endpoint = request.get("endpoint") or request.get("url")
        if isinstance(endpoint, str):
            paths.add(urlparse(endpoint).path or "/")
    return paths


def test_full_testsite_exclusion_positive_control() -> None:
    if os.getenv("RUN_E2E") != "1":
        pytest.skip("Set RUN_E2E=1 to run end-to-end crawl scenario")
    if shutil.which("katana") is None:
        pytest.skip("End-to-end crawl requires katana")

    default_exclusions = list(crawler.DEFAULT_EXCLUSION_PATTERNS)
    with ExitStack() as stack:
        site_a_url = stack.enter_context(static_site(TESTSITES_ROOT / "site-a-static" / "html"))
        auth_b_url = stack.enter_context(flask_site(TESTSITES_ROOT / "auth-b-http-basic"))
        cases = [
            FullSiteExclusionCase(
                name="site-a-static",
                target_url=site_a_url,
                headers=(),
                expected_path="/workspace.html",
                blocked_paths={"/workspace/deleted.html"},
            ),
            FullSiteExclusionCase(
                name="auth-b-http-basic",
                target_url=auth_b_url,
                headers=("Authorization: Basic dXNlcjpwYXNz",),
                expected_path="/app/overview",
                blocked_paths={"/logout"},
            ),
        ]

        for case in cases:
            default_paths = _katana_paths(
                case.target_url,
                headers=case.headers,
                exclusions=default_exclusions,
            )
            assert case.expected_path in default_paths, (case.name, sorted(default_paths))
            assert default_paths.isdisjoint(case.blocked_paths), (case.name, sorted(default_paths))

            disabled_paths = _katana_paths(case.target_url, headers=case.headers, exclusions=[])
            assert case.blocked_paths <= disabled_paths, (case.name, sorted(disabled_paths))
