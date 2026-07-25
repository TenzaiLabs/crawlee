from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from .common import redact_command
from .settings import CRAWLER_SUBPROCESS_GRACE_SECONDS, CRAWLER_SUBPROCESS_POLL_INTERVAL_SECONDS

logger = logging.getLogger(__name__)

SUBPROCESS_DIAGNOSTIC_TAIL_BYTES = 64 * 1024


@dataclass(frozen=True)
class SubprocessResult:
    exit_code: int
    output: str


class ProcessMemoryLimitExceeded(RuntimeError):
    pass


def _process_tree_rss_bytes(root_pids: tuple[int, ...]) -> int:
    """Return resident bytes for root process groups and their descendants."""

    roots = set(root_pids)
    if not roots:
        return 0
    page_size = os.sysconf("SC_PAGE_SIZE")
    processes: dict[int, tuple[int, int, int]] = {}
    try:
        entries = os.scandir("/proc")
    except OSError:
        return 0
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                with open(f"/proc/{pid}/stat") as stat_file:
                    stat = stat_file.read()
                with open(f"/proc/{pid}/statm") as statm_file:
                    statm = statm_file.read().split()
                fields = stat[stat.rfind(")") + 2 :].split()
                ppid = int(fields[1])
                process_group = int(fields[2])
                resident_bytes = int(statm[1]) * page_size
            except OSError, ValueError, IndexError:
                continue
            processes[pid] = (ppid, process_group, resident_bytes)

    selected = {pid for pid, (_, process_group, _) in processes.items() if process_group in roots}
    selected.update(pid for pid in roots if pid in processes)
    while True:
        descendants = {pid for pid, (ppid, _, _) in processes.items() if ppid in selected}
        expanded = selected | descendants
        if expanded == selected:
            break
        selected = expanded
    return sum(processes[pid][2] for pid in selected)


class ProcessMemoryBudget:
    """One resident-memory ceiling shared by all process trees in a crawler job."""

    def __init__(self, limit_bytes: int, *, poll_interval_seconds: float = 0.5) -> None:
        if limit_bytes < 1:
            raise ValueError("process memory limit must be positive")
        self.limit_bytes = limit_bytes
        self.poll_interval_seconds = poll_interval_seconds
        self.exceeded = asyncio.Event()
        self.observed_bytes = 0
        self._root_pids: set[int] = set()
        self._monitor_task: asyncio.Task[None] | None = None

    @property
    def failure_message(self) -> str:
        return (
            "Job process memory ceiling exceeded "
            f"({self.observed_bytes} > {self.limit_bytes} bytes)"
        )

    def register(self, pid: int) -> None:
        self.raise_if_exceeded()
        self._root_pids.add(pid)

    def unregister(self, pid: int) -> None:
        self._root_pids.discard(pid)

    def raise_if_exceeded(self) -> None:
        if self.exceeded.is_set():
            raise ProcessMemoryLimitExceeded(self.failure_message)

    async def start(self) -> None:
        if self._monitor_task is not None:
            return
        self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        task = self._monitor_task
        self._monitor_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            roots = tuple(self._root_pids)
            resident_bytes = await asyncio.to_thread(_process_tree_rss_bytes, roots)
            self.observed_bytes = max(self.observed_bytes, resident_bytes)
            if resident_bytes <= self.limit_bytes:
                continue
            logger.warning(
                "Job process memory ceiling exceeded resident_bytes=%d limit_bytes=%d roots=%s",
                resident_bytes,
                self.limit_bytes,
                sorted(roots),
            )
            self.exceeded.set()
            for pid in roots:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
            return


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    logger.debug("Sending SIGTERM to process group pid=%s", process.pid)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    logger.warning("Sending SIGKILL to process group pid=%s", process.pid)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


