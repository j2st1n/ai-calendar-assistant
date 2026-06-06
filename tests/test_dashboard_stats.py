"""
Tests for dashboard_stats date counting logic.

Current signature:  dashboard_stats(session, settings_service) -> dict[str, int]
Uses timezone-aware today (via caldav_timezone setting) and configurable
week_start_day (0=Sunday, 1=Monday).
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db.models import Base, EventRecord
from app.services.settings_service import SettingsService
import app.web.routes as routes
from app.web.routes import _event_key, dashboard_stats, update_data_settings


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _session():
    """In-memory SQLite session with all tables created."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/console/system/data", "headers": [], "session": {}})


def _seed(session, *, title="Test", start_time=None,  # type: ignore[assignment]
          operation="create", status="success", event_id=None,  # type: ignore[assignment]
          caldav_uid=None,  # type: ignore[assignment]
          created_at=None):
    """Add + commit one EventRecord. Returns the record.
    Defaults are chosen per-call; use keyword args to override."""
    rec = EventRecord(
        source="test",
        source_user_id="u1",
        conversation_id="c1",
        event_id=event_id,  # type: ignore[arg-type]
        operation=operation,
        title=title,
        start_time=start_time,  # type: ignore[arg-type]
        status=status,
        event_json="{}",
        caldav_uid=caldav_uid,  # type: ignore[arg-type]
        created_at=created_at or datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
    )
    session.add(rec)
    session.commit()
    return rec


def _svc(session, *, tz="Asia/Shanghai", week_start="1") -> SettingsService:
    """Create a SettingsService with timezone and week_start_day set."""
    svc = SettingsService(session)
    svc.set("caldav_timezone", tz)
    svc.set("week_start_day", week_start)
    svc.commit()
    return svc


class _FixedNow(datetime):
    """datetime subclass that overrides .now() for deterministic tests.

    Fixed to 2026-06-07 00:30 UTC, which is 2026-06-07 08:30 Asia/Shanghai.
    """

    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 6, 7, 0, 30, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# _event_key  (imported helper)
# ---------------------------------------------------------------------------

def test_event_key_prefers_event_id():
    session = _session()
    r = _seed(session, event_id="eid", caldav_uid="uid")
    assert _event_key(r) == "eid"


def test_event_key_falls_back_to_caldav_uid():
    session = _session()
    r = _seed(session, event_id=None, caldav_uid="uid")
    assert _event_key(r) == "uid"


def test_event_key_falls_back_to_row_id():
    session = _session()
    r = _seed(session, event_id=None, caldav_uid=None)
    assert _event_key(r) == f"_{r.id}"


# ---------------------------------------------------------------------------
# timezone-aware today
# ---------------------------------------------------------------------------

def test_dashboard_stats_uses_configured_timezone_for_today(monkeypatch):
    """Fixed now=2026-06-07 08:30 CST, today=2026-06-07.
    Event 06-07 counted; 06-06 excluded."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session, tz="Asia/Shanghai")
    _ = _seed(session, start_time="2026-06-07T09:00:00+08:00", event_id="ev-a", caldav_uid="a")
    _ = _seed(session, start_time="2026-06-06T23:00:00+08:00", event_id="ev-b", caldav_uid="b")
    stats = dashboard_stats(session, svc)
    assert stats["today_events"] == 1


def test_dashboard_stats_today_parses_naive_start_time_in_configured_tz(monkeypatch):
    """Naive start_time '2026-06-07' treated as Asia/Shanghai, counted as today."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session, tz="Asia/Shanghai")
    _ = _seed(session, start_time="2026-06-07", event_id="ev-a", caldav_uid="a")
    _ = _seed(session, start_time="2026-06-06", event_id="ev-b", caldav_uid="b")
    stats = dashboard_stats(session, svc)
    assert stats["today_events"] == 1


# ---------------------------------------------------------------------------
# configurable week start  (Sunday / Monday)
# ---------------------------------------------------------------------------

