from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db.models import Base, EventRecord, Setting
from app.services.settings_service import SettingsService
from app.channels.message_processor import _record, ChannelContext
from app.web import routes


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def test_fingerprint_deterministic_and_monotonic_versioning(db_session):
    svc = SettingsService(db_session)

    # Initial state
    v1 = svc.get_config_version()
    assert v1 == 1
    ai_hash1 = svc.get_ai_config_hash()
    cal_hash1 = svc.get_caldav_config_hash()
    assert len(ai_hash1) == 64
    assert len(cal_hash1) == 64

    # Repeated calls without change do not increment version
    assert svc.get_config_version() == 1

    # Non-config changes do not increment version
    svc.set("session_days", "30")
    svc.commit()
    assert svc.get_config_version() == 1
    assert svc.get_ai_config_hash() == ai_hash1

    # AI config change increments version monotonically
    svc.set("ai_model", "gpt-4o")
    svc.commit()
    v2 = svc.get_config_version()
    assert v2 == 2
    ai_hash2 = svc.get_ai_config_hash()
    assert ai_hash2 != ai_hash1

    # CalDAV config change increments version monotonically
    svc.set("caldav_url", "https://caldav.example.com/dav")
    svc.commit()
    v3 = svc.get_config_version()
    assert v3 == 3
    cal_hash2 = svc.get_caldav_config_hash()
    assert cal_hash2 != cal_hash1

    # Fingerprint includes both AI and CalDAV hashes
    fp = svc.get_config_fingerprint()
    import hashlib
    assert fp == hashlib.sha256(f"{ai_hash2}:{cal_hash2}".encode("utf-8")).hexdigest()


def test_fingerprint_changes_on_credential_rotation(db_session):
    svc = SettingsService(db_session)
    svc.set("caldav_password", "old-secret-password", encrypted=True)
    svc.set("ai_api_key", "old-ai-api-key", encrypted=True)
    svc.commit()
    v_base = svc.get_config_version()
    fp_base = svc.get_config_fingerprint()

    # Rotate CalDAV password -> version increments, fingerprint changes
    svc.set("caldav_password", "new-rotated-password", encrypted=True)
    svc.commit()
    v_after_caldav = svc.get_config_version()
    fp_after_caldav = svc.get_config_fingerprint()
    assert v_after_caldav > v_base
    assert fp_after_caldav != fp_base

    # Rotate AI API key -> version increments, fingerprint changes
    svc.set("ai_api_key", "new-ai-api-key", encrypted=True)
    svc.commit()
    v_after_ai = svc.get_config_version()
    fp_after_ai = svc.get_config_fingerprint()
    assert v_after_ai > v_after_caldav
    assert fp_after_ai != fp_after_caldav


def test_message_processor_record_stores_version_and_hashes(db_session):
    svc = SettingsService(db_session)
    svc.set("ai_model", "deepseek-chat")
    svc.set("caldav_url", "https://cal.example.com")
    svc.commit()
    expected_version = svc.get_config_version()
    expected_ai_hash = svc.get_ai_config_hash()
    expected_cal_hash = svc.get_caldav_config_hash()

    ctx = ChannelContext(source="telegram", source_user_id="u123", conversation_id="c123")
    rec_id = _record(
        db_session,
        ctx,
        op="create",
        title="开会",
        text="明天下午开会",
        status="success",
        js="{}",
        cr={"uid": "uid-1", "href": "/href-1"},
    )
    db_session.commit()

    record = db_session.get(EventRecord, rec_id)
    assert record is not None
    assert record.config_version == expected_version
    assert record.ai_config_hash == expected_ai_hash
    assert record.caldav_config_hash == expected_cal_hash


def test_status_context_eliminates_false_positives(db_session):
    svc = SettingsService(db_session)
    svc.set("caldav_url", "https://old-server.com")
    svc.commit()
    assert svc.get_config_version() == 1

    # Create a success under version 1
    ctx = ChannelContext(source="telegram", source_user_id="u1", conversation_id="c1")
    _record(
        db_session,
        ctx,
        op="create",
        title="旧配置成功事项",
        text="旧记录",
        status="success",
        js="{}",
        start_time="2026-06-01T10:00:00+08:00",
    )
    db_session.commit()

    # In version 1: current success matches
    ctx1 = routes.status_context(db_session)
    assert ctx1["current_config_version"] == 1
    assert ctx1["last_calendar_success_current"] != ""
    assert ctx1["last_calendar_success_legacy"] == ""

    # Now change configuration to invalid credentials/new server -> version becomes 2
    svc.set("caldav_url", "https://new-broken-server.com")
    svc.commit()
    assert svc.get_config_version() == 2

    # Under version 2, no success has occurred yet
    ctx2 = routes.status_context(db_session)
    assert ctx2["current_config_version"] == 2
    assert ctx2["last_calendar_success_current"] == ""
    # Legacy success exists from version 1
    assert ctx2["last_calendar_success_legacy"] != ""

    # Check template rendering under false-positive scenario (legacy only)
    request = Request({"type": "http", "method": "GET", "path": "/console", "headers": [], "session": {"admin_authenticated": True}})
    html = routes.templates.get_template("dashboard.html").render(
        request=request, stats=routes.dashboard_stats(db_session, svc), **ctx2
    )
    # Must clearly warn that current config has no success and history is from old config
    assert "当前配置暂无日历操作成功记录" in html
    assert "历史旧配置最近成功" in html
    assert "配置已变更，历史记录不代表当前配置有效" in html

    # Now record a success under version 2
    _record(
        db_session,
        ctx,
        op="create",
        title="新配置成功事项",
        text="新记录",
        status="success",
        js="{}",
        start_time="2026-06-02T10:00:00+08:00",
    )
    db_session.commit()

    ctx3 = routes.status_context(db_session)
    assert ctx3["last_calendar_success_current"] != ""
    html3 = routes.templates.get_template("dashboard.html").render(
        request=request, stats=routes.dashboard_stats(db_session, svc), **ctx3
    )
    assert "当前配置已生效" in html3
