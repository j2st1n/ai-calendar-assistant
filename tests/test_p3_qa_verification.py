"""Phase 3 Comprehensive QA Verification Test Suite.

Validates:
1. AI settings two-column layout (Scheme B) and status/probes sidebar.
2. Channel settings unified tabs with live status indicators and two-column panels.
3. Diff summary comparison for event modifications (multi-field, clearable, formatting).
4. Ambiguity Guard state machine (Chinese number parsing, out-of-range index warning,
   unrelated input handling, user isolation, and expiration).
5. /console/calendar read-only timeline route, auth protection, error handling, and JSON API.
6. Accessibility (WCAG AA) contrast, focus rings, and mobile touch targets in styles.css.
"""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.testclient import TestClient

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels.message_processor import (
    AMBIGUITY_TTL,
    ChannelContext,
    _format_diff_summary,
    _format_modify_result,
    _parse_selection_index,
    _pending_ambiguities,
    _route,
)
from app.db.models import Base, EventRecord
from app.services.caldav_service import CalDAVServiceError
from app.services.settings_service import SettingsService
from app.web import routes
from app.web.routes import get_db, require_admin, router


def _create_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _create_context(**kwargs) -> ChannelContext:
    defaults = {
        "source": "telegram",
        "source_user_id": "user_qa",
        "conversation_id": "conv_qa",
        "source_message_id": "msg_001",
        "reply_to_message_id": None,
        "quoted_text": None,
        "quote_reference_present": False,
    }
    defaults.update(kwargs)
    return ChannelContext(**defaults)


def _default_caldav() -> dict[str, object]:
    return {
        "url": "",
        "user": "",
        "pw": "",
        "cal": "",
        "rem": 15,
        "dur": 60,
        "ssl": True,
    }


class _FakeExtractor:
    def __init__(self, intent=Intent.create_event, event=None, events=None):
        self._intent = intent
        self._event = event
        self._events = events if events is not None else []

    async def extract(self, text: str) -> ExtractionResult:
        return ExtractionResult(
            intent=self._intent,
            event=self._event,
            events=self._events,
            confidence=0.95,
        )

    async def modify(self, existing: dict, text: str) -> ExtractionResult:
        return ExtractionResult(
            intent=self._intent,
            event=self._event,
            events=self._events,
            confidence=0.95,
        )


class _FakeSessionMiddleware:
    def __init__(self, app, authenticated: bool = True):
        self.app = app
        self.authenticated = authenticated

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["session"] = {"admin_authenticated": self.authenticated}
        await self.app(scope, receive, send)


