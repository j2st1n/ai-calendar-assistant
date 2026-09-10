import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels.message_processor import (
    ChannelContext,
    _route,
)
from app.db.models import Base, EventRecord
from app.services.caldav_service import CalDAVService
from app.services.settings_service import SettingsService


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["telegram", "discord", "wechat"])
@pytest.mark.parametrize("text,state,method", [
    ("撤销", "plain", "extract"),
    ("undo", "reply", "modify"),
    ("恢复", "draft", "merge_draft"),
    ("回退", "ambiguity", "extract"),
])
async def test_former_undo_words_use_normal_language_routing(source, text, state, method):
    import time
    from app.channels import message_processor as mp

    ctx = _ctx(source=source, reply_to_message_id="42" if state == "reply" else None)
    draft_key = f"draft_{source}:u1:c1"
    ambiguity_key = f"ambiguity_{source}:u1:c1"
    with _session() as session:
        record = EventRecord(source=source, source_user_id="u1", conversation_id="c1",
                             operation="create", title="原日程", status="success",
                             bot_message_id="42",
                             event_json=json.dumps({"title": "原日程", "start_time": "2026-09-11T10:00:00+08:00"}))
        session.add(record)
        session.commit()
        if state == "draft":
            mp._pending_drafts[draft_key] = {"ts": time.time(), "event": {"title": "待补充日程"}}
        if state == "ambiguity":
            mp._pending_ambiguities[ambiguity_key] = {"ts": time.time(), "action": "delete", "candidate_ids": [record.id]}
        extractor = AsyncMock()
        extractor.extract.return_value = ExtractionResult(intent=Intent.no_event)
        extractor.merge_draft.return_value = ExtractionResult(intent=Intent.no_event)
        extractor.modify.return_value = ExtractionResult(
            intent=Intent.update_event,
            event=CalendarEvent(title="AI 解释后的标题", start_time="2026-09-11T10:00:00+08:00"),
        )
        try:
            replies = await _route(session, ctx, text, extractor, _caldav(), SettingsService(session))
            getattr(extractor, method).assert_awaited_once()
            assert getattr(extractor, method).await_args.args[-1] == text
            assert "不支持撤销" not in replies[0][0]
            assert not session.execute(select(EventRecord).where(EventRecord.operation == "delete")).scalars().all()
        finally:
            mp._pending_drafts.pop(draft_key, None)
            mp._pending_ambiguities.pop(ambiguity_key, None)


@pytest.mark.anyio
async def test_normal_recovery_activity_is_still_extracted():
    with _session() as session:
        extractor = AsyncMock()
        extractor.extract.return_value = ExtractionResult(
            intent=Intent.create_event,
            event=CalendarEvent(title="恢复训练", start_time="2026-09-11T10:00:00+08:00"),
        )
        replies = await _route(session, _ctx(), "恢复训练安排在明天十点", extractor, _caldav(), SettingsService(session))
        extractor.extract.assert_awaited_once()
        assert replies[0][1] is not None
        assert session.get(EventRecord, replies[0][1]).title == "恢复训练"


@pytest.mark.anyio
async def test_normal_delete_does_not_read_remote_snapshot():
    from app.channels import message_processor as mp
    with _session() as session:
        record = EventRecord(source="telegram", operation="create", title="会议", status="success",
                             caldav_uid="uid", caldav_href="/uid.ics")
        session.add(record)
        session.commit()
        caldav = AsyncMock()
        caldav.delete_event.return_value = True
        with patch.object(mp, "CalDAVService", return_value=caldav):
            reply = await mp._do_delete_with(session, _ctx(), record, _caldav(True))
        assert "已删除" in reply
        caldav.delete_event.assert_awaited_once()
        caldav.get_event.assert_not_awaited()


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


def _caldav(enabled: bool = False) -> dict[str, object]:
    if not enabled:
        return {
            "url": "",
            "user": "",
            "pw": "",
            "cal": "",
            "rem": 30,
            "dur": 60,
            "ssl": True,
        }
    return {
        "url": "https://caldav.example.com/dav",
        "user": "testuser",
        "pw": "testpass",
        "cal": "https://caldav.example.com/dav/calendars/work",
        "rem": 30,
        "dur": 60,
        "ssl": True,
    }


