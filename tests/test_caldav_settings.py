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


def test_probes_do_not_persist_drafts_and_do_not_reuse_password_for_new_account(monkeypatch):
    import json
    from app.web import routes
    from app.db.models import Setting
    from sqlalchemy import select
    session = _session()
    svc = SettingsService(session)
    svc.set('caldav_url', 'https://old.example.com')
    svc.set('caldav_username', 'old-user')
    svc.set('caldav_password', 'saved-test-password')
    svc.commit()
    before = [(r.key, r.value) for r in session.scalars(select(Setting)).all()]
    calls = []
    class FakeError(Exception): pass
    class FakeCalendar:
        async def test_connection(self, url, user, password, **kwargs):
            calls.append((url,user,password,kwargs))
        async def list_calendars(self, url, user, password, **kwargs):
            calls.append((url,user,password,kwargs))
            return [{'name':'Work','url':url+'/work'}, {'name':'Work','url':url+'/other'}]
    monkeypatch.setattr(routes, '_caldav_components', lambda: (FakeCalendar, FakeError))
    for listing in (False, True):
        response = asyncio.run(routes._probe_caldav(session,'https://new.example.com','new-user','new-test-password','false',listing))
        assert response.status_code == 200
        assert json.loads(response.body)['ok']
    assert all(call[2] == 'new-test-password' and call[3]['ssl_verify'] is False for call in calls)
    assert before == [(r.key,r.value) for r in session.scalars(select(Setting)).all()]
    response = asyncio.run(routes._probe_caldav(session,'https://new.example.com','new-user','','true',False))
    assert response.status_code == 400 and len(calls) == 2
    response = asyncio.run(routes._probe_caldav(session,'https://old.example.com','old-user','','true',False))
    assert response.status_code == 200 and calls[-1][2] == 'saved-test-password'


def test_probe_failure_does_not_echo_credentials_or_write(monkeypatch):
    from app.web import routes
    class FakeError(Exception): pass
    class FakeCalendar:
        async def test_connection(self, *args, **kwargs):
            raise FakeError('password=private-test-credential')
    monkeypatch.setattr(routes,'_caldav_components',lambda: (FakeCalendar, FakeError))
    session=_session()
    response=asyncio.run(routes._probe_caldav(session,'https://example.com','user','private-test-credential','true',False))
    assert response.status_code == 400
    assert b'private-test-credential' not in response.body
    assert SettingsService(session).get('caldav_url') is None


def test_inline_save_validation_keeps_saved_settings():
    from app.web import routes
    session=_session()
    service=SettingsService(session)
    service.set('caldav_url','https://old.example.com');service.commit()
    req=Request({'type':'http','method':'POST','path':'/console/caldav','headers':[(b'accept',b'application/json')], 'session':{}})
    response=asyncio.run(routes.update_caldav_settings(req,caldav_reminder_minutes='bad',caldav_default_duration='60',session=session,_=None))
    assert response.status_code == 400
    assert service.get('caldav_url') == 'https://old.example.com'


def test_inline_save_persists_only_on_save_and_changed_account_requires_password():
    import json
    from app.web import routes
    session=_session()
    req=Request({'type':'http','method':'POST','path':'/console/caldav','headers':[(b'accept',b'application/json')], 'session':{}})
    kwargs=dict(caldav_url='https://example.com',caldav_username='user',caldav_password='',caldav_calendar_url='https://example.com/work',caldav_calendar_name='Work',caldav_timezone='Asia/Shanghai',caldav_reminder_minutes='15',caldav_default_duration='60',caldav_ssl_verify='true',session=session,_=None)
    response=asyncio.run(routes.update_caldav_settings(req,**kwargs))
    assert json.loads(response.body)['ok']
    service=SettingsService(session)
    assert service.get('caldav_calendar_name') == 'Work'
    service.set('caldav_password','saved-test-password');service.commit()
    kwargs['caldav_username']='other'
    response=asyncio.run(routes.update_caldav_settings(req,**kwargs))
    assert response.status_code == 400
    assert service.get('caldav_username') == 'user'


def test_get_discovery_urls_prioritizes_upward_for_leaf_urls():
    from app.services.caldav_service import _get_discovery_urls

    # Non-leaf base URLs: original URL comes first
    base_urls = _get_discovery_urls("https://caldav.icloud.com")
    assert base_urls == ["https://caldav.icloud.com"]

    base_slash = _get_discovery_urls("https://caldav.icloud.com/")
    assert base_slash == ["https://caldav.icloud.com/"]

    # Leaf URLs with calendar suffix: upward principal/home URLs come first
    icloud_leaf = _get_discovery_urls("https://caldav.icloud.com/123456789/calendars/home")
    assert icloud_leaf[0] == "https://caldav.icloud.com/123456789/calendars/"
    assert "https://caldav.icloud.com/123456789/" in icloud_leaf
    assert icloud_leaf[-1] == "https://caldav.icloud.com/123456789/calendars/home"

    # Multi-segment path
    nextcloud_leaf = _get_discovery_urls("https://dav.example.com/remote.php/dav/calendars/user/personal")
    assert nextcloud_leaf[0] == "https://dav.example.com/remote.php/dav/calendars/user/"
    assert nextcloud_leaf[-1] == "https://dav.example.com/remote.php/dav/calendars/user/personal"


