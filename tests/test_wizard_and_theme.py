import json
from pathlib import Path
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from app.db.models import Base, EventRecord
from app.web.event_presenter import event_feedback
from app.main import create_app


def test_base_html_theme_switcher_and_anti_flicker():
    template = Path("app/web/templates/base.html").read_text()

    # 防闪烁内联脚本在 head 中
    assert "localStorage.getItem('theme')" in template
    assert "document.documentElement.setAttribute('data-theme', theme);" in template

    # 顶部导航栏三态快捷切换按钮
    assert 'class="theme-switcher"' in template
    assert 'data-theme-btn="auto"' in template
    assert 'data-theme-btn="light"' in template
    assert 'data-theme-btn="dark"' in template
    assert "跟随系统" in template
    assert "浅色" in template
    assert "深色" in template

    # 侧边栏包含配置向导入口
    assert 'href="/console/wizard"' in template
    assert "配置向导" in template


def test_styles_css_semantic_variables_and_themes():
    styles = Path("app/web/static/styles.css").read_text()

    # :root 深色语义化变量
    assert ":root {" in styles
    assert "--color-scheme: dark;" in styles
    assert "--bg-app:" in styles
    assert "--bg-surface:" in styles
    assert "--bg-card:" in styles
    assert "--text-primary:" in styles
    assert "--text-regular:" in styles
    assert "--border-base:" in styles

    # 亮色皮肤支持
    assert 'html[data-theme="light"] {' in styles
    assert "--color-scheme: light;" in styles

    # 跟随系统媒体查询
    assert "@media (prefers-color-scheme: light)" in styles
    assert 'html[data-theme="auto"]' in styles

    # 主题切换按钮样式
    assert ".theme-switcher" in styles
    assert ".theme-btn" in styles

    # 失败阶段徽章与重试/复制样式
    assert ".stage-badge" in styles
    assert ".btn-copy-text" in styles
    assert ".btn-retry-write" in styles

    # 向导步进样式
    assert ".wizard-stepper" in styles
    assert ".wizard-step-item" in styles
    assert ".wizard-pane" in styles


def test_dashboard_has_wizard_card_entrance():
    template = Path("app/web/templates/dashboard.html").read_text()

    assert "dashboard-wizard-card" in template
    assert 'href="/console/wizard"' in template
    assert "进入配置向导" in template


def test_events_page_badges_copy_and_retry():
    template = Path("app/web/templates/events.html").read_text()

    # 失败阶段徽章
    assert "stage-badge" in template
    assert "failure_phase" in template or "failure_stage" in template

    # 一键复制原文按钮
    assert "btn-copy-text" in template
    assert "复制原文" in template
    assert "data-copy" in template

    # 重试写入按钮及前端防重即时反馈
    assert "btn-retry-write" in template
    assert "重试写入" in template
    assert "data-record-id" in template
    assert "retry-feedback" in template
    assert "btn.disabled = true" in template


def test_event_presenter_failure_stages_and_retryability():
    # 1. 日历写入失败 -> can_retry = True, 日历写入失败
    write_rec = EventRecord(operation="create", status="failed", error_message="CalDAV 500 error", event_json=json.dumps({"title": "测试会议", "start_time": "2026-10-15T15:00:00"}))
    write_feedback = event_feedback(write_rec)
    assert write_feedback["failure_stage_label"] == "日历写入失败"
    assert write_feedback["can_retry"] is True

    # 2. 校验失败 -> can_retry = False, 校验失败
    val_rec = EventRecord(operation="no_event", status="failed", error_message="缺少字段：时间", event_json=json.dumps({"missing_fields": ["时间"]}))
    val_feedback = event_feedback(val_rec)
    assert val_feedback["failure_stage_label"] == "校验失败"
    assert val_feedback["can_retry"] is False

    # 3. 提取失败 -> can_retry = False, 提取失败
    ext_rec = EventRecord(operation="no_event", status="failed", error_message="AI 调用失败：429 rate limit", event_json=json.dumps({"intent": "no_event", "error_type": "system_error"}))
    ext_feedback = event_feedback(ext_rec)
    assert ext_feedback["failure_stage_label"] == "提取失败"
    assert ext_feedback["can_retry"] is False


