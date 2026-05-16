from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
import re
from datetime import date as dt_date, timedelta

from app.channels.message_processor import MessageProcessor
from app.db.session import SessionLocal
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)


def register_handlers(client) -> None:
    @client.event
    async def on_ready():
        logger.info(f"Discord bot logged in as {client.user}")

    @client.event
    async def on_message(message):
        if message.author.bot:
            return
        import discord as _discord
        is_private = isinstance(message.channel, (_discord.DMChannel, _discord.Thread))
        if message.guild is not None and not is_private and message.guild.me not in message.mentions:
            return
        user_id = str(message.author.id)

        with SessionLocal() as session:
            from app.services.discord_service import DiscordService
            service = DiscordService()
            if not service.is_user_allowed(session, user_id):
                await message.channel.send(
                    f"你没有权限使用此 Bot。你的 Discord user_id 是：`{user_id}`\n请联系管理员在控制台中添加。"
                )
                return

            try:
                async with message.channel.typing():
                    text = re.sub(r'<@[!&]?\d+>', '', message.content or "").strip()
                    if message.attachments:
                        text = await _handle_attachments(message, text, session)

                    if not text.strip():
                        await message.reply("🤔 未识别到日程信息，请补充时间和事件内容。")
                        return

                    processor = MessageProcessor()
                    replies = await processor.process(session, user_id, text)
                    for response, _ in replies:
                        await message.reply(response)
            except Exception as exc:
                logger.exception("Discord message processing failed")
                await message.reply(f"处理消息时出错：{exc}")


async def _handle_attachments(message, text: str, session) -> str:
    import aiohttp
    settings_service = SettingsService(session)
    use_main = settings_service.get("ai_vision_use_main") or "true"

    for att in message.attachments:
        if not att.content_type or not att.content_type.startswith("image/"):
            continue
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(att.url) as resp:
                    img_bytes = await resp.read()
        except Exception as exc:
            await message.reply(f"图片下载失败：{exc}")
            continue

        img_b64 = base64.b64encode(img_bytes).decode()
        from app.services.ai_provider_service import AIProviderConfig, AIProviderService as AISvc

        if use_main != "false":
            config = AIProviderConfig(
                provider_type=settings_service.get("ai_provider_type") or "openai_compatible",
                base_url=settings_service.get("ai_base_url") or "https://api.openai.com/v1",
                api_key=settings_service.get("ai_api_key"),
                model=settings_service.get("ai_model"),
            )
        else:
            if not settings_service.get("ai_vision_model"):
                await message.reply("📸 未配置识图模型，请先在控制台 AI 设置中配置。")
                return text
            config = AIProviderConfig(
                provider_type=settings_service.get("ai_vision_provider_type") or "openai_compatible",
                base_url=settings_service.get("ai_vision_base_url") or "https://api.openai.com/v1",
                api_key=settings_service.get("ai_vision_api_key"),
                model=settings_service.get("ai_vision_model"),
            )
        try:
            result = await AISvc().vision_completion(config, img_b64)
            text = f"{text}\n{result}" if text.strip() else result
        except Exception as exc:
            await message.reply(f"图片识别失败：{exc}")

    return text
