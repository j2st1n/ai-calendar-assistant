from __future__ import annotations

import asyncio
import importlib
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

_wechat_runtime: "WechatBotRuntime | None" = None

# 轮询自愈/退避参数（风格对齐 telegram_service::_retry_telegram_network）。
# token 失效(401/403)有限重试：排除微信网关瞬时 401 抖动；仍失败判 token 真失效。
WECHAT_AUTH_RETRY_ATTEMPTS = 3
WECHAT_AUTH_RETRY_BASE_SECONDS = 5.0
# 瞬时错误(网络异常/一般 ILinkError)快速指数退避。
WECHAT_TRANSIENT_RETRY_ATTEMPTS = 5
WECHAT_TRANSIENT_BASE_SECONDS = 2.0
WECHAT_TRANSIENT_MAX_SECONDS = 30.0
# 瞬时错误快速退避用尽后，转入慢重试自动回活（网络恢复后无需手动重启）。
WECHAT_SLOW_RETRY_INTERVAL = 30.0

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
    _needs_relogin: bool
    _last_error_ts: float | None

    def __init__(self, poll_interval: float = 5.0) -> None:
        self._task = None
        self.running = False
        self._last_error = ""
        self._needs_relogin = False
        self._last_error_ts = None
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
        self._last_error_ts = None
        self._needs_relogin = False

        self.running = True
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._poll_loop(token))
        return "started"

    async def _poll_loop(self, token: str) -> None:
        ILinkAuthError = _load_component("ILinkAuthError")
        ILinkClient = _load_component("ILinkClient")
        ILinkError = _load_component("ILinkError")
        dispatch_wechat_message = _load_component("dispatch_wechat_message")
        import httpx as _httpx  # 用于捕获网络层异常，保持 lazy import

        client = ILinkClient(token)
        transient_fails = 0  # 连续瞬时失败计数：快速退避用尽后转入慢重试
        auth_fails = 0  # 401/403 鉴权连续失败次数

        def _record_error(exc: Exception, /) -> None:
            self._last_error = str(exc)
            self._last_error_ts = time.time()

        def _record_success() -> None:
            # 保留最近一次错误文本/时间供诊断（控制台可结合 running 判断时效），
            # 仅清除"需重新扫码"这类需人工处理的强信号。
            self._needs_relogin = False

        try:
            while self.running:
                try:
                    with SessionLocal() as session:
                        settings_service = SettingsService(session)
                        cursor = settings_service.get("wechat_updates_buf") or ""
                    messages, new_cursor = await client.get_updates(cursor)
                except ILinkAuthError as exc:
                    # 鉴权失败：有限重试排除瞬时 401/403，仍失败判 token 真失效需重扫。
                    auth_fails += 1
                    _record_error(exc)
                    if auth_fails >= WECHAT_AUTH_RETRY_ATTEMPTS:
                        self._needs_relogin = True
                        logger.error(
                            "iLink auth failed %d consecutive times, bot offline, needs re-login: %s",
                            auth_fails,
                            exc,
                        )
                        self.running = False
                        return
                    delay = WECHAT_AUTH_RETRY_BASE_SECONDS * auth_fails
                    logger.warning(
                        "iLink auth transient attempt=%d/%d delay=%.1fs error=%s",
                        auth_fails,
                        WECHAT_AUTH_RETRY_ATTEMPTS,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                except (ILinkError, _httpx.HTTPError) as exc:
                    # 瞬时错误：快速指数退避，用尽后转慢重试自动回活，恢复后无需手动重启。
                    transient_fails += 1
                    _record_error(exc)
                    if transient_fails >= WECHAT_TRANSIENT_RETRY_ATTEMPTS:
                        logger.error(
                            "iLink transient failed %d consecutive times, entering slow retry every %.0fs: %s",
                            transient_fails,
                            WECHAT_SLOW_RETRY_INTERVAL,
                            exc,
                        )
                        await asyncio.sleep(WECHAT_SLOW_RETRY_INTERVAL)
                        continue
                    delay = min(
                        WECHAT_TRANSIENT_BASE_SECONDS * (2 ** (transient_fails - 1)),
                        WECHAT_TRANSIENT_MAX_SECONDS,
                    )
                    logger.warning(
                        "iLink poll transient error attempt=%d/%d delay=%.1fs error_type=%s error=%s",
                        transient_fails,
                        WECHAT_TRANSIENT_RETRY_ATTEMPTS,
                        delay,
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue

                # 单次 get_updates 成功：重置错误计数（回到快速退避阈值）。
                transient_fails = 0
                auth_fails = 0
                _record_success()

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
            self._last_error_ts = time.time()
            logger.exception("Wechat bot runtime stopped unexpectedly")

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
        bot_needs_relogin = _wechat_runtime._needs_relogin if _wechat_runtime else False
        bot_last_error_ts = _wechat_runtime._last_error_ts if _wechat_runtime else None
        cursor = settings_service.get("wechat_updates_buf") or ""
        return {
            "wechat_token_set": bool(token),
            "wechat_token_masked": token_masked,
            "wechat_running": bot_running,
            "wechat_error": bot_error,
            "wechat_needs_relogin": bot_needs_relogin,
            "wechat_last_error_ts": bot_last_error_ts,
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
