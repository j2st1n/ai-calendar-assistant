from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

_wechat_runtime: "WechatBotRuntime | None" = None

_LAZY_COMPONENTS = {
    "dispatch_wechat_message": ("app.channels.wechat_handler", "dispatch_wechat_message"),
    "ILinkAuthError": ("app.integrations.ilink", "ILinkAuthError"),
    "ILinkClient": ("app.integrations.ilink", "ILinkClient"),
    "ILinkError": ("app.integrations.ilink", "ILinkError"),
}


def _load_component(name: str) -> Any:
    existing = globals().get(name)
    if existing is not None:
        return existing
    module_name, attribute = _LAZY_COMPONENTS[name]
    value = getattr(importlib.import_module(module_name), attribute)
    globals()[name] = value
    return value


def __getattr__(name: str) -> Any:
    if name in _LAZY_COMPONENTS:
        return _load_component(name)
    raise AttributeError(name)


def get_wechat_bot_runtime() -> "WechatBotRuntime | None":
    global _wechat_runtime
    return _wechat_runtime


class WechatBotRuntime:
    _task: asyncio.Task[None] | None
    running: bool
    _last_error: str
    _poll_interval: float

    def __init__(self, poll_interval: float = 5.0) -> None:
        self._task = None
        self.running = False
        self._last_error = ""
        self._poll_interval = poll_interval

    @property
    def last_error(self) -> str:
        return self._last_error

    async def reload(self, token: str) -> str:
        old_task = self._task
        if old_task is not None and not old_task.done():
            _ = old_task.cancel()
            await asyncio.sleep(1.5)

        self._task = None
        self.running = False
        self._last_error = ""

        self.running = True
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._poll_loop(token))
        return "started"

    async def _poll_loop(self, token: str) -> None:
        ILinkAuthError = _load_component("ILinkAuthError")
        ILinkClient = _load_component("ILinkClient")
        ILinkError = _load_component("ILinkError")
        dispatch_wechat_message = _load_component("dispatch_wechat_message")
        client = ILinkClient(token)
        try:
            while self.running:
                try:
                    with SessionLocal() as session:
                        settings_service = SettingsService(session)
                        cursor = settings_service.get("wechat_updates_buf") or ""
                    messages, new_cursor = await client.get_updates(cursor)
                except ILinkAuthError as exc:
                    self._last_error = str(exc)
                    self.running = False
                    return
                except ILinkError as exc:
                    self._last_error = str(exc)
                    logger.warning("iLink poll error: %s", exc)
                    await asyncio.sleep(self._poll_interval)
                    continue

                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    try:
                        with SessionLocal() as session:
                            _ = await dispatch_wechat_message(message, session, client)
                    except Exception:
                        logger.exception("Error dispatching wechat message")

                if new_cursor != cursor:
                    with SessionLocal() as session:
                        settings_service = SettingsService(session)
                        settings_service.set("wechat_updates_buf", new_cursor)
                        settings_service.commit()

                await asyncio.sleep(self._poll_interval)

        except asyncio.CancelledError:
            self.running = False
        except Exception as exc:
            self.running = False
            self._last_error = str(exc)

    def stop(self) -> None:
        self.running = False
        if self._task:
            _ = self._task.cancel()
            self._task = None


class WechatService:
    def config_summary(self, session: Session) -> dict[str, object]:
        settings_service = SettingsService(session)
        token = settings_service.get("wechat_bot_token")
        token_masked = settings_service.get_masked("wechat_bot_token")
        bot_running = _wechat_runtime is not None and _wechat_runtime.running
        bot_error = _wechat_runtime.last_error if _wechat_runtime else ""
        cursor = settings_service.get("wechat_updates_buf") or ""
        return {
            "wechat_token_set": bool(token),
            "wechat_token_masked": token_masked,
            "wechat_running": bot_running,
            "wechat_error": bot_error,
            "wechat_cursor_length": len(cursor),
        }

    def save_token(self, session: Session, token: str) -> None:
        settings_service = SettingsService(session)
        settings_service.set("wechat_bot_token", token, encrypted=True)
        settings_service.commit()

    async def reload_bot(self, token: str) -> str:
        global _wechat_runtime
        if _wechat_runtime is None:
            _wechat_runtime = WechatBotRuntime()
        return await _wechat_runtime.reload(token)

    async def stop_bot(self) -> None:
        global _wechat_runtime
        if _wechat_runtime is not None:
            _wechat_runtime.stop()
            _wechat_runtime = None
