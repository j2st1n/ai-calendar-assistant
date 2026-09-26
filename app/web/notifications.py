from __future__ import annotations

import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import NotificationDelivery, NotificationEvent, NotificationTarget
from app.services.notification_sender import DeliveryError, destination, send_notification
from app.services.notification_service import CHANNELS, account_key, configure_channel
from app.services.settings_service import SettingsService
from app.web.routes import get_db, require_admin, set_flash, set_error_flash

router = APIRouter(prefix="/console", dependencies=[Depends(require_admin)])
LABELS = {"pending": "等待提醒", "sending": "发送中", "sent": "渠道已接受", "retry": "等待重试",
          "failed": "发送失败", "unknown": "结果待核对", "expired": "已过期", "cancelled": "已取消"}


def channel_notification_panels(session: Session) -> dict:
    svc = SettingsService(session)
    try:
        zone = ZoneInfo(svc.get("caldav_timezone") or "Asia/Shanghai")
    except (ValueError, KeyError):
        zone = ZoneInfo("Asia/Shanghai")

    def display_time(value):
        return datetime.fromtimestamp(value, zone).strftime("%m-%d %H:%M:%S") if value else "—"

    panels = {}
    errors = [r.error for r in session.scalars(select(NotificationEvent).where(
        NotificationEvent.active.is_(True), NotificationEvent.error.is_not(None)).limit(5))]
    for channel, label in CHANNELS.items():
        rows = session.scalars(select(NotificationTarget).where(NotificationTarget.channel == channel)
                               .order_by(NotificationTarget.last_seen_at.desc())).all()
        key = account_key(session, channel)
        targets = [dict(id=r.id, conversation=r.conversation_id, user=r.user_id, thread=r.thread_id,
                        selected=r.selected and r.account_key == key, available=r.account_key == key,
                        seen=display_time(r.last_seen_at)) for r in rows]
        targets.sort(key=lambda t: (not t["selected"], not t["available"]))
        names = {t["id"]: t["conversation"] + (" · 话题 " + t["thread"] if t["thread"] else "") for t in targets}
        ids = [r.id for r in rows]
        deliveries = [dict(target=names[r.target_id], text=r.text, due=display_time(r.due_at),
                           status=LABELS.get(r.status, r.status), attempts=r.attempts, error=r.error)
                      for r in session.scalars(select(NotificationDelivery).where(
                          NotificationDelivery.target_id.in_(ids)).order_by(NotificationDelivery.due_at.desc()).limit(20))]
        counts = dict(session.execute(select(NotificationDelivery.status, func.count()).where(
            NotificationDelivery.target_id.in_(ids)).group_by(NotificationDelivery.status)).all())
        panels[channel] = dict(label=label, enabled=any(t["selected"] for t in targets),
                               targets=targets, available=[t for t in targets if t["available"]],
                               deliveries=deliveries, counts={LABELS.get(k, k): v for k, v in counts.items()},
                               schedule_errors=errors)
    return panels


def channel_url(channel: str) -> str:
    if channel not in CHANNELS:
        raise HTTPException(status_code=404)
    return f"/console/channels?tab={channel}#notifications-{channel}"


@router.get("/notifications")
async def old_notification_page():
    return RedirectResponse("/console/channels", status_code=303)


@router.post("/notifications")
async def old_notification_form(request: Request):
    set_flash(request, "通知开关已移入各消息渠道，请在对应渠道内设置。")
    return RedirectResponse("/console/channels", status_code=303)


@router.post("/channels/{channel}/notifications")
async def save_notifications(channel: str, request: Request, enabled: bool = Form(False),
                             target_ids: list[str] = Form(default=[]), session: Session = Depends(get_db)):
    url = channel_url(channel)
    try:
        configure_channel(session, channel, enabled, target_ids)
        set_flash(request, f"{CHANNELS[channel]}已作为通知渠道，所有来源的日程都会按提醒时间发送。" if enabled else f"已关闭{CHANNELS[channel]}的日程通知。")
    except ValueError as exc:
        session.rollback()
        set_error_flash(request, str(exc))
    return RedirectResponse(url, status_code=303)


@router.post("/channels/{channel}/notifications/test")
async def test_notification(channel: str, request: Request, target_id: str = Form(...), session: Session = Depends(get_db)):
    url = channel_url(channel)
    target = session.get(NotificationTarget, target_id)
    if target is None or target.channel != channel:
        set_error_flash(request, "接收会话不存在。")
        return RedirectResponse(url, status_code=303)
    try:
        dest = destination(session, target)
    except DeliveryError as exc:
        set_error_flash(request, str(exc))
        return RedirectResponse(url, status_code=303)
    now = time.time()
    claimed = session.execute(update(NotificationTarget).where(
        NotificationTarget.id == target_id, NotificationTarget.last_test_at <= now - 30,
    ).values(last_test_at=now))
    if claimed.rowcount != 1:
        session.rollback()
        set_error_flash(request, "请等待 30 秒后再发送测试通知。")
        return RedirectResponse(url, status_code=303)
    ident = uuid.uuid4().hex
    row = NotificationDelivery(id=ident, event_key=f"test:{ident}", event_version="test",
                               target_id=target_id, occurrence_at=now, due_at=now, expires_at=now + 300,
                               text="🔔 日程通知测试\n这是一条手动发送的测试消息，请确认已收到。",
                               status="sending", attempts=1, next_attempt_at=now, claimed_at=now)
    session.add(row)
    session.commit()
    try:
        await send_notification(dest, row.text)
        row.status = "sent"
        row.sent_at = time.time()
        set_flash(request, "渠道已接受测试消息，请在接收端确认实际到达。")
    except DeliveryError as exc:
        row.status = "unknown" if exc.uncertain else "failed"
        row.error = str(exc)
        set_error_flash(request, str(exc))
    except Exception:
        row.status = "unknown"
        row.error = "测试发送异常，实际到达情况请到接收端核对。"
        set_error_flash(request, row.error)
    session.commit()
    return RedirectResponse(url, status_code=303)
