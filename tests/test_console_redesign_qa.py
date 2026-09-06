"""QA Automated Regression & Acceptance Test Suite for Console Redesign.

Verifies:
1. Calendar Settings Option B Dual-Column Layout (CSS classes, grid layout, responsive breakpoints, sticky sidebar).
2. Dashboard Services Status Card Convergence (verification tray, AJAX badge updates, removal of redundant connections card).
3. Message Channels Convergence (/console/channels aggregation, 307 redirect matrix, query preservation, POST targets, tab polling lifecycle, sidebar streamlining).
4. Local Preview Sanity Check (strict local preview validation, no deployment / publishing).
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.db.models import Base
from app.services.settings_service import SettingsService
from app.web import routes
from app.web.routes import require_admin, router


class _FakeSessionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope["session"] = {"admin_authenticated": True}
        await self.app(scope, receive, send)


def _make_app(require_auth: bool = True) -> FastAPI:
    app = FastAPI()
    if require_auth:
        app.dependency_overrides[require_admin] = lambda: None
        app.add_middleware(_FakeSessionMiddleware)
    app.include_router(router)
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, follow_redirects=False)


async def _post_form(app: FastAPI, path: str, data: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, data=data or {}, follow_redirects=False)


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


# ============================================================================
# Part 1: Calendar Settings Option B Dual-Column Layout (方案 B 双栏布局与响应式)
# ============================================================================

class TestCalendarDualColumnLayout:
    """QA tests for Option B Calendar Settings dual-column layout."""

    def test_caldav_styles_css_layout_definitions(self):
        """Verify styles.css contains required 2-column grid and responsive rules."""
        css = Path("app/web/static/styles.css").read_text(encoding="utf-8")

        # 1. Container .settings-layout grid 1fr 320px
        assert ".settings-layout {" in css
        assert "grid-template-columns: 1fr 320px;" in css
        assert "gap: 20px;" in css
        assert "align-items: start;" in css

        # 2. Main and Sidebar definitions
        assert ".settings-main {" in css
        assert ".settings-sidebar {" in css
        assert "position: sticky;" in css
        assert "top: 16px;" in css

        # 3. Responsive breakpoint for mobile (<= 768px)
        assert "@media (max-width: 768px)" in css
        assert "grid-template-columns: minmax(0, 1fr);" in css
        assert "position: static;" in css

        # 4. Two-column form grid .form-grid-2col
        assert ".form-grid-2col {" in css
        assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css

        # 5. Caldav form max-width constraint removed
        assert ".caldav-form {" in css
        assert "max-width: none;" in css

    def test_caldav_template_option_b_structure(self):
        """Verify caldav.html implements Option B structure with left form & right sticky sidebar."""
        template_text = Path("app/web/templates/caldav.html").read_text(encoding="utf-8")

        # Layout containers
        assert '<div class="settings-layout">' in template_text
        assert '<div class="settings-main stack">' in template_text
        assert '<div class="settings-sidebar stack">' in template_text

        # Left column contents
        assert "1. 连接日历服务" in template_text
        assert '<div class="form-grid-2col">' in template_text
        assert 'name="caldav_username"' in template_text
        assert 'name="caldav_password"' in template_text
        assert "2. 选择目标日历" in template_text
        assert 'id="calendar-picker"' in template_text
        assert "3. 默认日程规则" in template_text
        assert '<button type="submit">保存日历设置</button>' in template_text

        # Right column contents
        assert "日历服务状态" in template_text
        assert 'id="caldav-service-badge"' in template_text
        assert 'id="caldav-service-status-text"' in template_text
        assert "连通性与探针测试" in template_text
        assert 'data-caldav-action="test"' in template_text
        assert 'data-caldav-action="write-test"' in template_text
        assert 'data-caldav-action="calendars"' in template_text
        assert "服务商提示" in template_text
        assert 'id="caldav-provider-hint"' in template_text

        # Dynamic service badge JS integration
        assert "function setServiceBadge(dotClass, label)" in template_text
        assert "setServiceBadge('dot-error', '异常');" in template_text
        assert "setServiceBadge('dot-success', '正常');" in template_text
        assert "setServiceBadge('dot-warning', '未保存');" in template_text


# ============================================================================
# Part 2: Dashboard Service Status Card & Verification Tray Convergence
# ============================================================================

class TestDashboardConvergence:
    """QA tests for Dashboard services status card and verification tray convergence."""

    def test_dashboard_template_tray_and_removal_of_redundant_sections(self, db_session):
        """Verify dashboard.html integrates verification tray and completely removes redundant bottom summary."""
        raw_html = Path("app/web/templates/dashboard.html").read_text(encoding="utf-8")

        # 1. Verification tray integrated into service card
        assert '<div class="dashboard-verification-tray">' in raw_html
        assert "连通性即时验证与同步记录" in raw_html
        assert "最近日历操作成功" in raw_html

        # 2. Redundant bottom connection card must NOT exist in raw template
        assert 'aria-labelledby="connections-title"' not in raw_html
        assert 'id="connections-title"' not in raw_html
        assert '<div class="dashboard-connections">' not in raw_html
        assert "管理渠道 →" not in raw_html

        # 3. Rendered template verification
        svc = SettingsService(db_session)
        ctx = routes.status_context(db_session)
        request = Request({"type": "http", "method": "GET", "path": "/console", "headers": [], "session": {"admin_authenticated": True}})
        stats = routes.dashboard_stats(db_session, svc)
        rendered = routes.templates.get_template("dashboard.html").render(
            request=request, stats=stats, **ctx
        )

        assert 'data-check-kind="ai"' in rendered
        assert 'data-check-kind="caldav"' in rendered
        assert 'id="check-ai"' in rendered
        assert 'id="check-caldav"' in rendered
        assert "AI 主模型配置验证" in rendered
        assert "CalDAV 日历连通性与写入验证" in rendered

    def test_dashboard_verification_tray_css(self):
        """Verify styles.css includes styles for .dashboard-verification-tray."""
        css = Path("app/web/static/styles.css").read_text(encoding="utf-8")
        assert ".dashboard-verification-tray {" in css
        assert ".dashboard-verification-tray .tray-heading {" in css
        assert ".dashboard-verification-tray .tray-title {" in css

    def test_dashboard_ajax_badge_update_script(self):
        """Verify that dashboard JS updates service badges dynamically when test buttons are clicked."""
        html = Path("app/web/templates/dashboard.html").read_text(encoding="utf-8")
        assert "fetch('/console/connections/' + kind + '/test'" in html
        assert "document.getElementById('service-status-' + kind)" in html
        assert "dot-success" in html
        assert "dot-error" in html
        assert "已验证" in html
        assert "测试失败" in html


# ============================================================================
# Part 3: Message Channels Consolidation & 307 Redirect Matrix
# ============================================================================

class TestChannelsConvergenceQA:
    """QA tests for /console/channels unified view, tabs, pills, and 307 redirects."""

    @pytest.mark.anyio
    @patch("app.web.routes.DiscordService")
    @patch("app.web.routes.TelegramService")
    @patch("app.web.routes.WechatService")
    async def test_channels_tabs_and_pills_rendering(self, MockWx, MockTg, MockDc):
        """Verify that /console/channels displays status pills, 3 tabs, and panel content."""
        MockWx.return_value.config_summary.return_value = {
            "wechat_token_set": True, "wechat_token_masked": "tok***", "wechat_running": True, "wechat_error": "", "wechat_cursor_length": 5
        }
        MockTg.return_value.config_summary.return_value = {
            "bot_token_set": False, "bot_token_masked": "", "bot_username": "", "bot_running": False, "bot_error": "", "allowed_users": [], "rejected_users": []
        }
        MockDc.return_value.config_summary.return_value = {
            "discord_token_set": False, "discord_token_masked": "", "discord_bot_running": False, "discord_bot_error": "", "discord_application_id": "", "discord_allowed_users": []
        }

        app = _make_app()
        resp = await _get(app, "/console/channels")
        assert resp.status_code == 200

        # Pills
        assert 'id="pill-wechat"' in resp.text
        assert 'id="pill-telegram"' in resp.text
        assert 'id="pill-discord"' in resp.text

        # Tab buttons
        assert 'id="tab-btn-wechat"' in resp.text
        assert 'id="tab-btn-telegram"' in resp.text
        assert 'id="tab-btn-discord"' in resp.text

        # Tab panels
        assert 'id="panel-wechat"' in resp.text
        assert 'id="panel-telegram"' in resp.text
        assert 'id="panel-discord"' in resp.text

    @pytest.mark.anyio
    async def test_307_redirect_matrix_comprehensive(self):
        """Comprehensive verification of 307 temporary redirects and query preserving."""
        app = _make_app()

        # 1. Basic 307 redirects
        resp_wx = await _get(app, "/console/wechat")
        assert resp_wx.status_code == 307
        assert resp_wx.headers["location"] == "/console/channels?tab=wechat"

        resp_tg = await _get(app, "/console/telegram")
        assert resp_tg.status_code == 307
        assert resp_tg.headers["location"] == "/console/channels?tab=telegram"

        resp_dc = await _get(app, "/console/discord")
        assert resp_dc.status_code == 307
        assert resp_dc.headers["location"] == "/console/channels?tab=discord"

        # 2. Query parameter preservation on redirect
        resp_tg_query = await _get(app, "/console/telegram?bind_token=xyz789&bind_link=https%3A%2F%2Ft.me%2Fmybot")
        assert resp_tg_query.status_code == 307
        loc_tg = resp_tg_query.headers["location"]
        assert "/console/channels?" in loc_tg
        assert "tab=telegram" in loc_tg
        assert "bind_token=xyz789" in loc_tg

        resp_wx_query = await _get(app, "/console/wechat?error=scan_timeout&message=try_again")
        assert resp_wx_query.status_code == 307
        loc_wx = resp_wx_query.headers["location"]
        assert "/console/channels?" in loc_wx
        assert "tab=wechat" in loc_wx
        assert "error=scan_timeout" in loc_wx
        assert "message=try_again" in loc_wx

    @pytest.mark.anyio
    @patch("app.web.routes.WechatService")
    @patch("app.web.routes.TelegramService")
    @patch("app.web.routes.DiscordService")
    @patch("app.web.routes.SettingsService")
    async def test_post_form_redirects_redirect_to_channels_tabs(self, MockSettings, MockDc, MockTg, MockWx):
        """Verify channel POST handlers redirect to /console/channels?tab=... with 303."""
        mock_svc = MagicMock()
        mock_svc.get.return_value = None
        mock_svc.get_masked.return_value = ""
        mock_svc.commit = MagicMock()
        MockSettings.return_value = mock_svc

        app = _make_app()

        # Telegram user remove POST
        MockTg.return_value.remove_user = MagicMock()
        resp_tg_remove = await _post_form(app, "/console/telegram/users/remove", {"user_id": "123"})
        assert resp_tg_remove.status_code == 303
        assert resp_tg_remove.headers["location"] == "/console/channels?tab=telegram"

        # Telegram user add POST
        MockTg.return_value.add_user = MagicMock()
        resp_tg_add = await _post_form(app, "/console/telegram/users/add", {"user_id": "456", "username": "qa_user"})
        assert resp_tg_add.status_code == 303
        assert resp_tg_add.headers["location"] == "/console/channels?tab=telegram"

        # Discord save POST
        MockDc.return_value.save_token = MagicMock()
        MockDc.return_value.reload_bot = AsyncMock(return_value=True)
        resp_dc = await _post_form(app, "/console/discord", {"bot_token": "dc_tok_123", "application_id": "app_123"})
        assert resp_dc.status_code == 303
        assert resp_dc.headers["location"] == "/console/channels?tab=discord"

        # WeChat start / stop / clear POST
        MockWx.return_value.reload_bot = AsyncMock(return_value=True)
        MockWx.return_value.stop_bot = AsyncMock(return_value=True)

        resp_wx_start = await _post_form(app, "/console/wechat/start")
        assert resp_wx_start.status_code == 303
        assert resp_wx_start.headers["location"] == "/console/channels?tab=wechat"

        resp_wx_stop = await _post_form(app, "/console/wechat/stop")
        assert resp_wx_stop.status_code == 303
        assert resp_wx_stop.headers["location"] == "/console/channels?tab=wechat"

        resp_wx_clear = await _post_form(app, "/console/wechat/clear")
        assert resp_wx_clear.status_code == 303
        assert resp_wx_clear.headers["location"] == "/console/channels?tab=wechat"

    def test_channels_tab_polling_lifecycle_cleanup(self):
        """Verify channels.html implements startWechatStatusPolling and clearInterval on tab switch."""
        html = Path("app/web/templates/channels.html").read_text(encoding="utf-8")
        assert "function startWechatStatusPolling()" in html
        assert "function stopWechatStatusPolling()" in html
        assert "clearInterval(wechatStatusTimer)" in html
        assert "setInterval" in html
        assert "startTgBindPolling" in html
        assert "stopTgBindPolling" in html
        assert "beforeunload" in html

    def test_sidebar_streamlining_and_wizard_links(self):
        """Verify base.html sidebar has unified 消息渠道 and wizard links point to channels tabs."""
        base_html = Path("app/web/templates/base.html").read_text(encoding="utf-8")
        wizard_html = Path("app/web/templates/wizard.html").read_text(encoding="utf-8")

        # Sidebar has unified /console/channels and no legacy channel items
        assert 'href="/console/channels"' in base_html
        assert "消息渠道" in base_html
        assert 'href="/console/wechat"' not in base_html
        assert 'href="/console/telegram"' not in base_html
        assert 'href="/console/discord"' not in base_html

        # Wizard links point to channels with query param
        assert 'href="/console/channels?tab=wechat"' in wizard_html
        assert 'href="/console/channels?tab=telegram"' in wizard_html
        assert 'href="/console/channels?tab=discord"' in wizard_html


# ============================================================================
# Part 4: Local Preview Sanity Check (严格限制本地开发环境预览，不执行发布与部署)
# ============================================================================

class TestLocalPreviewSanity:
    """Validate that local preview routes and application mount successfully without publication."""

    def test_local_preview_app_routes_registered(self):
        """Verify that core console routes are mounted and accessible in local environment."""
        app = routes.router
        registered_paths = {r.path for r in app.routes if hasattr(r, "path")}

        expected_paths = {
            "/console",
            "/console/caldav",
            "/console/ai",
            "/console/channels",
            "/console/wechat",
            "/console/telegram",
            "/console/discord",
            "/console/connections/{kind}/test",
            "/console/events",
            "/console/system",
        }
        for expected in expected_paths:
            assert expected in registered_paths, f"Expected path {expected} to be registered in local router"

    @pytest.mark.anyio
    async def test_local_preview_all_pages_render_authenticated(self):
        """Verify that CalDAV (Option B), Dashboard (Tray), and Channels all render 200 with layout elements in local preview."""
        app = _make_app(require_auth=True)
        r_caldav = await _get(app, "/console/caldav")
        assert r_caldav.status_code == 200
        assert "settings-layout" in r_caldav.text
        assert "caldav-service-badge" in r_caldav.text

        r_dash = await _get(app, "/console")
        assert r_dash.status_code == 200
        assert "dashboard-verification-tray" in r_dash.text
        assert "service-status-ai" in r_dash.text
        assert "service-status-caldav" in r_dash.text

        r_chan = await _get(app, "/console/channels")
        assert r_chan.status_code == 200
        assert "channel-pills" in r_chan.text
        assert "pill-wechat" in r_chan.text
        assert "tab-btn-wechat" in r_chan.text

    @pytest.mark.anyio
    async def test_local_preview_channels_redirect_flow(self):
        """Verify that following 307 redirects in local environment lands cleanly on /console/channels."""
        app = _make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/console/wechat", follow_redirects=True)
            assert resp.status_code == 200
            assert "消息渠道设置" in resp.text
            assert str(resp.url) == "http://test/console/channels?tab=wechat"