def test_list_calendars_prioritizes_full_discovery_over_propfind(monkeypatch):
    from app.services.caldav_service import CalDAVService

    class FakeCal:
        def __init__(self, name, url):
            self.name = name
            self.url = url

    class FakeClient:
        def __init__(self, url, **kwargs):
            self.url = url

        def get_calendars(self):
            return [
                FakeCal("Personal", "https://caldav.icloud.com/cal/personal"),
                FakeCal("Work", "https://caldav.icloud.com/cal/work"),
                FakeCal("Home", "https://caldav.icloud.com/cal/home"),
                FakeCal("AI", "https://caldav.icloud.com/cal/ai"),
            ]

        def calendar(self, url, **kwargs):
            raise AssertionError("Should not reach single-calendar fallback when get_calendars succeeds")

    monkeypatch.setattr("app.services.caldav_service._DAVClient", FakeClient)

    svc = CalDAVService()
    cals = asyncio.run(svc.list_calendars("https://caldav.icloud.com", "user", "pass"))
    assert len(cals) == 4
    names = [c["name"] for c in cals]
    assert names == ["Personal", "Work", "Home", "AI"]


def test_list_calendars_upward_discovery_discovers_all_calendars(monkeypatch):
    from app.services.caldav_service import CalDAVService

    class FakeCal:
        def __init__(self, name, url):
            self.name = name
            self.url = url

    # Simulate: probing the leaf URL directly returns nothing,
    # but probing the parent upward collection returns all 4 calendars.
    class FakeClient:
        def __init__(self, url, **kwargs):
            self.url = url

        def get_calendars(self):
            if self.url == "https://caldav.icloud.com/123456789/calendars/home":
                return []
            if "calendars" in self.url or "123456789" in self.url:
                return [
                    FakeCal("Personal", "https://caldav.icloud.com/cal/personal"),
                    FakeCal("Work", "https://caldav.icloud.com/cal/work"),
                    FakeCal("Home", "https://caldav.icloud.com/cal/home"),
                    FakeCal("AI", "https://caldav.icloud.com/cal/ai"),
                ]
            return []

        def principal(self):
            return self

        def calendars(self):
            return self.get_calendars()

    monkeypatch.setattr("app.services.caldav_service._DAVClient", FakeClient)

    svc = CalDAVService()
    cals = asyncio.run(svc.list_calendars("https://caldav.icloud.com/123456789/calendars/home", "user", "pass"))
    assert len(cals) == 4
    names = [c["name"] for c in cals]
    assert names == ["Personal", "Work", "Home", "AI"]


def test_list_calendars_fallback_to_propfind_for_163_qq(monkeypatch):
    from app.services.caldav_service import CalDAVService

    class FakeCal:
        def __init__(self, name, url):
            self.name = name
            self.url = url

    class Fake163Client:
        def __init__(self, url, **kwargs):
            self.url = url

        def get_calendars(self):
            raise Exception("163 does not support get_calendars")

        def principal(self):
            # 163 principal doesn't have calendars()
            class FakePrincipal:
                def calendars(self):
                    raise Exception("163 does not support principal.calendars")
            return FakePrincipal()

        def calendar(self, url, name=None, **kwargs):
            return FakeCal(name or "default", url)

    monkeypatch.setattr("app.services.caldav_service._DAVClient", Fake163Client)

    svc = CalDAVService()
    cals = asyncio.run(svc.list_calendars("https://caldav.163.com/dav/user@163.com/default/", "user@163.com", "pass"))
    assert len(cals) == 1
    assert cals[0]["name"] == "default"
    assert cals[0]["url"] == "https://caldav.163.com/dav/user@163.com/default/"


