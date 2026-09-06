import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db.models import Base
from app.services.ai_provider_service import AIProviderConfig, AIProviderError, AIProviderService
from app.services.caldav_service import CalDAVService, CalDAVServiceError
from app.services.settings_service import SettingsService
from app.web import routes


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/console", "headers": [], "session": {}})


@pytest.mark.anyio
async def test_ai_probe_schema_compliance_success(monkeypatch):
    service = AIProviderService()
    config = AIProviderConfig(provider_type="openai_compatible", base_url="https://api.example.com", api_key="test-key", model="test-model")

    mock_resp = json.dumps({
        "intent": "create_event",
        "events": [
            {
                "title": "项目进度同步会",
                "start_time": "2026-06-01T15:00:00+08:00",
                "is_all_day": False
            }
        ]
    })
    monkeypatch.setattr(service, "chat_completion", AsyncMock(return_value=mock_resp))

    result = await service.probe_schema_compliance(config)
    assert result["ok"] is True
    assert result["title"] == "项目进度同步会"
    assert result["start_time"] == "2026-06-01T15:00:00+08:00"
    assert result["intent"] == "create_event"


@pytest.mark.anyio
async def test_ai_probe_schema_compliance_markdown_and_single_event(monkeypatch):
    service = AIProviderService()
    config = AIProviderConfig(provider_type="anthropic", base_url="https://api.anthropic.com", api_key="sk-ant", model="claude-3")

    mock_resp = "```json\n" + json.dumps({
        "intent": "create_event",
        "event": {
            "title": "架构研讨",
            "start_time": "2026-06-02T10:00:00+08:00"
        }
    }) + "\n```"
    monkeypatch.setattr(service, "chat_completion", AsyncMock(return_value=mock_resp))

    result = await service.probe_schema_compliance(config)
    assert result["ok"] is True
    assert result["title"] == "架构研讨"
    assert result["start_time"] == "2026-06-02T10:00:00+08:00"


@pytest.mark.anyio
async def test_ai_probe_schema_compliance_missing_model():
    service = AIProviderService()
    config = AIProviderConfig(provider_type="openai_compatible", base_url="https://api.example.com", api_key="k", model="")
    with pytest.raises(AIProviderError, match="请先选择或输入模型"):
        await service.probe_schema_compliance(config)


@pytest.mark.anyio
async def test_ai_probe_schema_compliance_invalid_json(monkeypatch):
    service = AIProviderService()
    config = AIProviderConfig(provider_type="openai_compatible", base_url="https://api.example.com", api_key="k", model="test-model")
    monkeypatch.setattr(service, "chat_completion", AsyncMock(return_value="I am an AI, I cannot generate events."))
    with pytest.raises(AIProviderError, match="未返回合法的 JSON"):
        await service.probe_schema_compliance(config)


@pytest.mark.anyio
async def test_ai_probe_schema_compliance_schema_violations(monkeypatch):
    service = AIProviderService()
    config = AIProviderConfig(provider_type="openai_compatible", base_url="https://api.example.com", api_key="k", model="test-model")

    # Missing intent
    monkeypatch.setattr(service, "chat_completion", AsyncMock(return_value=json.dumps({"events": []})))
    with pytest.raises(AIProviderError, match="缺少 'intent' 字段"):
        await service.probe_schema_compliance(config)

    # Missing events
    monkeypatch.setattr(service, "chat_completion", AsyncMock(return_value=json.dumps({"intent": "create_event"})))
    with pytest.raises(AIProviderError, match="缺少有效的 'events' 列表"):
        await service.probe_schema_compliance(config)

    # Missing title
    monkeypatch.setattr(service, "chat_completion", AsyncMock(return_value=json.dumps({
        "intent": "create_event", "events": [{"start_time": "2026-06-01T10:00:00"}]
    })))
    with pytest.raises(AIProviderError, match="未能提取到有效的日程标题"):
        await service.probe_schema_compliance(config)

    # Missing start_time
    monkeypatch.setattr(service, "chat_completion", AsyncMock(return_value=json.dumps({
        "intent": "create_event", "events": [{"title": "开会"}]
    })))
    with pytest.raises(AIProviderError, match="未能提取到有效的日程时间"):
        await service.probe_schema_compliance(config)


@pytest.mark.anyio
async def test_ai_schema_test_route_endpoint(monkeypatch):
    session = _session()
    settings = SettingsService(session)
    settings.set("ai_provider_name", "DeepSeek")
    settings.set("ai_provider_type", "openai_compatible")
    settings.set("ai_base_url", "https://api.deepseek.com/v1")
    settings.set("ai_api_key", "saved-key")
    settings.set("ai_model", "deepseek-chat")
    settings.commit()

    class FakeAI:
        async def probe_schema_compliance(self, config):
            assert config.model == "deepseek-chat"
            return {"ok": True, "title": "测试会议", "start_time": "2026-06-01T15:00:00+08:00"}

    monkeypatch.setattr(routes, "_ai_components", lambda: (AIProviderConfig, AIProviderError, FakeAI))

    resp = await routes.test_ai_schema_compliance(
        _request(),
        provider_name="DeepSeek",
        provider_type="openai_compatible",
        base_url="",
        api_key="",
        clear_api_key="",
        model="",
        session=session,
        _=None,
    )
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert "测试会议" in data["message"]


