from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Protocol, cast

from sqlalchemy.orm import Session

from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

_discord_runtime: "DiscordBotRuntime | None" = None


class DiscordIntentsProtocol(Protocol):
    message_content: bool


class DiscordIntentsFactory(Protocol):
    def default(self) -> DiscordIntentsProtocol: ...


class DiscordClientProtocol(Protocol):
    async def start(self, token: str) -> None: ...

    async def close(self) -> None: ...


class DiscordClientFactory(Protocol):
    def __call__(self, *, intents: DiscordIntentsProtocol) -> DiscordClientProtocol: ...


class DiscordIntegrationProtocol(Protocol):
    def register_handlers(self, client: DiscordClientProtocol) -> None: ...


def get_discord_bot_runtime() -> "DiscordBotRuntime | None":
    global _discord_runtime
    return _discord_runtime


class DiscordBotRuntime:
    _client: DiscordClientProtocol | None
    _task: asyncio.Task[None] | None
    running: bool
    _last_error: str

    def __init__(self) -> None:
        self._client = None
        self._task = None
        self.running = False
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    async def reload(self, token: str) -> str:
        discord = importlib.import_module("discord")
        discord_integration = cast(
            DiscordIntegrationProtocol,
            cast(object, importlib.import_module("app.integrations.discord")),
        )

        old_task = self._task
        if old_task is not None and not old_task.done():
            _ = old_task.cancel()
            await asyncio.sleep(1.5)

        self._task = None
        self._client = None
        self.running = False
        self._last_error = ""

        intents_factory = cast(DiscordIntentsFactory, getattr(discord, "Intents"))
        client_factory = cast(DiscordClientFactory, getattr(discord, "Client"))
        intents = intents_factory.default()
        intents.message_content = True
        client = client_factory(intents=intents)
        discord_integration.register_handlers(client)
        self._client = client
        self.running = True

        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._start_client(client, token))
        logger.info("Discord bot start task created")
        return "started"

    async def _start_client(self, client: DiscordClientProtocol, token: str) -> None:
        discord = importlib.import_module("discord")
        login_failure = cast(type[Exception], getattr(discord, "LoginFailure"))

        try:
            await client.start(token)
        except asyncio.CancelledError:
            self.running = False
        except login_failure as exc:
            self.running = False
            self._last_error = str(exc)
        except Exception as exc:
            self.running = False
            self._last_error = str(exc)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            _ = self._task.cancel()
            self._task = None
        if self._client:
            await self._client.close()
            self._client = None


class DiscordService:
    def config_summary(self, session: Session) -> dict[str, object]:
        settings_service = SettingsService(session)
        token = settings_service.get("discord_bot_token")
        token_masked = settings_service.get_masked("discord_bot_token")
        bot_running = _discord_runtime is not None and _discord_runtime.running
        bot_error = _discord_runtime.last_error if _discord_runtime else ""
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
