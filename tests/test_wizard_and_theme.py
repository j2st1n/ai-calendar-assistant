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