def test_list_calendars_strategy_probe_sequence():
    from unittest.mock import patch
    from app.services.caldav_service import CalDAVService

    calls = []

    class MockCal:
        def __init__(self, name, url):
            self._name = name
            self.url = url

        def get_display_name(self):
            return self._name

    class MockClient:
        def __init__(self, url, **kwargs):
            self.url = url

        def get_calendars(self):
            calls.append(("get_calendars", self.url))
            raise Exception("get_calendars failed")

        def principal(self):
            calls.append(("principal", self.url))
            return self

        def calendars(self):
            calls.append(("principal.calendars", self.url))
            if "calendars" in self.url and not self.url.endswith("/home"):
                # upward collection path succeeds!
                return [MockCal("Work", "https://caldav.example.com/user/calendars/work")]
            raise Exception("principal.calendars failed")

        def calendar(self, url, name=None, **kwargs):
            calls.append(("calendar", url))
            return MockCal(name or "default", url)

    with patch("app.services.caldav_service._DAVClient", MockClient):
        svc = CalDAVService()
        leaf_url = "https://caldav.example.com/user/calendars/home"
        res = asyncio.run(svc.list_calendars(leaf_url, "user", "pass"))
        assert len(res) == 1
        assert res[0]["name"] == "Work"

        # Assert strategy order:
        # First candidate is upward URL (https://caldav.example.com/user/calendars/)
        assert calls[0] == ("get_calendars", "https://caldav.example.com/user/calendars/")
        assert calls[1] == ("principal", "https://caldav.example.com/user/calendars/")
        assert calls[2] == ("principal.calendars", "https://caldav.example.com/user/calendars/")
        # _try_propfind (single calendar fallback) was NEVER called because upward principal succeeded
        assert not any(c[0] == "calendar" for c in calls)


def test_get_calendar_name_resolution():
    from app.services.caldav_service import _get_calendar_name

    # 1. get_display_name method
    class CalWithMethod:
        def get_display_name(self):
            return "Display Name Calendar"
    assert _get_calendar_name(CalWithMethod()) == "Display Name Calendar"

    # 2. name property
    class CalWithName:
        name = "Name Attr Calendar"
    assert _get_calendar_name(CalWithName()) == "Name Attr Calendar"

    # 3. unquoted url segment
    class CalWithUrl:
        url = "https://example.com/calendars/user/Personal%20Events/"
    assert _get_calendar_name(CalWithUrl()) == "Personal Events"

    # 4. default_name fallback
    class CalEmpty:
        pass
    assert _get_calendar_name(CalEmpty(), default_name="Fallback Name") == "Fallback Name"


def test_caldav_html_template_renders_review_guidance_and_recheck_button():
    """Verify caldav.html template renders review status, notice box, meta timestamps, and recheck button."""
    from pathlib import Path
    html = Path("app/web/templates/caldav.html").read_text(encoding="utf-8")

    # 1. Right column review elements
    assert 'id="caldav-status-card"' in html
    assert 'id="caldav-service-badge"' in html
    assert 'id="caldav-service-status-text"' in html
    assert 'class="caldav-check-meta"' in html
    assert 'id="caldav-check-label"' in html
    assert 'id="caldav-check-time"' in html
    assert 'id="caldav-review-notice"' in html
    assert 'id="caldav-review-reason-text"' in html
    assert 'id="btn-recheck-caldav"' in html
    assert '一键复核 / 验证已保存配置' in html

    # 2. JS synchronization hooks
    assert "updateReviewState(" in html
    assert "btnRecheck.addEventListener('click'" in html
    assert "'/console/connections/caldav/test'" in html


def test_caldav_payload_and_status_alignment_when_review_needed():
    """Verify caldav_payload accurately reflects needs_review / warning when configuration changes."""
    import json
    from datetime import datetime, timezone
    from app.web.connection_checks import revision, save_check

    session = _session()
    settings = SettingsService(session)
    settings.set("caldav_url", "https://caldav.icloud.com")
    settings.set("caldav_calendar_url", "https://caldav.icloud.com/cal/work")
    settings.set("caldav_calendar_name", "Work Calendar")
    settings.commit()

    # Before check -> 待验证
    p1 = caldav_payload(settings, session)
    assert p1["caldav_dot_class"] == "dot-warning"
    assert p1["caldav_state_label"] == "待验证"
    assert "尚未进行连通性测试" in p1["caldav_review_reason"]

    # After passed check -> 已验证
    sig = revision(session, "caldav")
    save_check(session, "caldav", sig, True)
    session.expire_all()

    p2 = caldav_payload(settings, session)
    assert p2["caldav_dot_class"] == "dot-success"
    assert p2["caldav_state_label"] == "已验证"
    assert p2["caldav_review_reason"] == ""
    assert p2["caldav_check"]["label"] == "最近测试成功"

    # When URL changes -> 需复核
    settings.set("caldav_url", "https://caldav.new-host.com")
    settings.commit()
    session.expire_all()

    p3 = caldav_payload(settings, session)
    assert p3["caldav_dot_class"] == "dot-warning"
    assert p3["caldav_state_label"] == "需复核"
    assert "配置已变更" in p3["caldav_review_reason"]
