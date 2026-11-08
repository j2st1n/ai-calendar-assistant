import asyncio
import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Base
from app.services import ai_provider_service as provider
from app.services.settings_service import SettingsService


def test_conversation_ids_are_stable_isolated_and_opaque(monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "synthetic-application-secret")
    first = provider.conversation_session_id("telegram", "private-user", "private-chat")
    assert first == provider.conversation_session_id("telegram", "private-user", "private-chat")
    assert len(first) == 68
    assert "private" not in first
    assert first != provider.conversation_session_id("discord", "private-user", "private-chat")
    assert first != provider.conversation_session_id("telegram", "different-user", "private-chat")
    assert first != provider.conversation_session_id("telegram", "private-user", "different-chat")
    monkeypatch.setattr(settings, "app_secret_key", "different-installation-secret")
    assert first != provider.conversation_session_id("telegram", "private-user", "private-chat")


def test_uninitialized_secret_fallback_and_independent_probes(monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", None)
    assert provider.conversation_session_id("telegram", "user") == provider.conversation_session_id("telegram", "user")
    configs = [provider.AIProviderConfig("openai_compatible", "https://opencode.ai/zen/go/v1", "test") for _ in range(2)]
    assert configs[0].session_id != configs[1].session_id


@pytest.mark.parametrize("name,expected", [("APITimeoutError", "超时"), ("APIConnectionError", "无法连接")])
def test_nested_network_errors_keep_safe_actionable_category(name, expected):
    error = type(name, (Exception,), {})("synthetic-private-key")
    error.__cause__ = OSError("https://private.invalid")
    wrapped = provider.AIProviderError("private provider message")
    wrapped.__cause__ = error
    message = provider.safe_ai_connection_error(wrapped)
    assert expected in message
    assert "private" not in message


@pytest.mark.parametrize("base", [
    "https://api.openai.com/v1", "https://opencode.ai.evil.invalid/v1",
    "https://opencode.ai@evil.invalid/v1", "http://opencode.ai/zen/go/v1",
    "https://opencode.ai:8443/zen/go/v1", "https://example.invalid/opencode.ai",
])
def test_session_header_is_not_sent_to_other_origins(base):
    config = provider.AIProviderConfig("openai_compatible", base, "test")
    assert "x-opencode-session" not in provider._openai_client_kwargs(config, timeout=20)["default_headers"]
    assert "default_headers" not in provider._anthropic_client_kwargs(config, timeout=20)


def install_transport(monkeypatch, protocol):
    requests = []
    transport_http = httpx

    def handle(request):
        requests.append(request)
        assert request.headers["x-opencode-session"] == "cal-synthetic-session"
        assert request.headers["user-agent"] == "ai-calendar-assistant"
        if request.url.path.endswith("/models"):
            data = {"data": [{"id": "test-model", "type": "model", "display_name": "Test", "created_at": "2026-01-01T00:00:00Z"}], "has_more": False}
        elif request.url.path.endswith("/messages"):
            data = {"id": "test", "type": "message", "role": "assistant", "model": "test-model", "content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
        else:
            content = json.dumps({"intent": "create_event", "events": [{"title": "Synthetic", "start_time": "2026-11-09T15:00:00+08:00"}]})
            data = {"id": "test", "object": "chat.completion", "created": 1, "model": "test-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
        return transport_http.Response(200, json=data)

    name = "_anthropic_client_kwargs" if protocol == "anthropic" else "_openai_client_kwargs"
    original = getattr(provider, name)

    def kwargs(config, *, timeout):
        return {**original(config, timeout=timeout), "http_client": transport_http.AsyncClient(transport=transport_http.MockTransport(handle)), "max_retries": 0}

    monkeypatch.setattr(provider, name, kwargs)
    return requests


@pytest.mark.parametrize("operation", ["connection", "chat", "vision", "models", "schema"])
def test_opencode_openai_paths_send_session_through_real_sdk(monkeypatch, operation):
    requests = install_transport(monkeypatch, "openai_compatible")
    config = provider.AIProviderConfig("openai_compatible", "https://opencode.ai/zen/go/v1", "synthetic-key", "test-model", "cal-synthetic-session")
    service = provider.AIProviderService()

    async def run():
        if operation == "connection":
            await service.test_connection(config)
        elif operation == "chat":
            await service.chat_completion(config, "Synthetic system", "Synthetic input")
        elif operation == "vision":
            await service.vision_completion(config, "iVBORw0KGgo=")
        elif operation == "models":
            assert await service.list_models(config) == ["test-model"]
        else:
            assert (await service.probe_schema_compliance(config))["ok"] is True

    asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize("operation", ["connection", "chat", "vision", "models"])
def test_opencode_anthropic_clients_receive_scoped_header(monkeypatch, operation):
    from types import SimpleNamespace
    clients = []

    class Client:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.messages = SimpleNamespace(create=self.create)
            self.models = SimpleNamespace(list=self.list)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")])

        async def list(self):
            return SimpleNamespace(data=[SimpleNamespace(id="test-model")])

    monkeypatch.setattr(provider, "_anthropic_sdk", lambda: (Client, RuntimeError))
    config = provider.AIProviderConfig("anthropic", "https://opencode.ai/inference/go/anthropic", "synthetic-key", "test-model", "cal-synthetic-session")
    service = provider.AIProviderService()

    async def run():
        if operation == "connection":
            await service.test_connection(config)
        elif operation == "chat":
            await service.chat_completion(config, "Synthetic system", "Synthetic input")
        elif operation == "vision":
            await service.vision_completion(config, "iVBORw0KGgo=")
        else:
            assert await service.list_models(config) == ["test-model"]

    asyncio.run(run())
    assert len(clients) == 1
    assert clients[0]["default_headers"] == {"User-Agent": "ai-calendar-assistant", "x-opencode-session": "cal-synthetic-session"}


@pytest.mark.parametrize("status,body,expected", [
    (400, "Missing x-opencode-session header", "x-opencode-session"),
    (401, "invalid key", "API Key"),
    (403, "denied", "账号权限"),
    (404, "missing model", "接口或模型不存在"),
    (429, "rate limit", "速率或用量限制"),
    (503, "unavailable", "供应商服务暂时异常"),
])
def test_saved_ai_failure_reports_safe_category_without_persisting_error(monkeypatch, status, body, expected):
    from app.web import routes
    from app.web.connection_checks import check_summary

    class UpstreamError(Exception):
        status_code = status

    upstream = UpstreamError("synthetic-secret https://private.invalid/api")
    upstream.body = {"error": body, "key": "synthetic-secret"}

    class FakeProvider:
        async def test_connection(self, config):
            raise provider.AIProviderError("private provider error") from upstream

    monkeypatch.setattr(routes, "_ai_components", lambda: (provider.AIProviderConfig, provider.AIProviderError, FakeProvider))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        svc = SettingsService(db)
        svc.set("ai_model", "test-model")
        svc.set("ai_api_key", "synthetic-key")
        svc.commit()
        response = asyncio.run(routes.test_saved_connection("ai", db, None))
        data = json.loads(response.body)
        assert data["ok"] is False
        assert str(status) in data["error"] and expected in data["error"]
        assert "synthetic-secret" not in response.body.decode()
        assert "private.invalid" not in response.body.decode()
        assert check_summary(db, "ai")["ok"] is False
        assert svc.get("ai_api_key") == "synthetic-key"
        assert "error" not in json.loads(svc.get("connection_check_ai"))


def test_message_processor_reuses_scoped_session_across_followups(monkeypatch):
    from app.channels import message_processor

    captured = []

    async def route(db, ctx, text, extractor, caldav, svc):
        captured.append(extractor._config.session_id)
        return []

    monkeypatch.setattr(message_processor, "_route", route)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        async def run():
            processor = message_processor.MessageProcessor()
            await processor.process(db, "user1", "Initial", conversation_id="chat1")
            await processor.process(db, "user1", "Followup", conversation_id="chat1")
            await processor.process(db, "user1", "Other chat", conversation_id="chat2")
            await processor.process(db, "user2", "Other user", conversation_id="chat1")
        asyncio.run(run())
    assert captured[0] == captured[1]
    assert len(set(captured)) == 3
