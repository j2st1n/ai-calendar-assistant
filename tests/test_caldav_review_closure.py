import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.core.config import settings
from app.db.models import Base, EventRecord
from app.services.settings_service import SettingsService
from app.web import routes
from app.web.connection_checks import check_summary, revision, save_check


@pytest.fixture
def db_session():
    settings.app_secret_key = "test-secret-key-32-characters-len"
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


# ==============================================================================
# 1. 保存配置后签名自动同步与消除误报 (Signature Auto-Sync on Save)
# ==============================================================================

def test_caldav_save_with_unchanged_credentials_auto_syncs_signature(db_session):
    """
    场景 1：已验证的 CalDAV 配置在更新非凭据字段（如时区、日历名、时长、提醒）或留空密码保存时，
    后端应自动同步 signature，状态保持为「最近测试成功」，绝不产生「配置已变更，请重新验证」的误报。
    """
    svc = SettingsService(db_session)
    svc.set("caldav_url", "https://caldav.example.com")
    svc.set("caldav_username", "alice")
    svc.set("caldav_password", "topsecret", encrypted=True)
    svc.set("caldav_ssl_verify", "true")
    svc.set("caldav_calendar_url", "https://caldav.example.com/cal/primary")
    svc.set("caldav_calendar_name", "Initial Calendar")
    svc.commit()

    # 模拟先前已成功验证
    save_check(db_session, "caldav", revision(db_session, "caldav"), True)
    summary_before = check_summary(db_session, "caldav")
    assert summary_before["ok"] is True
    assert summary_before["label"] == "最近测试成功"

    # 执行保存：密码留空（保持原密码），修改日历名称、默认时长与提醒时间
    req = Request({
        "type": "http",
        "method": "POST",
        "path": "/console/caldav",
        "headers": [(b"accept", b"application/json")],
        "session": {},
    })
    kwargs = dict(
        caldav_url="https://caldav.example.com",
        caldav_username="alice",
        caldav_password="",  # 密码留空
        caldav_calendar_url="https://caldav.example.com/cal/secondary",
        caldav_calendar_name="Updated Calendar Name",
        caldav_timezone="Asia/Shanghai",
        caldav_reminder_minutes="45",
        caldav_default_duration="120",
        caldav_ssl_verify="true",
        session=db_session,
        _=None,
    )
    resp = asyncio.run(routes.update_caldav_settings(req, **kwargs))
    assert resp.status_code == 200

    resp_data = json.loads(resp.body)
    assert resp_data["ok"] is True
    assert "check" in resp_data
    assert resp_data["check"]["ok"] is True
    assert resp_data["check"]["label"] == "最近测试成功"

    # 验证数据库中持久化的 check 签名与当前数据库最新 revision 完全一致
    current_rev = revision(db_session, "caldav")
    summary_after = check_summary(db_session, "caldav")
    assert summary_after["ok"] is True
    assert summary_after["label"] == "最近测试成功"
    assert summary_after["reason"] == "recent_success"

    # 验证首页状态与日历页面 payload 绝不误报需复核
    ctx = routes.status_context(db_session)
    services = {s["id"]: s for s in ctx["services_status"]}
    assert services["caldav"]["dot_class"] == "dot-success"
    assert services["caldav"]["state_label"] == "已验证"

    payload = routes.caldav_payload(svc, db_session)
    assert payload["caldav_dot_class"] == "dot-success"
    assert payload["caldav_state_label"] == "已验证"
    assert payload["caldav_review_reason"] == ""


