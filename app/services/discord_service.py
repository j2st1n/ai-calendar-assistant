from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

from app.channels.discord_handler import register_handlers
from app.db.session import SessionLocal
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

_discord_runtime: "DiscordBotRuntime | None" = None


def get_discord_bot_runtime():
    global _discord_runtime
    return _discord_runtime


class DiscordBotRuntime:
    def __init__(self) -> None:
        self._client = None
        self._task = None
        self.running = False
        self._last_error = ""

    async def reload(self, token: str) -> str:
        import discord

        old_task = self._task
        if old_task is not None and not old_task.done():
            old_task.cancel()
            await asyncio.sleep(1.5)

        self._task = None
        self._client = None
        self.running = False
        self._last_error = ""

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        register_handlers(client)
        self._client = client
        self.running = True

        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._start_client(client, token))
        return "started"

    async def _start_client(self, client, token: str) -> None:
        import discord

        try:
            await client.start(token)
        except asyncio.CancelledError:
            self.running = False
        except discord.LoginFailure as exc:
            self.running = False
            self._last_error = str(exc)
        except Exception as exc:
            self.running = False
            self._last_error = str(exc)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._client:
            await self._client.close()
            self._client = None


class DiscordService:
    def config_summary(self, session: Session) -> dict:
        settings_service = SettingsService(session)
        token = settings_service.get("discord_bot_token")
        token_masked = settings_service.get_masked("discord_bot_token")
        bot_running = _discord_runtime is not None and _discord_runtime.running
        bot_error = _discord_runtime._last_error if _discord_runtime else ""
        application_id = settings_service.get("discord_application_id") or ""

        from app.db.models import DiscordIdentity
        from sqlalchemy import select as sa_select
        identities = session.scalars(
            sa_select(DiscordIdentity).where(DiscordIdentity.enabled.is_(True))
        ).all()
        allowed = [
            {"id": ident.id, "user_id": ident.discord_user_id, "username": ident.username or ""}
            for ident in identities
        ]

        return {
            "discord_token_masked": token_masked,
            "discord_token_set": bool(token),
            "discord_bot_running": bot_running,
            "discord_bot_error": bot_error,
            "discord_application_id": application_id,
            "discord_allowed_users": allowed,
        }

    def save_token(self, session: Session, token: str, application_id: str = "") -> None:
        settings_service = SettingsService(session)
        settings_service.set("discord_bot_token", token, encrypted=True)
        if application_id:
            settings_service.set("discord_application_id", application_id)
        settings_service.commit()

    async def reload_bot(self, token: str) -> str:
        global _discord_runtime
        if _discord_runtime is None:
            _discord_runtime = DiscordBotRuntime()
        return await _discord_runtime.reload(token)

    async def stop_bot(self) -> None:
        global _discord_runtime
        if _discord_runtime:
            await _discord_runtime.stop()
            _discord_runtime = None

    def is_user_allowed(self, session: Session, user_id: str) -> bool:
        from app.db.models import DiscordIdentity
        from sqlalchemy import select
        row = session.scalar(
            select(DiscordIdentity).where(
                DiscordIdentity.discord_user_id == user_id,
                DiscordIdentity.enabled.is_(True),
            )
        )
        return row is not None

    def has_any_user(self, session: Session) -> bool:
        from app.db.models import DiscordIdentity
        from sqlalchemy import func, select
        count = session.scalar(
            select(func.count()).select_from(DiscordIdentity).where(
                DiscordIdentity.enabled.is_(True),
            )
        )
        return (count or 0) > 0

    def auto_register(self, session: Session, user_id: str, username: str = "") -> None:
        from app.db.models import DiscordIdentity
        session.add(DiscordIdentity(
            discord_user_id=user_id,
            username=username or "",
            enabled=True,
        ))
        session.commit()
