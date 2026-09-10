from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.channels.message_bindings import resolve_bot_message

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
AMBIGUITY_TTL = 600
CLEARABLE_FIELDS = {"description", "location"}
_pending_drafts: dict[str, dict[str, Any]] = {}
_pending_ambiguities: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class ChannelContext:
    source: str
    source_user_id: str
    conversation_id: str | None = None
    source_message_id: str | None = None
    reply_to_message_id: str | None = None
    quoted_text: str | None = None
    quote_reference_present: bool = False


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
        quoted_text: str | None = None, quote_reference_present: bool = False,
    ) -> list[tuple[str, int | None]]:
        ctx = ChannelContext(
            source, user_id, conversation_id, source_message_id, reply_to_message_id,
            quoted_text, quote_reference_present,
        )
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
        "ssl": svc.get("caldav_ssl_verify") != "false",
    }


async def _route(session: Session, ctx: ChannelContext, text: str, extractor: EventExtractor, caldav: dict[str, Any], svc: SettingsService) -> list[tuple[str, int | None]]:
    draft_key = f"draft_{ctx.source}:{ctx.source_user_id}:{ctx.conversation_id or ''}"
    draft = _pending_drafts.get(draft_key)
    if draft and (time.time() - draft.get("ts", 0)) < PENDING_DRAFT_TTL:
        _ = _pending_drafts.pop(draft_key, None)
        result = await extractor.merge_draft(draft.get("event", {}), text)
        if result.event and not result.missing_fields:
            r, caldav_ok = await _write_one(session, ctx, text, result.event, caldav)
            session.commit()
            if caldav_ok is False:
                return [(_format_one(result.event, "⚠️ 日程已记录到本地，但日历同步失败"), r)]
            return [(_format_one(result.event), r)]
        return [("🤔 仍缺少信息，请重新描述。", None)]

    ambiguity_key = f"ambiguity_{ctx.source}:{ctx.source_user_id}:{ctx.conversation_id or ''}"
    ambiguity = _pending_ambiguities.get(ambiguity_key)
    if ambiguity and (time.time() - ambiguity.get("ts", 0)) < AMBIGUITY_TTL:
        if _is_cancel_command(text):
            _pending_ambiguities.pop(ambiguity_key, None)
            return [("已取消操作。", None)]
        cand_ids = ambiguity.get("candidate_ids", [])
        sel_idx = _parse_selection_index(text, len(cand_ids))
        if sel_idx is not None:
            _pending_ambiguities.pop(ambiguity_key, None)
            target = session.get(EventRecord, cand_ids[sel_idx - 1])
            if target and _latest_record_for_event(session, [], target) is target:
                action = ambiguity.get("action")
                if action == "delete":
                    return [(await _do_delete_with(session, ctx, target, caldav), None)]
                elif action == "update":
                    existing = json.loads(target.event_json) if target.event_json else {}
                    new_evt = ambiguity.get("new_event") or {}
                    merged = _merge_event(existing, new_evt, caldav["dur"])
                    rec_id, warning = await _do_modify_with(session, ctx, ambiguity.get("text", text), target, merged, caldav)
                    session.commit()
                    return [(_format_modify_result(merged, warning, old_event=existing), rec_id)]
            return [("🤔 目标日程未找到或已变更。", None)]
        elif _looks_like_invalid_selection_index(text):
            return [(f"🤔 序号超出有效范围，请回复 1 到 {len(cand_ids)} 之间的序号确认，或回复「取消」放弃操作。", None)]

    if _is_retry_command(text):
        return await _handle_batch_retry(session, ctx, caldav)

    reply_to = ctx.reply_to_message_id
    if ctx.quote_reference_present and not reply_to and not ctx.quoted_text:
        logger.warning(
            "Quote reference unreadable: source=%s source_message_id=%s",
            ctx.source, ctx.source_message_id,
        )
        _record_quote_failure(session, ctx, "quote_reference_unreadable")
        return [(
            "🤔 检测到微信引用，但无法读取被引用的日程。请重新引用我发送的日程确认消息。",
            None,
        )]
    target = await _find_target(session, ctx)
    if ctx.reply_to_message_id and target is None:
        _record_quote_failure(session, ctx, "reply_target_not_found")
        return [("🤔 没有找到这条回复对应的日程。请回复我发送的某条日程消息，或重新描述要修改的日程。", None)]
    if ctx.quoted_text and target is None:
        _record_quote_failure(session, ctx, "quoted_target_not_found")
        return [("🤔 没有找到引用消息对应的日程。请引用我发送的日程确认消息，或说“删除xx日程”。", None)]
    if target and target.event_json and (reply_to or ctx.quoted_text):
        existing = json.loads(target.event_json)
        if _is_delete_command(text):
            return [(await _do_delete_with(session, ctx, target, caldav), None)]
        quick = _try_quick_modify(text, existing, caldav["dur"])
        if quick:
            rec_id, warning = await _do_modify_with(session, ctx, text, target, quick, caldav)
            session.commit()
            return [(_format_modify_result(quick, warning, old_event=existing), rec_id)]
        mod_result = await extractor.modify(existing, text)
        if mod_result.error_type or mod_result.missing_fields or mod_result.unsupported_reason:
            return [("🤔 未获得有效修改，请明确说明要修改的内容。日程未变更。", None)]
        if mod_result.intent == Intent.delete_event:
            return [(await _do_delete_with(session, ctx, target, caldav), None)]
        if mod_result.intent != Intent.update_event or mod_result.event is None:
            return [("🤔 未识别到修改内容，请说明要调整的时间、地点或其他信息。日程未变更。", None)]
        merged = _merge_event(existing, mod_result.event, caldav["dur"])
        rec_id, warning = await _do_modify_with(session, ctx, text, target, merged, caldav)
        session.commit()
        return [(_format_modify_result(merged, warning, old_event=existing), rec_id)]

    if not reply_to and not ctx.quoted_text and _is_delete_command(text):
        active_candidates = _get_active_recent_events(session, ctx)
        if len(active_candidates) > 1:
            _record_ambiguity_block(session, ctx, "delete", text, len(active_candidates))
            _pending_ambiguities[ambiguity_key] = {
                "ts": time.time(),
                "action": "delete",
                "text": text,
                "candidate_ids": [c.id for c in active_candidates],
            }
            return [(_format_ambiguity_prompt("delete", active_candidates), None)]
        elif len(active_candidates) == 1:
            return [(await _do_delete_with(session, ctx, active_candidates[0], caldav), None)]
        else:
            return [("🤔 没有找到要删除的日程。请回复某条日程消息，或最近 24 小时内创建过日程。", None)]

    result = await extractor.extract(text)
    if result.intent == Intent.delete_event:
        active_candidates = _get_active_recent_events(session, ctx)
        query_title = result.event.title if (result.event and result.event.title) else _extract_target_title(text)
        matched = _filter_candidates_by_title(active_candidates, query_title)
        if len(matched) > 1:
            _record_ambiguity_block(session, ctx, "delete", text, len(matched))
            _pending_ambiguities[ambiguity_key] = {
                "ts": time.time(),
                "action": "delete",
                "text": text,
                "candidate_ids": [c.id for c in matched],
            }
            return [(_format_ambiguity_prompt("delete", matched), None)]
        elif len(matched) == 1:
            return [(await _do_delete_with(session, ctx, matched[0], caldav), None)]
        else:
            return [("🤔 没有找到要删除的日程。请回复某条日程消息，或最近 24 小时内创建过日程。", None)]

    if result.intent == Intent.update_event and result.event:
        active_candidates = _get_active_recent_events(session, ctx)
        query_title = result.event.title if (result.event and result.event.title) else _extract_target_title(text)
        matched = _filter_candidates_by_title(active_candidates, query_title)
        if len(matched) > 1:
            _record_ambiguity_block(session, ctx, "update", text, len(matched))
            _pending_ambiguities[ambiguity_key] = {
                "ts": time.time(),
                "action": "update",
                "text": text,
                "candidate_ids": [c.id for c in matched],
                "new_event": _to_dict(result.event),
            }
            return [(_format_ambiguity_prompt("update", matched), None)]
        elif len(matched) == 1:
            target = matched[0]
            existing = json.loads(target.event_json) if target.event_json else {}
            merged = _merge_event(existing, result.event, caldav["dur"])
            rec_id, warning = await _do_modify_with(session, ctx, text, target, merged, caldav)
            session.commit()
            return [(_format_modify_result(merged, warning, old_event=existing), rec_id)]
        else:
            return [("🤔 没有找到要修改的日程。请回复某条日程消息，或重新描述要修改的日程。", None)]

    return await _handle_new(session, ctx, text, result, caldav, svc)


