from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.ai.extractor import EventExtractor
from datetime import datetime, timedelta, timezone
from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.db.models import EventRecord
from app.services.ai_provider_service import AIProviderConfig
from app.services.caldav_service import CalDAVService, CalDAVServiceError
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

PENDING_DRAFT_TTL = 24 * 3600
LAST_EVENT_WINDOW = 24 * 3600
CLEARABLE_FIELDS = {"description", "location"}
_pending_drafts: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class ChannelContext:
    source: str
    source_user_id: str
    conversation_id: str | None = None
    source_message_id: str | None = None
    reply_to_message_id: str | None = None


@runtime_checkable
class _ModelDumpable(Protocol):
    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...


@runtime_checkable
class _DictDumpable(Protocol):
    def dict(self) -> dict[str, Any]: ...


class MessageProcessor:
    async def process(
        self, session: Session, user_id: str, text: str, reply_to_message_id: str | None = None,
        source: str = "telegram", conversation_id: str | None = None, source_message_id: str | None = None,
    ) -> list[tuple[str, int | None]]:
        ctx = ChannelContext(source, user_id, conversation_id, source_message_id, reply_to_message_id)
        svc = SettingsService(session)
        config = AIProviderConfig(
            provider_type=svc.get("ai_provider_type") or "openai_compatible",
            base_url=svc.get("ai_base_url") or "https://api.openai.com/v1",
            api_key=svc.get("ai_api_key"),
            model=svc.get("ai_model"),
        )
        tz = svc.get("caldav_timezone") or "Asia/Shanghai"
        extractor = EventExtractor(config, tz)
        caldav_cfg = _caldav_config(svc)

        return await _route(session, ctx, text, extractor, caldav_cfg, svc)


def _caldav_config(svc: SettingsService) -> dict[str, Any]:
    return {
        "url": svc.get("caldav_url") or "",
        "user": svc.get("caldav_username") or "",
        "pw": svc.get("caldav_password") or "",
        "cal": svc.get("caldav_calendar_url") or "",
        "rem": int(svc.get("caldav_reminder_minutes") or "30"),
        "dur": int(svc.get("caldav_default_duration") or "60"),
    }


async def _route(session: Session, ctx: ChannelContext, text: str, extractor: EventExtractor, caldav: dict[str, Any], svc: SettingsService) -> list[tuple[str, int | None]]:
    draft_key = f"draft_{ctx.source}:{ctx.source_user_id}:{ctx.conversation_id or ''}"
    draft = _pending_drafts.get(draft_key)
    if draft and (time.time() - draft.get("ts", 0)) < PENDING_DRAFT_TTL:
        _pending_drafts.pop(draft_key, None)
        result = await extractor.merge_draft(draft.get("event", {}), text)
        if result.event and not result.missing_fields:
            r = await _write_one(session, ctx, text, result.event, caldav)
            session.commit()
            return [(_format_one(result.event), r)]
        return [("🤔 仍缺少信息，请重新描述。", None)]

    reply_to = ctx.reply_to_message_id
    target = await _find_target(session, ctx)
    if ctx.reply_to_message_id and target is None:
        return [("🤔 没有找到这条回复对应的日程。请回复我发送的某条日程消息，或重新描述要修改的日程。", None)]
    if reply_to and target and target.event_json:
        existing = json.loads(target.event_json)
        quick = _try_quick_modify(text, existing, caldav["dur"])
        if quick:
            rec_id = await _do_modify_with(session, ctx, text, target, quick, caldav)
            session.commit()
            return [(_format_modify_result(quick), rec_id)]
        mod_result = await extractor.modify(existing, text)
        if mod_result.intent == Intent.delete_event:
            return [(await _do_delete_with(session, ctx, target, caldav), None)]
        merged = _merge_event(existing, mod_result.event, caldav["dur"])
        rec_id = await _do_modify_with(session, ctx, text, target, merged, caldav)
        session.commit()
        return [(_format_modify_result(merged), rec_id)]

    result = await extractor.extract(text)
    if result.intent == Intent.delete_event:
        return [(await _do_delete(session, ctx, caldav), None)]
    if result.intent == Intent.update_event and target and result.event:
        existing = json.loads(target.event_json) if target.event_json else {}
        merged = _merge_event(existing, result.event, caldav["dur"])
        rec_id = await _do_modify_with(session, ctx, text, target, merged, caldav)
        session.commit()
        return [(_format_modify_result(merged), rec_id)]
    return await _handle_new(session, ctx, text, result, caldav, svc)


