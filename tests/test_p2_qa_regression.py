import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels.message_processor import ChannelContext, _handle_new, _write_one, _do_delete_with, _do_modify_with
from app.db.models import Base, EventRecord
from app.main import create_app
from app.services.caldav_service import CalDAVServiceError
from app.services.settings_service import SettingsService
from app.web.event_presenter import event_feedback
from app.web.routes import _retry_locks, _retry_mutex, get_db, require_admin, setup_wizard

ORIGIN_HEADERS = {"origin": "http://testserver"}


def _create_test_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return session_factory()


def _override_db(session):
    def _get_db():
        yield session
    return _get_db


# =========================================================================
# 1. 失败收件箱阶段打标单元测试 (Extraction / Validation / Write)
# =========================================================================

def test_failure_phase_tagging_comprehensive():
    """验证 MessageProcessor 处理链路中各类失败分支的精准阶段打标及 EventRecord 字段。"""
    async def _run():
        session = _create_test_session()
        ctx = ChannelContext(source="telegram", source_user_id="user_qa")
        caldav = {"url": "http://caldav.test", "user": "test_user", "pw": "test_pw", "cal": "default", "rem": 30, "dur": 60, "ssl": True}
        svc = SettingsService(session)

        # 1.1 Extraction 阶段：模型 API 报错
        res_api_err = ExtractionResult(intent=Intent.no_event, error_type="RATE_LIMIT_EXCEEDED")
        await _handle_new(session, ctx, "明天开会", res_api_err, caldav, svc)
        rec1 = session.query(EventRecord).filter_by(id=1).first()
        assert rec1.status == "failed"
        assert rec1.failure_phase == "extraction"

        # 1.2 Extraction 阶段：无日程意图
        res_no_event = ExtractionResult(intent=Intent.no_event)
        await _handle_new(session, ctx, "今天天气真好", res_no_event, caldav, svc)
        rec2 = session.query(EventRecord).filter_by(id=2).first()
        assert rec2.status == "failed"
        assert rec2.failure_phase == "extraction"

        # 1.3 Validation 阶段：缺少关键时间字段
        res_missing = ExtractionResult(intent=Intent.create_event, missing_fields=["时间"])
        await _handle_new(session, ctx, "安排技术方案评审", res_missing, caldav, svc)
        rec3 = session.query(EventRecord).filter_by(id=3).first()
        assert rec3.status == "failed"
        assert rec3.failure_phase == "validation"

        # 1.4 Validation 阶段：不支持的复杂重复规则
        res_unsupported = ExtractionResult(intent=Intent.create_event, unsupported_reason="每三周的周二和周四")
        await _handle_new(session, ctx, "定期开会", res_unsupported, caldav, svc)
        rec4 = session.query(EventRecord).filter_by(id=4).first()
        assert rec4.status == "failed"
        assert rec4.failure_phase == "validation"

        # 1.5 Write 阶段：新建日程 CalDAV 写入失败
        event = CalendarEvent(title="P2 优化验收会", start_time="2026-10-20T10:00:00")
        with patch("app.channels.message_processor._write_caldav", side_effect=CalDAVServiceError("Connection refused")):
            rec_id, ok = await _write_one(session, ctx, "2026-10-20上午10点P2优化验收会", event, caldav)
            session.commit()
            assert ok is False
            rec5 = session.query(EventRecord).filter_by(id=rec_id).first()
            assert rec5.status == "failed"
            assert rec5.failure_phase == "write"
            assert rec5.caldav_uid is not None
            assert rec5.retry_count == 0

        # 1.6 Write 阶段：修改日程 CalDAV 写入失败
        target = EventRecord(
            operation="create", status="success", title="旧日程",
            caldav_uid="uid-orig-10", caldav_href="/cal/10.ics",
            event_json=json.dumps({"title": "旧日程", "start_time": "2026-10-20T10:00:00"}),
        )
        session.add(target)
        session.commit()
        with patch("app.channels.message_processor._write_caldav_dict", side_effect=CalDAVServiceError("Write error")):
            rec_mod_id, warn = await _do_modify_with(
                session, ctx, "把时间改到下午3点", target,
                {"title": "旧日程", "start_time": "2026-10-20T15:00:00"}, caldav
            )
            session.commit()
            rec_mod = session.query(EventRecord).filter_by(id=rec_mod_id).first()
            assert rec_mod.status == "failed"
            assert rec_mod.failure_phase == "write"

        # 1.7 Write 阶段：删除日程 CalDAV 删除失败
        target_del = EventRecord(
            operation="create", status="success", title="待删日程",
            caldav_uid="uid-orig-11", caldav_href="/cal/11.ics",
            event_json=json.dumps({"title": "待删日程"}),
        )
        session.add(target_del)
        session.commit()
        with patch("app.services.caldav_service.CalDAVService.delete_event", AsyncMock(return_value=False)):
            msg = await _do_delete_with(session, ctx, target_del, caldav)
            assert "CalDAV 删除失败" in msg
            rec_del = session.query(EventRecord).filter_by(operation="delete").first()
            assert rec_del.status == "failed"
            assert rec_del.failure_phase == "write"

    asyncio.run(_run())


