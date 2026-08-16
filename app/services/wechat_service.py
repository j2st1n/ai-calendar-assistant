from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import logging
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

_wechat_runtime: WechatBotRuntime | None = None

# Tencent's official openclaw-weixin monitor cadence.
WECHAT_MAX_CONSECUTIVE_FAILURES = 3
WECHAT_RETRY_DELAY_SECONDS = 2.0
WECHAT_BACKOFF_DELAY_SECONDS = 30.0
WECHAT_DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000

_LAZY_COMPONENTS = {
    "dispatch_wechat_message": ("app.channels.wechat_handler", "dispatch_wechat_message"),
    "ILinkClient": ("app.integrations.ilink", "ILinkClient"),
    "ILinkError": ("app.integrations.ilink", "ILinkError"),
    "ILinkStaleTokenError": ("app.integrations.ilink", "ILinkStaleTokenError"),
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


def get_wechat_bot_runtime() -> WechatBotRuntime | None:
    return _wechat_runtime


class WechatRuntimeState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    POLLING = "polling"
    RETRYING = "retrying"
    PAUSED = "paused"
    STOPPING = "stopping"
    CRASHED = "crashed"


STATE_LABELS: dict[WechatRuntimeState, str] = {
    WechatRuntimeState.STOPPED: "已停止",
    WechatRuntimeState.STARTING: "启动中",
    WechatRuntimeState.POLLING: "在线",
    WechatRuntimeState.RETRYING: "异常，自动恢复中",
    WechatRuntimeState.PAUSED: "会话冷却中",
    WechatRuntimeState.STOPPING: "正在停止",
    WechatRuntimeState.CRASHED: "运行任务异常",
}


@dataclass(frozen=True)
class WechatErrorInfo:
    category: str
    message: str
    at: float
    http_status: int | None = None
    ret: int | None = None
    errcode: int | None = None
    code: str = ""


@dataclass(frozen=True)
class WechatRuntimeSnapshot:
    state: str
    state_label: str
    task_alive: bool
    consecutive_failures: int
    last_poll_success_at: float | None
    last_message_at: float | None
    next_retry_at: float | None
    pause_until: float | None
    current_error: dict[str, object] | None
    last_error: dict[str, object] | None


def _error_info(exc: Exception, *, category: str | None = None) -> WechatErrorInfo:
    inferred = category or str(getattr(exc, "category", "") or "")
    if not inferred:
        if hasattr(exc, "status_code"):
            inferred = "http"
        elif hasattr(exc, "ret") or hasattr(exc, "errcode"):
            inferred = "protocol"
        else:
            inferred = "internal"
    return WechatErrorInfo(
        category=inferred,
        message=str(exc),
        at=time.time(),
        http_status=getattr(exc, "status_code", None),
        ret=getattr(exc, "ret", None),
        errcode=getattr(exc, "errcode", None),
        code=str(getattr(exc, "code", "") or ""),
    )


class WechatBotRuntime:
    def __init__(self, poll_interval: float = 0.0) -> None:
        # poll_interval is retained only as a deterministic test hook. Production
        # uses zero: a completed long poll is followed immediately by the next.
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self.running = False
        self.state = WechatRuntimeState.STOPPED
        self.consecutive_failures = 0
        self.last_poll_success_at: float | None = None
        self.last_message_at: float | None = None
        self.next_retry_at: float | None = None
        self.pause_until: float | None = None
        self.current_error: WechatErrorInfo | None = None
        self.last_error_info: WechatErrorInfo | None = None

    @property
    def last_error(self) -> str:
        error = self.current_error or self.last_error_info
        return error.message if error else ""

    @property
    def task_alive(self) -> bool:
        return bool(self.running and self._task is not None and not self._task.done())

    def snapshot(self) -> WechatRuntimeSnapshot:
        state = self.state
        if self.running and self._task is not None and self._task.done():
            state = WechatRuntimeState.CRASHED
        return WechatRuntimeSnapshot(
            state=state.value,
            state_label=STATE_LABELS[state],
            task_alive=self.task_alive,
            consecutive_failures=self.consecutive_failures,
            last_poll_success_at=self.last_poll_success_at,
            last_message_at=self.last_message_at,
            next_retry_at=self.next_retry_at,
            pause_until=self.pause_until,
            current_error=asdict(self.current_error) if self.current_error else None,
            last_error=asdict(self.last_error_info) if self.last_error_info else None,
        )

    async def reload(self, token: str) -> str:
        async with self._lifecycle_lock:
            await self._stop_locked()
            self.consecutive_failures = 0
            self.last_poll_success_at = None
            self.last_message_at = None
            self.next_retry_at = None
            self.pause_until = None
            self.current_error = None
            self.last_error_info = None
            self.running = True
            self.state = WechatRuntimeState.STARTING
            self._task = asyncio.create_task(self._poll_loop(token))
        return "started"

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        task = self._task
        if task is not None and not task.done():
            self.state = WechatRuntimeState.STOPPING
            self.running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self.running = False
        self.state = WechatRuntimeState.STOPPED
        self.next_retry_at = None

    def _record_failure(self, exc: Exception, *, category: str | None = None) -> None:
        info = _error_info(exc, category=category)
        self.current_error = info
        self.last_error_info = info

    async def _sleep(self, seconds: float) -> None:
        self.next_retry_at = time.time() + seconds
        try:
            await asyncio.sleep(seconds)
        finally:
            self.next_retry_at = None

    async def _poll_loop(self, token: str) -> None:
        ILinkClient = _load_component("ILinkClient")
        ILinkError = _load_component("ILinkError")
        ILinkStaleTokenError = _load_component("ILinkStaleTokenError")
        dispatch_wechat_message = _load_component("dispatch_wechat_message")

        client = ILinkClient(token)
        next_timeout_ms = WECHAT_DEFAULT_LONG_POLL_TIMEOUT_MS
        cursor = ""
        try:
            with SessionLocal() as session:
                cursor = SettingsService(session).get("wechat_updates_buf") or ""
            self.state = WechatRuntimeState.POLLING

            while self.running:
                try:
                    result = await client.get_updates(cursor, timeout_ms=next_timeout_ms)
                    # Compatibility for local test doubles and old integrations.
                    if isinstance(result, tuple):
                        messages, new_cursor = result
                        suggested_timeout = None
                    else:
                        messages = result.messages
                        new_cursor = result.cursor
                        suggested_timeout = result.next_timeout_ms

                    if suggested_timeout is not None and suggested_timeout > 0:
                        next_timeout_ms = suggested_timeout

                    if new_cursor and new_cursor != cursor:
                        with SessionLocal() as session:
                            settings_service = SettingsService(session)
                            settings_service.set("wechat_updates_buf", new_cursor)
                            settings_service.commit()
                        cursor = new_cursor

                    self.consecutive_failures = 0
                    self.current_error = None
                    self.pause_until = None
                    self.last_poll_success_at = time.time()
                    self.state = WechatRuntimeState.POLLING

                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        self.last_message_at = time.time()
                        try:
                            with SessionLocal() as session:
                                await dispatch_wechat_message(message, session, client)
                        except Exception:
                            logger.exception("Error dispatching WeChat message")

                    if self._poll_interval > 0:
                        await asyncio.sleep(self._poll_interval)

                except ILinkStaleTokenError as exc:
                    self.consecutive_failures = 0
                    self._record_failure(exc, category="stale_token")
                    self.state = WechatRuntimeState.PAUSED
                    self.pause_until = client.pause_until
                    pause_seconds = max(0.0, (self.pause_until or time.time()) - time.time())
                    logger.warning(
                        "iLink stale token signal; pausing account requests for %.0fs ret=%s errcode=%s",
                        pause_seconds,
                        getattr(exc, "ret", None),
                        getattr(exc, "errcode", None),
                    )
                    await self._sleep(pause_seconds)
                    self.pause_until = None
                    continue
                except ILinkError as exc:
                    await self._handle_retryable_error(exc)
                except Exception as exc:
                    # Match the official monitor: an unexpected iteration failure
                    # is observable and backed off, but does not become a QR signal.
                    logger.exception("Unexpected WeChat poll iteration failure")
                    await self._handle_retryable_error(exc, category="internal")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure(exc, category="internal")
            self.state = WechatRuntimeState.CRASHED
            logger.exception("WeChat bot runtime crashed")
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()
            if self.state not in (WechatRuntimeState.CRASHED, WechatRuntimeState.STOPPING):
                self.state = WechatRuntimeState.STOPPED
            self.running = False

    async def _handle_retryable_error(self, exc: Exception, *, category: str | None = None) -> None:
        self.consecutive_failures += 1
        self._record_failure(exc, category=category)
        self.state = WechatRuntimeState.RETRYING
        if self.consecutive_failures >= WECHAT_MAX_CONSECUTIVE_FAILURES:
            delay = WECHAT_BACKOFF_DELAY_SECONDS
            logger.error(
                "iLink poll failed %d consecutive times; backing off %.0fs category=%s error=%s",
                self.consecutive_failures,
                delay,
                self.current_error.category if self.current_error else "unknown",
                exc,
            )
            self.consecutive_failures = 0
        else:
            delay = WECHAT_RETRY_DELAY_SECONDS
            logger.warning(
                "iLink poll failed attempt=%d/%d retry_in=%.0fs category=%s error=%s",
                self.consecutive_failures,
                WECHAT_MAX_CONSECUTIVE_FAILURES,
                delay,
                self.current_error.category if self.current_error else "unknown",
                exc,
            )
        await self._sleep(delay)


class WechatService:
    def config_summary(self, session: Session) -> dict[str, object]:
        settings_service = SettingsService(session)
        token = settings_service.get("wechat_bot_token")
        token_masked = settings_service.get_masked("wechat_bot_token")
        cursor = settings_service.get("wechat_updates_buf") or ""
        if _wechat_runtime is None:
            snapshot = WechatRuntimeSnapshot(
                state=WechatRuntimeState.STOPPED.value,
                state_label=STATE_LABELS[WechatRuntimeState.STOPPED],
                task_alive=False,
                consecutive_failures=0,
                last_poll_success_at=None,
                last_message_at=None,
                next_retry_at=None,
                pause_until=None,
                current_error=None,
                last_error=None,
            )
        else:
            snapshot = _wechat_runtime.snapshot()

        current_error = snapshot.current_error or {}
        return {
            "wechat_token_set": bool(token),
            "wechat_token_masked": token_masked,
            "wechat_running": snapshot.task_alive,
            "wechat_task_alive": snapshot.task_alive,
            "wechat_state": snapshot.state,
            "wechat_state_label": snapshot.state_label,
            "wechat_error": str(current_error.get("message") or ""),
            "wechat_current_error": snapshot.current_error,
            "wechat_last_error": snapshot.last_error,
            "wechat_consecutive_failures": snapshot.consecutive_failures,
            "wechat_last_poll_success_at": snapshot.last_poll_success_at,
            "wechat_last_message_at": snapshot.last_message_at,
            "wechat_next_retry_at": snapshot.next_retry_at,
            "wechat_pause_until": snapshot.pause_until,
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
            result = _wechat_runtime.stop()
            if inspect.isawaitable(result):
                await result
            _wechat_runtime = None
