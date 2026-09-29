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
from app.services.notification_service import (backfill_events, configure_channel, migrate_channel_notifications, plan_deliveries, remember_target,
                                              reminder_instances, sync_record)
from app.services.notification_sender import DeliveryError, Destination, send_notification
from app.services.notification_worker import run_once
from app.services.settings_service import SettingsService

NOW = datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc).timestamp()


def configure(session, enabled, target_ids, now=None):
    # Exercise the three independent channel controls in legacy scheduler scenarios.
    for channel in ("wechat", "telegram", "discord"):
        ids = list(session.scalars(select(NotificationTarget.id).where(
            NotificationTarget.channel == channel, NotificationTarget.id.in_(target_ids))))
        configure_channel(session, channel, enabled and bool(ids), ids, now=now)


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
    from app.web.routes import get_db, require_admin, router as web_router
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    app.include_router(router)
    app.include_router(web_router)
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
            assert response.status_code == 303 and response.headers["location"] == "/console/channels"
            response = await client.get("/console/channels?tab=wechat")
            assert response.status_code == 200 and response.text.count("作为通知渠道") == 3
            assert "日程通知设置" not in response.text and "启用日程通知" not in response.text
            assert "first-context" not in response.text and "fake-wechat" not in response.text
            response = await client.post("/console/channels/wechat/notifications", data={"enabled": "true", "target_ids": "bogus"}, follow_redirects=True)
            assert "接收会话无效" in response.text
            with factory() as session:
                target = session.scalar(select(NotificationTarget).where(NotificationTarget.channel == "wechat"))
                ident = target.id
            response = await client.post("/console/channels/wechat/notifications/test", data={"target_id": ident}, follow_redirects=True)
            assert "渠道已接受测试消息" in response.text
            await client.post("/console/channels/wechat/notifications/test", data={"target_id": ident})
            sender.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("legacy_enabled", [True, False])
def test_channel_migration_preserves_legacy_intent_once(factory, legacy_enabled):
    with factory() as session:
        svc = SettingsService(session)
        svc.set("notifications_enabled", "true" if legacy_enabled else "false")
        session.commit()
        migrate_channel_notifications(session)
        session.commit()
        target = session.scalar(select(NotificationTarget).where(NotificationTarget.channel == "wechat"))
        assert target.selected is legacy_enabled
        configure_channel(session, "wechat", True, [], now=NOW)
        migrate_channel_notifications(session)
        session.commit()
        assert target.selected


def test_channel_toggle_is_independent_and_has_no_master_gate(factory):
    with factory() as session:
        svc = SettingsService(session)
        svc.set("notifications_enabled", "false")  # obsolete setting cannot block channel switches
        session.commit()
        configure_channel(session, "telegram", True, [], now=NOW - 60)
        record(session, source="discord")
        plan_deliveries(session, NOW)
        wx = session.scalar(select(NotificationTarget).where(NotificationTarget.channel == "wechat"))
        original_selected_at = wx.selected_at
        configure_channel(session, "telegram", False, [], now=NOW)
        assert wx.selected and wx.selected_at == original_selected_at
    sender = AsyncMock()
    asyncio.run(run_once(factory, sender, NOW))
    assert sender.await_count == 1 and sender.call_args.args[0].channel == "wechat"
    assert {j.status for j in jobs(factory)} == {"sent", "cancelled"}


def test_channel_cannot_select_another_channel_or_guess_multiple_recipients(factory):
    with factory() as session:
        tg = session.scalar(select(NotificationTarget).where(NotificationTarget.channel == "telegram"))
        with pytest.raises(ValueError, match="接收会话无效"):
            configure_channel(session, "wechat", True, [tg.id], now=NOW)
        remember_target(session, "wechat", "second", "second", context_token="fake")
        session.commit()
        with pytest.raises(ValueError, match="请选择"):
            configure_channel(session, "wechat", True, [], now=NOW)
        with pytest.raises(ValueError, match="请先向"):
            configure_channel(session, "discord", True, [], now=NOW)


