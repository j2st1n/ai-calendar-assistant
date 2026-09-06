import pytest
from unittest.mock import MagicMock, patch
from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.models import Base
from app.services.settings_service import SettingsService
from app.web import routes

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()

def test_status_context_contains_5_services_unconfigured(db_session):
    ctx = routes.status_context(db_session)
    assert "services_status" in ctx
    services = {s["id"]: s for s in ctx["services_status"]}
    assert set(services.keys()) == {"ai", "caldav", "wechat", "telegram", "discord"}

    # All unconfigured by default
    for sid in ("ai", "caldav", "wechat", "telegram", "discord"):
        assert services[sid]["dot_class"] == "dot-muted"
        assert services[sid]["state_label"] in ("未配置", "未启动")
        assert services[sid]["href"] == f"/console/{sid}"

def test_status_context_ai_and_caldav_statuses(db_session):
    svc = SettingsService(db_session)
    svc.set("ai_provider_name", "OpenAI")
    svc.set("ai_model", "gpt-4o")
    svc.set("caldav_url", "https://caldav.icloud.com")
    svc.set("caldav_calendar_name", "Work")
    svc.commit()

    # Before validation check -> dot-warning (待验证)
    ctx = routes.status_context(db_session)
    services = {s["id"]: s for s in ctx["services_status"]}
    assert services["ai"]["dot_class"] == "dot-warning"
    assert services["ai"]["state_label"] == "待验证"
    assert services["caldav"]["dot_class"] == "dot-warning"
    assert services["caldav"]["state_label"] == "待验证"

    # With passed check -> dot-success
    from datetime import datetime, timezone
    import json
    rev_ai = routes.revision(db_session, "ai")
    svc.set("connection_check_ai", json.dumps({"revision": rev_ai, "ok": True, "at": datetime.now(timezone.utc).isoformat()}))
    rev_cal = routes.revision(db_session, "caldav")
    svc.set("connection_check_caldav", json.dumps({"revision": rev_cal, "ok": True, "at": datetime.now(timezone.utc).isoformat()}))
    svc.commit()

    ctx2 = routes.status_context(db_session)
    services2 = {s["id"]: s for s in ctx2["services_status"]}
    assert services2["ai"]["dot_class"] == "dot-success"
    assert services2["ai"]["state_label"] == "已验证"
    assert services2["caldav"]["dot_class"] == "dot-success"
    assert services2["caldav"]["state_label"] == "已验证"

def test_status_context_channel_runtimes_and_dots(db_session):
    svc = SettingsService(db_session)
    svc.set("wechat_bot_token", "wechat-tok")
    svc.set("telegram_bot_token", "tg-tok")
    svc.set("telegram_bot_username", "my_bot")
    svc.set("discord_bot_token", "dc-tok")
    svc.commit()

    # Mock runtimes
    mock_wx_rt = MagicMock()
    mock_wx_rt.task_alive = True
    mock_wx_rt.snapshot.return_value = MagicMock(state="polling", state_label="在线")

    mock_tg_rt = MagicMock()
    mock_tg_rt.running = True

    mock_dc_rt = MagicMock()
    mock_dc_rt.running = True

    with patch("app.web.routes._runtime_if_loaded") as mock_rt_loader:
        def get_rt(name):
            if name == "get_wechat_bot_runtime":
                return mock_wx_rt
            elif name == "get_telegram_bot_runtime":
                return mock_tg_rt
            elif name == "get_discord_bot_runtime":
                return mock_dc_rt
            return None
        mock_rt_loader.side_effect = get_rt

        ctx = routes.status_context(db_session)
        services = {s["id"]: s for s in ctx["services_status"]}

        assert services["wechat"]["dot_class"] == "dot-success"
        assert services["wechat"]["state_label"] == "在线"
        assert services["telegram"]["dot_class"] == "dot-success"
        assert services["telegram"]["state_label"] == "运行中"
        assert services["telegram"]["summary"] == "@my_bot"
        assert services["discord"]["dot_class"] == "dot-success"
        assert services["discord"]["state_label"] == "运行中"

def test_dashboard_template_renders_services_status_card(db_session):
    svc = SettingsService(db_session)
    ctx = routes.status_context(db_session)
    request = Request({"type": "http", "method": "GET", "path": "/console", "headers": [], "session": {"admin_authenticated": True}})
    stats = routes.dashboard_stats(db_session, svc)

    html = routes.templates.get_template("dashboard.html").render(
        request=request, stats=stats, **ctx
    )

    assert "dashboard-services-card" in html
    assert "服务运行状态" in html
    assert "5 大核心服务与消息渠道实时状态监控" in html
    for sid in ("ai", "caldav", "wechat", "telegram", "discord"):
        assert f'id="service-status-{sid}"' in html
        assert f'href="/console/{sid}"' in html
    assert "dot-muted" in html
