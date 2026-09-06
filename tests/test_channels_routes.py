"""Tests for consolidated /console/channels view, 307 redirects, and sidebar convergence."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from app.web.routes import router, require_admin


class _FakeSessionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope["session"] = {"admin_authenticated": True}
        await self.app(scope, receive, send)


def _make_app(require_auth: bool = True) -> FastAPI:
    app = FastAPI()
    if require_auth:
        app.dependency_overrides[require_admin] = lambda: None
        app.add_middleware(_FakeSessionMiddleware)
    app.include_router(router)
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, follow_redirects=False)


async def _post_form(app: FastAPI, path: str, data: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, data=data or {}, follow_redirects=False)


def _mock_settings(overrides: dict[str, str | None] | None = None) -> MagicMock:
    store = overrides or {}
    svc = MagicMock()
    svc.get = lambda k: store.get(k)
    svc.get_masked = lambda k: ""
    svc.set = MagicMock()
    svc.commit = MagicMock()
    return svc


class TestChannelsPage:
    @pytest.mark.anyio
    @patch("app.web.routes.DiscordService")
    @patch("app.web.routes.TelegramService")
    @patch("app.web.routes.WechatService")
    async def test_channels_renders_default_tab(self, MockWx, MockTg, MockDc):
        MockWx.return_value.config_summary = MagicMock(return_value={
            "wechat_token_set": True,
            "wechat_token_masked": "tok-***",
            "wechat_running": False,
            "wechat_error": "",
            "wechat_cursor_length": 0,
        })
        MockTg.return_value.config_summary = MagicMock(return_value={
            "bot_token_set": False,
            "bot_token_masked": "",
            "bot_username": "",
            "bot_running": False,
            "bot_error": "",
            "allowed_users": [],
            "rejected_users": [],
        })
        MockDc.return_value.config_summary = MagicMock(return_value={
            "discord_token_set": False,
            "discord_token_masked": "",
            "discord_bot_running": False,
            "discord_bot_error": "",
            "discord_application_id": "",
            "discord_allowed_users": [],
        })

        app = _make_app()
        resp = await _get(app, "/console/channels")
        assert resp.status_code == 200
        # Check title & components
        assert "消息渠道设置" in resp.text
        assert 'id="pill-wechat"' in resp.text
        assert 'id="pill-telegram"' in resp.text
        assert 'id="pill-discord"' in resp.text
        assert 'id="tab-btn-wechat"' in resp.text
        assert 'id="tab-btn-telegram"' in resp.text
        assert 'id="tab-btn-discord"' in resp.text
        assert 'id="panel-wechat"' in resp.text
        assert 'id="panel-telegram"' in resp.text
        assert 'id="panel-discord"' in resp.text
        # Default active tab is wechat
        assert 'id="tab-btn-wechat" aria-controls="panel-wechat" aria-selected="true"' in resp.text
        # Lifecycle timers
        assert "startWechatStatusPolling" in resp.text
        assert "stopWechatStatusPolling" in resp.text
        assert "switchChannelTab" in resp.text

    @pytest.mark.anyio
    @patch("app.web.routes.DiscordService")
    @patch("app.web.routes.TelegramService")
    @patch("app.web.routes.WechatService")
    async def test_channels_renders_specific_tab(self, MockWx, MockTg, MockDc):
        MockWx.return_value.config_summary = MagicMock(return_value={"wechat_token_set": False})
        MockTg.return_value.config_summary = MagicMock(return_value={
            "bot_token_set": True,
            "bot_token_masked": "tg-***",
            "bot_username": "my_bot",
            "bot_running": True,
            "bot_error": "",
            "allowed_users": [{"id": 1, "user_id": "1001", "username": "user1"}],
            "rejected_users": [],
        })
        MockDc.return_value.config_summary = MagicMock(return_value={"discord_token_set": False, "discord_allowed_users": []})

        app = _make_app()
        resp = await _get(app, "/console/channels?tab=telegram")
        assert resp.status_code == 200
        assert 'id="tab-btn-telegram" aria-controls="panel-telegram" aria-selected="true"' in resp.text
        assert "Telegram：" in resp.text
        assert "my_bot" in resp.text

        resp_dc = await _get(app, "/console/channels?tab=discord")
        assert resp_dc.status_code == 200
        assert 'id="tab-btn-discord" aria-controls="panel-discord" aria-selected="true"' in resp_dc.text


class TestLegacyRoutesRedirect:
    @pytest.mark.anyio
    async def test_wechat_redirects_307(self):
        app = _make_app()
        resp = await _get(app, "/console/wechat")
        assert resp.status_code == 307
        assert resp.headers["location"] == "/console/channels?tab=wechat"

    @pytest.mark.anyio
    async def test_wechat_redirects_307_preserves_query(self):
        app = _make_app()
        resp = await _get(app, "/console/wechat?message=test_flash&error=none")
        assert resp.status_code == 307
        location = resp.headers["location"]
        assert location.startswith("/console/channels?")
        assert "tab=wechat" in location
        assert "message=test_flash" in location

    @pytest.mark.anyio
    async def test_telegram_redirects_307(self):
        app = _make_app()
        resp = await _get(app, "/console/telegram")
        assert resp.status_code == 307
        assert resp.headers["location"] == "/console/channels?tab=telegram"

    @pytest.mark.anyio
    async def test_telegram_redirects_307_preserves_bind_params(self):
        app = _make_app()
        resp = await _get(app, "/console/telegram?bind_token=tok123&bind_link=https://t.me/bot")
        assert resp.status_code == 307
        location = resp.headers["location"]
        assert location.startswith("/console/channels?")
        assert "tab=telegram" in location
        assert "bind_token=tok123" in location

    @pytest.mark.anyio
    async def test_discord_redirects_307(self):
        app = _make_app()
        resp = await _get(app, "/console/discord")
        assert resp.status_code == 307
        assert resp.headers["location"] == "/console/channels?tab=discord"


class TestPostActionsRedirectToChannels:
    @pytest.mark.anyio
    @patch("app.web.routes.TelegramService")
    @patch("app.web.routes.SettingsService")
    async def test_telegram_post_redirects_to_telegram_tab(self, MockSettings, MockTgService):
        MockSettings.return_value = _mock_settings()
        mock_svc = MockTgService.return_value
        mock_svc.save_token = MagicMock()
        mock_svc.reload_bot = AsyncMock(return_value=True)

        app = _make_app()
        resp = await _post_form(app, "/console/telegram", {"bot_token": "123:ABC", "bot_username": "testbot"})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/console/channels?tab=telegram"

    @pytest.mark.anyio
    @patch("app.web.routes.TelegramService")
    @patch("app.web.routes.SettingsService")
    async def test_telegram_bind_post_redirects_to_telegram_tab(self, MockSettings, MockTgService):
        MockSettings.return_value = _mock_settings({"telegram_bot_username": "testbot"})
        mock_svc = MockTgService.return_value
        mock_svc.generate_bind_link = MagicMock(return_value=("https://t.me/testbot?start=bind", "bind_token_123"))

        app = _make_app()
        resp = await _post_form(app, "/console/telegram/bind")
        assert resp.status_code == 303
        assert "/console/channels?tab=telegram" in resp.headers["location"]
        assert "bind_token=bind_token_123" in resp.headers["location"]

    @pytest.mark.anyio
    @patch("app.web.routes.DiscordService")
    @patch("app.web.routes.SettingsService")
    async def test_discord_post_redirects_to_discord_tab(self, MockSettings, MockDcService):
        MockSettings.return_value = _mock_settings()
        mock_svc = MockDcService.return_value
        mock_svc.save_token = MagicMock()
        mock_svc.reload_bot = AsyncMock(return_value=True)

        app = _make_app()
        resp = await _post_form(app, "/console/discord", {"bot_token": "discord_token_123", "application_id": "app_999"})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/console/channels?tab=discord"


class TestSidebarConvergence:
    @pytest.mark.anyio
    @patch("app.web.routes.DiscordService")
    @patch("app.web.routes.TelegramService")
    @patch("app.web.routes.WechatService")
    async def test_sidebar_has_unified_channels_item(self, MockWx, MockTg, MockDc):
        MockWx.return_value.config_summary = MagicMock(return_value={"wechat_token_set": False})
        MockTg.return_value.config_summary = MagicMock(return_value={"bot_token_set": False, "allowed_users": [], "rejected_users": []})
        MockDc.return_value.config_summary = MagicMock(return_value={"discord_token_set": False, "discord_allowed_users": []})

        app = _make_app()
        resp = await _get(app, "/console/channels")
        assert resp.status_code == 200
        # Unified menu item exists and is active
        assert '<a href="/console/channels" class="sidebar-item active">' in resp.text
        assert "消息渠道" in resp.text
        # Legacy items do NOT exist in sidebar navigation
        assert '<a href="/console/telegram" class="sidebar-item' not in resp.text
        assert '<a href="/console/discord" class="sidebar-item' not in resp.text
        assert '<a href="/console/wechat" class="sidebar-item' not in resp.text
