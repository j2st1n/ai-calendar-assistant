import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.extractor import EventExtractor
from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels.message_processor import (
    ChannelContext,
    _do_modify_with,
    _find_target,
    _format_modify_result,
    _handle_new,
    _parse_title_and_start_from_quote,
    _route,
)
from app.db.models import Base, EventRecord
from app.services.settings_service import SettingsService


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _ctx():
    return ChannelContext(source="telegram", source_user_id="u1", conversation_id="c1", source_message_id="m1")


def _caldav(**overrides):
    cfg = {"url": "", "user": "", "pw": "", "cal": "", "rem": 15, "dur": 60, "ssl": True}
    cfg.update(overrides)
    return cfg


def _event():
    return CalendarEvent(title="买药", start_time="2026-06-01T12:00:00+08:00")


def test_handle_new_system_error_uses_system_message_and_records_error():
    async def run():
        session = _session()
        result = ExtractionResult(
            intent=Intent.no_event,
            missing_fields=["JSON parse failed"],
            error_type="parse_error",
        )

        replies = await _handle_new(session, _ctx(), "坏输入", result, _caldav(), SettingsService(session))

        assert replies == [("⚠️ 系统处理失败，请稍后重试。", None)]
        record = session.query(EventRecord).one()
        assert record.status == "failed"
        assert record.error_message == "JSON parse failed"

    asyncio.run(run())


def test_handle_new_redacts_api_key_from_record():
    async def run():
        session = _session()
        result = ExtractionResult(intent=Intent.no_event)
        secret = "sk-" + ("a" * 48)

        _ = await _handle_new(session, _ctx(), secret, result, _caldav(), SettingsService(session))

        record = session.query(EventRecord).one()
        assert record.original_text == "[REDACTED]"
        assert secret not in (record.event_json or "")

    asyncio.run(run())


def test_handle_new_caldav_not_configured_keeps_success_message():
    async def run():
        session = _session()
        result = ExtractionResult(intent=Intent.create_event, event=_event())

        replies = await _handle_new(session, _ctx(), "买药", result, _caldav(), SettingsService(session))

        assert replies[0][0].startswith("✅ 日程已安排好啦！")
        record = session.query(EventRecord).one()
        assert record.status == "success"
        assert record.caldav_uid is None

    asyncio.run(run())


def test_handle_new_caldav_write_failure_uses_warning_message(monkeypatch):
    async def run():
        async def fail_caldav(*_args, **_kwargs):
            from app.services.caldav_service import CalDAVServiceError
            raise CalDAVServiceError("创建事件失败")

        import app.channels.message_processor as mp

        monkeypatch.setattr(mp, "_write_caldav", fail_caldav)
        session = _session()
        result = ExtractionResult(intent=Intent.create_event, event=_event())

        replies = await _handle_new(session, _ctx(), "买药", result, _caldav(url="https://caldav.example", user="u"), SettingsService(session))

        assert replies[0][0].startswith("⚠️ 日程已记录到本地，但日历同步失败")
        record = session.query(EventRecord).one()
        assert record.status == "failed"
        assert record.error_message == "创建事件失败"

    asyncio.run(run())


def test_handle_new_schema_error_with_create_intent_uses_system_message():
    async def run():
        session = _session()
        result = ExtractionResult(
            intent=Intent.create_event,
            missing_fields=["1 validation error for ExtractionResult"],
            error_type="schema_error",
        )

        replies = await _handle_new(session, _ctx(), "买药", result, _caldav(), SettingsService(session))

        assert replies == [("⚠️ 系统处理失败，请稍后重试。", None)]
        record = session.query(EventRecord).one()
        assert record.status == "failed"
        assert record.error_message == "1 validation error for ExtractionResult"

    asyncio.run(run())