def test_caldav_test_then_save_syncs_new_credentials(db_session):
    """
    场景 2：用户在前端输入了新服务器凭据并点击测试成功后，直接保存配置，
    保存逻辑应通过 session hash 识别并承接验证状态，自动同步持久化最新签名。
    """
    class MockCalDAVService:
        async def test_connection(self, *args, **kwargs):
            return True

    with patch("app.web.routes._caldav_components", return_value=(MockCalDAVService, Exception)):
        session_store = {}
        probe_req = Request({
            "type": "http",
            "method": "POST",
            "path": "/console/caldav/test",
            "headers": [(b"accept", b"application/json")],
            "session": session_store,
        })

        # 1. 连通性测试新凭据
        test_resp = asyncio.run(routes._probe_caldav(
            db_session,
            "https://caldav.brand-new.org",
            "newbie",
            "password123",
            "true",
            False,
            request=probe_req,
        ))
        assert test_resp.status_code == 200
        test_data = json.loads(test_resp.body)
        assert test_data["ok"] is True
        assert test_data["state_label"] == "待保存"
        assert "caldav_verified_cred_hash" in session_store

        # 2. 紧接着保存该新配置
        svc = SettingsService(db_session)
        save_req = Request({
            "type": "http",
            "method": "POST",
            "path": "/console/caldav",
            "headers": [(b"accept", b"application/json")],
            "session": session_store,
        })
        save_kwargs = dict(
            caldav_url="https://caldav.brand-new.org",
            caldav_username="newbie",
            caldav_password="password123",
            caldav_calendar_url="https://caldav.brand-new.org/cal/home",
            caldav_calendar_name="Home Calendar",
            caldav_timezone="UTC",
            caldav_reminder_minutes="15",
            caldav_default_duration="30",
            caldav_ssl_verify="true",
            session=db_session,
            _=None,
        )
        save_resp = asyncio.run(routes.update_caldav_settings(save_req, **save_kwargs))
        assert save_resp.status_code == 200

        # 校验哈希已被消费且 check 记录为最近测试成功
        assert "caldav_verified_cred_hash" not in session_store
        summary = check_summary(db_session, "caldav")
        assert summary["ok"] is True
        assert summary["label"] == "最近测试成功"

        # 首页服务状态卡片同步为 dot-success 已验证
        ctx = routes.status_context(db_session)
        services = {s["id"]: s for s in ctx["services_status"]}
        assert services["caldav"]["dot_class"] == "dot-success"
        assert services["caldav"]["state_label"] == "已验证"


def test_caldav_save_unverified_does_not_falsely_mark_success(db_session):
    """
    场景 3：从未验证过的新配置直接保存时，不应伪造测试成功状态，保持为未验证/待验证。
    """
    svc = SettingsService(db_session)
    req = Request({
        "type": "http",
        "method": "POST",
        "path": "/console/caldav",
        "headers": [(b"accept", b"application/json")],
        "session": {},
    })
    save_kwargs = dict(
        caldav_url="https://caldav.unverified.org",
        caldav_username="unverified_user",
        caldav_password="secretpassword",
        caldav_calendar_url="https://caldav.unverified.org/cal",
        caldav_calendar_name="Cal",
        caldav_timezone="Asia/Shanghai",
        caldav_reminder_minutes="30",
        caldav_default_duration="60",
        caldav_ssl_verify="true",
        session=db_session,
        _=None,
    )
    save_resp = asyncio.run(routes.update_caldav_settings(req, **save_kwargs))
    assert save_resp.status_code == 200

    summary = check_summary(db_session, "caldav")
    assert summary["ok"] is None
    assert summary["label"] == "未验证"

    ctx = routes.status_context(db_session)
    services = {s["id"]: s for s in ctx["services_status"]}
    assert services["caldav"]["dot_class"] == "dot-warning"
    assert services["caldav"]["state_label"] == "待验证"


# ==============================================================================
# 2. 日历页面测试与首页徽标联动 (Calendar Page Test & Dashboard Badge Linkage)
# ==============================================================================

