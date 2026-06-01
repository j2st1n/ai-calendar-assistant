import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings as app_settings
from app.db.models import Base
from app.integrations.ilink import ILinkAuthError, ILinkError
from app.services.settings_service import SettingsService
from app.services.wechat_service import (
    WechatBotRuntime,
    WechatService,
    get_wechat_bot_runtime,
)

# Ensure APP_SECRET_KEY is initialized for tests that use encrypted settings
app_settings.app_secret_key = "test-secret-key-for-pytest"


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _fresh_runtime(poll_interval: float = 0.005) -> WechatBotRuntime:
    return WechatBotRuntime(poll_interval=poll_interval)


# ---------------------------------------------------------------------------
# WechatBotRuntime - unit tests
# ---------------------------------------------------------------------------

def test_runtime_initial_state():
    rt = _fresh_runtime()
    assert rt.running is False
    assert rt.last_error == ""


def test_reload_returns_started():
    async def _run():
        rt = _fresh_runtime(poll_interval=0.0)
        with patch.object(rt, "_poll_loop"):
            _ = await rt.reload("tok")
        assert rt._task is not None
        assert rt.running is True

    asyncio.run(_run())


def test_reload_clears_previous_state():
    async def _run():
        rt = _fresh_runtime(poll_interval=0.0)
        rt._last_error = "old"
        rt.running = True
        old_task = MagicMock()
        old_task.done.return_value = False
        rt._task = old_task

        with patch.object(rt, "_poll_loop"):
            _ = await rt.reload("tok")

        old_task.cancel.assert_called_once()
        assert rt._last_error == ""

    asyncio.run(_run())


def test_stop_sets_running_false_and_cancels_task():
    async def _run():
        rt = _fresh_runtime(poll_interval=0.0)
        with patch.object(rt, "_poll_loop"):
            _ = await rt.reload("tok")
        task = rt._task
        assert rt.running is True
        assert task is not None

        rt.stop()
        assert rt.running is False
        assert rt._task is None

    asyncio.run(_run())


def test_stop_noop_when_not_running():
    rt = _fresh_runtime()
    rt.stop()
    assert rt.running is False


# ---------------------------------------------------------------------------
# Poll loop tests
# ---------------------------------------------------------------------------

def test_poll_loop_processes_messages():
    msgs = [{"message_id": 1, "from_user_id": "u@im.wechat", "context_token": "ctx"}]
    call_count = 0

    async def fake_get_updates(_cursor):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return msgs, "next-cursor"
        return [], "next-cursor"

    async def _run():
        mgr = MagicMock()
        mgr.get_updates = AsyncMock(side_effect=fake_get_updates)

        rt = WechatBotRuntime(poll_interval=0.001)

        with patch("app.services.wechat_service.ILinkClient", return_value=mgr), \
             patch("app.services.wechat_service.dispatch_wechat_message") as mock_dispatch, \
             patch("app.services.wechat_service.SettingsService") as MockSettings, \
             patch("app.services.wechat_service.SessionLocal") as MockSessionLocal:

            settings_mock = MagicMock()
            settings_mock.get.return_value = ""
            MockSettings.return_value = settings_mock
            MockSessionLocal.return_value = MagicMock()
            mock_dispatch.return_value = []

            rt.running = True
            task = asyncio.get_event_loop().create_task(rt._poll_loop("tok"))

            await asyncio.sleep(0.05)
            rt.running = False
            await asyncio.wait_for(task, timeout=2.0)

        mgr.get_updates.assert_awaited()
        _ = mock_dispatch.assert_awaited_once()
        assert mock_dispatch.await_args is not None
        assert mock_dispatch.await_args.args[0] == msgs[0]

    asyncio.run(_run())


def test_poll_loop_persists_cursor():
    msgs = [{"message_id": 1, "from_user_id": "u@im.wechat", "context_token": "ctx"}]

    async def fake_get_updates(_cursor):
        return msgs, "cursor-xyz"

    async def _run():
        mgr = MagicMock()
        mgr.get_updates = AsyncMock(side_effect=fake_get_updates)

        rt = WechatBotRuntime(poll_interval=0.001)
        mock_settings = MagicMock()
        mock_settings.get.return_value = ""

        with patch("app.services.wechat_service.ILinkClient", return_value=mgr), \
             patch("app.services.wechat_service.dispatch_wechat_message", return_value=[]), \
             patch("app.services.wechat_service.SettingsService", return_value=mock_settings), \
             patch("app.services.wechat_service.SessionLocal", return_value=MagicMock()):

            rt.running = True
            task = asyncio.get_event_loop().create_task(rt._poll_loop("tok"))

            await asyncio.sleep(0.03)
            rt.running = False
            await asyncio.wait_for(task, timeout=2.0)

        mock_settings.set.assert_any_call("wechat_updates_buf", "cursor-xyz")
        mock_settings.commit.assert_called()

    asyncio.run(_run())