async def _find_target(session: Session, ctx: ChannelContext) -> EventRecord | None:
    base_filter = [
        EventRecord.source == ctx.source,
        EventRecord.conversation_id == ctx.conversation_id,
        EventRecord.operation.in_(["create", "update"]),
    ]
    if not ctx.conversation_id:
        base_filter.append(EventRecord.source_user_id == ctx.source_user_id)

    if ctx.reply_to_message_id:
        bound = resolve_bot_message(session, ctx.source, ctx.conversation_id, ctx.reply_to_message_id)
        if bound:
            # The binding authorizes the displayed list item; follow its original event history.
            base_filter[0] = EventRecord.source == bound.source
            base_filter[1] = EventRecord.conversation_id == bound.conversation_id
        rec = session.execute(
            select(EventRecord).where(
                *base_filter,
                EventRecord.id == bound.id if bound else EventRecord.bot_message_id == ctx.reply_to_message_id,
            ).order_by(EventRecord.created_at.desc(), EventRecord.id.desc())
        ).scalar()
        if rec:
            return _latest_record_for_event(session, base_filter, rec)
        if ctx.quoted_text:
            match = await _match_by_quoted_text(session, base_filter, ctx.quoted_text)
            if match:
                return match
        logger.warning(
            "Reply target not found: source=%s source_message_id=%s reply_to=%s quote_text_present=%s",
            ctx.source, ctx.source_message_id, ctx.reply_to_message_id, bool(ctx.quoted_text),
        )
        return None

    if ctx.quoted_text:
        match = await _match_by_quoted_text(session, base_filter, ctx.quoted_text)
        if match:
            return match
        logger.warning(
            "Quoted target not found: source=%s source_message_id=%s quote_length=%d",
            ctx.source, ctx.source_message_id, len(ctx.quoted_text),
        )
        return None

    candidates = _get_active_recent_events(session, ctx)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _get_active_recent_events(session: Session, ctx: ChannelContext) -> list[EventRecord]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=LAST_EVENT_WINDOW)
    scope = [EventRecord.source == ctx.source, EventRecord.conversation_id == ctx.conversation_id]
    if not ctx.conversation_id:
        scope.append(EventRecord.source_user_id == ctx.source_user_id)
    records = session.execute(
        select(EventRecord).where(*scope, EventRecord.operation.in_(["create", "update"]),
                                  EventRecord.created_at >= cutoff)
        .order_by(EventRecord.created_at.desc(), EventRecord.id.desc())
    ).scalars().all()
    seen: set[int] = set()
    active = []
    for rec in records:
        current = _latest_record_for_event(session, [], rec)
        if current and current.id not in seen:
            seen.add(current.id)
            active.append(current)
    return active


