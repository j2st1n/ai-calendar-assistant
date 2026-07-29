from __future__ import annotations

import logging
import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
WECHAT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

from app.channels.commands import handle_command
from app.channels.message_bindings import bind_bot_message
from app.channels.message_processor import ChannelContext, MessageProcessor
from app.integrations.ilink import ILinkClient
from app.services.ai_provider_service import AIProviderConfig, AIProviderService
from app.services.settings_service import SettingsService


@dataclass(frozen=True)
class WeChatImageSource:
    url: str | None = None
    encrypted_query_param: str | None = None
    aes_key: bytes | None = None


def quoted_text_from_message(message: dict[str, Any]) -> str | None:
    """Extract the first non-empty ref_msg quote text from a message payload.

    WeChat quoted messages have parent_id=0, root_id=0, but
    a nested ref_msg contains the bot's original reply text.
    """
    def find_ref_text(value: object) -> str | None:
        if isinstance(value, dict):
            ref_msg = value.get("ref_msg")
            if isinstance(ref_msg, dict):
                text = _text_from_message_item(ref_msg)
                if text:
                    return text
            for nested in value.values():
                text = find_ref_text(nested)
                if text:
                    return text
        elif isinstance(value, list):
            for nested in value:
                text = find_ref_text(nested)
                if text:
                    return text
        return None

    return find_ref_text(message)


def has_quoted_reference(message: dict[str, Any]) -> bool:
    def find_ref(value: object) -> bool:
        if isinstance(value, dict):
            if isinstance(value.get("ref_msg"), dict):
                return True
            return any(find_ref(nested) for nested in value.values())
        if isinstance(value, list):
            return any(find_ref(nested) for nested in value)
        return False

    return find_ref(message)


def _text_from_message_item(message_item: dict[str, Any]) -> str | None:
    text_item = message_item.get("text_item")
    if isinstance(text_item, dict):
        text = text_item.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()

    for value in message_item.values():
        if isinstance(value, dict):
            text = _text_from_message_item(value)
            if text:
                return text
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                text = _text_from_message_item(item)
                if text:
                    return text
    return None


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
        quoted_text=quoted_text_from_message(message),
        quote_reference_present=has_quoted_reference(message),
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


def image_urls_from_message(message: dict[str, Any]) -> list[str]:
    return [source.url for source in image_sources_from_message(message) if source.url]


def image_sources_from_message(message: dict[str, Any]) -> list[WeChatImageSource]:
    sources: list[WeChatImageSource] = []
    item_list = message.get("item_list")
    if not isinstance(item_list, list):
        return sources
    for item in item_list:
        if not isinstance(item, dict) or item.get("type") != 2:
            continue
        image_item = item.get("image_item")
        if not isinstance(image_item, dict):
            continue
        sources.extend(WeChatImageSource(url=url) for url in _urls_from_image_item(image_item))
        media_source = _media_source_from_image_item(image_item)
        if media_source is not None:
            sources.append(media_source)
    return sources


def has_image_items(message: dict[str, Any]) -> bool:
    item_list = message.get("item_list")
    if not isinstance(item_list, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == 2 for item in item_list)


