import json
import logging
import re
from datetime import datetime, timezone

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
- Missing end_time → default 1 hour later.
- All-day event → set is_all_day=true, only dates.
- Reminders → minutes_before; default 30.
- Recurrence → frequency=daily|weekly|monthly, interval, days_of_week=[MO,TU,...], day_of_month, until, count.
  Supported: daily, weekly days, weekdays (all 5), monthly-by-day. Unsupported → set unsupported_reason.
- Past/started event → set intent=create_event normally; the caller will handle confirmation.
- location, description as provided.
- If title or start_time missing → set missing_fields accordingly.

Return JSON:
{
  "intent": "create_event|update_event|delete_event|provide_missing_fields|no_event",
  "event": {
    "title": "...",
    "start_time": "2026-05-15T15:00:00+08:00",
    "end_time": "2026-05-15T16:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "location": "...",
    "description": "...",
    "reminders": [{"minutes_before": 30}],
    "recurrence": null,
    "is_all_day": false
  },
  "missing_fields": [],
  "unsupported_reason": null,
  "confidence": 0.9
}
"""

MODIFY_PROMPT = """You are a calendar event modifier. Update the existing event based on the user's instruction.

Current time: {current_time}
Existing event: {existing_event}
User instruction: {instruction}

Return the COMPLETE updated event in the same JSON format as creation. Do NOT return a diff - return the entire updated event with intent=update_event.
"""

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

    async def modify(self, existing_event: dict, instruction: str) -> ExtractionResult:
        prompt = MODIFY_PROMPT.format(
            current_time=datetime.now(timezone.utc).isoformat(),
            existing_event=json.dumps(existing_event, ensure_ascii=False),
            instruction=instruction,
        )
        return await self._call(prompt, instruction)

    async def merge_draft(self, draft: dict, new_input: str) -> ExtractionResult:
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
            return ExtractionResult.model_validate(data)
        except AIProviderError as exc:
            logger.exception("AI provider error in extraction")
            return ExtractionResult(intent=Intent.no_event, missing_fields=[str(exc)], confidence=0.0)
        except Exception as exc:
            logger.exception("Failed to parse AI extraction result")
            return ExtractionResult(intent=Intent.no_event, missing_fields=[str(exc)], confidence=0.0)


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    return json.loads(text)
