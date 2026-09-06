import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels.message_processor import (
    ChannelContext,
    _do_delete_with,
    _do_modify_with,
    _do_undo,
    _is_undo_command,
    _record,
    _route,
    _write_one,
)
from app.db.models import Base, EventRecord
from app.services.caldav_service import CalDAVService
from app.services.settings_service import SettingsService


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


class _FakeExtractor:
    def __init__(self, intent: Intent = Intent.no_event, event: CalendarEvent | None = None):
        self.intent = intent
        self.event = event

    async def extract(self, text: str) -> ExtractionResult:
        return ExtractionResult(intent=self.intent, event=self.event)

    async def modify(self, existing: dict, text: str) -> ExtractionResult:
        return ExtractionResult(intent=self.intent, event=self.event)

    async def merge_draft(self, existing: dict, text: str) -> ExtractionResult:
        return ExtractionResult(intent=self.intent, event=self.event)


# ==============================================================================
# 1. 命令模式测试
# ==============================================================================

def test_is_undo_command_matches_common_variations():
    assert _is_undo_command("撤销") is True
    assert _is_undo_command("undo") is True
    assert _is_undo_command("UNDO") is True
    assert _is_undo_command("/undo") is True
    assert _is_undo_command("恢复") is True
    assert _is_undo_command("撤回") is True
    assert _is_undo_command("回退") is True
    assert _is_undo_command("撤销上一步") is True
    assert _is_undo_command("撤销修改") is True
    assert _is_undo_command("撤销删除") is True
    assert _is_undo_command("撤销创建") is True
    assert _is_undo_command("撤销 周会") is True
    assert _is_undo_command("恢复周会") is True
    assert _is_undo_command("撤销：项目会议") is True

    assert _is_undo_command("明天下午开会") is False
    assert _is_undo_command("把周会改到下午三点") is False
    assert _is_undo_command("删除周会") is False


# ==============================================================================
# 2. 本地撤销逆向补偿 (create -> delete, update -> restore, delete -> create)
# ==============================================================================

@pytest.mark.anyio
async def test_undo_create_reverses_to_delete_local():
    session = _session()
    ctx = _ctx()
    caldav = _caldav()

    event = CalendarEvent(title="周会", start_time="2026-06-01T10:00:00+08:00")
    rec_id, _ = await _write_one(session, ctx, "明天开周会", event, caldav)
    session.commit()

    rec = session.get(EventRecord, rec_id)
    assert rec is not None
    assert rec.operation == "create"
    assert rec.status == "success"

    # 执行撤销
    replies = await _do_undo(session, ctx, "撤销", caldav)
    assert len(replies) == 1
    reply_text, undo_rec_id = replies[0]
    assert "已撤销创建" in reply_text
    assert "已从日历中删除" in reply_text

    undo_rec = session.get(EventRecord, undo_rec_id)
    assert undo_rec is not None
    assert undo_rec.operation == "delete"
    assert undo_rec.status == "success"
    assert undo_rec.event_id == rec.event_id
    assert undo_rec.error_message == "[UNDO] 成功撤销创建日程"


@pytest.mark.anyio
async def test_undo_update_restores_snapshot_local():
    session = _session()
    ctx = _ctx()
    caldav = _caldav()

    # 1. 创建初始日程
    init_event = CalendarEvent(title="原始周会", start_time="2026-06-01T10:00:00+08:00", location="会议室A")
    rec_id, _ = await _write_one(session, ctx, "开周会", init_event, caldav)
    session.commit()
    init_rec = session.get(EventRecord, rec_id)

    # 2. 修改日程
    new_event_dict = {
        "title": "修改后的周会",
        "start_time": "2026-06-01T14:00:00+08:00",
        "location": "会议室B",
    }
    upd_rec_id, _ = await _do_modify_with(session, ctx, "改到下午两点在B", init_rec, new_event_dict, caldav)
    session.commit()
    upd_rec = session.get(EventRecord, upd_rec_id)
    assert upd_rec.operation == "update"
    assert upd_rec.snapshot_json is not None
    assert "原始周会" in upd_rec.snapshot_json

    # 3. 撤销修改
    replies = await _do_undo(session, ctx, "撤销", caldav)
    assert len(replies) == 1
    reply_text, undo_rec_id = replies[0]
    assert "已撤销修改" in reply_text
    assert "原始周会" in reply_text

    undo_rec = session.get(EventRecord, undo_rec_id)
    assert undo_rec.operation == "update"
    assert undo_rec.title == "原始周会"
    assert undo_rec.start_time == "2026-06-01T10:00:00+08:00"
    assert undo_rec.error_message == "[UNDO] 成功撤销修改，已恢复历史快照"