def test_channel_controls_keep_delivery_history_scoped(factory):
    from app.web.notifications import channel_notification_panels
    with factory() as session:
        record(session)
        plan_deliveries(session, NOW)
        panels = channel_notification_panels(session)
        assert panels["wechat"]["enabled"] and len(panels["wechat"]["deliveries"]) == 1
        assert not panels["telegram"]["enabled"] and panels["telegram"]["deliveries"] == []
        assert "first-context" not in json.dumps(panels)


@pytest.mark.parametrize('channel,body,expected', [
    ('telegram', {'ok': True, 'result': {'message_id': 123}}, '123'),
    ('discord', {'id': '456'}, '456'),
    ('telegram', {'ok': True}, None),
])
def test_sender_returns_server_message_id(monkeypatch, channel, body, expected):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=body)
    monkeypatch.setattr('httpx.AsyncClient', lambda **kw: client)
    assert asyncio.run(send_notification(Destination(channel, 'test', '123', '', None), '提醒')) == expected


@pytest.mark.parametrize('body,expected', [
    ({'ret': 0, 'msg': {'message_id': 123}}, '123'),
    ({'ret': 0, 'client_id': 'local-client-id'}, None),
])
def test_wechat_sender_returns_only_server_id(monkeypatch, body, expected):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.send_message.return_value = body
    monkeypatch.setattr('app.integrations.ilink.ILinkClient', lambda token: client)
    assert asyncio.run(send_notification(Destination('wechat', 'test', 'wx', '', 'context'), '提醒')) == expected


def test_friendly_reminder_format():
    from app.services.notification_service import message_text
    event = {'title': '带耳机', 'timezone': 'Asia/Shanghai', 'location': '门口'}
    text = message_text(event, NOW, 0)
    assert text == ('⏰ 日程提醒\n\n📌 带耳机\n'
                    '🕒 2026-09-25 周五 12:00（北京时间）\n📍 门口')
    event.update(is_all_day=True, timezone='America/New_York', recurrence={'frequency': 'daily'})
    text = message_text(event, NOW, 30)
    assert '提前 30 分钟' in text and '全天（America/New_York）' in text
    assert '整个日程' in text and '📍 门口' in text


def reminder_context(**changes):
    from app.channels.message_processor import ChannelContext
    values = dict(source='wechat', source_user_id='wx-user', conversation_id='wx-user',
                  reply_to_message_id='reminder-id')
    values.update(changes)
    return ChannelContext(**values)


@pytest.mark.parametrize('source', ['wechat', 'telegram', 'discord'])
def test_sent_reminder_id_resolves_across_origins_and_after_restart(factory, source):
    from app.channels.message_processor import _find_target
    with factory() as session:
        original = record(session, source=source)
        original_id = original.id
    asyncio.run(run_once(factory, AsyncMock(return_value='reminder-id'), NOW))
    with factory() as session:
        target = asyncio.run(_find_target(session, reminder_context()))
        assert target.id == original_id
        assert asyncio.run(_find_target(session, reminder_context(conversation_id='other'))) is None


@pytest.mark.parametrize('legacy', [True, False])
@pytest.mark.parametrize('reply_id', [None, 'unavailable-server-id'])
def test_wechat_text_fallback_matches_only_delivered_text(factory, legacy, reply_id):
    from app.channels.message_processor import _find_target
    with factory() as session:
        original_id = record(session, source='telegram').id
        plan_deliveries(session, NOW)
    asyncio.run(run_once(factory, AsyncMock(return_value=None), NOW))
    with factory() as session:
        if legacy:
            job = session.scalar(select(NotificationDelivery))
            job.text = '⏰ 日程提醒\n项目会议\n时间：2026-09-25 12:30（Asia/Shanghai）\n提前 30 分钟提醒'
            session.commit()
    quote = '  '.join(jobs(factory)[0].text.split())
    ctx = reminder_context(reply_to_message_id=reply_id, quoted_text=quote)
    with factory() as session:
        assert asyncio.run(_find_target(session, ctx)).id == original_id
        assert asyncio.run(_find_target(session, reminder_context(quoted_text=quote, conversation_id='other'))) is None
        assert asyncio.run(_find_target(session, reminder_context(quoted_text=quote + '伪造'))) is None