def test_poll_loop_skips_non_dict_messages():
    msgs = ["not-a-dict", {"message_id": 1, "from_user_id": "u@im.wechat", "context_token": "ctx"}, None]

    async def fake_get_updates(_cursor):
        return msgs, "cursor"

    async def _run():
        mgr = MagicMock()
        mgr.get_updates = AsyncMock(side_effect=fake_get_updates)

        rt = WechatBotRuntime(poll_interval=0.05)

        with patch("app.services.wechat_service.ILinkClient", return_value=mgr), \
             patch("app.services.wechat_service.dispatch_wechat_message") as mock_dispatch, \
             patch("app.services.wechat_service.SettingsService") as MockSettings, \
             patch("app.services.wechat_service.SessionLocal") as MockSessionLocal:

            settings_mock = MagicMock()
            settings_mock.get.return_value = ""
            MockSettings.return_value = settings_mock
            MockSessionLocal.return_value = MagicMock()
            mock_dispatch.return_value = []

            rt.running = True
            task = asyncio.get_event_loop().create_task(rt._poll_loop("tok"))

            await asyncio.sleep(0.02)
            rt.running = False
            await asyncio.wait_for(task, timeout=2.0)

        assert mock_dispatch.await_count == 1
        assert mock_dispatch.await_args is not None
        assert isinstance(mock_dispatch.await_args.args[0], dict)

    asyncio.run(_run())


def test_poll_loop_handles_auth_error():
    async def fake_get_updates(_cursor):
        raise ILinkAuthError("token expired")

    async def _run():
        mgr = MagicMock()
        mgr.get_updates = AsyncMock(side_effect=fake_get_updates)

        rt = WechatBotRuntime(poll_interval=0.0)

        with patch("app.services.wechat_service.ILinkClient", return_value=mgr), \
             patch("app.services.wechat_service.SettingsService") as MockSettings, \
             patch("app.services.wechat_service.SessionLocal") as MockSessionLocal:

            settings_mock = MagicMock()
            settings_mock.get.return_value = ""
            MockSettings.return_value = settings_mock
            MockSessionLocal.return_value = MagicMock()

            rt.running = True
            task = asyncio.get_event_loop().create_task(rt._poll_loop("tok"))
            await asyncio.wait_for(task, timeout=2.0)

        assert rt.running is False
        assert "token expired" in rt.last_error

    asyncio.run(_run())


def test_poll_loop_handles_ilink_error_and_continues():
    call_count = 0

    async def fake_get_updates(_cursor):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ILinkError("timeout")
        return [{"message_id": 1, "from_user_id": "u@im.wechat", "context_token": "ctx"}], "cursor"

    async def _run():
        mgr = MagicMock()
        mgr.get_updates = AsyncMock(side_effect=fake_get_updates)

        rt = WechatBotRuntime(poll_interval=0.001)

        with patch("app.services.wechat_service.ILinkClient", return_value=mgr), \
             patch("app.services.wechat_service.dispatch_wechat_message") as mock_dispatch, \
             patch("app.services.wechat_service.SettingsService") as MockSettings, \
             patch("app.services.wechat_service.SessionLocal") as MockSessionLocal:

            settings_mock = MagicMock()
            settings_mock.get.return_value = ""
            MockSettings.return_value = settings_mock
            MockSessionLocal.return_value = MagicMock()
            mock_dispatch.return_value = []

            rt.running = True
            task = asyncio.get_event_loop().create_task(rt._poll_loop("tok"))

            await asyncio.sleep(0.03)
            rt.running = False
            await asyncio.wait_for(task, timeout=2.0)

        assert rt.running is False
        assert "timeout" in rt.last_error
        assert mock_dispatch.awaited
        assert call_count >= 2

    asyncio.run(_run())


def test_poll_loop_handles_dispatch_exception():
    async def fake_get_updates(_cursor):
        return [{"message_id": 1, "from_user_id": "u@im.wechat", "context_token": "ctx"}], "cursor"

    async def _run():
        mgr = MagicMock()
        mgr.get_updates = AsyncMock(side_effect=fake_get_updates)

        rt = WechatBotRuntime(poll_interval=0.0)

        with patch("app.services.wechat_service.ILinkClient", return_value=mgr), \
             patch("app.services.wechat_service.dispatch_wechat_message", side_effect=RuntimeError("boom")), \
             patch("app.services.wechat_service.SettingsService") as MockSettings, \
             patch("app.services.wechat_service.SessionLocal") as MockSessionLocal:

            settings_mock = MagicMock()
            settings_mock.get.return_value = ""
            MockSettings.return_value = settings_mock
            MockSessionLocal.return_value = MagicMock()

            rt.running = True
            task = asyncio.get_event_loop().create_task(rt._poll_loop("tok"))

            await asyncio.sleep(0.02)
            rt.running = False
            await asyncio.wait_for(task, timeout=2.0)

        assert rt.running is False

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# WechatService - config / token
# ---------------------------------------------------------------------------

