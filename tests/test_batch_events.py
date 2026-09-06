import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.schemas import CalendarEvent, ExtractionResult, Intent
from app.channels.message_processor import (
    ChannelContext,
    _handle_new,
    _is_retry_command,
    _route,
    derive_batch_uid,
)
from app.db.models import Base, EventRecord
from app.main import create_app
from app.services.caldav_service import CalDAVServiceError
from app.services.settings_service import SettingsService
from app.web.routes import get_db, require_admin

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


class _FakeExtractor:
    def __init__(self, result: ExtractionResult):
        self._result = result

    async def extract(self, text: str):
        return self._result

    async def modify(self, existing: dict, text: str):
        return self._result

    async def merge_draft(self, draft: dict, text: str):
        return self._result


def test_deterministic_batch_uid_derivation():
    """验证确定性 UID 派生规则：相同 batch_id 与 index 产物恒等，不同参数严格隔离，符合 RFC 4122。"""
    batch_id = "batch_test_12345"
    uid_0_a = derive_batch_uid(batch_id, 0)
    uid_0_b = derive_batch_uid(batch_id, 0)
    uid_1 = derive_batch_uid(batch_id, 1)
    uid_diff_batch = derive_batch_uid("batch_other_999", 0)

    # 1. 严格幂等恒等
    assert uid_0_a == uid_0_b
    assert isinstance(uid_0_a, str)
    # 2. 有效 UUID 格式
    parsed = uuid.UUID(uid_0_a)
    assert parsed.version == 5
    # 3. 子项索引隔离
    assert uid_0_a != uid_1
    # 4. 批次 ID 隔离
    assert uid_0_a != uid_diff_batch


def test_batch_events_creation_and_aggregation_report_all_success():
    """验证多日程抽取链路：生成单一结构化聚合报告（非逐条刷屏）、记录 batch_id/batch_index、UID 确定性分配。"""
    async def _run():
        session = _create_test_session()
        ctx = ChannelContext(source="telegram", source_user_id="user_batch_1", conversation_id="conv_100")
        caldav = {"url": "http://caldav.test", "user": "test_user", "pw": "test_pw", "cal": "default", "rem": 30, "dur": 60, "ssl": True}
        svc = SettingsService(session)

        ev1 = CalendarEvent(title="上午项目对齐会", start_time="2026-06-10T09:30:00", location="会议室A")
        ev2 = CalendarEvent(title="下午拜访客户", start_time="2026-06-10T14:00:00", location="科技园B座")
        ev3 = CalendarEvent(title="傍晚复盘总结", start_time="2026-06-10T18:00:00")
        extraction_res = ExtractionResult(intent=Intent.create_event, events=[ev1, ev2, ev3])

        async def mock_write_caldav(event, caldav_cfg, uid=None):
            return {"uid": uid, "href": f"/cal/{uid}.ics", "etag": f'"{uid}-etag"'}

        with patch("app.channels.message_processor._write_caldav", side_effect=mock_write_caldav):
            replies = await _handle_new(session, ctx, "明天上午开会，下午拜访客户，傍晚复盘", extraction_res, caldav, svc)

        # 1. 仅返回 1 条聚合消息（彻底告别刷屏）
        assert len(replies) == 1
        report_text, primary_id = replies[0]
        assert primary_id is not None

        # 2. 报告内容清晰包含汇总与各项明细
        assert "📋 批量日程处理完成（共 3 项全部成功）" in report_text
        assert "共识别 3 项日程，全部已成功写入日历" in report_text
        assert "[1] ✅ 📌 上午项目对齐会 (📍 会议室A)" in report_text
        assert "[2] ✅ 📌 下午拜访客户 (📍 科技园B座)" in report_text
        assert "[3] ✅ 📌 傍晚复盘总结" in report_text
        assert "2026-06-10 09:30" in report_text
        assert "2026-06-10 14:00" in report_text

        # 3. 校验 DB 中的批次追踪与确定性 UID
        records = session.execute(
            select(EventRecord).order_by(EventRecord.id.asc())
        ).scalars().all()
        assert len(records) == 3

        batch_id = records[0].batch_id
        assert batch_id is not None and batch_id.startswith("batch_")
        for idx, rec in enumerate(records):
            assert rec.batch_id == batch_id
            assert rec.batch_index == idx
            assert rec.status == "success"
            assert rec.failure_phase is None
            # 校验确定性 UID 吻合
            expected_uid = derive_batch_uid(batch_id, idx)
            assert rec.caldav_uid == expected_uid

    asyncio.run(_run())