def _filter_candidates_by_title(records: list[EventRecord], title_query: str | None) -> list[EventRecord]:
    if not title_query:
        return records
    q = title_query.strip().lower()
    if not q or q in {"日程", "会议", "待办", "事件", "任务"}:
        return records
    matched = []
    for r in records:
        rtitle = (r.title or "").strip().lower()
        if rtitle and (q in rtitle or rtitle in q):
            matched.append(r)
    return matched


def _extract_target_title(text: str) -> str | None:
    m = re.search(r"(?:删除|删掉|取消|修改|把)(?:这[个条]?)?([^\s,，。!！?？]+?)(?:日程|会议|改到|调整到|调到|换到|$)", text)
    if m:
        extracted = m.group(1).strip()
        if extracted and extracted not in {"这", "那个", "此", "上个"}:
            return extracted
    return None


def _is_cancel_command(text: str) -> bool:
    clean = re.sub(r"[\s,，。.!！?？、]+", "", text)
    return clean in {"取消", "算了", "不用了", "放弃", "取消操作"}


def _looks_like_invalid_selection_index(text: str) -> bool:
    clean = text.strip()
    return bool(re.match(r"^(?:确认|选|第)?\s*([0-9]+|[一二三四五六七八九十])\s*(?:个|项|号)?$", clean))


def _parse_selection_index(text: str, max_idx: int) -> int | None:
    clean = text.strip()
    cn_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    m = re.match(r"^(?:确认|选|第)?\s*([0-9]+)\s*(?:个|项|号)?$", clean)
    if m:
        val = int(m.group(1))
        if 1 <= val <= max_idx:
            return val
        return None
    m = re.match(r"^(?:确认|选|第)?\s*([一二三四五六七八九十])\s*(?:个|项|号)?$", clean)
    if m:
        val = cn_map.get(m.group(1))
        if val and 1 <= val <= max_idx:
            return val
    return None


def _format_ambiguity_prompt(action: str, candidates: list[EventRecord]) -> str:
    action_text = "删除" if action == "delete" else "修改"
    lines = [
        f"⚠️ 发现多条匹配的日程，为防误{action_text}已暂停执行。请回复对应序号确认：",
        "",
    ]
    for idx, rec in enumerate(candidates, 1):
        title = rec.title or "日程"
        st = rec.start_time or ""
        time_str = st[:16].replace("T", " ") if st else "(未设置时间)"
        lines.append(f"[{idx}] 📌 {title}")
        lines.append(f"    🕒 时间：{time_str}")
    lines.append("")
    lines.append(f"💡 请直接回复序号（如「1」或「确认 1」）继续{action_text}，或回复「取消」放弃。")
    return "\n".join(lines)


def _record_ambiguity_block(
    session: Session,
    ctx: ChannelContext,
    action: str,
    text: str,
    candidate_count: int,
) -> int:
    rec_id = _record(
        session,
        ctx,
        "ambiguity_guard",
        None,
        text,
        "failed",
        json.dumps({"action": action, "candidate_count": candidate_count}, ensure_ascii=False),
        err=f"命中多条候选日程（共 {candidate_count} 项），阻断执行并引导用户确认",
        failure_phase="validation",
    )
    session.commit()
    return rec_id


def _parse_title_and_start_from_quote(quoted_text: str) -> tuple[str | None, str | None]:
    """Parse title and start_time prefix from a formatted bot reply quote.

    Returns (title, start_time_prefix) where start_time_prefix is like '2026-06-02T15:00'.
    Returns (None, None) if not parseable.
    """
    import re
    title = None
    start_prefix = None

    m = re.search(r"📌\s*标题[：:]\s*(.+)", quoted_text)
    if m:
        title = m.group(1).strip()

    m = re.search(r"🕒\s*时间[：:]\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", quoted_text)
    if m:
        start_prefix = f"{m.group(1)}T{m.group(2)}"

    return title, start_prefix


async def _match_by_quoted_text(
    session: Session, base_filter: list, quoted_text: str,
) -> EventRecord | None:
    title, start_prefix = _parse_title_and_start_from_quote(quoted_text)
    if not title or not start_prefix:
        logger.debug(
            "Could not parse title/start from quoted text: quote_length=%d",
            len(quoted_text),
        )
        return None

    candidates = session.execute(
        select(EventRecord).where(
            *base_filter,
            EventRecord.title == title,
            EventRecord.start_time.startswith(start_prefix),
        ).order_by(EventRecord.created_at.desc(), EventRecord.id.desc())
    ).scalars().all()

    event_ids = {candidate.event_id for candidate in candidates if candidate.event_id}
    if len(candidates) == 1 or len(event_ids) == 1:
        return _latest_record_for_event(session, base_filter, candidates[0])
    if len(candidates) > 1:
        logger.debug(
            "Quote match ambiguous: candidates=%d",
            len(candidates),
        )
    return None


def _latest_record_for_event(session: Session, base_filter: list, rec: EventRecord) -> EventRecord | None:
    # Bindings establish access; event history may continue through another channel.
    identity = (EventRecord.event_id == rec.event_id if rec.event_id else
                EventRecord.caldav_uid == rec.caldav_uid if rec.caldav_uid else
                EventRecord.id == rec.id)
    latest = session.execute(
        select(EventRecord).where(identity, EventRecord.status == "success",
                                  EventRecord.operation.in_(["create", "update", "delete"]))
        .order_by(EventRecord.created_at.desc(), EventRecord.id.desc()).limit(1)
    ).scalar_one_or_none()
    if latest:
        return None if latest.operation == "delete" else latest
    return rec if rec.operation in {"create", "update"} else None


