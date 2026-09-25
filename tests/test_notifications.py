import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.db.models import Base, EventRecord, NotificationDelivery, NotificationEvent, NotificationTarget, TelegramIdentity
from app.services.notification_service import (backfill_events, configure, plan_deliveries, remember_target,
                                              reminder_instances, sync_record)
from app.services.notification_sender import DeliveryError, Destination, send_notification
from app.services.notification_worker import run_once
from app.services.settings_service import SettingsService

NOW = datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "notification-tests-only")
    engine = create_engine(f"sqlite:///{tmp_path / 'notify.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    result = sessionmaker(bind=engine, autoflush=False)
    with result() as session:
        svc = SettingsService(session)
        svc.set("wechat_bot_token", "fake-wechat", encrypted=True)
        svc.set("telegram_bot_token", "fake-telegram", encrypted=True)
        svc.set("discord_bot_token", "fake-discord", encrypted=True)
        session.add(TelegramIdentity(telegram_user_id="123", enabled=True))
        session.commit()
        remember_target(session, "wechat", "wx-user", "wx-user", context_token="first-context")
        remember_target(session, "telegram", "123", "123")
        session.commit()
        wx = session.scalar(select(NotificationTarget).where(NotificationTarget.channel == "wechat"))
        configure(session, True, [wx.id], now=NOW - 3600)
    yield result
    engine.dispose()


def record(session, *, source="wechat", key="evt", operation="create", status="success", **changes):
    event = dict(title="项目会议", start_time="2026-09-25T12:30:00+08:00", timezone="Asia/Shanghai",
                 end_time="2026-09-25T13:30:00+08:00", reminders=[{"minutes_before": 30}])
    event.update(changes)
    row = EventRecord(source=source, source_user_id="owner", conversation_id="chat", event_id=key,
                      operation=operation, status=status, event_json=json.dumps(event), title=event['title'])
    session.add(row)
    session.flush()
    sync_record(session, row)
    session.commit()
    return row


def jobs(factory):
    with factory() as session:
        return session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.due_at)).all()


def test_concurrent_target_capture_preserves_selection(factory):
    from threading import Barrier
    barrier = Barrier(2)
    def capture(index):
        with factory() as session:
            barrier.wait(timeout=5)
            remember_target(session, "wechat", "new-user", "new-user", context_token=f"context-{index}")
            session.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(capture, [1, 2]))
    with factory() as session:
        targets = session.scalars(select(NotificationTarget).where(NotificationTarget.user_id == "new-user")).all()
        assert len(targets) == 1
        target = targets[0]
        configure(session, True, [target.id], now=NOW)
        remember_target(session, "wechat", "new-user", "new-user", context_token="fresh")
        session.commit()
        assert target.selected and target.selected_at == NOW
        assert decrypt_secret(target.context_token) == "fresh"


def test_app_lifecycle_backfills_and_cancels_worker(factory, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "bootstrap_application", lambda: None)
    auto_start = AsyncMock()
    monkeypatch.setattr(main, "auto_start_bots", auto_start)
    async def run():
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def loop():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        monkeypatch.setattr("app.services.notification_worker.notification_loop", loop)
        with factory() as session:
            record(session)
            session.execute(delete(NotificationEvent))
            session.commit()
        app = main.create_app()
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(started.wait(), timeout=2)
            with factory() as session:
                assert session.scalar(select(NotificationEvent)).active
            assert not app.state.notification_task.done()
        assert stopped.is_set() and app.state.notification_task.cancelled()
        auto_start.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("source", ["wechat", "telegram", "discord"])
def test_all_inputs_deliver_to_selected_wechat_once_after_restart(factory, source):
    with factory() as session:
        record(session, source=source)
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    asyncio.run(run_once(factory, sender, NOW + 30))
    assert sender.await_count == 1
    dest, text = sender.call_args.args
    assert dest.channel == "wechat" and dest.conversation_id == "wx-user"
    assert "项目会议" in text and "12:30" in text
    assert jobs(factory)[0].status == "sent"


