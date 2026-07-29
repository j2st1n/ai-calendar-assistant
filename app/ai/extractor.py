import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from zoneinfo import ZoneInfo

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.services.ai_provider_service import AIProviderConfig, AIProviderService

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """You are a calendar event extraction assistant. Extract event information from the user's message.

Current time: {current_time}
Default timezone: {default_timezone}

Rules:
- If no event info found, return intent=no_event.
- Use ISO 8601 format: "2026-05-15T14:30:00+08:00".
- Missing year → use nearest future date.
- Missing time → default: morning=09:00, noon=12:00, afternoon=14:00, evening=19:00, else 09:00.
- Terse time/date + action inputs are events. If a clear action follows the time/date, use that action as title; do not require a separate title.
  Examples: "下午3点开会" → title "开会"; "明天吃饭" → title "吃饭"; "周五晚上健身" → title "健身".
- Missing end_time → set to null. Do not invent end_time.
- All-day event → set is_all_day=true, only dates.
- Reminders → minutes_before. Only include if user explicitly mentions reminders/alarms.
- Recurrence → frequency=daily|weekly|monthly, interval, days_of_week=[MO,TU,...], day_of_month, until, count.
  Supported: daily, weekly days, weekdays (all 5), monthly-by-day. Unsupported → set unsupported_reason.
- Past/started event → set intent=create_event normally; the caller will handle confirmation.
- location, description as provided.
- If title or start_time missing → set missing_fields accordingly.

Return JSON:
{{
  "intent": "create_event|update_event|delete_event|provide_missing_fields|no_event",
  "events": [{{
    "title": "...",
    "start_time": "2026-05-15T15:00:00+08:00",
    "end_time": null,
    "timezone": "Asia/Shanghai",
    "location": "...",
    "description": "...",
    "reminders": null,
    "recurrence": null,
    "is_all_day": false
  }}],
  "missing_fields": [],
  "unsupported_reason": null,
  "confidence": 0.9
}}
"""

MODIFY_PROMPT = """You are an event editor. Understand the user's modification intent, edit the appropriate event fields, and return changed fields only.

Existing event: {existing_event}
User request: {instruction}

CRITICAL RULES:
1. Return intent=update_event with changed fields only. Omit unchanged fields; do not return unchanged defaults.
2. To DELETE the event, return intent=delete_event.
3. A request may contain multiple changes. Apply EVERY requested change in one event object; do not stop after the first change.
4. Split requests by punctuation/conjunctions like "，", ",", "并且", "同时", "然后"; each clause may contain a separate field change.
5. A change can be direct replacement, relative date/time change, reminder change, clearing/removing a field, or transforming an existing field.
6. For transformation requests like simplify/shorten/summarize/polish/rewrite/clarify/expand/remove/clean up (精简/简化/缩短/总结/概括/润色/重写/改写/说清楚/扩充/删除/去掉/清理), infer the target field and generate the new field value from existing_event. Do not omit the transformed field just because the user did not provide explicit replacement text.
7. If the user does not name a field, text transformation requests default to description when existing_event.description exists. Title/name requests target title; place/where requests target location; reminder/alert/提前/取消提醒 requests target reminders; date/time/day/hour requests target start_time/end_time.
8. Only transform fields explicitly requested or clearly implied. Do not rewrite title/location/description/date/time/reminders unless requested.
9. If user says "改到10点" and existing start_time is "21:00" (9 PM), the new time is 22:00 (10 PM). Use the existing event's AM/PM context to resolve ambiguity.
10. When changing only the date, preserve the existing time-of-day and duration. When changing only the start time, preserve the existing date and duration unless the user also changes date/duration.
11. For reminder changes like "提前20分钟提醒", return {{"reminders":[{{"minutes_before":20}}]}}. Always extract the exact number. For cancel/remove reminder requests, return {{"reminders":[]}}.
12. For clear/remove field requests like "清空描述", "删除备注", "去掉地点", return an empty string for the target field, e.g. {{"description":""}} or {{"location":""}}.

Return JSON examples:
- Time: {{"intent":"update_event","event":{{"start_time":"2026-05-14T22:00:00+08:00"}}}}
- Title: {{"intent":"update_event","event":{{"title":"新标题"}}}}
- Location: {{"intent":"update_event","event":{{"location":"会议室B"}}}}
- Description: {{"intent":"update_event","event":{{"description":"带资料"}}}}
- Date + reminder: {{"intent":"update_event","event":{{"start_time":"2026-05-19T09:30:00+08:00","end_time":"2026-05-19T10:30:00+08:00","reminders":[{{"minutes_before":15}}]}}}}
- Time + location: {{"intent":"update_event","event":{{"start_time":"2026-05-14T15:00:00+08:00","end_time":"2026-05-14T16:00:00+08:00","location":"会议室B"}}}}
- Reminder + location: {{"intent":"update_event","event":{{"reminders":[{{"minutes_before":10}}],"location":"线上"}}}}
- Date + time + reminder: {{"intent":"update_event","event":{{"start_time":"2026-05-19T15:00:00+08:00","end_time":"2026-05-19T16:00:00+08:00","reminders":[{{"minutes_before":15}}]}}}}
- Simplify description: {{"intent":"update_event","event":{{"description":"学习廉洁从业规定1-5章，马总讲话"}}}}
- Reminder + simplified description: {{"intent":"update_event","event":{{"reminders":[{{"minutes_before":15}}],"description":"学习廉洁从业规定1-5章，马总讲话"}}}}
- Shorten title + location: {{"intent":"update_event","event":{{"title":"周会","location":"线上"}}}}
- Clear description: {{"intent":"update_event","event":{{"description":""}}}}
- Remove location and cancel reminders: {{"intent":"update_event","event":{{"location":"","reminders":[]}}}}"""

