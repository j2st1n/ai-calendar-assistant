import json
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.ai.extractor import EventExtractor
from app.ai.schemas import Intent, Reminder
from app.db.models import EventRecord
from app.services.ai_provider_service import AIProviderConfig
from app.services.caldav_service import CalDAVService, CalDAVServiceError
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

PENDING_DRAFT_TTL = 24 * 3600
LAST_EVENT_WINDOW = 24 * 3600
_pending_drafts: dict[str, dict] = {}


class MessageProcessor:
    async def process(
        self, session: Session, user_id: str, text: str, reply_to_message_id: str | None = None
    ) -> list[tuple[str, int | None]]:
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

        return await _route(session, user_id, text, reply_to_message_id, extractor, caldav_cfg, svc)


def _caldav_config(svc: SettingsService) -> dict:
    return {
        "url": svc.get("caldav_url") or "",
        "user": svc.get("caldav_username") or "",
        "pw": svc.get("caldav_password") or "",
        "cal": svc.get("caldav_calendar_url") or "",
        "rem": int(svc.get("caldav_reminder_minutes") or "30"),
        "dur": int(svc.get("caldav_default_duration") or "60"),
    }


async def _route(session, user_id, text, reply_to, extractor, caldav, svc) -> list[tuple[str, int | None]]:
    draft_key = f"draft_{user_id}"
    draft = _pending_drafts.get(draft_key)
    if draft and (time.time() - draft.get("ts", 0)) < PENDING_DRAFT_TTL:
        _pending_drafts.pop(draft_key, None)
        result = await extractor.merge_draft(draft.get("event", {}), text)
        if result.event and not result.missing_fields:
            r = await _write_one(session, user_id, text, result.event, caldav)
            session.commit()
            return [(_format_one(result.event), r)]
        return [("🤔 仍缺少信息，请重新描述。", None)]

    target = await _find_target(session, user_id, reply_to)
    if reply_to and target and target.event_json:
        existing = json.loads(target.event_json)
        quick = _try_quick_modify(text, existing)
        if quick:
            await _do_modify_with(session, user_id, text, target, quick, caldav)
            session.commit()
            return [(_format_modify_result(quick), None)]
        mod_result = await extractor.modify(existing, text)
        if mod_result.intent == Intent.delete_event:
            return [(await _do_delete_with(session, user_id, target, caldav), None)]
        merged = _merge_event(existing, mod_result.event, caldav["dur"])
        await _do_modify_with(session, user_id, text, target, merged, caldav)
        session.commit()
        return [(_format_modify_result(merged), None)]

    result = await extractor.extract(text)
    if result.intent == Intent.delete_event:
        return [(await _do_delete(session, user_id, reply_to, caldav), None)]
    if result.intent == Intent.update_event and target and result.event:
        existing = json.loads(target.event_json) if target.event_json else {}
        merged = _merge_event(existing, result.event, caldav["dur"])
        await _do_modify_with(session, user_id, text, target, merged, caldav)
        session.commit()
        return [(_format_modify_result(merged), None)]
    return await _handle_new(session, user_id, text, result, caldav, svc)


async def _find_target(session, user_id, reply_to) -> EventRecord | None:
    deleted_uids = select(EventRecord.caldav_uid).where(
        EventRecord.operation == "delete",
        EventRecord.caldav_uid.isnot(None),
    )
    if reply_to:
        rec = session.execute(
            select(EventRecord).where(
                EventRecord.bot_message_id == reply_to,
                or_(
                    EventRecord.caldav_uid.is_(None),
                    ~EventRecord.caldav_uid.in_(deleted_uids),
                ),
            ).order_by(EventRecord.created_at.desc())
        ).scalar()
        if rec:
            return rec
    cutoff = int((time.time() - LAST_EVENT_WINDOW) * 1000)
    return session.execute(
        select(EventRecord)
        .where(
            EventRecord.telegram_user_id == user_id,
            EventRecord.operation.in_(["create", "update"]),
            or_(
                EventRecord.caldav_uid.is_(None),
                ~EventRecord.caldav_uid.in_(deleted_uids),
            ),
        )
        .order_by(EventRecord.created_at.desc())
    ).scalar()


