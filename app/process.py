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


@dataclass(frozen=True)
class SubprocessResult:
    exit_code: int
    output: str


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
    env: dict[str, str] | None = None,
    stderr_path: str | None = None,
) -> SubprocessResult:
    command_parts = [str(part) for part in cmd]
    logger.info("Starting subprocess cmd=%s", redact_command(command_parts))
    subprocess_env: dict[str, str] | None = None
    if env:
        subprocess_env = {**os.environ, **env}

    stderr_file = open(stderr_path, "a") if stderr_path else None
    try:
        process = await asyncio.create_subprocess_exec(
            *command_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_file or asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=subprocess_env,
            limit=10 * 1024 * 1024,  # 10 MiB – proxify can emit very long lines
        )
    except BaseException:
        if stderr_file:
            stderr_file.close()
        raise

    wait_task = asyncio.create_task(process.wait())

    output_lines: list[str] = []
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
                    await stream.read(len(stream._buffer))  # type: ignore[attr-defined]
                except Exception:
                    pass
                last_output = time.monotonic()
                continue
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            output_lines.append(text)
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
            if wait_task.done():
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
        if stderr_file:
            stderr_file.close()

    output = "".join(output_lines)
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
