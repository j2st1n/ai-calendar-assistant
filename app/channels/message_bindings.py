from sqlalchemy.orm import Session

from app.db.models import EventRecord


def bind_bot_message(session: Session, record_id: int | None, bot_message_id: str) -> None:
    if not record_id:
        return
    rec = session.get(EventRecord, record_id)
    if rec:
        rec.bot_message_id = bot_message_id