@pytest.mark.parametrize('condition', ['deleted', 'cleared', 'reused', 'account_changed'])
@pytest.mark.parametrize('by_id', [True, False])
def test_reminder_cannot_resolve_invalid_or_reused_history(factory, condition, by_id):
    from app.channels.message_processor import _find_target
    with factory() as session:
        record(session)
    asyncio.run(run_once(factory, AsyncMock(return_value='reminder-id'), NOW))
    quote = jobs(factory)[0].text
    with factory() as session:
        if condition == 'deleted':
            record(session, operation='delete')
        elif condition in ('cleared', 'reused'):
            session.execute(delete(EventRecord)); session.commit()
            if condition == 'reused':
                record(session, key='different', title='无关日程')
        else:
            SettingsService(session).set('wechat_bot_token', 'changed-account', encrypted=True)
            session.commit()
        ctx = reminder_context(reply_to_message_id='reminder-id' if by_id else None, quoted_text=quote)
        assert asyncio.run(_find_target(session, ctx)) is None


def test_identical_delivered_reminders_are_ambiguous_without_id(factory):
    from app.channels.message_processor import _find_target
    with factory() as session:
        record(session, key='one'); record(session, key='two')
    asyncio.run(run_once(factory, AsyncMock(side_effect=['first', 'second']), NOW))
    quote = jobs(factory)[0].text
    with factory() as session:
        assert asyncio.run(_find_target(session, reminder_context(quoted_text=quote))) is None
        assert asyncio.run(_find_target(session, reminder_context(reply_to_message_id='first'))) is not None


@pytest.mark.parametrize('status', ['pending', 'failed', 'cancelled', 'expired'])
def test_unsent_reminder_text_is_not_authorization(factory, status):
    from app.channels.message_processor import _find_target
    with factory() as session:
        record(session)
        plan_deliveries(session, NOW)
        job = session.scalar(select(NotificationDelivery)); job.status = status; session.commit()
        assert asyncio.run(_find_target(session, reminder_context(quoted_text=job.text))) is None


def test_old_reminder_follows_successful_update_and_ignores_failed_update(factory):
    from app.channels.message_processor import _find_target
    with factory() as session:
        record(session, recurrence={'frequency': 'daily'})
    asyncio.run(run_once(factory, AsyncMock(return_value='reminder-id'), NOW))
    quote = jobs(factory)[0].text
    with factory() as session:
        latest = record(session, source='discord', operation='update', title='最新标题')
        record(session, operation='update', status='failed', title='失败标题')
        assert asyncio.run(_find_target(session, reminder_context())) is latest
        assert asyncio.run(_find_target(session, reminder_context(reply_to_message_id=None, quoted_text=quote))) is latest


def test_reply_to_reminder_reschedules_and_plans_new_delivery(factory):
    from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
    from app.channels.message_processor import _route
    with factory() as session:
        record(session, source='telegram', start_time='2026-09-25T12:00:00+08:00', reminders=[{'minutes_before': 0}])
    asyncio.run(run_once(factory, AsyncMock(return_value=None), NOW))
    quote = jobs(factory)[0].text
    updated = CalendarEvent(title='项目会议', start_time='2026-09-25T21:30:00+08:00',
                            end_time='2026-09-25T22:30:00+08:00', reminders=[{'minutes_before': 0}])
    extractor = AsyncMock()
    extractor.modify.return_value = ExtractionResult(intent=Intent.update_event, event=updated)
    cfg = {'url': '', 'user': '', 'pw': '', 'cal': '', 'rem': 30, 'dur': 60, 'ssl': True}
    with factory() as session:
        replies = asyncio.run(_route(session, reminder_context(quoted_text=quote), '21:30提醒', extractor, cfg, SettingsService(session)))
        assert replies[0][1] is not None and '21:30' in replies[0][0]
        extractor.modify.assert_awaited_once()
        projection = session.scalar(select(NotificationEvent))
        assert json.loads(projection.event_json)['start_time'] == updated.start_time
        plan_deliveries(session, NOW)
    pending = [j for j in jobs(factory) if j.status == 'pending']
    assert len(pending) == 1
    assert pending[0].due_at == datetime.fromisoformat(updated.start_time).timestamp()