def test_dashboard_stats_week_start_sunday_includes_sunday(monkeypatch):
    """week_start_day=0, now=Sun 2026-06-07.  Week=Sun..Sat.  Sun counted; Sat before excluded."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session, week_start="0")
    _ = _seed(session, start_time="2026-06-07T09:00:00+08:00", event_id="ev1", caldav_uid="u1")
    _ = _seed(session, start_time="2026-06-06T09:00:00+08:00", event_id="ev2", caldav_uid="u2")
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


def test_dashboard_stats_week_start_monday_excludes_previous_sunday(monkeypatch):
    """week_start_day=1, now=Sun 2026-06-07.  Week=Mon..Sun.  Mon counted; Sun before excluded."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session, week_start="1")
    _ = _seed(session, start_time="2026-06-01T09:00:00+08:00", event_id="ev1", caldav_uid="u1")
    _ = _seed(session, start_time="2026-05-31T09:00:00+08:00", event_id="ev2", caldav_uid="u2")
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


# ---------------------------------------------------------------------------
# week upper bound
# ---------------------------------------------------------------------------

def test_dashboard_stats_week_has_upper_bound(monkeypatch):
    """Next-week event (Mon 06-08) excluded from current week (Mon 06-01..Sun 06-07)."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session, week_start="1")
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="ev1", caldav_uid="u1")
    _ = _seed(session, start_time="2026-06-08T09:00:00+08:00", event_id="ev2", caldav_uid="u2")
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


def test_dashboard_stats_week_lower_bound_respected(monkeypatch):
    """Previous-week event (Mon 05-25) excluded from current week (Mon 06-01..Sun 06-07)."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session, week_start="1")
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="ev1", caldav_uid="u1")
    _ = _seed(session, start_time="2026-05-25T09:00:00+08:00", event_id="ev2", caldav_uid="u2")
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


def test_dashboard_stats_week_end_inclusive(monkeypatch):
    """Sunday 06-07 (week end) is included in the week."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session, week_start="1")
    _ = _seed(session, start_time="2026-06-01T09:00:00+08:00", event_id="ev1", caldav_uid="u1")
    _ = _seed(session, start_time="2026-06-07T09:00:00+08:00", event_id="ev2", caldav_uid="u2")
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 2


# ---------------------------------------------------------------------------
# month counting with parsed dates
# ---------------------------------------------------------------------------

def test_dashboard_stats_month_uses_parsed_event_date(monkeypatch):
    """Month boundary: June event counted, July event excluded."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="2026-06-30T09:00:00+08:00", event_id="ev1", caldav_uid="u1")
    _ = _seed(session, start_time="2026-07-01T09:00:00+08:00", event_id="ev2", caldav_uid="u2")
    stats = dashboard_stats(session, svc)
    assert stats["month_events"] == 1


# ---------------------------------------------------------------------------
# deduplication  (all_records loop, _event_key)
# ---------------------------------------------------------------------------

def test_dashboard_stats_dedupe_same_event_id(monkeypatch):
    """Two records sharing event_id, deduplicated to one count (latest wins)."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="ev1", caldav_uid="old",
         created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", operation="update", event_id="ev1", caldav_uid="new",
         created_at=datetime(2026, 6, 2, tzinfo=timezone.utc))
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


def test_dashboard_stats_dedupe_same_caldav_uid_null_event_id(monkeypatch):
    """Two records with same caldav_uid and null event_id, deduplicated."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id=None, caldav_uid="uid1",
         created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id=None, caldav_uid="uid1",
         created_at=datetime(2026, 6, 2, tzinfo=timezone.utc))
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


def test_dashboard_stats_different_event_id_same_uid_not_deduped(monkeypatch):
    """Different event_id but same caldav_uid: _event_key prefers event_id, both counted."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="e1", caldav_uid="uid1",
         created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="e2", caldav_uid="uid1",
         created_at=datetime(2026, 6, 2, tzinfo=timezone.utc))
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 2


# ---------------------------------------------------------------------------
# delete preservation  (all_records loop)
# ---------------------------------------------------------------------------

def test_dashboard_stats_deleted_latest_event_not_counted(monkeypatch):
    """Latest record is delete, event not counted."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="ev1", caldav_uid="u1",
         created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", operation="delete", event_id="ev1", caldav_uid="u1",
         created_at=datetime(2026, 6, 2, tzinfo=timezone.utc))
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 0
    assert stats["month_events"] == 0