def test_context_encrypted_refreshed_and_account_scoped(factory):
    with factory() as session:
        record(session)
        remember_target(session, "wechat", "wx-user", "wx-user", context_token="new-context")
        session.commit()
        target = session.scalar(select(NotificationTarget).where(NotificationTarget.channel == "wechat"))
        assert target.context_token != "new-context"
        assert decrypt_secret(target.context_token) == "new-context"
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    assert sender.call_args.args[0].context_token == "new-context"
    with factory() as session:
        SettingsService(session).set("wechat_bot_token", "different-account", encrypted=True)
        session.commit()
        remember_target(session, "wechat", "wx-user", "wx-user", context_token="other-context")
        session.commit()
        targets = session.scalars(select(NotificationTarget).where(NotificationTarget.channel == "wechat")).all()
        assert len(targets) == 2
        assert sum(t.selected for t in targets) == 1
        old = next(t for t in targets if t.selected)
        with pytest.raises(ValueError):
            configure(session, True, [old.id], now=NOW)


def test_edit_cancel_and_failed_edits_do_not_override_success(factory):
    with factory() as session:
        record(session)
        plan_deliveries(session, NOW - 30)
        record(session, operation="update", status="failed", start_time="2026-09-25T18:00:00+08:00")
        assert session.scalar(select(NotificationEvent)).record_id == 1
        record(session, operation="update", start_time="2026-09-25T14:30:00+08:00",
               end_time="2026-09-25T15:30:00+08:00")
        assert session.scalar(select(NotificationDelivery)).status == "cancelled"
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    sender.assert_not_called()
    with factory() as session:
        record(session, operation="delete")
    asyncio.run(run_once(factory, sender, NOW + 7200))
    sender.assert_not_called()
    assert all(j.status == "cancelled" for j in jobs(factory))


def test_successful_retry_and_history_cleanup_keep_reminders(factory):
    with factory() as session:
        r = record(session, status="failed")
        assert session.scalar(select(NotificationEvent)) is None
        r.status = "success"
        sync_record(session, r)
        session.commit()
        session.execute(delete(EventRecord))
        session.commit()
        backfill_events(session)
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    sender.assert_awaited_once()


def test_disable_and_reenable_do_not_replay_old_reminders(factory):
    with factory() as session:
        record(session)
        plan_deliveries(session, NOW - 30)
        target = session.scalar(select(NotificationTarget).where(NotificationTarget.selected.is_(True)))
        configure(session, False, [target.id], now=NOW - 10)
        configure(session, True, [target.id], now=NOW + 10)
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW + 30))
    sender.assert_not_called()


def test_sent_content_edit_does_not_send_same_occurrence_again(factory):
    with factory() as session:
        record(session)
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    with factory() as session:
        record(session, operation="update", title="改标题")
    asyncio.run(run_once(factory, sender, NOW + 30))
    sender.assert_awaited_once()


def test_empty_reminders_and_malformed_event_isolation(factory):
    with factory() as session:
        record(session, key="empty", reminders=[])
        record(session, key="invalid", start_time="bad-date")
        record(session, key="valid")
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    sender.assert_awaited_once()
    with factory() as session:
        assert session.get(NotificationEvent, "event:invalid").error


def test_recurring_timezone_and_count():
    event = dict(title="每天", start_time="2026-09-24T12:30:00", timezone="Asia/Shanghai",
                 reminders=[{"minutes_before": 30}], recurrence={"frequency": "daily", "count": 2})
    instances = list(reminder_instances(event, NOW))
    assert len(instances) == 1 and instances[0][2] == NOW
    assert list(reminder_instances(event, NOW + 86400)) == []


@pytest.mark.parametrize("frequency,extra,start", [
    ("weekly", {"days_of_week": ["FR"]}, "2026-09-18T12:30:00+08:00"),
    ("monthly", {"day_of_month": 25}, "2026-08-25T12:30:00+08:00"),
])
def test_weekly_monthly(frequency, extra, start):
    event = dict(start_time=start, timezone="Asia/Shanghai", reminders=[{"minutes_before": 30}],
                 recurrence={"frequency": frequency, **extra})
    assert list(reminder_instances(event, NOW))[0][2] == NOW


def test_bounded_retries_and_uncertain_delivery(factory):
    with factory() as session:
        record(session)
    sender = AsyncMock(side_effect=DeliveryError("连接失败", retry=True))
    for t in [NOW, NOW + 30, NOW + 90, NOW + 180]:
        asyncio.run(run_once(factory, sender, t))
    assert sender.await_count == 3
    assert jobs(factory)[0].status == "failed"


def test_timeout_never_automatically_resends(factory):
    with factory() as session:
        record(session)
    sender = AsyncMock(side_effect=DeliveryError("结果未知", uncertain=True))
    asyncio.run(run_once(factory, sender, NOW))
    asyncio.run(run_once(factory, sender, NOW + 30))
    sender.assert_awaited_once()
    assert jobs(factory)[0].status == "unknown"