MISSING_FIELDS_PROMPT = """You are merging a partial event draft with new user input.

Current time: {current_time}
Partial draft: {draft}
New user input: {new_input}

Return a complete event with intent=create_event if all required fields are now present, or provide_missing_fields if key fields still missing.
"""


class EventExtractor:
    def __init__(self, config: AIProviderConfig, timezone: str = "Asia/Shanghai"):
        self._config: AIProviderConfig = config
        self._timezone: str = timezone
        self._service: AIProviderService = AIProviderService()

    async def extract(self, text: str) -> ExtractionResult:
        prompt = EXTRACT_PROMPT.format(
            current_time=datetime.now(timezone.utc).isoformat(),
            default_timezone=self._timezone,
        )
        result = await self._call(prompt, text)
        if result.intent == Intent.create_event:
            result = _ensure_hour_only_is_future(result, text, self._timezone)
        return result

    async def modify(self, existing_event: dict[str, object], instruction: str) -> ExtractionResult:
        prompt = MODIFY_PROMPT.format(
            existing_event=json.dumps(existing_event, ensure_ascii=False),
            instruction=instruction,
        )
        return await self._call(prompt, instruction)

    async def merge_draft(self, draft: dict[str, object], new_input: str) -> ExtractionResult:
        prompt = MISSING_FIELDS_PROMPT.format(
            current_time=datetime.now(timezone.utc).isoformat(),
            draft=json.dumps(draft, ensure_ascii=False),
            new_input=new_input,
        )
        return await self._call(prompt, new_input)

    async def _call(self, system_prompt: str, user_message: str) -> ExtractionResult:
        try:
            raw = await self._service.chat_completion(self._config, system_prompt, user_message)
            if not raw:
                return ExtractionResult(intent=Intent.no_event, missing_fields=["empty_response"],
                                        error_type="empty_response", confidence=0.0)
            try:
                data = _parse_json(raw)
            except ValueError as exc:
                return ExtractionResult(intent=Intent.no_event, missing_fields=[str(exc)],
                                        error_type="parse_error", confidence=0.0)
            return _build_result(data)
        except Exception as exc:
            return ExtractionResult(intent=Intent.no_event, missing_fields=[str(exc)],
                                    error_type="system_error", confidence=0.0)


