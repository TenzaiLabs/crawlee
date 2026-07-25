from __future__ import annotations

import asyncio
import sys
import time

import pytest

from app import process
from app.process import run_safe_subprocess


@pytest.mark.asyncio
async def test_process_memory_budget_kills_registered_groups_at_shared_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(process, "_process_tree_rss_bytes", lambda roots: 101)
    monkeypatch.setattr(
        process.os,
        "killpg",
        lambda pid, signal_number: killed.append((pid, signal_number)),
    )
    budget = process.ProcessMemoryBudget(100, poll_interval_seconds=0.001)
    budget.register(424242)

    await budget.start()
    await asyncio.wait_for(budget.exceeded.wait(), timeout=1)
    await budget.stop()

    assert budget.observed_bytes == 101
    assert killed == [(424242, process.signal.SIGKILL)]
    with pytest.raises(process.ProcessMemoryLimitExceeded, match="memory ceiling exceeded"):
        budget.raise_if_exceeded()


@pytest.mark.asyncio
async def test_run_safe_subprocess_captures_output():
    result = await run_safe_subprocess(
        [sys.executable, "-c", "print('hello')"],
        timeout=5,
    )

    assert result.exit_code == 0
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_run_safe_subprocess_retains_only_bounded_diagnostic_tail():
    result = await run_safe_subprocess(
        [sys.executable, "-c", "print('x' * 200_000 + 'tail-marker')"],
        timeout=5,
        diagnostic_tail_bytes=1024,
    )

    assert result.exit_code == 0
    assert len(result.output.encode("utf-8")) <= 1024
    assert result.output.rstrip().endswith("tail-marker")


@pytest.mark.asyncio
async def test_run_safe_subprocess_can_disable_diagnostic_capture_while_streaming():
    streamed: list[str] = []

    async def on_output(line: str) -> None:
        streamed.append(line)

    result = await run_safe_subprocess(
        [sys.executable, "-c", "print('streamed')"],
        timeout=5,
        on_output=on_output,
        diagnostic_tail_bytes=0,
    )

    assert streamed == ["streamed\n"]
    assert result.output == ""


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
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'], "
        "start_new_session=True); "
        "print(f'descendant_pid={p.pid}'); "
        "print('parent done')"
    )

    started = time.monotonic()
    result = await run_safe_subprocess(
        [sys.executable, "-c", code],
        timeout=5,
    )

    assert time.monotonic() - started < 4
    assert result.exit_code == 0
    assert "parent done" in result.output
    assert "descendant_pid=" in result.output
    # Do not signal a PID from a nested test sandbox: PID namespace translation can
    # identify the test runner rather than the child. The short-lived child exits here.
    await asyncio.sleep(3)
