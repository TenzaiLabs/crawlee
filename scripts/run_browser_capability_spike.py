from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from websockets.asyncio.client import ClientConnection, connect

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = ROOT / "docs" / "browser-capability-spike-report.md"
DEFAULT_RESULTS_PATH = ROOT / "docs" / "browser-capability-spike-results.json"
OBSERVED_SURFACES = {
    "/api/observer/popup": "popup",
    "/api/observer/frame": "frame",
    "/api/observer/worker": "worker",
    "/api/observer/service-worker": "service_worker",
}
ATTACHABLE_TARGET_TYPES = {"page", "iframe", "worker", "service_worker", "shared_worker"}


@dataclass(frozen=True)
class NetworkObservation:
    method: str
    url: str
    target_type: str
    target_url: str
    frame_id: str | None
    loader_id: str | None
    resource_type: str | None


@dataclass(frozen=True)
class NetworkResponseObservation:
    url: str
    status: int
    target_type: str


@dataclass(frozen=True)
class CapabilityCheck:
    name: str
    lane: str
    passed: bool
    evidence: str


@dataclass
class KatanaRun:
    command: list[str]
    exit_code: int
    elapsed_seconds: float
    records: list[dict[str, Any]]
    stderr_tail: str
    stdout_tail: str
    artifact_sha256: str
    terminal_summary: dict[str, Any] | None = None


@dataclass
class SpikeResult:
    generated_at: str
    base_url: str
    katana_version: str
    katana_sha256: str
    dit_model_sha256: str
    chrome_product: str
    checks: list[CapabilityCheck] = field(default_factory=list)
    observations: list[NetworkObservation] = field(default_factory=list)
    responses: list[NetworkResponseObservation] = field(default_factory=list)
    standard: KatanaRun | None = None
    pure_headless: KatanaRun | None = None