async def _do_delete_with(session: Session, ctx: ChannelContext, target: EventRecord, caldav: dict[str, Any]) -> str:
    title = target.title or "日程"
    deleted = not bool(caldav["url"])
    if caldav["url"]:
        cal = CalDAVService()
        deleted = await cal.delete_event(caldav["url"], caldav["user"], caldav["pw"],
                                          target.caldav_uid, target.caldav_href, ssl_verify=caldav["ssl"])
    _ = _record(session, ctx, "delete", title, "", "success" if deleted else "failed",
                target.event_json or "", cr={"uid": target.caldav_uid, "href": target.caldav_href, "etag": target.remote_etag},
                start_time=target.start_time or "", event_id=target.event_id,
                failure_phase="write" if not deleted else None,
                snapshot_json=target.event_json,
                remote_etag=target.remote_etag)
    session.commit()
    if not deleted:
        return f"⚠️ CalDAV 删除失败：{title}。未标记为已删除，请核对日历后重试。"
    return f"🗑️ 已删除日程：{title}"


async def _do_modify_with(session: Session, ctx: ChannelContext, text: str, target: EventRecord, new_event: dict[str, Any], caldav: dict[str, Any]) -> tuple[int, str | None]:
    existing = json.loads(target.event_json) if target.event_json else {}
    if new_event == existing:
        return target.id, "ℹ️ 日程内容没有变化，未修改日历。"
    title = _g(new_event, "title") or "日程"
    status = "success"
    error_msg = None
    warning = None
    result = None
    pre_snapshot_json = target.event_json
    if caldav["url"] and target.caldav_uid:
        old_uid = target.caldav_uid
        old_href = target.caldav_href
        try:
            result = await _write_caldav_dict(new_event, caldav)
        except CalDAVServiceError as exc:
            result = None
            error_msg = f"CalDAV 新日程创建失败，原日程已保留：{exc}"
        if result:
            cal = CalDAVService()
            deleted_old = await cal.delete_event(caldav["url"], caldav["user"], caldav["pw"], old_uid, old_href, ssl_verify=caldav["ssl"])
            if not deleted_old:
                status = "failed"
                error_msg = "旧日程删除失败，可能产生重复日程"
                warning = "⚠️ 日程已更新，但旧日程删除失败，可能出现重复日程"
        else:
            status = "failed"
            error_msg = error_msg or "CalDAV 新日程创建失败，原日程已保留"
            warning = "⚠️ 日程已记录到本地，但日历同步可能失败"
        session.commit()
    rec_id = _record(session, ctx, "update", title, text, status,
             json.dumps(new_event, ensure_ascii=False),
             cr={"href": result.get("href"), "uid": result.get("uid"), "etag": result.get("etag")} if result else {"href": target.caldav_href, "uid": target.caldav_uid, "etag": target.remote_etag},
             err=error_msg,
             start_time=new_event.get("start_time", ""), event_id=target.event_id,
             failure_phase="write" if status == "failed" else None,
             snapshot_json=pre_snapshot_json,
             remote_etag=result.get("etag") if result else target.remote_etag)
    return rec_id, warning


def _g(obj: object, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _format_time_span(event: dict[str, Any]) -> str:
    is_all_day = _g(event, "is_all_day", False)
    st = _g(event, "start_time", "") or ""
    et = _g(event, "end_time", "") or ""
    if is_all_day and st:
        return f"{st[:10]} (全天)"
    if not st:
        return "(未指定时间)"
    if et and st[:10] == et[:10]:
        return f"{st[:16].replace('T', ' ')} - {et[11:16]}"
    elif et:
        return f"{st[:16].replace('T', ' ')} - {et[:16].replace('T', ' ')}"
    return st[:16].replace("T", " ")


def _format_diff_summary(old_event: dict[str, Any], new_event: dict[str, Any]) -> list[str]:
    diffs: list[str] = []

    old_title = (_g(old_event, "title") or "").strip()
    new_title = (_g(new_event, "title") or "").strip()
    if old_title and new_title and old_title != new_title:
        diffs.append(f"• 标题：{old_title} ➔ {new_title}")

    old_time = _format_time_span(old_event)
    new_time = _format_time_span(new_event)
    if old_time != new_time and old_time != "(未指定时间)" and new_time != "(未指定时间)":
        diffs.append(f"• 时间：{old_time} ➔ {new_time}")

    old_loc = (_g(old_event, "location") or "").strip()
    new_loc = (_g(new_event, "location") or "").strip()
    if old_loc != new_loc:
        old_disp = old_loc or "(无)"
        new_disp = new_loc or "(已清除)"
        diffs.append(f"• 地点：{old_disp} ➔ {new_disp}")

    old_desc = (_g(old_event, "description") or "").strip()
    new_desc = (_g(new_event, "description") or "").strip()
    if old_desc != new_desc:
        old_disp = old_desc or "(无)"
        new_disp = new_desc or "(已清除)"
        diffs.append(f"• 描述：{old_disp} ➔ {new_disp}")

    return diffs


def _format_modify_result(
    event: dict[str, Any],
    warning: str | None = None,
    old_event: dict[str, Any] | None = None,
) -> str:
    header = warning or "✅ 日程已更新！"
    base_result = _format_event_result(event, header)
    if not old_event:
        return base_result

    diff_lines = _format_diff_summary(old_event, event)
    if not diff_lines:
        return base_result

    lines = base_result.split("\n")
    diff_block = ["🔄 修改对照："] + diff_lines + [""]
    if len(lines) > 1 and lines[1] == "":
        new_lines = [lines[0], ""] + diff_block + lines[2:]
    else:
        new_lines = [lines[0], ""] + diff_block + lines[1:]
    return "\n".join(new_lines)


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
    tm = re.search(r"(\d{1,2})[:：](\d{2})", text)
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


def _is_delete_command(text: str) -> bool:
    import re
    normalized = re.sub(r"[\s,，。.!！?？、]+", "", text)
    return normalized in {"删", "删除", "删掉", "取消", "取消日程", "删除日程", "删掉日程"}


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
                                          target.caldav_uid, target.caldav_href, ssl_verify=caldav["ssl"])
    _ = _record(session, ctx, "delete", title, "", "success" if deleted else "failed",
                target.event_json or "", cr={"uid": target.caldav_uid},
                start_time=target.start_time or "", event_id=target.event_id)
    session.commit()
    status = "" if deleted else "（CalDAV 删除失败，但本地记录已标记）"
    return f"🗑️ 已删除日程：{title}{status}"