def test_dashboard_stats_update_latest_record_counted_once(monkeypatch):
    """Latest record is update (not delete), counted once."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", operation="create", event_id="ev1", caldav_uid="u1",
         created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    _ = _seed(session, start_time="2026-06-04T09:00:00+08:00", operation="update", event_id="ev1", caldav_uid="u1",
         created_at=datetime(2026, 6, 2, tzinfo=timezone.utc))
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


def test_dashboard_stats_delete_skipped_create_counted(monkeypatch):
    """One event created-then-deleted (counted 0), another just created (counted 1)."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="ev1", caldav_uid="u1",
         created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", operation="delete", event_id="ev1", caldav_uid="u1",
         created_at=datetime(2026, 6, 2, tzinfo=timezone.utc))
    _ = _seed(session, start_time="2026-06-04T09:00:00+08:00", event_id="ev2", caldav_uid="u2",
         created_at=datetime(2026, 6, 3, tzinfo=timezone.utc))
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


# ---------------------------------------------------------------------------
# invalid / empty start_time
# ---------------------------------------------------------------------------

def test_dashboard_stats_skips_unparseable_start_time(monkeypatch):
    """Unparseable start_time, _parse_event_date returns None, skipped."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="not-a-date", event_id="ev1", caldav_uid="u1")
    stats = dashboard_stats(session, svc)
    assert stats["today_events"] == 0
    assert stats["week_events"] == 0
    assert stats["month_events"] == 0


def test_dashboard_stats_skips_empty_start_time(monkeypatch):
    """Empty start_time excluded by WHERE start_time != '' clause."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="", event_id="ev1", caldav_uid="u1")
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="ev2", caldav_uid="u2")
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


def test_dashboard_stats_skips_none_start_time(monkeypatch):
    """NULL start_time excluded by WHERE start_time != '' in SQL."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time=None, event_id="ev1", caldav_uid="u1")
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="ev2", caldav_uid="u2")
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


# ---------------------------------------------------------------------------
# created-at counts  (count_since sub-function)
# ---------------------------------------------------------------------------

def test_today_created_counts_recent_create_records(monkeypatch):
    """today_created counts create+success records with created_at >= today."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    today_start = datetime(2026, 6, 7, 0, 0, tzinfo=timezone.utc)
    yesterday = datetime(2026, 6, 6, 23, 59, tzinfo=timezone.utc)
    _ = _seed(session, operation="create", status="success", created_at=today_start)
    _ = _seed(session, operation="create", status="success", created_at=yesterday)
    _ = _seed(session, operation="create", status="success", created_at=today_start,
         event_id="e3", caldav_uid="u3")
    stats = dashboard_stats(session, svc)
    assert stats["today_created"] == 2
    assert stats["week_created"] == 3
    assert stats["month_created"] == 3


def test_created_counts_exclude_deleted_uid_events(monkeypatch):
    """Events whose caldav_uid appears in a delete record are excluded from created counts."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    now = datetime(2026, 6, 7, 0, 0, tzinfo=timezone.utc)
    _ = _seed(session, operation="create", status="success", caldav_uid="uid_del", created_at=now)
    _ = _seed(session, operation="delete", status="success", caldav_uid="uid_del", created_at=now)
    _ = _seed(session, operation="create", status="success", caldav_uid="uid_ok", created_at=now)
    stats = dashboard_stats(session, svc)
    assert stats["today_created"] == 1


def test_created_counts_include_null_uid_events(monkeypatch):
    """Events with caldav_uid=NULL always counted (no delete match possible)."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    now = datetime(2026, 6, 7, 0, 0, tzinfo=timezone.utc)
    _ = _seed(session, operation="create", status="success", caldav_uid=None, created_at=now)
    _ = _seed(session, operation="create", status="success", caldav_uid=None, created_at=now,
         event_id="e2")
    stats = dashboard_stats(session, svc)
    assert stats["today_created"] == 2


