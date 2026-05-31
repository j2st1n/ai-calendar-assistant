import asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels.message_processor import ChannelContext, _handle_new
from app.db.models import Base, EventRecord
from app.services.settings_service import SettingsService


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _ctx():
    return ChannelContext(source="telegram", source_user_id="u1", conversation_id="c1", source_message_id="m1")


def _caldav(**overrides):
    cfg = {"url": "", "user": "", "pw": "", "cal": "", "rem": 15, "dur": 60}
    cfg.update(overrides)
    return cfg


def _event():
    return CalendarEvent(title="买药", start_time="2026-06-01T12:00:00+08:00")


def test_handle_new_system_error_uses_system_message_and_records_error():
    async def run():
        session = _session()
        result = ExtractionResult(
            intent=Intent.no_event,
            missing_fields=["JSON parse failed"],
            error_type="parse_error",
        )

        replies = await _handle_new(session, _ctx(), "坏输入", result, _caldav(), SettingsService(session))

        assert replies == [("⚠️ 系统处理失败，请稍后重试。", None)]
        record = session.query(EventRecord).one()
        assert record.status == "failed"
        assert record.error_message == "JSON parse failed"

    asyncio.run(run())


def test_handle_new_caldav_not_configured_keeps_success_message():
    async def run():
        session = _session()
        result = ExtractionResult(intent=Intent.create_event, event=_event())

        replies = await _handle_new(session, _ctx(), "买药", result, _caldav(), SettingsService(session))

        assert replies[0][0].startswith("✅ 日程已安排好啦！")
        record = session.query(EventRecord).one()
        assert record.status == "success"
        assert record.caldav_uid is None

    asyncio.run(run())


def test_handle_new_caldav_write_failure_uses_warning_message(monkeypatch):
    async def run():
        async def fail_caldav(*_args, **_kwargs):
            from app.services.caldav_service import CalDAVServiceError
            raise CalDAVServiceError("创建事件失败")

        import app.channels.message_processor as mp

        monkeypatch.setattr(mp, "_write_caldav", fail_caldav)
        session = _session()
        result = ExtractionResult(intent=Intent.create_event, event=_event())

        replies = await _handle_new(session, _ctx(), "买药", result, _caldav(url="https://caldav.example", user="u"), SettingsService(session))

        assert replies[0][0].startswith("⚠️ 日程已记录到本地，但日历同步失败")
        record = session.query(EventRecord).one()
        assert record.status == "failed"
        assert record.error_message == "创建事件失败"

    asyncio.run(run())


def test_handle_new_schema_error_with_create_intent_uses_system_message():
    async def run():
        session = _session()
        result = ExtractionResult(
            intent=Intent.create_event,
            missing_fields=["1 validation error for ExtractionResult"],
            error_type="schema_error",
        )

        replies = await _handle_new(session, _ctx(), "买药", result, _caldav(), SettingsService(session))

        assert replies == [("⚠️ 系统处理失败，请稍后重试。", None)]
        record = session.query(EventRecord).one()
        assert record.status == "failed"
        assert record.error_message == "1 validation error for ExtractionResult"

    asyncio.run(run())