def _build_result(data: dict[str, Any]) -> ExtractionResult:
    data = _normalize_reminders_in_data(data)
    intent_str: object = data.get("intent", "no_event")
    try:
        intent = Intent(intent_str)
    except ValueError:
        intent = Intent.no_event

    try:
        if intent == Intent.update_event and isinstance(data.get("event"), dict):
            from app.ai.schemas import CalendarEvent, Reminder, Recurrence
            ev_data = data["event"]
            kwargs: dict[str, Any] = {}
            for k in ("title", "start_time", "end_time", "timezone", "location", "description", "is_all_day"):
                if k in ev_data:
                    kwargs[k] = ev_data[k]
            if "reminders" in ev_data and isinstance(ev_data["reminders"], list):
                kwargs["reminders"] = [Reminder(**r) for r in ev_data["reminders"]]
            if "recurrence" in ev_data and isinstance(ev_data["recurrence"], dict):
                kwargs["recurrence"] = Recurrence(**ev_data["recurrence"])
            ev = CalendarEvent.model_construct(_fields_set=set(kwargs.keys()), **kwargs)
            return ExtractionResult(intent=intent, event=ev)
        return ExtractionResult.model_validate(data)
    except Exception as exc:
        events = []
        events_data = data.get("events", [])
        if not isinstance(events_data, list):
            events_data = []
        if not events_data:
            ev = data.get("event")
            if isinstance(ev, dict):
                events_data = [ev]

        for ed in events_data:
            if isinstance(ed, dict):
                ev = _build_event(ed)
                if ev:
                    events.append(ev)

        return ExtractionResult(intent=intent, events=events,
                                missing_fields=[str(exc)], error_type="schema_error")


def _build_event(data: dict[str, Any]) -> CalendarEvent | None:
    from app.ai.schemas import CalendarEvent
    _normalize_reminders(data)
    try:
        return CalendarEvent.model_validate(data)
    except Exception:
        filled = dict(data)
        if "title" not in filled:
            filled["title"] = ""
        if "start_time" not in filled:
            filled["start_time"] = ""
        try:
            return CalendarEvent.model_validate(filled)
        except Exception:
            return None


def _normalize_reminders(ev_data: dict[str, Any]) -> None:
    """Normalize reminders field in a single event dict before Pydantic validation.

    Accepts: 0, 30, [0], [15], null, [{"minutes_before": n}] and converts to
    the canonical list[dict] form. Does NOT drop a minutes_before=0 value when
    the caller/model explicitly provides one.
    """
    if "reminders" not in ev_data:
        return
    reminders = ev_data["reminders"]
    if reminders is None:
        return
    if isinstance(reminders, dict):
        ev_data["reminders"] = [reminders]
    elif isinstance(reminders, int):
        ev_data["reminders"] = [{"minutes_before": reminders}]
    elif isinstance(reminders, list):
        normalized: list[dict[str, object]] = []
        for r in reminders:
            if isinstance(r, int):
                normalized.append({"minutes_before": r})
            elif isinstance(r, dict):
                normalized.append(r)
        ev_data["reminders"] = normalized


def _is_ambiguous_hour_only(text: str) -> bool:
    """Check if text contains a bare hour-only reference without explicit date/period words.

    Returns True for inputs like "10点测试" or "3:00pm" (without 上午/下午/今天/明天 etc.)
    so the caller can detect when the AI might have interpreted a past time as today.
    """
    if not re.search(r"\d+\u70b9", text) and not re.search(r"(?<!\d)\d{1,2}:\d{2}(?!\d)", text):
        return False

    # Explicit date words — these scope the hour to a specific day
    _DATE_WORDS = frozenset({
        "今天", "明天", "昨天", "后天", "前天",
        "今晚", "昨晚", "明晚",
        "这周", "下周", "上周",
        "这个月", "下个月", "上个月", "本月", "下月", "上月",
    })
    for w in _DATE_WORDS:
        if w in text:
            return False

    # Period words — these scope the hour to a specific part of today
    _PERIOD_WORDS = frozenset({
        "上午", "早上", "凌晨", "中午", "下午", "晚上",
        "今早", "明早", "今晨", "明晨", "今晚上", "明晚上",
    })
    for w in _PERIOD_WORDS:
        if w in text:
            return False

    if re.search(r"周[一二三四五六日天末]|星期[一二三四五六日天]|礼拜[一二三四五六日天]", text):
        return False
    if re.search(r"\d+\u6708\d+\u65e5", text):
        return False

    return True