def test_existing_database_adds_reminder_binding_table(factory, monkeypatch):
    from sqlalchemy import inspect
    from app.db.models import NotificationMessageBinding
    from app.db import session as database
    with factory() as session:
        original_id = record(session).id
        engine = session.get_bind()
    NotificationMessageBinding.__table__.drop(engine)
    monkeypatch.setattr(database, 'engine', engine)
    database.init_db()
    database.init_db()
    assert 'notification_message_bindings' in inspect(engine).get_table_names()
    with factory() as session:
        assert session.get(EventRecord, original_id).title == '项目会议'


def test_recurring_later_occurrence_quote_resolves_series(factory):
    from app.channels.message_processor import _find_target
    with factory() as session:
        original_id = record(session, start_time='2026-09-24T12:30:00+08:00',
                             recurrence={'frequency': 'daily'}).id
    asyncio.run(run_once(factory, AsyncMock(return_value=None), NOW))
    quote = next(j.text for j in jobs(factory) if j.status == 'sent')
    assert '2026-09-25' in quote
    with factory() as session:
        assert asyncio.run(_find_target(session, reminder_context(quoted_text=quote))).id == original_id


def test_quote_can_delete_delivered_event(factory):
    from app.channels.message_processor import _route
    with factory() as session:
        record(session, source='discord')
    asyncio.run(run_once(factory, AsyncMock(return_value='reminder-id'), NOW))
    cfg = {'url': '', 'user': '', 'pw': '', 'cal': '', 'rem': 30, 'dur': 60, 'ssl': True}
    with factory() as session:
        replies = asyncio.run(_route(session, reminder_context(), '取消日程', AsyncMock(), cfg, SettingsService(session)))
        assert '已删除' in replies[0][0]
        assert not session.scalar(select(NotificationEvent)).active


@pytest.mark.parametrize('status', ['pending', 'retry', 'sent', 'unknown', 'sending'])
def test_wording_refresh_preserves_sent_quotes_and_retry_state(factory, status):
    with factory() as session:
        record(session)
        plan_deliveries(session, NOW)
        job = session.scalar(select(NotificationDelivery))
        job.text = 'old reminder text'
        job.status = status
        job.attempts = 2
        job.next_attempt_at = NOW + 60
        job.claimed_at = NOW
        session.commit()
        plan_deliveries(session, NOW)
        session.refresh(job)
        assert (job.status, job.attempts, job.next_attempt_at) == (status, 2, NOW + 60)
        if status in ('pending', 'retry'):
            assert job.text.startswith('⏰ 日程提醒') and 'old reminder text' not in job.text
            assert '需要调整' not in job.text and '到时间啦' not in job.text
        else:
            assert job.text == 'old reminder text'