async def _find_target(session: Session, ctx: ChannelContext) -> EventRecord | None:
    deleted_uids = select(EventRecord.caldav_uid).where(
        EventRecord.operation == "delete",
        EventRecord.caldav_uid.isnot(None),
    )
    if ctx.reply_to_message_id:
        rec = session.execute(
            select(EventRecord).where(
                EventRecord.source == ctx.source,
                EventRecord.conversation_id == ctx.conversation_id,
                EventRecord.bot_message_id == ctx.reply_to_message_id,
                EventRecord.operation.in_(["create", "update"]),
                or_(
                    EventRecord.caldav_uid.is_(None),
                    ~EventRecord.caldav_uid.in_(deleted_uids),
                ),
            ).order_by(EventRecord.created_at.desc())
        ).scalar()
        if rec:
            return rec
        logger.warning(
            "Reply target not found: source=%s user_id=%s conversation_id=%s reply_to=%s",
            ctx.source, ctx.source_user_id, ctx.conversation_id, ctx.reply_to_message_id,
        )
        return None
    _cutoff = int((time.time() - LAST_EVENT_WINDOW) * 1000)
    return session.execute(
        select(EventRecord)
        .where(
            EventRecord.source == ctx.source,
            EventRecord.conversation_id == ctx.conversation_id,
            EventRecord.operation.in_(["create", "update"]),
            or_(
                EventRecord.caldav_uid.is_(None),
                ~EventRecord.caldav_uid.in_(deleted_uids),
            ),
        )
        .order_by(EventRecord.created_at.desc())
    ).scalar()


async def _do_delete_with(session: Session, ctx: ChannelContext, target: EventRecord, caldav: dict[str, Any]) -> str:
    title = target.title or "日程"
    deleted = False
    if caldav["url"]:
        cal = CalDAVService()
        deleted = await cal.delete_event(caldav["url"], caldav["user"], caldav["pw"],
                                          target.caldav_uid, target.caldav_href)
    _record(session, ctx, "delete", title, "", "success" if deleted else "failed",
            target.event_json or "", cr={"uid": target.caldav_uid},
            start_time=target.start_time or "", event_id=target.event_id)
    session.commit()
    status = "" if deleted else "（CalDAV 删除失败，但本地记录已标记）"
    return f"🗑️ 已删除日程：{title}{status}"


async def _do_modify_with(session: Session, ctx: ChannelContext, text: str, target: EventRecord, new_event: dict[str, Any], caldav: dict[str, Any]) -> int:
    title = _g(new_event, "title") or "日程"
    if caldav["url"] and target.caldav_uid:
        cal = CalDAVService()
        await cal.delete_event(caldav["url"], caldav["user"], caldav["pw"],
                               target.caldav_uid, target.caldav_href)
        result = await _write_caldav_dict(new_event, caldav)
        if result:
            target.caldav_href = result.get("href")
            target.caldav_uid = result.get("uid")
            target.start_time = new_event.get("start_time", "")
            target.event_json = json.dumps(new_event, ensure_ascii=False)
        session.commit()
    return _record(session, ctx, "update", title, text, "success",
             json.dumps(new_event, ensure_ascii=False),
             cr={"href": target.caldav_href, "uid": target.caldav_uid},
             start_time=new_event.get("start_time", ""), event_id=target.event_id)


