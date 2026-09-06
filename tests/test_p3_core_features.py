import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.testclient import TestClient

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels.message_processor import (
    ChannelContext,
    _format_diff_summary,
    _format_modify_result,
    _get_active_recent_events,
    _pending_ambiguities,
    _route,
)
from app.db.models import Base, EventRecord
from app.main import create_app
from app.services.caldav_service import CalDAVService, clear_events_cache
from app.services.settings_service import SettingsService
from app.web import routes
from app.web.routes import get_db, require_admin, router


def _session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _ctx(**kwargs) -> ChannelContext:
    defaults = {
        "source": "telegram",
        "source_user_id": "u1",
        "conversation_id": "c1",
        "source_message_id": "m1",
        "reply_to_message_id": None,
        "quoted_text": None,
        "quote_reference_present": False,
    }
    defaults.update(kwargs)
    return ChannelContext(**defaults)


def _caldav() -> dict[str, object]:
    return {
        "url": "",
        "user": "",
        "pw": "",
        "cal": "",
        "rem": 15,
        "dur": 60,
        "ssl": True,
    }


class _FakeExtractor:
    def __init__(self, intent=Intent.create_event, event=None, events=None):
        self._intent = intent
        self._event = event
        self._events = events if events is not None else []

    async def extract(self, text: str) -> ExtractionResult:
        return ExtractionResult(
            intent=self._intent,
            event=self._event,
            events=self._events,
            confidence=0.95,
        )

    async def modify(self, existing: dict, text: str) -> ExtractionResult:
        return ExtractionResult(
            intent=self._intent,
            event=self._event,
            events=self._events,
            confidence=0.95,
        )


# =========================================================================
# 1. 字段 Diff 对照测试
# =========================================================================

def test_format_diff_summary_time_and_location():
    old_event = {
        "title": "团队周会",
        "start_time": "2026-06-02T10:00:00+08:00",
        "end_time": "2026-06-02T11:00:00+08:00",
        "location": "会议室A",
        "description": "讨论季度规划",
    }
    new_event = {
        "title": "团队周会",
        "start_time": "2026-06-02T14:00:00+08:00",
        "end_time": "2026-06-02T15:00:00+08:00",
        "location": "会议室B",
        "description": "讨论季度规划与人力分配",
    }

    diffs = _format_diff_summary(old_event, new_event)
    assert any("时间" in d and "10:00" in d and "14:00" in d and "➔" in d for d in diffs)
    assert any("地点" in d and "会议室A" in d and "会议室B" in d and "➔" in d for d in diffs)
    assert any("描述" in d and "➔" in d for d in diffs)


def test_format_modify_result_renders_diff_block():
    old_event = {
        "title": "例会",
        "start_time": "2026-06-02T10:00:00+08:00",
        "end_time": "2026-06-02T11:00:00+08:00",
        "location": "办公室",
    }
    new_event = {
        "title": "例会",
        "start_time": "2026-06-02T15:00:00+08:00",
        "end_time": "2026-06-02T16:00:00+08:00",
        "location": "线上会议室",
    }

    result = _format_modify_result(new_event, old_event=old_event)
    assert "✅ 日程已更新！" in result
    assert "🔄 修改对照：" in result
    assert "• 时间：2026-06-02 10:00 - 11:00 ➔ 2026-06-02 15:00 - 16:00" in result
    assert "• 地点：办公室 ➔ 线上会议室" in result
    assert "📌 标题：例会" in result
    assert "🕒 时间：2026-06-02 15:00 - 16:00" in result


def test_format_modify_result_backward_compatible():
    event = {
        "title": "例会",
        "start_time": "2026-06-02T15:00:00+08:00",
    }
    result = _format_modify_result(event)
    assert "✅ 日程已更新！" in result
    assert "🔄 修改对照：" not in result


# =========================================================================
# 2. 歧义阻断机制（Ambiguity Guard）测试
# =========================================================================

