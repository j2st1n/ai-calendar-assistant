import asyncio
from typing import cast

from app.ai.extractor import EventExtractor, _build_result
from app.ai.schemas import Intent
from app.services.ai_provider_service import AIProviderConfig, AIProviderService


def _event_payload(reminders):
    return {
        "intent": "create_event",
        "events": [{
            "title": "买药",
            "start_time": "2026-06-01T12:00:00+08:00",
            "end_time": None,
            "timezone": "Asia/Shanghai",
            "location": None,
            "description": None,
            "reminders": reminders,
            "recurrence": None,
            "is_all_day": False,
        }],
        "missing_fields": [],
        "confidence": 0.9,
    }


def test_build_result_normalizes_bare_int_reminder():
    result = _build_result(_event_payload(30))

    assert result.error_type is None
    assert result.missing_fields == []
    assert result.events[0].reminders is not None
    assert result.events[0].reminders[0].minutes_before == 30


def test_build_result_normalizes_zero_reminder_without_dropping():
    result = _build_result(_event_payload(0))

    assert result.error_type is None
    assert result.missing_fields == []
    assert result.events[0].reminders is not None
    assert result.events[0].reminders[0].minutes_before == 0


def test_build_result_normalizes_list_int_reminder():
    result = _build_result(_event_payload([15]))

    assert result.error_type is None
    assert result.missing_fields == []
    assert result.events[0].reminders is not None
    assert result.events[0].reminders[0].minutes_before == 15


def test_build_result_keeps_null_reminder():
    result = _build_result(_event_payload(None))

    assert result.error_type is None
    assert result.missing_fields == []
    assert result.events[0].reminders is None


def test_build_result_normalizes_update_event_reminder():
    result = _build_result({"intent": "update_event", "event": {"reminders": [0]}})

    assert result.error_type is None
    assert result.event is not None
    assert result.event.reminders is not None
    assert result.event.reminders[0].minutes_before == 0


class _FakeService:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc

    async def chat_completion(self, *_args, **_kwargs):
        if self.exc:
            raise self.exc
        return self.response


def test_call_marks_parse_error():
    async def run():
        extractor = EventExtractor(AIProviderConfig(provider_type="openai_compatible", base_url="", api_key="", model=""))
        extractor._service = cast(AIProviderService, _FakeService(response="not json"))

        result = await extractor._call("system", "user")

        assert result.intent == Intent.no_event
        assert result.error_type == "parse_error"
        assert result.missing_fields

    asyncio.run(run())


def test_call_marks_system_error():
    async def run():
        extractor = EventExtractor(AIProviderConfig(provider_type="openai_compatible", base_url="", api_key="", model=""))
        extractor._service = cast(AIProviderService, _FakeService(exc=RuntimeError("boom")))

        result = await extractor._call("system", "user")

        assert result.intent == Intent.no_event
        assert result.error_type == "system_error"
        assert result.missing_fields == ["boom"]

    asyncio.run(run())