@pytest.mark.anyio
async def test_undo_delete_reverses_to_create_local():
    session = _session()
    ctx = _ctx()
    caldav = _caldav()

    # 1. 创建日程
    event = CalendarEvent(title="将被删除的日程", start_time="2026-06-01T10:00:00+08:00")
    rec_id, _ = await _write_one(session, ctx, "开会", event, caldav)
    session.commit()
    init_rec = session.get(EventRecord, rec_id)

    # 2. 删除日程
    await _do_delete_with(session, ctx, init_rec, caldav)
    del_rec = session.execute(
        select(EventRecord).where(EventRecord.event_id == init_rec.event_id, EventRecord.operation == "delete")
    ).scalar()
    assert del_rec is not None
    assert del_rec.snapshot_json is not None

    # 3. 撤销删除
    replies = await _do_undo(session, ctx, "恢复", caldav)
    assert len(replies) == 1
    reply_text, undo_rec_id = replies[0]
    assert "已撤销删除" in reply_text
    assert "将被删除的日程" in reply_text

    undo_rec = session.get(EventRecord, undo_rec_id)
    assert undo_rec.operation == "create"
    assert undo_rec.title == "将被删除的日程"
    assert undo_rec.error_message == "[UNDO] 成功撤销删除，已重新创建日程"


# ==============================================================================
# 3. TTL 与边界条件测试
# ==============================================================================

@pytest.mark.anyio
async def test_undo_ttl_expiration():
    session = _session()
    ctx = _ctx()
    caldav = _caldav()

    event = CalendarEvent(title="超时日程", start_time="2026-06-01T10:00:00+08:00")
    rec_id, _ = await _write_one(session, ctx, "开会", event, caldav)
    rec = session.get(EventRecord, rec_id)
    # 模拟 11 分钟前（超过 600 秒 TTL）
    rec.created_at = datetime.now(timezone.utc) - timedelta(seconds=660)
    session.commit()

    replies = await _do_undo(session, ctx, "撤销", caldav)
    assert len(replies) == 1
    assert "已超过 10 分钟撤销时效" in replies[0][0]


@pytest.mark.anyio
async def test_undo_superseded_operation_blocked():
    session = _session()
    ctx = _ctx()
    caldav = _caldav()

    # 1. 创建日程
    event = CalendarEvent(title="会议", start_time="2026-06-01T10:00:00+08:00")
    rec_id, _ = await _write_one(session, ctx, "开会", event, caldav)
    session.commit()
    first_rec = session.get(EventRecord, rec_id)

    # 2. 修改日程（产生了第二条记录）
    upd_rec_id, _ = await _do_modify_with(session, ctx, "改到11点", first_rec, {"title": "会议", "start_time": "2026-06-01T11:00:00+08:00"}, caldav)
    session.commit()

    # 3. 如果试图针对旧的 first_rec 执行撤销（例如通过旧 message id），应提示已有更新操作
    first_rec.bot_message_id = "bot-msg-1"
    session.commit()
    old_ctx = _ctx(reply_to_message_id="bot-msg-1")

    replies = await _do_undo(session, old_ctx, "撤销", caldav)
    assert len(replies) == 1
    assert "已有更新的操作" in replies[0][0]


@pytest.mark.anyio
async def test_undo_no_candidate_returns_friendly_prompt():
    session = _session()
    ctx = _ctx()
    caldav = _caldav()

    replies = await _do_undo(session, ctx, "撤销", caldav)
    assert len(replies) == 1
    assert "没有可撤销的最近操作" in replies[0][0]


# ==============================================================================
# 4. 远端 CalDAV 状态校验与 ETag 乐观防覆盖
# ==============================================================================

@pytest.mark.anyio
async def test_undo_create_with_caldav_etag_match_deletes_remote():
    session = _session()
    ctx = _ctx()
    caldav = _caldav(enabled=True)

    event = CalendarEvent(title="CalDAV周会", start_time="2026-06-01T10:00:00+08:00")

    mock_caldav = AsyncMock()
    mock_caldav.create_event.return_value = {"uid": "uid-123", "href": "/dav/123.ics", "etag": "etag-v1"}
    mock_caldav.get_event.return_value = {"uid": "uid-123", "href": "/dav/123.ics", "etag": "etag-v1", "data": "BEGIN:VCALENDAR..."}
    mock_caldav.delete_event.return_value = True

    with patch("app.channels.message_processor.CalDAVService", return_value=mock_caldav):
        rec_id, _ = await _write_one(session, ctx, "开周会", event, caldav)
        session.commit()
        rec = session.get(EventRecord, rec_id)
        assert rec.remote_etag == "etag-v1"

        replies = await _do_undo(session, ctx, "撤销", caldav)
        assert len(replies) == 1
        assert "已撤销创建" in replies[0][0]

        # 校验调用了远端查询与删除
        mock_caldav.get_event.assert_called_once()
        mock_caldav.delete_event.assert_called_once_with(
            caldav["url"], caldav["user"], caldav["pw"],
            "uid-123", "/dav/123.ics", ssl_verify=True
        )


