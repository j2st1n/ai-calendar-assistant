import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.channels.commands import handle_command
from app.channels.message_processor import ChannelContext
from app.db.models import Base, EventRecord


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _ctx(source: str = "telegram", conversation_id: str = "c1") -> ChannelContext:
    return ChannelContext(source=source, source_user_id="u1", conversation_id=conversation_id, source_message_id="m1")


def _add_record(session, title: str, day_offset: int, *, source: str = "telegram",
                conversation_id: str = "c1", operation: str = "create") -> EventRecord:
    start = f"{date.today() + timedelta(days=day_offset)}T10:00:00+08:00"
    return _add_record_at(session, title, start, source=source, conversation_id=conversation_id, operation=operation)


def _add_record_at(session, title: str, start: str, *, source: str = "telegram",
                   conversation_id: str = "c1", operation: str = "create",
                   event_id: str | None = None) -> EventRecord:
    rec = EventRecord(
        source=source,
        source_user_id="u1",
        conversation_id=conversation_id,
        operation=operation,
        title=title,
        start_time=start,
        event_json=json.dumps({"title": title, "start_time": start, "end_time": start.replace("10:00", "11:00")}),
        status="success",
        event_id=event_id or title,
    )
    session.add(rec)
    session.commit()
    return rec


def test_list_returns_one_bound_reply_per_event_sorted_by_start():
    async def run():
        session = _session()
        later = _add_record(session, "后天会议", 2)
        earlier = _add_record(session, "明天会议", 1)

        replies = await handle_command(session, _ctx(), "/list")

        assert replies is not None
        assert [record_id for _, record_id in replies] == [earlier.id, later.id]
        assert "明天会议" in replies[0][0]
        assert "回复这条消息可修改或删除" in replies[0][0]

    asyncio.run(run())


def test_list_days_argument_filters_future_window():
    async def run():
        session = _session()
        included = _add_record(session, "三天内", 2)
        _ = _add_record(session, "三天外", 4)

        replies = await handle_command(session, _ctx(), "/list 3")

        assert replies is not None
        assert [record_id for _, record_id in replies] == [included.id]
        assert "三天内" in replies[0][0]
        assert "三天外" not in replies[0][0]

    asyncio.run(run())


def test_list_excludes_past_today_but_keeps_future_today():
    async def run():
        session = _session()
        now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        past = _add_record_at(session, "今天已过", "2026-06-05T18:00:00+08:00")
        future = _add_record_at(session, "今天未过", "2026-06-05T21:00:00+08:00")

        with patch("app.channels.commands.datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            mock_datetime.fromisoformat = datetime.fromisoformat
            mock_datetime.combine = datetime.combine
            replies = await handle_command(session, _ctx(), "/list")

        assert replies is not None
        assert [record_id for _, record_id in replies] == [future.id]
        text = "\n".join(response for response, _ in replies)
        assert "今天未过" in text
        assert "今天已过" not in text
        assert past.id != future.id

    asyncio.run(run())


def test_list_days_argument_includes_final_day_after_update():
    async def run():
        session = _session()
        now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
        event_id = "event-june-9"
        _ = _add_record_at(session, "原日程", "2026-06-08T10:00:00+08:00", event_id=event_id)
        updated = _add_record_at(session, "改后日程", "2026-06-09T22:00:00+08:00", operation="update", event_id=event_id)

        with patch("app.channels.commands.datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            mock_datetime.fromisoformat = datetime.fromisoformat
            mock_datetime.combine = datetime.combine
            replies = await handle_command(session, _ctx(), "/list 7")

        assert replies is not None
        assert [record_id for _, record_id in replies] == [updated.id]
        assert "改后日程" in replies[0][0]
        assert "2026-06-09 22:00" in replies[0][0]

    asyncio.run(run())


def test_list_invalid_days_falls_back_to_default_and_caps_large_values():
    async def run():
        session = _session()
        default_event = _add_record(session, "默认范围", 6)
        capped_event = _add_record(session, "十四天内", 13)
        _ = _add_record(session, "十四天外", 20)

        invalid_replies = await handle_command(session, _ctx(), "/list abc")
        capped_replies = await handle_command(session, _ctx(), "/list 999")

        assert invalid_replies is not None
        assert [record_id for _, record_id in invalid_replies] == [default_event.id]
        assert capped_replies is not None
        assert [record_id for _, record_id in capped_replies] == [default_event.id, capped_event.id]

    asyncio.run(run())


def test_list_is_global_across_conversations_and_sources_but_excludes_deleted_records():
    async def run():
        session = _session()
        visible = _add_record(session, "当前会话", 1)
        other_conversation = _add_record(session, "其它会话", 1, conversation_id="c2")
        other_source = _add_record(session, "其它平台", 1, source="discord")
        _ = _add_record(session, "已删除", 1, operation="delete")

        replies = await handle_command(session, _ctx(), "/list")

        assert replies is not None
        assert {record_id for _, record_id in replies} == {visible.id, other_conversation.id, other_source.id}
        text = "\n".join(response for response, _ in replies)
        assert "当前会话" in text
        assert "其它会话" in text
        assert "其它平台" in text
        assert "已删除" not in text

    asyncio.run(run())


def test_list_no_records_returns_single_unbound_message():
    async def run():
        replies = await handle_command(_session(), _ctx(), "/list")

        assert replies == [("📅 未来 7 天暂无生效日程", None)]

    asyncio.run(run())


def test_help_mentions_list_not_upcoming():
    async def run():
        replies = await handle_command(_session(), _ctx(), "/help")

        assert replies is not None
        assert "/list" in replies[0][0]
        assert "/upcoming" not in replies[0][0]

    asyncio.run(run())


def test_upcoming_is_not_a_command():
    async def run():
        replies = await handle_command(_session(), _ctx(), "/upcoming 7")

        assert replies is None

    asyncio.run(run())
