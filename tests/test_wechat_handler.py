import asyncio
import logging
from unittest.mock import AsyncMock, call, patch

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.channels.wechat_handler import (
    WECHAT_CDN_BASE_URL,
    _decrypt_wechat_image,
    dispatch_wechat_message,
    has_quoted_reference,
    has_image_items,
    image_sources_from_message,
    image_urls_from_message,
    quoted_message_id_from_message,
    quoted_text_from_message,
    text_items_from_message,
    wechat_context_from_message,
)
from app.db.models import Base
from app.services.settings_service import SettingsService


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


def test_image_urls_from_message_extracts_image_urls():
    msg = _message("hello")
    msg["item_list"].append({"type": 2, "image_item": {"url": " https://example.test/a.jpg "}})
    msg["item_list"].append({"type": 2, "image_item": {"cdn_url": "https://example.test/b.jpg"}})
    msg["item_list"].append({"type": 2, "image_item": {"file": {"download_url": "https://example.test/c.jpg"}}})

    assert image_urls_from_message(msg) == [
        "https://example.test/a.jpg",
        "https://example.test/b.jpg",
        "https://example.test/c.jpg",
    ]


def test_image_sources_from_message_extracts_encrypted_media_source():
    msg = _message("hello")
    msg["item_list"].append({
        "type": 2,
        "image_item": {
            "aeskey": "00112233445566778899aabbccddeeff",
            "media": {"encrypt_query_param": "abc+/="},
        },
    })

    sources = image_sources_from_message(msg)

    assert len(sources) == 1
    assert sources[0].encrypted_query_param == "abc+/="
    assert sources[0].aes_key == bytes.fromhex("00112233445566778899aabbccddeeff")


def test_image_urls_from_message_skips_invalid_items():
    assert image_urls_from_message({}) == []
    msg = _message("hello")
    msg["item_list"].extend([
        {"type": 1, "text_item": {"text": "x"}},
        {"type": 2},
        {"type": 2, "image_item": {}},
        {"type": 2, "image_item": {"url": ""}},
        {"type": 3, "image_item": {"url": "https://example.test/c.jpg"}},
    ])

    assert image_urls_from_message(msg) == []


def test_has_image_items_detects_type_two_items():
    assert has_image_items({}) is False
    assert has_image_items(_message("hello")) is False
    msg = _message("hello")
    msg["item_list"].append({"type": 2, "image_item": {}})
    assert has_image_items(msg) is True


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


def test_dispatch_warns_when_event_reply_has_no_message_id(caplog):
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={})
        session = _session()
        caplog.set_level(logging.WARNING)

        with patch("app.channels.wechat_handler.MessageProcessor") as MockProcessor:
            MockProcessor.return_value.process = AsyncMock(return_value=[("ok", 123)])
            replies = await dispatch_wechat_message(_message("明天三点开会"), session, client)

        assert replies == [{"text": "ok", "record_id": 123, "bot_message_id": None}]
        assert "WeChat reply message ID missing" in caplog.text
        assert "record_id=123" in caplog.text
        assert "response_keys=[]" in caplog.text

    asyncio.run(run())


def _image_message(url="https://example.test/a.jpg", text: str | None = None):
    msg = _message(text or "")
    msg["item_list"] = [{"type": 2, "image_item": {"url": url}}]
    if text is not None:
        msg["item_list"].append({"type": 1, "text_item": {"text": text}})
    return msg


def _encrypted_image_message(encrypt_query_param="abc+/="):
    msg = _message("")
    msg["item_list"] = [{
        "type": 2,
        "image_item": {
            "aeskey": "00112233445566778899aabbccddeeff",
            "media": {"encrypt_query_param": encrypt_query_param},
        },
    }]
    return msg


