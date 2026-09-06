import asyncio
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels.message_processor import ChannelContext, _handle_new, _write_one
from app.db.models import Base, EventRecord
from app.main import create_app
from app.services.caldav_service import CalDAVServiceError
from app.services.settings_service import SettingsService
from app.web.routes import _retry_locks, _retry_mutex, get_db, require_admin

from sqlalchemy.pool import StaticPool

ORIGIN_HEADERS = {"origin": "http://testserver"}


def _create_test_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return session_factory()


def _override_db(session):
    def _get_db():
        yield session
    return _get_db


def test_failure_phase_tagging_in_message_processor():
    async def _run():
        session = _create_test_session()
        ctx = ChannelContext(source="telegram", source_user_id="user123")
        caldav = {"url": "http://caldav", "user": "usr", "pw": "pwd", "cal": "default", "rem": 30, "dur": 60, "ssl": True}
        svc = SettingsService(session)

        # 1. Error type -> extraction
        res1 = ExtractionResult(intent=Intent.no_event, error_type="AI_API_ERROR")
        await _handle_new(session, ctx, "明天开会", res1, caldav, svc)
        rec1 = session.query(EventRecord).filter_by(id=1).first()
        assert rec1 is not None
        assert rec1.status == "failed"
        assert rec1.failure_phase == "extraction"

        # 2. Intent no_event -> extraction
        res2 = ExtractionResult(intent=Intent.no_event)
        await _handle_new(session, ctx, "你好呀", res2, caldav, svc)
        rec2 = session.query(EventRecord).filter_by(id=2).first()
        assert rec2 is not None
        assert rec2.status == "failed"
        assert rec2.failure_phase == "extraction"

        # 3. Missing fields -> validation
        res3 = ExtractionResult(intent=Intent.create_event, missing_fields=["时间"])
        await _handle_new(session, ctx, "开会", res3, caldav, svc)
        rec3 = session.query(EventRecord).filter_by(id=3).first()
        assert rec3 is not None
        assert rec3.status == "failed"
        assert rec3.failure_phase == "validation"

        # 4. Unsupported reason -> validation
        res4 = ExtractionResult(intent=Intent.create_event, unsupported_reason="每两周隔周五")
        await _handle_new(session, ctx, "重复开会", res4, caldav, svc)
        rec4 = session.query(EventRecord).filter_by(id=4).first()
        assert rec4 is not None
        assert rec4.status == "failed"
        assert rec4.failure_phase == "validation"

        # 5. CalDAV write failure -> write
        event = CalendarEvent(title="周五周会", start_time="2026-10-16T15:00:00")
        with patch("app.channels.message_processor._write_caldav", side_effect=CalDAVServiceError("Network timeout")):
            rec_id, ok = await _write_one(session, ctx, "周五下午3点开周会", event, caldav)
            session.commit()
            assert ok is False
            rec5 = session.query(EventRecord).filter_by(id=rec_id).first()
            assert rec5 is not None
            assert rec5.status == "failed"
            assert rec5.failure_phase == "write"
            assert rec5.caldav_uid is not None  # Preserves pre-generated UID for idempotent retry

    asyncio.run(_run())


def test_retry_event_requires_admin():
    app = create_app()
    client = TestClient(app)
    res = client.post("/console/events/1/retry", headers=ORIGIN_HEADERS, follow_redirects=False)
    assert res.status_code in (303, 401, 403)