def test_recheck_caldav_endpoint_syncs_with_dashboard(db_session):
    """
    验证一键复核接口 POST /console/connections/caldav/test：
    1. 测试成功时返回 ok=True, dot_class=dot-success, state_label=已验证，首页徽标同步；
    2. 测试失败时返回 ok=False, dot_class=dot-error, state_label=连接失败，首页徽标同步。
    """
    svc = SettingsService(db_session)
    svc.set("caldav_url", "https://caldav.recheck.com")
    svc.set("caldav_username", "bob")
    svc.set("caldav_password", "pass123", encrypted=True)
    svc.set("caldav_calendar_url", "https://caldav.recheck.com/cal/default")
    svc.set("caldav_calendar_name", "Primary")
    svc.commit()

    class SuccessProvider:
        async def test_connection(self, *args, **kwargs):
            return True

    class FailureProvider:
        async def test_connection(self, *args, **kwargs):
            raise Exception("Connection timeout")

    # --- 阶段 A：测试成功 ---
    with patch("app.web.routes._caldav_components", return_value=(SuccessProvider, Exception)):
        resp = asyncio.run(routes.test_saved_connection("caldav", db_session, None))
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["ok"] is True
        assert data["dot_class"] == "dot-success"
        assert data["state_label"] == "已验证"
        assert data["check"]["label"] == "最近测试成功"

        # 验证首页联动
        ctx = routes.status_context(db_session)
        services = {s["id"]: s for s in ctx["services_status"]}
        assert services["caldav"]["dot_class"] == "dot-success"
        assert services["caldav"]["state_label"] == "已验证"

    # --- 阶段 B：测试失败 ---
    with patch("app.web.routes._caldav_components", return_value=(FailureProvider, Exception)):
        resp_fail = asyncio.run(routes.test_saved_connection("caldav", db_session, None))
        assert resp_fail.status_code == 200
        data_fail = json.loads(resp_fail.body)
        assert data_fail["ok"] is False
        assert data_fail["dot_class"] == "dot-error"
        assert data_fail["state_label"] == "连接失败"
        assert data_fail["check"]["label"] == "最近测试失败"

        # 验证首页联动为连接失败 (dot-error)
        ctx_fail = routes.status_context(db_session)
        services_fail = {s["id"]: s for s in ctx_fail["services_status"]}
        assert services_fail["caldav"]["dot_class"] == "dot-error"
        assert services_fail["caldav"]["state_label"] == "连接失败"


def test_probe_caldav_with_saved_config_persists_check(db_session):
    """
    当在日历页面针对已保存的配置执行连通性测试或日历发现时，
    应自动持久化 check 记录并返回与首页一致的 state_label 和 dot_class。
    """
    svc = SettingsService(db_session)
    svc.set("caldav_url", "https://caldav.probe.com")
    svc.set("caldav_username", "probe_user")
    svc.set("caldav_password", "probe_pass", encrypted=True)
    svc.set("caldav_ssl_verify", "false")
    svc.commit()

    class MockProvider:
        async def test_connection(self, *args, **kwargs):
            return True

        async def list_calendars(self, *args, **kwargs):
            return [{"id": "cal-1", "name": "Work", "url": "https://caldav.probe.com/cal-1"}]

    with patch("app.web.routes._caldav_components", return_value=(MockProvider, Exception)):
        req = Request({"type": "http", "method": "POST", "path": "/console/caldav/test", "headers": [], "session": {}})

        # 1. 连通性测试 (test_caldav_connection)
        resp_test = asyncio.run(routes._probe_caldav(
            db_session, "https://caldav.probe.com", "probe_user", "", "false", False, request=req
        ))
        assert resp_test.status_code == 200
        data_test = json.loads(resp_test.body)
        assert data_test["ok"] is True
        assert data_test["state_label"] == "已验证"
        assert data_test["dot_class"] == "dot-success"
        assert data_test["check"]["label"] == "最近测试成功"

        # 2. 获取日历列表 (list_caldav_calendars)
        resp_cals = asyncio.run(routes._probe_caldav(
            db_session, "https://caldav.probe.com", "probe_user", "", "false", True, request=req
        ))
        assert resp_cals.status_code == 200
        data_cals = json.loads(resp_cals.body)
        assert data_cals["ok"] is True
        assert len(data_cals["calendars"]) == 1
        assert data_cals["state_label"] == "已验证"
        assert data_cals["dot_class"] == "dot-success"


# ==============================================================================
# 3. 复核文案与状态研判所有分支 (Review Notice & Status Evaluation Branches)
# ==============================================================================