def _g(obj: object, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _format_modify_result(event: dict[str, Any]) -> str:
    return _format_event_result(event, "✅ 日程已更新！")


def _merge_event(existing: dict[str, Any], ai_event: object, dur_minutes: int = 60) -> dict[str, Any]:
    changes = _to_dict(ai_event)
    start_changed = changes.get("start_time") and changes["start_time"] != existing.get("start_time")
    merged = dict(existing)
    for key, val in changes.items():
        if val is None:
            continue
        if val == "" and key not in CLEARABLE_FIELDS:
            continue
        merged[key] = val
    if start_changed and not changes.get("end_time"):
        merged["end_time"] = _shift_end(merged["start_time"], dur_minutes)
    return merged


def _shift_end(start_iso: str, dur_minutes: int = 60) -> str:
    st = _parse_time(start_iso)
    if st:
        return (st + timedelta(minutes=dur_minutes)).isoformat()
    return ""


def _to_dict(obj: object) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if obj is None:
        return {}
    if isinstance(obj, _ModelDumpable):
        return obj.model_dump(exclude_unset=True)
    if isinstance(obj, _DictDumpable):
        return obj.dict()
    return {}


def _try_quick_modify(text: str, existing: dict[str, Any], dur_minutes: int = 60) -> dict[str, Any] | None:
    import re
    from datetime import timedelta as td
    old_st = existing.get("start_time", "")
    if not old_st or "T" not in old_st:
        return None
    st = _parse_time(old_st)
    if not st:
        return None
    changed = False
    consumed: list[tuple[int, int]] = []

    # month+day (more specific, check first)
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if m:
        consumed.append(m.span())
        month, day = int(m.group(1)), int(m.group(2))
        try:
            st = st.replace(month=month, day=day)
        except ValueError:
            return None
        changed = True
    else:
        # day only
        m = re.search(r"(\d{1,2})[日号]", text)
        if m:
            consumed.append(m.span())
            day = int(m.group(1))
            try:
                st = st.replace(day=day)
            except ValueError:
                return None
            changed = True

    # time (always checked, even if date changed)
    h = mi = 0
    tm = re.search(r"(\d{1,2}):(\d{2})", text)
    if tm:
        consumed.append(tm.span())
        h, mi = int(tm.group(1)), int(tm.group(2))
    else:
        tm = re.search(r"(\d{1,2})点", text)
        if tm:
            consumed.append(tm.span())
            h = int(tm.group(1))
    if tm:
        old_h = st.hour
        if old_h >= 12 and h < 12:
            h += 12
        st = st.replace(hour=h, minute=mi, second=0)
        changed = True

    if not changed:
        return None
    if _quick_modify_leftover(text, consumed):
        return None

    result = dict(existing)
    new_st = st.isoformat()
    et = st + td(minutes=dur_minutes)
    new_et = et.isoformat()
    result["start_time"] = new_st
    result["end_time"] = new_et
    return result


def _quick_modify_leftover(text: str, consumed: list[tuple[int, int]]) -> str:
    import re
    chars = list(text)
    for start, end in consumed:
        for idx in range(start, end):
            chars[idx] = " "
    leftover = "".join(chars)
    leftover = re.sub(r"[\s,，。.!！?？、]+", "", leftover)
    leftover = re.sub(r"^(把)?(日程|会议|时间|日期)?(改|改成|改到|调整到|调整为|调到|调为|换到|换成|设到|设为|到|成|为|在)+", "", leftover)
    leftover = re.sub(r"(日程|会议|时间|日期)?$", "", leftover)
    return leftover


async def _do_delete(session: Session, ctx: ChannelContext, caldav: dict[str, Any]) -> str:
    target = await _find_target(session, ctx)
    if target is None:
        return "🤔 没有找到要删除的日程。请回复某条日程消息，或最近 24 小时内创建过日程。"
    title = target.title or "日程"
    deleted = False
    if caldav["url"]:
        cal = CalDAVService()
        deleted = await cal.delete_event(caldav["url"], caldav["user"], caldav["pw"],
                                          target.caldav_uid, target.caldav_href)
    _record(session, ctx, "delete", title, "", "success" if deleted else "failed",
            target.event_json or "", cr={"uid": target.caldav_uid},
            start_time=target.start_time or "", event_id=target.event_id)
    session.commit()
    status = "" if deleted else "（CalDAV 删除失败，但本地记录已标记）"
    return f"🗑️ 已删除日程：{title}{status}"


async def _handle_new(session: Session, ctx: ChannelContext, text: str, result: ExtractionResult, caldav: dict[str, Any], _svc: SettingsService) -> list[tuple[str, int | None]]:
    if result.intent == Intent.no_event:
        _record(session, ctx, "no_event", None, text, "failed", result.model_dump_json(), err="未识别到日程信息")
        session.commit()
        return [("🤔 未识别到日程信息，请补充时间和事件内容。", None)]

    if result.missing_fields:
        _pending_drafts[f"draft_{ctx.source}:{ctx.source_user_id}:{ctx.conversation_id or ''}"] = {
            "ts": time.time(),
            "event": result.event.model_dump() if result.event else {},
            "missing": result.missing_fields,
        }
        _record(session, ctx, "no_event", None, text, "failed", result.model_dump_json(),
                err=f"缺少字段：{'、'.join(result.missing_fields)}")
        return [(f"🤔 未识别到{'、'.join(result.missing_fields)}，请补充。", None)]

    if result.unsupported_reason:
        _record(session, ctx, "no_event", None, text, "failed", result.model_dump_json(),
                err=f"不支持：{result.unsupported_reason}")
        session.commit()
        return [(f"🔁 {result.unsupported_reason}", None)]

    events = result.events or ([result.event] if result.event else [])
    if not events:
        return [("🤔 未识别到日程信息，请补充时间和事件内容。", None)]

    replies = []
    for event in events:
        rec_id = await _write_one(session, ctx, text, event, caldav)
        line = _format_one(event)
        replies.append((line, rec_id))
    session.commit()
    return replies


def _format_one(event: object) -> str:
    return _format_event_result(event, "✅ 日程已安排好啦！")


def _format_event_result(event: object, header: str) -> str:
    title = _g(event, "title", "日程")
    st = _g(event, "start_time", "") or ""
    et = _g(event, "end_time", "") or ""
    loc = _g(event, "location")
    desc = _g(event, "description")
    reminders = _g(event, "reminders") or []
    lines = [header, ""]
    lines.append(f"📌 标题：{title}")
    if _g(event, "is_all_day", False) and st:
        lines.append(f"📅 日期：{st[:10]}")
    elif st:
        if et and st[:10] == et[:10]:
            lines.append(f"🕒 时间：{st[:16].replace('T', ' ')} - {et[11:16]}")
        elif et:
            lines.append(f"🕒 时间：{st[:16].replace('T', ' ')} - {et[:16].replace('T', ' ')}")
        else:
            lines.append(f"🕒 时间：{st[:16].replace('T', ' ')}")
    if loc:
        lines.append(f"📍 地点：{loc}")
    if desc:
        lines.append(f"📝 描述：{desc}")
    recurrence = _g(event, "recurrence")
    freq = recurrence.get("frequency", "") if isinstance(recurrence, dict) else getattr(recurrence, "frequency", "")
    if freq:
        lines.append(f"🔁 重复：{freq}")
    if reminders:
        first = reminders[0]
        minutes = first.get("minutes_before") if isinstance(first, dict) else getattr(first, "minutes_before", None)
        if minutes:
            lines.append(f"⏰ 提醒：提前 {minutes} 分钟")
    return "\n".join(lines)


async def _write_one(session: Session, ctx: ChannelContext, text: str, event: CalendarEvent, caldav: dict[str, Any]) -> int:
    if not getattr(event, 'reminders', None):
        from app.ai.schemas import Reminder
        event.reminders = [Reminder(minutes_before=caldav["rem"])]
    if not getattr(event, 'end_time', None) and getattr(event, 'start_time', None):
        from dateutil.parser import parse as parse_date
        dt = parse_date(event.start_time)
        event.end_time = (dt + timedelta(minutes=caldav["dur"])).isoformat()

    caldav_result = None
    error_msg = None
    if caldav["url"] and caldav["user"]:
        try:
            caldav_result = await _write_caldav(event, caldav)
        except CalDAVServiceError as exc:
            error_msg = str(exc)

    return _record(session, ctx, "create", event.title, text,
            "success" if caldav_result else "failed",
            event.model_dump_json(), caldav_result, error_msg,
            start_time=getattr(event, "start_time", ""))


async def _write_caldav_dict(event_dict: dict[str, Any], caldav: dict[str, Any]) -> dict[str, Any] | None:
    svc = CalDAVService()
    return await svc.create_event(
        caldav["url"], caldav["user"], caldav["pw"], caldav["cal"],
        event_dict["title"], cast(str, event_dict.get("start_time")), event_dict.get("end_time"),
        event_dict.get("timezone", "Asia/Shanghai"),
        event_dict.get("location"), event_dict.get("description"),
        event_dict.get("reminders"), event_dict.get("recurrence"),
        event_dict.get("is_all_day", False),
    )


async def _write_caldav(event: CalendarEvent, caldav: dict[str, Any]) -> dict[str, Any] | None:
    svc = CalDAVService()
    rec: dict[str, Any] = event.model_dump() if hasattr(event, 'model_dump') else {}
    return await svc.create_event(
        caldav["url"], caldav["user"], caldav["pw"], caldav["cal"],
        event.title, event.start_time, event.end_time, event.timezone,
        event.location, event.description,
        [{"minutes_before": r.minutes_before} for r in (event.reminders or [])],
        rec.get("recurrence"),
        event.is_all_day,
    )


def _record(session: Session, ctx: ChannelContext, op: str, title: str | None, text: str, status: str, js: str, cr: dict[str, Any] | None = None, err: str | None = None, start_time: str = "", event_id: str | None = None) -> int:
    rec = EventRecord(
        source=ctx.source, telegram_user_id=ctx.source_user_id, source_user_id=ctx.source_user_id,
        conversation_id=ctx.conversation_id, event_id=event_id or uuid.uuid4().hex, operation=op,
        title=title, start_time=start_time, status=status,
        source_message_id=ctx.source_message_id,
        original_text=(text or "")[:2000],
        event_json=(js or "")[:4000],
        caldav_uid=cr.get("uid") if cr else None,
        caldav_href=cr.get("href") if cr else None,
        error_message=err,
    )
    session.add(rec)
    session.flush()
    return rec.id


def _parse_time(iso: str):
    try:
        from dateutil.parser import parse as parse_date
        dt = parse_date(iso)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None