def test_crash_recovery_and_overdue_expiry(factory):
    with factory() as session:
        record(session)
        plan_deliveries(session, NOW - 30)
        job = session.scalar(select(NotificationDelivery))
        job.status = "sending"
        job.claimed_at = NOW - 180
        session.commit()
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    assert jobs(factory)[0].status == "unknown"
    sender.assert_not_called()
    with factory() as session:
        session.execute(update(NotificationDelivery).values(status="pending"))
        session.commit()
    asyncio.run(run_once(factory, sender, NOW + 301))
    assert jobs(factory)[0].status == "expired"


def test_two_workers_claim_only_once(factory):
    with factory() as session:
        record(session)
    calls = []
    async def sender(dest, text):
        calls.append(text)
        await asyncio.sleep(0.01)
    async def run():
        await asyncio.gather(run_once(factory, sender, NOW), run_once(factory, sender, NOW))
    asyncio.run(run())
    assert len(calls) == 1


def test_mutation_rollback_preserves_old_schedule(factory):
    with factory() as session:
        record(session)
        plan_deliveries(session, NOW - 30)
        r = EventRecord(event_id="evt", operation="delete", status="success", event_json="{}")
        session.add(r)
        session.flush()
        sync_record(session, r)
        session.rollback()
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    sender.assert_awaited_once()


def test_history_id_reuse_does_not_block_future_edits(factory):
    with factory() as session:
        original = record(session)
        old_id = original.id
        session.execute(delete(EventRecord))
        session.commit()
        newer = record(session, operation="update", title="清理日志后的新标题")
        assert newer.id == old_id
        projection = session.scalar(select(NotificationEvent))
        assert json.loads(projection.event_json)["title"] == "清理日志后的新标题"


@pytest.mark.parametrize("source", ["wechat", "telegram", "discord"])
def test_real_write_modify_delete_hooks(factory, source):
    from app.ai.schemas import CalendarEvent
    from app.channels.message_processor import ChannelContext, _write_one, _do_modify_with, _do_delete_with
    cfg = {"url": "", "user": "", "pw": "", "cal": "", "rem": 30, "dur": 60, "ssl": True}
    ctx = ChannelContext(source, "u", "c")
    async def run():
        with factory() as session:
            event = CalendarEvent(title="长描述", start_time="2026-09-25T12:30:00+08:00", description="正文" * 3000)
            ident, _ = await _write_one(session, ctx, "创建", event, cfg)
            session.commit()
            original = session.get(EventRecord, ident)
            assert len(json.loads(original.event_json)["description"]) == 6000
            assert session.scalar(select(NotificationEvent)).active
            plan_deliveries(session, NOW - 30)
            changed = json.loads(original.event_json)
            changed["reminders"] = []
            ident, _ = await _do_modify_with(session, ctx, "取消提醒", original, changed, cfg)
            session.commit()
            assert jobs(factory)[0].status == "cancelled"
            await _do_delete_with(session, ctx, session.get(EventRecord, ident), cfg)
            assert session.scalar(select(NotificationEvent)).active is False
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("uid", [None, "legacy-caldav-uid"])
def test_legacy_edit_cancels_old_projection_across_restart(factory, operation, uid):
    from app.channels.message_processor import ChannelContext, _do_modify_with, _do_delete_with
    cfg = {"url": "", "user": "", "pw": "", "cal": "", "rem": 30, "dur": 60, "ssl": True}
    async def run():
        with factory() as session:
            original = EventRecord(source="wechat", source_user_id="u", operation="create", status="success",
                                   caldav_uid=uid, event_json=json.dumps({"title": "旧日程",
                                   "start_time": "2026-09-25T12:30:00+08:00"}))
            session.add(original)
            session.flush()
            sync_record(session, original)
            plan_deliveries(session, NOW)
            ctx = ChannelContext("wechat", "u")
            if operation == "delete":
                await _do_delete_with(session, ctx, original, cfg)
            else:
                await _do_modify_with(session, ctx, "推迟一小时", original,
                                      {"title": "改期", "start_time": "2026-09-25T13:30:00+08:00"}, cfg)
                session.commit()
        with factory() as session:
            backfill_events(session)
            plan_deliveries(session, NOW)
            active = session.scalars(select(NotificationEvent).where(NotificationEvent.active.is_(True))).all()
            assert len(active) == (1 if operation == "update" else 0)
            assert jobs(factory)[0].status == "cancelled"
    asyncio.run(run())