def test_config_summary_returns_expected_keys():
    session = _session()
    import app.services.wechat_service as ws_mod

    SettingsService(session).set("wechat_bot_token", "secret-123", encrypted=True)
    SettingsService(session).commit()

    old = ws_mod._wechat_runtime
    ws_mod._wechat_runtime = None
    try:
        result = WechatService().config_summary(session)
        assert result["wechat_token_set"] is True
        assert result["wechat_token_masked"]
        assert result["wechat_running"] is False
        assert result["wechat_error"] == ""
        assert result["wechat_cursor_length"] == 0
    finally:
        ws_mod._wechat_runtime = old


def test_config_summary_reflects_runtime_state():
    session = _session()
    import app.services.wechat_service as ws_mod

    SettingsService(session).set("wechat_bot_token", "secret-456", encrypted=True)
    SettingsService(session).set("wechat_updates_buf", "some-cursor-data")
    SettingsService(session).commit()

    rt = WechatBotRuntime(poll_interval=5.0)
    rt.running = True
    rt._last_error = "test error"

    old = ws_mod._wechat_runtime
    ws_mod._wechat_runtime = rt
    try:
        result = WechatService().config_summary(session)
        assert result["wechat_token_set"] is True
        assert result["wechat_running"] is True
        assert result["wechat_error"] == "test error"
        assert result["wechat_cursor_length"] == len("some-cursor-data")
    finally:
        ws_mod._wechat_runtime = old


def test_config_summary_when_no_token():
    session = _session()
    import app.services.wechat_service as ws_mod

    old = ws_mod._wechat_runtime
    ws_mod._wechat_runtime = None
    try:
        result = WechatService().config_summary(session)
        assert result["wechat_token_set"] is False
        assert result["wechat_token_masked"] == ""
        assert result["wechat_running"] is False
        assert result["wechat_error"] == ""
    finally:
        ws_mod._wechat_runtime = old


def test_save_token_persists_encrypted():
    session = _session()
    WechatService().save_token(session, "my-token")
    stored = SettingsService(session).get("wechat_bot_token")
    assert stored == "my-token"


# ---------------------------------------------------------------------------
# WechatService - reload / stop
# ---------------------------------------------------------------------------

def test_reload_bot_creates_and_invokes_runtime():
    async def _run():
        import app.services.wechat_service as ws_mod

        old = ws_mod._wechat_runtime
        ws_mod._wechat_runtime = None
        try:
            svc = WechatService()
            assert ws_mod._wechat_runtime is None
            with patch("app.services.wechat_service.WechatBotRuntime") as MockRuntime:
                mock_rt = MagicMock()
                mock_rt.reload = AsyncMock(return_value="started")
                MockRuntime.return_value = mock_rt

                result = await svc.reload_bot("tok")
                MockRuntime.assert_called_once()
                mock_rt.reload.assert_awaited_once_with("tok")
                assert ws_mod._wechat_runtime is mock_rt
                assert result == "started"
        finally:
            ws_mod._wechat_runtime = old

    asyncio.run(_run())


def test_reload_bot_reuses_existing_runtime():
    async def _run():
        import app.services.wechat_service as ws_mod

        old = ws_mod._wechat_runtime
        existing = MagicMock()
        existing.reload = AsyncMock(return_value="ok")
        ws_mod._wechat_runtime = existing
        try:
            svc = WechatService()
            result = await svc.reload_bot("tok2")
            existing.reload.assert_awaited_once_with("tok2")
            assert result == "ok"
            assert ws_mod._wechat_runtime is existing
        finally:
            ws_mod._wechat_runtime = old

    asyncio.run(_run())


def test_stop_bot_stops_and_clears_runtime():
    async def _run():
        import app.services.wechat_service as ws_mod

        old = ws_mod._wechat_runtime
        mock_rt = MagicMock()
        mock_rt.stop = MagicMock()
        ws_mod._wechat_runtime = mock_rt
        try:
            await WechatService().stop_bot()
            mock_rt.stop.assert_called_once()
            assert ws_mod._wechat_runtime is None
        finally:
            ws_mod._wechat_runtime = old

    asyncio.run(_run())


def test_stop_bot_noop_when_no_runtime():
    async def _run():
        import app.services.wechat_service as ws_mod

        old = ws_mod._wechat_runtime
        ws_mod._wechat_runtime = None
        try:
            await WechatService().stop_bot()
        finally:
            ws_mod._wechat_runtime = old

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# get_wechat_bot_runtime
# ---------------------------------------------------------------------------

def test_get_wechat_bot_runtime_returns_module_value():
    import app.services.wechat_service as ws_mod

    old = ws_mod._wechat_runtime
    ws_mod._wechat_runtime = "fake"
    try:
        assert get_wechat_bot_runtime() == "fake"
    finally:
        ws_mod._wechat_runtime = old