def test_do_modify_creates_new_before_deleting_old(monkeypatch):
    async def run():
        calls = []

        async def create_new(_event, _caldav):
            calls.append("create")
            return {"uid": "new-uid", "href": "new-href"}

        class FakeCalDAVService:
            async def delete_event(self, *_args, **_kwargs):
                calls.append("delete")
                return True

        import app.channels.message_processor as mp

        monkeypatch.setattr(mp, "_write_caldav_dict", create_new)
        monkeypatch.setattr(mp, "CalDAVService", FakeCalDAVService)
        session = _session()
        target = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="旧日程",
            start_time="2026-06-01T12:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "旧日程", "start_time": "2026-06-01T12:00:00+08:00"}),
            caldav_uid="old-uid",
            caldav_href="old-href",
            event_id="event-1",
        )
        session.add(target)
        session.flush()

        rec_id, warning = await _do_modify_with(
            session, _ctx(), "改", target,
            {"title": "新日程", "start_time": "2026-06-02T12:00:00+08:00"},
            _caldav(url="https://caldav.example", user="u"),
        )

        assert warning is None
        assert calls == ["create", "delete"]
        assert target.caldav_uid == "old-uid"
        assert target.caldav_href == "old-href"
        record = session.get(EventRecord, rec_id)
        assert record is not None
        assert record.status == "success"
        assert record.caldav_uid == "new-uid"
        assert record.caldav_href == "new-href"

    asyncio.run(run())


def test_do_modify_preserves_old_event_when_new_create_fails(monkeypatch):
    async def run():
        calls = []

        async def create_new(_event, _caldav):
            calls.append("create")
            return None

        class FakeCalDAVService:
            async def delete_event(self, *_args, **_kwargs):
                calls.append("delete")
                return True

        import app.channels.message_processor as mp

        monkeypatch.setattr(mp, "_write_caldav_dict", create_new)
        monkeypatch.setattr(mp, "CalDAVService", FakeCalDAVService)
        session = _session()
        target = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="旧日程",
            start_time="2026-06-01T12:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "旧日程", "start_time": "2026-06-01T12:00:00+08:00"}),
            caldav_uid="old-uid",
            caldav_href="old-href",
            event_id="event-1",
        )
        session.add(target)
        session.flush()

        rec_id, warning = await _do_modify_with(
            session, _ctx(), "改", target,
            {"title": "新日程", "start_time": "2026-06-02T12:00:00+08:00"},
            _caldav(url="https://caldav.example", user="u"),
        )

        assert warning == "⚠️ 日程已记录到本地，但日历同步可能失败"
        assert calls == ["create"]
        assert target.caldav_uid == "old-uid"
        assert target.caldav_href == "old-href"
        record = session.get(EventRecord, rec_id)
        assert record is not None
        assert record.status == "failed"
        assert record.error_message == "CalDAV 新日程创建失败，原日程已保留"

    asyncio.run(run())


def test_do_modify_preserves_old_event_when_new_create_raises(monkeypatch):
    async def run():
        calls = []

        async def create_new(_event, _caldav):
            calls.append("create")
            from app.services.caldav_service import CalDAVServiceError
            raise CalDAVServiceError("创建事件失败")

        class FakeCalDAVService:
            async def delete_event(self, *_args, **_kwargs):
                calls.append("delete")
                return True

        import app.channels.message_processor as mp

        monkeypatch.setattr(mp, "_write_caldav_dict", create_new)
        monkeypatch.setattr(mp, "CalDAVService", FakeCalDAVService)
        session = _session()
        target = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="旧日程",
            start_time="2026-06-01T12:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "旧日程", "start_time": "2026-06-01T12:00:00+08:00"}),
            caldav_uid="old-uid",
            caldav_href="old-href",
            event_id="event-1",
        )
        session.add(target)
        session.flush()

        rec_id, warning = await _do_modify_with(
            session, _ctx(), "改", target,
            {"title": "新日程", "start_time": "2026-06-02T12:00:00+08:00"},
            _caldav(url="https://caldav.example", user="u"),
        )

        assert warning == "⚠️ 日程已记录到本地，但日历同步可能失败"
        assert calls == ["create"]
        assert target.caldav_uid == "old-uid"
        assert target.caldav_href == "old-href"
        record = session.get(EventRecord, rec_id)
        assert record is not None
        assert record.status == "failed"
        assert record.error_message is not None
        assert "原日程已保留" in record.error_message

    asyncio.run(run())


