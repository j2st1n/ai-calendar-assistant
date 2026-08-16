import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.integrations.ilink import (
    BOT_AGENT,
    CHANNEL_VERSION,
    GetUpdatesResult,
    ILinkAuthError,
    ILinkClient,
    ILinkError,
    ILinkHttpError,
    ILinkStaleTokenError,
    ILinkTransportError,
    STALE_TOKEN_ERRCODE,
    _reset_session_pauses_for_test,
)


BASE_INFO = {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


@pytest.fixture(autouse=True)
def reset_session_pauses():
    _reset_session_pauses_for_test()
    yield
    _reset_session_pauses_for_test()


class FakeAsyncClient:
    requests: list[dict] = []
    response = httpx.Response(200, json={})

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, method, url, **kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        self.requests.append({"method": method, "url": url, "kwargs": kwargs, "client_kwargs": self.kwargs})
        return self.response

    async def aclose(self):
        return None


def _patch_client(monkeypatch, response):
    FakeAsyncClient.requests = []
    FakeAsyncClient.response = response
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


def test_get_qrcode_calls_expected_endpoint(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"qrcode": "qr-1", "qrcode_img_content": "https://weixin.qq.com/x/qr-1"}))
        qrcode = await ILinkClient("token", base_url="https://example.test").get_qrcode()
        req = FakeAsyncClient.requests[0]
        assert qrcode == "https://weixin.qq.com/x/qr-1"
        assert req["method"] == "POST"
        assert req["url"] == "https://example.test/ilink/bot/get_bot_qrcode"
        assert req["kwargs"]["params"] == {"bot_type": "3"}
        assert req["kwargs"]["json"] == {"local_token_list": []}
        assert req["kwargs"]["headers"]["Authorization"] == "Bearer token"
        assert req["kwargs"]["headers"]["AuthorizationType"] == "ilink_bot_token"
        assert req["kwargs"]["headers"]["X-WECHAT-UIN"]

    asyncio.run(run())


def test_get_qrcode_status_returns_status(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"status": "pending"}))
        status = await ILinkClient("token", base_url="https://example.test").get_qrcode_status("qr-1")
        req = FakeAsyncClient.requests[0]
        assert status == "pending"
        assert req["method"] == "GET"
        assert req["url"].endswith("/ilink/bot/get_qrcode_status")
        assert req["kwargs"]["params"] == {"qrcode": "qr-1"}

    asyncio.run(run())


def test_get_updates_posts_cursor_and_returns_new_cursor(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"msgs": [{"id": "m1"}], "get_updates_buf": "next"}))
        result = await ILinkClient("token", base_url="https://example.test").get_updates("old")
        req = FakeAsyncClient.requests[0]
        assert isinstance(result, GetUpdatesResult)
        assert result.messages == [{"id": "m1"}]
        assert result.cursor == "next"
        assert req["method"] == "POST"
        assert req["url"].endswith("/ilink/bot/getupdates")
        assert req["kwargs"]["json"] == {
            "get_updates_buf": "old",
            "base_info": BASE_INFO,
        }
        assert req["kwargs"]["timeout"] == 35.0

    asyncio.run(run())


def test_get_updates_preserves_cursor_when_response_omits_it(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"msgs": []}))
        result = await ILinkClient("token", base_url="https://example.test").get_updates("old")
        assert result.messages == []
        assert result.cursor == "old"

    asyncio.run(run())


def test_get_updates_uses_server_suggested_timeout(monkeypatch):
    async def run():
        _patch_client(
            monkeypatch,
            httpx.Response(200, json={"ret": 0, "msgs": [], "longpolling_timeout_ms": 42000}),
        )
        result = await ILinkClient("token", base_url="https://example.test").get_updates("old")
        assert result.next_timeout_ms == 42000

    asyncio.run(run())


def test_get_updates_read_timeout_is_normal_empty_poll(monkeypatch):
    async def run():
        request = httpx.Request("POST", "https://example.test/ilink/bot/getupdates")
        _patch_client(monkeypatch, httpx.ReadTimeout("long poll timeout", request=request))
        result = await ILinkClient("token", base_url="https://example.test").get_updates("old")
        assert result.messages == []
        assert result.cursor == "old"
        assert result.ret == 0

    asyncio.run(run())


def test_connect_error_is_classified_as_transport_error(monkeypatch):
    async def run():
        request = httpx.Request("POST", "https://example.test/ilink/bot/getupdates")
        _patch_client(monkeypatch, httpx.ConnectError("connection refused", request=request))
        with pytest.raises(ILinkTransportError) as raised:
            await ILinkClient("token", base_url="https://example.test").get_updates()
        assert raised.value.category == "tcp"

    asyncio.run(run())


