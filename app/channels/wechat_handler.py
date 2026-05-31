from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.channels.commands import handle_command
from app.channels.message_bindings import bind_bot_message
from app.channels.message_processor import ChannelContext, MessageProcessor
from app.integrations.ilink import ILinkClient


def wechat_context_from_message(message: dict[str, Any]) -> ChannelContext:
    from_user_id = str(message.get("from_user_id") or "")
    message_id = message.get("message_id")
    parent_id = message.get("parent_id")
    reply_to = str(parent_id) if parent_id not in (None, 0, "0", "") else None
    return ChannelContext(
        source="wechat",
        source_user_id=from_user_id,
        conversation_id=from_user_id,
        source_message_id=str(message_id) if message_id not in (None, "") else None,
        reply_to_message_id=reply_to,
    )


def text_items_from_message(message: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    item_list = message.get("item_list")
    if not isinstance(item_list, list):
        return texts
    for item in item_list:
        if not isinstance(item, dict) or item.get("type") != 1:
            continue
        text_item = item.get("text_item")
        if isinstance(text_item, dict):
            text = text_item.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return texts


def _bot_message_id(response: dict[str, Any]) -> str | None:
    for key in ("message_id", "msg_id", "id"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    msg = response.get("msg")
    if isinstance(msg, dict):
        return _bot_message_id(msg)
    return None


async def dispatch_wechat_message(message: dict[str, Any], session: Session, client: ILinkClient) -> list[dict[str, object]]:
    ctx = wechat_context_from_message(message)
    context_token = str(message.get("context_token") or "")
    if not ctx.source_user_id or not context_token:
        return [{"error": "missing from_user_id or context_token"}]

    sent: list[dict[str, object]] = []
    for text in text_items_from_message(message):
        replies = await handle_command(session, ctx, text)
        if replies is None:
            replies = await MessageProcessor().process(
                session,
                ctx.source_user_id,
                text,
                ctx.reply_to_message_id,
                source="wechat",
                conversation_id=ctx.conversation_id,
                source_message_id=ctx.source_message_id,
            )
        for response_text, record_id in replies:
            response = await client.send_message(ctx.source_user_id, response_text, context_token)
            bot_message_id = _bot_message_id(response)
            if bot_message_id:
                bind_bot_message(session, record_id, bot_message_id)
            sent.append({"text": response_text, "record_id": record_id, "bot_message_id": bot_message_id})
    session.commit()
    return sent
