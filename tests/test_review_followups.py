import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels import message_processor as mp
from app.channels.commands import _current_records, _global_current_records
from app.channels.message_bindings import bind_bot_message
from app.db.models import EventRecord
from app.services.settings_service import SettingsService
from tests.test_message_history import _session, _ctx, _caldav


def seed(session, title="项目周会", **kwargs):
    payload = {"title": title, "start_time": "2026-09-11T10:00:00+08:00", "end_time": "2026-09-11T11:00:00+08:00"}
    data = dict(source="telegram", source_user_id="u1", conversation_id="c1", operation="create",
                title=title, start_time=payload["start_time"], event_json=json.dumps(payload), status="success")
    data.update(kwargs)
    rec = EventRecord(**data)
    session.add(rec)
    session.commit()
    return rec


@pytest.mark.anyio
@pytest.mark.parametrize("intent", [Intent.delete_event, Intent.update_event])
@pytest.mark.parametrize("existing_title", ["项目周会", ""])
async def test_unmatched_title_never_mutates_unrelated_event(intent, existing_title):
    with _session() as s:
        seed(s, existing_title, caldav_uid="unrelated")
        extractor = AsyncMock()
        extractor.extract.return_value = ExtractionResult(intent=intent, event=CalendarEvent(title="牙医预约", start_time=""))
        with patch.object(mp, "CalDAVService") as remote:
            reply = await mp._route(s, _ctx(), "删除牙医预约" if intent == Intent.delete_event else "修改牙医预约", extractor, _caldav(True), SettingsService(s))
        remote.assert_not_called()
        assert "没有找到" in reply[0][0]
        assert len(s.execute(select(EventRecord)).scalars().all()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("history", ["failed_delete", "successful_delete", "restore", "failed_update"])
@pytest.mark.parametrize("identity", ["event_id", "caldav_uid"])
async def test_current_state_ignores_failed_operations_and_follows_restoration(history, identity):
    with _session() as s:
        common = {identity: "same-event", "created_at": datetime.now(timezone.utc)}
        original = seed(s, bot_message_id="42", **common)
        if history == "failed_update":
            seed(s, "失败的新标题", operation="update", status="failed", **common)
        else:
            seed(s, operation="delete", status="failed" if history == "failed_delete" else "success", **common)
        expected = original
        if history == "restore":
            expected = seed(s, "恢复的周会", **common)
        if history == "successful_delete":
            expected = None
        assert await mp._find_target(s, _ctx(reply_to_message_id="42")) is expected
        assert mp._get_active_recent_events(s, _ctx()) == ([expected] if expected else [])
        assert _current_records(s, _ctx()) == ([expected] if expected else [])
        assert _global_current_records(s) == ([expected] if expected else [])


@pytest.mark.anyio
async def test_cross_channel_history_follows_binding_and_later_successful_delete():
    with _session() as s:
        original = seed(s, source="discord", conversation_id="d1", event_id="shared")
        bind_bot_message(s, original.id, "42", source="telegram", conversation_id="c1")
        updated = seed(s, "新标题", operation="update", event_id="shared")
        assert await mp._find_target(s, _ctx(reply_to_message_id="42")) is updated
        seed(s, operation="delete", event_id="shared")
        assert await mp._find_target(s, _ctx(reply_to_message_id="42")) is None


@pytest.mark.anyio
@pytest.mark.parametrize("old_batch_status", ["success", "failed"])
async def test_new_failed_single_precedes_old_batch(old_batch_status):
    with _session() as s:
        old = seed(s, batch_id="old-batch", batch_index=0, status=old_batch_status, caldav_uid="old")
        new = seed(s, "新单条", status="failed", caldav_uid="new")
        with patch.object(mp, "_write_caldav", AsyncMock(return_value={"uid": "new", "etag": "fresh"})) as write:
            reply = await mp._handle_batch_retry(s, _ctx(), _caldav(True))
        write.assert_awaited_once()
        assert write.await_args.kwargs["uid"] == "new"
        assert new.status == "success" and new.remote_etag == "fresh"
        assert old.status == old_batch_status
        assert reply[0][1] == new.id


@pytest.mark.anyio
@pytest.mark.parametrize("reference", ["batch", "single", "unknown", "foreign", "already_successful"])
async def test_retry_respects_explicit_reference_without_fallback(reference):
    with _session() as s:
        batch = seed(s, batch_id="batch", batch_index=0, caldav_uid="batch-success")
        failed = seed(s, batch_id="batch", batch_index=1, status="failed", caldav_uid="batch-failed")
        single = seed(s, status="failed", caldav_uid="single")
        if reference == "batch":
            target = batch
        elif reference == "already_successful":
            target = seed(s, caldav_uid="success")
        else:
            target = single
        bind_bot_message(s, target.id, "42", source="telegram", conversation_id="foreign" if reference == "foreign" else "c1")
        s.commit()
        with patch.object(mp, "_write_caldav", AsyncMock(return_value={"uid": "result"})) as write:
            await mp._handle_batch_retry(s, _ctx(reply_to_message_id="missing" if reference == "unknown" else "42"), _caldav(True))
        if reference in {"unknown", "foreign", "already_successful"}:
            write.assert_not_awaited()
        else:
            write.assert_awaited_once()
            assert write.await_args.kwargs["uid"] == ("batch-failed" if reference == "batch" else "single")
        assert (failed.status == "success") == (reference == "batch")
        assert (single.status == "success") == (reference == "single")


@pytest.mark.anyio
@pytest.mark.parametrize("later_operation", ["create", "update", "delete"])
async def test_retry_never_replays_superseded_failure(later_operation):
    with _session() as s:
        seed(s, status="failed", event_id="shared", bot_message_id="42")
        seed(s, operation=later_operation, event_id="shared")
        with patch.object(mp, "_write_caldav", AsyncMock()) as write:
            await mp._handle_batch_retry(s, _ctx(reply_to_message_id="42"), _caldav(True))
        write.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("result", [
    ExtractionResult(intent=Intent.no_event),
    ExtractionResult(intent=Intent.create_event, event=CalendarEvent(title="新日程", start_time="")),
    ExtractionResult(intent=Intent.update_event),
    ExtractionResult(intent=Intent.update_event, error_type="timeout"),
    ExtractionResult(intent=Intent.delete_event, error_type="schema_error"),
    ExtractionResult(intent=Intent.update_event, missing_fields=["时间"]),
    ExtractionResult(intent=Intent.update_event, unsupported_reason="unsupported"),
    ExtractionResult(intent=Intent.update_event, event=CalendarEvent(title="", start_time="")),
    ExtractionResult(intent=Intent.update_event, event=CalendarEvent(title="项目周会", start_time="2026-09-11T10:00:00+08:00")),
])
async def test_invalid_or_unchanged_reply_does_not_write(result):
    with _session() as s:
        original = seed(s, bot_message_id="42", caldav_uid="original")
        extractor = AsyncMock()
        extractor.modify.return_value = result
        with patch.object(mp, "CalDAVService") as remote:
            reply = await mp._route(s, _ctx(reply_to_message_id="42"), "自然语言回复", extractor, _caldav(True), SettingsService(s))
        remote.assert_not_called()
        assert "已更新" not in reply[0][0]
        assert s.execute(select(EventRecord)).scalars().all() == [original]


@pytest.mark.anyio
@pytest.mark.parametrize("intent", [Intent.update_event, Intent.delete_event])
async def test_valid_reply_still_performs_requested_operation(intent):
    with _session() as s:
        seed(s, bot_message_id="42", caldav_uid="original")
        extractor = AsyncMock()
        extractor.modify.return_value = ExtractionResult(intent=intent, event=CalendarEvent(title="新标题", start_time=""))
        remote = AsyncMock()
        remote.delete_event.return_value = True
        with patch.object(mp, "CalDAVService", return_value=remote), patch.object(mp, "_write_caldav_dict", AsyncMock(return_value={"uid": "new"})) as write:
            await mp._route(s, _ctx(reply_to_message_id="42"), "自然语言操作", extractor, _caldav(True), SettingsService(s))
        remote.delete_event.assert_awaited_once()
        if intent == Intent.update_event:
            write.assert_awaited_once()
        else:
            write.assert_not_awaited()


@pytest.mark.anyio
async def test_failed_delete_can_be_retried_then_disappears():
    with _session() as s:
        original = seed(s, event_id="event", caldav_uid="uid", bot_message_id="42")
        remote = AsyncMock()
        remote.delete_event.side_effect = [False, True]
        with patch.object(mp, "CalDAVService", return_value=remote):
            failed = await mp._do_delete_with(s, _ctx(), original, _caldav(True))
            assert "删除失败" in failed
            assert await mp._find_target(s, _ctx(reply_to_message_id="42")) is original
            assert _current_records(s, _ctx()) == [original]
            await mp._do_delete_with(s, _ctx(), original, _caldav(True))
        assert await mp._find_target(s, _ctx(reply_to_message_id="42")) is None
        assert _current_records(s, _ctx()) == []


@pytest.mark.anyio
async def test_local_only_delete_updates_current_state():
    with _session() as s:
        original = seed(s, event_id="local")
        await mp._do_delete_with(s, _ctx(), original, _caldav())
        assert mp._get_active_recent_events(s, _ctx()) == []
        assert _current_records(s, _ctx()) == []
