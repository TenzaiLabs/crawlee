from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.proxy import check_target_connectivity


@pytest.mark.asyncio
async def test_check_target_connectivity_success():
    mock_response = httpx.Response(200)
    with patch("app.proxy.httpx.AsyncClient") as mock_cls:
        ctx = AsyncMock()
        ctx.head = AsyncMock(return_value=mock_response)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await check_target_connectivity("http://example.com", timeout=5.0)
        ctx.head.assert_called_once_with("http://example.com")


@pytest.mark.asyncio
async def test_check_target_connectivity_accepts_error_status():
    mock_response = httpx.Response(403)
    with patch("app.proxy.httpx.AsyncClient") as mock_cls:
        ctx = AsyncMock()
        ctx.head = AsyncMock(return_value=mock_response)
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await check_target_connectivity("http://example.com", timeout=5.0)


@pytest.mark.asyncio
async def test_check_target_connectivity_timeout():
    with patch("app.proxy.httpx.AsyncClient") as mock_cls:
        ctx = AsyncMock()
        ctx.head = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(RuntimeError, match="not reachable.*timeout"):
            await check_target_connectivity("http://example.com", timeout=5.0)


@pytest.mark.asyncio
async def test_check_target_connectivity_connect_error():
    with patch("app.proxy.httpx.AsyncClient") as mock_cls:
        ctx = AsyncMock()
        ctx.head = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(RuntimeError, match="not reachable"):
            await check_target_connectivity("http://example.com", timeout=5.0)
