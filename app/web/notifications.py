from __future__ import annotations

import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import NotificationDelivery, NotificationEvent, NotificationTarget
from app.services.notification_sender import DeliveryError, destination, send_notification
from app.services.notification_service import CHANNELS, account_key, configure
from app.services.settings_service import SettingsService
from app.web.routes import get_db, require_admin, templates, get_flash, get_error_flash, set_flash, set_error_flash

router = APIRouter(prefix="/console/notifications", dependencies=[Depends(require_admin)])
LABELS = {"pending": "等待提醒", "sending": "发送中", "sent": "渠道已接受", "retry": "等待重试",
          "failed": "发送失败", "unknown": "结果待核对", "expired": "已过期", "cancelled": "已取消"}


@router.get("", response_class=HTMLResponse)
async def notification_settings(request: Request, session: Session = Depends(get_db)):
    svc = SettingsService(session)
    try:
        zone = ZoneInfo(svc.get("caldav_timezone") or "Asia/Shanghai")
    except (ValueError, KeyError):
        zone = ZoneInfo("Asia/Shanghai")

    def display_time(value):
        return datetime.fromtimestamp(value, zone).strftime("%m-%d %H:%M:%S") if value else "—"

    targets = []
    for row in session.scalars(select(NotificationTarget).order_by(NotificationTarget.last_seen_at.desc())):
        targets.append(dict(id=row.id, channel=CHANNELS.get(row.channel, row.channel),
                            conversation=row.conversation_id, user=row.user_id, thread=row.thread_id,
                            selected=row.selected, available=account_key(session, row.channel) == row.account_key,
                            seen=display_time(row.last_seen_at),
                            context_ready=row.channel != "wechat" or bool(row.context_token)))
    targets.sort(key=lambda t: (not t["selected"], t["channel"] != "微信", t["channel"]))
    names = {t["id"]: f"{t['channel']} · {t['conversation']}" for t in targets}
    deliveries = [dict(id=r.id, target=names.get(r.target_id, "已移除的目标"), text=r.text,
                       due=display_time(r.due_at), status=LABELS.get(r.status, r.status), state=r.status,
                       attempts=r.attempts, error=r.error)
                  for r in session.scalars(select(NotificationDelivery).order_by(
                      NotificationDelivery.due_at.desc()).limit(50))]
    counts = dict(session.execute(select(NotificationDelivery.status, func.count()).group_by(
        NotificationDelivery.status)).all())
    errors = [r.error for r in session.scalars(select(NotificationEvent).where(
        NotificationEvent.active.is_(True), NotificationEvent.error.is_not(None)).limit(10))]
    return templates.TemplateResponse(request, "notifications.html", {
        "request": request, "enabled": svc.get("notifications_enabled") == "true", "targets": targets,
        "deliveries": deliveries, "counts": {LABELS.get(k, k): v for k, v in counts.items()},
        "schedule_errors": errors, "message": get_flash(request), "error": get_error_flash(request),
    })


@router.post("")
async def save_notifications(request: Request, enabled: bool = Form(False),
                             target_ids: list[str] = Form(default=[]), session: Session = Depends(get_db)):
    try:
        configure(session, enabled, target_ids)
        set_flash(request, "通知设置已保存。所有输入渠道的日程将按所选目标提醒。" if enabled else "日程通知已关闭。")
    except ValueError as exc:
        session.rollback()
        set_error_flash(request, str(exc))
    return RedirectResponse("/console/notifications", status_code=303)


@router.post("/test")
async def test_notification(request: Request, target_id: str = Form(...), session: Session = Depends(get_db)):
    target = session.get(NotificationTarget, target_id)
    if target is None:
        set_error_flash(request, "接收会话不存在。")
        return RedirectResponse("/console/notifications", status_code=303)
    try:
        dest = destination(session, target)
    except DeliveryError as exc:
        set_error_flash(request, str(exc))
        return RedirectResponse("/console/notifications", status_code=303)
    now = time.time()
    claimed = session.execute(update(NotificationTarget).where(
        NotificationTarget.id == target_id, NotificationTarget.last_test_at <= now - 30,
    ).values(last_test_at=now))
    if claimed.rowcount != 1:
        session.rollback()
        set_error_flash(request, "请等待 30 秒后再发送测试通知。")
        return RedirectResponse("/console/notifications", status_code=303)
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
    return RedirectResponse("/console/notifications", status_code=303)
