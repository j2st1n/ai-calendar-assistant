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
    @patch("app.web.routes.SettingsService")
    async def test_get_renders_when_configured(self, MockSettings):
        MockSettings.return_value = _mock_settings({"wechat_bot_token": "encrypted-tok"})
        app = _make_app()
        resp = await _get(app, "/console/wechat")
        assert resp.status_code == 200
        assert "已配置" in resp.text
        assert "扫码登录" in resp.text

    @pytest.mark.anyio
    @patch("app.web.routes.SettingsService")
    async def test_get_renders_when_not_configured(self, MockSettings):
        MockSettings.return_value = _mock_settings({})
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
