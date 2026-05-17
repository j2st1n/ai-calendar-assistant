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
        conn.execute(text("UPDATE event_records SET source_user_id = telegram_user_id WHERE source_user_id IS NULL"))