async def _do_delete_with(session, user_id, target, caldav) -> str:
    title = target.title or "日程"
    deleted = False
    if caldav["url"]:
        cal = CalDAVService()
        deleted = await cal.delete_event(caldav["url"], caldav["user"], caldav["pw"],
                                          target.caldav_uid, target.caldav_href)
    _record(session, user_id, "delete", title, "", "success" if deleted else "failed",
            target.event_json or "", cr={"uid": target.caldav_uid})
    session.commit()
    status = "" if deleted else "（CalDAV 删除失败，但本地记录已标记）"
    return f"🗑️ 已删除日程：{title}{status}"


async def _do_modify_with(session, user_id, text, target, new_event, caldav):
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
    _record(session, user_id, "update", title, text, "success",
             json.dumps(new_event, ensure_ascii=False),
             cr={"href": target.caldav_href, "uid": target.caldav_uid},
             start_time=new_event.get("start_time", ""))


def _g(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _format_modify_result(event) -> str:
    title = _g(event, "title", "日程")
    st = _g(event, "start_time", "?")
    et = _g(event, "end_time")
    lines = ["✅ 日程已更新！", ""]
    lines.append(f"📌 标题：{title}")
    lines.append(f"🕒 时间：{st[:16].replace('T', ' ')} - {(et or '?')[:16].replace('T', ' ')}")
    return "\n".join(lines)


def _merge_event(existing: dict, ai_event, dur_minutes: int = 60) -> dict:
    changes = _to_dict(ai_event)
    start_changed = changes.get("start_time") and changes["start_time"] != existing.get("start_time")
    merged = dict(existing)
    for key, val in changes.items():
        if val is not None and val != "":
            merged[key] = val
    if start_changed and not changes.get("end_time"):
        merged["end_time"] = _shift_end(merged["start_time"], dur_minutes)
    return merged


def _shift_end(start_iso: str, dur_minutes: int = 60) -> str:
    st = _parse_time(start_iso)
    if st:
        return (st + timedelta(minutes=dur_minutes)).isoformat()
    return ""


def _to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if obj is None:
        return {}
    if hasattr(obj, 'model_dump'):
        return obj.model_dump()
    if hasattr(obj, 'dict'):
        return obj.dict()
    return {}


def _try_quick_modify(text: str, existing: dict) -> dict | None:
    import re
    from datetime import timedelta as td
    old_st = existing.get("start_time", "")
    if not old_st or "T" not in old_st:
        return None
    st = _parse_time(old_st)
    if not st:
        return None
    changed = False

    # month+day (more specific, check first)
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if m:
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
            day = int(m.group(1))
            try:
                st = st.replace(day=day)
            except ValueError:
                return None
            changed = True

    # time (always checked, even if date changed)
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        old_h = st.hour
        if old_h >= 12 and h < 12:
            h += 12
        st = st.replace(hour=h, minute=mi, second=0)
        changed = True

    if not changed:
        return None

    result = dict(existing)
    new_st = st.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    et = st + td(hours=1)
    new_et = et.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    result["start_time"] = new_st
    result["end_time"] = new_et
    return result


async def _do_delete(session, user_id, reply_to, caldav) -> str:
    target = await _find_target(session, user_id, reply_to)
    if target is None:
        return "🤔 没有找到要删除的日程。请回复某条日程消息，或最近 24 小时内创建过日程。"
    title = target.title or "日程"
    deleted = False
    if caldav["url"]:
        cal = CalDAVService()
        deleted = await cal.delete_event(caldav["url"], caldav["user"], caldav["pw"],
                                          target.caldav_uid, target.caldav_href)
    _record(session, user_id, "delete", title, "", "success" if deleted else "failed",
            target.event_json or "", cr={"uid": target.caldav_uid})
    session.commit()
    status = "" if deleted else "（CalDAV 删除失败，但本地记录已标记）"
    return f"🗑️ 已删除日程：{title}{status}"


async def _handle_new(session, user_id, text, result, caldav, svc) -> list[tuple[str, int | None]]:
    if result.intent == Intent.no_event:
        _record(session, user_id, "no_event", None, text, "failed", result.model_dump_json(), err="未识别到日程信息")
        session.commit()
        return [("🤔 未识别到日程信息，请补充时间和事件内容。", None)]

    if result.missing_fields:
        _pending_drafts[f"draft_{user_id}"] = {
            "ts": time.time(),
            "event": result.event.model_dump() if result.event else {},
            "missing": result.missing_fields,
        }
        _record(session, user_id, "no_event", None, text, "failed", result.model_dump_json(),
                err=f"缺少字段：{'、'.join(result.missing_fields)}")
        return [(f"🤔 未识别到{'、'.join(result.missing_fields)}，请补充。", None)]

    if result.unsupported_reason:
        _record(session, user_id, "no_event", None, text, "failed", result.model_dump_json(),
                err=f"不支持：{result.unsupported_reason}")
        session.commit()
        return [(f"🔁 {result.unsupported_reason}", None)]

    events = result.events or ([result.event] if result.event else [])
    if not events:
        return [("🤔 未识别到日程信息，请补充时间和事件内容。", None)]

    replies = []
    for event in events:
        rec_id = await _write_one(session, user_id, text, event, caldav)
        line = _format_one(event)
        replies.append((line, rec_id))
    session.commit()
    return replies


def _format_one(event) -> str:
    lines = ["✅ 日程已安排好啦！", ""]
    lines.append(f"📌 {event.title}")
    if getattr(event, 'is_all_day', False):
        lines.append(f"📅 {event.start_time[:10]}")
    elif getattr(event, 'start_time', None):
        st = event.start_time[:16].replace("T", " ")
        et = (getattr(event, 'end_time', None) or "")[:16].replace("T", " ")
        if et and st[:10] == et[:10]:
            lines.append(f"🕒 {st} - {et[11:]}")
        elif et:
            lines.append(f"🕒 {st} - {et}")
        else:
            lines.append(f"🕒 {st}")
    if getattr(event, 'location', None):
        lines.append(f"📍 {event.location}")
    if getattr(event, 'description', None):
        lines.append(f"📝 {event.description}")
    if getattr(event, 'recurrence', None):
        rec = event.recurrence
        freq = rec.get("frequency", "") if isinstance(rec, dict) else getattr(rec, 'frequency', '')
        if freq:
            lines.append(f"🔁 {freq}")
    reminders = getattr(event, 'reminders', []) or []
    if reminders and reminders[0].minutes_before:
        lines.append(f"⏰ 提前 {reminders[0].minutes_before} 分钟")
    return "\n".join(lines)


async def _write_one(session, user_id, text, event, caldav) -> int:
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

    return _record(session, user_id, "create", event.title, text,
            "success" if caldav_result else "failed",
            event.model_dump_json(), caldav_result, error_msg,
            start_time=getattr(event, "start_time", ""))


async def _write_caldav_dict(event_dict, caldav) -> dict | None:
    svc = CalDAVService()
    return await svc.create_event(
        caldav["url"], caldav["user"], caldav["pw"], caldav["cal"],
        event_dict["title"], event_dict.get("start_time"), event_dict.get("end_time"),
        event_dict.get("timezone", "Asia/Shanghai"),
        event_dict.get("location"), event_dict.get("description"),
        event_dict.get("reminders"), event_dict.get("recurrence"),
        event_dict.get("is_all_day", False),
    )


async def _write_caldav(event, caldav) -> dict | None:
    svc = CalDAVService()
    rec = event.model_dump() if hasattr(event, 'model_dump') else {}
    return await svc.create_event(
        caldav["url"], caldav["user"], caldav["pw"], caldav["cal"],
        event.title, event.start_time, event.end_time, event.timezone,
        event.location, event.description,
        [{"minutes_before": r.minutes_before} for r in (event.reminders or [])],
        rec.get("recurrence"),
        event.is_all_day,
    )


def _record(session, user, op, title, text, status, js, cr=None, err=None, start_time="") -> int:
    rec = EventRecord(
        source="telegram", telegram_user_id=user, operation=op,
        title=title, start_time=start_time, status=status,
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
