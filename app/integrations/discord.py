from __future__ import annotations

import base64
import importlib
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import AsyncContextManager, Protocol, TypeVar, cast

from sqlalchemy.orm import Session

from app.channels.commands import handle_command
from app.channels.message_bindings import bind_bot_message
from app.channels.message_processor import ChannelContext, MessageProcessor
from app.db.session import SessionLocal
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

TFunc = TypeVar("TFunc", bound=Callable[..., Awaitable[None]])


class DiscordUserProtocol(Protocol):
    id: object
    bot: bool


class DiscordReferenceProtocol(Protocol):
    message_id: object


class DiscordGuildProtocol(Protocol):
    me: object


class DiscordAttachmentProtocol(Protocol):
    content_type: str | None
    url: str


class DiscordSentMessageProtocol(Protocol):
    id: object


class DiscordChannelProtocol(Protocol):
    id: object


    def send(self, content: str) -> Awaitable[DiscordSentMessageProtocol]: ...

    def typing(self) -> AsyncContextManager[object]: ...


class DiscordMessageProtocol(Protocol):
    id: object
    author: DiscordUserProtocol
    channel: DiscordChannelProtocol
    guild: DiscordGuildProtocol | None
    mentions: Sequence[object]
    content: str | None
    reference: DiscordReferenceProtocol | None
    attachments: Sequence[DiscordAttachmentProtocol]

    def reply(self, content: str) -> Awaitable[DiscordSentMessageProtocol]: ...


class DiscordResponseProtocol(Protocol):
    def send_message(self, content: str, *, ephemeral: bool = False) -> Awaitable[object]: ...


class DiscordFollowupProtocol(Protocol):
    def send(self, content: str, *, wait: bool) -> Awaitable[DiscordSentMessageProtocol]: ...


class DiscordInteractionProtocol(Protocol):
    id: object
    channel_id: object
    user: DiscordUserProtocol
    response: DiscordResponseProtocol
    followup: DiscordFollowupProtocol

    def original_response(self) -> Awaitable[DiscordSentMessageProtocol]: ...


class DiscordCommandTreeProtocol(Protocol):
    def command(self, *, name: str, description: str) -> Callable[[TFunc], TFunc]: ...

    def copy_global_to(self, *, guild: object) -> None: ...

    def sync(self, *, guild: object) -> Awaitable[object]: ...


class DiscordAppCommandsProtocol(Protocol):
    def CommandTree(self, client: "DiscordClientProtocol") -> DiscordCommandTreeProtocol: ...

    def describe(self, **kwargs: str) -> Callable[[TFunc], TFunc]: ...


class DiscordClientProtocol(Protocol):
    user: object
    guilds: Sequence[object]

    def event(self, handler: TFunc) -> TFunc: ...


class DiscordModuleProtocol(Protocol):
    app_commands: DiscordAppCommandsProtocol
    DMChannel: type[object]
    Thread: type[object]


class AioHTTPResponseProtocol(Protocol):
    def read(self) -> Awaitable[bytes]: ...


class AioHTTPSessionProtocol(Protocol):
    def get(self, url: str) -> AsyncContextManager[AioHTTPResponseProtocol]: ...


class AioHTTPModuleProtocol(Protocol):
    def ClientSession(self) -> AsyncContextManager[AioHTTPSessionProtocol]: ...


