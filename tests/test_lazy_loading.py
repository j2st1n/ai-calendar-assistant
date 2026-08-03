import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.ai_provider_service import AIProviderConfig, AIProviderService


HEAVY_MODULES = (
    "aiohttp",
    "anthropic",
    "caldav",
    "cryptography",
    "discord",
    "httpx",
    "icalendar",
    "openai",
    "PIL",
    "qrcode",
    "telegram",
)


def test_unconfigured_startup_does_not_import_optional_sdks(tmp_path: Path) -> None:
    code = """
import asyncio
import sys
import app.main

asyncio.run(app.main.auto_start_bots())
heavy = [name for name in sys.argv[1:] if name in sys.modules]
if heavy:
    raise SystemExit(f"unexpected optional imports: {heavy}")
"""
    env = os.environ.copy()
    env.update(
        {
            "ADMIN_PASSWORD": "local-test-password",
            "APP_SECRET_KEY": "local-test-secret",
            "DATA_DIR": str(tmp_path),
            "DATABASE_URL": f"sqlite:///{tmp_path / 'app.db'}",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", code, *HEAVY_MODULES],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_configured_channels_are_started() -> None:
    import app.main as main
    from app.services.discord_service import DiscordService
    from app.services.telegram_service import TelegramService
    from app.services.wechat_service import WechatService

    configured = {
        "telegram_bot_token": "telegram-test-token",
        "discord_bot_token": "discord-test-token",
        "wechat_bot_token": "wechat-test-token",
    }
    settings_service = MagicMock()
    settings_service.get.side_effect = configured.get
    session_context = MagicMock()

    async def run() -> None:
        with (
            patch.object(main, "SessionLocal", return_value=session_context),
            patch.object(main, "SettingsService", return_value=settings_service),
            patch.object(TelegramService, "reload_bot", new=AsyncMock(return_value="started")) as telegram_reload,
            patch.object(DiscordService, "reload_bot", new=AsyncMock(return_value="started")) as discord_reload,
            patch.object(WechatService, "reload_bot", new=AsyncMock(return_value="started")) as wechat_reload,
        ):
            await main.auto_start_bots()

        telegram_reload.assert_awaited_once_with(configured["telegram_bot_token"])
        discord_reload.assert_awaited_once_with(configured["discord_bot_token"])
        wechat_reload.assert_awaited_once_with(configured["wechat_bot_token"])

    asyncio.run(run())


def test_first_openai_call_uses_and_closes_lazy_client(monkeypatch) -> None:
    instances = []

    class FakeCompletions:
        async def create(self, **_kwargs):
            message = SimpleNamespace(content="{\"ok\": true}")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())
            self.closed = False
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.closed = True

    import app.services.ai_provider_service as provider_module

    monkeypatch.setattr(provider_module, "_openai_sdk", lambda: (FakeClient, RuntimeError))
    config = AIProviderConfig("openai_compatible", "https://example.invalid/v1", "test", "model")

    result = asyncio.run(AIProviderService().chat_completion(config, "system", "user"))

    assert result == "{\"ok\": true}"
    assert len(instances) == 1
    assert instances[0].closed is True


def test_first_anthropic_call_uses_and_closes_lazy_client(monkeypatch) -> None:
    instances = []

    class FakeMessages:
        async def create(self, **_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="{\"ok\": true}")])

    class FakeClient:
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()
            self.closed = False
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.closed = True

    import app.services.ai_provider_service as provider_module

    monkeypatch.setattr(provider_module, "_anthropic_sdk", lambda: (FakeClient, RuntimeError))
    config = AIProviderConfig("anthropic", "https://api.anthropic.com", "test", "model")

    result = asyncio.run(AIProviderService().chat_completion(config, "system", "user"))

    assert result == "{\"ok\": true}"
    assert len(instances) == 1
    assert instances[0].closed is True


def test_telegram_reload_bot_creates_and_reuses_runtime() -> None:
    import app.services.telegram_service as telegram_module

    original = telegram_module._runtime

    async def run() -> None:
        telegram_module._runtime = None
        runtime = MagicMock()
        runtime.reload = AsyncMock(side_effect=["started", "reloaded"])
        with patch.object(telegram_module, "TelegramBotRuntime", return_value=runtime) as runtime_factory:
            service = telegram_module.TelegramService()
            assert await service.reload_bot("first") == "started"
            assert await service.reload_bot("second") == "reloaded"

        runtime_factory.assert_called_once()
        assert runtime.reload.await_args_list[0].args == ("first",)
        assert runtime.reload.await_args_list[1].args == ("second",)

    try:
        asyncio.run(run())
    finally:
        telegram_module._runtime = original


def test_discord_reload_bot_creates_and_reuses_runtime() -> None:
    import app.services.discord_service as discord_module

    original = discord_module._discord_runtime

    async def run() -> None:
        discord_module._discord_runtime = None
        runtime = MagicMock()
        runtime.reload = AsyncMock(side_effect=["started", "reloaded"])
        with patch.object(discord_module, "DiscordBotRuntime", return_value=runtime) as runtime_factory:
            service = discord_module.DiscordService()
            assert await service.reload_bot("first") == "started"
            assert await service.reload_bot("second") == "reloaded"

        runtime_factory.assert_called_once()
        assert runtime.reload.await_args_list[0].args == ("first",)
        assert runtime.reload.await_args_list[1].args == ("second",)

    try:
        asyncio.run(run())
    finally:
        discord_module._discord_runtime = original
