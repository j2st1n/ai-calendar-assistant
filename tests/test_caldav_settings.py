import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db.models import Base
from app.services.settings_service import SettingsService
from app.web.routes import _normalize_caldav_int_setting, update_caldav_settings


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/console/caldav", "headers": [], "session": {}})


def test_normalize_caldav_int_setting_accepts_valid_and_empty_values():
    assert _normalize_caldav_int_setting("15", 30, 0, "bad int", "too small") == (15, None)
    assert _normalize_caldav_int_setting("0", 30, 0, "bad int", "too small") == (0, None)
    assert _normalize_caldav_int_setting("", 30, 0, "bad int", "too small") == (30, None)
    assert _normalize_caldav_int_setting(" 60 ", 30, 0, "bad int", "too small") == (60, None)


def test_normalize_caldav_int_setting_rejects_invalid_values():
    assert _normalize_caldav_int_setting("abc", 30, 0, "bad int", "too small") == (None, "bad int")
    assert _normalize_caldav_int_setting("10.5", 30, 0, "bad int", "too small") == (None, "bad int")
    assert _normalize_caldav_int_setting("-1", 30, 0, "bad int", "too small") == (None, "too small")
    assert _normalize_caldav_int_setting("4", 60, 5, "bad int", "too small") == (None, "too small")


def test_update_caldav_settings_saves_normalized_numeric_values():
    async def run():
        session = _session()

        response = await update_caldav_settings(
            _request(),
            caldav_url=" https://example.com/caldav/ ",
            caldav_username=" user ",
            caldav_password="",
            caldav_calendar_url=" cal-url ",
            caldav_calendar_name=" cal-name ",
            caldav_timezone=" Asia/Shanghai ",
            caldav_reminder_minutes="",
            caldav_default_duration=" 5 ",
            session=session,
            _=None,
        )

        settings = SettingsService(session)
        assert response.status_code == 303
        assert settings.get("caldav_reminder_minutes") == "30"
        assert settings.get("caldav_default_duration") == "5"

    asyncio.run(run())


def test_update_caldav_settings_rejects_bad_reminder_without_saving():
    async def run():
        session = _session()
        settings = SettingsService(session)
        settings.set("caldav_reminder_minutes", "45")
        settings.set("caldav_default_duration", "60")
        settings.commit()
        request = _request()

        response = await update_caldav_settings(
            request,
            caldav_reminder_minutes="bad",
            caldav_default_duration="30",
            session=session,
            _=None,
        )

        assert response.status_code == 303
        assert request.session["error_flash"] == "提醒分钟数必须是整数。"
        assert settings.get("caldav_reminder_minutes") == "45"
        assert settings.get("caldav_default_duration") == "60"

    asyncio.run(run())


def test_update_caldav_settings_rejects_short_duration_without_saving():
    async def run():
        session = _session()
        settings = SettingsService(session)
        settings.set("caldav_reminder_minutes", "45")
        settings.set("caldav_default_duration", "60")
        settings.commit()
        request = _request()

        response = await update_caldav_settings(
            request,
            caldav_reminder_minutes="15",
            caldav_default_duration="4",
            session=session,
            _=None,
        )

        assert response.status_code == 303
        assert request.session["error_flash"] == "默认持续时间不能少于 5 分钟。"
        assert settings.get("caldav_reminder_minutes") == "45"
        assert settings.get("caldav_default_duration") == "60"

    asyncio.run(run())