async def _handle_new(session: Session, ctx: ChannelContext, text: str, result: ExtractionResult, caldav: dict[str, Any], _svc: SettingsService) -> list[tuple[str, int | None]]:
    if result.error_type:
        _ = _record(session, ctx, "no_event", None, text, "failed", result.model_dump_json(),
                    err=f"{'、'.join(result.missing_fields)}" if result.missing_fields else result.error_type,
                    failure_phase="extraction")
        session.commit()
        return [("⚠️ 系统处理失败，请稍后重试。", None)]

    if result.intent == Intent.no_event:
        _ = _record(session, ctx, "no_event", None, text, "failed", result.model_dump_json(), err="未识别到日程信息",
                    failure_phase="extraction")
        session.commit()
        return [("🤔 未识别到日程信息，请补充时间和事件内容。", None)]

    if result.missing_fields:
        _pending_drafts[f"draft_{ctx.source}:{ctx.source_user_id}:{ctx.conversation_id or ''}"] = {
            "ts": time.time(),
            "event": result.event.model_dump() if result.event else {},
            "missing": result.missing_fields,
        }
        _ = _record(session, ctx, "no_event", None, text, "failed", result.model_dump_json(),
                    err=f"缺少字段：{'、'.join(result.missing_fields)}",
                    failure_phase="validation")
        session.commit()
        return [(f"🤔 未识别到{'、'.join(result.missing_fields)}，请补充。", None)]

    if result.unsupported_reason:
        _ = _record(session, ctx, "no_event", None, text, "failed", result.model_dump_json(),
                    err=f"不支持：{result.unsupported_reason}",
                    failure_phase="validation")
        session.commit()
        return [(f"🔁 {result.unsupported_reason}", None)]

    events = result.events or ([result.event] if result.event else [])
    if not events:
        return [("🤔 未识别到日程信息，请补充时间和事件内容。", None)]

    if len(events) == 1:
        rec_id, caldav_ok = await _write_one(session, ctx, text, events[0], caldav)
        session.commit()
        if caldav_ok is False:
            line = _format_one(events[0], "⚠️ 日程已记录到本地，但日历同步失败")
        else:
            line = _format_one(events[0])
        return [(line, rec_id)]

    batch_id = _generate_batch_id(ctx, text)
    batch_items: list[dict[str, Any]] = []
    for idx, event in enumerate(events):
        item_uid = derive_batch_uid(batch_id, idx)
        rec_id, caldav_ok, err_msg = await _write_batch_one(
            session, ctx, text, event, caldav, batch_id=batch_id, batch_index=idx, uid=item_uid
        )
        batch_items.append({
            "record_id": rec_id,
            "index": idx,
            "event": event,
            "success": caldav_ok is not False,
            "error": err_msg,
            "uid": item_uid,
        })
    session.commit()

    report_text = _format_batch_report(batch_items, is_retry=False)
    primary_id = batch_items[0]["record_id"] if batch_items else None
    return [(report_text, primary_id)]


def _format_one(event: object, header: str = "✅ 日程已安排好啦！") -> str:
    return _format_event_result(event, header)


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


async def _write_one(session: Session, ctx: ChannelContext, text: str, event: CalendarEvent, caldav: dict[str, Any]) -> tuple[int, bool | None]:
    if not getattr(event, 'reminders', None):
        from app.ai.schemas import Reminder
        event.reminders = [Reminder(minutes_before=caldav["rem"])]
    if not getattr(event, 'end_time', None) and getattr(event, 'start_time', None):
        from dateutil.parser import parse as parse_date
        dt = parse_date(event.start_time)
        event.end_time = (dt + timedelta(minutes=caldav["dur"])).isoformat()

    event_uid = str(uuid.uuid4())
    caldav_result = None
    error_msg = None
    if caldav["url"] and caldav["user"]:
        try:
            caldav_result = await _write_caldav(event, caldav, uid=event_uid)
        except CalDAVServiceError as exc:
            error_msg = str(exc)

    caldav_enabled = bool(caldav["url"] and caldav["user"])
    caldav_ok: bool | None = caldav_result is not None if caldav_enabled else None
    status = "success" if (caldav_result or not caldav_enabled) else "failed"
    failure_phase = "write" if status == "failed" else None

    rec_id = _record(session, ctx, "create", event.title, text,
            status, event.model_dump_json(),
            caldav_result or ({"uid": event_uid} if caldav_enabled else None),
            error_msg,
            start_time=getattr(event, "start_time", ""),
            failure_phase=failure_phase,
            remote_etag=caldav_result.get("etag") if caldav_result else None)
    return rec_id, caldav_ok