@pytest.mark.anyio
async def test_undo_create_blocked_when_remote_etag_mismatched():
    session = _session()
    ctx = _ctx()
    caldav = _caldav(enabled=True)

    event = CalendarEvent(title="外部被修改的日程", start_time="2026-06-01T10:00:00+08:00")

    mock_caldav = AsyncMock()
    mock_caldav.create_event.return_value = {"uid": "uid-456", "href": "/dav/456.ics", "etag": "etag-original"}
    # 模拟外部客户端（如手机日历）将日程修改，远端 ETag 变为 etag-modified-by-phone
    mock_caldav.get_event.return_value = {"uid": "uid-456", "href": "/dav/456.ics", "etag": "etag-modified-by-phone", "data": "BEGIN:VCALENDAR..."}
    mock_caldav.delete_event.return_value = True

    with patch("app.channels.message_processor.CalDAVService", return_value=mock_caldav):
        rec_id, _ = await _write_one(session, ctx, "开会", event, caldav)
        session.commit()

        replies = await _do_undo(session, ctx, "撤销", caldav)
        assert len(replies) == 1
        reply_text = replies[0][0]
        # 坚决阻断防覆盖
        assert "已在外部日历中被修改" in reply_text
        assert "为防覆盖最新内容，撤销操作已阻断" in reply_text

        # 确保未执行删除
        mock_caldav.delete_event.assert_not_called()


@pytest.mark.anyio
async def test_undo_update_blocked_when_remote_etag_mismatched():
    session = _session()
    ctx = _ctx()
    caldav = _caldav(enabled=True)

    # 构造一条 update 记录
    rec = EventRecord(
        source=ctx.source,
        source_user_id=ctx.source_user_id,
        conversation_id=ctx.conversation_id,
        operation="update",
        title="会议",
        status="success",
        caldav_uid="uid-upd-1",
        caldav_href="/dav/upd-1.ics",
        remote_etag="etag-v2",
        snapshot_json=json.dumps({"title": "旧会议", "start_time": "2026-06-01T10:00:00+08:00"}, ensure_ascii=False),
        event_json=json.dumps({"title": "新会议", "start_time": "2026-06-01T14:00:00+08:00"}, ensure_ascii=False),
        created_at=datetime.now(timezone.utc),
    )
    session.add(rec)
    session.commit()

    mock_caldav = AsyncMock()
    # 模拟远端 ETag 不一致（已被外部手机日历再次改动）
    mock_caldav.get_event.return_value = {"uid": "uid-upd-1", "href": "/dav/upd-1.ics", "etag": "etag-v3-external"}

    with patch("app.channels.message_processor.CalDAVService", return_value=mock_caldav):
        replies = await _do_undo(session, ctx, "撤销修改", caldav)
        assert len(replies) == 1
        assert "已在外部日历中被修改" in replies[0][0]
        assert "为防覆盖最新内容，撤销操作已阻断" in replies[0][0]


@pytest.mark.anyio
async def test_undo_delete_blocked_if_event_recreated_on_remote():
    session = _session()
    ctx = _ctx()
    caldav = _caldav(enabled=True)

    # 构造一条 delete 记录
    rec = EventRecord(
        source=ctx.source,
        source_user_id=ctx.source_user_id,
        conversation_id=ctx.conversation_id,
        operation="delete",
        title="已删会议",
        status="success",
        caldav_uid="uid-del-1",
        snapshot_json=json.dumps({"title": "已删会议", "start_time": "2026-06-01T10:00:00+08:00"}, ensure_ascii=False),
        created_at=datetime.now(timezone.utc),
    )
    session.add(rec)
    session.commit()

    mock_caldav = AsyncMock()
    # 远端居然存在该日程（说明被外部重新创建了）
    mock_caldav.get_event.return_value = {"uid": "uid-del-1", "href": "/dav/del-1.ics", "etag": "some-etag"}

    with patch("app.channels.message_processor.CalDAVService", return_value=mock_caldav):
        replies = await _do_undo(session, ctx, "恢复删除", caldav)
        assert len(replies) == 1
        assert "已存在该日程" in replies[0][0]
        assert "撤销删除已终止以防冲突" in replies[0][0]


