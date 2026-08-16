import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings as app_settings
from app.db.models import Base
from app.integrations.ilink import (
    GetUpdatesResult,
    ILinkError,
    ILinkHttpError,
    ILinkStaleTokenError,
    STALE_TOKEN_ERRCODE,
)
from app.services.settings_service import SettingsService
from app.services.wechat_service import (
    WechatBotRuntime,
    WechatErrorInfo,
    WechatRuntimeState,
    WechatService,
    get_wechat_bot_runtime,
)

app_settings.app_secret_key = "test-secret-key-for-pytest"


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class FakeILinkClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.timeouts: list[int] = []
        self.pause_until: float | None = None
        self.closed = False
        self.block = asyncio.Event()

    async def get_updates(self, _cursor, *, timeout_ms):
        self.calls += 1
        self.timeouts.append(timeout_ms)
        if not self.responses:
            await self.block.wait()
            return GetUpdatesResult([], _cursor, None)
        response = self.responses.pop(0)
        if isinstance(response, ILinkStaleTokenError):
            self.pause_until = time.time() + 0.01
            raise response
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self):
        self.closed = True


def _runtime_context(client: FakeILinkClient, dispatch: AsyncMock | None = None):
    import app.services.wechat_service as ws_mod

    settings = MagicMock()
    settings.get.return_value = ""
    session_local = MagicMock()
    session_local.return_value.__enter__.return_value = MagicMock()
    session_local.return_value.__exit__.return_value = False
    return (
        patch.object(ws_mod, "ILinkClient", return_value=client, create=True),
        patch.object(ws_mod, "ILinkError", ILinkError, create=True),
        patch.object(ws_mod, "ILinkStaleTokenError", ILinkStaleTokenError, create=True),
        patch.object(
            ws_mod,
            "dispatch_wechat_message",
            dispatch or AsyncMock(return_value=[]),
            create=True,
        ),
        patch.object(ws_mod, "SettingsService", return_value=settings),
        patch.object(ws_mod, "SessionLocal", session_local),
        settings,
    )


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.001)


def test_runtime_initial_state_and_snapshot():
    rt = WechatBotRuntime()
    snapshot = rt.snapshot()
    assert rt.running is False
    assert rt.last_error == ""
    assert snapshot.state == "stopped"
    assert snapshot.task_alive is False


def test_reload_and_stop_cancel_inflight_task():
    async def run():
        rt = WechatBotRuntime()
        started = asyncio.Event()

        async def fake_loop(_token):
            started.set()
            await asyncio.Event().wait()

        with patch.object(rt, "_poll_loop", side_effect=fake_loop):
            assert await rt.reload("tok") == "started"
            await asyncio.wait_for(started.wait(), timeout=1)
            assert rt.task_alive is True
            await rt.stop()
        assert rt.running is False
        assert rt._task is None
        assert rt.state is WechatRuntimeState.STOPPED

    asyncio.run(run())


def test_reload_never_overlaps_two_pollers():
    async def run():
        rt = WechatBotRuntime()
        active = 0
        max_active = 0

        async def fake_loop(_token):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        with patch.object(rt, "_poll_loop", side_effect=fake_loop):
            await rt.reload("one")
            await asyncio.sleep(0)
            await rt.reload("two")
            await asyncio.sleep(0)
            await rt.stop()
        assert max_active == 1

    asyncio.run(run())


def test_poll_success_persists_cursor_and_uses_server_timeout():
    async def run():
        message = {"message_id": 1, "from_user_id": "u@im.wechat", "context_token": "ctx"}
        client = FakeILinkClient([GetUpdatesResult([message], "next", 42_000)])
        dispatched = asyncio.Event()

        async def dispatch(*_args):
            dispatched.set()
            return []

        mock_dispatch = AsyncMock(side_effect=dispatch)
        ctx = _runtime_context(client, mock_dispatch)
        rt = WechatBotRuntime()
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5]:
            rt.running = True
            task = asyncio.create_task(rt._poll_loop("tok"))
            await asyncio.wait_for(dispatched.wait(), timeout=1)
            await _wait_until(lambda: client.calls >= 2)
            await _cancel(task)

        settings = ctx[6]
        settings.set.assert_any_call("wechat_updates_buf", "next")
        settings.commit.assert_called()
        assert client.timeouts[:2] == [35_000, 42_000]
        assert rt.last_poll_success_at is not None
        assert rt.last_message_at is not None
        assert client.closed is True

    asyncio.run(run())


def test_http_401_retries_and_recovers_without_relogin_state():
    async def run():
        client = FakeILinkClient(
            [
                ILinkHttpError(401),
                GetUpdatesResult([], "cursor", None),
            ]
        )
        ctx = _runtime_context(client)
        rt = WechatBotRuntime()
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], patch(
            "app.services.wechat_service.WECHAT_RETRY_DELAY_SECONDS", 0.001
        ):
            rt.running = True
            task = asyncio.create_task(rt._poll_loop("tok"))
            await _wait_until(lambda: rt.last_poll_success_at is not None)
            assert rt.running is True
            assert rt.state is WechatRuntimeState.POLLING
            assert rt.current_error is None
            assert rt.last_error_info is not None
            assert rt.last_error_info.http_status == 401
            await _cancel(task)

    asyncio.run(run())