def test_wechat_id_only_quote_roundtrip_without_ret(factory, monkeypatch):
    from app.channels.wechat_handler import wechat_context_from_message
    from app.channels.message_processor import _route
    from app.db.models import NotificationMessageBinding
    # Exercise real ILinkClient decoding: uint64 ID, no optional ret field.
    client = AsyncMock()
    client.request.return_value = httpx.Response(200, content=b'{"message_id":18446744073709551610}')
    monkeypatch.setattr('httpx.AsyncClient', lambda **kw: client)
    with factory() as session:
        original = record(session, source='telegram')
        event_id = original.event_id
    asyncio.run(run_once(factory, send_notification, NOW))
    assert jobs(factory)[0].status == 'sent'
    ctx = wechat_context_from_message({'from_user_id': 'wx-user', 'message_id': 123,
          'item_list': [{'type': 1, 'text_item': {'text': '改到21:30'},
                         'ref_msg': {'svr_id': '18446744073709551610'}}]})
    assert ctx.quoted_text is None
    cfg = {'url': '', 'user': '', 'pw': '', 'cal': '', 'rem': 30, 'dur': 60, 'ssl': True}
    with factory() as session:
        assert session.get(NotificationMessageBinding, ('wechat', 'wx-user', '18446744073709551610'))
        replies = asyncio.run(_route(session, ctx, '改到21:30', AsyncMock(), cfg, SettingsService(session)))
        assert replies[0][1] is not None and '21:30' in replies[0][0]
        changed = session.get(EventRecord, replies[0][1])
        assert changed.event_id == event_id
        assert json.loads(changed.event_json)['start_time'].endswith('21:30:00+08:00')


@pytest.mark.parametrize('body,expected', [
    ({'message_id': '18446744073709551610'}, '18446744073709551610'),
    ({'ret': '0', 'message_id': 123}, '123'),
    ({'ret': 0}, None),
    ({'ret': '0'}, None),
])
def test_wechat_acknowledgement_accepts_documented_success(monkeypatch, body, expected):
    client = AsyncMock()
    client.request.return_value = httpx.Response(200, json=body)
    monkeypatch.setattr('httpx.AsyncClient', lambda **kw: client)
    assert asyncio.run(send_notification(Destination('wechat', 'token', 'wx', '', 'ctx'), '测试')) == expected


@pytest.mark.parametrize('body', [{}, {'client_id': 'local'}, {'message_id': 0},
                                  {'message_id': '123.0'}, {'ret': False, 'message_id': '123'},
                                  {'ret': 'invalid', 'message_id': '123'}])
def test_wechat_malformed_ack_remains_uncertain(monkeypatch, body):
    client = AsyncMock()
    client.request.return_value = httpx.Response(200, json=body)
    monkeypatch.setattr('httpx.AsyncClient', lambda **kw: client)
    with pytest.raises(DeliveryError) as failure:
        asyncio.run(send_notification(Destination('wechat', 'token', 'wx', '', 'ctx'), '测试'))
    assert failure.value.uncertain and not failure.value.retry


@pytest.mark.parametrize('code', ['ret', 'errcode'])
def test_wechat_server_error_cannot_bind_message_id(monkeypatch, code):
    client = AsyncMock()
    client.request.return_value = httpx.Response(200, json={code: -2, 'message_id': '123'})
    monkeypatch.setattr('httpx.AsyncClient', lambda **kw: client)
    with pytest.raises(DeliveryError) as failure:
        asyncio.run(send_notification(Destination('wechat', 'token', 'wx', '', 'ctx'), '测试'))
    assert not failure.value.uncertain


def test_old_id_only_reminder_is_not_guessed_from_recent_delivery(factory):
    from app.channels.wechat_handler import wechat_context_from_message
    from app.channels.message_processor import _route
    with factory() as session:
        record(session)
    asyncio.run(run_once(factory, AsyncMock(return_value=None), NOW))
    ctx = wechat_context_from_message({'from_user_id': 'wx-user', 'item_list': [
        {'type': 1, 'text_item': {'text': '改到21:30'}, 'ref_msg': {'svr_id': '12345'}}]})
    with factory() as session:
        replies = asyncio.run(_route(session, ctx, '改到21:30', AsyncMock(), {'dur': 60}, SettingsService(session)))
        assert '只提供了引用消息的编号' in replies[0][0]
        assert session.scalar(select(NotificationEvent)).record_id == 1
        assert session.scalar(select(EventRecord).where(EventRecord.operation == 'update')) is None