def test_caldav_probe_write_sync_creates_and_deletes_event(monkeypatch):
    service = CalDAVService()

    saved_items = []
    deleted_items = []

    class FakeEventObject:
        def __init__(self, ical_str, uid, url):
            self.data = ical_str
            self.id = uid
            self.url = url

        def delete(self):
            deleted_items.append((self.id, self.url))

    class FakeCalendar:
        def __init__(self, name, url):
            self.name = name
            self.url = url
            self._objects = []

        def save_event(self, ical_str):
            assert "[PROBE-TEST]" in ical_str
            import re
            m = re.search(r"UID:(.+)", ical_str)
            uid = m.group(1).strip() if m else "fake-uid"
            obj = FakeEventObject(ical_str, uid, f"{self.url}/{uid}.ics")
            self._objects.append(obj)
            saved_items.append(obj)
            return obj

        def objects(self):
            return list(self._objects)

    fake_cal = FakeCalendar("Work", "https://cal.example.com/dav/work")

    class FakeClient:
        def get_calendars(self):
            return [fake_cal]

    monkeypatch.setattr("app.services.caldav_service._DAVClient", lambda **kwargs: FakeClient())

    result = service._probe_write_sync(
        caldav_url="https://cal.example.com/dav",
        username="user",
        password="pwd",
        calendar_url="https://cal.example.com/dav/work",
        ssl_verify=True,
    )

    assert result["ok"] is True
    assert "[PROBE-TEST]" in result["summary"]
    # Verify event was saved
    assert len(saved_items) == 1
    # Verify event was deleted in finally: (safe rollback)
    assert len(deleted_items) == 1
    assert deleted_items[0][0] == result["uid"]


@pytest.mark.anyio
async def test_caldav_write_test_route_endpoint(monkeypatch):
    session = _session()
    settings = SettingsService(session)
    settings.set("caldav_url", "https://cal.example.com/dav")
    settings.set("caldav_username", "user")
    settings.set("caldav_password", "saved-pwd")
    settings.set("caldav_calendar_url", "https://cal.example.com/dav/work")
    settings.commit()

    class FakeCalService:
        async def probe_write(self, url, username, password, calendar_url, ssl_verify):
            assert url == "https://cal.example.com/dav"
            assert username == "user"
            assert password == "saved-pwd"
            assert calendar_url == "https://cal.example.com/dav/work"
            return {"ok": True, "uid": "probe-test-uid"}

    class FakeCalError(Exception):
        pass

    monkeypatch.setattr(routes, "_caldav_components", lambda: (FakeCalService, FakeCalError))

    resp = await routes.test_caldav_write_permission(
        _request(),
        caldav_url="https://cal.example.com/dav",
        caldav_username="user",
        caldav_password="",
        caldav_calendar_url="https://cal.example.com/dav/work",
        caldav_ssl_verify="true",
        session=session,
        _=None,
    )
    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert "安全回滚清理" in data["message"]


def test_caldav_probe_write_no_calendars_raises_error(monkeypatch):
    service = CalDAVService()

    class FakeEmptyClient:
        def get_calendars(self):
            return []

    monkeypatch.setattr("app.services.caldav_service._DAVClient", lambda **kwargs: FakeEmptyClient())

    with pytest.raises(CalDAVServiceError, match="未找到可用日历"):
        service._probe_write_sync(
            caldav_url="https://cal.example.com/dav",
            username="user",
            password="pwd",
            calendar_url="",
            ssl_verify=True,
        )


@pytest.mark.anyio
async def test_caldav_write_test_route_error_returns_400(monkeypatch):
    session = _session()
    settings = SettingsService(session)
    settings.set("caldav_url", "https://cal.example.com/dav")
    settings.set("caldav_username", "user")
    settings.set("caldav_password", "saved-pwd")
    settings.commit()

    class FailingCalService:
        async def probe_write(self, *args, **kwargs):
            raise CalDAVServiceError("日历权限不足 (403 Forbidden)")

    monkeypatch.setattr(routes, "_caldav_components", lambda: (FailingCalService, CalDAVServiceError))

    resp = await routes.test_caldav_write_permission(
        _request(),
        caldav_url="https://cal.example.com/dav",
        caldav_username="user",
        caldav_password="saved-pwd",
        caldav_calendar_url="",
        caldav_ssl_verify="true",
        session=session,
        _=None,
    )
    assert resp.status_code == 400
    data = json.loads(resp.body)
    assert data["ok"] is False
    assert "日历权限不足" in data["error"]



def test_caldav_template_presets_and_iana_datalist():
    template = (Path(__file__).parents[1] / "app/web/templates/caldav.html").read_text()

    # 1. 验证服务商预设下拉框及主流服务商预设
    assert 'id="caldav-provider-select"' in template
    assert 'value="icloud"' in template
    assert 'value="nextcloud"' in template
    assert 'value="163"' in template
    assert 'value="qq"' in template
    assert 'value="fastmail"' in template
    assert 'value="custom"' in template
    assert 'id="caldav-provider-hint"' in template

    # 2. 验证写入权限探针按钮
    assert 'data-caldav-action="write-test"' in template
    assert "测试写入权限" in template

    # 3. 验证 IANA 时区 datalist
    assert 'list="iana-timezones"' in template
    assert '<datalist id="iana-timezones">' in template
    assert "{% for tz in timezones %}" in template

    # 4. 验证 ai.html 中的结构验证按钮
    ai_template = (Path(__file__).parents[1] / "app/web/templates/ai.html").read_text()
    assert 'data-endpoint="/console/ai/schema-test"' in ai_template
    assert "验证结构输出" in ai_template
