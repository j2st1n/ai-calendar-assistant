"""Persisted reminder state and scheduling. No network calls or AI decisions here."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse
from dateutil.rrule import rrulestr
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.calendar.recurrence import to_rrule
from app.core.crypto import encrypt_secret
from app.db.models import EventRecord, NotificationDelivery, NotificationEvent, NotificationTarget, Setting, NotificationMessageBinding
from app.services.settings_service import SettingsService

CHANNELS = {"wechat": "微信", "telegram": "Telegram", "discord": "Discord"}
GRACE_SECONDS = 300
PENDING = ("pending", "retry")


def digest(*values: object) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def account_key(session: Session, channel: str) -> str | None:
    if channel not in CHANNELS:
        return None
    token = SettingsService(session).get(f"{channel}_bot_token")
    return digest(channel, token) if token else None


def remember_target(session: Session, channel: str, user_id: str, conversation_id: str,
                    *, context_token: str | None = None, thread_id: str = "") -> None:
    key = account_key(session, channel)
    if not key or not user_id or not conversation_id:
        return
    ident = digest(channel, key, conversation_id, thread_id, user_id)
    changes = {"last_seen_at": time.time()}
    if channel == "wechat" and context_token:
        changes["context_token"] = encrypt_secret(context_token)
    # Multiple processes may observe the same new conversation simultaneously.
    # Refresh its context atomically without resetting the user's selection.
    statement = insert(NotificationTarget).values(
        id=ident, channel=channel, account_key=key, conversation_id=conversation_id,
        user_id=user_id, thread_id=thread_id, selected=False, selected_at=0, **changes)
    session.execute(statement.on_conflict_do_update(
        index_elements=[NotificationTarget.id], set_=changes))
    session.flush()


def sync_record(session: Session, record: EventRecord) -> None:
    if record.status != "success" or record.operation not in {"create", "update", "delete"}:
        return
    key = (f"event:{record.event_id}" if record.event_id else
           f"caldav:{record.caldav_uid}" if record.caldav_uid else f"record:{record.id}")
    row = session.get(NotificationEvent, key)
    created = record.created_at
    recorded_at = (created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created).timestamp()
    # SQLite may reuse audit IDs after history is cleared. Compare chronology too.
    if row and (row.recorded_at, row.record_id) >= (recorded_at, record.id):
        return
    if row is None:
        row = NotificationEvent(event_key=key)
        session.add(row)
    row.record_id = record.id
    row.recorded_at = recorded_at
    row.version = uuid.uuid4().hex
    row.event_json = record.event_json or "{}"
    row.active = record.operation != "delete"
    row.error = None
    session.execute(update(NotificationDelivery).where(
        NotificationDelivery.event_key == key, NotificationDelivery.status.in_(PENDING),
    ).values(status="cancelled", error="日程已变更"))
    session.flush()


def backfill_events(session: Session) -> None:
    """Only surviving successful history; failed edits never replace known state."""
    records = session.scalars(select(EventRecord).where(
        EventRecord.status == "success", EventRecord.operation.in_(["create", "update", "delete"])
    ).order_by(EventRecord.id)).all()
    for record in records:
        sync_record(session, record)


def retire_legacy_event(session: Session, previous: EventRecord) -> None:
    """A legacy edit acquires a new event ID; keep its old projection inactive."""
    if previous.event_id:
        return
    sync_record(session, previous)
    key = f"caldav:{previous.caldav_uid}" if previous.caldav_uid else f"record:{previous.id}"
    row = session.get(NotificationEvent, key)
    if row:
        row.active = False
        session.execute(update(NotificationDelivery).where(
            NotificationDelivery.event_key == key, NotificationDelivery.status.in_(PENDING),
        ).values(status="cancelled", error="历史日程已变更"))
        session.flush()


def selected_targets(session: Session) -> list[NotificationTarget]:
    return [t for t in session.scalars(select(NotificationTarget).where(
        NotificationTarget.selected.is_(True))).all()
        if t.account_key == account_key(session, t.channel)]


def migrate_channel_notifications(session: Session) -> None:
    """Preserve v1.22.0 active recipients; a disabled master switch stays disabled."""
    marker = insert(Setting).values(key="notification_channel_settings_migrated", value="true", encrypted=False)
    claimed = session.execute(marker.on_conflict_do_nothing(index_elements=[Setting.key]))
    if claimed.rowcount == 1 and SettingsService(session).get("notifications_enabled") != "true":
        session.execute(update(NotificationTarget).values(selected=False))
        session.execute(update(NotificationDelivery).where(
            NotificationDelivery.status.in_(PENDING)).values(status="cancelled", error="通知尚未启用"))
    session.flush()


def configure_channel(session: Session, channel: str, enabled: bool,
                      target_ids: list[str], now: float | None = None) -> None:
    if channel not in CHANNELS:
        raise ValueError("不支持的通知渠道。")
    now = time.time() if now is None else now
    targets = session.scalars(select(NotificationTarget).where(NotificationTarget.channel == channel)).all()
    key = account_key(session, channel)
    valid = {t.id for t in targets if key and t.account_key == key}
    if enabled and not target_ids:
        if len(valid) == 1:
            target_ids = list(valid)
        else:
            raise ValueError("请先向该渠道的机器人发一条消息。" if not valid else "请选择该渠道的接收会话。")
    if enabled and set(target_ids) - valid:
        raise ValueError("接收会话无效或账号已变更，请先通过对应渠道发一条消息。")
    selected_ids = set(target_ids) if enabled else set()
    addresses = [(t.account_key, t.conversation_id, t.thread_id) for t in targets if t.id in selected_ids]
    if len(set(addresses)) != len(addresses):
        raise ValueError("同一个接收会话只能选择一次，请取消重复的会话选项。")
    for target in targets:
        selected = target.id in selected_ids
        if selected and not target.selected:
            target.selected_at = now
        target.selected = selected
    # Changing one channel cannot cancel or reset another channel's reminders.
    query = update(NotificationDelivery).where(
        NotificationDelivery.status.in_(PENDING),
        NotificationDelivery.target_id.in_([t.id for t in targets]),
    )
    if selected_ids:
        query = query.where(NotificationDelivery.target_id.not_in(selected_ids))
    session.execute(query.values(status="cancelled", error="该渠道通知设置已变更"))
    session.commit()


def _start(event: dict) -> datetime:
    zone = ZoneInfo(event.get("timezone") or "Asia/Shanghai")
    start = isoparse(event["start_time"])
    if event.get("is_all_day"):
        start = datetime.combine(start.date(), datetime.min.time(), zone)
    elif start.tzinfo is None:
        start = start.replace(tzinfo=zone)
    return start.astimezone(zone)


def reminder_instances(event: dict, now: float):
    start = _start(event)
    reminders = event.get("reminders")
    if reminders is None:
        reminders = [{"minutes_before": 30}]
    offsets = sorted({int(r["minutes_before"]) for r in reminders})
    if not offsets:
        return
    if any(offset < 0 or offset > 525600 for offset in offsets):
        raise ValueError("提醒提前量超出支持范围（0 至 525600 分钟）")
    recurrence = event.get("recurrence")
    if recurrence and (recurrence.get("frequency") not in {"daily", "weekly", "monthly"}
                       or int(recurrence.get("interval", 1)) < 1):
        raise ValueError("无效的重复规则")
    rule = rrulestr(to_rrule(recurrence), dtstart=start) if recurrence else None
    for offset in offsets:
        low = datetime.fromtimestamp(now - GRACE_SECONDS, timezone.utc) + timedelta(minutes=offset)
        high = datetime.fromtimestamp(now + 86400, timezone.utc) + timedelta(minutes=offset)
        occurrences = rule.between(low, high, inc=True) if rule else [start]
        for occurrence in occurrences:
            timestamp = occurrence.timestamp()
            due = timestamp - offset * 60
            if now - GRACE_SECONDS <= due <= now + 86400:
                # At most five minutes late, never after this occurrence ends.
                duration = 86400 if event.get("is_all_day") else 3600
                if event.get("end_time") and not event.get("is_all_day"):
                    end = isoparse(event["end_time"])
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=start.tzinfo)
                    duration = max(0, end.timestamp() - start.timestamp())
                yield timestamp, offset, due, min(due + GRACE_SECONDS, timestamp + duration)


def message_text(event: dict, occurrence: float, offset: int) -> str:
    zone = ZoneInfo(event.get("timezone") or "Asia/Shanghai")
    when = datetime.fromtimestamp(occurrence, zone)
    weekday = "一二三四五六日"[when.weekday()]
    time_text = f"{when:%Y-%m-%d} 周{weekday} " + ("全天" if event.get("is_all_day") else f"{when:%H:%M}")
    zone_label = "北京时间" if zone.key == "Asia/Shanghai" else zone.key
    title = " ".join(str(event.get("title") or "日程").split())[:500]
    header = "⏰ 日程提醒" if offset == 0 else f"⏰ 日程提醒 · 提前 {offset} 分钟提醒你"
    lines = [header, "", f"📌 {title}", f"🕒 {time_text}（{zone_label}）"]
    if event.get("location"):
        location = " ".join(str(event['location']).split())[:300]
        lines.append(f"📍 {location}")
    if event.get("recurrence"):
        lines.append("🔁 重复日程（引用修改将应用于整个日程）")
    return "\n".join(lines)


def resolve_reminder_reply(session: Session, source: str, conversation_id: str | None,
                           message_id: str | None, quoted_text: str | None) -> tuple[bool, EventRecord | None]:
    """Recognize reminders before generic quote matching, scoped to actual recipients.

    The boolean prevents a rejected/ambiguous reminder from falling through to a
    title-only lookup or an unrelated recent event. Old reminder text stays usable.
    """
    normalized = " ".join((quoted_text or "").split())
    is_reminder = normalized.startswith("⏰ 日程提醒")
    if not conversation_id:
        return is_reminder, None
    binding = session.get(NotificationMessageBinding, (source, conversation_id, message_id)) if message_id else None
    if not binding and not is_reminder:
        return False, None
    key = account_key(session, source)
    if not key:
        return True, None
    query = select(NotificationDelivery).join(
        NotificationTarget, NotificationTarget.id == NotificationDelivery.target_id,
    ).where(NotificationTarget.channel == source,
            NotificationTarget.conversation_id == conversation_id,
            NotificationTarget.account_key == key,
            NotificationDelivery.status.in_(["sent", "unknown"]))
    if binding:
        query = query.where(NotificationDelivery.id == binding.delivery_id)
    deliveries = session.scalars(query).all()
    matches = [d for d in deliveries if binding or " ".join(d.text.split()) == normalized]
    event_keys = {d.event_key for d in matches}
    if len(event_keys) != 1:
        return True, None
    event_key = event_keys.pop()
    event = session.get(NotificationEvent, event_key)
    if not event or not event.active:
        return True, None
    record = session.get(EventRecord, event.record_id)
    if not record or record.status != "success" or record.operation not in {"create", "update"}:
        return True, None
    # Retention can remove audit records and SQLite can reuse their integer IDs.
    record_key = (f"event:{record.event_id}" if record.event_id else
                  f"caldav:{record.caldav_uid}" if record.caldav_uid else f"record:{record.id}")
    created = record.created_at
    recorded_at = (created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created).timestamp()
    if record_key != event_key or recorded_at != event.recorded_at:
        return True, None
    return True, record


def plan_deliveries(session: Session, now: float) -> None:
    # A process can die after the platform accepted a send. Never silently resend it.
    session.execute(update(NotificationDelivery).where(
        NotificationDelivery.status == "sending", NotificationDelivery.claimed_at < now - 120,
    ).values(status="unknown", error="发送进程中断，投递结果待核对"))
    session.execute(update(NotificationDelivery).where(
        NotificationDelivery.status.in_(PENDING), NotificationDelivery.expires_at < now,
    ).values(status="expired", error="已超过补发窗口"))
    targets = selected_targets(session)
    if not targets:
        session.commit()
        return
    for row in session.scalars(select(NotificationEvent).where(NotificationEvent.active.is_(True))).all():
        try:
            event = json.loads(row.event_json)
            instances = list(reminder_instances(event, now))
            row.error = None
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError) as exc:
            row.error = f"无法生成提醒：{type(exc).__name__}"
            continue
        for occurrence, offset, due, expires in instances:
            for target in targets:
                if due < target.selected_at or expires < now:
                    continue
                ident = digest(row.event_key, occurrence, offset, target.id)
                values = dict(id=ident, event_key=row.event_key, event_version=row.version,
                              target_id=target.id, occurrence_at=occurrence, due_at=due,
                              expires_at=expires, text=message_text(event, occurrence, offset),
                              status="pending", attempts=0, next_attempt_at=due)
                # Sent/unknown/failed tasks survive content edits without duplicate delivery.
                statement = insert(NotificationDelivery).values(**values)
                session.execute(statement.on_conflict_do_update(
                    index_elements=[NotificationDelivery.id], set_=values,
                    where=NotificationDelivery.status == "cancelled"))
                # Apply wording updates to queued reminders, but preserve sent text
                # for quote matching and keep retry counts/backoff unchanged.
                session.execute(update(NotificationDelivery).where(
                    NotificationDelivery.id == ident,
                    NotificationDelivery.event_version == row.version,
                    NotificationDelivery.status.in_(PENDING),
                ).values(text=values["text"]))
    session.commit()