def test_do_modify_warns_precisely_when_old_delete_fails(monkeypatch):
    async def run():
        calls = []

        async def create_new(_event, _caldav):
            calls.append("create")
            return {"uid": "new-uid", "href": "new-href"}

        class FakeCalDAVService:
            async def delete_event(self, *_args, **_kwargs):
                calls.append("delete")
                return False

        import app.channels.message_processor as mp

        monkeypatch.setattr(mp, "_write_caldav_dict", create_new)
        monkeypatch.setattr(mp, "CalDAVService", FakeCalDAVService)
        session = _session()
        target = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="旧日程",
            start_time="2026-06-01T12:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "旧日程", "start_time": "2026-06-01T12:00:00+08:00"}),
            caldav_uid="old-uid",
            caldav_href="old-href",
            event_id="event-1",
        )
        session.add(target)
        session.flush()

        rec_id, warning = await _do_modify_with(
            session, _ctx(), "改", target,
            {"title": "新日程", "start_time": "2026-06-02T12:00:00+08:00"},
            _caldav(url="https://caldav.example", user="u"),
        )

        assert calls == ["create", "delete"]
        assert warning == "⚠️ 日程已更新，但旧日程删除失败，可能出现重复日程"
        assert _format_modify_result({"title": "新日程"}, warning).startswith(warning)
        record = session.get(EventRecord, rec_id)
        assert record is not None
        assert record.status == "failed"
        assert record.error_message == "旧日程删除失败，可能产生重复日程"

    asyncio.run(run())


def test_find_target_ignores_non_reply_events_older_than_24h():
    async def run():
        session = _session()
        old = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="旧日程",
            start_time="2026-06-01T12:00:00+08:00",
            status="success",
            event_json="{}",
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        session.add(old)
        session.commit()

        assert await _find_target(session, _ctx()) is None

    asyncio.run(run())


def test_find_target_keeps_recent_non_reply_event():
    async def run():
        session = _session()
        recent = EventRecord(
            source="telegram",
            source_user_id="u1",
            conversation_id="c1",
            operation="create",
            title="近日程",
            start_time="2026-06-01T12:00:00+08:00",
            status="success",
            event_json="{}",
            created_at=datetime.now(timezone.utc),
        )
        session.add(recent)
        session.commit()

        found = await _find_target(session, _ctx())

        assert found is not None
        assert found.title == "近日程"

    asyncio.run(run())


# ── _parse_title_and_start_from_quote ──────────────────────────

def test_parse_title_and_start_from_quote_success():
    text = "✅ 日程已安排好啦！\n\n📌 标题：测试\n🕒 时间：2026-06-02 15:00 - 16:00\n⏰ 提醒：提前 15 分钟"
    title, start = _parse_title_and_start_from_quote(text)
    assert title == "测试"
    assert start == "2026-06-02T15:00"


def test_parse_title_and_start_from_quote_missing_title_returns_none():
    text = "✅ 日程已安排好啦！\n\n🕒 时间：2026-06-02 15:00 - 16:00"
    title, start = _parse_title_and_start_from_quote(text)
    assert title is None
    assert start == "2026-06-02T15:00"


def test_parse_title_and_start_from_quote_missing_time_returns_none():
    text = "✅ 日程已安排好啦！\n\n📌 标题：测试"
    title, start = _parse_title_and_start_from_quote(text)
    assert title == "测试"
    assert start is None


def test_parse_title_and_start_from_quote_garbage_returns_nones():
    title, start = _parse_title_and_start_from_quote("一些无关文本")
    assert title is None
    assert start is None


# ── _find_target with quoted_text ───────────────────────────────