def _urls_from_image_item(image_item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("url", "image_url", "cdn_url", "download_url", "file_url", "thumb_url", "preview_url"):
        value = image_item.get(key)
        if isinstance(value, str) and value.strip():
            urls.append(value.strip())
    for key in ("file", "image", "origin", "thumb", "preview"):
        value = image_item.get(key)
        if isinstance(value, dict):
            urls.extend(_urls_from_image_item(value))
    return urls


def _media_source_from_image_item(image_item: dict[str, Any]) -> WeChatImageSource | None:
    media = image_item.get("media")
    if not isinstance(media, dict):
        return None
    encrypted_query_param = media.get("encrypt_query_param")
    if not isinstance(encrypted_query_param, str) or not encrypted_query_param.strip():
        return None
    aes_key = _image_aes_key(image_item, media)
    if aes_key is None:
        return None
    return WeChatImageSource(encrypted_query_param=encrypted_query_param.strip(), aes_key=aes_key)


def _image_aes_key(image_item: dict[str, Any], media: dict[str, Any]) -> bytes | None:
    aeskey = image_item.get("aeskey")
    if isinstance(aeskey, str) and aeskey.strip():
        try:
            return bytes.fromhex(aeskey.strip())
        except ValueError:
            logger.debug("Invalid WeChat image aeskey hex", exc_info=True)

    aes_key = media.get("aes_key")
    if isinstance(aes_key, str) and aes_key.strip():
        return _decode_media_aes_key(aes_key.strip())
    return None


def _decode_media_aes_key(value: str) -> bytes | None:
    try:
        key = base64.b64decode(value)
    except Exception:
        logger.debug("Invalid WeChat media aes_key base64", exc_info=True)
        return None
    if len(key) == 16:
        return key
    try:
        decoded = key.decode().strip()
        if len(decoded) == 32:
            return bytes.fromhex(decoded)
    except Exception:
        logger.debug("Invalid WeChat media aes_key hex compatibility value", exc_info=True)
    return None


def _bot_message_id(response: dict[str, Any]) -> str | None:
    for key in ("message_id", "msg_id", "id"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    msg = response.get("msg")
    if isinstance(msg, dict):
        return _bot_message_id(msg)
    return None


async def _download_image_bytes(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            _ = response.raise_for_status()
            return response.content
    except Exception:
        logger.debug("Failed to download WeChat image", exc_info=True)
        return None


async def _download_wechat_image_source(source: WeChatImageSource) -> bytes | None:
    if source.url:
        return await _download_image_bytes(source.url)
    if source.encrypted_query_param and source.aes_key:
        url = f"{WECHAT_CDN_BASE_URL}/download?encrypted_query_param={quote(source.encrypted_query_param, safe='')}"
        encrypted = await _download_image_bytes(url)
        if encrypted is None:
            return None
        return _decrypt_wechat_image(encrypted, source.aes_key)
    return None


def _decrypt_wechat_image(encrypted: bytes, aes_key: bytes) -> bytes | None:
    try:
        decryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except Exception:
        logger.debug("Failed to decrypt WeChat image", exc_info=True)
        return None


def _vision_config(session: Session) -> AIProviderConfig | str:
    settings_service = SettingsService(session)
    use_main = settings_service.get("ai_vision_use_main") or "true"
    if use_main != "false":
        return AIProviderConfig(
            provider_type=settings_service.get("ai_provider_type") or "openai_compatible",
            base_url=settings_service.get("ai_base_url") or "https://api.openai.com/v1",
            api_key=settings_service.get("ai_api_key"),
            model=settings_service.get("ai_model"),
        )
    if not settings_service.get("ai_vision_model"):
        return "📸 未配置识图模型，请先在控制台 AI 设置中配置。"
    return AIProviderConfig(
        provider_type=settings_service.get("ai_vision_provider_type") or "openai_compatible",
        base_url=settings_service.get("ai_vision_base_url") or "https://api.openai.com/v1",
        api_key=settings_service.get("ai_vision_api_key"),
        model=settings_service.get("ai_vision_model"),
    )


async def _image_texts_from_message(message: dict[str, Any], session: Session) -> tuple[list[str], list[str]]:
    texts: list[str] = []
    errors: list[str] = []
    sources = image_sources_from_message(message)
    if not sources:
        if has_image_items(message):
            logger.warning("WeChat image item did not include a supported URL field: %s", message.get("item_list"))
            return texts, ["收到图片，但消息里没有可下载的图片地址，暂时无法识别。"]
        return texts, errors

    config = _vision_config(session)
    if isinstance(config, str):
        return texts, [config]

    service = AIProviderService()
    for source in sources:
        img_bytes = await _download_wechat_image_source(source)
        if img_bytes is None:
            errors.append("图片下载失败，请稍后重试。")
            continue
        img_b64 = base64.b64encode(img_bytes).decode()
        try:
            text = await service.vision_completion(config, img_b64)
        except Exception as exc:
            errors.append(f"图片识别失败：{exc}")
            continue
        if text.strip():
            texts.append(text.strip())
    return texts, errors


async def _send_wechat_replies(
    session: Session,
    client: ILinkClient,
    ctx: ChannelContext,
    context_token: str,
    replies: list[tuple[str, int | None]],
    sent: list[dict[str, object]],
) -> None:
    for response_text, record_id in replies:
        response = await client.send_message(ctx.source_user_id, response_text, context_token)
        bot_message_id = _bot_message_id(response)
        if bot_message_id:
            bind_bot_message(session, record_id, bot_message_id)
        sent.append({"text": response_text, "record_id": record_id, "bot_message_id": bot_message_id})


async def dispatch_wechat_message(message: dict[str, Any], session: Session, client: ILinkClient) -> list[dict[str, object]]:
    ctx = wechat_context_from_message(message)
    context_token = str(message.get("context_token") or "")
    if not ctx.source_user_id or not context_token:
        return [{"error": "missing from_user_id or context_token"}]

    typing_ticket: str | None = None
    try:
        typing_ticket = await client.get_typing_ticket(ctx.source_user_id)
        _ = await client.send_typing(ctx.source_user_id, typing_ticket, 1)
    except Exception:
        logger.debug("Failed to start typing indicator", exc_info=True)
        typing_ticket = None

    sent: list[dict[str, object]] = []
    try:
        image_texts, image_errors = await _image_texts_from_message(message, session)
        for error in image_errors:
            await _send_wechat_replies(session, client, ctx, context_token, [(error, None)], sent)

        texts = image_texts + text_items_from_message(message)
        for text in texts:
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
                    quoted_text=ctx.quoted_text,
                    quote_reference_present=ctx.quote_reference_present,
                )
            await _send_wechat_replies(session, client, ctx, context_token, replies, sent)
        session.commit()
    finally:
        if typing_ticket:
            try:
                _ = await client.send_typing(ctx.source_user_id, typing_ticket, 2)
            except Exception:
                logger.debug("Failed to cancel typing indicator", exc_info=True)
    return sent