def test_ambiguity_guard_blocks_delete_when_multiple_candidates():
    async def run():
        session = _session()
        _pending_ambiguities.clear()

        # Seed two distinct active events with similar title
        rec1 = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="项目周会",
            start_time="2026-06-02T10:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "项目周会", "start_time": "2026-06-02T10:00:00+08:00"}),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            event_id="e1",
        )
        rec2 = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="项目周会",
            start_time="2026-06-03T10:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "项目周会", "start_time": "2026-06-03T10:00:00+08:00"}),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            event_id="e2",
        )
        session.add_all([rec1, rec2])
        session.commit()

        svc = SettingsService(session)
        extractor = _FakeExtractor(intent=Intent.delete_event)

        # Trigger delete without exact reference
        replies = await _route(session, _ctx(), "删除周会", extractor, _caldav(), svc)
        assert len(replies) == 1
        text, rid = replies[0]

        # 严禁猜测执行，强制暂停并输出候选编号
        assert "⚠️ 发现多条匹配的日程，为防误删除已暂停执行" in text
        assert "[1] 📌 项目周会" in text
        assert "[2] 📌 项目周会" in text
        assert "请直接回复序号" in text

        # Check DB: no delete operation was performed
        del_count = session.query(EventRecord).filter(EventRecord.operation == "delete").count()
        assert del_count == 0

        # Check ambiguity guard log
        guard_recs = session.query(EventRecord).filter(EventRecord.operation == "ambiguity_guard").all()
        assert len(guard_recs) == 1
        assert "阻断执行" in guard_recs[0].error_message

    asyncio.run(run())


def test_ambiguity_guard_resolves_delete_on_number_confirmation():
    async def run():
        session = _session()
        _pending_ambiguities.clear()

        rec1 = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="周会A",
            start_time="2026-06-02T10:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "周会A"}),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            event_id="e1",
        )
        rec2 = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="周会B",
            start_time="2026-06-03T10:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "周会B"}),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            event_id="e2",
        )
        session.add_all([rec1, rec2])
        session.commit()

        svc = SettingsService(session)
        extractor = _FakeExtractor(intent=Intent.delete_event)

        # 1. Trigger ambiguity
        _ = await _route(session, _ctx(), "删除日程", extractor, _caldav(), svc)

        # 2. Confirm selection with "确认 1"
        replies2 = await _route(session, _ctx(), "确认 1", extractor, _caldav(), svc)
        assert len(replies2) == 1
        text2, _ = replies2[0]
        assert "已删除" in text2

        # Check delete record recorded in DB
        del_recs = session.query(EventRecord).filter(EventRecord.operation == "delete").all()
        assert len(del_recs) == 1

    asyncio.run(run())


def test_ambiguity_guard_blocks_update_and_resolves():
    async def run():
        session = _session()
        _pending_ambiguities.clear()

        rec1 = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="开会",
            start_time="2026-06-02T10:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "开会", "start_time": "2026-06-02T10:00:00+08:00"}),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            event_id="e1",
        )
        rec2 = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="开会",
            start_time="2026-06-03T10:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "开会", "start_time": "2026-06-03T10:00:00+08:00"}),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            event_id="e2",
        )
        session.add_all([rec1, rec2])
        session.commit()

        svc = SettingsService(session)
        new_event = CalendarEvent(title="开会", start_time="2026-06-02T16:00:00+08:00")
        extractor = _FakeExtractor(intent=Intent.update_event, event=new_event)

        # 1. Trigger ambiguity on update
        replies = await _route(session, _ctx(), "把开会改到下午4点", extractor, _caldav(), svc)
        assert len(replies) == 1
        text, _ = replies[0]
        assert "⚠️ 发现多条匹配的日程，为防误修改已暂停执行" in text
        assert "[1]" in text and "[2]" in text

        # 2. Confirm selection with "2"
        replies2 = await _route(session, _ctx(), "2", extractor, _caldav(), svc)
        assert len(replies2) == 1
        text2, rid2 = replies2[0]
        assert "✅ 日程已更新！" in text2
        assert "🔄 修改对照：" in text2
        assert "• 时间：" in text2

        # Check DB: only 1 update record created
        update_recs = session.query(EventRecord).filter(EventRecord.operation == "update").all()
        assert len(update_recs) == 1

    asyncio.run(run())