def test_caldav_service_extract_event_info():
    svc = CalDAVService()

    class FakeObj:
        id = "event-uid-999"
        url = "https://caldav.example.com/cal/999.ics"
        etag = '"etag-abc"'
        data = b"BEGIN:VCALENDAR\nSUMMARY:Test\nEND:VCALENDAR"

    info = svc._extract_event_info(FakeObj())
    assert info["uid"] == "event-uid-999"
    assert info["href"] == "https://caldav.example.com/cal/999.ics"
    assert info["etag"] == "etag-abc"
    assert "SUMMARY:Test" in info["data"]


def test_caldav_service_extract_event_info_fallback_hash():
    svc = CalDAVService()

    class FakeObjNoEtag:
        id = "event-uid-888"
        url = "https://caldav.example.com/cal/888.ics"
        etag = None
        props = {}
        data = "BEGIN:VCALENDAR\nSUMMARY:HashFallback\nEND:VCALENDAR"

    info = svc._extract_event_info(FakeObjNoEtag())
    assert info["uid"] == "event-uid-888"
    assert info["etag"].startswith("sha256:")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_get_event_confirmed_not_found_is_none(anyio_backend):
    from caldav.lib.error import NotFoundError
    client = MagicMock()
    cal = MagicMock()
    client.get_calendars.return_value = [cal]
    cal.event_by_uid.side_effect = NotFoundError("missing")
    cal.objects.return_value = []
    with patch("app.services.caldav_service._DAVClient", return_value=client):
        assert await CalDAVService().get_event("https://example.test", "test", "test", uid="missing") is None


@pytest.mark.anyio
@pytest.mark.parametrize("source,conversation", [("telegram", "c2"), ("discord", "c1")])
async def test_ordinary_reply_rejects_foreign_message_without_binding(source, conversation):
    from app.channels.message_processor import _find_target
    with _session() as session:
        session.add(EventRecord(source=source, source_user_id="u1", conversation_id=conversation,
                                operation="create", title="Foreign", status="success", event_id="foreign",
                                bot_message_id="42", created_at=datetime.now(timezone.utc)))
        session.commit()
        assert await _find_target(session, _ctx(reply_to_message_id="42")) is None


def test_batch_identity_includes_conversation_and_is_stable():
    from app.channels.message_processor import _generate_batch_id, derive_batch_uid
    a = _ctx(conversation_id="A", source_message_id="42")
    b = _ctx(conversation_id="B", source_message_id="42")
    first = _generate_batch_id(a, "meeting")
    assert first == _generate_batch_id(a, "meeting")
    assert first != _generate_batch_id(b, "meeting")
    assert derive_batch_uid(first, 0) != derive_batch_uid(_generate_batch_id(b, "meeting"), 0)


@pytest.mark.anyio
async def test_message_binding_keeps_multiple_list_replies_separate():
    from app.channels.message_bindings import bind_bot_message, resolve_bot_message
    with _session() as session:
        a = EventRecord(source="telegram", conversation_id="A", operation="create", status="success", title="A")
        b = EventRecord(source="telegram", conversation_id="B", operation="create", status="success", title="B")
        session.add_all([a, b]); session.commit()
        bind_bot_message(session, a.id, "42", source="telegram", conversation_id="A")
        bind_bot_message(session, b.id, "42", source="telegram", conversation_id="B")
        bind_bot_message(session, a.id, "43", source="telegram", conversation_id="A")
        session.commit()
        assert resolve_bot_message(session, "telegram", "A", "42").id == a.id
        assert resolve_bot_message(session, "telegram", "A", "43").id == a.id
        assert resolve_bot_message(session, "telegram", "B", "42").id == b.id
        assert resolve_bot_message(session, "discord", "A", "42") is None


@pytest.mark.anyio
async def test_legacy_colliding_batch_is_blocked_in_chat_and_web():
    from app.channels.message_processor import _handle_batch_retry
    from app.web.routes import retry_batch_events
    with _session() as session:
        for conversation in ["c1", "c2"]:
            session.add(EventRecord(source="telegram", source_user_id="u1", conversation_id=conversation,
                                    batch_id="legacy", batch_index=0, operation="create", status="failed",
                                    title="Fixture", created_at=datetime.now(timezone.utc)))
        session.commit()
        with patch("app.channels.message_processor._write_caldav", new_callable=AsyncMock) as writer:
            chat = await _handle_batch_retry(session, _ctx(), _caldav(True))
            web = await retry_batch_events("legacy", MagicMock(), None, session)
        assert "多个会话" in chat[0][0]
        assert web.status_code == 409
        writer.assert_not_awaited()