def test_explicit_no_reminder_survives_creation(factory):
    from app.ai.schemas import CalendarEvent
    from app.channels.message_processor import ChannelContext, _write_one
    async def run():
        with factory() as session:
            await _write_one(session, ChannelContext("wechat", "u"), "不要提醒",
                             CalendarEvent(title="无提醒", start_time="2026-09-25T12:30:00+08:00", reminders=[]),
                             {"url": "", "user": "", "pw": "", "cal": "", "rem": 30, "dur": 60, "ssl": True})
            session.commit()
            plan_deliveries(session, NOW)
            assert session.scalar(select(NotificationDelivery)) is None
    asyncio.run(run())


def test_backfill_uses_latest_success_not_failed_edit(factory):
    with factory() as session:
        record(session)
        record(session, operation="delete")
        record(session, operation="update", status="failed")
        session.execute(delete(NotificationEvent))
        session.commit()
        backfill_events(session)
        assert session.scalar(select(NotificationEvent)).active is False


def test_multiple_selected_channels(factory):
    with factory() as session:
        ids = list(session.scalars(select(NotificationTarget.id)))
        configure(session, True, ids, now=NOW - 3600)
        record(session)
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    assert {c.args[0].channel for c in sender.call_args_list} == {"wechat", "telegram"}


def test_selected_user_revocation_prevents_send(factory):
    with factory() as session:
        tg = session.scalar(select(NotificationTarget).where(NotificationTarget.channel == "telegram"))
        configure(session, True, [tg.id], now=NOW - 3600)
        session.execute(delete(TelegramIdentity))
        record(session)
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    sender.assert_not_called()
    assert jobs(factory)[0].status == "failed"


def test_wechat_sender_requires_explicit_success(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.send_message.return_value = {}
    monkeypatch.setattr("app.integrations.ilink.ILinkClient", lambda token: client)
    with pytest.raises(DeliveryError) as failure:
        asyncio.run(send_notification(Destination("wechat", "token", "user", "", "ctx"), "提醒"))
    assert failure.value.uncertain
    client.send_message.return_value = {"ret": 0}
    asyncio.run(send_notification(Destination("wechat", "token", "user", "", "ctx"), "提醒"))


@pytest.mark.parametrize("channel,status,body,retry,uncertain", [
    ("telegram", 429, {"parameters": {"retry_after": 45}}, True, False),
    ("discord", 403, {}, False, False),
    ("telegram", 200, {"ok": False}, False, True),
    ("discord", 502, {}, False, True),
])
def test_sender_error_mapping_does_not_expose_credentials(monkeypatch, channel, status, body, retry, uncertain):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(status, json=body)
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: client)
    with pytest.raises(DeliveryError) as failure:
        asyncio.run(send_notification(Destination(channel, "secret-test-token", "123", "", None), "提醒"))
    assert failure.value.retry is retry and failure.value.uncertain is uncertain
    assert "secret-test-token" not in str(failure.value)


def test_settings_page_validation_test_and_auth(factory, monkeypatch):
    from app.web.notifications import router
    from app.web.routes import get_db, require_admin
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    app.include_router(router)
    def db():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = db
    sender = AsyncMock()
    monkeypatch.setattr("app.web.notifications.send_notification", sender)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/console/notifications")
            assert response.status_code in (302, 303, 401, 403, 307)
            def authenticated(request: Request):
                request.session["admin_authenticated"] = True
            app.dependency_overrides[require_admin] = authenticated
            response = await client.get("/console/notifications")
            assert response.status_code == 200 and "日程通知" in response.text
            assert "first-context" not in response.text and "fake-wechat" not in response.text
            response = await client.post("/console/notifications", data={"enabled": "true", "target_ids": "bogus"}, follow_redirects=True)
            assert "接收会话无效" in response.text
            with factory() as session:
                target = session.scalar(select(NotificationTarget).where(NotificationTarget.channel == "wechat"))
                ident = target.id
            response = await client.post("/console/notifications/test", data={"target_id": ident}, follow_redirects=True)
            assert "渠道已接受测试消息" in response.text
            await client.post("/console/notifications/test", data={"target_id": ident})
            sender.assert_awaited_once()
    asyncio.run(run())