def test_ambiguity_guard_cancel_command():
    async def run():
        session = _session()
        _pending_ambiguities.clear()

        rec1 = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="周会",
            start_time="2026-06-02T10:00:00+08:00",
            status="success",
            event_json="{}",
            created_at=datetime.now(timezone.utc),
            event_id="e1",
        )
        rec2 = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="周会",
            start_time="2026-06-03T10:00:00+08:00",
            status="success",
            event_json="{}",
            created_at=datetime.now(timezone.utc),
            event_id="e2",
        )
        session.add_all([rec1, rec2])
        session.commit()

        svc = SettingsService(session)
        extractor = _FakeExtractor(intent=Intent.delete_event)
        _ = await _route(session, _ctx(), "删除周会", extractor, _caldav(), svc)

        # Cancel
        replies = await _route(session, _ctx(), "取消", extractor, _caldav(), svc)
        assert replies == [("已取消操作。", None)]
        assert len(_pending_ambiguities) == 0

    asyncio.run(run())


# =========================================================================
# 3. CalDAVService list_events 与 30s 内存短缓存测试
# =========================================================================

def test_caldav_list_events_and_30s_cache(monkeypatch):
    clear_events_cache()

    class FakeEventObj:
        def __init__(self, uid, summary, dtstart, dtend):
            self.id = uid
            self.url = f"https://cal.example.com/dav/{uid}.ics"
            self.data = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:{uid}
