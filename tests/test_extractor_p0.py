import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import cast

from zoneinfo import ZoneInfo

from app.ai.extractor import EventExtractor, _build_result, _ensure_hour_only_is_future, _is_ambiguous_hour_only
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


def test_extract_prompt_documents_terse_time_action_inputs():
    from app.ai.extractor import EXTRACT_PROMPT

    assert "Terse time/date + action inputs are events" in EXTRACT_PROMPT
    assert "下午3点开会" in EXTRACT_PROMPT
    assert "明天吃饭" in EXTRACT_PROMPT
    assert "周五晚上健身" in EXTRACT_PROMPT


def test_is_ambiguous_hour_only_matches_bare_hour():
    assert _is_ambiguous_hour_only("10点测试") is True
    assert _is_ambiguous_hour_only("3点开会") is True
    assert _is_ambiguous_hour_only("14:30开会") is True
    assert _is_ambiguous_hour_only("9:00出发") is True


def test_is_ambiguous_hour_only_rejects_explicit_today():
    assert _is_ambiguous_hour_only("今天10点测试") is False


def test_is_ambiguous_hour_only_rejects_explicit_tomorrow():
    assert _is_ambiguous_hour_only("明天10点测试") is False


def test_is_ambiguous_hour_only_rejects_period_words():
    assert _is_ambiguous_hour_only("上午10点测试") is False
    assert _is_ambiguous_hour_only("下午3点开会") is False
    assert _is_ambiguous_hour_only("晚上8点看电影") is False
    assert _is_ambiguous_hour_only("早上6点跑步") is False
    assert _is_ambiguous_hour_only("凌晨5点出发") is False
    assert _is_ambiguous_hour_only("中午12点吃饭") is False


def test_is_ambiguous_hour_only_rejects_day_of_week():
    assert _is_ambiguous_hour_only("周一10点开会") is False
    assert _is_ambiguous_hour_only("星期三十点") is False
    assert _is_ambiguous_hour_only("礼拜五10点") is False


def test_is_ambiguous_hour_only_rejects_specific_date():
    assert _is_ambiguous_hour_only("6月1日10点") is False


def test_is_ambiguous_hour_only_rejects_null():
    assert _is_ambiguous_hour_only("没有时间信息") is False


def _past_payload(hours_ago: int = 2, end_delta_hours: int | None = None) -> dict:
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    st = now - timedelta(hours=hours_ago)
    ev: dict = {
        "title": "测试",
        "start_time": st.isoformat(),
        "end_time": None,
        "timezone": "Asia/Shanghai",
        "location": None,
        "description": None,
        "reminders": None,
        "recurrence": None,
        "is_all_day": False,
    }
    if end_delta_hours is not None:
        ev["end_time"] = (st + timedelta(hours=end_delta_hours)).isoformat()
    return {
        "intent": "create_event",
        "events": [ev],
        "missing_fields": [],
        "confidence": 0.9,
    }


def _future_payload(hours_ahead: int = 1) -> dict:
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    st = now + timedelta(hours=hours_ahead)
    return {
        "intent": "create_event",
        "events": [{
            "title": "测试",
            "start_time": st.isoformat(),
            "end_time": None,
            "timezone": "Asia/Shanghai",
            "location": None,
            "description": None,
            "reminders": None,
            "recurrence": None,
            "is_all_day": False,
        }],
        "missing_fields": [],
        "confidence": 0.9,
    }


def test_ensure_hour_only_does_not_modify_future_time():
    payload = _future_payload(1)
    result = _build_result(payload)
    original = result.events[0].start_time

    result = _ensure_hour_only_is_future(result, "10点测试", "Asia/Shanghai")

    assert result.events[0].start_time == original