def test_batch_events_partial_failure_and_chat_retry_closure():
    """验证批量日程部分失败时的细粒度提示，以及聊天回复「重试失败项」精准局部重试闭环。"""
    async def _run():
        session = _create_test_session()
        ctx = ChannelContext(source="wechat", source_user_id="wx_user_1", conversation_id="conv_200")
        caldav = {"url": "http://caldav.test", "user": "test_user", "pw": "test_pw", "cal": "default", "rem": 30, "dur": 60, "ssl": True}
        svc = SettingsService(session)

        ev1 = CalendarEvent(title="早会", start_time="2026-06-11T09:00:00")
        ev2 = CalendarEvent(title="技术评审", start_time="2026-06-11T11:00:00")
        ev3 = CalendarEvent(title="周报总结", start_time="2026-06-11T17:00:00")
        extraction_res = ExtractionResult(intent=Intent.create_event, events=[ev1, ev2, ev3])

        # 模拟中间项 ev2 写入 CalDAV 失败
        async def mock_write_with_partial_fail(event, caldav_cfg, uid=None):
            if "技术评审" in event.title:
                raise CalDAVServiceError("iCloud CalDAV 408 Request Timeout")
            return {"uid": uid, "href": f"/cal/{uid}.ics", "etag": f'"{uid}-etag"'}

        with patch("app.channels.message_processor._write_caldav", side_effect=mock_write_with_partial_fail):
            replies = await _handle_new(session, ctx, "安排三个日程", extraction_res, caldav, svc)

        # 1. 验证失败明细报告
        assert len(replies) == 1
        report_text, _ = replies[0]
        assert "共识别 3 项日程，2 项成功，1 项失败" in report_text
        assert "[1] ✅ 📌 早会" in report_text
        assert "[2] ❌ 📌 技术评审" in report_text
        assert "⚠️ 失败原因：iCloud CalDAV 408 Request Timeout" in report_text
        assert "[3] ✅ 📌 周报总结" in report_text
        assert "回复「重试失败项」可仅对失败项重新尝试写入" in report_text

        # 检查数据库初始状态
        recs = session.execute(select(EventRecord).order_by(EventRecord.batch_index.asc())).scalars().all()
        assert len(recs) == 3
        assert recs[0].status == "success"
        assert recs[1].status == "failed"
        assert recs[1].failure_phase == "write"
        assert recs[1].retry_count == 0
        assert recs[2].status == "success"

        batch_id = recs[0].batch_id
        failed_uid = recs[1].caldav_uid
        assert failed_uid == derive_batch_uid(batch_id, 1)

        # 2. 模拟用户在聊天中回复「重试失败项」
        # 此时网络已恢复，重试成功
        async def mock_write_success(event, caldav_cfg, uid=None):
            return {"uid": uid, "href": f"/cal/{uid}.ics", "etag": f'"{uid}-etag-retry"'}

        extractor = _FakeExtractor(ExtractionResult(intent=Intent.no_event))
        with patch("app.channels.message_processor._write_caldav", side_effect=mock_write_success) as mock_write:
            retry_replies = await _route(session, ctx, "重试失败项", extractor, caldav, svc)

        # 验证仅调用了一次 CalDAV 写入（只重试了第 2 项，成功项 1、3 被自动跳过）
        assert mock_write.call_count == 1
        # 验证传入的 UID 保持确定性一致
        assert mock_write.call_args[1]["uid"] == failed_uid

        assert len(retry_replies) == 1
        retry_report, _ = retry_replies[0]
        assert "📋 批量日程局部重试报告" in retry_report
        assert "跳过已成功项 2 项" in retry_report
        assert "当前共 3 项成功，0 项失败" in retry_report
        assert "[1] ✅ 📌 早会 [已跳过]" in retry_report
        assert "[2] ✅ 📌 技术评审" in retry_report
        assert "[3] ✅ 📌 周报总结 [已跳过]" in retry_report

        # 检查数据库更新结果
        session.refresh(recs[1])
        assert recs[1].status == "success"
        assert recs[1].failure_phase is None
        assert recs[1].error_message is None
        assert recs[1].retry_count == 1
        assert recs[1].caldav_uid == failed_uid

        # 3. 再次回复「重试失败项」，提示已全部成功
        all_done_replies = await _route(session, ctx, "重试失败项", extractor, caldav, svc)
        assert "已全部成功写入日历，无需重试" in all_done_replies[0][0]

    asyncio.run(_run())


