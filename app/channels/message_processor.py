import json
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ai.extractor import EventExtractor
from app.ai.schemas import Intent, Reminder
from app.db.models import EventRecord
from app.services.ai_provider_service import AIProviderConfig
from app.services.caldav_service import CalDAVService, CalDAVServiceError
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

PENDING_DRAFT_TTL = 24 * 3600
_pending_drafts: dict[str, dict] = {}


class MessageProcessor:
    async def process(
        self, session: Session, user_id: str, text: str, reply_to_message_id: str | None = None
    ) -> str:
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


async def _route(session, user_id, text, reply_to, extractor, caldav, svc):
    draft_key = f"draft_{user_id}"
    draft = _pending_drafts.get(draft_key)
    if draft and (time.time() - draft.get("ts", 0)) < PENDING_DRAFT_TTL:
        _pending_drafts.pop(draft_key, None)
        result = await extractor.merge_draft(draft.get("event", {}), text)
        if result.event and not result.missing_fields:
            return await _write(session, user_id, text, result.event, caldav, svc)
        return "🤔 仍缺少信息，请重新描述。"

    return await _new(session, user_id, text, extractor, caldav, svc)


async def _new(session, user_id, text, extractor, caldav, svc):
    result = await extractor.extract(text)

    if result.intent == Intent.no_event:
        _record(session, user_id, "no_event", None, text, "pending", result.model_dump_json())
        session.commit()
        return "🤔 未识别到日程信息，请补充时间和事件内容。"

    if result.missing_fields:
        _pending_drafts[f"draft_{user_id}"] = {
            "ts": time.time(),
            "event": result.event.model_dump() if result.event else {},
            "missing": result.missing_fields,
        }
        _record(session, user_id, "no_event", None, text, "pending", result.model_dump_json())
        session.commit()
        return f"🤔 未识别到{'、'.join(result.missing_fields)}，请补充。"

    if result.unsupported_reason:
        _record(session, user_id, "no_event", None, text, "pending", result.model_dump_json())
        session.commit()
        return f"🔁 {result.unsupported_reason}"

    event = result.event
    if event is None:
        return "🤔 未识别到日程信息，请补充时间和事件内容。"

    if event.start_time and _parse_time(event.start_time) and _parse_time(event.start_time) < datetime.now(timezone.utc):
        _pending_drafts[f"past_{user_id}"] = {"ts": time.time(), "event": event.model_dump()}
        return '⏳ 识别到日程已开始，是否需要添加？\n回复"是"添加，回复"否"取消。'

    return await _write(session, user_id, text, event, caldav, svc)


async def _write(session, user_id, text, event, caldav, svc):
    if not event.reminders:
        event.reminders = [Reminder(minutes_before=caldav["rem"])]
    if not event.end_time and event.start_time:
        from dateutil.parser import parse as parse_date
        dt = parse_date(event.start_time)
        event.end_time = (dt + timedelta(minutes=caldav["dur"])).isoformat()

    lines = ["✅ 日程已安排好啦！", ""]
    lines.append(f"📌 标题：{event.title}")
    if event.is_all_day:
        lines.append(f"📅 日期：{event.start_time[:10]}")
    else:
        st = event.start_time[:16].replace("T", " ")
        et = event.end_time[:16].replace("T", " ") if event.end_time else "?"
        lines.append(f"🕒 时间：{st} - {et}")
    if event.location:
        lines.append(f"📍 地点：{event.location}")
    if event.recurrence:
        freq = event.recurrence.get("frequency", "") if isinstance(event.recurrence, dict) else getattr(event.recurrence, 'frequency', '')
        lines.append(f"🔁 重复：{freq}")
    lines.append(f"⏰ 提醒：提前 {event.reminders[0].minutes_before} 分钟")
    lines.append("")
    lines.append("想改的话，直接回复这条消息：")
    lines.append('"时间改成下午4点"')
    lines.append('"删除这条"')

    caldav_result = None
    error_msg = None
    if caldav["url"] and caldav["user"]:
        try:
            svc = CalDAVService()
            rec = event.model_dump() if hasattr(event, 'model_dump') else {}
            caldav_result = await svc.create_event(
                caldav["url"], caldav["user"], caldav["pw"], caldav["cal"],
                event.title, event.start_time, event.end_time, event.timezone,
                event.location, event.description,
                [{"minutes_before": r.minutes_before} for r in (event.reminders or [])],
                rec.get("recurrence"),
                event.is_all_day,
            )
            if caldav_result:
                lines.append("✅ 已写入日历")
        except CalDAVServiceError as exc:
            error_msg = str(exc)
            lines.append(f"❌ 写入日历失败：{error_msg}")

    _record(session, user_id, "create", event.title, text,
            "success" if caldav_result else ("failed" if error_msg else "pending"),
            event.model_dump_json(), caldav_result, error_msg)
    session.commit()
    return "\n".join(lines)


def _record(session, user, op, title, text, status, js, cr=None, err=None):
    session.add(EventRecord(
        source="telegram", telegram_user_id=user, operation=op,
        title=title, start_time="", status=status,
        original_text=(text or "")[:2000],
        event_json=(js or "")[:4000],
        caldav_uid=cr.get("uid") if cr else None,
        caldav_href=cr.get("href") if cr else None,
        error_message=err,
    ))


def _parse_time(iso: str):
    try:
        from dateutil.parser import parse as parse_date
        dt = parse_date(iso)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None