# =========================================================================
# 2. Web 重试并发锁、非法重试拦截与幂等防重机制测试
# =========================================================================

def test_retry_validation_and_illegal_rejections():
    """测试重试接口对各类非法状态的严密拦截。"""
    app = create_app()
    session = _create_test_session()
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db] = _override_db(session)
    client = TestClient(app)

    # 2.1 404: 记录不存在
    r_404 = client.post("/console/events/99999/retry", headers=ORIGIN_HEADERS)
    assert r_404.status_code == 404
    assert "未找到" in r_404.json()["error"]

    # 2.2 400: 已成功的记录不允许重试
    succ_rec = EventRecord(operation="create", status="success", title="成功日程", event_json='{"title":"ok"}')
    session.add(succ_rec)
    session.commit()
    r_succ = client.post(f"/console/events/{succ_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert r_succ.status_code == 400
    assert "无需重试" in r_succ.json()["error"]

    # 2.3 400: extraction 阶段不允许重试
    ext_rec = EventRecord(operation="no_event", status="failed", failure_phase="extraction", title=None, event_json='{}')
    session.add(ext_rec)
    session.commit()
    r_ext = client.post(f"/console/events/{ext_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert r_ext.status_code == 400
    assert "仅支持日历写入阶段" in r_ext.json()["error"]

    # 2.4 400: validation 阶段不允许重试
    val_rec = EventRecord(operation="no_event", status="failed", failure_phase="validation", title=None, event_json='{}')
    session.add(val_rec)
    session.commit()
    r_val = client.post(f"/console/events/{val_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert r_val.status_code == 400
    assert "仅支持日历写入阶段" in r_val.json()["error"]

    # 2.5 400: delete 操作不允许重试
    del_rec = EventRecord(operation="delete", status="failed", failure_phase="write", title="删除失败项", event_json='{"title":"待删"}')
    session.add(del_rec)
    session.commit()
    r_del = client.post(f"/console/events/{del_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert r_del.status_code == 400
    assert "仅支持日历写入阶段" in r_del.json()["error"]

    # 2.6 400: 缺少 event_json 或 json 格式损坏或缺少标题
    corrupt_rec = EventRecord(operation="create", status="failed", failure_phase="write", title="损坏项", event_json='{invalid-json}')
    session.add(corrupt_rec)
    session.commit()
    r_corrupt = client.post(f"/console/events/{corrupt_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert r_corrupt.status_code == 400
    assert "解析日程数据失败" in r_corrupt.json()["error"]

    notitle_rec = EventRecord(operation="create", status="failed", failure_phase="write", title="无标题项", event_json='{"start_time":"2026-10-10"}')
    session.add(notitle_rec)
    session.commit()
    r_notitle = client.post(f"/console/events/{notitle_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert r_notitle.status_code == 400
    assert "缺少标题" in r_notitle.json()["error"]

    # 2.7 400: 未配置有效的 CalDAV
    valid_rec = EventRecord(operation="create", status="failed", failure_phase="write", title="待重试", event_json='{"title":"开会","start_time":"2026-10-10T10:00:00"}')
    session.add(valid_rec)
    session.commit()
    r_nocal = client.post(f"/console/events/{valid_rec.id}/retry", headers=ORIGIN_HEADERS)
    assert r_nocal.status_code == 400
    assert "尚未配置有效的日历连接" in r_nocal.json()["error"]


def test_retry_concurrency_lock_and_release():
    """测试重试并发锁竞争机制与 finally 保证的锁释放。"""
    async def _run():
        app = create_app()
        session = _create_test_session()
        app.dependency_overrides[require_admin] = lambda: None
        app.dependency_overrides[get_db] = lambda: session
        client = TestClient(app)

        rec = EventRecord(
            id=88, operation="create", status="failed", failure_phase="write",
            title="并发锁测试", event_json='{"title":"并发锁测试","start_time":"2026-10-20T10:00:00"}'
        )
        session.add(rec)
        session.commit()

        # 手动上锁模拟并发竞争
        async with _retry_mutex:
            _retry_locks.add(88)

        res = client.post("/console/events/88/retry", headers=ORIGIN_HEADERS)
        assert res.status_code == 409
        assert "正在重试写入中" in res.json()["error"]

        # 释放锁
        async with _retry_mutex:
            _retry_locks.discard(88)

        # 再次请求，由于未配置 CalDAV，应越过并发锁并返回 400（证明锁已释放，不再返回 409）
        res2 = client.post("/console/events/88/retry", headers=ORIGIN_HEADERS)
        assert res2.status_code == 400
        assert "尚未配置有效的日历连接" in res2.json()["error"]

    asyncio.run(_run())


def test_retry_state_machine_success_idempotency():
    """验证重试成功时状态机原子更新：caldav_uid 幂等保留、retry_count 自增、错误信息清空、配置版本记录。"""
    app = create_app()
    session = _create_test_session()
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db] = _override_db(session)
    client = TestClient(app)

    settings_svc = SettingsService(session)
    settings_svc.set("caldav_url", "http://caldav.example.com")
    settings_svc.set("caldav_username", "admin_user")
    settings_svc.set("caldav_password", "admin_pass")
    settings_svc.commit()
    expected_config_version = settings_svc.get_config_version()

    original_uid = "idempotent-uuid-999"
    rec = EventRecord(
        operation="create",
        status="failed",
        failure_phase="write",
        title="架构方案研讨",
        start_time="2026-10-21T14:00:00",
        caldav_uid=original_uid,
        event_json=json.dumps({
            "title": "架构方案研讨",
            "start_time": "2026-10-21T14:00:00",
            "end_time": "2026-10-21T15:00:00",
            "reminders": [{"minutes_before": 10}],
        }),
        error_message="CalDAV 503 临时不可用",
        retry_count=0,
    )
    session.add(rec)
    session.commit()
    rec_id = rec.id

    mock_caldav = AsyncMock(return_value={"uid": original_uid, "href": "/caldav/event-999.ics"})
    with patch("app.services.caldav_service.CalDAVService.create_event", mock_caldav):
        res = client.post(f"/console/events/{rec_id}/retry", headers=ORIGIN_HEADERS)
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["data"]["status"] == "success"
        assert body["data"]["retry_count"] == 1
        assert body["data"]["caldav_uid"] == original_uid

        # 核对 mock 调用使用了相同的幂等 uid
        _, kwargs = mock_caldav.call_args
        assert kwargs.get("uid") == original_uid

        # 刷新数据库验证持久化
        session.refresh(rec)
        assert rec.status == "success"
        assert rec.failure_phase is None
        assert rec.error_message is None
        assert rec.retry_count == 1
        assert rec.caldav_uid == original_uid
        assert rec.caldav_href == "/caldav/event-999.ics"
        assert rec.config_version == expected_config_version


# =========================================================================
# 3. 配置向导接口与首条测试日程端到端测试
# =========================================================================

def test_wizard_endpoint_and_test_event():
    """测试初次配置向导路由与首条日程端到端测试接口。"""
    app = create_app()
    session = _create_test_session()
    client = TestClient(app)

    # 3.1 未认证重定向
    res_unauth = client.get("/console/wizard", follow_redirects=False)
    assert res_unauth.status_code == 303
    assert "/console/login" in res_unauth.headers.get("location", "")

    res_post_unauth = client.post("/console/wizard/test-event", json={"text": "开会"}, headers=ORIGIN_HEADERS, follow_redirects=False)
    assert res_post_unauth.status_code in (303, 401, 403)

    # 3.2 认证通过渲染向导
    req_auth = Request({
        "type": "http",
        "method": "GET",
        "path": "/console/wizard",
        "headers": [],
        "query_string": b"",
        "session": {"admin_authenticated": True},
    })
    res_auth = asyncio.run(setup_wizard(req_auth, session, _=None))
    assert res_auth.status_code == 200
    html = res_auth.body.decode("utf-8")
    assert "初次配置向导" in html
    assert "wizard-stepper" in html
    assert "wizard-pane-1" in html
    assert "wizard-pane-4" in html

    # 3.3 认证后测试日程接口参数边界：空字符
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db] = _override_db(session)

    res_empty = client.post("/console/wizard/test-event", json={"text": "   "}, headers=ORIGIN_HEADERS)
    assert res_empty.status_code == 400
    assert "请输入测试日程文本" in res_empty.json()["error"]

    # 3.4 首条日程生成端到端（Form 与 JSON 两种传参方式均支持）
    mock_process = AsyncMock(return_value=[("✅ 日程已安排好啦！\n📌 标题：向导测试会议", 101)])
    rec_test = EventRecord(
        id=101, source="wizard", operation="create", status="success",
        title="向导测试会议", start_time="2026-10-22T09:00:00",
    )
    session.add(rec_test)
    session.commit()

    with patch("app.channels.message_processor.MessageProcessor.process", mock_process):
        # JSON 载荷
        res_json = client.post("/console/wizard/test-event", json={"text": "下周一上午9点向导测试会议"}, headers=ORIGIN_HEADERS)
        assert res_json.status_code == 200
        data_j = res_json.json()
        assert data_j["ok"] is True
        assert data_j["data"]["record_id"] == 101
        assert "向导测试会议" in data_j["data"]["title"]
        assert data_j["data"]["source"] == "wizard"

        # Form 载荷
        res_form = client.post("/console/wizard/test-event", data={"text": "下周一上午9点向导测试会议"}, headers=ORIGIN_HEADERS)
        assert res_form.status_code == 200
        data_f = res_form.json()
        assert data_f["ok"] is True
        assert data_f["data"]["record_id"] == 101


# =========================================================================
# 4. 亮暗色主题、CSS 变量规范与模板属性校验
# =========================================================================

def test_theme_and_css_specifications():
    """校验 CSS 变量多主题成对规范、防闪烁逻辑及 HTML 元素属性。"""
    styles = Path("app/web/static/styles.css").read_text(encoding="utf-8")
    base_html = Path("app/web/templates/base.html").read_text(encoding="utf-8")
    events_html = Path("app/web/templates/events.html").read_text(encoding="utf-8")

    # 4.1 CSS 变量必须在 :root 与 html[data-theme="light"] 均具备成对定义
    critical_vars = [
        "--color-scheme",
        "--bg-app",
        "--bg-surface",
        "--bg-card",
        "--bg-card-hover",
        "--text-primary",
        "--text-regular",
        "--text-secondary",
        "--text-muted",
        "--border-base",
        "--border-card",
        "--input-bg",
        "--input-border",
        "--btn-secondary-bg",
        "--btn-secondary-color",
        "--link-color",
    ]
    for var in critical_vars:
        assert f"{var}:" in styles, f"缺少变量 {var} 的定义"

    # 4.2 校验亮暗色与跟随系统选择器
    assert ':root {' in styles
    assert 'html[data-theme="light"] {' in styles
    assert '@media (prefers-color-scheme: light)' in styles
    assert 'html[data-theme="auto"]' in styles

    # 4.3 校验防闪烁脚本必须在 base.html 的 head 中
    assert "localStorage.getItem('theme')" in base_html
    assert "setAttribute('data-theme', theme)" in base_html

    # 4.4 校验 theme-switcher 包含 auto, light, dark 三态
    assert 'data-theme-btn="auto"' in base_html
    assert 'data-theme-btn="light"' in base_html
    assert 'data-theme-btn="dark"' in base_html

    # 4.5 校验 events.html 包含失败徽章与重试/复制功能
    assert "stage-badge" in events_html
    assert "btn-copy-text" in events_html
    assert "btn-retry-write" in events_html
    assert "data-record-id" in events_html
    assert "btn.disabled = true" in events_html
