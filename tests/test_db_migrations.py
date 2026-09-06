from sqlalchemy import create_engine, text

from app.db.models import Base
import app.db.session as db_session


def test_migration_clears_only_definitely_invalid_wechat_message_ids(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO event_records "
                "(source, operation, status, bot_message_id, is_recurring, created_at, updated_at) "
                "VALUES "
                "('wechat', 'create', 'success', '7488170983529358600', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "('wechat', 'create', 'success', 'not-a-message-id', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "('telegram', 'create', 'success', 'telegram-id', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    monkeypatch.setattr(db_session, "engine", engine)

    db_session._migrate_event_records()

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT source, bot_message_id FROM event_records ORDER BY id")
        ).all()
    assert rows == [
        ("wechat", "7488170983529358600"),
        ("wechat", None),
        ("telegram", "telegram-id"),
    ]


def test_migration_adds_config_version_and_hashes(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE event_records ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "source VARCHAR(50), "
                "operation VARCHAR(50), "
                "status VARCHAR(50), "
                "created_at TIMESTAMP, "
                "updated_at TIMESTAMP"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO event_records (source, operation, status) "
                "VALUES ('telegram', 'create', 'success')"
            )
        )
    monkeypatch.setattr(db_session, "engine", engine)

    db_session._migrate_event_records()

    from sqlalchemy import inspect
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("event_records")}
    assert "config_version" in columns
    assert "ai_config_hash" in columns
    assert "caldav_config_hash" in columns
    indexes = {idx["name"] for idx in inspector.get_indexes("event_records")}
    assert "ix_event_records_config_version" in indexes


def test_migration_adds_failure_phase_and_retry_count_and_backfills(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE event_records ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "source VARCHAR(50), "
                "operation VARCHAR(50), "
                "status VARCHAR(50), "
                "error_message TEXT, "
                "created_at TIMESTAMP, "
                "updated_at TIMESTAMP"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO event_records (source, operation, status, error_message) VALUES "
                "('wechat', 'create', 'failed', 'CalDAV connection timeout'), "
                "('telegram', 'quote_not_found', 'failed', 'quote target not found'), "
                "('wechat', 'no_event', 'failed', '缺少字段：时间'), "
                "('wechat', 'no_event', 'failed', '未识别到日程信息'), "
                "('telegram', 'create', 'success', NULL)"
            )
        )
    monkeypatch.setattr(db_session, "engine", engine)

    db_session._migrate_event_records()

    from sqlalchemy import inspect
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("event_records")}
    assert "failure_phase" in columns
    assert "retry_count" in columns
    indexes = {idx["name"] for idx in inspector.get_indexes("event_records")}
    assert "ix_event_records_failure_phase" in indexes

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT operation, status, failure_phase, retry_count FROM event_records ORDER BY id")
        ).all()
    assert rows == [
        ("create", "failed", "write", 0),
        ("quote_not_found", "failed", "validation", 0),
        ("no_event", "failed", "validation", 0),
        ("no_event", "failed", "extraction", 0),
        ("create", "success", None, 0),
    ]