SUMMARY:{summary}
DTSTART:{dtstart}
DTEND:{dtend}
LOCATION:会议室
DESCRIPTION:测试会议
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR"""

    call_count = {"count": 0}

    class FakeCalendar:
        def __init__(self):
            self.url = "https://cal.example.com/dav/work"
            self.name = "Work"

        def date_search(self, start, end, compfilter="VEVENT", expand=True):
            call_count["count"] += 1
            return [
                FakeEventObj("ev-1", "项目评审", "20260602T100000Z", "20260602T110000Z"),
                FakeEventObj("ev-2", "架构设计", "20260602T140000Z", "20260602T150000Z"),
            ]

    class FakeClient:
        def get_calendars(self):
            return [FakeCalendar()]

    monkeypatch.setattr("app.services.caldav_service._DAVClient", lambda **kwargs: FakeClient())

    svc = CalDAVService()
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 10, tzinfo=timezone.utc)

    # 1. First fetch: hits CalDAV server
    events1 = asyncio.run(svc.list_events("https://cal.example.com/dav", "user", "pwd", "https://cal.example.com/dav/work", start, end))
    assert len(events1) == 2
    assert events1[0]["title"] == "项目评审"
    assert events1[1]["title"] == "架构设计"
    assert call_count["count"] == 1

    # 2. Second fetch within 30s: hits in-memory cache
    events2 = asyncio.run(svc.list_events("https://cal.example.com/dav", "user", "pwd", "https://cal.example.com/dav/work", start, end))
    assert len(events2) == 2
    assert call_count["count"] == 1  # Not incremented! Cache hit.

    # 3. Force refresh: ignores cache and hits CalDAV server
    events3 = asyncio.run(svc.list_events("https://cal.example.com/dav", "user", "pwd", "https://cal.example.com/dav/work", start, end, force_refresh=True))
    assert len(events3) == 2
    assert call_count["count"] == 2  # Incremented! Cache bypassed.

    # 4. Clear cache
    svc.clear_cache()
    events4 = asyncio.run(svc.list_events("https://cal.example.com/dav", "user", "pwd", "https://cal.example.com/dav/work", start, end))
    assert call_count["count"] == 3  # Hit server again.


# =========================================================================
# 4. /console/calendar 路由与只读时间轴展示测试
# =========================================================================

def test_calendar_template_renders_direct():
    request = Request({"type": "http", "method": "GET", "path": "/console/calendar", "headers": [], "session": {"admin_authenticated": True}})

    rendered = routes.templates.get_template("calendar.html").render(
        request=request,
        caldav_configured=True,
        data_source="工作日历",
        calendar_url="https://cal.example.com/dav/work",
        caldav_server="https://cal.example.com/dav",
        timezone="Asia/Shanghai",
        fetched_at="2026-09-06 18:00:00",
        range="month",
        events=[
            {
                "uid": "1",
                "title": "战略对齐会议",
                "start_time": "2026-06-02T10:00:00+08:00",
                "end_time": "2026-06-02T11:00:00+08:00",
                "is_all_day": False,
                "location": "大会议室",
                "description": "半年度核心战略规划",
                "status": "CONFIRMED",
            }
        ],
        timeline_groups=[
            {
                "date": "2026-06-02",
                "events": [
                    {
                        "uid": "1",
                        "title": "战略对齐会议",
                        "start_time": "2026-06-02T10:00:00+08:00",
                        "end_time": "2026-06-02T11:00:00+08:00",
                        "is_all_day": False,
                        "location": "大会议室",
                        "description": "半年度核心战略规划",
                        "status": "CONFIRMED",
                    }
                ],
            }
        ],
        total=1,
        error=None,
        message=None,
    )

    assert "日程列表" in rendered
    assert "工作日历" in rendered
    assert "Asia/Shanghai" in rendered
    assert "2026-09-06 18:00:00" in rendered
    assert "30s 内存短缓存" in rendered
    assert "战略对齐会议" in rendered
    assert "大会议室" in rendered
    assert "只读" in rendered
    assert "btn-refresh-calendar" in rendered
    assert "/console/calendar" in rendered


class _FakeSessionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["session"] = {"admin_authenticated": True}
        await self.app(scope, receive, send)


def test_console_calendar_route_authenticated(monkeypatch):
    session = _session()
    svc = SettingsService(session)
    svc.set("caldav_url", "https://cal.example.com/dav")
    svc.set("caldav_username", "alice")
    svc.set("caldav_password", "secret")
    svc.set("caldav_calendar_name", "工作日历")
    svc.set("caldav_calendar_url", "https://cal.example.com/dav/work")
    svc.set("caldav_timezone", "Asia/Shanghai")
    svc.commit()

    mock_events = [
        {
            "uid": "event-101",
            "href": "https://cal.example.com/dav/event-101.ics",
            "title": "战略对齐会议",
            "start_time": "2026-06-02T09:00:00+08:00",
            "end_time": "2026-06-02T10:00:00+08:00",
            "is_all_day": False,
            "location": "大会议室",
            "description": "半年度核心战略规划",
            "status": "CONFIRMED",
        }
    ]

    async def mock_list_events(*args, **kwargs):
        return mock_events

    monkeypatch.setattr("app.services.caldav_service.CalDAVService.list_events", mock_list_events)

    from fastapi import FastAPI
    app = FastAPI()
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db] = lambda: session
    app.add_middleware(_FakeSessionMiddleware)
    app.include_router(router)

    client = TestClient(app)
    # 1. HTML request
    resp = client.get("/console/calendar")
    assert resp.status_code == 200
    assert "工作日历" in resp.text
    assert "战略对齐会议" in resp.text
    assert "30s 内存短缓存" in resp.text

    # 2. JSON request
    resp_json = client.get("/console/calendar", headers={"Accept": "application/json"})
    assert resp_json.status_code == 200
    data = resp_json.json()
    assert data["ok"] is True
    assert data["data_source"] == "工作日历"
    assert data["total"] == 1
    assert data["events"][0]["title"] == "战略对齐会议"