def test_status_and_payload_branches_comprehensive(db_session):
    """
    全量覆盖 caldav_payload 与 status_context 的所有状态分支：
    - 分支 1：未配置
    - 分支 2：已配置但未测试过 (待验证)
    - 分支 3：测试成功，配置未变，无业务写入 (已验证，dot-success)
    - 分支 4：测试成功，配置未变，有业务写入 (已连接，dot-success)
    - 分支 5：测试成功，但配置发生变更 (需复核，dot-warning)
    - 分支 6：测试已超过 24 小时，无配置变更 (仍为 dot-success，提示超过24小时)
    - 分支 7：测试失败 (连接失败，dot-error)
    """
    svc = SettingsService(db_session)

    # -------------------------------------------------------------
    # 分支 1：未配置日历 (缺少 URL 或 Calendar URL)
    # -------------------------------------------------------------
    p1 = routes.caldav_payload(svc, db_session)
    assert p1["caldav_dot_class"] == "dot-muted"
    assert p1["caldav_state_label"] == "未配置"

    ctx1 = routes.status_context(db_session)
    assert ctx1["services_status"][1]["dot_class"] == "dot-muted"
    assert ctx1["services_status"][1]["state_label"] == "未配置"

    # -------------------------------------------------------------
    # 分支 2：已配置但从未测试 (待验证)
    # -------------------------------------------------------------
    svc.set("caldav_url", "https://caldav.example.com")
    svc.set("caldav_calendar_url", "https://caldav.example.com/cal")
    svc.set("caldav_calendar_name", "Primary")
    svc.commit()

    p2 = routes.caldav_payload(svc, db_session)
    assert p2["caldav_dot_class"] == "dot-warning"
    assert p2["caldav_state_label"] == "待验证"
    assert "尚未进行连通性测试" in p2["caldav_review_reason"]

    ctx2 = routes.status_context(db_session)
    assert ctx2["services_status"][1]["dot_class"] == "dot-warning"
    assert ctx2["services_status"][1]["state_label"] == "待验证"

    # -------------------------------------------------------------
    # 分支 3：测试成功，配置未变，当前版本无业务写入 (已验证)
    # -------------------------------------------------------------
    save_check(db_session, "caldav", revision(db_session, "caldav"), True)
    db_session.expire_all()

    p3 = routes.caldav_payload(svc, db_session)
    assert p3["caldav_dot_class"] == "dot-success"
    assert p3["caldav_state_label"] == "已验证"
    assert p3["caldav_review_reason"] == ""

    ctx3 = routes.status_context(db_session)
    assert ctx3["services_status"][1]["dot_class"] == "dot-success"
    assert ctx3["services_status"][1]["state_label"] == "已验证"

    # -------------------------------------------------------------
    # 分支 4：测试成功，配置未变，当前版本有业务写入 (已连接)
    # -------------------------------------------------------------
    current_ver = svc.get_config_version()
    rec = EventRecord(
        status="success",
        operation="create",
        config_version=current_ver,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(rec)
    db_session.commit()

    p4 = routes.caldav_payload(svc, db_session)
    assert p4["caldav_dot_class"] == "dot-success"
    assert p4["caldav_state_label"] == "已连接"

    ctx4 = routes.status_context(db_session)
    assert ctx4["services_status"][1]["dot_class"] == "dot-success"
    assert ctx4["services_status"][1]["state_label"] == "已连接"

    # -------------------------------------------------------------
    # 分支 5：配置发生变更，且新版本无业务写入 (需复核)
    # -------------------------------------------------------------
    svc.set("caldav_url", "https://caldav.modified-host.com")
    svc.commit()
    db_session.expire_all()

    p5 = routes.caldav_payload(svc, db_session)
    assert p5["caldav_dot_class"] == "dot-warning"
    assert p5["caldav_state_label"] == "需复核"
    assert "配置已变更" in p5["caldav_review_reason"]

    ctx5 = routes.status_context(db_session)
    assert ctx5["services_status"][1]["dot_class"] == "dot-warning"
    assert ctx5["services_status"][1]["state_label"] == "需复核"

    # -------------------------------------------------------------
    # 分支 6：测试已超过 24 小时，但配置未变更 (保持 dot-success 已验证，不盲目报警)
    # -------------------------------------------------------------
    # 重置为当前配置并记录 48 小时前的成功测试
    cur_rev = revision(db_session, "caldav")
    old_time = datetime.now(timezone.utc) - timedelta(hours=48)
    svc.set("connection_check_caldav", json.dumps({
        "revision": cur_rev,
        "ok": True,
        "at": old_time.isoformat(),
    }))
    svc.commit()
    db_session.expire_all()

    # 无当前版本业务写入时
    p6 = routes.caldav_payload(svc, db_session)
    assert p6["caldav_dot_class"] == "dot-success"
    assert p6["caldav_state_label"] == "已验证"
    assert "测试已超过 24 小时" in p6["caldav_review_reason"]

    ctx6 = routes.status_context(db_session)
    assert ctx6["services_status"][1]["dot_class"] == "dot-success"
    assert ctx6["services_status"][1]["state_label"] == "已验证"

    # -------------------------------------------------------------
    # 分支 7：测试失败 (连接失败)
    # -------------------------------------------------------------
    save_check(db_session, "caldav", cur_rev, False)
    db_session.expire_all()

    p7 = routes.caldav_payload(svc, db_session)
    assert p7["caldav_dot_class"] == "dot-error"
    assert p7["caldav_state_label"] == "连接失败"
    assert "连接测试失败" in p7["caldav_review_reason"]

    ctx7 = routes.status_context(db_session)
    assert ctx7["services_status"][1]["dot_class"] == "dot-error"
    assert ctx7["services_status"][1]["state_label"] == "连接失败"


# ==============================================================================
# 4. 前端 HTML 模板与渲染完整性验证 (Template Rendering Integration)
# ==============================================================================

def test_caldav_template_html_rendering_states(db_session):
    """
    验证 caldav.html 模板在需复核与已验证不同状态下的真实 HTML 渲染输出：
    1. 包含右侧卡片、徽标、时间戳与一键复核按钮；
    2. 需复核时提示框展示黄色警示与对应文字；
    3. 已验证时展示绿色校验通过状态。
    """
    svc = SettingsService(db_session)
    svc.set("caldav_url", "https://caldav.template-test.com")
    svc.set("caldav_calendar_url", "https://caldav.template-test.com/cal")
    svc.set("caldav_calendar_name", "Template Cal")
    svc.commit()

    # 状态 A：需复核状态
    # 设置一个旧签名使得 revision 不匹配
    svc.set("connection_check_caldav", json.dumps({
        "revision": "stale_signature_123",
        "ok": True,
        "at": datetime.now(timezone.utc).isoformat(),
    }))
    svc.commit()

    payload_review = routes.caldav_payload(svc, db_session)
    payload_review["request"] = Request({"type": "http", "method": "GET", "path": "/console/caldav", "headers": [], "session": {"admin_authenticated": True}})
    payload_review["message"] = None
    payload_review["error"] = None

    rendered_review = routes.templates.get_template("caldav.html").render(**payload_review)
    assert 'id="caldav-status-card"' in rendered_review
    assert 'id="btn-recheck-caldav"' in rendered_review
    assert "一键复核 / 验证已保存配置" in rendered_review
    assert 'id="caldav-review-notice"' in rendered_review
    assert "需复核" in rendered_review
    assert "配置已变更，请重新验证" in rendered_review
    assert "dot-warning" in rendered_review

    # 状态 B：已验证状态
    save_check(db_session, "caldav", revision(db_session, "caldav"), True)
    db_session.expire_all()

    payload_verified = routes.caldav_payload(svc, db_session)
    payload_verified["request"] = Request({"type": "http", "method": "GET", "path": "/console/caldav", "headers": [], "session": {"admin_authenticated": True}})
    payload_verified["message"] = None
    payload_verified["error"] = None

    rendered_verified = routes.templates.get_template("caldav.html").render(**payload_verified)
    assert "已验证" in rendered_verified
    assert "dot-success" in rendered_verified
    assert "最近测试成功" in rendered_verified
