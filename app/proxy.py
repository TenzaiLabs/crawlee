from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass

import httpx

from .db import LOG_DIR, ensure_data_dirs
from .settings import (
    CRAWLER_PROXY_CONNECTIVITY_TIMEOUT_SECONDS,
    CRAWLER_PROXY_HEALTHCHECK_INTERVAL_SECONDS,
    CRAWLER_PROXY_START_TIMEOUT_SECONDS,
    CRAWLER_PROXY_STOP_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


@dataclass
class ProxyProcess:
    process: asyncio.subprocess.Process
    log_path: str


async def start_proxy(job_id: str) -> ProxyProcess:
    ensure_data_dirs()
    log_path = os.path.join(LOG_DIR, f"{job_id}.jsonl")
    logger.info("Starting proxify for job_id=%s log_path=%s", job_id, log_path)
    process = await asyncio.create_subprocess_exec(
        "proxify",
        "-http-addr",
        "127.0.0.1:8888",
        "-output",
        log_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    return ProxyProcess(process=process, log_path=log_path)


async def wait_for_proxy(
    proxy: ProxyProcess,
    host: str = "127.0.0.1",
    port: int = 8888,
    timeout: float = CRAWLER_PROXY_START_TIMEOUT_SECONDS,
) -> None:
    logger.debug("Waiting for proxify to become healthy at %s:%d", host, port)
    start = asyncio.get_running_loop().time()
    while True:
        if proxy.process.returncode is not None:
            stderr_bytes = await proxy.process.stderr.read() if proxy.process.stderr else b""
            detail = stderr_bytes.decode(errors="replace").strip()
            logger.warning(
                "Proxify exited early with code=%s: %s", proxy.process.returncode, detail
            )
            raise RuntimeError(
                "Proxify exited before becoming healthy "
                f"(code {proxy.process.returncode}): {detail}"
            )
        try:
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            logger.info("Proxify is healthy at %s:%d", host, port)
            return
        except OSError as err:
            if asyncio.get_running_loop().time() - start > timeout:
                logger.warning("Proxify health check timed out after %.1fs", timeout)
                raise RuntimeError("Proxify did not start listening in time") from err
            await asyncio.sleep(CRAWLER_PROXY_HEALTHCHECK_INTERVAL_SECONDS)


async def check_target_connectivity(
    target_url: str,
    proxy_url: str = "http://127.0.0.1:8888",
    timeout: float = CRAWLER_PROXY_CONNECTIVITY_TIMEOUT_SECONDS,
) -> None:
    """Verify the target is reachable through the proxy before starting work.

    Raises RuntimeError if the target cannot be reached.
    """
    logger.info("Pre-flight connectivity check target_url=%s via proxy=%s", target_url, proxy_url)
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=httpx.Timeout(timeout),
            verify=False,
        ) as client:
            response = await client.head(target_url)
            # Accept any HTTP response – even a 403/404 means the target is reachable.
            logger.info(
                "Pre-flight check passed: target_url=%s status=%d",
                target_url,
                response.status_code,
            )
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"Target {target_url} is not reachable through the proxy (timeout after {timeout}s)"
        ) from exc
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"Target {target_url} is not reachable through the proxy: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Target {target_url} connectivity check failed: {exc}") from exc


async def stop_proxy(
    proxy: ProxyProcess,
    timeout: float = CRAWLER_PROXY_STOP_TIMEOUT_SECONDS,
) -> None:
    if proxy.process.returncode is not None:
        logger.debug("Proxify already exited with return code=%s", proxy.process.returncode)
        return

    logger.info("Stopping proxify process group pid=%s", proxy.process.pid)
    try:
        os.killpg(proxy.process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(proxy.process.wait(), timeout=timeout)
        return
    except TimeoutError:
        logger.warning(
            "Proxify did not stop after SIGTERM, escalating to SIGKILL pid=%s",
            proxy.process.pid,
        )
        try:
            os.killpg(proxy.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await proxy.process.wait()
