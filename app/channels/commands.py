from __future__ import annotations

import json
from datetime import date as dt_date, timedelta

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.message_processor import ChannelContext
from app.db.models import EventRecord
from app.services.settings_service import SettingsService

CommandReplies = list[tuple[str, int | None]]


async def handle_command(session: Session, ctx: ChannelContext, text: str) -> CommandReplies | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    command = parts[0].split("@", 1)[0].lower()
    args = parts[1:]
    if command == "/help":
        return [(_format_help(ctx), None)]
    if command == "/upcoming":
        return [(_format_upcoming(session, ctx, args), None)]
    if command == "/latest":
        return [_format_latest(session, ctx)]
    if command == "/status":
        return [(_format_status(session), None)]
    return None


def _format_help(ctx: ChannelContext) -> str:
    lines = [
        "👋 使用方式",
        "",
        "直接发消息即可创建日程：",
        "明天下午 3 点和张三开会，地点会议室 A",
        "",
        "回复日程消息可以修改或删除。",
        "支持图片识别（需配置）。",
    ]
    if ctx.source == "discord":
        lines.extend(["", "Discord 频道中请 @Bot 后发送消息；私聊和 Thread 可直接对话。"])
    lines.extend([
        "",
        "/help — 使用帮助",
        "/upcoming — 未来日程（/upcoming 7 = 未来 7 天，最多 14 天）",
        "/latest — 最近一条日程",
        "/status — 配置状态",
    ])
    return "\n".join(lines)


def _format_status(session: Session) -> str:
    svc = SettingsService(session)
    ai_vendor = svc.get("ai_provider_name") or "未配置"
    ai_model = svc.get("ai_model") or "未配置"
    caldav_name = svc.get("caldav_calendar_name") or "未配置"
    return f"🤖 AI：{ai_vendor} / {ai_model}\n📆 日历：{caldav_name}"


def _format_upcoming(session: Session, ctx: ChannelContext, args: list[str]) -> str:
    days = 7
    if args:
        try:
            days = min(int(args[0]), 14)
        except ValueError:
            pass
    records = _current_records(session, ctx)
    if not records:
        return f"📅 未来 {days} 天暂无日程"
    cutoff = dt_date.today().isoformat()
    end = (dt_date.today() + timedelta(days=days)).isoformat()
    active = [rec for rec in records if cutoff <= _get_start(rec) < end]
    if not active:
        return f"📅 未来 {days} 天暂无生效日程"
    active.sort(key=_get_start)
    groups: dict[str, list[EventRecord]] = {}
    for rec in active:
        groups.setdefault(_get_start(rec)[:10], []).append(rec)
    lines = [f"📅 未来 {days} 天日程", ""]
    for day in sorted(groups):
        lines.append(day)
        for rec in groups[day]:
            lines.append(f"🕒 {_get_start(rec)[11:16]}  {rec.title or '(无标题)'}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_latest(session: Session, ctx: ChannelContext) -> tuple[str, int | None]:
    records = _current_records(session, ctx)
    if not records:
        return ("📌 暂无日程记录", None)
    rec = records[0]
    data = _event_data(rec)
    lines = ["📌 最近日程", ""]
    lines.append(f"📌 标题：{rec.title or '(无标题)'}")
    start = data.get("start_time") or rec.start_time or ""
    end = data.get("end_time") or ""
    if start:
        if end and start[:10] == end[:10]:
            lines.append(f"🕒 时间：{start[:16].replace('T', ' ')} - {end[11:16]}")
        elif end:
            lines.append(f"🕒 时间：{start[:16].replace('T', ' ')} - {end[:16].replace('T', ' ')}")
        else:
            lines.append(f"🕒 时间：{start[:16].replace('T', ' ')}")
    if data.get("location"):
        lines.append(f"📍 地点：{data['location']}")
    if data.get("description"):
        lines.append(f"📝 描述：{data['description']}")
    reminders = data.get("reminders") or []
    if reminders and reminders[0].get("minutes_before"):
        lines.append(f"⏰ 提醒：提前 {reminders[0]['minutes_before']} 分钟")
    lines.extend(["", "回复这条消息可修改或删除。"])
    return ("\n".join(lines), rec.id)


def _current_records(session: Session, ctx: ChannelContext) -> list[EventRecord]:
    records = session.execute(
        select(EventRecord)
        .where(
            EventRecord.source == ctx.source,
            EventRecord.conversation_id == ctx.conversation_id,
            EventRecord.operation.in_(["create", "update", "delete"]),
            EventRecord.status == "success",
        )
        .order_by(EventRecord.created_at.desc())
    ).scalars().all()
    seen = set()
    active = []
    for rec in records:
        key = _event_key(rec)
        if key in seen:
            continue
        seen.add(key)
        if rec.operation != "delete":
            active.append(rec)
    return active


def _event_key(rec: EventRecord) -> str:
    return rec.event_id or rec.caldav_uid or f"_{rec.id}"


def _get_start(rec: EventRecord) -> str:
    return _event_data(rec).get("start_time", "") or rec.start_time or "9"


def _event_data(rec: EventRecord) -> dict[str, Any]:
    if not rec.event_json:
        return {}
    try:
        data = json.loads(rec.event_json)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