def test_repeated_errors_back_off_and_still_recover():
    async def run():
        client = FakeILinkClient(
            [
                ILinkError("network down"),
                ILinkError("network down"),
                ILinkError("network down"),
                ILinkError("network down"),
                GetUpdatesResult([], "cursor", None),
            ]
        )
        ctx = _runtime_context(client)
        rt = WechatBotRuntime()
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], patch(
            "app.services.wechat_service.WECHAT_RETRY_DELAY_SECONDS", 0.001
        ), patch("app.services.wechat_service.WECHAT_BACKOFF_DELAY_SECONDS", 0.001):
            rt.running = True
            task = asyncio.create_task(rt._poll_loop("tok"))
            await _wait_until(lambda: rt.last_poll_success_at is not None)
            assert client.calls >= 5
            assert rt.running is True
            assert rt.consecutive_failures == 0
            await _cancel(task)

    asyncio.run(run())


def test_stale_token_pauses_then_reuses_same_runtime():
    async def run():
        stale = ILinkStaleTokenError(
            "stale token",
            ret=STALE_TOKEN_ERRCODE,
            errcode=STALE_TOKEN_ERRCODE,
        )
        client = FakeILinkClient([stale, GetUpdatesResult([], "cursor", None)])
        ctx = _runtime_context(client)
        rt = WechatBotRuntime()
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5]:
            rt.running = True
            task = asyncio.create_task(rt._poll_loop("same-token"))
            await _wait_until(lambda: rt.last_poll_success_at is not None)
            assert client.calls >= 2
            assert rt.running is True
            assert rt.last_error_info is not None
            assert rt.last_error_info.category == "stale_token"
            assert rt.pause_until is None
            await _cancel(task)

    asyncio.run(run())


def test_unexpected_iteration_error_is_retryable():
    async def run():
        client = FakeILinkClient([RuntimeError("unexpected"), GetUpdatesResult([], "cursor", None)])
        ctx = _runtime_context(client)
        rt = WechatBotRuntime()
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], patch(
            "app.services.wechat_service.WECHAT_RETRY_DELAY_SECONDS", 0.001
        ):
            rt.running = True
            task = asyncio.create_task(rt._poll_loop("tok"))
            await _wait_until(lambda: rt.last_poll_success_at is not None)
            assert rt.last_error_info is not None
            assert rt.last_error_info.category == "internal"
            await _cancel(task)

    asyncio.run(run())


def test_config_summary_exposes_runtime_health():
    session = _session()
    import app.services.wechat_service as ws_mod

    SettingsService(session).set("wechat_bot_token", "secret-456", encrypted=True)
    SettingsService(session).set("wechat_updates_buf", "some-cursor-data")
    SettingsService(session).commit()

    rt = WechatBotRuntime()
    rt.running = True
    rt.state = WechatRuntimeState.RETRYING
    rt._task = MagicMock()
    rt._task.done.return_value = False
    rt.consecutive_failures = 2
    rt.current_error = WechatErrorInfo("tcp", "network down", time.time())
    rt.last_error_info = rt.current_error

    old = ws_mod._wechat_runtime
    ws_mod._wechat_runtime = rt
    try:
        result = WechatService().config_summary(session)
        assert result["wechat_running"] is True
        assert result["wechat_state"] == "retrying"
        assert result["wechat_consecutive_failures"] == 2
        assert result["wechat_current_error"]["category"] == "tcp"
        assert result["wechat_cursor_length"] == len("some-cursor-data")
    finally:
        ws_mod._wechat_runtime = old


def test_config_summary_without_runtime_is_stopped():
    session = _session()
    import app.services.wechat_service as ws_mod

    old = ws_mod._wechat_runtime
    ws_mod._wechat_runtime = None
    try:
        result = WechatService().config_summary(session)
        assert result["wechat_running"] is False
        assert result["wechat_state"] == "stopped"
        assert result["wechat_current_error"] is None
    finally:
        ws_mod._wechat_runtime = old


def test_save_token_persists_encrypted():
    session = _session()
    WechatService().save_token(session, "my-token")
    assert SettingsService(session).get("wechat_bot_token") == "my-token"


def test_reload_bot_creates_runtime_and_stop_awaits_it():
    async def run():
        import app.services.wechat_service as ws_mod

        old = ws_mod._wechat_runtime
        ws_mod._wechat_runtime = None
        try:
            with patch("app.services.wechat_service.WechatBotRuntime") as runtime_type:
                runtime = runtime_type.return_value
                runtime.reload = AsyncMock(return_value="started")
                runtime.stop = AsyncMock()
                service = WechatService()
                assert await service.reload_bot("tok") == "started"
                runtime.reload.assert_awaited_once_with("tok")
                assert get_wechat_bot_runtime() is runtime
                await service.stop_bot()
                runtime.stop.assert_awaited_once()
                assert get_wechat_bot_runtime() is None
        finally:
            ws_mod._wechat_runtime = old

    asyncio.run(run())
