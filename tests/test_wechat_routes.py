from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.integrations.ilink import ILinkError
from app.web.routes import _qr_image_data_url, router, require_admin


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
        assert resp.status_code == 200
        assert "已配置" in resp.text
        assert "扫码登录" in resp.text

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
        assert resp.status_code == 200
        assert "未配置" in resp.text

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
    async def test_returns_expired_when_no_qrcode(self):
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status")
        data = resp.json()
        assert data["status"] == "expired"

    @pytest.mark.anyio
    @patch("app.web.routes.ILinkClient")
    async def test_returns_unknown_on_ilink_error(self, MockClient):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(side_effect=ILinkError("timeout"))
        app = _make_app()
        resp = await _get(app, "/console/wechat/qr/status?qrcode=qr-1")
        data = resp.json()
        assert data["status"] == "unknown"


class TestSaveToken:
    @pytest.mark.anyio
    @patch("app.web.routes.SettingsService")
    @patch("app.web.routes.ILinkClient")
    async def test_saves_token_encrypted(self, MockClient, MockSettings):
        mock_inst = MockClient.return_value
        mock_inst.get_qrcode_status_detail = AsyncMock(
            return_value={"status": "confirmed", "bot_token": "tok-abc"}
        )
        svc = _mock_settings({})
        MockSettings.return_value = svc
        app = _make_app()
        resp = await _post_json(app, "/console/wechat/save", {"qrcode": "qr-1"})
        data = resp.json()
        assert data["ok"] is True
        svc.set.assert_called_once_with("wechat_bot_token", "tok-abc", encrypted=True)
        svc.commit.assert_called_once()

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
    @patch("app.web.routes.SettingsService")
    async def test_clears_token(self, MockSettings):
        svc = _mock_settings({"wechat_bot_token": "encrypted"})
        MockSettings.return_value = svc
        app = _make_app()
        resp = await _post_form(app, "/console/wechat/clear")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/console/wechat"
        svc.set.assert_called_once_with("wechat_bot_token", None)
        svc.commit.assert_called_once()


class TestWechatProbe:
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
        MockClient.assert_called_once_with("tok")
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
    async def test_probe_auth_error_prompts_rescan(self, MockClient, MockSettings):
        from app.integrations.ilink import ILinkAuthError

        mock_inst = MockClient.return_value
        mock_inst.get_updates = AsyncMock(side_effect=ILinkAuthError("expired"))
        MockSettings.return_value = _mock_settings({"wechat_bot_token": "tok"})
        app = _make_app()

        resp = await _post_json(app, "/console/wechat/probe", {})
        data = resp.json()

        assert data["error"] == "Token 无效或已过期，请重新扫码。"

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
        MockClient.assert_called_once_with("tok")
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
        resp = await _get(app, "/console/wechat")
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
        resp = await _get(app, "/console/wechat")
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
        assert resp.headers["location"] == "/console/wechat"

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
        assert resp.headers["location"] == "/console/wechat"
        mock_svc.reload_bot.assert_awaited_once_with("tok-abc")


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
        assert resp.headers["location"] == "/console/wechat"
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
            "wechat_error": "some error",
            "wechat_cursor_length": 99,
        })
        app = _make_app()
        resp = await _get(app, "/console/wechat/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["last_error"] == "some error"
        assert data["cursor_length"] == 99
