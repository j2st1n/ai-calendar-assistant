from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.models import Base


engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_event_records()
    _migrate_passkey_credentials()


def _migrate_event_records() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("event_records"):
        return
    columns = {column["name"] for column in inspector.get_columns("event_records")}
    with engine.begin() as conn:
        if "source_user_id" not in columns:
            conn.execute(text("ALTER TABLE event_records ADD COLUMN source_user_id VARCHAR(64)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_event_records_source_user_id ON event_records (source_user_id)"))
        if "conversation_id" not in columns:
            conn.execute(text("ALTER TABLE event_records ADD COLUMN conversation_id VARCHAR(128)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_event_records_conversation_id ON event_records (conversation_id)"))
        if "event_id" not in columns:
            conn.execute(text("ALTER TABLE event_records ADD COLUMN event_id VARCHAR(64)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_event_records_event_id ON event_records (event_id)"))
        if "config_version" not in columns:
            conn.execute(text("ALTER TABLE event_records ADD COLUMN config_version INTEGER"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_event_records_config_version ON event_records (config_version)"))
        if "ai_config_hash" not in columns:
            conn.execute(text("ALTER TABLE event_records ADD COLUMN ai_config_hash VARCHAR(64)"))
        if "caldav_config_hash" not in columns:
            conn.execute(text("ALTER TABLE event_records ADD COLUMN caldav_config_hash VARCHAR(64)"))
        if "failure_phase" not in columns:
            conn.execute(text("ALTER TABLE event_records ADD COLUMN failure_phase VARCHAR(50)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_event_records_failure_phase ON event_records (failure_phase)"))
            conn.execute(text(
                "UPDATE event_records SET failure_phase = 'write' "
                "WHERE status = 'failed' AND failure_phase IS NULL AND operation IN ('create', 'update', 'delete')"
            ))
            if "error_message" in columns:
                conn.execute(text(
                    "UPDATE event_records SET failure_phase = 'validation' "
                    "WHERE status = 'failed' AND failure_phase IS NULL AND (operation = 'quote_not_found' OR error_message LIKE '缺少字段%' OR error_message LIKE '不支持%')"
                ))
            else:
                conn.execute(text(
                    "UPDATE event_records SET failure_phase = 'validation' "
                    "WHERE status = 'failed' AND failure_phase IS NULL AND operation = 'quote_not_found'"
                ))
            conn.execute(text(
                "UPDATE event_records SET failure_phase = 'extraction' "
                "WHERE status = 'failed' AND failure_phase IS NULL"
            ))
        if "retry_count" not in columns:
            conn.execute(text("ALTER TABLE event_records ADD COLUMN retry_count INTEGER DEFAULT 0"))
            conn.execute(text("UPDATE event_records SET retry_count = 0 WHERE retry_count IS NULL"))
        if "telegram_user_id" in columns:
            conn.execute(text("UPDATE event_records SET source_user_id = telegram_user_id WHERE source_user_id IS NULL"))
        if "bot_message_id" in columns:
            conn.execute(text(
                "UPDATE event_records SET bot_message_id = NULL "
                "WHERE source = 'wechat' AND bot_message_id IS NOT NULL "
                "AND (bot_message_id = '' OR bot_message_id GLOB '*[^0-9]*')"
            ))


def _migrate_passkey_credentials() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("passkey_credentials"):
        return
    columns = {column["name"] for column in inspector.get_columns("passkey_credentials")}
    if "transports" not in columns:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE passkey_credentials "
                "ADD COLUMN transports TEXT NOT NULL DEFAULT '[]'"
            ))
