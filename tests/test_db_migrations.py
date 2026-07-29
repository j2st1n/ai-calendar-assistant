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
