import asyncio
import re
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.integrations.ilink import (
    ILinkError,
    _reset_qrcode_credentials_for_test,
    get_qrcode_credentials,
    remember_qrcode_credentials,
)
from app.web.routes import _qr_image_data_url, router, require_admin

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
REPO_ROOT = TEMPLATES_DIR.parents[2]


@pytest.fixture(autouse=True)
def _reset_qr_cache():
    _reset_qrcode_credentials_for_test()
    yield
    _reset_qrcode_credentials_for_test()


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
    else:
        app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(router)
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, follow_redirects=False)


async def _post_json(app: FastAPI, path: str, body: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body, follow_redirects=False)


async def _post_form(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, follow_redirects=False)


def _mock_settings(overrides: dict[str, str | None] | None = None) -> MagicMock:
    store = overrides or {}
    svc = MagicMock()
    svc.get = lambda k: store.get(k)
    svc.get_masked = lambda k: ""
    svc.set = MagicMock()
    svc.commit = MagicMock()
    return svc


def test_qr_image_data_url_returns_png_when_dependency_available():
    data_url = _qr_image_data_url("2d730f316eea2b79ecd89e043f6e632a")
    if data_url is None:
        return
    assert data_url.startswith("data:image/png;base64,")


class TestWechatPage:
    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    async def test_get_renders_when_configured(self, MockWxService):
        MockWxService.return_value.config_summary = MagicMock(return_value={
            "wechat_token_set": True,
            "wechat_token_masked": "tok-***",
            "wechat_running": False,
            "wechat_error": "",
            "wechat_cursor_length": 0,
        })
        app = _make_app()
        resp = await _get(app, "/console/wechat")
        assert resp.status_code == 307
        assert resp.headers["location"] == "/console/channels?tab=wechat"
        resp_channels = await _get(app, "/console/channels?tab=wechat")
        assert resp_channels.status_code == 200
        assert "已配置" in resp_channels.text
        assert "扫码登录" in resp_channels.text

    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    async def test_get_renders_when_not_configured(self, MockWxService):
        MockWxService.return_value.config_summary = MagicMock(return_value={
            "wechat_token_set": False,
            "wechat_token_masked": "",
            "wechat_running": False,
            "wechat_error": "",
            "wechat_cursor_length": 0,
        })
        app = _make_app()
        resp = await _get(app, "/console/wechat")
        assert resp.status_code == 307
        assert resp.headers["location"] == "/console/channels?tab=wechat"
        resp_channels = await _get(app, "/console/channels?tab=wechat")
        assert resp_channels.status_code == 200
        assert "未配置" in resp_channels.text

    @pytest.mark.anyio
    async def test_get_redirects_without_auth(self):
        app = _make_app(require_auth=False)
        resp = await _get(app, "/console/wechat")
        assert resp.status_code in (303, 307)