def _wechat_ctx(**overrides):
    return ChannelContext(
        source="wechat",
        source_user_id="u1",
        conversation_id="u1",
        **overrides,
    )


def _seed_event(session, title="测试", start_time="2026-06-02T15:00:00+08:00", operation="create", **kw):
    rec = EventRecord(
        source="wechat",
        source_user_id="u1",
        conversation_id="u1",
        operation=operation,
        title=title,
        start_time=start_time,
        status="success",
        event_json=json.dumps({"title": title, "start_time": start_time}),
        **kw,
    )
    session.add(rec)
    session.flush()
    return rec


QUOTE_TEXT = "✅ 日程已安排好啦！\n\n📌 标题：测试\n🕒 时间：2026-06-02 15:00 - 16:00\n⏰ 提醒：提前 15 分钟"
QUOTE_TEXT_OTHER = "✅ 日程已安排好啦！\n\n📌 标题：其他\n🕒 时间：2026-06-02 16:00 - 17:00\n⏰ 提醒：提前 15 分钟"


def test_find_target_by_quoted_text_finds_matching_event():
    async def run():
        session = _session()
        _ = _seed_event(session)
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT)
        found = await _find_target(session, ctx)
        assert found is not None
        assert found.title == "测试"
    asyncio.run(run())


def test_find_target_by_quoted_text_returns_none_when_two_match():
    async def run():
        session = _session()
        _ = _seed_event(session)
        _ = _seed_event(session)  # same title & same start_time → both match
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT)
        found = await _find_target(session, ctx)
        assert found is None
    asyncio.run(run())


def test_find_target_by_quoted_text_returns_none_when_no_match():
    async def run():
        session = _session()
        _ = _seed_event(session, title="其他", start_time="2026-06-02T16:00:00+08:00")
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT)
        found = await _find_target(session, ctx)
        assert found is None
    asyncio.run(run())


def test_route_with_unmatched_quoted_text_does_not_extract_as_new_input():
    async def run():
        session = _session()
        _ = _seed_event(session, title="其他", start_time="2026-06-02T16:00:00+08:00")
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT)
        svc = SettingsService(session)
        with patch.object(svc, "get", return_value=""):
            replies = await _route(
                session, ctx, "删除",
                extractor=_FakeExtractor(intent=Intent.no_event),
                caldav={"url": "", "user": "", "pw": "", "cal": "",
                        "rem": 15, "dur": 60, "ssl": True},
                svc=svc,
            )
        assert replies == [("🤔 没有找到引用消息对应的日程。请引用我发送的日程确认消息，或说“删除xx日程”。", None)]
        records = session.query(EventRecord).filter(EventRecord.operation == "delete").all()
        assert records == []
    asyncio.run(run())