def test_wizard_endpoint_renders_four_steps():
    import asyncio
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.models import Base
    from app.web.routes import setup_wizard

    app = create_app()
    client = TestClient(app)

    # 未登录访问向导重定向至登录页
    res_unauth = client.get("/console/wizard", follow_redirects=False)
    assert res_unauth.status_code == 303
    assert "/console/login" in res_unauth.headers.get("location", "")

    # 已登录渲染完整四步
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        request = Request({"type": "http", "method": "GET", "path": "/console/wizard", "headers": [], "query_string": b"", "session": {"admin_authenticated": True}})
        res = asyncio.run(setup_wizard(request, session, _=None))
        html = res.body.decode()

        assert "初次配置向导" in html
        assert "AI 模型设置" in html
        assert "日历服务连接" in html
        assert "选择聊天渠道" in html
        assert "首条测试日程" in html
        assert "wizard-stepper" in html
        assert "wizard-pane-1" in html
        assert "wizard-pane-2" in html
        assert "wizard-pane-3" in html
        assert "wizard-pane-4" in html


def test_wizard_test_event_validation():
    from app.web.routes import require_admin
    app = create_app()
    client = TestClient(app, base_url="http://testserver")

    # 未登录拒绝访问 (403 或 303)
    res_unauth = client.post("/console/wizard/test-event", data={"text": "开会"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert res_unauth.status_code in (401, 403, 303)

    # 已登录为空校验
    app.dependency_overrides[require_admin] = lambda: None
    res_empty = client.post("/console/wizard/test-event", data={"text": "   "}, headers={"origin": "http://testserver"})
    assert res_empty.status_code == 400
    assert "请输入测试日程文本" in res_empty.json()["error"]


def test_dashboard_wizard_card_visibility_based_on_config():
    from app.web import routes
    request = Request({"type": "http", "method": "GET", "path": "/console", "headers": [], "session": {"admin_authenticated": True}})

    base_ctx = {
        "request": request,
        "stats": {"today_processed": 0, "today_created": 0, "today_failed": 0, "today_success_rate": None, "today_no_event": 0, "today_quote_failures": 0, "week_created": 0, "month_created": 0, "today_events": 0, "week_events": 0, "month_events": 0},
        "activity_timezone": "Asia/Shanghai",
        "recent_activity": [],
        "connection_checks": {"ai": {"label": "未验证", "time": ""}, "caldav": {"label": "未验证", "time": ""}},
        "tg_running": False,
        "dc_running": False,
        "wechat_running": False,
        "message": None,
        "changes": None,
        "ai_name": "DeepSeek",
        "vision_label": "共用主模型",
        "caldav_source": "icloud.com",
        "caldav_name": "Work",
        "last_calendar_success_current": "",
        "last_calendar_success_legacy": "",
    }

    # 1. 均未配置 -> 显示向导卡片
    html_none = routes.templates.get_template("dashboard.html").render(ai_ok=False, caldav_ok=False, **base_ctx)
    assert "dashboard-wizard-card" in html_none
    assert "初次配置向导" in html_none

    # 2. 仅 AI 配置完成 -> 仍显示向导卡片
    html_ai_only = routes.templates.get_template("dashboard.html").render(ai_ok=True, caldav_ok=False, **base_ctx)
    assert "dashboard-wizard-card" in html_ai_only

    # 3. 仅日历配置完成 -> 仍显示向导卡片
    html_caldav_only = routes.templates.get_template("dashboard.html").render(ai_ok=False, caldav_ok=True, **base_ctx)
    assert "dashboard-wizard-card" in html_caldav_only

    # 4. 核心配置全部完成 -> 不再显示向导卡片
    html_all_done = routes.templates.get_template("dashboard.html").render(ai_ok=True, caldav_ok=True, **base_ctx)
    assert "dashboard-wizard-card" not in html_all_done
    assert "初次配置向导" not in html_all_done


def test_caldav_and_wizard_dropdown_options():
    from app.web import routes
    request = Request({"type": "http", "method": "GET", "path": "/console/caldav", "headers": [], "session": {"admin_authenticated": True}})

    # caldav.html 包含已保存日历时
    html_saved = routes.templates.get_template("caldav.html").render(
        request=request,
        caldav_url="https://caldav.icloud.com",
        caldav_username="user@icloud.com",
        caldav_password_masked=True,
        caldav_ssl_verify="true",
        caldav_calendar_name="个人日历",
        caldav_calendar_url="https://caldav.icloud.com/cal/1",
        caldav_timezone="Asia/Shanghai",
        caldav_reminder_minutes="30",
        caldav_default_duration="60",
        timezones=["Asia/Shanghai"],
    )
    assert "[当前已保存] 个人日历" in html_saved

    # caldav.html 未配置日历时
    html_unsaved = routes.templates.get_template("caldav.html").render(
        request=request,
        caldav_url="",
        caldav_username="",
        caldav_password_masked=False,
        caldav_ssl_verify="true",
        caldav_calendar_name="",
        caldav_calendar_url="",
        caldav_timezone="Asia/Shanghai",
        caldav_reminder_minutes="30",
        caldav_default_duration="60",
        timezones=["Asia/Shanghai"],
    )
    assert "未配置日历（请先点击上方「拉取日历列表」）" in html_unsaved

    # wizard.html 包含已保存日历时
    wizard_req = Request({"type": "http", "method": "GET", "path": "/console/wizard", "headers": [], "session": {"admin_authenticated": True}})
    wizard_saved = routes.templates.get_template("wizard.html").render(
        request=wizard_req,
        ai={},
        caldav={"caldav_calendar_name": "工作日历", "caldav_calendar_url": "https://caldav.example.com/work", "timezones": []},
        status={},
        ai_ok=True,
        caldav_ok=True,
        telegram_running=False,
        discord_running=False,
        wechat_running=False,
        wechat_state="stopped",
        telegram_bot_token_masked="",
        discord_bot_token_masked="",
        wechat_token_saved=False,
    )
    assert "[当前已保存] 工作日历" in wizard_saved

    # wizard.html 未配置日历时
    wizard_unsaved = routes.templates.get_template("wizard.html").render(
        request=wizard_req,
        ai={},
        caldav={"caldav_calendar_name": "", "caldav_calendar_url": "", "timezones": []},
        status={},
        ai_ok=False,
        caldav_ok=False,
        telegram_running=False,
        discord_running=False,
        wechat_running=False,
        wechat_state="stopped",
        telegram_bot_token_masked="",
        discord_bot_token_masked="",
        wechat_token_saved=False,
    )
    assert "未配置日历（请先点击上方「拉取日历列表」）" in wizard_unsaved


def test_dashboard_route_wizard_card_visibility():
    import asyncio
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.models import Base
    from app.services.settings_service import SettingsService
    from app.web import routes

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        settings_service = SettingsService(session)
        request = Request({"type": "http", "method": "GET", "path": "/console", "headers": [], "query_string": b"", "session": {"admin_authenticated": True}})

        # 1. 均未配置 -> 出现向导卡片，且保留侧边栏入口
        res_unconfigured = asyncio.run(routes.dashboard(request, session, _=None))
        html_unconfigured = res_unconfigured.body.decode()
        assert "dashboard-wizard-card" in html_unconfigured
        assert "初次配置向导" in html_unconfigured
        assert 'href="/console/wizard"' in html_unconfigured

        # 2. 仅 AI 配置完成 -> 仍显示向导卡片
        settings_service.set("ai_provider_name", "OpenAI")
        settings_service.set("ai_model", "gpt-4o")
        res_ai_only = asyncio.run(routes.dashboard(request, session, _=None))
        html_ai_only = res_ai_only.body.decode()
        assert "dashboard-wizard-card" in html_ai_only

        # 3. 核心配置全部完成 (AI + CalDAV) -> 向导卡片隐藏，但侧边栏入口始终保留
        settings_service.set("caldav_url", "https://caldav.example.com")
        settings_service.set("caldav_calendar_name", "Work")
        res_all_done = asyncio.run(routes.dashboard(request, session, _=None))
        html_all_done = res_all_done.body.decode()
        assert "dashboard-wizard-card" not in html_all_done
        assert "初次配置向导" not in html_all_done
        assert 'href="/console/wizard"' in html_all_done