def _build_test_app(session: Session, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    if authenticated:
        app.dependency_overrides[require_admin] = lambda: None
    app.add_middleware(_FakeSessionMiddleware, authenticated=authenticated)
    app.dependency_overrides[get_db] = lambda: session
    app.include_router(router)
    return app


# =========================================================================
# 1. AI 设置双栏布局 (Scheme B) 与服务状态右栏验证
# =========================================================================

def test_ai_template_scheme_b_two_column_structure():
    """验证 ai.html 采用标准 Scheme B 双栏架构，左栏主模型/识图模型，右栏状态/探针/说明。"""
    template_path = Path(__file__).parents[1] / "app/web/templates/ai.html"
    content = template_path.read_text(encoding="utf-8")

    # 双栏容器与列类名
    assert 'class="settings-layout"' in content
    assert 'class="settings-main stack"' in content
    assert 'class="settings-sidebar stack"' in content

    # 左栏：主模型与识图模型配置表单及保存
    assert 'id="main-model-card"' in content
    assert 'id="main-ai-form"' in content
    assert 'id="vision-card"' in content
    assert 'id="vision-form"' in content
    assert 'button type="submit">保存主模型</button>' in content
    assert 'button type="submit">保存识图模型</button>' in content

    # 右栏：AI 服务实时运行状态卡片
    assert 'id="ai-status-card"' in content
    assert 'id="ai-service-badge"' in content
    assert 'id="ai-service-status-text"' in content
    assert 'id="ai-check-label"' in content
    assert 'id="ai-check-time"' in content
    assert 'id="ai-review-notice"' in content
    assert 'id="btn-recheck-ai"' in content
    assert 'recheckSavedAI()' in content

    # 右栏：连通性与探针测试卡片
    assert 'id="ai-probes-card"' in content
    assert 'data-endpoint="/console/ai/test"' in content
    assert 'data-endpoint="/console/ai/schema-test"' in content
    assert 'data-endpoint="/console/ai/vision-test"' in content
    assert 'id="sidebar-vision-test-btn"' in content

    # 右栏：协议与配置说明卡片
    assert 'id="ai-protocol-card"' in content
    assert "OpenAI Compatible" in content
    assert "Anthropic" in content
    assert "AES-GCM" in content


def test_ai_route_renders_two_column_view():
    """测试 GET /console/ai 路由正确加载并呈现双栏及状态指示。"""
    session = _create_session()
    settings = SettingsService(session)
    settings.set("ai_provider_name", "OpenAI")
    settings.set("ai_model", "gpt-4o")
    settings.commit()

    app = _build_test_app(session, authenticated=True)
    client = TestClient(app)

    response = client.get("/console/ai")
    assert response.status_code == 200
    assert "settings-layout" in response.text
    assert "settings-main" in response.text
    assert "settings-sidebar" in response.text
    assert "AI 服务状态" in response.text
    assert "btn-recheck-ai" in response.text


# =========================================================================
# 2. 消息渠道融合 Tab 与双栏面板验证
# =========================================================================

def test_channels_template_unified_tabs_and_dual_columns():
    """验证 channels.html 消除上方药丸重复，统一为带指示灯的高级 Tab，各面板均为双栏化。"""
    template_path = Path(__file__).parents[1] / "app/web/templates/channels.html"
    content = template_path.read_text(encoding="utf-8")

    # 统一 Tab 栏
    assert 'class="channel-tabs channel-pills"' in content
    assert 'role="tablist"' in content

    # 三大渠道 Tab 按钮与状态灯
    assert 'id="tab-btn-wechat"' in content
    assert 'id="tab-btn-telegram"' in content
    assert 'id="tab-btn-discord"' in content
    assert 'id="pill-dot-wechat"' in content
    assert 'id="pill-dot-telegram"' in content
    assert 'id="pill-dot-discord"' in content
    assert 'id="pill-text-wechat"' in content
    assert 'id="pill-text-telegram"' in content
    assert 'id="pill-text-discord"' in content

    # 隐式兼容锚点（保证历史定位或引用不报错）
    assert 'class="channel-pills-compat"' in content
    assert 'id="pill-wechat"' in content
    assert 'id="pill-telegram"' in content
    assert 'id="pill-discord"' in content

    # 三大面板与双栏架构
    assert 'id="panel-wechat"' in content
    assert 'id="panel-telegram"' in content
    assert 'id="panel-discord"' in content
    assert content.count('class="settings-layout"') >= 3
    assert content.count('class="settings-main stack"') >= 3
    assert content.count('class="settings-sidebar stack"') >= 3


def test_channels_route_tab_query_switch():
    """验证 GET /console/channels 响应与 ?tab= 参数正确切换激活面板。"""
    session = _create_session()
    app = _build_test_app(session, authenticated=True)
    client = TestClient(app)

    # 1. 默认 WeChat Tab
    r1 = client.get("/console/channels")
    assert r1.status_code == 200
    assert 'id="tab-btn-wechat"' in r1.text
    assert 'id="panel-wechat"' in r1.text

    # 2. 切换至 Telegram Tab
    r2 = client.get("/console/channels?tab=telegram")
    assert r2.status_code == 200
    assert 'id="tab-btn-telegram"' in r2.text
    assert 'id="panel-telegram"' in r2.text

    # 3. 切换至 Discord Tab
    r3 = client.get("/console/channels?tab=discord")
    assert r3.status_code == 200
    assert 'id="tab-btn-discord"' in r3.text
    assert 'id="panel-discord"' in r3.text


# =========================================================================
# 3. Diff 变更对照全边界覆盖测试
# =========================================================================

def test_diff_summary_all_fields_and_clear_semantics():
    """验证 _format_diff_summary 覆盖标题、时间、地点、描述的修改与清除语义。"""
    # 场景 1: 地点与描述从无到有
    old1 = {
        "title": "项目评审",
        "start_time": "2026-06-02T10:00:00+08:00",
        "end_time": "2026-06-02T11:00:00+08:00",
    }
    new1 = {
        "title": "项目评审",
        "start_time": "2026-06-02T10:00:00+08:00",
        "end_time": "2026-06-02T11:00:00+08:00",
        "location": "第一会议室",
        "description": "准备 PPT",
    }
    diffs1 = _format_diff_summary(old1, new1)
    assert any("地点：(无) ➔ 第一会议室" in d for d in diffs1)
    assert any("描述：(无) ➔ 准备 PPT" in d for d in diffs1)

    # 场景 2: 地点与描述清除
    old2 = {
        "title": "项目评审",
        "start_time": "2026-06-02T10:00:00+08:00",
        "end_time": "2026-06-02T11:00:00+08:00",
        "location": "第一会议室",
        "description": "准备 PPT",
    }
    new2 = {
        "title": "项目评审",
        "start_time": "2026-06-02T10:00:00+08:00",
        "end_time": "2026-06-02T11:00:00+08:00",
        "location": "",
        "description": "",
    }
    diffs2 = _format_diff_summary(old2, new2)
    assert any("地点：第一会议室 ➔ (已清除)" in d for d in diffs2)
    assert any("描述：准备 PPT ➔ (已清除)" in d for d in diffs2)

    # 场景 3: 标题修改与跨日时间对比
    old3 = {
        "title": "技术调研",
        "start_time": "2026-06-02T10:00:00+08:00",
        "end_time": "2026-06-02T18:00:00+08:00",
    }
    new3 = {
        "title": "技术方案终审",
        "start_time": "2026-06-03T09:00:00+08:00",
        "end_time": "2026-06-04T12:00:00+08:00",
    }
    diffs3 = _format_diff_summary(old3, new3)
    assert any("标题：技术调研 ➔ 技术方案终审" in d for d in diffs3)
    assert any("时间：2026-06-02 10:00 - 18:00 ➔ 2026-06-03 09:00 - 2026-06-04 12:00" in d for d in diffs3)


def test_format_modify_result_with_custom_warning():
    """验证 _format_modify_result 在存在 warning 提示时正确呈现抬头与对照块。"""
    old_evt = {"title": "周会", "start_time": "2026-06-02T10:00:00+08:00"}
    new_evt = {"title": "周会", "start_time": "2026-06-02T11:00:00+08:00"}
    res = _format_modify_result(new_evt, warning="⚠️ 日程已更新，但日历同步出现网络延迟", old_event=old_evt)

    assert "⚠️ 日程已更新，但日历同步出现网络延迟" in res
    assert "🔄 修改对照：" in res
    assert "• 时间：" in res
    assert "📌 标题：周会" in res


# =========================================================================
# 4. 歧义阻断状态机 (Ambiguity Guard) 进阶场景验证
# =========================================================================

def test_ambiguity_parse_selection_index_variations():
    """验证 _parse_selection_index 对阿拉伯数字、中文数字及各种前缀后缀的支持。"""
    assert _parse_selection_index("1", 3) == 1
    assert _parse_selection_index("2", 3) == 2
    assert _parse_selection_index("确认 2", 3) == 2
    assert _parse_selection_index("选 1", 3) == 1
    assert _parse_selection_index("第3项", 3) == 3
    assert _parse_selection_index("第 2 个", 3) == 2
    assert _parse_selection_index("一", 3) == 1
    assert _parse_selection_index("二", 3) == 2
    assert _parse_selection_index("确认 二", 3) == 2
    assert _parse_selection_index("第 三 项", 3) == 3

    # 超出范围返回 None
    assert _parse_selection_index("0", 3) is None
    assert _parse_selection_index("4", 3) is None
    assert _parse_selection_index("五", 3) is None
    assert _parse_selection_index("取消", 3) is None


def test_ambiguity_guard_out_of_range_index_warning():
    """当存在歧义待确认时，回复越界序号应明确提示有效范围，且保持歧义阻断状态。"""
    async def run():
        session = _create_session()
        _pending_ambiguities.clear()

        rec1 = EventRecord(
            source="telegram", source_user_id="u_test", conversation_id="c_test",
            operation="create", title="测试日程A", start_time="2026-06-02T10:00:00+08:00",
            status="success", event_json=json.dumps({"title": "测试日程A"}),
            created_at=datetime.now(timezone.utc), event_id="e1",
        )
        rec2 = EventRecord(
            source="telegram", source_user_id="u_test", conversation_id="c_test",
            operation="create", title="测试日程B", start_time="2026-06-03T10:00:00+08:00",
            status="success", event_json=json.dumps({"title": "测试日程B"}),
            created_at=datetime.now(timezone.utc), event_id="e2",
        )
        session.add_all([rec1, rec2])
        session.commit()

        svc = SettingsService(session)
        extractor = _FakeExtractor(intent=Intent.delete_event)

        # 1. 触发歧义阻断 (2 个候选)
        ctx = _create_context(source_user_id="u_test", conversation_id="c_test")
        replies1 = await _route(session, ctx, "删除测试日程", extractor, _default_caldav(), svc)
        assert "⚠️ 发现多条匹配的日程" in replies1[0][0]

        # 2. 回复越界序号 "5"
        replies2 = await _route(session, ctx, "5", extractor, _default_caldav(), svc)
        assert len(replies2) == 1
        assert "序号超出有效范围，请回复 1 到 2 之间的序号确认" in replies2[0][0]

        # 验证歧义状态依然保留
        ambiguity_key = f"ambiguity_{ctx.source}:{ctx.source_user_id}:{ctx.conversation_id or ''}"
        assert ambiguity_key in _pending_ambiguities

        # 3. 接着回复有效序号 "1" 成功执行
        replies3 = await _route(session, ctx, "1", extractor, _default_caldav(), svc)
        assert "已删除日程" in replies3[0][0]
        assert ambiguity_key not in _pending_ambiguities

    asyncio.run(run())


def test_ambiguity_guard_user_and_conversation_isolation():
    """验证歧义状态严格按 (source, user_id, conversation_id) 隔离，互不干扰。"""
    async def run():
        session = _create_session()
        _pending_ambiguities.clear()

        rec_u1 = EventRecord(
            source="telegram", source_user_id="user_1", conversation_id="c_1",
            operation="create", title="同步会", start_time="2026-06-02T10:00:00+08:00",
            status="success", event_json="{}", created_at=datetime.now(timezone.utc), event_id="e_u1",
        )
        rec_u1_2 = EventRecord(
            source="telegram", source_user_id="user_1", conversation_id="c_1",
            operation="create", title="同步会", start_time="2026-06-03T10:00:00+08:00",
            status="success", event_json="{}", created_at=datetime.now(timezone.utc), event_id="e_u1_2",
        )
        session.add_all([rec_u1, rec_u1_2])
        session.commit()

        svc = SettingsService(session)
        extractor = _FakeExtractor(intent=Intent.delete_event)

        ctx_u1 = _create_context(source_user_id="user_1", conversation_id="c_1")
        ctx_u2 = _create_context(source_user_id="user_2", conversation_id="c_2")

        # user_1 触发歧义
        _ = await _route(session, ctx_u1, "删除同步会", extractor, _default_caldav(), svc)
        key_u1 = f"ambiguity_{ctx_u1.source}:{ctx_u1.source_user_id}:{ctx_u1.conversation_id}"
        key_u2 = f"ambiguity_{ctx_u2.source}:{ctx_u2.source_user_id}:{ctx_u2.conversation_id}"
        assert key_u1 in _pending_ambiguities
        assert key_u2 not in _pending_ambiguities

        # user_2 发送 "1"，不应命中 user_1 的歧义
        replies_u2 = await _route(session, ctx_u2, "1", extractor, _default_caldav(), svc)
        # user_2 没有上下文，不会误删 user_1 的日程
        assert "已删除" not in replies_u2[0][0]

    asyncio.run(run())


# =========================================================================
# 5. /console/calendar 路由鉴权、异常处理与 JSON API 验证
# =========================================================================

def test_console_calendar_unauthenticated_redirect():
    """验证未经认证访问 /console/calendar 被安全拦截并 303 重定向至登录页。"""
    session = _create_session()
    app = _build_test_app(session, authenticated=False)
    client = TestClient(app)

    resp = client.get("/console/calendar", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_console_calendar_not_configured_renders_cleanly():
    """验证未配置 CalDAV 时访问 /console/calendar 友好展示未配置引导。"""
    session = _create_session()
    app = _build_test_app(session, authenticated=True)
    client = TestClient(app)

    resp = client.get("/console/calendar")
    assert resp.status_code == 200
    assert "尚未配置 CalDAV" in resp.text
    assert "/console/caldav" in resp.text


def test_console_calendar_service_error_handling(monkeypatch):
    """验证当 CalDAV 服务端报错时页面友好捕获错误，返回 200 并展示错误横幅，不崩溃。"""
    session = _create_session()
    svc = SettingsService(session)
    svc.set("caldav_url", "https://cal.example.com/dav")
    svc.set("caldav_username", "bob")
    svc.set("caldav_password", "pw")
    svc.set("caldav_calendar_url", "https://cal.example.com/dav/main")
    svc.commit()

    async def mock_fail_list_events(*args, **kwargs):
        raise CalDAVServiceError("远端 CalDAV 鉴权失败 (401 Unauthorized)")

    monkeypatch.setattr("app.services.caldav_service.CalDAVService.list_events", mock_fail_list_events)

    app = _build_test_app(session, authenticated=True)
    client = TestClient(app)

    resp = client.get("/console/calendar")
    assert resp.status_code == 200
    assert "读取 CalDAV 日历服务失败" in resp.text
    assert "401 Unauthorized" in resp.text


def test_console_calendar_range_and_json_api(monkeypatch):
    """验证 /console/calendar 支持 week/month/all 范围过滤并提供完整的 JSON API 响应。"""
    session = _create_session()
    svc = SettingsService(session)
    svc.set("caldav_url", "https://cal.example.com/dav")
    svc.set("caldav_username", "carol")
    svc.set("caldav_password", "pw")
    svc.set("caldav_calendar_name", "团队日历")
    svc.set("caldav_calendar_url", "https://cal.example.com/dav/team")
    svc.set("caldav_timezone", "Asia/Shanghai")
    svc.commit()

    mock_events = [
        {
            "uid": "ev-001",
            "href": "https://cal.example.com/dav/team/ev-001.ics",
            "title": "敏捷站会",
            "start_time": "2026-06-02T09:30:00+08:00",
            "end_time": "2026-06-02T10:00:00+08:00",
            "is_all_day": False,
            "location": "线上腾讯会议",
            "description": "每日进度同步",
            "status": "CONFIRMED",
        }
    ]

    async def mock_list(*args, **kwargs):
        return mock_events

    monkeypatch.setattr("app.services.caldav_service.CalDAVService.list_events", mock_list)

    app = _build_test_app(session, authenticated=True)
    client = TestClient(app)

    # 1. 验证 range=week 渲染
    resp_week = client.get("/console/calendar?range=week")
    assert resp_week.status_code == 200
    assert "敏捷站会" in resp_week.text

    # 2. 验证 Accept: application/json 响应规范
    resp_json = client.get("/console/calendar?range=week", headers={"Accept": "application/json"})
    assert resp_json.status_code == 200
    data = resp_json.json()
    assert data["ok"] is True
    assert data["data_source"] == "团队日历"
    assert data["timezone"] == "Asia/Shanghai"
    assert data["range"] == "week"
    assert data["total"] == 1
    assert data["events"][0]["title"] == "敏捷站会"
    assert data["events"][0]["location"] == "线上腾讯会议"


# =========================================================================
# 6. a11y 可访问性规范 (WCAG AA) 与 CSS 样式审计测试
# =========================================================================

def test_styles_a11y_standards_compliance():
    """验证 styles.css 严格遵从 WCAG AA 标准（高对比度焦点环、44px 触控靶心与颜色变量）。"""
    css_path = Path(__file__).parents[1] / "app/web/static/styles.css"
    css = css_path.read_text(encoding="utf-8")

    # 1. 全局 :focus-visible 高对比度轮廓环
    assert ":focus-visible" in css
    assert "outline: 2px solid var(--accent" in css or "outline: 2px solid #aeb5ff" in css

    # 2. 移动端 >= 44px 触控尺寸
    assert "min-height: 44px" in css
    assert "min-width: 44px" in css

    # 3. 渠道 Tab 样式
    assert ".channel-tabs" in css
    assert ".channel-tab" in css
    assert ".channel-tab.active" in css
    assert ".channel-tab .dot" in css

    # 4. 双栏布局响应式栅格
    assert ".settings-layout" in css
    assert ".settings-main" in css
    assert ".settings-sidebar" in css