def test_stale_token_business_code_pauses_session(monkeypatch):
    async def run():
        _patch_client(
            monkeypatch,
            httpx.Response(200, json={"ret": STALE_TOKEN_ERRCODE, "errmsg": "stale token"}),
        )
        client = ILinkClient("token", base_url="https://example.test")
        with pytest.raises(ILinkStaleTokenError) as raised:
            await client.get_updates()
        assert raised.value.ret == STALE_TOKEN_ERRCODE
        assert client.pause_until is not None
        with pytest.raises(ILinkStaleTokenError):
            await client.send_message("u1", "hello", "ctx")
        replacement_client = ILinkClient("token", base_url="https://example.test")
        with pytest.raises(ILinkStaleTokenError):
            await replacement_client.get_updates()

    asyncio.run(run())


def test_nonzero_business_code_is_protocol_error(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"ret": 123, "errmsg": "busy"}))
        with pytest.raises(ILinkError, match="busy"):
            await ILinkClient("token", base_url="https://example.test").get_updates()

    asyncio.run(run())


def test_send_message_posts_required_body(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"ok": True}))
        response = await ILinkClient("token", base_url="https://example.test").send_message("u1", "hello", "ctx")
        req = FakeAsyncClient.requests[0]
        body = req["kwargs"]["json"]
        assert response == {"ok": True}
        assert req["method"] == "POST"
        assert req["url"].endswith("/ilink/bot/sendmessage")
        assert body["base_info"] == BASE_INFO
        assert body["msg"]["to_user_id"] == "u1"
        assert body["msg"]["message_type"] == 2
        assert body["msg"]["message_state"] == 2
        assert body["msg"]["context_token"] == "ctx"
        assert body["msg"]["client_id"]
        assert body["msg"]["item_list"] == [{"type": 1, "text_item": {"text": "hello"}}]

    asyncio.run(run())


def test_send_message_requires_context_token():
    async def run():
        with pytest.raises(ILinkError):
            result = await ILinkClient("token").send_message("u1", "hello", "")
            assert result is None

    asyncio.run(run())


def test_http_error_raises_ilink_error(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(500, json={"error": "boom"}))
        with pytest.raises(ILinkError):
            result = await ILinkClient("token", base_url="https://example.test").get_updates()
            assert result is None

    asyncio.run(run())


def test_http_401_is_not_mapped_to_relogin(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(401, json={"error": "auth"}))
        with pytest.raises(ILinkHttpError) as raised:
            result = await ILinkClient("token", base_url="https://example.test").get_updates()
            assert result is None
        assert raised.value.status_code == 401

    asyncio.run(run())


def test_malformed_json_raises_ilink_error(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, content=b"not-json"))
        with pytest.raises(ILinkError):
            result = await ILinkClient("token", base_url="https://example.test").get_updates()
            assert result is None

    asyncio.run(run())


def test_async_client_request_can_be_mocked_directly(monkeypatch):
    async def run():
        mock_request = AsyncMock(return_value=httpx.Response(200, json={"qrcode": "qr"}))

        class MockClient(FakeAsyncClient):
            request = mock_request

        monkeypatch.setattr(httpx, "AsyncClient", MockClient)
        assert await ILinkClient("token", base_url="https://example.test").get_qrcode() == "qr"
        assert mock_request.await_count == 1

    asyncio.run(run())


def test_get_qrcode_works_without_token(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"qrcode": "qr-noauth"}))
        qrcode = await ILinkClient(base_url="https://example.test").get_qrcode()
        req = FakeAsyncClient.requests[0]
        assert qrcode == "qr-noauth"
        assert "Authorization" not in req["kwargs"]["headers"]

    asyncio.run(run())


def test_get_qrcode_status_works_without_token(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"status": "scanned"}))
        status = await ILinkClient(base_url="https://example.test").get_qrcode_status("qr-1")
        req = FakeAsyncClient.requests[0]
        assert status == "scanned"
        assert "Authorization" not in req["kwargs"]["headers"]

    asyncio.run(run())


def test_get_qrcode_detail_returns_full_login_payload(monkeypatch):
    async def run():
        payload = {"qrcode": "qr-1", "qrcode_img_content": "https://weixin.qq.com/x/qr-1"}
        _patch_client(monkeypatch, httpx.Response(200, json=payload))
        detail = await ILinkClient(base_url="https://example.test").get_qrcode_detail()
        assert detail == payload
        assert FakeAsyncClient.requests[0]["method"] == "POST"
        assert "Authorization" not in FakeAsyncClient.requests[0]["kwargs"]["headers"]

    asyncio.run(run())