class TestFetchQR:
    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_returns_qr_payload(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_detail = AsyncMock(return_value={"qrcode": "abc123", "qrcode_img_content": "https://weixin.qq.com/q/abc123"})
        app = _make_app()
        resp = await _post_json(app, "/console/wechat/qr", {})
        assert resp.status_code == 200
        data = resp.json()
        assert data["qr_payload"] == "https://weixin.qq.com/q/abc123"
        assert data["qrcode"] == "abc123"
        assert "qr_image" in data
        MockClient.assert_called_once_with()

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_returns_error_on_ilink_error(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_detail = AsyncMock(side_effect=ILinkError("network down"))
        app = _make_app()
        resp = await _post_json(app, "/console/wechat/qr", {})
        data = resp.json()
        assert "error" in data
        assert "network down" in data["error"]


class TestQRStatus:
    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_returns_status_pending(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(return_value={"status": "pending"})
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        data = resp.json()
        assert data["status"] == "pending"
        assert data["has_token"] is False
        assert data["qrcode"] == "qr-1"

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_returns_has_token_true_when_token_present(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(
            return_value={"status": "confirmed", "bot_token": "tok-xyz"}
        )
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        data = resp.json()
        assert data["status"] == "confirmed"
        assert data["has_token"] is True

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_confirmed_response_never_leaks_bot_token(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(
            return_value={
                "status": "confirmed",
                "bot_token": "tok-synthetic-secret",
                "ilink_bot_id": "bot-synthetic",
                "baseurl": "https://redirect.example.test",
                "ilink_user_id": "user-synthetic",
            }
        )
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        assert "tok-synthetic-secret" not in resp.text
        assert "bot-synthetic" not in resp.text
        assert "user-synthetic" not in resp.text
        data = resp.json()
        assert data["has_token"] is True
        # The confirmed payload is cached for the save route.
        assert get_qrcode_credentials("qr-1")["bot_token"] == "tok-synthetic-secret"  # type: ignore[index]

    @pytest.mark.anyio
    async def test_returns_expired_when_no_qrcode(self):
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status")
        data = resp.json()
        assert data["status"] == "expired"

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_long_poll_timeout_is_wait_not_unknown(self, MockClient):
        # The client maps ReadTimeout to status=wait; the route must surface it.
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(
            return_value={"ret": 0, "status": "wait", "transient_error": "timeout"}
        )
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        data = resp.json()
        assert data["status"] == "wait"

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_ilink_error_is_wait_not_unknown(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(side_effect=ILinkError("timeout"))
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        data = resp.json()
        assert data["status"] == "wait"
        assert data["has_token"] is False

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_status_without_status_field_is_wait(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(return_value={"ret": 0})
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        assert resp.json()["status"] == "wait"

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_verify_code_and_redirect_host_are_forwarded(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(return_value={"status": "wait"})
        app = _make_app()
        resp = await _get(
            app,
            "/console/wechat/qr/status?qrcode=qr-1&verify_code=1234&redirect_host=redirect.example.test",
        )
        assert resp.status_code == 200
        mock_inst.get_qrcode_status_detail.assert_awaited_once_with(
            "qr-1", verify_code="1234", redirect_host="redirect.example.test"
        )

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_scaned_but_redirect_exposes_redirect_host_only(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(
            return_value={"status": "scaned_but_redirect", "redirect_host": "redirect.example.test", "bot_token": "tok-hidden"}
        )
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        data = resp.json()
        assert data["status"] == "scaned_but_redirect"
        assert data["redirect_host"] == "redirect.example.test"
        assert "tok-hidden" not in resp.text

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_legacy_scanned_spelling_is_folded_to_scaned(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(return_value={"status": "scanned"})
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        assert resp.json()["status"] == "scaned"

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_need_verifycode_is_flagged(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(return_value={"status": "need_verifycode"})
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        data = resp.json()
        assert data["status"] == "need_verifycode"
        assert data["need_verifycode"] is True


class TestSaveToken:
    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    @patch("app.web.routes.SettingsService")
    @patch("app.web.routes.ILinkClient")
    async def test_saves_token_encrypted_and_reloads_runtime(self, MockClient, MockSettings, MockWxService):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(
            return_value={"status": "confirmed", "bot_token": "tok-abc"}
        )
        svc = _mock_settings({})
        MockSettings.return_value = svc
        mock_wx = MockWxService.return_value
        mock_wx.reload_bot = AsyncMock(return_value="started")
        app = _make_app()
        resp = await _post_json(app, "/console/wechat/save", {"qrcode": "qr-1"})
        data = resp.json()
        assert data["ok"] is True
        assert data["base_url_saved"] is False
        assert "tok-abc" not in resp.text
        svc.set.assert_any_call("wechat_bot_token", "tok-abc", encrypted=True)
        svc.set.assert_any_call("wechat_bot_baseurl", None)
        svc.set.assert_any_call("wechat_ilink_bot_id", None)
        svc.set.assert_any_call("wechat_ilink_user_id", None)
        svc.set.assert_any_call("wechat_updates_buf", None)
        svc.commit.assert_called_once()
        mock_wx.reload_bot.assert_awaited_once_with("tok-abc", base_url="")

    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    @patch("app.web.routes.SettingsService")
    @patch("app.web.routes.ILinkClient")
    async def test_saves_baseurl_and_identity_and_starts_runtime_with_it(
        self, MockClient, MockSettings, MockWxService
    ):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(
            return_value={
                "status": "confirmed",
                "bot_token": "tok-abc",
                "ilink_bot_id": "bot-synthetic",
                "baseurl": "https://redirect.example.test",
                "ilink_user_id": "user-synthetic",
            }
        )
        svc = _mock_settings({})
        MockSettings.return_value = svc
        mock_wx = MockWxService.return_value
        mock_wx.reload_bot = AsyncMock(return_value="started")
        app = _make_app()
        resp = await _post_json(app, "/console/wechat/save", {"qrcode": "qr-1"})
        data = resp.json()
        assert data["ok"] is True
        assert data["base_url_saved"] is True
        assert "bot-synthetic" not in resp.text
        assert "user-synthetic" not in resp.text
        svc.set.assert_any_call("wechat_bot_baseurl", "https://redirect.example.test")
        svc.set.assert_any_call("wechat_ilink_bot_id", "bot-synthetic")
        svc.set.assert_any_call("wechat_ilink_user_id", "user-synthetic")
        svc.set.assert_any_call("wechat_updates_buf", None)
        mock_wx.reload_bot.assert_awaited_once_with("tok-abc", base_url="https://redirect.example.test")

    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    @patch("app.web.routes.SettingsService")
    @patch("app.web.routes.ILinkClient")
    async def test_save_prefers_cached_confirmed_payload(self, MockClient, MockSettings, MockWxService):
        # A confirmed status poll cached the credentials; saving must not trigger
        # a second 35s long poll (which could answer binded_redirect).
        remember_qrcode_credentials(
            "qr-cached",
            {
                "status": "confirmed",
                "bot_token": "tok-cached",
                "baseurl": "https://redirect.example.test",
                "ilink_bot_id": "bot-synthetic",
                "ilink_user_id": "user-synthetic",
            },
        )
        svc = _mock_settings({})
        MockSettings.return_value = svc
        mock_wx = MockWxService.return_value
        mock_wx.reload_bot = AsyncMock(return_value="started")
        app = _make_app()
        resp = await _post_json(app, "/console/wechat/save", {"qrcode": "qr-cached"})
        assert resp.json()["ok"] is True
        MockClient.assert_not_called()
        svc.set.assert_any_call("wechat_bot_token", "tok-cached", encrypted=True)
        mock_wx.reload_bot.assert_awaited_once_with(
            "tok-cached", base_url="https://redirect.example.test"
        )

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_returns_error_when_no_token_in_response(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(return_value={"status": "pending"})
        app = _make_app()
        resp = await _post_json(app, "/console/wechat/save", {"qrcode": "qr-1"})
        data = resp.json()
        assert "error" in data
        assert "Bot Token" in data["error"]

    @pytest.mark.anyio
    async def test_returns_error_when_no_qrcode(self):
        app = _make_app()
        resp = await _post_json(app, "/console/wechat/save", {})
        data = resp.json()
        assert "error" in data

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_returns_error_on_ilink_error(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(side_effect=ILinkError("bad request"))
        app = _make_app()
        resp = await _post_json(app, "/console/wechat/save", {"qrcode": "qr-1"})
        data = resp.json()
        assert "error" in data
        assert "bad request" in data["error"]


class TestClearToken:
    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    @patch("app.web.routes.SettingsService")
    async def test_clears_token_stops_runtime_and_cursor(self, MockSettings, MockWxService):
        svc = _mock_settings({"wechat_bot_token": "encrypted"})
        MockSettings.return_value = svc
        mock_wx = MockWxService.return_value
        mock_wx.stop_bot = AsyncMock()
        app = _make_app()
        resp = await _post_form(app, "/console/wechat/clear")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/console/channels?tab=wechat"
        mock_wx.stop_bot.assert_awaited_once()
        svc.set.assert_any_call("wechat_bot_token", None)
        svc.set.assert_any_call("wechat_bot_baseurl", None)
        svc.set.assert_any_call("wechat_ilink_bot_id", None)
        svc.set.assert_any_call("wechat_ilink_user_id", None)
        svc.set.assert_any_call("wechat_updates_buf", None)
        svc.commit.assert_called_once()


class TestWechatProbe:
    @pytest.mark.anyio
    @patch("app.web.routes.get_wechat_bot_runtime")
    async def test_probe_refuses_second_poller(self, mock_get_runtime):
        runtime = MagicMock()
        runtime.task_alive = True
        mock_get_runtime.return_value = runtime
        app = _make_app()

        resp = await _post_json(app, "/console/wechat/probe", {})

        assert resp.json() == {"error": "WeChat Bot 运行中，不能启动第二个消息轮询。"}

    @pytest.mark.anyio
    @patch("app.web.routes.SettingsService")
    @patch("app.web.routes.ILinkClient")
    async def test_probe_returns_messages_and_saves_cursor(self, MockClient, MockSettings):
        mock_inst = MockClient.return_value
        mock_inst.get_updates = AsyncMock(return_value=([{"text": "hello"}], "next-cursor"))
        svc = _mock_settings({"wechat_bot_token": "tok", "wechat_updates_buf": "old-cursor"})
        MockSettings.return_value = svc
        app = _make_app()

        resp = await _post_json(app, "/console/wechat/probe", {})
        data = resp.json()

        assert data["count"] == 1
        assert data["cursor"] == "next-cursor"
        assert data["messages"] == [{"text": "hello"}]
        MockClient.assert_called_once_with("tok", base_url="")
        mock_inst.get_updates.assert_awaited_once_with("old-cursor")
        svc.set.assert_called_once_with("wechat_updates_buf", "next-cursor")
        svc.commit.assert_called_once()

    @pytest.mark.anyio
    @patch("app.web.routes.SettingsService")
    async def test_probe_fails_without_token(self, MockSettings):
        MockSettings.return_value = _mock_settings({})
        app = _make_app()

        resp = await _post_json(app, "/console/wechat/probe", {})
        data = resp.json()

        assert "error" in data
        assert "Token 未配置" in data["error"]

    @pytest.mark.anyio
    @patch("app.web.routes.SettingsService")
    @patch("app.web.routes.ILinkClient")
    async def test_probe_auth_error_does_not_invent_rescan_instruction(self, MockClient, MockSettings):
        from app.integrations.ilink import ILinkAuthError

        mock_inst = MockClient.return_value
        mock_inst.get_updates = AsyncMock(side_effect=ILinkAuthError("expired"))
        MockSettings.return_value = _mock_settings({"wechat_bot_token": "tok"})
        app = _make_app()

        resp = await _post_json(app, "/console/wechat/probe", {})
        data = resp.json()

        assert data["error"] == "expired"

    @pytest.mark.anyio
    @patch("app.web.routes.SettingsService")
    @patch("app.web.routes.ILinkClient")
    async def test_probe_sanitizes_html_in_messages(self, MockClient, MockSettings):
        mock_inst = MockClient.return_value
        mock_inst.get_updates = AsyncMock(return_value=([{"text": "<script>alert('x')</script>hello"}], "next"))
        MockSettings.return_value = _mock_settings({"wechat_bot_token": "tok"})
        app = _make_app()

        resp = await _post_json(app, "/console/wechat/probe", {})
        message = resp.json()["messages"][0]

        assert message["text"] == "scriptalert(x)/scripthello"

    @pytest.mark.anyio
    @patch("app.web.routes.SettingsService")
    async def test_clear_probe_cursor(self, MockSettings):
        svc = _mock_settings({"wechat_updates_buf": "old"})
        MockSettings.return_value = svc
        app = _make_app()

        resp = await _post_json(app, "/console/wechat/probe/clear-cursor", {})
        data = resp.json()

        assert data["ok"] is True
        svc.set.assert_called_once_with("wechat_updates_buf", None)
        svc.commit.assert_called_once()


class TestWechatProbeProcess:
    @pytest.mark.anyio
    @patch("app.web.routes.dispatch_wechat_message")
    @patch("app.web.routes.SettingsService")
    @patch("app.web.routes.ILinkClient")
    async def test_process_probe_dispatches_messages_and_saves_cursor(self, MockClient, MockSettings, mock_dispatch):
        mock_inst = MockClient.return_value
        message = {"message_id": 1, "from_user_id": "u", "context_token": "ctx", "item_list": []}
        mock_inst.get_updates = AsyncMock(return_value=([message], "next"))
        mock_dispatch.return_value = [{"text": "ok", "record_id": None, "bot_message_id": "bot"}]
        svc = _mock_settings({"wechat_bot_token": "tok", "wechat_updates_buf": "old"})
        MockSettings.return_value = svc
        app = _make_app()

        resp = await _post_json(app, "/console/wechat/probe/process", {})
        data = resp.json()

        assert data["count"] == 1
        assert data["processed"][0]["message_id"] == "1"
        MockClient.assert_called_once_with("tok", base_url="")
        mock_inst.get_updates.assert_awaited_once_with("old")
        mock_dispatch.assert_awaited_once()
        assert mock_dispatch.await_args is not None
        assert mock_dispatch.await_args.args[0] == message
        assert mock_dispatch.await_args.args[2] == mock_inst
        svc.set.assert_called_once_with("wechat_updates_buf", "next")
        svc.commit.assert_called_once()

    @pytest.mark.anyio
    @patch("app.web.routes.SettingsService")
    async def test_process_probe_fails_without_token(self, MockSettings):
        MockSettings.return_value = _mock_settings({})
        app = _make_app()

        resp = await _post_json(app, "/console/wechat/probe/process", {})
        data = resp.json()

        assert "error" in data
        assert "Token 未配置" in data["error"]


# ---------------------------------------------------------------------------
# Runtime status on page
# ---------------------------------------------------------------------------

class TestWechatPageRuntime:
    @pytest.mark.anyio
    @patch("app.web.routes.get_wechat_bot_runtime")
    @patch("app.web.routes.WechatService")
    async def test_page_shows_running_when_runtime_active(self, MockWxService, mock_get_rt):
        mock_rt = MagicMock()
        mock_rt.running = True
        mock_rt.last_error = ""
        mock_get_rt.return_value = mock_rt
        MockWxService.return_value.config_summary = MagicMock(return_value={
            "wechat_token_set": True,
            "wechat_token_masked": "tok-***",
            "wechat_running": True,
            "wechat_error": "",
            "wechat_cursor_length": 42,
        })
        app = _make_app()
        resp = await _get(app, "/console/channels?tab=wechat")
        assert resp.status_code == 200
        assert "运行中" in resp.text
        assert "已停止" not in resp.text

    @pytest.mark.anyio
    @patch("app.web.routes.get_wechat_bot_runtime")
    @patch("app.web.routes.WechatService")
    async def test_page_shows_stopped_when_runtime_inactive(self, MockWxService, mock_get_rt):
        mock_rt = MagicMock()
        mock_rt.running = False
        mock_get_rt.return_value = mock_rt
        MockWxService.return_value.config_summary = MagicMock(return_value={
            "wechat_token_set": True,
            "wechat_token_masked": "tok-***",
            "wechat_running": False,
            "wechat_error": "auth failed",
            "wechat_cursor_length": 0,
        })
        app = _make_app()
        resp = await _get(app, "/console/channels?tab=wechat")
        assert resp.status_code == 200
        assert "已停止" in resp.text
        assert "auth failed" in resp.text
        assert "启动 Bot" in resp.text


# ---------------------------------------------------------------------------
# POST /wechat/start
# ---------------------------------------------------------------------------

class TestStartWechat:
    @pytest.mark.anyio
    @patch("app.web.routes.SettingsService")
    async def test_start_without_token_flashes_error(self, MockSettings):
        MockSettings.return_value = _mock_settings({})
        app = _make_app()
        resp = await _post_form(app, "/console/wechat/start")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/console/channels?tab=wechat"

    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    @patch("app.web.routes.SettingsService")
    async def test_start_with_token_calls_reload(self, MockSettings, MockWxService):
        MockSettings.return_value = _mock_settings({"wechat_bot_token": "tok-abc"})
        mock_svc = MockWxService.return_value
        mock_svc.reload_bot = AsyncMock(return_value="started")
        app = _make_app()
        resp = await _post_form(app, "/console/wechat/start")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/console/channels?tab=wechat"
        mock_svc.reload_bot.assert_awaited_once_with("tok-abc", base_url="")


# ---------------------------------------------------------------------------
# POST /wechat/stop
# ---------------------------------------------------------------------------

class TestStopWechat:
    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    async def test_stop_calls_stop_bot(self, MockWxService):
        mock_svc = MockWxService.return_value
        mock_svc.stop_bot = AsyncMock()
        app = _make_app()
        resp = await _post_form(app, "/console/wechat/stop")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/console/channels?tab=wechat"
        mock_svc.stop_bot.assert_awaited_once()


# ---------------------------------------------------------------------------
# GET /wechat/status
# ---------------------------------------------------------------------------

class TestWechatStatusJSON:
    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    async def test_status_returns_json_with_runtime_fields(self, MockWxService):
        MockWxService.return_value.config_summary = MagicMock(return_value={
            "wechat_token_set": True,
            "wechat_token_masked": "tok-***",
            "wechat_running": True,
            "wechat_task_alive": True,
            "wechat_state": "retrying",
            "wechat_state_label": "异常，自动恢复中",
            "wechat_error": "some error",
            "wechat_current_error": {"category": "tcp", "message": "some error"},
            "wechat_last_error": {"category": "tcp", "message": "some error"},
            "wechat_consecutive_failures": 2,
            "wechat_last_poll_success_at": 123.0,
            "wechat_last_message_at": 120.0,
            "wechat_next_retry_at": 130.0,
            "wechat_pause_until": None,
            "wechat_cursor_length": 99,
        })
        app = _make_app()
        resp = await _get(app, "/console/wechat/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["state"] == "retrying"
        assert data["task_alive"] is True
        assert data["current_error"]["category"] == "tcp"
        assert data["consecutive_failures"] == 2
        assert data["cursor_length"] == 99


# ---------------------------------------------------------------------------
# Template / route consistency for the QR login flow
# ---------------------------------------------------------------------------

def _template_text(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _registered_console_paths() -> set[str]:
    return {str(getattr(route, "path", "") or "") for route in router.routes}


def test_channels_template_polls_the_registered_qr_status_route():
    template = _template_text("channels.html")
    # The console must poll the route the backend actually serves.
    assert "/console/wechat/qr/status" in template
    assert "/qr/poll" not in template
    assert "wechatQrTimer" not in template
    assert "/console/wechat/qr/poll" not in template
    # Serial long poll: the next request is scheduled from the previous
    # fetch's completion, never from a fixed setInterval tick.
    qr_section = template.split("// 微信 QR 获取与轮询", 1)[1].split("function showError", 1)[0]
    assert "setInterval(" not in qr_section
    assert template.count("fetch(qrStatusUrl(state))") == 1
    assert "scheduleQrPoll(QR_RETRY_DELAY_MS)" in template
    assert "/console/wechat/save" in template


def test_template_console_urls_are_all_registered_routes():
    registered = _registered_console_paths()
    for name in ("channels.html", "wechat.html"):
        template = _template_text(name)
        for url in set(re.findall(r"'(/console/wechat/[a-z/]*)'", template)):
            assert url in registered, f"{name} references unregistered route {url}"


def test_channels_template_covers_the_official_qr_status_machine():
    template = _template_text("channels.html")
    for status in (
        "scaned",
        "confirmed",
        "expired",
        "need_verifycode",
        "confirmed_but_need_verifycode",
        "verify_code_blocked",
        "scaned_but_redirect",
        "binded_redirect",
    ):
        assert status in template, f"channels.html has no branch for {status}"
    # Official spelling is scaned; the old misspelling must be gone.
    assert "=== 'scanned'" not in template
    # Confirming the login persists the credentials through the save endpoint.
    assert "saveQrCredentials(state.qrcode)" in template
    # Verify code input keeps the existing layout but adds the needed control.
    assert 'id="qr-verifycode"' in template
    assert 'id="qr-verifycode-input"' in template
    assert "submitQrVerifyCode" in template
    assert "verify_code=" in template
    assert "redirect_host=" in template


def test_legacy_wechat_template_matches_fixed_polling():
    template = _template_text("wechat.html")
    assert "/console/wechat/qr/poll" not in template
    assert "/console/wechat/qr/status" in template
    assert "pollTimer" not in template
    assert "schedulePoll(QR_RETRY_DELAY_MS)" in template
    for status in ("scaned", "confirmed", "expired", "need_verifycode", "verify_code_blocked", "scaned_but_redirect", "binded_redirect"):
        assert status in template
    assert "/console/wechat/save" in template


# ---------------------------------------------------------------------------
# Persisted baseurl is used by the runtime and the probe helpers
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@patch("app.web.routes.WechatService")
@patch("app.web.routes.SettingsService")
async def test_start_uses_persisted_baseurl(MockSettings, MockWxService):
    MockSettings.return_value = _mock_settings({
        "wechat_bot_token": "tok-abc",
        "wechat_bot_baseurl": "https://redirect.example.test",
    })
    mock_svc = MockWxService.return_value
    mock_svc.reload_bot = AsyncMock(return_value="started")
    app = _make_app()
    resp = await _post_form(app, "/console/wechat/start")
    assert resp.status_code == 303
    mock_svc.reload_bot.assert_awaited_once_with(
        "tok-abc", base_url="https://redirect.example.test"
    )


@pytest.mark.anyio
@patch("app.web.routes.SettingsService")
@patch("app.web.routes.ILinkClient")
async def test_probe_uses_persisted_baseurl(MockClient, MockSettings):
    mock_inst = MockClient.return_value
    mock_inst.get_updates = AsyncMock(return_value=([], "next"))
    MockSettings.return_value = _mock_settings({
        "wechat_bot_token": "tok",
        "wechat_updates_buf": "old",
        "wechat_bot_baseurl": "https://redirect.example.test",
    })
    app = _make_app()
    resp = await _post_json(app, "/console/wechat/probe", {})
    assert resp.status_code == 200
    MockClient.assert_called_once_with("tok", base_url="https://redirect.example.test")


def test_refresh_runtime_forwards_base_url(monkeypatch):
    from app.services import wechat_service as ws

    captured: dict[str, str] = {}

    async def fake_loop(token: str, *, base_url: str = "") -> None:
        captured["token"] = token
        captured["base_url"] = base_url

    async def run() -> None:
        runtime = ws.WechatBotRuntime()
        monkeypatch.setattr(runtime, "_poll_loop", fake_loop)
        await runtime.reload("tok", base_url="https://redirect.example.test")
        await asyncio.sleep(0)
        await runtime.stop()

    asyncio.run(run())
    assert captured == {"token": "tok", "base_url": "https://redirect.example.test"}


class _FakeSessionContext:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return False


class _FakeSettingsService:
    saved: dict[str, str] = {}

    def __init__(self, _session) -> None:
        pass

    def get(self, key: str) -> str:
        return self.saved.get(key, "")

    def set(self, _key: str, _value) -> None:
        pass

    def commit(self) -> None:
        pass


def test_poll_loop_builds_client_with_saved_baseurl(monkeypatch):
    from app.services import wechat_service as ws

    created: dict[str, str] = {}

    class _FakeClient:
        def __init__(self, token: str, base_url: str = "") -> None:
            created["token"] = token
            created["base_url"] = base_url

        async def get_updates(self, cursor: str, timeout_ms: int = 0):
            runtime.running = False
            return ([], cursor)

        async def aclose(self) -> None:
            pass

    class _FakeStaleError(RuntimeError):
        pass

    monkeypatch.setattr(ws, "ILinkClient", _FakeClient)
    monkeypatch.setattr(ws, "ILinkError", RuntimeError)
    monkeypatch.setattr(ws, "ILinkStaleTokenError", _FakeStaleError)
    monkeypatch.setattr(ws, "ILINK_BASE", "https://default.example.test")
    monkeypatch.setattr(ws, "dispatch_wechat_message", lambda *_a, **_k: [])
    monkeypatch.setattr(ws, "SessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr(ws, "SettingsService", _FakeSettingsService)

    async def run(base_url: str) -> None:
        nonlocal runtime
        runtime = ws.WechatBotRuntime()
        runtime.running = True
        await runtime._poll_loop("tok", base_url=base_url)

    runtime = None
    asyncio.run(run("https://redirect.example.test"))
    assert created == {"token": "tok", "base_url": "https://redirect.example.test"}

    created.clear()
    _FakeSettingsService.saved = {}
    asyncio.run(run(""))
    assert created == {"token": "tok", "base_url": "https://default.example.test"}

    # Startup path (app/main.py) passes no base_url; the persisted one is used.
    created.clear()
    _FakeSettingsService.saved = {"wechat_bot_baseurl": "https://saved.example.test"}
    asyncio.run(run(""))
    assert created == {"token": "tok", "base_url": "https://saved.example.test"}
    _FakeSettingsService.saved = {}


# ---------------------------------------------------------------------------
# Repair round 2:终态提示的可见性（binded_redirect 不得写入被隐藏的 step2）
# ---------------------------------------------------------------------------

class _DivAncestors(HTMLParser):
    """Map every ``id`` on a ``<div>`` to its ancestor div ids."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str | None] = []
        self.ancestors: dict[str, tuple[str, ...]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ancestors[element_id] = tuple(item for item in self._stack if item)
        if tag == "div":
            self._stack.append(element_id)

    def handle_startendtag(self, tag: str, attrs) -> None:
        return None

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._stack:
            self._stack.pop()


def _div_ancestors(template: str) -> dict[str, tuple[str, ...]]:
    parser = _DivAncestors()
    parser.feed(template)
    parser.close()
    return parser.ancestors


def _js_status_branch(template: str, condition: str) -> str:
    """Return one ``if (status === ...)`` block body, up to the next branch."""
    start = template.index(condition)
    remainder = template[start:]
    for stop in (
        "\n    if (status === ",
        "\n    state.waits += 1;",
        "\n    // wait",
    ):
        index = remainder.find(stop)
        if index != -1:
            remainder = remainder[:index]
    return remainder


def test_notice_region_lives_outside_the_hidden_qr_step2():
    # #qr-status is inside #qr-step-2, which the terminal branches hide; the
    # binded_redirect hint must therefore live in a separate visible region.
    for name in ("channels.html", "wechat.html"):
        template = _template_text(name)
        ancestors = _div_ancestors(template)
        assert "qr-status" in ancestors, name
        assert "qr-step-2" in ancestors["qr-status"], name
        assert "qr-notice" in ancestors, name
        assert "qr-step-2" not in ancestors["qr-notice"], name
        assert "qr-notice-msg" in ancestors, name
        assert "qr-step-2" not in ancestors["qr-notice-msg"], name


def test_binded_redirect_hint_is_written_to_the_visible_notice():
    for name in ("channels.html", "wechat.html"):
        template = _template_text(name)
        branch = _js_status_branch(template, "if (status === 'binded_redirect')")
        assert "showQrNotice(" in branch, name
        assert "该微信号已绑定到此服务" in branch, name
        # Writing into the hidden status area would make the hint invisible.
        assert "setQrStatus(" not in branch, name


def test_expired_and_blocked_return_to_fetch_qr_button():
    for name in ("channels.html", "wechat.html"):
        template = _template_text(name)
        for status in ("expired", "verify_code_blocked"):
            branch = _js_status_branch(template, f"if (status === '{status}')")
            assert "showQrStepsIdle(" in branch, (name, status)
        match = re.search(r"function showQrStepsIdle\(\) \{(.*?)\n  \}", template, re.S)
        assert match is not None, name
        body = match.group(1)
        assert "step1.style.display = ''" in body, name
        assert "step2.style.display = 'none'" in body, name


def test_docs_document_manual_recode_for_expired_and_blocked():
    doc = (REPO_ROOT / "docs" / "wechat_qr_login.md").read_text(encoding="utf-8")
    assert "手动重新取码" in doc
    assert "不实现" in doc
    assert "bot_spam_ticket" in doc
    assert "60 秒" in doc
    for status in ("`expired`", "`verify_code_blocked`"):
        row = next(
            line for line in doc.splitlines() if line.startswith(f"| {status} ")
        )
        assert "手动重新取码" in row, status