def test_route_with_unreadable_quote_does_not_fall_back_to_recent_event():
    async def run():
        session = _session()
        _ = _seed_event(
            session,
            start_time=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        session.commit()
        ctx = _wechat_ctx(quote_reference_present=True)
        svc = SettingsService(session)

        with patch.object(svc, "get", return_value=""):
            replies = await _route(
                session, ctx, "删除",
                extractor=_FakeExtractor(intent=Intent.delete_event),
                caldav=_caldav(),
                svc=svc,
            )

        assert replies == [(
            "🤔 检测到微信引用，但无法读取被引用的日程。请重新引用我发送的日程确认消息。",
            None,
        )]
        assert session.query(EventRecord).filter(EventRecord.operation == "delete").count() == 0

    asyncio.run(run())


def test_find_target_quoted_text_does_not_fall_back_to_recent():
    async def run():
        session = _session()
        _ = _seed_event(session, title="近日程",
                     start_time=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT_OTHER)
        found = await _find_target(session, ctx)
        assert found is None
    asyncio.run(run())


def test_find_target_reply_to_still_works_for_telegram():
    async def run():
        session = _session()
        _ = _seed_event(session)
        session.commit()
        ctx = ChannelContext(source="telegram", source_user_id="u1", conversation_id="c1",
                             reply_to_message_id="some-bot-msg-id")
        with patch.object(session, "execute") as mock_exec:
            mock_exec.return_value.scalar.return_value = None
            found = await _find_target(session, ctx)
            assert found is None
    asyncio.run(run())


def test_find_target_reply_to_list_item_works_across_source_and_conversation():
    async def run():
        session = _session()
        target = EventRecord(
            source="discord",
            source_user_id="u1",
            conversation_id="discord-channel",
            operation="create",
            title="测试",
            start_time="2026-06-02T15:00:00+08:00",
            status="success",
            event_json=json.dumps({"title": "测试", "start_time": "2026-06-02T15:00:00+08:00"}),
            bot_message_id="list-item-msg",
        )
        session.add(target)
        session.commit()
        ctx = ChannelContext(
            source="telegram",
            source_user_id="u1",
            conversation_id="telegram-chat",
            reply_to_message_id="list-item-msg",
        )

        found = await _find_target(session, ctx)

        assert found is not None
        assert found.id == target.id

    asyncio.run(run())


def test_find_target_by_old_quote_after_modify_returns_latest_event():
    async def run():
        session = _session()
        _ = _seed_event(
            session,
            title="测试",
            start_time="2026-06-02T15:00:00+08:00",
            event_id="event-1",
            caldav_uid="old-uid",
        )
        latest = _seed_event(
            session,
            title="测试",
            start_time="2026-06-02T16:00:00+08:00",
            operation="update",
            event_id="event-1",
            caldav_uid="new-uid",
        )
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT)

        found = await _find_target(session, ctx)

        assert found is not None
        assert found.id == latest.id

    asyncio.run(run())


def test_find_target_by_new_quote_after_modify_returns_latest_event_without_ambiguity():
    async def run():
        session = _session()
        _ = _seed_event(
            session,
            title="测试",
            start_time="2026-06-02T16:00:00+08:00",
            event_id="event-1",
            caldav_uid="old-uid",
        )
        latest = _seed_event(
            session,
            title="测试",
            start_time="2026-06-02T16:00:00+08:00",
            operation="update",
            event_id="event-1",
            caldav_uid="new-uid",
        )
        session.commit()
        quote = "✅ 日程已更新！\n\n📌 标题：测试\n🕒 时间：2026-06-02 16:00 - 17:00"
        ctx = _wechat_ctx(quoted_text=quote)

        found = await _find_target(session, ctx)

        assert found is not None
        assert found.id == latest.id

    asyncio.run(run())


def test_find_target_by_old_reply_after_modify_returns_latest_event():
    async def run():
        session = _session()
        old = _seed_event(
            session,
            title="测试",
            start_time="2026-06-02T15:00:00+08:00",
            event_id="event-1",
            caldav_uid="old-uid",
            bot_message_id="old-bot-msg",
        )
        latest = _seed_event(
            session,
            title="测试",
            start_time="2026-06-02T16:00:00+08:00",
            operation="update",
            event_id="event-1",
            caldav_uid="new-uid",
            bot_message_id="new-bot-msg",
        )
        session.commit()
        ctx = ChannelContext(source="wechat", source_user_id="u1", conversation_id="u1", reply_to_message_id="old-bot-msg")

        found = await _find_target(session, ctx)

        assert old.id != latest.id
        assert found is not None
        assert found.id == latest.id

    asyncio.run(run())


# ── _route with quoted_text (end-to-end-ish) ────────────────────


class _FakeExtractor(EventExtractor):
    def __init__(self, intent=Intent.update_event, event=None):
        self._intent = intent
        self._event = event

    async def extract(self, _text: str) -> ExtractionResult:
        return ExtractionResult(intent=self._intent, event=self._event)

    async def modify(self, _existing_event: dict, _instruction: str) -> ExtractionResult:
        return ExtractionResult(intent=self._intent, event=self._event)

    async def merge_draft(self, _draft: dict, _new_input: str) -> ExtractionResult:
        return ExtractionResult(intent=self._intent, event=self._event)