def test_ensure_hour_only_rolls_past_time_forward():
    payload = _past_payload(2)
    result = _build_result(payload)
    original_st = datetime.fromisoformat(result.events[0].start_time)

    result = _ensure_hour_only_is_future(result, "10点测试", "Asia/Shanghai")
    new_st = datetime.fromisoformat(result.events[0].start_time)

    assert (new_st - original_st).days == 1
    assert new_st.hour == original_st.hour
    assert new_st.minute == original_st.minute


def test_ensure_hour_only_preserves_duration():
    payload = _past_payload(2, end_delta_hours=1)
    result = _build_result(payload)
    assert result.events[0].end_time is not None
    orig_st = datetime.fromisoformat(result.events[0].start_time)
    orig_et = datetime.fromisoformat(result.events[0].end_time)
    orig_duration = orig_et - orig_st

    result = _ensure_hour_only_is_future(result, "10点测试", "Asia/Shanghai")
    assert result.events[0].end_time is not None
    new_st = datetime.fromisoformat(result.events[0].start_time)
    new_et = datetime.fromisoformat(result.events[0].end_time)
    new_duration = new_et - new_st

    assert orig_duration == new_duration


def test_ensure_hour_only_no_op_for_explicit_today():
    payload = _past_payload(2)
    result = _build_result(payload)
    original = result.events[0].start_time

    result = _ensure_hour_only_is_future(result, "今天10点测试", "Asia/Shanghai")

    assert result.events[0].start_time == original


def test_ensure_hour_only_no_op_for_period_words():
    payload = _past_payload(2)
    result = _build_result(payload)
    original = result.events[0].start_time

    result = _ensure_hour_only_is_future(result, "上午10点测试", "Asia/Shanghai")

    assert result.events[0].start_time == original


def test_ensure_hour_only_no_op_for_non_create():
    payload = _past_payload(2)
    payload["intent"] = "update_event"
    result = _build_result(payload)
    original = result.events[0].start_time

    result = _ensure_hour_only_is_future(result, "10点测试", "Asia/Shanghai")

    assert result.events[0].start_time == original


def test_extract_with_ambiguous_hour_only_rolls_forward():
    async def run():
        now_sh = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        past_st = now_sh - timedelta(hours=2)
        response = json.dumps({
            "intent": "create_event",
            "events": [{
                "title": "测试",
                "start_time": past_st.isoformat(),
                "end_time": None,
                "timezone": "Asia/Shanghai",
                "location": None,
                "description": None,
                "reminders": None,
                "recurrence": None,
                "is_all_day": False,
            }],
            "missing_fields": [],
            "confidence": 0.9,
        })

        extractor = EventExtractor(
            AIProviderConfig(provider_type="openai_compatible", base_url="", api_key="", model=""),
            timezone="Asia/Shanghai",
        )
        extractor._service = cast(AIProviderService, _FakeService(response=response))

        result = await extractor.extract("10点测试")

        assert result.intent == Intent.create_event
        assert len(result.events) == 1
        new_st = datetime.fromisoformat(result.events[0].start_time)
        assert new_st > now_sh, f"start_time {new_st} should be after now {now_sh}"

    asyncio.run(run())


def test_extract_with_explicit_today_does_not_roll():
    async def run():
        now_sh = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        past_st = now_sh - timedelta(hours=2)
        response = json.dumps({
            "intent": "create_event",
            "events": [{
                "title": "测试",
                "start_time": past_st.isoformat(),
                "end_time": None,
                "timezone": "Asia/Shanghai",
                "location": None,
                "description": None,
                "reminders": None,
                "recurrence": None,
                "is_all_day": False,
            }],
            "missing_fields": [],
            "confidence": 0.9,
        })

        extractor = EventExtractor(
            AIProviderConfig(provider_type="openai_compatible", base_url="", api_key="", model=""),
            timezone="Asia/Shanghai",
        )
        extractor._service = cast(AIProviderService, _FakeService(response=response))

        result = await extractor.extract("今天10点测试")

        assert result.intent == Intent.create_event
        st = datetime.fromisoformat(result.events[0].start_time)
        assert st == past_st

    asyncio.run(run())
