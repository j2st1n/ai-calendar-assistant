from sqlalchemy.orm import Session

from app.db.models import BotMessageBinding, EventRecord


def bind_bot_message(session: Session, record_id: int | None, bot_message_id: str,
                     *, source: str | None = None, conversation_id: str | None = None) -> None:
    if not record_id:
        return
    rec = session.get(EventRecord, record_id)
    if rec:
        if source and conversation_id:
            session.merge(BotMessageBinding(source=source, conversation_id=conversation_id,
                                           message_id=bot_message_id, record_id=record_id))
        # Legacy fallback is valid only in the event's original conversation.
        if source is None or (source == rec.source and conversation_id == rec.conversation_id):
            rec.bot_message_id = bot_message_id


def resolve_bot_message(session: Session, source: str, conversation_id: str | None,
                        message_id: str) -> EventRecord | None:
    if not conversation_id:
        return None
    binding = session.get(BotMessageBinding, (source, conversation_id, message_id))
    return session.get(EventRecord, binding.record_id) if binding else None