class _FailingModifyExtractor(_FakeExtractor):
    async def modify(self, _existing_event: dict, _instruction: str) -> ExtractionResult:
        raise AssertionError("quoted quick modify should not call AI modify")


def test_route_with_quoted_text_modifies_quoted_event():
    async def run():
        session = _session()
        _ = _seed_event(session)
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT)
        svc = SettingsService(session)
        with patch.object(svc, "get", return_value=""):
            new_event = CalendarEvent(title="测试", start_time="2026-06-02T16:00:00+08:00")
            extractor = _FakeExtractor(intent=Intent.update_event, event=new_event)
            replies = await _route(
                session, ctx, "改成4点",
                extractor=extractor,
                caldav={"url": "", "user": "", "pw": "", "cal": "",
                        "rem": 15, "dur": 60, "ssl": True},
                svc=svc,
            )
        assert len(replies) == 1
        text, _rid = replies[0]
        assert "已更新" in text
        update_records = session.query(EventRecord).filter(
            EventRecord.operation == "update",
        ).all()
        assert len(update_records) == 1
        updated_start = update_records[0].start_time
        assert updated_start is not None and "16:00" in updated_start
    asyncio.run(run())


def test_route_with_quoted_text_deletes_quoted_event():
    async def run():
        session = _session()
        _ = _seed_event(session)
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT)
        svc = SettingsService(session)
        with patch.object(svc, "get", return_value=""):
            extractor = _FakeExtractor(intent=Intent.delete_event)
            replies = await _route(
                session, ctx, "删掉日程",
                extractor=extractor,
                caldav={"url": "", "user": "", "pw": "", "cal": "",
                        "rem": 15, "dur": 60, "ssl": True},
                svc=svc,
            )
        assert len(replies) == 1
        text, _rid = replies[0]
        assert "已删除" in text
        del_recs = session.query(EventRecord).filter(
            EventRecord.operation == "delete",
            EventRecord.title == "测试",
        ).all()
        assert len(del_recs) == 1
    asyncio.run(run())


def test_route_with_quoted_text_deletes_on_bare_delete_word():
    async def run():
        session = _session()
        _ = _seed_event(session)
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT)
        svc = SettingsService(session)
        with patch.object(svc, "get", return_value=""):
            replies = await _route(
                session, ctx, "删除",
                extractor=_FakeExtractor(intent=Intent.no_event),
                caldav={"url": "", "user": "", "pw": "", "cal": "",
                        "rem": 15, "dur": 60, "ssl": True},
                svc=svc,
            )
        assert len(replies) == 1
        text, _rid = replies[0]
        assert "已删除" in text
        del_recs = session.query(EventRecord).filter(
            EventRecord.operation == "delete",
            EventRecord.title == "测试",
        ).all()
        assert len(del_recs) == 1
    asyncio.run(run())


def test_route_with_quoted_text_quick_modifies_full_width_colon_time():
    async def run():
        session = _session()
        _ = _seed_event(session)
        session.commit()
        ctx = _wechat_ctx(quoted_text=QUOTE_TEXT)
        svc = SettingsService(session)
        with patch.object(svc, "get", return_value=""):
            replies = await _route(
                session, ctx, "改成10：30",
                extractor=_FailingModifyExtractor(),
                caldav={"url": "", "user": "", "pw": "", "cal": "",
                        "rem": 15, "dur": 60, "ssl": True},
                svc=svc,
            )
        assert len(replies) == 1
        text, _rid = replies[0]
        assert "已更新" in text
        update_records = session.query(EventRecord).filter(
            EventRecord.operation == "update",
        ).all()
        assert len(update_records) == 1
        updated_start = update_records[0].start_time
        assert updated_start is not None and "22:30" in updated_start
    asyncio.run(run())