def _bare_hour_minute(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{1,2})\u70b9(?:半|(\d{1,2})分?)?", text)
    if m:
        hour = int(m.group(1))
        if not 0 <= hour <= 23:
            return None
        minute = 30 if "半" in m.group(0) else int(m.group(2) or 0)
        if not 0 <= minute <= 59:
            return None
        return hour, minute

    m = re.search(r"(?<!\d)(\d{1,2})[:：](\d{2})(?!\d)", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return None


def _ensure_hour_only_is_future(
    result: ExtractionResult, text: str, timezone_str: str, now: datetime | None = None,
) -> ExtractionResult:
    """Post-processing: roll past start_times forward for ambiguous hour-only inputs.

    When the user says "10点测试" (bare hour, no date/period qualifier) and the
    model returns a past morning time, first try the same-day PM interpretation
    (10:00 -> 22:00). If that is also past, advance by whole days until future.
    Preserves original duration when end_time is set.
    """
    if result.intent != Intent.create_event or not result.events:
        return result
    if not _is_ambiguous_hour_only(text):
        return result

    try:
        now_local = now or datetime.now(timezone.utc)
        if now_local.tzinfo is None:
            now_local = now_local.replace(tzinfo=timezone.utc)
        now_local = now_local.astimezone(ZoneInfo(timezone_str))
    except Exception:
        return result
    bare_time = _bare_hour_minute(text)

    rolled: list[CalendarEvent] = []
    for event in result.events:
        if not event.start_time:
            rolled.append(event)
            continue

        try:
            st = datetime.fromisoformat(event.start_time)
        except (ValueError, TypeError):
            rolled.append(event)
            continue

        # Make st offset-aware for comparison if it's naive
        if st.tzinfo is None:
            st = st.replace(tzinfo=ZoneInfo(timezone_str))

        if st > now_local and st.date() == now_local.date():
            rolled.append(event)
            continue

        if bare_time and 1 <= bare_time[0] <= 11:
            same_day_pm = now_local.replace(
                hour=bare_time[0] + 12,
                minute=bare_time[1],
                second=0,
                microsecond=0,
            )
            if same_day_pm > now_local and st != same_day_pm:
                duration = _event_duration(event, st, timezone_str)
                event.start_time = same_day_pm.isoformat()
                if duration is not None and event.end_time:
                    event.end_time = (same_day_pm + duration).isoformat()
                rolled.append(event)
                continue

        duration = _event_duration(event, st, timezone_str)

        if 1 <= st.hour <= 11:
            pm_candidate = st + timedelta(hours=12)
            if pm_candidate > now_local:
                event.start_time = pm_candidate.isoformat()
                if duration is not None and event.end_time:
                    event.end_time = (pm_candidate + duration).isoformat()
                rolled.append(event)
                continue

        days = 1
        while True:
            candidate = st + timedelta(days=days)
            if candidate > now_local:
                break
            days += 1

        event.start_time = candidate.isoformat()
        if duration is not None and event.end_time:
            event.end_time = (candidate + duration).isoformat()

        rolled.append(event)

    result.events = rolled
    return result


def _event_duration(event: CalendarEvent, start_time: datetime, timezone_str: str) -> timedelta | None:
    if not event.end_time:
        return None
    try:
        et = datetime.fromisoformat(event.end_time)
        if et.tzinfo is None:
            et = et.replace(tzinfo=ZoneInfo(timezone_str))
        return et - start_time
    except (ValueError, TypeError):
        return None


def _normalize_reminders_in_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize reminders in the top-level data dict returned by the LLM.

    Handles both the "event" key (update_event) and the "events" list
    (create_event / provide_missing_fields).
    """
    if "event" in data and isinstance(data["event"], dict):
        _normalize_reminders(data["event"])
    events = data.get("events")
    if isinstance(events, list):
        for ev in events:
            if isinstance(ev, dict):
                _normalize_reminders(ev)
    return data


def _parse_json(raw: str) -> dict[str, Any]:
    text = raw.strip()

    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()

    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{[^{}]*\{[^{}]*\}[^{}]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    if text.startswith('"') and not text.startswith('{'):
        text = "{" + text

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse failed. Raw (first 300 chars): {raw[:300]}") from exc