# ==============================================================================
# 5. 端到端路由与持久化快照验证
# ==============================================================================

@pytest.mark.anyio
async def test_route_intercepts_undo_command_end_to_end():
    session = _session()
    ctx = _ctx()
    caldav = _caldav()
    svc = SettingsService(session)

    # 1. 模拟用户先成功创建日程
    event = CalendarEvent(title="周五周报", start_time="2026-06-05T17:00:00+08:00")
    await _write_one(session, ctx, "周五下午5点发周报", event, caldav)
    session.commit()

    extractor = _FakeExtractor(intent=Intent.no_event)

    # 2. 用户发送 "undo"
    replies = await _route(session, ctx, "undo", extractor, caldav, svc)
    assert len(replies) == 1
    assert "已撤销创建" in replies[0][0]
    assert "周五周报" in replies[0][0]

    # 3. 再次发送 "undo"，应无可用操作
    replies2 = await _route(session, ctx, "撤销", extractor, caldav, svc)
    assert len(replies2) == 1
    assert "没有可撤销的最近操作" in replies2[0][0]


@pytest.mark.anyio
async def test_undo_target_by_title_match():
    session = _session()
    ctx = _ctx()
    caldav = _caldav()

    event1 = CalendarEvent(title="设计评审", start_time="2026-06-05T10:00:00+08:00")
    event2 = CalendarEvent(title="技术周会", start_time="2026-06-05T14:00:00+08:00")

    await _write_one(session, ctx, "开设计评审", event1, caldav)
    await _write_one(session, ctx, "开技术周会", event2, caldav)
    session.commit()

    # 精确撤销指定标题的日程
    replies = await _do_undo(session, ctx, "撤销设计评审", caldav)
    assert len(replies) == 1
    assert "设计评审" in replies[0][0]
    assert "已从日历中删除" in replies[0][0]


@pytest.mark.anyio
async def test_undo_target_by_quote_reference():
    session = _session()
    caldav = _caldav()

    event = CalendarEvent(title="引用测试会议", start_time="2026-06-05T10:00:00+08:00")
    await _write_one(session, _ctx(), "开会", event, caldav)
    session.commit()

    quote_ctx = _ctx(quoted_text="📌 标题：引用测试会议\n🕒 时间：2026-06-05 10:00")
    replies = await _do_undo(session, quote_ctx, "撤销", caldav)
    assert len(replies) == 1
    assert "引用测试会议" in replies[0][0]


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
@pytest.mark.parametrize("foreign_source,foreign_conversation", [
    ("telegram", "c2"), ("discord", "c1"),
])
@pytest.mark.parametrize("local_message_id", ["42", "different"])
async def test_undo_reply_is_scoped_and_never_falls_back(foreign_source, foreign_conversation, local_message_id):
    with _session() as session:
        now = datetime.now(timezone.utc)
        local = EventRecord(source="telegram", source_user_id="u1", conversation_id="c1",
                            bot_message_id=local_message_id, event_id="local-event", operation="create",
                            title="Local", status="success", created_at=now - timedelta(seconds=1))
        foreign = EventRecord(source=foreign_source, source_user_id="u1", conversation_id=foreign_conversation,
                              bot_message_id="42", event_id="foreign-event", operation="create",
                              title="Foreign", status="success", created_at=now)
        session.add_all([local, foreign])
        session.commit()
        replies = await _do_undo(session, _ctx(reply_to_message_id="42"), "撤销", _caldav())
        undo_id = replies[0][1]
        if local_message_id == "42":
            assert session.get(EventRecord, undo_id).event_id == "local-event"
        else:
            assert undo_id is None
            assert len(session.execute(select(EventRecord)).scalars().all()) == 2


@pytest.mark.anyio
async def test_undo_reply_missing_conversation_does_not_match_other_user():
    with _session() as session:
        session.add(EventRecord(source="telegram", source_user_id="other", conversation_id=None,
                                bot_message_id="42", operation="create", title="Other",
                                status="success", created_at=datetime.now(timezone.utc)))
        session.commit()
        replies = await _do_undo(session, _ctx(conversation_id=None, reply_to_message_id="42"), "撤销", _caldav())
        assert replies[0][1] is None