def register_handlers(client: DiscordClientProtocol) -> None:
    discord = cast(DiscordModuleProtocol, cast(object, importlib.import_module("discord")))
    tree = discord.app_commands.CommandTree(client)

    @tree.command(name="help", description="查看使用帮助")
    async def slash_help(interaction: DiscordInteractionProtocol) -> None:
        await _handle_slash_command(interaction, "/help")

    @tree.command(name="upcoming", description="查看未来日程")
    @discord.app_commands.describe(days="未来天数，最多 14 天")
    async def slash_upcoming(interaction: DiscordInteractionProtocol, days: int = 7) -> None:
        await _handle_slash_command(interaction, f"/upcoming {days}")

    @tree.command(name="latest", description="查看最近一条日程")
    async def slash_latest(interaction: DiscordInteractionProtocol) -> None:
        await _handle_slash_command(interaction, "/latest")

    @tree.command(name="status", description="查看配置状态")
    async def slash_status(interaction: DiscordInteractionProtocol) -> None:
        await _handle_slash_command(interaction, "/status")

    @client.event
    async def on_ready() -> None:
        logger.info(f"Discord bot logged in as {client.user}")
        if _commands_synced(client):
            return
        try:
            for guild in client.guilds:
                tree.copy_global_to(guild=guild)
                _ = await tree.sync(guild=guild)
            _mark_commands_synced(client)
            logger.info("Discord slash commands synced")
        except Exception:
            logger.exception("Discord slash command sync failed")

    @client.event
    async def on_message(message: DiscordMessageProtocol) -> None:
        if message.author.bot:
            return
        channel_types = (discord.DMChannel, discord.Thread)
        is_private = isinstance(message.channel, channel_types)
        if message.guild is not None and not is_private and message.guild.me not in message.mentions:
            return
        user_id = str(message.author.id)

        with SessionLocal() as session:
            from app.db.models import DiscordIdentity
            if session.query(DiscordIdentity).filter_by(discord_user_id=user_id, enabled=True).first() is None:
                _ = await message.channel.send(
                    f"你没有权限使用此 Bot。你的 Discord user_id 是：`{user_id}`\n请联系管理员在控制台中添加。"
                )
                return

            try:
                async with message.channel.typing():
                    text = re.sub(r'<@[!&]?\d+>', '', message.content or "").strip()
                    ctx = ChannelContext(
                        "discord",
                        user_id,
                        str(message.channel.id),
                        str(message.id),
                        str(message.reference.message_id) if message.reference else None,
                    )
                    command_replies = await handle_command(session, ctx, text)
                    if command_replies is not None:
                        await _send_discord_replies(message, session, command_replies)
                        return

                    if message.attachments:
                        text = await _handle_attachments(message, text, session)

                    if not text.strip():
                        _ = await message.reply("🤔 未识别到日程信息，请补充时间和事件内容。")
                        return

                    replies = await MessageProcessor().process(
                        session,
                        user_id,
                        text,
                        ctx.reply_to_message_id,
                        source="discord",
                        conversation_id=ctx.conversation_id,
                        source_message_id=ctx.source_message_id,
                    )
                    await _send_discord_replies(message, session, replies)
            except Exception as exc:
                logger.exception("Discord message processing failed")
                _ = await message.reply(f"处理消息时出错：{exc}")

    _ = (slash_help, slash_upcoming, slash_latest, slash_status, on_ready, on_message)


async def _send_discord_replies(message: DiscordMessageProtocol, session: Session,
                                replies: list[tuple[str, int | None]]) -> None:
    for response, record_id in replies:
        sent = await message.reply(response)
        bind_bot_message(session, record_id, str(sent.id))
    session.commit()


async def _handle_slash_command(interaction: DiscordInteractionProtocol, command_text: str) -> None:
    user_id = str(interaction.user.id)
    with SessionLocal() as session:
        from app.db.models import DiscordIdentity
        if session.query(DiscordIdentity).filter_by(discord_user_id=user_id, enabled=True).first() is None:
            _ = await interaction.response.send_message(
                f"你没有权限使用此 Bot。你的 Discord user_id 是：`{user_id}`\n请联系管理员在控制台中添加。",
                ephemeral=True,
            )
            return

        ctx = ChannelContext(
            "discord",
            user_id,
            str(interaction.channel_id),
            str(interaction.id),
            None,
        )
        replies = await handle_command(session, ctx, command_text)
        await _send_interaction_replies(interaction, session, replies or [])


async def _send_interaction_replies(interaction: DiscordInteractionProtocol, session: Session,
                                    replies: list[tuple[str, int | None]]) -> None:
    first = True
    for response, record_id in replies:
        if first:
            _ = await interaction.response.send_message(response)
            sent = await interaction.original_response()
            first = False
        else:
            sent = await interaction.followup.send(response, wait=True)
        bind_bot_message(session, record_id, str(sent.id))
    session.commit()


async def _handle_attachments(message: DiscordMessageProtocol, text: str, session: Session) -> str:
    aiohttp = cast(AioHTTPModuleProtocol, cast(object, importlib.import_module("aiohttp")))
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
            _ = await message.reply(f"图片下载失败：{exc}")
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
                _ = await message.reply("📸 未配置识图模型，请先在控制台 AI 设置中配置。")
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
            _ = await message.reply(f"图片识别失败：{exc}")

    return text


def _commands_synced(client: DiscordClientProtocol) -> bool:
    return bool(getattr(client, "_ai_calendar_commands_synced", False))


def _mark_commands_synced(client: DiscordClientProtocol) -> None:
    setattr(client, "_ai_calendar_commands_synced", True)
