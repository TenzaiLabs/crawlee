from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

ATTACHABLE_TARGET_TYPES = {"page", "iframe", "worker", "service_worker", "shared_worker"}
MAX_OBSERVER_DIAGNOSTICS = 100


@dataclass(frozen=True)
class NetworkRequest:
    method: str
    url: str
    session_id: str | None
    target_type: str
    target_url: str
    request_id: str | None
    frame_id: str | None
    loader_id: str | None
    resource_type: str | None
    initiator_type: str | None
    initiator_url: str | None
    epoch: str
    observed_at: str


@dataclass(frozen=True)
class NetworkResponse:
    url: str
    status: int
    session_id: str | None
    target_type: str
    request_id: str | None
    resource_type: str | None
    mime_type: str | None
    epoch: str
    observed_at: str


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _initiator_url(initiator: dict[str, Any] | None) -> str | None:
    if not initiator:
        return None
    direct = _optional_string(initiator.get("url"))
    if direct is not None:
        return direct
    stack = initiator.get("stack")
    if not isinstance(stack, dict):
        return None
    call_frames = stack.get("callFrames")
    if not isinstance(call_frames, list):
        return None
    for frame in call_frames:
        if isinstance(frame, dict):
            frame_url = _optional_string(frame.get("url"))
            if frame_url is not None:
                return frame_url
    return None


class PassiveCDPObserver:
    """Observe browser network metadata without intercepting browser traffic."""

    def __init__(self, websocket_url: str) -> None:
        self.websocket_url = websocket_url
        self.requests: list[NetworkRequest] = []
        self.responses: list[NetworkResponse] = []
        self.diagnostics: list[dict[str, str]] = []
        self.disconnected = asyncio.Event()
        self._epoch = "startup"
        self._connection: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._sessions: dict[str, dict[str, str]] = {}
        self._setup_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    @property
    def epoch(self) -> str:
        return self._epoch

    def set_epoch(self, epoch: str) -> None:
        value = str(epoch).strip()
        if not value:
            raise ValueError("CDP observation epoch must not be empty")
        self._epoch = value

    async def start(self) -> None:
        if self._connection is not None:
            raise RuntimeError("CDP observer is already connected")
        self._connection = await connect(self.websocket_url, max_size=16 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._read_messages())
        await self._send("Target.setDiscoverTargets", {"discover": True})
        await self._send(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            },
        )
        response = await self._send("Target.getTargets")
        for target in response.get("targetInfos", []):
            if not isinstance(target, dict) or target.get("type") not in ATTACHABLE_TARGET_TYPES:
                continue
            self._track_setup_task(
                asyncio.create_task(self._attach_existing(str(target["targetId"])))
            )

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
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
            await asyncio.gather(*tuple(self._setup_tasks), return_exceptions=True)
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
        if self._reader_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        self._cancel_pending(RuntimeError("CDP observer stopped"))
        self._connection = None

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
        await self._send(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            },
            session_id=session_id,
        )
        await self._send("Network.enable", session_id=session_id)
        target = self._sessions.get(session_id, {})
        if len(self.diagnostics) < MAX_OBSERVER_DIAGNOSTICS:
            self.diagnostics.append(
                {
                    "kind": "initial-network-observation-gap",
                    "session_id": session_id,
                    "target_type": target.get("type", "unknown"),
                    "target_url": target.get("url", ""),
                    "reason": "target_was_not_paused_before_network_enable",
                }
            )

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
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
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
        failure: BaseException | None = None
        try:
            async for raw_message in self._connection:
                message = json.loads(raw_message)
                if not isinstance(message, dict):
                    continue
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
        except BaseException as exc:
            failure = exc
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            if not self._stopping:
                self.disconnected.set()
            self._cancel_pending(failure or RuntimeError("CDP observer disconnected"))

    def _cancel_pending(self, failure: BaseException) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(failure)

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
            self._track_setup_task(asyncio.create_task(self._enable_session(session_id)))
            return
        if method == "Target.detachedFromTarget":
            session_id = params.get("sessionId")
            if isinstance(session_id, str):
                self._sessions.pop(session_id, None)
            return

        session_id = _optional_string(message.get("sessionId"))
        target = self._sessions.get(session_id or "", {})
        request_id = _optional_string(params.get("requestId"))
        if method == "Network.responseReceived":
            response = params.get("response")
            if not isinstance(response, dict):
                return
            url = response.get("url")
            status = response.get("status")
            if not isinstance(url, str) or not isinstance(status, int | float):
                return
            self.responses.append(
                NetworkResponse(
                    url=url,
                    status=int(status),
                    session_id=session_id,
                    target_type=target.get("type", "unknown"),
                    request_id=request_id,
                    resource_type=_optional_string(params.get("type")),
                    mime_type=_optional_string(response.get("mimeType")),
                    epoch=self._epoch,
                    observed_at=_now(),
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
        initiator = params.get("initiator")
        initiator_dict = initiator if isinstance(initiator, dict) else None
        self.requests.append(
            NetworkRequest(
                method=method_value.upper(),
                url=url,
                session_id=session_id,
                target_type=target.get("type", "unknown"),
                target_url=target.get("url", ""),
                request_id=request_id,
                frame_id=_optional_string(params.get("frameId")),
                loader_id=_optional_string(params.get("loaderId")),
                resource_type=_optional_string(params.get("type")),
                initiator_type=(
                    _optional_string(initiator_dict.get("type")) if initiator_dict else None
                ),
                initiator_url=_initiator_url(initiator_dict),
                epoch=self._epoch,
                observed_at=_now(),
            )
        )