def _encrypt_image_bytes(data: bytes, aes_key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def test_decrypt_wechat_image_decrypts_aes_ecb_payload():
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    encrypted = _encrypt_image_bytes(b"image-bytes", key)

    assert _decrypt_wechat_image(encrypted, key) == b"image-bytes"


def test_dispatch_handles_image_with_main_model():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-img"})
        session = _session()
        settings = SettingsService(session)
        settings.set("ai_model", "main-model")
        settings.commit()

        with patch("app.channels.wechat_handler._download_image_bytes", AsyncMock(return_value=b"img")) as mock_download:
            with patch("app.channels.wechat_handler.AIProviderService") as MockAISvc:
                MockAISvc.return_value.vision_completion = AsyncMock(return_value="明天三点开会")
                with patch("app.channels.wechat_handler.MessageProcessor") as MockProcessor:
                    MockProcessor.return_value.process = AsyncMock(return_value=[("ok", None)])
                    replies = await dispatch_wechat_message(_image_message(), session, client)

        assert replies == [{"text": "ok", "record_id": None, "bot_message_id": "bot-img"}]
        mock_download.assert_awaited_once_with("https://example.test/a.jpg")
        MockAISvc.return_value.vision_completion.assert_awaited_once()
        assert MockAISvc.return_value.vision_completion.await_args is not None
        config = MockAISvc.return_value.vision_completion.await_args.args[0]
        assert config.model == "main-model"
        MockProcessor.return_value.process.assert_awaited_once()
        assert MockProcessor.return_value.process.await_args is not None
        assert MockProcessor.return_value.process.await_args.args[2] == "明天三点开会"

    asyncio.run(run())


def test_dispatch_handles_encrypted_wechat_image_media():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-img"})
        session = _session()
        settings = SettingsService(session)
        settings.set("ai_model", "main-model")
        settings.commit()
        encrypted = _encrypt_image_bytes(b"real-image", bytes.fromhex("00112233445566778899aabbccddeeff"))

        with patch("app.channels.wechat_handler._download_image_bytes", AsyncMock(return_value=encrypted)) as mock_download:
            with patch("app.channels.wechat_handler.AIProviderService") as MockAISvc:
                MockAISvc.return_value.vision_completion = AsyncMock(return_value="明天三点开会")
                with patch("app.channels.wechat_handler.MessageProcessor") as MockProcessor:
                    MockProcessor.return_value.process = AsyncMock(return_value=[("ok", None)])
                    replies = await dispatch_wechat_message(_encrypted_image_message(), session, client)

        expected_url = f"{WECHAT_CDN_BASE_URL}/download?encrypted_query_param=abc%2B%2F%3D"
        mock_download.assert_awaited_once_with(expected_url)
        assert MockAISvc.return_value.vision_completion.await_args is not None
        assert MockAISvc.return_value.vision_completion.await_args.args[1] == "cmVhbC1pbWFnZQ=="
        assert replies == [{"text": "ok", "record_id": None, "bot_message_id": "bot-img"}]

    asyncio.run(run())


def test_dispatch_handles_image_with_separate_vision_model():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-img"})
        session = _session()
        svc = SettingsService(session)
        svc.set("ai_vision_use_main", "false")
        svc.set("ai_vision_provider_type", "openai_compatible")
        svc.set("ai_vision_base_url", "https://vision.example/v1")
        svc.set("ai_vision_model", "vision-model")
        svc.commit()

        with patch("app.channels.wechat_handler._download_image_bytes", AsyncMock(return_value=b"img")):
            with patch("app.channels.wechat_handler.AIProviderService") as MockAISvc:
                MockAISvc.return_value.vision_completion = AsyncMock(return_value="明天三点开会")
                with patch("app.channels.wechat_handler.MessageProcessor") as MockProcessor:
                    MockProcessor.return_value.process = AsyncMock(return_value=[("ok", None)])
                    _ = await dispatch_wechat_message(_image_message(), session, client)

        assert MockAISvc.return_value.vision_completion.await_args is not None
        config = MockAISvc.return_value.vision_completion.await_args.args[0]
        assert config.base_url == "https://vision.example/v1"
        assert config.model == "vision-model"

    asyncio.run(run())


def test_dispatch_image_without_vision_model_sends_error():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-img"})
        session = _session()
        settings = SettingsService(session)
        settings.set("ai_vision_use_main", "false")
        settings.commit()

        with patch("app.channels.wechat_handler._download_image_bytes", AsyncMock(return_value=b"img")) as mock_download:
            replies = await dispatch_wechat_message(_image_message(), session, client)

        assert replies == [{"text": "📸 未配置识图模型，请先在控制台 AI 设置中配置。", "record_id": None, "bot_message_id": "bot-img"}]
        mock_download.assert_not_awaited()

    asyncio.run(run())


def test_dispatch_image_download_failure_sends_error():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-img"})
        session = _session()

        with patch("app.channels.wechat_handler._download_image_bytes", AsyncMock(return_value=None)):
            replies = await dispatch_wechat_message(_image_message(), session, client)

        assert replies == [{"text": "图片下载失败，请稍后重试。", "record_id": None, "bot_message_id": "bot-img"}]

    asyncio.run(run())


def test_dispatch_image_without_supported_url_sends_error():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-img"})
        session = _session()
        msg = _message("")
        msg["item_list"] = [{"type": 2, "image_item": {"file_id": "file-1"}}]

        replies = await dispatch_wechat_message(msg, session, client)

        assert replies == [{"text": "收到图片，但消息里没有可下载的图片地址，暂时无法识别。", "record_id": None, "bot_message_id": "bot-img"}]

    asyncio.run(run())


def test_dispatch_image_vision_failure_sends_error():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-img"})
        session = _session()

        with patch("app.channels.wechat_handler._download_image_bytes", AsyncMock(return_value=b"img")):
            with patch("app.channels.wechat_handler.AIProviderService") as MockAISvc:
                MockAISvc.return_value.vision_completion = AsyncMock(side_effect=RuntimeError("bad vision"))
                replies = await dispatch_wechat_message(_image_message(), session, client)

        assert replies == [{"text": "图片识别失败：bad vision", "record_id": None, "bot_message_id": "bot-img"}]

    asyncio.run(run())


def test_dispatch_image_and_text_processes_both_inputs():
    async def run():
        client = AsyncMock()
        client.send_message = AsyncMock(return_value={"message_id": "bot-img"})
        session = _session()

        with patch("app.channels.wechat_handler._download_image_bytes", AsyncMock(return_value=b"img")):
            with patch("app.channels.wechat_handler.AIProviderService") as MockAISvc:
                MockAISvc.return_value.vision_completion = AsyncMock(return_value="图片里的日程")
                with patch("app.channels.wechat_handler.MessageProcessor") as MockProcessor:
                    MockProcessor.return_value.process = AsyncMock(side_effect=[[("img ok", None)], [("text ok", None)]])
                    replies = await dispatch_wechat_message(_image_message(text="文字里的日程"), session, client)

        assert [reply["text"] for reply in replies] == ["img ok", "text ok"]
        processed_texts = [await_call.args[2] for await_call in MockProcessor.return_value.process.await_args_list]
        assert processed_texts == ["图片里的日程", "文字里的日程"]

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


def test_quoted_text_from_message_extracts_nested_ref_item_list_text():
    msg = _quoted_message()
    ref_msg = msg["item_list"][0]["ref_msg"]
    ref_msg["message_item"] = {
        "item_list": [
            {
                "type": 1,
                "text_item": {
                    "text": "✅ 日程已安排好啦！\n\n📌 标题：测试\n🕒 时间：2026-06-02 15:00 - 16:00",
                },
            }
        ]
    }

    text = quoted_text_from_message(msg)

    assert text is not None
    assert "📌 标题：测试" in text


def test_quoted_text_from_message_extracts_deeply_nested_ref_msg():
    msg = _message("删除")
    msg["item_list"][0]["text_item"]["metadata"] = {
        "wrapper": {
            "ref_msg": {
                "payload": {
                    "message_item": {
                        "item_list": [
                            {
                                "type": 1,
                                "text_item": {
                                    "text": "✅ 日程已安排好啦！\n\n📌 标题：测试\n🕒 时间：2026-06-02 15:00 - 16:00",
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    text = quoted_text_from_message(msg)

    assert text is not None
    assert "📌 标题：测试" in text


def test_quoted_text_from_message_returns_none_on_no_ref_msg():
    assert quoted_text_from_message(_message("hello")) is None


def test_wechat_context_marks_unreadable_ref_msg():
    msg = _message("删除")
    msg["item_list"][0]["ref_msg"] = {"message_item": {}}

    ctx = wechat_context_from_message(msg)

    assert has_quoted_reference(msg) is True
    assert ctx.quote_reference_present is True
    assert ctx.quoted_text is None


def test_wechat_context_uses_quoted_message_item_id_without_body():
    msg = _message("删除")
    msg["item_list"][0]["ref_msg"] = {
        "title": "引用了一条消息",
        "message_item": {
            "type": 1,
            "msg_id": "bot-message-123",
        },
    }

    ctx = wechat_context_from_message(msg)

    assert quoted_message_id_from_message(msg) == "bot-message-123"
    assert ctx.quote_reference_present is True
    assert ctx.quoted_text is None
    assert ctx.reply_to_message_id == "bot-message-123"


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
