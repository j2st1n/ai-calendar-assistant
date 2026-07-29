import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import NetworkError, RetryAfter

from app.services.telegram_service import _retry_telegram_network


def test_telegram_network_call_returns_without_retry():
    async def run():
        operation = AsyncMock(return_value="ok")
        with patch("app.services.telegram_service.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await _retry_telegram_network(operation, "test")
        assert result == "ok"
        operation.assert_awaited_once()
        sleep.assert_not_awaited()

    asyncio.run(run())


def test_telegram_network_call_retries_with_exponential_backoff():
    async def run():
        operation = AsyncMock(
            side_effect=[NetworkError("temporary"), NetworkError("temporary"), "ok"]
        )
        with patch("app.services.telegram_service.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await _retry_telegram_network(operation, "test")
        assert result == "ok"
        assert operation.await_count == 3
        assert [call.args[0] for call in sleep.await_args_list] == [2.0, 4.0]

    asyncio.run(run())


def test_telegram_retry_after_uses_server_delay():
    async def run():
        operation = AsyncMock(side_effect=[RetryAfter(7), "ok"])
        with patch("app.services.telegram_service.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await _retry_telegram_network(operation, "test")
        assert result == "ok"
        sleep.assert_awaited_once_with(7.0)

    asyncio.run(run())


def test_telegram_network_call_raises_after_final_attempt():
    async def run():
        operation = AsyncMock(side_effect=NetworkError("still unavailable"))
        with patch("app.services.telegram_service.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(NetworkError):
                await _retry_telegram_network(operation, "test")
        assert operation.await_count == 5
        assert [call.args[0] for call in sleep.await_args_list] == [2.0, 4.0, 8.0, 16.0]

    asyncio.run(run())