def test_created_counts_ignore_non_success_status(monkeypatch):
    """Only status='success' records contribute to created counts."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    now = datetime(2026, 6, 7, 0, 0, tzinfo=timezone.utc)
    _ = _seed(session, operation="create", status="success", created_at=now)
    _ = _seed(session, operation="create", status="failed", created_at=now, event_id="e2", caldav_uid="u2")
    _ = _seed(session, operation="create", status="pending", created_at=now, event_id="e3", caldav_uid="u3")
    stats = dashboard_stats(session, svc)
    assert stats["today_created"] == 1


def test_created_counts_ignore_non_create_operations(monkeypatch):
    """Only operation='create' records are counted by count_since."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    now = datetime(2026, 6, 7, 0, 0, tzinfo=timezone.utc)
    _ = _seed(session, operation="create", status="success", created_at=now)
    _ = _seed(session, operation="update", status="success", created_at=now, event_id="e2", caldav_uid="u2")
    _ = _seed(session, operation="delete", status="success", created_at=now, event_id="e3", caldav_uid="u3")
    stats = dashboard_stats(session, svc)
    assert stats["today_created"] == 1


# ---------------------------------------------------------------------------
# operation filtering in all_records loop
# ---------------------------------------------------------------------------

def test_all_records_loop_ignores_non_create_update_delete(monkeypatch):
    """Operations other than create/update/delete are not fetched."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", operation="remind", status="success",
         event_id="ev1", caldav_uid="u1")
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", operation="create", status="success",
         event_id="ev2", caldav_uid="u2")
    stats = dashboard_stats(session, svc)
    assert stats["week_events"] == 1


# ---------------------------------------------------------------------------
# edge cases
# ---------------------------------------------------------------------------

def test_dashboard_stats_empty_database(monkeypatch):
    """Empty database, all counts zero."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    stats = dashboard_stats(session, svc)
    assert stats == {
        "today_created": 0,
        "week_created": 0,
        "month_created": 0,
        "today_events": 0,
        "week_events": 0,
        "month_events": 0,
    }


def test_dashboard_stats_preserves_key_set(monkeypatch):
    """Returned dict has exactly the six expected keys."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    stats = dashboard_stats(session, svc)
    assert set(stats.keys()) == {
        "today_created", "week_created", "month_created",
        "today_events", "week_events", "month_events",
    }


def test_dashboard_stats_returns_int_values(monkeypatch):
    """Every value in the returned dict is an int."""
    monkeypatch.setattr(routes, "datetime", _FixedNow)
    session = _session()
    svc = _svc(session)
    _ = _seed(session, start_time="2026-06-03T09:00:00+08:00", event_id="ev1", caldav_uid="u1")
    stats = dashboard_stats(session, svc)
    for key, val in stats.items():
        assert isinstance(val, int), f"{key} is {type(val).__name__}, not int"


# ---------------------------------------------------------------------------
# settings service – week_start save/load
# ---------------------------------------------------------------------------

def test_settings_service_can_save_and_retrieve_week_start():
    """Prove that SettingsService can persist and read a week_start_day key."""
    session = _session()
    settings = SettingsService(session)
    assert settings.get("week_start_day") is None
    settings.set("week_start_day", "0")
    settings.commit()
    assert SettingsService(session).get("week_start_day") == "0"
    settings.set("week_start_day", "1")
    settings.commit()
    assert SettingsService(session).get("week_start_day") == "1"


# ---------------------------------------------------------------------------
# update_data_settings route – system data setting save
# ---------------------------------------------------------------------------

def test_update_data_settings_saves_week_start_day():
    async def run():
        session = _session()
        response = await update_data_settings(
            _request(),
            event_record_limit=500,
            week_start_day="0",
            session=session,
            _=None,
        )
        assert response.status_code == 303
        assert SettingsService(session).get("week_start_day") == "0"
    asyncio.run(run())


def test_update_data_settings_rejects_invalid_week_start_day():
    async def run():
        session = _session()
        settings = SettingsService(session)
        settings.set("week_start_day", "1")
        settings.commit()
        request = _request()
        response = await update_data_settings(
            request,
            event_record_limit=500,
            week_start_day="2",
            session=session,
            _=None,
        )
        assert response.status_code == 303
        assert request.session["error_flash"] == "每周起始日必须是周日或周一。"
        assert settings.get("week_start_day") == "1"
    asyncio.run(run())