def test_retry_event_not_found_and_already_success():
    app = create_app()
    session = _create_test_session()

    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db] = _override_db(session)
    client = TestClient(app)

    # 1. Not found -> 404
    res_404 = client.post("/console/events/9999/retry", headers=ORIGIN_HEADERS)
    assert res_404.status_code == 404
    assert res_404.json()["ok"] is False

    # 2. Already success -> 400
    succ_rec = EventRecord(
        operation="create",
        status="success",
        title="已成功日程",
        start_time="2026-10-15T10:00:00",
        event_json=json.dumps({"title": "已成功日程", "start_time": "2026-10-15T10:00:00"}),
    )
    session.add(succ_rec)
    session.commit()

    res_succ = client.post(f"/console/events/{succ_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert res_succ.status_code == 400
    assert "已成功写入" in res_succ.json()["error"]


def test_retry_event_rejects_non_write_phases():
    app = create_app()
    session = _create_test_session()

    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db] = _override_db(session)
    client = TestClient(app)

    # Extraction failure -> reject
    ext_rec = EventRecord(
        operation="no_event",
        status="failed",
        failure_phase="extraction",
        error_message="AI 模型超时",
    )
    session.add(ext_rec)

    # Validation failure -> reject
    val_rec = EventRecord(
        operation="no_event",
        status="failed",
        failure_phase="validation",
        error_message="缺少字段：时间",
    )
    session.add(val_rec)
    session.commit()

    res_ext = client.post(f"/console/events/{ext_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert res_ext.status_code == 400
    assert "仅支持日历写入阶段失败" in res_ext.json()["error"]

    res_val = client.post(f"/console/events/{val_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert res_val.status_code == 400
    assert "仅支持日历写入阶段失败" in res_val.json()["error"]


def test_retry_concurrency_lock_prevents_duplicate_runs():
    async def _run():
        app = create_app()
        session = _create_test_session()

        app.dependency_overrides[require_admin] = lambda: None
        app.dependency_overrides[get_db] = lambda: session

        rec = EventRecord(
            operation="create",
            status="failed",
            failure_phase="write",
            title="测试锁会议",
            event_json=json.dumps({"title": "测试锁会议", "start_time": "2026-10-15T14:00:00"}),
        )
        session.add(rec)
        session.commit()

        # Manually hold lock for rec.id
        async with _retry_mutex:
            _retry_locks.add(rec.id)

        client = TestClient(app)
        try:
            res = client.post(f"/console/events/{rec.id}/retry", headers=ORIGIN_HEADERS)
            assert res.status_code == 409
            assert "正在重试写入中" in res.json()["error"]
        finally:
            async with _retry_mutex:
                _retry_locks.discard(rec.id)

    asyncio.run(_run())


def test_retry_success_with_idempotent_caldav_uid():
    app = create_app()
    session = _create_test_session()

    settings_svc = SettingsService(session)
    settings_svc.set("caldav_url", "http://test-caldav")
    settings_svc.set("caldav_username", "admin")
    settings_svc.set("caldav_password", "pass")
    settings_svc.set("caldav_calendar_url", "http://test-caldav/calendars/work")
    settings_svc.commit()

    original_uid = "pre-assigned-uuid-12345"
    failed_rec = EventRecord(
        operation="create",
        status="failed",
        failure_phase="write",
        title="技术评审会",
        start_time="2026-10-15T16:00:00",
        caldav_uid=original_uid,
        event_json=json.dumps({
            "title": "技术评审会",
            "start_time": "2026-10-15T16:00:00",
            "end_time": "2026-10-15T17:00:00",
            "timezone": "Asia/Shanghai",
            "reminders": [{"minutes_before": 15}],
        }),
        error_message="CalDAV 服务临时不可用",
        retry_count=0,
    )
    session.add(failed_rec)
    session.commit()

    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db] = _override_db(session)
    client = TestClient(app)

    mock_create = AsyncMock(return_value={"uid": original_uid, "href": "/calendars/work/event.ics"})
    with patch("app.services.caldav_service.CalDAVService.create_event", mock_create):
        res = client.post(f"/console/events/{failed_rec.id}/retry", headers=ORIGIN_HEADERS)
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["data"]["status"] == "success"
        assert data["data"]["retry_count"] == 1
        assert data["data"]["caldav_uid"] == original_uid

        # Verify create_event was called with the exact idempotent UID
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs.get("uid") == original_uid

        # Verify DB state was updated
        session.refresh(failed_rec)
        assert failed_rec.status == "success"
        assert failed_rec.failure_phase is None
        assert failed_rec.error_message is None
        assert failed_rec.retry_count == 1
        assert failed_rec.caldav_href == "/calendars/work/event.ics"


def test_retry_failure_increments_retry_count_and_updates_error():
    app = create_app()
    session = _create_test_session()

    settings_svc = SettingsService(session)
    settings_svc.set("caldav_url", "http://test-caldav")
    settings_svc.set("caldav_username", "admin")
    settings_svc.set("caldav_password", "pass")
    settings_svc.commit()

    failed_rec = EventRecord(
        operation="create",
        status="failed",
        failure_phase="write",
        title="失败重试测试会",
        event_json=json.dumps({"title": "失败重试测试会", "start_time": "2026-10-15T16:00:00"}),
        error_message="首次写入失败",
        retry_count=1,
    )
    session.add(failed_rec)
    session.commit()

    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db] = _override_db(session)
    client = TestClient(app)

    with patch("app.services.caldav_service.CalDAVService.create_event", side_effect=CalDAVServiceError("第二次写入依然失败")):
        res = client.post(f"/console/events/{failed_rec.id}/retry", headers=ORIGIN_HEADERS)
        assert res.status_code == 400
        data = res.json()
        assert data["ok"] is False
        assert "第二次写入依然失败" in data["error"]
        assert data["retry_count"] == 2

        session.refresh(failed_rec)
        assert failed_rec.status == "failed"
        assert failed_rec.failure_phase == "write"
        assert failed_rec.retry_count == 2
        assert "第二次写入依然失败" in failed_rec.error_message


def test_wizard_test_event_end_to_end():
    app = create_app()
    session = _create_test_session()

    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db] = _override_db(session)
    client = TestClient(app)

    mock_process = AsyncMock(return_value=[("✅ 日程已安排好啦！\n📌 标题：测试向导日程", 42)])
    rec = EventRecord(
        id=42,
        source="wizard",
        operation="create",
        status="success",
        title="测试向导日程",
        start_time="2026-10-16T15:00:00",
        caldav_uid="wizard-uid-123",
    )
    session.add(rec)
    session.commit()

    with patch("app.channels.message_processor.MessageProcessor.process", mock_process):
        res = client.post("/console/wizard/test-event", json={"text": "明天下午3点测试向导日程"}, headers=ORIGIN_HEADERS)
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["data"]["record_id"] == 42
        assert "测试向导日程" in data["data"]["title"]
        assert data["data"]["source"] == "wizard"
        mock_process.assert_called_once()
        data = res.json()
        assert data["ok"] is True
        assert data["data"]["record_id"] == 42
        assert "测试向导日程" in data["data"]["title"]
        assert data["data"]["source"] == "wizard"
        mock_process.assert_called_once()