def test_get_qrcode_status_detail_returns_full_payload(monkeypatch):
    async def run():
        payload = {"status": "confirmed", "bot_token": "tok-123"}
        _patch_client(monkeypatch, httpx.Response(200, json=payload))
        detail = await ILinkClient(base_url="https://example.test").get_qrcode_status_detail("qr-1")
        assert detail == payload
        assert FakeAsyncClient.requests[0]["method"] == "GET"
        assert "Authorization" not in FakeAsyncClient.requests[0]["kwargs"]["headers"]

    asyncio.run(run())


def test_get_updates_requires_token():
    async def run():
        with pytest.raises(ILinkAuthError, match="Bot token is required for get_updates"):
            result = await ILinkClient(base_url="https://example.test").get_updates()
            assert result is None

    asyncio.run(run())


def test_send_message_requires_token():
    async def run():
        with pytest.raises(ILinkAuthError, match="Bot token is required for send_message"):
            result = await ILinkClient(base_url="https://example.test").send_message("u1", "hello", "ctx")
            assert result is None

    asyncio.run(run())


def test_get_typing_ticket_posts_expected_body_and_returns_ticket(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"typing_ticket": "ticket-abc"}))
        ticket = await ILinkClient("token", base_url="https://example.test").get_typing_ticket("u@im.wechat")
        req = FakeAsyncClient.requests[0]
        assert ticket == "ticket-abc"
        assert req["method"] == "POST"
        assert req["url"].endswith("/ilink/bot/getconfig")
        assert req["kwargs"]["json"] == {
            "ilink_user_id": "u@im.wechat",
            "base_info": BASE_INFO,
        }

    asyncio.run(run())


def test_get_typing_ticket_passes_context_token(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"typing_ticket": "ticket-abc"}))
        await ILinkClient("token", base_url="https://example.test").get_typing_ticket(
            "u@im.wechat", "ctx"
        )
        assert FakeAsyncClient.requests[0]["kwargs"]["json"]["context_token"] == "ctx"

    asyncio.run(run())


def test_get_typing_ticket_missing_ticket_raises_error(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={}))
        with pytest.raises(ILinkError, match="typing_ticket"):
            _ = await ILinkClient("token", base_url="https://example.test").get_typing_ticket("u@im.wechat")

    asyncio.run(run())


def test_get_typing_ticket_non_string_ticket_raises_error(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"typing_ticket": 123}))
        with pytest.raises(ILinkError, match="typing_ticket"):
            _ = await ILinkClient("token", base_url="https://example.test").get_typing_ticket("u@im.wechat")

    asyncio.run(run())


def test_get_typing_ticket_requires_token():
    async def run():
        with pytest.raises(ILinkAuthError, match="Bot token is required for get_typing_ticket"):
            _ = await ILinkClient(base_url="https://example.test").get_typing_ticket("u@im.wechat")

    asyncio.run(run())


def test_send_typing_posts_expected_body(monkeypatch):
    async def run():
        _patch_client(monkeypatch, httpx.Response(200, json={"ok": True}))
        response = await ILinkClient("token", base_url="https://example.test").send_typing("u@im.wechat", "ticket-abc", 1)
        req = FakeAsyncClient.requests[0]
        assert response == {"ok": True}
        assert req["method"] == "POST"
        assert req["url"].endswith("/ilink/bot/sendtyping")
        assert req["kwargs"]["json"] == {
            "ilink_user_id": "u@im.wechat",
            "typing_ticket": "ticket-abc",
            "status": 1,
            "base_info": BASE_INFO,
        }

    asyncio.run(run())


def test_send_typing_requires_user_id():
    async def run():
        with pytest.raises(ILinkError, match="ilink_user_id is required"):
            _ = await ILinkClient("token", base_url="https://example.test").send_typing("", "ticket-abc", 1)

    asyncio.run(run())


def test_send_typing_requires_ticket():
    async def run():
        with pytest.raises(ILinkError, match="typing_ticket is required"):
            _ = await ILinkClient("token", base_url="https://example.test").send_typing("u@im.wechat", "", 1)

    asyncio.run(run())


def test_send_typing_requires_token():
    async def run():
        with pytest.raises(ILinkAuthError, match="Bot token is required for send_typing"):
            _ = await ILinkClient(base_url="https://example.test").send_typing("u@im.wechat", "ticket-abc", 1)

    asyncio.run(run())


def test_headers_omit_authorization_when_no_token():
    client = ILinkClient(base_url="https://example.test")
    headers = client._headers()
    assert "Authorization" not in headers
    assert headers["AuthorizationType"] == "ilink_bot_token"


def test_headers_include_authorization_when_token_set():
    client = ILinkClient("my-token", base_url="https://example.test")
    headers = client._headers()
    assert headers["Authorization"] == "Bearer my-token"