def derive_batch_uid(batch_id: str, index: int) -> str:
    """
    基于 batch_id 与子项索引 index 派生严格确定性的 RFC 4122 UUID。
    杜绝底层重发或重试时生成新 UID 导致重复日程。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{batch_id}:{index}"))


def _generate_batch_id(ctx: ChannelContext, text: str) -> str:
    seed = ctx.source_message_id or uuid.uuid4().hex
    identity = json.dumps([ctx.source, ctx.conversation_id, ctx.source_user_id, seed], ensure_ascii=False)
    return f"batch_{uuid.uuid5(uuid.NAMESPACE_OID, identity).hex}"


async def _write_batch_one(
    session: Session,
    ctx: ChannelContext,
    text: str,
    event: CalendarEvent,
    caldav: dict[str, Any],
    batch_id: str,
    batch_index: int,
    uid: str,
) -> tuple[int, bool | None, str | None]:
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
            caldav_result = await _write_caldav(event, caldav, uid=uid)
        except CalDAVServiceError as exc:
            error_msg = str(exc)

    caldav_enabled = bool(caldav["url"] and caldav["user"])
    caldav_ok: bool | None = caldav_result is not None if caldav_enabled else None
    status = "success" if (caldav_result or not caldav_enabled) else "failed"
    failure_phase = "write" if status == "failed" else None

    rec_id = _record(
        session, ctx, "create", event.title, text,
        status, event.model_dump_json(),
        caldav_result or ({"uid": uid} if caldav_enabled else None),
        error_msg,
        start_time=getattr(event, "start_time", ""),
        failure_phase=failure_phase,
        batch_id=batch_id,
        batch_index=batch_index,
        remote_etag=caldav_result.get("etag") if caldav_result else None,
    )
    return rec_id, caldav_ok, error_msg


def _format_batch_report(items: list[dict[str, Any]], is_retry: bool = False, skipped_count: int = 0) -> str:
    total = len(items)
    success_items = [it for it in items if it.get("success")]
    failed_items = [it for it in items if not it.get("success")]
    success_cnt = len(success_items)
    failed_cnt = len(failed_items)

    lines: list[str] = []
    if is_retry:
        lines.append("📋 批量日程局部重试报告")
        if skipped_count > 0:
            lines.append(f"📊 总体汇总：共 {total} 项日程，跳过已成功项 {skipped_count} 项，当前共 {success_cnt} 项成功，{failed_cnt} 项失败")
        else:
            lines.append(f"📊 总体汇总：共 {total} 项日程，当前共 {success_cnt} 项成功，{failed_cnt} 项失败")
    else:
        if failed_cnt == 0:
            lines.append(f"📋 批量日程处理完成（共 {total} 项全部成功）")
            lines.append(f"📊 总体汇总：共识别 {total} 项日程，全部已成功写入日历")
        elif success_cnt == 0:
            lines.append("📋 批量日程处理报告（全部失败）")
            lines.append(f"📊 总体汇总：共识别 {total} 项日程，全部写入失败")
        else:
            lines.append("📋 批量日程处理报告")
            lines.append(f"📊 总体汇总：共识别 {total} 项日程，{success_cnt} 项成功，{failed_cnt} 项失败")

    lines.append("")
    for it in items:
        idx = it["index"] + 1
        ev = it["event"]
        ev_dict = ev if isinstance(ev, dict) else (ev.model_dump() if hasattr(ev, "model_dump") else {})
        title = _g(ev, "title", "日程")
        time_span = _format_time_span(ev_dict)
        loc = _g(ev, "location")
        loc_suffix = f" (📍 {loc})" if loc else ""

        if it.get("success"):
            status_tag = " [已跳过]" if (is_retry and it.get("skipped")) else ""
            lines.append(f"[{idx}] ✅ 📌 {title}{loc_suffix}{status_tag}")
            lines.append(f"    🕒 时间：{time_span}")
        else:
            err = it.get("error") or "写入日历失败"
            lines.append(f"[{idx}] ❌ 📌 {title}{loc_suffix}")
            lines.append(f"    🕒 时间：{time_span}")
            lines.append(f"    ⚠️ 失败原因：{err}")

    if failed_cnt > 0:
        lines.append("")
        lines.append("💡 提示：回复「重试失败项」可仅对失败项重新尝试写入，已成功项将自动跳过。")

    return "\n".join(lines)


def _is_retry_command(text: str) -> bool:
    clean = re.sub(r"[\s,，。.!！?？、]+", "", text).lower()
    if clean.startswith("请"):
        clean = clean[1:]
    if clean in {
        "重试",
        "重试失败项",
        "重试失败",
        "重试失败日程",
        "重试失败项日程",
        "重新执行失败项",
        "重新写入失败项",
        "重试写入",
        "retry",
        "retryfailed",
    }:
        return True
    return bool(re.match(r"^(?:请)?重试(?:失败(?:项|日程)?)?$", clean))


def _retry_not_superseded(session: Session, rec: EventRecord) -> bool:
    identity = (EventRecord.event_id == rec.event_id if rec.event_id else
                EventRecord.caldav_uid == rec.caldav_uid if rec.caldav_uid else
                EventRecord.id == rec.id)
    return session.execute(select(EventRecord.id).where(
        identity, EventRecord.id > rec.id,
        EventRecord.operation.in_(["create", "update", "delete"]),
        or_(EventRecord.status == "success", EventRecord.operation.in_(["create", "update"])),
    ).limit(1)).scalar_one_or_none() is None


async def _handle_batch_retry(
    session: Session,
    ctx: ChannelContext,
    caldav: dict[str, Any],
) -> list[tuple[str, int | None]]:
    scope = [EventRecord.source == ctx.source, EventRecord.conversation_id == ctx.conversation_id]
    if not ctx.conversation_id:
        scope.append(EventRecord.source_user_id == ctx.source_user_id)

    referenced = bool(ctx.reply_to_message_id or ctx.quoted_text or ctx.quote_reference_present)
    if referenced:
        latest_rec = None
        if ctx.reply_to_message_id:
            latest_rec = resolve_bot_message(session, ctx.source, ctx.conversation_id, ctx.reply_to_message_id)
            if not latest_rec:
                latest_rec = session.execute(select(EventRecord).where(
                    *scope, EventRecord.bot_message_id == ctx.reply_to_message_id,
                    EventRecord.operation.in_(["create", "update"]),
                ).order_by(EventRecord.id.desc()).limit(1)).scalar_one_or_none()
        elif ctx.quoted_text:
            latest_rec = await _find_target(session, ctx)
        if not latest_rec:
            return [("🤔 没有找到引用消息对应的日程，未执行重试。", None)]
    else:
        # Compare failed single events and batches together, not batches first.
        candidates = session.execute(select(EventRecord).where(
            *scope, EventRecord.operation.in_(["create", "update"]),
        ).order_by(EventRecord.id.desc())).scalars().all()
        latest_rec = next((r for r in candidates if r.status == "failed" and
                           _retry_not_superseded(session, r)), None)
        if latest_rec is None:
            latest_rec = next((r for r in candidates if r.batch_id), None)

    if not latest_rec:
        return [("🤔 未找到最近需要重试的失败日程记录。", None)]
    if not latest_rec.batch_id:
        single_rec = latest_rec
        if single_rec.status != "failed":
            return [("ℹ️ 该日程没有需要重试的失败写入。", single_rec.id)]
        if not _retry_not_superseded(session, single_rec):
            return [("ℹ️ 该日程已有后续操作，未重试旧记录。", single_rec.id)]
        if not single_rec or not single_rec.event_json:
            return [("🤔 未找到最近需要重试的失败日程记录。", None)]

        try:
            ev_data = json.loads(single_rec.event_json)
            ev = CalendarEvent(**ev_data)
        except Exception as e:
            return [(f"🤔 解析日程数据失败：{e}", single_rec.id)]

        target_uid = single_rec.caldav_uid or str(uuid.uuid4())
        caldav_result = None
        error_msg = None
        if caldav["url"] and caldav["user"]:
            try:
                caldav_result = await _write_caldav(ev, caldav, uid=target_uid)
            except CalDAVServiceError as exc:
                error_msg = str(exc)

        caldav_enabled = bool(caldav["url"] and caldav["user"])
        is_ok = bool(caldav_result or not caldav_enabled)
        single_rec.retry_count = (single_rec.retry_count or 0) + 1
        single_rec.updated_at = datetime.now(timezone.utc)
        if is_ok:
            single_rec.status = "success"
            single_rec.failure_phase = None
            single_rec.error_message = None
            single_rec.caldav_uid = target_uid
            if caldav_result:
                single_rec.caldav_href = caldav_result.get("href")
                single_rec.remote_etag = caldav_result.get("etag")
            try:
                svc = SettingsService(session)
                single_rec.config_version = svc.get_config_version()
                single_rec.caldav_config_hash = svc.get_caldav_config_hash()
            except Exception:
                pass
            session.commit()
            return [(f"✅ 日程「{single_rec.title or '日程'}」重试写入成功！", single_rec.id)]
        else:
            single_rec.error_message = f"重试失败：{error_msg}"
            single_rec.failure_phase = "write"
            session.commit()
            return [(f"❌ 日程「{single_rec.title or '日程'}」重试写入失败：{error_msg}", single_rec.id)]

    batch_id = latest_rec.batch_id
    records = session.execute(
        select(EventRecord)
        .where(EventRecord.batch_id == batch_id)
        .order_by(EventRecord.batch_index.asc(), EventRecord.id.asc())
    ).scalars().all()

    origins = {(r.source, r.conversation_id, r.source_user_id) for r in records}
    if len(origins) > 1:
        return [("⚠️ 历史批次包含多个会话，已停止批量重试，请先核对日程。", None)]

    latest_by_index: dict[int, EventRecord] = {}
    for r in records:
        idx = r.batch_index if r.batch_index is not None else 0
        latest_by_index[idx] = r

    items = sorted(latest_by_index.values(), key=lambda r: r.batch_index if r.batch_index is not None else 0)
    failed_items = [r for r in items if r.status == "failed"]
    if any(not _retry_not_superseded(session, r) for r in failed_items):
        return [("⚠️ 该批次部分日程已有后续操作，已停止重试旧批次，请核对日程。", latest_rec.id)]

    if not failed_items:
        return [(f"🎉 该批次日程（共 {len(items)} 项）已全部成功写入日历，无需重试。", latest_rec.id)]

    skipped_count = len(items) - len(failed_items)
    batch_report_items: list[dict[str, Any]] = []

    for r in items:
        idx = r.batch_index if r.batch_index is not None else 0
        ev_data: dict[str, Any] = {}
        if r.event_json:
            try:
                ev_data = json.loads(r.event_json)
            except Exception:
                ev_data = {}
        if not ev_data:
            ev_data = {"title": r.title or "日程", "start_time": r.start_time or ""}

        if r.status == "success":
            batch_report_items.append({
                "record_id": r.id,
                "index": idx,
                "event": ev_data,
                "success": True,
                "error": None,
                "uid": r.caldav_uid,
                "skipped": True,
            })
            continue

        target_uid = r.caldav_uid or derive_batch_uid(batch_id, idx)
        try:
            ev = CalendarEvent(**ev_data)
        except Exception:
            ev = CalendarEvent(title=r.title or "日程", start_time=r.start_time or "")

        caldav_result = None
        error_msg = None
        if caldav["url"] and caldav["user"]:
            try:
                caldav_result = await _write_caldav(ev, caldav, uid=target_uid)
            except CalDAVServiceError as exc:
                error_msg = str(exc)

        caldav_enabled = bool(caldav["url"] and caldav["user"])
        is_success = bool(caldav_result or not caldav_enabled)

        r.retry_count = (r.retry_count or 0) + 1
        r.updated_at = datetime.now(timezone.utc)
        if is_success:
            r.status = "success"
            r.failure_phase = None
            r.error_message = None
            r.caldav_uid = target_uid
            if caldav_result:
                r.caldav_href = caldav_result.get("href")
                r.remote_etag = caldav_result.get("etag")
            try:
                svc = SettingsService(session)
                r.config_version = svc.get_config_version()
                r.caldav_config_hash = svc.get_caldav_config_hash()
            except Exception:
                pass
        else:
            r.error_message = f"重试失败：{error_msg}"
            r.failure_phase = "write"

        batch_report_items.append({
            "record_id": r.id,
            "index": idx,
            "event": ev_data,
            "success": is_success,
            "error": error_msg,
            "uid": target_uid,
            "skipped": False,
        })

    session.commit()
    report = _format_batch_report(batch_report_items, is_retry=True, skipped_count=skipped_count)
    return [(report, latest_rec.id)]


async def _write_caldav_dict(event_dict: dict[str, Any], caldav: dict[str, Any], uid: str | None = None) -> dict[str, Any] | None:
    svc = CalDAVService()
    return await svc.create_event(
        caldav["url"], caldav["user"], caldav["pw"], caldav["cal"],
        event_dict["title"], cast(str, event_dict.get("start_time")), event_dict.get("end_time"),
        event_dict.get("timezone", "Asia/Shanghai"),
        event_dict.get("location"), event_dict.get("description"),
        event_dict.get("reminders"), event_dict.get("recurrence"),
        event_dict.get("is_all_day", False),
        ssl_verify=caldav["ssl"],
        uid=uid,
    )


async def _write_caldav(event: CalendarEvent, caldav: dict[str, Any], uid: str | None = None) -> dict[str, Any] | None:
    svc = CalDAVService()
    rec: dict[str, Any] = event.model_dump() if hasattr(event, 'model_dump') else {}
    return await svc.create_event(
        caldav["url"], caldav["user"], caldav["pw"], caldav["cal"],
        event.title, event.start_time, event.end_time, event.timezone,
        event.location, event.description,
        [{"minutes_before": r.minutes_before} for r in (event.reminders or [])],
        rec.get("recurrence"),
        event.is_all_day,
        ssl_verify=caldav["ssl"],
        uid=uid,
    )


def _record(session: Session, ctx: ChannelContext, op: str, title: str | None, text: str, status: str, js: str, cr: dict[str, Any] | None = None, err: str | None = None, start_time: str = "", event_id: str | None = None, failure_phase: str | None = None, retry_count: int = 0, batch_id: str | None = None, batch_index: int | None = None, snapshot_json: str | None = None, remote_etag: str | None = None) -> int:
    config_version = None
    ai_hash = None
    caldav_hash = None
    try:
        svc = SettingsService(session)
        config_version = svc.get_config_version()
        ai_hash = svc.get_ai_config_hash()
        caldav_hash = svc.get_caldav_config_hash()
    except Exception:
        pass

    rec = EventRecord(
        source=ctx.source, telegram_user_id=ctx.source_user_id, source_user_id=ctx.source_user_id,
        conversation_id=ctx.conversation_id, event_id=event_id or uuid.uuid4().hex, operation=op,
        title=_redact_sensitive_text(title) if title else title, start_time=start_time, status=status,
        source_message_id=ctx.source_message_id,
        original_text=_redact_sensitive_text(text or "")[:2000],
        event_json=_redact_sensitive_text(js or "")[:4000],
        caldav_uid=cr.get("uid") if cr else None,
        caldav_href=cr.get("href") if cr else None,
        remote_etag=remote_etag or (cr.get("etag") if cr else None),
        snapshot_json=_redact_sensitive_text(snapshot_json) if snapshot_json else None,
        error_message=_redact_sensitive_text(err) if err else err,
        config_version=config_version,
        ai_config_hash=ai_hash,
        caldav_config_hash=caldav_hash,
        failure_phase=failure_phase,
        retry_count=retry_count,
        batch_id=batch_id,
        batch_index=batch_index,
    )
    session.add(rec)
    session.flush()
    return rec.id


def _record_quote_failure(session: Session, ctx: ChannelContext, reason: str) -> None:
    _ = _record(
        session,
        ctx,
        "quote_not_found",
        None,
        "",
        "failed",
        "",
        err=reason,
        failure_phase="validation",
    )
    session.commit()


def _redact_sensitive_text(text: str) -> str:
    patterns = (
        r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}",
        r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{20,}",
    )
    for pattern in patterns:
        text = re.sub(pattern, "[REDACTED]", text)
    return text


def _parse_time(iso: str):
    try:
        from dateutil.parser import parse as parse_date
        dt = parse_date(iso)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None