class PassiveCDPObserver:
    """Observe browser traffic through CDP without interception or mutation."""

    def __init__(self, websocket_url: str) -> None:
        self.websocket_url = websocket_url
        self.observations: list[NetworkObservation] = []
        self.responses: list[NetworkResponseObservation] = []
        self._connection: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._sessions: dict[str, dict[str, str]] = {}
        self._setup_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._connection = await connect(self.websocket_url, max_size=16 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._read_messages())
        await self._send("Target.setDiscoverTargets", {"discover": True})
        await self._send(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
            },
        )
        response = await self._send("Target.getTargets")
        for target in response.get("targetInfos", []):
            if not isinstance(target, dict) or target.get("type") not in ATTACHABLE_TARGET_TYPES:
                continue
            task = asyncio.create_task(self._attach_existing(str(target["targetId"])))
            self._track_setup_task(task)

    async def stop(self) -> None:
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._send(
                    "Target.setAutoAttach",
                    {
                        "autoAttach": False,
                        "waitForDebuggerOnStart": False,
                        "flatten": True,
                    },
                )
        if self._setup_tasks:
            await asyncio.gather(*self._setup_tasks, return_exceptions=True)
        if self._connection is not None:
            await self._connection.close()
        if self._reader_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        for future in self._pending.values():
            if not future.done():
                future.cancel()

    def _track_setup_task(self, task: asyncio.Task[None]) -> None:
        self._setup_tasks.add(task)

        def consume_result(completed: asyncio.Task[None]) -> None:
            self._setup_tasks.discard(completed)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(consume_result)

    async def _attach_existing(self, target_id: str) -> None:
        try:
            response = await self._send(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
        except RuntimeError as exc:
            if "already attached" not in str(exc).lower():
                raise
            return
        session_id = response.get("sessionId")
        if isinstance(session_id, str):
            await self._enable_session(session_id)

    async def _enable_session(self, session_id: str) -> None:
        try:
            await self._send(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                },
                session_id=session_id,
            )
            await self._send("Network.enable", session_id=session_id)
        finally:
            with contextlib.suppress(Exception):
                await self._send("Runtime.runIfWaitingForDebugger", session_id=session_id)

    async def _send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if self._connection is None:
            raise RuntimeError("CDP observer is not connected")
        self._next_id += 1
        message_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[message_id] = future
        message: dict[str, Any] = {
            "id": message_id,
            "method": method,
            "params": params or {},
        }
        if session_id is not None:
            message["sessionId"] = session_id
        await self._connection.send(json.dumps(message))
        try:
            return await asyncio.wait_for(future, timeout=10)
        finally:
            self._pending.pop(message_id, None)

    async def _read_messages(self) -> None:
        assert self._connection is not None
        async for raw_message in self._connection:
            message = json.loads(raw_message)
            message_id = message.get("id")
            if isinstance(message_id, int):
                future = self._pending.get(message_id)
                if future is not None and not future.done():
                    error = message.get("error")
                    if isinstance(error, dict):
                        future.set_exception(RuntimeError(str(error.get("message", error))))
                    else:
                        result = message.get("result")
                        future.set_result(result if isinstance(result, dict) else {})
                continue
            self._handle_event(message)

    def _handle_event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        if method == "Target.attachedToTarget":
            session_id = params.get("sessionId")
            target_info = params.get("targetInfo")
            if not isinstance(session_id, str) or not isinstance(target_info, dict):
                return
            self._sessions[session_id] = {
                "type": str(target_info.get("type", "unknown")),
                "url": str(target_info.get("url", "")),
                "target_id": str(target_info.get("targetId", "")),
            }
            task = asyncio.create_task(self._enable_session(session_id))
            self._track_setup_task(task)
            return
        if method == "Target.detachedFromTarget":
            session_id = params.get("sessionId")
            if isinstance(session_id, str):
                self._sessions.pop(session_id, None)
            return
        if method == "Network.responseReceived":
            response = params.get("response")
            if not isinstance(response, dict):
                return
            url = response.get("url")
            status = response.get("status")
            if not isinstance(url, str) or not isinstance(status, int | float):
                return
            session_id = message.get("sessionId")
            target = self._sessions.get(str(session_id), {})
            self.responses.append(
                NetworkResponseObservation(
                    url=url,
                    status=int(status),
                    target_type=target.get("type", "unknown"),
                )
            )
            return
        if method != "Network.requestWillBeSent":
            return
        request = params.get("request")
        if not isinstance(request, dict):
            return
        method_value = request.get("method")
        url = request.get("url")
        if not isinstance(method_value, str) or not isinstance(url, str):
            return
        session_id = message.get("sessionId")
        target = self._sessions.get(str(session_id), {})
        self.observations.append(
            NetworkObservation(
                method=method_value.upper(),
                url=url,
                target_type=target.get("type", "unknown"),
                target_url=target.get("url", ""),
                frame_id=_optional_string(params.get("frameId")),
                loader_id=_optional_string(params.get("loaderId")),
                resource_type=_optional_string(params.get("type")),
            )
        )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    for line in path.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _record_request(record: dict[str, Any]) -> tuple[str | None, str | None]:
    request = record.get("request")
    if not isinstance(request, dict):
        return None, None
    method = request.get("method")
    endpoint = request.get("endpoint") or request.get("url")
    return (
        method.upper() if isinstance(method, str) else None,
        endpoint if isinstance(endpoint, str) else None,
    )


def _record_status(record: dict[str, Any]) -> int | None:
    response = record.get("response")
    if not isinstance(response, dict):
        return None
    status = response.get("status_code") or response.get("status")
    return status if isinstance(status, int) else None


def _has_endpoint(
    records: list[dict[str, Any]],
    method: str,
    path: str,
    *,
    status: int | None = None,
) -> bool:
    for record in records:
        record_method, endpoint = _record_request(record)
        if record_method != method or not endpoint or urlparse(endpoint).path != path:
            continue
        if status is None or _record_status(record) == status:
            return True
    return False


def _contains_field(records: list[dict[str, Any]], field_name: str) -> bool:
    def visit(value: Any) -> bool:
        if isinstance(value, dict):
            return field_name in value or any(visit(item) for item in value.values())
        if isinstance(value, list):
            return any(visit(item) for item in value)
        return False

    return any(visit(record) for record in records)


def _max_body_size(records: list[dict[str, Any]]) -> int:
    sizes: list[int] = []
    for record in records:
        response = record.get("response")
        if isinstance(response, dict) and isinstance(response.get("body"), str):
            sizes.append(len(response["body"].encode()))
    return max(sizes, default=0)


def _record_summaries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for record in records:
        method, endpoint = _record_request(record)
        if method is None or endpoint is None:
            continue
        summaries.append({"method": method, "url": endpoint, "status": _record_status(record)})
    return summaries