@pytest.mark.anyio
async def test_undo_quote_does_not_cross_channels():
    with _session() as session:
        session.add(EventRecord(source="discord", source_user_id="u1", conversation_id="c1",
                                operation="create", title="周会", status="success",
                                created_at=datetime.now(timezone.utc)))
        session.commit()
        replies = await _do_undo(session, _ctx(quoted_text="📌 标题：周会\n🕒 时间：2026-06-05 10:00"), "撤销", _caldav())
        assert replies[0][1] is None


@pytest.mark.anyio
async def test_undo_unreadable_quote_does_not_undo_latest_event():
    with _session() as session:
        session.add(EventRecord(source="telegram", source_user_id="u1", conversation_id="c1",
                                operation="create", title="Unrelated", status="success",
                                created_at=datetime.now(timezone.utc)))
        session.commit()
        replies = await _do_undo(session, _ctx(quoted_text="unreadable quote"), "撤销", _caldav())
        assert replies[0][1] is None
        assert len(session.execute(select(EventRecord)).scalars().all()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("cleanup_ok", [True, False])
async def test_undo_update_delete_failure_compensates(cleanup_ok):
    with _session() as session:
        snapshot = json.dumps({"title": "Before", "start_time": "2026-06-01T10:00:00+08:00"})
        session.add(EventRecord(source="telegram", source_user_id="u1", conversation_id="c1",
                                operation="update", title="After", status="success", event_id="event",
                                caldav_uid="original", caldav_href="/original", remote_etag="v2",
                                snapshot_json=snapshot, event_json=snapshot,
                                created_at=datetime.now(timezone.utc)))
        session.commit()
        cal = AsyncMock()
        cal.get_event.return_value = {"etag": "v2"}
        cal.delete_event.side_effect = [False, cleanup_ok]
        with patch("app.channels.message_processor.CalDAVService", return_value=cal), patch(
            "app.channels.message_processor._write_caldav_dict", new=AsyncMock(return_value={"uid": "copy", "href": "/copy"})
        ):
            replies = await _do_undo(session, _ctx(), "撤销", _caldav(True))
        assert replies[0][1] is None
        assert "失败" in replies[0][0]
        assert cal.delete_event.await_args_list[0].args[3:5] == ("original", "/original")
        assert cal.delete_event.await_args_list[1].args[3:5] == ("copy", "/copy")
        records = session.execute(select(EventRecord)).scalars().all()
        assert not any(r.status == "success" and (r.error_message or "").startswith("[UNDO]") for r in records)
        if not cleanup_ok:
            assert records[-1].status == "failed"
            assert records[-1].caldav_uid == "copy"


@pytest.mark.anyio
@pytest.mark.parametrize("failure_at", ["discovery", "lookup", "listing", "load"])
async def test_undo_delete_blocks_real_service_query_failures(failure_at):
    with _session() as session:
        session.add(EventRecord(source="telegram", source_user_id="u1", conversation_id="c1",
                                operation="delete", title="Deleted", status="success", caldav_uid="original",
                                snapshot_json=json.dumps({"title": "Deleted", "start_time": "2026-06-01T10:00:00+08:00"}),
                                created_at=datetime.now(timezone.utc)))
        session.commit()
        client = MagicMock()
        cal = MagicMock()
        client.get_calendars.return_value = [cal]
        cal.event_by_uid.return_value = None
        cal.objects.return_value = []
        if failure_at == "discovery":
            client.get_calendars.side_effect = ConnectionError("test failure")
        elif failure_at == "lookup":
            cal.event_by_uid.side_effect = ConnectionError("test failure")
        elif failure_at == "listing":
            cal.objects.side_effect = ConnectionError("test failure")
        else:
            obj = MagicMock()
            obj.data = None
            obj.load.side_effect = ConnectionError("test failure")
            cal.event_by_uid.return_value = obj
        with patch("app.services.caldav_service._DAVClient", return_value=client), patch(
            "app.channels.message_processor._write_caldav_dict", new_callable=AsyncMock
        ) as writer:
            replies = await _do_undo(session, _ctx(), "撤销", _caldav(True))
        assert "撤销已阻断" in replies[0][0]
        assert replies[0][1] is None
        writer.assert_not_awaited()
        assert len(session.execute(select(EventRecord)).scalars().all()) == 1


@pytest.mark.anyio
async def test_get_event_confirmed_not_found_is_none():
    from caldav.lib.error import NotFoundError
    client = MagicMock()
    cal = MagicMock()
    client.get_calendars.return_value = [cal]
    cal.event_by_uid.side_effect = NotFoundError("missing")
    cal.objects.return_value = []
    with patch("app.services.caldav_service._DAVClient", return_value=client):
        assert await CalDAVService().get_event("https://example.test", "test", "test", uid="missing") is None