def test_is_retry_command_patterns():
    """验证重试指令模式匹配覆盖度。"""
    assert _is_retry_command("重试")
    assert _is_retry_command("重试失败项")
    assert _is_retry_command("重试失败日程")
    assert _is_retry_command("请重试失败项")
    assert _is_retry_command("重新执行失败项")
    assert _is_retry_command("retry")
    assert _is_retry_command("retry failed")
    assert _is_retry_command("重试！")
    assert _is_retry_command("重试失败项。")

    # 非重试指令
    assert not _is_retry_command("明天上午重试")
    assert not _is_retry_command("删除日程")
    assert not _is_retry_command("取消")


def test_web_console_batch_retry_endpoint():
    """验证 Web 控制台批量局部重试端点 POST /console/events/batch/{batch_id}/retry。"""
    session = _create_test_session()
    app = create_app()
    app.dependency_overrides[get_db] = _override_db(session)
    app.dependency_overrides[require_admin] = lambda: None
    client = TestClient(app)

    # 1. 模拟未配置 CalDAV 凭据
    svc = SettingsService(session)
    svc.set("caldav_url", "")
    svc.set("caldav_username", "")
    session.commit()

    batch_id = "batch_web_retry_test"
    rec1 = EventRecord(
        source="telegram", source_user_id="u1", batch_id=batch_id, batch_index=0,
        operation="create", title="任务A", status="success", caldav_uid="uid-a",
        event_json=json.dumps({"title": "任务A", "start_time": "2026-06-15T10:00:00"}),
    )
    rec2 = EventRecord(
        source="telegram", source_user_id="u1", batch_id=batch_id, batch_index=1,
        operation="create", title="任务B", status="failed", failure_phase="write",
        error_message="Connection timed out", caldav_uid=derive_batch_uid(batch_id, 1),
        event_json=json.dumps({"title": "任务B", "start_time": "2026-06-15T14:00:00"}),
    )
    session.add_all([rec1, rec2])
    session.commit()

    # 404 测试
    res_404 = client.post("/console/events/batch/non_existent_batch/retry", headers=ORIGIN_HEADERS)
    assert res_404.status_code == 404

    # 未配置 CalDAV 测试
    res_nocal = client.post(f"/console/events/batch/{batch_id}/retry", headers=ORIGIN_HEADERS)
    assert res_nocal.status_code == 400
    assert "尚未配置有效的日历连接" in res_nocal.json()["error"]

    # 配置 CalDAV
    svc.set("caldav_url", "http://caldav.server")
    svc.set("caldav_username", "user")
    svc.set("caldav_password", "pass")
    session.commit()

    # 2. 模拟 CalDAV 写入成功
    async def mock_create_event(*args, **kwargs):
        return {"uid": kwargs.get("uid"), "href": f"/cal/{kwargs.get('uid')}.ics"}

    with patch("app.services.caldav_service.CalDAVService.create_event", side_effect=mock_create_event) as mock_ce:
        res = client.post(f"/console/events/batch/{batch_id}/retry", headers=ORIGIN_HEADERS)

    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["batch_id"] == batch_id
    assert data["total"] == 2
    assert data["skipped_count"] == 1
    assert data["retried_count"] == 1
    assert data["success_count"] == 1
    assert data["failed_count"] == 0

    # 校验仅调用了 1 次 CalDAV 且 UID 正确
    assert mock_ce.call_count == 1
    assert mock_ce.call_args[1]["uid"] == derive_batch_uid(batch_id, 1)

    # 校验数据库更新
    session.refresh(rec2)
    assert rec2.status == "success"
    assert rec2.retry_count == 1
    assert rec2.failure_phase is None

    # 3. 再次请求时全部已成功
    res_again = client.post(f"/console/batches/{batch_id}/retry", headers=ORIGIN_HEADERS)
    assert res_again.status_code == 200
    data_again = res_again.json()
    assert data_again["ok"] is True
    assert data_again["retried_count"] == 0
    assert "该批次所有日程均已成功" in data_again["message"]

    # 4. 测试便捷接口 POST /console/events/{event_id}/retry-batch
    res_wrapper = client.post(f"/console/events/{rec1.id}/retry-batch", headers=ORIGIN_HEADERS)
    assert res_wrapper.status_code == 200
    assert res_wrapper.json()["ok"] is True

    # 非批量日程调用 retry-batch 返回 400
    single_rec = EventRecord(source="telegram", operation="create", title="单独日程", status="failed")
    session.add(single_rec)
    session.commit()
    res_single = client.post(f"/console/events/{single_rec.id}/retry-batch", headers=ORIGIN_HEADERS)
    assert res_single.status_code == 400
    assert "不属于批量日程" in res_single.json()["error"]
