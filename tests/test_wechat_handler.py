import asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.channels.wechat_handler import dispatch_wechat_message, text_items_from_message, wechat_context_from_message
from app.db.models import Base


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _message(text="Hi"):
    return {
        "message_id": 7466869436612690000,
        "from_user_id": "u@im.wechat",
        "context_token": "ctx",
        "parent_id": 0,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


def test_wechat_context_uses_user_conversation_and_string_ids():
    ctx = wechat_context_from_message(_message())
    assert ctx.source == "wechat"
    assert ctx.source_user_id == "u@im.wechat"
    assert ctx.conversation_id == "u@im.wechat"
    assert ctx.source_message_id == "7466869436612690000"
    assert ctx.reply_to_message_id is None


def test_wechat_context_uses_parent_id_as_reply():
    msg = _message()
    msg["parent_id"] = 123
    ctx = wechat_context_from_message(msg)
    assert ctx.reply_to_message_id == "123"


def test_text_items_from_message_extracts_text_only():
    msg = _message("  hello  ")
    msg["item_list"].append({"type": 2, "image_item": {"url": "x"}})
    assert text_items_from_message(msg) == ["hello"]


def test_dispatch_handles_command_and_sends_reply():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-1"})
        session = _session()

        replies = await dispatch_wechat_message(_message("/status"), session, client)

        assert str(replies[0]["text"]).startswith("🤖 主模型")
        client.send_message.assert_awaited_once()
        assert client.send_message.await_args is not None
        args = client.send_message.await_args.args
        assert args[0] == "u@im.wechat"
        assert args[2] == "ctx"

    asyncio.run(run())


def test_dispatch_uses_message_processor_for_plain_text():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"msg": {"id": "bot-2"}})
        session = _session()

        with patch("app.channels.wechat_handler.MessageProcessor") as MockProcessor:
            MockProcessor.return_value.process = AsyncMock(return_value=[("ok", None)])
            replies = await dispatch_wechat_message(_message("明天三点开会"), session, client)

        assert replies == [{"text": "ok", "record_id": None, "bot_message_id": "bot-2"}]
        MockProcessor.return_value.process.assert_awaited_once()
        assert MockProcessor.return_value.process.await_args is not None
        args = MockProcessor.return_value.process.await_args.args
        kwargs = MockProcessor.return_value.process.await_args.kwargs
        assert args[1] == "u@im.wechat"
        assert kwargs["source"] == "wechat"
        assert kwargs["conversation_id"] == "u@im.wechat"
        assert kwargs["source_message_id"] == "7466869436612690000"

    asyncio.run(run())
