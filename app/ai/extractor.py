import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.services.ai_provider_service import AIProviderConfig, AIProviderError, AIProviderService

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """You are a calendar event extraction assistant. Extract event information from the user's message.

Current time: {current_time}
Default timezone: {default_timezone}

Rules:
- If no event info found, return intent=no_event.
- Use ISO 8601 format: "2026-05-15T14:30:00+08:00".
- Missing year → use nearest future date.
- Missing time → default: morning=09:00, noon=12:00, afternoon=14:00, evening=19:00, else 09:00.
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
        self._config = config
        self._timezone = timezone
        self._service = AIProviderService()

    async def extract(self, text: str) -> ExtractionResult:
        prompt = EXTRACT_PROMPT.format(
            current_time=datetime.now(timezone.utc).isoformat(),
            default_timezone=self._timezone,
        )
        return await self._call(prompt, text)

    async def modify(self, existing_event: dict[str, Any], instruction: str) -> ExtractionResult:
        prompt = MODIFY_PROMPT.format(
            existing_event=json.dumps(existing_event, ensure_ascii=False),
            instruction=instruction,
        )
        return await self._call(prompt, instruction)

    async def merge_draft(self, draft: dict[str, Any], new_input: str) -> ExtractionResult:
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
                return ExtractionResult(intent=Intent.no_event, missing_fields=["empty_response"], confidence=0.0)
            data = _parse_json(raw)
            return _build_result(data)
        except Exception as exc:
            return ExtractionResult(intent=Intent.no_event, missing_fields=[str(exc)], confidence=0.0)


def _build_result(data: dict[str, Any]) -> ExtractionResult:
    intent_str = data.get("intent", "no_event")
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

        return ExtractionResult(intent=intent, events=events, missing_fields=[str(exc)])


def _build_event(data: dict[str, Any]):
    from app.ai.schemas import CalendarEvent
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
