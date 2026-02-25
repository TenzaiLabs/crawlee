from __future__ import annotations

import os
import signal
import sys

import pytest

from app.process import run_safe_subprocess


@pytest.mark.asyncio
async def test_run_safe_subprocess_captures_output():
    result = await run_safe_subprocess(
        [sys.executable, "-c", "print('hello')"],
        timeout=5,
    )

    assert result.exit_code == 0
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_run_safe_subprocess_does_not_hang_when_descendant_holds_pipes_open():
    # Simulate a tool that spawns a descendant which inherits stdout/stderr, then exits.
    # If we wait forever for EOF on the stdout/stderr pipes, the caller hangs even though
    # the main process is already gone.
    code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
        "print('parent done')"
    )

    result = await run_safe_subprocess(
        [sys.executable, "-c", code],
        timeout=5,
    )

    assert result.exit_code == 0
    assert "parent done" in result.output


@pytest.mark.asyncio
async def test_run_safe_subprocess_does_not_hang_when_descendant_starts_new_session():
    # Harder case: descendant moves to a new session/pgid so killing the original process
    # group won't terminate it, yet it still inherited stdout/stderr. We should still not
    # hang during finalize.
    code = (
        "import os, signal, subprocess, sys; "
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True); "
        "print(f'descendant_pid={p.pid}'); "
        "print('parent done')"
    )

    result = await run_safe_subprocess(
        [sys.executable, "-c", code],
        timeout=5,
    )

    assert result.exit_code == 0
    assert "parent done" in result.output
    pid_line = next(
        (line for line in result.output.splitlines() if line.startswith("descendant_pid=")), None
    )
    assert pid_line is not None
    pid = int(pid_line.split("=", 1)[1])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # The implementation may choose to terminate the descendant as part of cleanup.
        pass
