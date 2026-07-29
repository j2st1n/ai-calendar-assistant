"""Regression tests: reminders normalization in AI extraction.

P0 fix: LLM-returned reminders like 0, 30, [0], [15], null, or
[{"minutes_before": n}] must not trigger validation-error missing_fields.
"""

from app.ai.extractor import _normalize_reminders, _normalize_reminders_in_data


class TestNormalizeReminders:
    """Test _normalize_reminders on a single event dict."""

    def test_int_zero_preserved(self):
        """0 → [{"minutes_before": 0}], not dropped."""
        ev = {"title": "test", "reminders": 0}
        _normalize_reminders(ev)
        assert ev["reminders"] == [{"minutes_before": 0}]

    def test_int_thirty(self):
        """30 → [{"minutes_before": 30}]."""
        ev = {"title": "test", "reminders": 30}
        _normalize_reminders(ev)
        assert ev["reminders"] == [{"minutes_before": 30}]

    def test_list_of_ints(self):
        """[0] → [{"minutes_before": 0}], [15] → [{"minutes_before": 15}]."""
        ev = {"title": "test", "reminders": [15]}
        _normalize_reminders(ev)
        assert ev["reminders"] == [{"minutes_before": 15}]

        ev2 = {"title": "test", "reminders": [0]}
        _normalize_reminders(ev2)
        assert ev2["reminders"] == [{"minutes_before": 0}]

    def test_null_passthrough(self):
        """None → stays None (no reminders requested)."""
        ev = {"title": "test", "reminders": None}
        _normalize_reminders(ev)
        assert ev["reminders"] is None

    def test_valid_dict_list_passthrough(self):
        """Already-valid [{"minutes_before": n}] passes through."""
        ev = {"title": "test", "reminders": [{"minutes_before": 10}]}
        _normalize_reminders(ev)
        assert ev["reminders"] == [{"minutes_before": 10}]

    def test_single_dict_wrapped_in_list(self):
        """{"minutes_before": n} → [{"minutes_before": n}]."""
        ev = {"title": "test", "reminders": {"minutes_before": 10}}
        _normalize_reminders(ev)
        assert ev["reminders"] == [{"minutes_before": 10}]

    def test_empty_list_passthrough(self):
        """[] stays [] (cancel reminders)."""
        ev = {"title": "test", "reminders": []}
        _normalize_reminders(ev)
        assert ev["reminders"] == []

    def test_mixed_list(self):
        """Mixed [5, {"minutes_before": 10}] → all canonical."""
        ev = {"title": "test", "reminders": [5, {"minutes_before": 10}]}
        _normalize_reminders(ev)
        assert ev["reminders"] == [{"minutes_before": 5}, {"minutes_before": 10}]

    def test_no_reminders_key_unchanged(self):
        """Dict without 'reminders' key is not modified."""
        ev = {"title": "test"}
        _normalize_reminders(ev)
        assert "reminders" not in ev


class TestNormalizeRemindersInData:
    """Test _normalize_reminders_in_data on the top-level LLM data dict."""

    def test_normalizes_events_list(self):
        """Reminders in 'events' list are normalized."""
        data = {
            "intent": "create_event",
            "events": [
                {"title": "a", "reminders": 0},
                {"title": "b", "reminders": [15]},
                {"title": "c", "reminders": [{"minutes_before": 30}]},
            ],
        }
        _normalize_reminders_in_data(data)
        assert data["events"][0]["reminders"] == [{"minutes_before": 0}]
        assert data["events"][1]["reminders"] == [{"minutes_before": 15}]
        assert data["events"][2]["reminders"] == [{"minutes_before": 30}]

    def test_normalizes_event_key(self):
        """Reminders in 'event' dict (update_event) are normalized."""
        data = {
            "intent": "update_event",
            "event": {"reminders": 30},
        }
        _normalize_reminders_in_data(data)
        assert data["event"]["reminders"] == [{"minutes_before": 30}]

    def test_both_event_and_events(self):
        """Both keys are normalized when present."""
        data = {
            "intent": "create_event",
            "event": {"reminders": 0},
            "events": [{"reminders": 15}],
        }
        _normalize_reminders_in_data(data)
        assert data["event"]["reminders"] == [{"minutes_before": 0}]
        assert data["events"][0]["reminders"] == [{"minutes_before": 15}]

    def test_no_reminders_no_change(self):
        """Data without reminders passes through unchanged."""
        data = {"intent": "no_event", "events": [{"title": "test"}]}
        result = _normalize_reminders_in_data(data)
        assert result is data  # same object returned