async def run_safe_subprocess(
    cmd: Iterable[str],
    timeout: float,
    on_output: Callable[[str], Awaitable[None]] | None = None,
    cancel_event: asyncio.Event | None = None,
    stop_event: asyncio.Event | None = None,
    stderr_path: str | None = None,
    memory_budget: ProcessMemoryBudget | None = None,
    diagnostic_tail_bytes: int = SUBPROCESS_DIAGNOSTIC_TAIL_BYTES,
) -> SubprocessResult:
    if diagnostic_tail_bytes < 0:
        raise ValueError("diagnostic_tail_bytes must not be negative")
    command_parts = [str(part) for part in cmd]
    logger.info("Starting subprocess cmd=%s", redact_command(command_parts))
    stderr_file = open(stderr_path, "a") if stderr_path else None
    if memory_budget is not None:
        memory_budget.raise_if_exceeded()
    try:
        process = await asyncio.create_subprocess_exec(
            *command_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_file or asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=10 * 1024 * 1024,  # Katana JSONL responses can produce very long lines.
        )
    except BaseException:
        if stderr_file:
            stderr_file.close()
        raise
    if memory_budget is not None:
        try:
            memory_budget.register(process.pid)
        except BaseException:
            await _kill_process_group(process)
            await process.wait()
            if stderr_file:
                stderr_file.close()
            raise

    wait_task = asyncio.create_task(process.wait())

    # stdout is normally streamed to ``on_output``. Keep only a bounded tail for
    # diagnostics so a verbose subprocess cannot duplicate its complete output
    # in the server process.
    diagnostic_tail = bytearray()
    last_output = time.monotonic()

    async def _read_stream(stream: asyncio.StreamReader | None) -> None:
        nonlocal last_output
        if stream is None:
            return
        while True:
            try:
                line = await stream.readline()
            except ValueError:
                # Line exceeded even the enlarged buffer limit – drain the
                # oversized chunk so the pipe doesn't block the subprocess.
                logger.warning("Subprocess output line exceeded buffer limit, draining")
                try:
                    await stream.read(64 * 1024)
                except Exception:
                    pass
                last_output = time.monotonic()
                continue
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            if diagnostic_tail_bytes:
                if len(line) >= diagnostic_tail_bytes:
                    diagnostic_tail[:] = line[-diagnostic_tail_bytes:]
                else:
                    diagnostic_tail.extend(line)
                    excess = len(diagnostic_tail) - diagnostic_tail_bytes
                    if excess > 0:
                        del diagnostic_tail[:excess]
            last_output = time.monotonic()
            if on_output is not None:
                await on_output(text)

    stdout_task = asyncio.create_task(_read_stream(process.stdout))

    async def _watch_stall() -> None:
        while process.returncode is None:
            await asyncio.sleep(CRAWLER_SUBPROCESS_POLL_INTERVAL_SECONDS)
            if (cancel_event is not None and cancel_event.is_set()) or (
                stop_event is not None and stop_event.is_set()
            ):
                logger.info(
                    "Subprocess stop requested via cancel_event/stop_event pid=%s",
                    process.pid,
                )
                await _terminate_process_group(process)
                await asyncio.sleep(CRAWLER_SUBPROCESS_GRACE_SECONDS)
                await _kill_process_group(process)
                return
            if time.monotonic() - last_output > timeout:
                logger.warning("Subprocess stalled for %.1fs pid=%s", timeout, process.pid)
                await _terminate_process_group(process)
                await asyncio.sleep(CRAWLER_SUBPROCESS_GRACE_SECONDS)
                await _kill_process_group(process)
                return

    stall_task = asyncio.create_task(_watch_stall())

    def _close_stream(stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        transport = getattr(stream, "_transport", None)
        if transport is None:
            return
        with contextlib.suppress(Exception):
            transport.close()

    async def _finalize() -> None:
        """Stop stall watcher + avoid hanging forever on stdout drains.

        If the subprocess spawns descendants that inherit stdout, those descendants
        can keep the PIPE file descriptor open even after the parent exits. In that case
        the reader task never sees EOF and would hang the caller.
        """

        stall_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stall_task

        reader_tasks = {stdout_task}
        _, pending = await asyncio.wait(reader_tasks, timeout=1.0)
        if not pending:
            return

        # Ensure any descendants in the original process group are terminated so the pipes close.
        await _terminate_process_group(process)
        await asyncio.sleep(CRAWLER_SUBPROCESS_GRACE_SECONDS)
        await _kill_process_group(process)

        # Some descendants may have moved to a different session/pgid yet still inherited
        # stdout. In that case, even killing the process group won't close the pipe.
        # Closing our side of the transport guarantees the reader task can unwind.
        _close_stream(process.stdout)

        for task in pending:
            task.cancel()

        # Never block forever during finalize; at worst, we leak a cancelled task, but we
        # let orchestration proceed.
        await asyncio.wait(pending, timeout=1.0)

    try:
        readers_done_at: float | None = None
        while True:
            # asyncio's Process.wait() can remain pending until inherited pipe
            # descriptors reach EOF, even after the direct child has exited.
            # returncode is updated by the child watcher independently.
            if wait_task.done() or process.returncode is not None:
                break
            if stdout_task.done():
                if readers_done_at is None:
                    readers_done_at = time.monotonic()
                elif time.monotonic() - readers_done_at > 1.0:
                    # If the pipes are closed but `process.wait()` doesn't resolve, the
                    # child watcher is wedged. Treat this as process completion and move on.
                    wait_task.cancel()
                    break
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        logger.warning("Subprocess task cancelled pid=%s", process.pid)
        await _terminate_process_group(process)
        await asyncio.sleep(CRAWLER_SUBPROCESS_GRACE_SECONDS)
        await _kill_process_group(process)
        wait_task.cancel()
        raise
    finally:
        await _finalize()
        if memory_budget is not None:
            memory_budget.unregister(process.pid)
        if stderr_file:
            stderr_file.close()

    output = diagnostic_tail.decode("utf-8", errors="replace")
    if memory_budget is not None:
        memory_budget.raise_if_exceeded()
    exit_code = process.returncode
    if exit_code is None and wait_task.done() and not wait_task.cancelled():
        with contextlib.suppress(Exception):
            exit_code = wait_task.result()
    logger.info(
        "Subprocess finished cmd=%s exit_code=%s",
        redact_command(command_parts),
        exit_code or 0,
    )
    return SubprocessResult(exit_code or 0, output)