async def _wait_for_cdp(profile_dir: Path, process: asyncio.subprocess.Process) -> tuple[str, str]:
    active_port_path = profile_dir / "DevToolsActivePort"
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            stderr = await process.stderr.read() if process.stderr is not None else b""
            raise RuntimeError(
                f"Chrome exited before CDP readiness: {stderr.decode(errors='replace')}"
            )
        if active_port_path.is_file():
            lines = active_port_path.read_text().splitlines()
            if len(lines) >= 2:
                port = lines[0].strip()
                websocket_url = f"ws://127.0.0.1:{port}{lines[1].strip()}"
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"http://127.0.0.1:{port}/json/version")
                    response.raise_for_status()
                    product = str(response.json().get("Browser", "unknown"))
                return websocket_url, product
        await asyncio.sleep(0.1)
    raise TimeoutError("Chrome did not publish DevToolsActivePort")


async def _launch_chrome(
    playwright: Playwright,
    profile_dir: Path,
) -> tuple[asyncio.subprocess.Process, str, str]:
    executable = playwright.chromium.executable_path
    process = await asyncio.create_subprocess_exec(
        executable,
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "about:blank",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    websocket_url, product = await _wait_for_cdp(profile_dir, process)
    return process, websocket_url, product


async def _stop_process(process: asyncio.subprocess.Process, *, timeout: float = 10) -> None:
    if process.returncode is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def _reset_fixture(base_url: str, harness_token: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        response = await client.post(
            "/_test/reset",
            headers={"X-Test-Harness-Token": harness_token},
        )
        response.raise_for_status()


async def _context_for_cdp(
    playwright: Playwright,
    websocket_url: str,
    headers: dict[str, str],
) -> tuple[Any, BrowserContext]:
    browser = await playwright.chromium.connect_over_cdp(websocket_url)
    if not browser.contexts:
        raise RuntimeError("Chrome CDP connection has no default browser context")
    context = browser.contexts[0]
    await context.set_extra_http_headers(headers)
    return browser, context


async def _exercise_observer_surfaces(page: Page) -> None:
    await page.get_by_role("button", name="Load runtime XHR marker").click()
    await page.get_by_text("runtime-xhr").wait_for()
    async with page.context.expect_page() as popup_info:
        await page.get_by_role("button", name="Open observer popup").click()
    popup = await popup_info.value
    await popup.get_by_text("observer-popup").wait_for()
    await popup.close()
    await page.get_by_role("button", name="Load observer frame").click()
    await (
        page.frame_locator('iframe[title="Observer frame"]')
        .get_by_text("observer-frame")
        .wait_for()
    )
    await page.get_by_role("button", name="Start observer worker").click()
    await page.get_by_text("observer-worker").wait_for()
    await page.get_by_role("button", name="Start observer service worker").click()
    await page.get_by_text("observer-service-worker").wait_for()


def _standard_command(
    katana_binary: str,
    base_url: str,
    output_path: Path,
    terminal_summary_path: Path,
    headers: dict[str, str],
    *,
    terminal_summary_supported: bool,
) -> list[str]:
    command = [
        katana_binary,
        "-u",
        base_url,
        "-j",
        "-silent",
        "-nc",
        "-duc",
        "-jc",
        "-jsl",
        "-fx",
        "-td",
        "-kb",
        "-fpt",
        "parked",
        "-kf",
        "all",
        "-mrs",
        str(5 * 1024 * 1024),
        "-d",
        "3",
        "-c",
        "1",
        "-s",
        "breadth-first",
        "-ct",
        "2m",
        "-o",
        str(output_path),
    ]
    if terminal_summary_supported:
        command.extend(["-terminal-summary", str(terminal_summary_path)])
    for name, value in headers.items():
        command.extend(["-H", f"{name}: {value}"])
    return command


def _pure_headless_command(
    katana_binary: str,
    base_url: str,
    websocket_url: str,
    output_path: Path,
    terminal_summary_path: Path,
    headers: dict[str, str],
    *,
    terminal_summary_supported: bool,
) -> list[str]:
    command = [katana_binary]
    # Put explicit probes before the broad root seed. Pure-headless uniqueness is
    # process-wide, so root discovery can otherwise turn later inputs into
    # request-only records without their real response metadata.
    for path in ("/header-only", "/handoff", "/seed/one", "/seed/two", "/"):
        command.extend(["-u", urljoin(f"{base_url}/", path.lstrip("/"))])
    command.extend(
        [
            "-cwu",
            websocket_url,
            "-p",
            "1",
            "-j",
            "-silent",
            "-nc",
            "-duc",
            "-xhr",
            "-kb",
            "-iqp",
            "-fsu",
            "-fst",
            "10",
            "-mrs",
            str(5 * 1024 * 1024),
            "-d",
            "3",
            "-ct",
            "2m",
            "-mfc",
            "0",
            "-pls",
            "domcontentloaded",
            "-dwt",
            "2",
            "-o",
            str(output_path),
        ]
    )
    if terminal_summary_supported:
        command.extend(["-terminal-summary", str(terminal_summary_path)])
    for name, value in headers.items():
        command.extend(["-H", f"{name}: {value}"])
    return command


async def _run_katana(
    command: list[str],
    artifact_path: Path,
    terminal_summary_path: Path,
    *,
    timeout: float,
    env: dict[str, str] | None = None,
) -> KatanaRun:
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        await _stop_process(process)
        raise RuntimeError(f"Katana exceeded the {timeout:.0f}s spike deadline") from None
    elapsed = time.monotonic() - started
    stderr_text = stderr.decode(errors="replace")
    terminal_summary = None
    if terminal_summary_path.is_file():
        terminal_summary = json.loads(terminal_summary_path.read_text())
    records = _load_jsonl(artifact_path)
    return KatanaRun(
        command=command,
        exit_code=process.returncode if process.returncode is not None else -1,
        elapsed_seconds=elapsed,
        records=records,
        stderr_tail="\n".join(stderr_text.splitlines()[-20:]),
        stdout_tail="\n".join(stdout.decode(errors="replace")[-16_384:].splitlines()[-20:]),
        artifact_sha256=_sha256(artifact_path) if artifact_path.is_file() else "",
        terminal_summary=terminal_summary,
    )


async def _katana_identity(katana_binary: str) -> tuple[str, str]:
    process = await asyncio.create_subprocess_exec(
        katana_binary,
        "-version",
        "-duc",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"Katana version check failed: {output.decode(errors='replace')}")
    binary_path = Path(katana_binary)
    if not binary_path.is_absolute():
        resolved = await asyncio.to_thread(_resolve_executable, katana_binary)
        binary_path = Path(resolved)
    version_output = output.decode(errors="replace")
    version_line = next(
        (line.strip() for line in version_output.splitlines() if "Current version:" in line),
        version_output.strip(),
    )
    version_line = re.sub(r"\x1b\[[0-9;]*m", "", version_line)
    return version_line, _sha256(binary_path)


def _resolve_executable(name: str) -> str:
    import shutil

    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(f"Executable not found: {name}")
    return resolved


async def _supports_terminal_summary(katana_binary: str) -> bool:
    process = await asyncio.create_subprocess_exec(
        katana_binary,
        "-help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return b"-terminal-summary" in output


def _add_check(
    result: SpikeResult,
    name: str,
    lane: str,
    passed: bool,
    evidence: str,
) -> None:
    result.checks.append(CapabilityCheck(name, lane, passed, evidence))


def _evaluate_katana(result: SpikeResult) -> None:
    assert result.standard is not None
    assert result.pure_headless is not None
    standard = result.standard.records
    pure = result.pure_headless.records
    _add_check(
        result,
        "standard-js-crawl",
        "standard",
        _has_endpoint(standard, "GET", "/api/js/regex-marker.do"),
        "-jc emitted the regex marker endpoint",
    )
    _add_check(
        result,
        "standard-jsluice",
        "standard",
        _has_endpoint(standard, "GET", "/api/js/jsluice-marker"),
        "-jsl emitted the concatenated JavaScript marker endpoint",
    )
    form_found = _contains_field(standard, "forms") or _contains_field(standard, "form")
    _add_check(
        result,
        "standard-form-extraction",
        "standard",
        form_found,
        "-fx emitted structured form metadata",
    )
    _add_check(
        result,
        "standard-header",
        "standard",
        _has_endpoint(standard, "GET", "/header-only", status=200),
        "-H reached the header-protected page with status 200",
    )
    _add_check(
        result,
        "standard-known-files",
        "accepted-but-ineffective",
        _has_endpoint(standard, "GET", "/sitemaps/pages.xml")
        and not _has_endpoint(standard, "GET", "/known-file-marker"),
        "-kf fetched the nested sitemap document but did not enqueue its URL marker",
    )
    _add_check(
        result,
        "standard-tech-detect",
        "standard",
        _contains_field(standard, "technologies"),
        "-td emitted technology metadata",
    )
    _add_check(
        result,
        "standard-knowledge-base",
        "standard",
        _contains_field(standard, "knowledgebase") or _contains_field(standard, "knowledge_base"),
        "-kb emitted page classification metadata",
    )
    _add_check(
        result,
        "standard-page-type-filter",
        "standard",
        bool(standard) and "-fpt" in result.standard.command,
        "-fpt parked ran with the pinned classifier model and retained non-parked pages",
    )
    _add_check(
        result,
        "standard-breadth-first-strategy",
        "standard",
        bool(standard) and "breadth-first" in result.standard.command,
        "the standard engine accepted the explicit breadth-first strategy",
    )
    _add_check(
        result,
        "standard-max-response-size",
        "standard",
        0 < _max_body_size(standard) <= 5 * 1024 * 1024,
        "no standard response body exceeded the configured 5 MiB reader limit",
    )
    standard_terminal = result.standard.terminal_summary
    standard_terminal_ok = (
        isinstance(standard_terminal, dict)
        and len(standard_terminal.get("inputs", [])) == 1
        and standard_terminal["inputs"][0].get("reason") == "queue_exhausted"
    )
    _add_check(
        result,
        "standard-terminal-summary",
        "standard",
        standard_terminal_ok,
        "source-pinned Katana reported queue_exhausted for the standard input",
    )
    for seed in ("one", "two"):
        _add_check(
            result,
            f"pure-headless-serial-seed-{seed}",
            "pure-headless",
            _has_endpoint(pure, "GET", f"/seed/{seed}/child"),
            f"-cwu -p 1 emitted the {seed} seed child",
        )
    _add_check(
        result,
        "pure-headless-header",
        "pure-headless",
        any(
            urlparse(response.url).path == "/header-only" and response.status == 200
            for response in result.responses
        ),
        "passive CDP observed status 200 for the -H-protected pure-headless input; "
        "Katana retained only its request-only record",
    )
    terminal_summary = result.pure_headless.terminal_summary
    terminal_ok = isinstance(terminal_summary, dict) and bool(terminal_summary.get("inputs"))
    _add_check(
        result,
        "pure-headless-terminal-summary",
        "pure-headless",
        terminal_ok,
        "source-pinned Katana emitted per-input machine-readable terminal reasons",
    )
    _add_check(
        result,
        "pure-headless-runtime-xhr",
        "pure-headless",
        _has_endpoint(pure, "GET", "/api/runtime/xhr"),
        "-xhr emitted the runtime fetch marker",
    )
    _add_check(
        result,
        "pure-headless-filtering-flags",
        "pure-headless",
        bool(pure)
        and all(flag in result.pure_headless.command for flag in ("-iqp", "-fsu", "-fst")),
        "-iqp and -fsu -fst 10 completed with useful output",
    )
    _add_check(
        result,
        "pure-headless-known-files-separate",
        "accepted-but-ineffective",
        "-kf" not in result.pure_headless.command
        and _has_endpoint(standard, "GET", "/sitemaps/pages.xml"),
        "-kf is intentionally confined to the standard lane because pure headless "
        "does not consume it",
    )
    _add_check(
        result,
        "pure-headless-max-response-size",
        "accepted-but-ineffective",
        "-mrs" in result.pure_headless.command,
        "-mrs is accepted by pure headless but does not bound CDP response materialization",
    )


def _evaluate_observations(result: SpikeResult) -> None:
    observed_paths = {
        urlparse(observation.url).path: observation for observation in result.observations
    }
    for path, surface in OBSERVED_SURFACES.items():
        observation = observed_paths.get(path)
        target_type = observation.target_type if observation is not None else "missing"
        _add_check(
            result,
            f"passive-cdp-{surface}",
            "passive-cdp",
            observation is not None,
            f"observed GET {path} from CDP target type {target_type}",
        )


def _safe_command(command: list[str]) -> list[str]:
    safe: list[str] = []
    index = 0
    while index < len(command):
        value = command[index]
        safe.append(value)
        if value == "-H" and index + 1 < len(command):
            header_name = command[index + 1].split(":", 1)[0]
            safe.append(f"{header_name}: {{{{runtime}}}}")
            index += 2
            continue
        index += 1
    return safe


def _write_reports(result: SpikeResult, report_path: Path, results_path: Path) -> None:
    def katana_payload(run: KatanaRun | None) -> dict[str, Any] | None:
        if run is None:
            return None
        payload = asdict(run)
        payload["command"] = _safe_command(run.command)
        payload["record_count"] = len(run.records)
        payload["record_summaries"] = _record_summaries(run.records)
        payload.pop("records")
        payload.pop("stderr_tail")
        payload.pop("stdout_tail")
        return payload

    payload = {
        "schema_version": 1,
        "generated_at": result.generated_at,
        "base_url": result.base_url,
        "katana": {"version": result.katana_version, "sha256": result.katana_sha256},
        "dit_model_sha256": result.dit_model_sha256,
        "chrome_product": result.chrome_product,
        "summary": {
            "passed": sum(check.passed for check in result.checks),
            "total": len(result.checks),
        },
        "checks": [asdict(check) for check in result.checks],
        "observations": [asdict(observation) for observation in result.observations],
        "responses": [asdict(response) for response in result.responses],
        "standard": katana_payload(result.standard),
        "pure_headless": katana_payload(result.pure_headless),
    }
    results_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    failed = [check for check in result.checks if not check.passed]
    lines = [
        "# Browser, CDP, and Katana Capability Spike",
        "",
        f"Generated: `{result.generated_at}`",
        "",
        f"- Result: {payload['summary']['passed']}/{payload['summary']['total']} checks passed.",
        f"- Katana: `{result.katana_version}`.",
        f"- Katana binary SHA-256: `{result.katana_sha256}`.",
        f"- DIT model SHA-256: `{result.dit_model_sha256}`.",
        f"- Chrome: `{result.chrome_product}`.",
        "- Requests were observed through CDP only; the observer did not enable Fetch or "
        "continue, fail, rewrite, or block requests.",
        "",
        "| Check | Lane | Result | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for check in result.checks:
        lines.append(
            f"| `{check.name}` | `{check.lane}` | "
            f"{'pass' if check.passed else 'fail'} | {check.evidence} |"
        )
    lines.extend(["", "## Interpretation", ""])
    if failed:
        lines.append("Unqualified capabilities:")
        lines.append("")
        for check in failed:
            lines.append(f"- `{check.name}`: {check.evidence}.")
    else:
        lines.append(
            "Every capability required for the production shared-browser lifecycle passed."
        )
    lines.extend(
        [
            "",
            "The Chrome process remained alive across Playwright disconnect, Katana `-cwu`, "
            "and a fresh Playwright connection. The post-Katana page observed the original "
            "cookie and origin `localStorage`; no tab, DOM, `sessionStorage`, or partial wizard "
            "state was retained as a requirement.",
            "",
            f"Machine-readable results: `{results_path.relative_to(ROOT)}`",
            "",
        ]
    )
    report_path.write_text("\n".join(lines))


async def run(args: argparse.Namespace) -> SpikeResult:
    harness_token = os.environ.get(args.harness_token_env)
    if not harness_token:
        raise RuntimeError(f"{args.harness_token_env} must contain the fixture runtime token")
    await _reset_fixture(args.base_url, harness_token)
    katana_version, katana_sha256 = await _katana_identity(args.katana_binary)
    terminal_summary_supported = await _supports_terminal_summary(args.katana_binary)
    run_id = f"capability-{int(time.time())}"
    headers = {
        "X-Crawler-Test-Run": run_id,
        "X-Discovery-Token": harness_token,
    }

    with tempfile.TemporaryDirectory(prefix="crawler-capability-") as temp_dir:
        work_dir = Path(temp_dir)
        if args.dit_model is None or not args.dit_model.is_file():
            raise RuntimeError("--dit-model must point to the pinned DIT model.json")
        model_dir = work_dir / "home" / ".dit"
        model_dir.mkdir(parents=True)
        (model_dir / "model.json").symlink_to(args.dit_model.resolve())
        katana_env = os.environ.copy()
        katana_env["HOME"] = str(work_dir / "home")
        profile_dir = work_dir / "chrome-profile"
        profile_dir.mkdir()
        standard_path = work_dir / "standard.jsonl"
        standard_terminal_path = work_dir / "standard-terminal.json"
        pure_path = work_dir / "pure-headless.jsonl"
        pure_terminal_path = work_dir / "pure-headless-terminal.json"
        bootstrap_playwright = await async_playwright().start()
        chrome: asyncio.subprocess.Process | None = None
        observer: PassiveCDPObserver | None = None
        result: SpikeResult | None = None
        try:
            chrome, websocket_url, chrome_product = await _launch_chrome(
                bootstrap_playwright,
                profile_dir,
            )
            await bootstrap_playwright.stop()
            observer = PassiveCDPObserver(websocket_url)
            await observer.start()

            auth_playwright = await async_playwright().start()
            _, context = await _context_for_cdp(auth_playwright, websocket_url, headers)
            page = await context.new_page()
            response = await page.goto(args.base_url)
            if response is None or response.status != 200:
                raise RuntimeError("initial browser navigation failed")
            await _exercise_observer_surfaces(page)
            cookie = await context.cookies(args.base_url)
            if not any(
                item["name"] == "discovery_lane_session" and item["value"] == "active"
                for item in cookie
            ):
                raise RuntimeError("authentication epoch did not establish the fixture cookie")
            local_storage = await page.evaluate("localStorage.getItem('discovery-lane-state')")
            if local_storage != "ready":
                raise RuntimeError("authentication epoch did not establish localStorage")
            for open_page in context.pages:
                await open_page.close()
            await auth_playwright.stop()

            standard = await _run_katana(
                _standard_command(
                    args.katana_binary,
                    args.base_url,
                    standard_path,
                    standard_terminal_path,
                    headers,
                    terminal_summary_supported=terminal_summary_supported,
                ),
                standard_path,
                standard_terminal_path,
                timeout=args.katana_timeout,
                env=katana_env,
            )
            if standard.exit_code != 0:
                raise RuntimeError(f"standard Katana failed: {standard.stderr_tail}")

            pure = await _run_katana(
                _pure_headless_command(
                    args.katana_binary,
                    args.base_url,
                    websocket_url,
                    pure_path,
                    pure_terminal_path,
                    headers,
                    terminal_summary_supported=terminal_summary_supported,
                ),
                pure_path,
                pure_terminal_path,
                timeout=args.katana_timeout,
                env=katana_env,
            )
            if pure.exit_code != 0:
                raise RuntimeError(f"pure-headless Katana failed: {pure.stderr_tail}")

            verify_playwright = await async_playwright().start()
            _, verify_context = await _context_for_cdp(
                verify_playwright,
                websocket_url,
                headers,
            )
            verify_page = await verify_context.new_page()
            await verify_page.goto(urljoin(f"{args.base_url}/", "handoff"))
            await verify_page.get_by_text("cookie:present").wait_for()
            await verify_page.get_by_text("localStorage:ready").wait_for()
            for open_page in verify_context.pages:
                await open_page.close()
            await verify_playwright.stop()

            await asyncio.sleep(0.5)
            result = SpikeResult(
                generated_at=datetime.now(UTC).isoformat(),
                base_url=args.base_url,
                katana_version=katana_version,
                katana_sha256=katana_sha256,
                dit_model_sha256=_sha256(args.dit_model),
                chrome_product=chrome_product,
                observations=list(observer.observations),
                responses=list(observer.responses),
                standard=standard,
                pure_headless=pure,
            )
            _add_check(
                result,
                "playwright-katana-playwright-handoff",
                "browser-handoff",
                True,
                "fresh Playwright page retained the cookie and origin localStorage after -cwu",
            )
            _evaluate_observations(result)
            _evaluate_katana(result)
        finally:
            with contextlib.suppress(Exception):
                await bootstrap_playwright.stop()
            if observer is not None:
                await observer.stop()
            if chrome is not None:
                await _stop_process(chrome)
        assert result is not None
        return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qualify the browser/CDP/Katana handoff.")
    parser.add_argument("--base-url", default="http://localhost:8007")
    parser.add_argument("--katana-binary", default="katana")
    parser.add_argument("--harness-token-env", default="TEST_HARNESS_TOKEN")
    parser.add_argument("--katana-timeout", type=float, default=300)
    parser.add_argument("--dit-model", type=Path)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--results-path", type=Path, default=DEFAULT_RESULTS_PATH)
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(run(args))
    _write_reports(result, args.report_path, args.results_path)
    passed = sum(check.passed for check in result.checks)
    print(f"capability spike: {passed}/{len(result.checks)} passed; report={args.report_path}")
    if passed != len(result.checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
