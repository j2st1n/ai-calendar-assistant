import asyncio
from unittest.mock import AsyncMock, call, patch

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.channels.wechat_handler import (
    dispatch_wechat_message,
    quoted_text_from_message,
    text_items_from_message,
    wechat_context_from_message,
)
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


def test_dispatch_starts_and_cancels_typing():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-1"})
        client.get_typing_ticket = AsyncMock(return_value="ticket-abc")
        client.send_typing = AsyncMock(return_value={})
        session = _session()

        replies = await dispatch_wechat_message(_message("/status"), session, client)

        assert str(replies[0]["text"]).startswith("🤖 主模型")
        client.get_typing_ticket.assert_awaited_once_with("u@im.wechat")
        client.send_typing.assert_has_awaits([
            call("u@im.wechat", "ticket-abc", 1),
            call("u@im.wechat", "ticket-abc", 2),
        ])
        assert client.send_typing.await_count == 2

    asyncio.run(run())


def test_dispatch_typing_failure_does_not_block_processing():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-1"})
        client.get_typing_ticket = AsyncMock(side_effect=RuntimeError("network error"))
        session = _session()

        replies = await dispatch_wechat_message(_message("/status"), session, client)

        assert str(replies[0]["text"]).startswith("🤖 主模型")
        client.send_message.assert_awaited_once()
        client.get_typing_ticket.assert_awaited_once()
        client.send_typing.assert_not_awaited()

    asyncio.run(run())


def test_dispatch_cancel_typing_in_finally_even_on_processor_error():
    async def run():
        client = AsyncMock()
        client.get_typing_ticket = AsyncMock(return_value="ticket-abc")
        client.send_typing = AsyncMock(return_value={})
        session = _session()

        with patch("app.channels.wechat_handler.MessageProcessor") as MockProcessor:
            MockProcessor.return_value.process = AsyncMock(side_effect=ValueError("processor error"))
            with pytest.raises(ValueError, match="processor error"):
                _ = await dispatch_wechat_message(_message("明天三点开会"), session, client)

        client.send_typing.assert_awaited_with("u@im.wechat", "ticket-abc", 2)

    asyncio.run(run())


def _quoted_message(user_text="改成4点"):
    return {
        "message_id": 7466869436612690000,
        "from_user_id": "u@im.wechat",
        "context_token": "ctx",
        "parent_id": 0,
        "root_id": 0,
        "item_list": [
            {
                "type": 1,
                "text_item": {"text": user_text},
                "ref_msg": {
                    "message_item": {
                        "text_item": {
                            "text": "✅ 日程已安排好啦！\n\n📌 标题：测试\n🕒 时间：2026-06-02 15:00 - 16:00\n⏰ 提醒：提前 15 分钟",
                        }
                    }
                },
            }
        ],
    }


def test_quoted_text_from_message_extracts_ref_msg_text():
    text = quoted_text_from_message(_quoted_message())
    assert text is not None
    assert "📌 标题：测试" in text
    assert "🕒 时间：2026-06-02 15:00 - 16:00" in text


def test_quoted_text_from_message_returns_none_on_no_ref_msg():
    assert quoted_text_from_message(_message("hello")) is None


def test_quoted_text_from_message_returns_none_on_empty_item_list():
    msg = _message("hello")
    msg["item_list"] = []
    assert quoted_text_from_message(msg) is None


def test_quoted_text_from_message_returns_none_on_missing_item_list():
    assert quoted_text_from_message({"parent_id": 0}) is None


def test_wechat_context_sets_quoted_text_when_ref_msg_present():
    ctx = wechat_context_from_message(_quoted_message())
    assert ctx.quoted_text is not None
    assert "测试" in ctx.quoted_text
    assert ctx.reply_to_message_id is None  # parent_id=0 means no reply_to


def test_wechat_context_quoted_text_none_when_no_ref_msg():
    ctx = wechat_context_from_message(_message("hello"))
    assert ctx.quoted_text is None
