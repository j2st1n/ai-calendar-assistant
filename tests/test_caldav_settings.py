import asyncio

from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db.models import Base
from app.services.settings_service import SettingsService
from app.web.routes import (
    _caldav_ssl_from_form,
    _caldav_ssl_from_settings,
    _normalize_caldav_int_setting,
    caldav_payload,
    require_admin,
    router,
    update_caldav_settings,
)


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/console/caldav", "headers": [], "session": {}})


def test_list_caldav_calendars_requires_admin():
    route = next(
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == "/console/caldav/calendars"
        and route.methods
        and "POST" in route.methods
    )

    dependency_calls = [dep.call for dep in route.dependant.dependencies]
    assert require_admin in dependency_calls


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
            caldav_ssl_verify="true",
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


def test_caldav_ssl_from_form_only_accepts_true():
    assert _caldav_ssl_from_form("true") is True
    assert _caldav_ssl_from_form(" true ") is True
    assert _caldav_ssl_from_form("false") is False
    assert _caldav_ssl_from_form("") is False
    assert _caldav_ssl_from_form("on") is False


def test_caldav_ssl_from_settings_defaults_true_when_not_set():
    session = _session()
    settings = SettingsService(session)
    assert _caldav_ssl_from_settings(settings) is True


def test_caldav_ssl_from_settings_returns_false_when_explicitly_false():
    session = _session()
    settings = SettingsService(session)
    settings.set("caldav_ssl_verify", "false")
    settings.commit()
    assert _caldav_ssl_from_settings(settings) is False


def test_caldav_ssl_from_settings_returns_true_for_any_non_false_value():
    session = _session()
    settings = SettingsService(session)
    for val in ("true", "1", "yes", "on", ""):
        settings.set("caldav_ssl_verify", val)
        settings.commit()
        assert _caldav_ssl_from_settings(settings) is True


def test_caldav_payload_includes_ssl_verify_default_true():
    session = _session()
    settings = SettingsService(session)
    payload = caldav_payload(settings)
    assert payload["caldav_ssl_verify"] == "true"


def test_caldav_payload_reflects_saved_ssl_verify():
    session = _session()
    settings = SettingsService(session)
    settings.set("caldav_ssl_verify", "false")
    settings.commit()
    payload = caldav_payload(settings)
    assert payload["caldav_ssl_verify"] == "false"


def test_update_caldav_settings_saves_ssl_verify_true():
    async def run():
        session = _session()

        response = await update_caldav_settings(
            _request(),
            caldav_url="https://test.example.com",
            caldav_username="testuser",
            caldav_password="",
            caldav_calendar_url="",
            caldav_calendar_name="",
            caldav_timezone="Asia/Shanghai",
            caldav_reminder_minutes="30",
            caldav_default_duration="60",
            caldav_ssl_verify="true",
            session=session,
            _=None,
        )

        settings = SettingsService(session)
        assert response.status_code == 303
        assert settings.get("caldav_ssl_verify") == "true"

    asyncio.run(run())


def test_update_caldav_settings_saves_ssl_verify_false():
    async def run():
        session = _session()
        settings = SettingsService(session)
        settings.set("caldav_ssl_verify", "true")
        settings.commit()

        response = await update_caldav_settings(
            _request(),
            caldav_url="https://test.example.com",
            caldav_username="testuser",
            caldav_password="",
            caldav_calendar_url="",
            caldav_calendar_name="",
            caldav_timezone="Asia/Shanghai",
            caldav_reminder_minutes="30",
            caldav_default_duration="60",
            caldav_ssl_verify="false",
            session=session,
            _=None,
        )

        assert response.status_code == 303
        assert settings.get("caldav_ssl_verify") == "false"

    asyncio.run(run())


def test_ssl_verify_reaches_davclient_param():
    """Prove that ssl_verify_cert param is wired to _DAVClient constructor."""
    from unittest.mock import patch

    class FakeClient:
        """Record the kwargs passed to _DAVClient."""

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def principal(self):
            return None

    async def run():
        from app.services.caldav_service import CalDAVService

        with patch("app.services.caldav_service._DAVClient", side_effect=FakeClient) as mock_dav:
            svc = CalDAVService()
            try:
                await svc.test_connection("https://example.com", "user", "pass", ssl_verify=False)
            except Exception:
                pass
            call_kwargs = mock_dav.call_args.kwargs
            assert call_kwargs.get("ssl_verify_cert") is False, f"Expected ssl_verify_cert=False, got {call_kwargs}"

            mock_dav.reset_mock()
            try:
                await svc.test_connection("https://example.com", "user", "pass", ssl_verify=True)
            except Exception:
                pass
            call_kwargs = mock_dav.call_args.kwargs
            assert call_kwargs.get("ssl_verify_cert") is True, f"Expected ssl_verify_cert=True, got {call_kwargs}"

    asyncio.run(run())


def test_message_processor_caldav_config_includes_ssl_bool():
    from app.channels.message_processor import _caldav_config

    session = _session()
    settings = SettingsService(session)
    cfg = _caldav_config(settings)
    assert cfg["ssl"] is True

    settings.set("caldav_ssl_verify", "false")
    settings.commit()
    cfg = _caldav_config(settings)
    assert cfg["ssl"] is False

    settings.set("caldav_ssl_verify", "true")
    settings.commit()
    cfg = _caldav_config(settings)
    assert cfg["ssl"] is True


def test_update_caldav_settings_saves_ssl_verify_false_when_checkbox_missing():
    async def run():
        session = _session()
        settings = SettingsService(session)
        settings.set("caldav_ssl_verify", "true")
        settings.commit()

        response = await update_caldav_settings(
            _request(),
            caldav_url="https://test.example.com",
            caldav_username="testuser",
            caldav_password="",
            caldav_calendar_url="",
            caldav_calendar_name="",
            caldav_timezone="Asia/Shanghai",
            caldav_reminder_minutes="30",
            caldav_default_duration="60",
            caldav_ssl_verify="",
            session=session,
            _=None,
        )

        assert response.status_code == 303
        assert settings.get("caldav_ssl_verify") == "false"

    asyncio.run(run())
