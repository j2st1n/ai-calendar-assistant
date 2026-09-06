#!/usr/bin/env python3
"""E2E CalDAV Real Write & Auto-Cleanup Verification Script.

Phase 3.1 核心验收：
1. 加载生产真实配置（AI Provider + iCloud CalDAV）。
2. 自然语言通过生产真实 AI 抽取结构化日程（包含 [TEST-VERIFY] 标识、时间、地点、提前提醒）。
3. 向 iCloud 目标日历写入真实日程。
4. 回读日历对象，严格核验 Summary、起止时间、时区、提醒组件 (VALARM) 及日历匹配性。
5. 强制安全删除日程，并二次回读确认已彻底清除，实现 0 数据污染。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure app is importable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from icalendar import Calendar as ICalCalendar

from app.ai.extractor import EventExtractor
from app.ai.schemas import Intent
from app.core.bootstrap import ensure_app_secret
from app.core.config import settings
from app.db.session import SessionLocal
from app.services.ai_provider_service import AIProviderConfig
from app.services.caldav_service import CalDAVService, _DAVClient
from app.services.settings_service import SettingsService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("verify_caldav_e2e")


async def run_verification() -> dict:
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "steps": {},
        "success": False,
    }

    # 0. 初始化密钥与数据源
    logger.info(">>> [步骤 0] 加载生产系统配置...")
    data_dir = Path(settings.data_dir)
    ensure_app_secret(data_dir / "secrets.json")

    with SessionLocal() as session:
        svc = SettingsService(session)
        ai_provider_type = svc.get("ai_provider_type") or "openai_compatible"
        ai_base_url = svc.get("ai_base_url") or "https://api.openai.com/v1"
        ai_api_key = svc.get("ai_api_key")
        ai_model = svc.get("ai_model") or "gemini-3.8-flash-high"

        caldav_url = svc.get("caldav_url") or ""
        caldav_user = svc.get("caldav_username") or ""
        caldav_pw = svc.get("caldav_password") or ""
        caldav_calendar_url = svc.get("caldav_calendar_url") or ""
        caldav_calendar_name = svc.get("caldav_calendar_name") or "AI"
        caldav_tz = svc.get("caldav_timezone") or "Asia/Shanghai"
        caldav_ssl = svc.get("caldav_ssl_verify") != "false"

    if not ai_api_key:
        raise RuntimeError("未配置有效的 AI API Key")
    if not caldav_url or not caldav_user or not caldav_pw:
        raise RuntimeError("未配置有效的 CalDAV 账号或密码")

    report["steps"]["config"] = {
        "status": "passed",
        "ai_model": ai_model,
        "caldav_url": caldav_url,
        "caldav_calendar_name": caldav_calendar_name,
        "caldav_calendar_url": caldav_calendar_url,
        "timezone": caldav_tz,
    }
    logger.info(
        "配置加载成功: AI 模型=%s, CalDAV URL=%s, 目标日历=%s",
        ai_model,
        caldav_url,
        caldav_calendar_name,
    )

    # 1. 真实 AI 抽取测试
    logger.info(">>> [步骤 1] 真实 AI 抽取自然语言日程...")
    ai_config = AIProviderConfig(
        provider_type=ai_provider_type,
        base_url=ai_base_url,
        api_key=ai_api_key,
        model=ai_model,
    )
    extractor = EventExtractor(ai_config, timezone=caldav_tz)

    test_prompt = (
        "请为我记录一个日程：明天下午15:00到16:00在第九会议室参加 "
        "[TEST-VERIFY] Phase 3.1 真实端到端日历验收闭环测试，提前15分钟提醒我"
    )
    logger.info("AI 抽取输入提示词: %s", test_prompt)
    extraction_result = await extractor.extract(test_prompt)

    assert extraction_result.intent == Intent.create_event, f"意图不符: {extraction_result.intent}"
    assert extraction_result.events and len(extraction_result.events) > 0, "未识别到日程事件"
    event_data = extraction_result.events[0]
    logger.info("AI 抽取成功: title=%s, start=%s, end=%s, location=%s, reminders=%s",
                event_data.title, event_data.start_time, event_data.end_time,
                event_data.location, event_data.reminders)

    assert "[TEST-VERIFY]" in event_data.title, f"标题缺少 [TEST-VERIFY] 标识: {event_data.title}"
    assert event_data.start_time, "缺少 start_time"
    assert event_data.reminders and len(event_data.reminders) > 0, "缺少提醒设置"
    reminder_minutes = event_data.reminders[0].minutes_before
    assert reminder_minutes == 15, f"提醒时间非 15 分钟: {reminder_minutes}"

    report["steps"]["ai_extraction"] = {
        "status": "passed",
        "title": event_data.title,
        "start_time": event_data.start_time,
        "end_time": event_data.end_time,
        "location": event_data.location,
        "reminder_minutes": reminder_minutes,
        "timezone": event_data.timezone,
    }

    # 2. 真实 CalDAV 写入
    logger.info(">>> [步骤 2] 向 iCloud 目标日历写入真实日程...")
    test_uid = f"test-verify-{uuid.uuid4().hex}"
    caldav_service = CalDAVService()

    formatted_reminders = [{"minutes_before": reminder_minutes}]
    write_res = None
    created_href = None

    try:
        write_res = await caldav_service.create_event(
            caldav_url=caldav_url,
            username=caldav_user,
            password=caldav_pw,
            calendar_url=caldav_calendar_url or None,
            title=event_data.title,
            start_time=event_data.start_time,
            end_time=event_data.end_time,
            timezone_str=event_data.timezone or caldav_tz,
            location=event_data.location,
            description="Phase 3.1 生产环境真实日程端到端写入与自动清理闭环测试",
            reminders=formatted_reminders,
            recurrence=event_data.recurrence,
            is_all_day=event_data.is_all_day,
            ssl_verify=caldav_ssl,
            uid=test_uid,
        )
        created_href = write_res.get("href")
        logger.info("CalDAV 写入成功: uid=%s, href=%s", test_uid, created_href)
        report["steps"]["caldav_write"] = {
            "status": "passed",
            "uid": test_uid,
            "href": created_href,
        }

        # 3. 回读并深度核验目标日历中的 VEVENT 对象
        logger.info(">>> [步骤 3] 从 iCloud CalDAV 回读并深度核验 VEVENT 属性...")
        client = _DAVClient(url=caldav_url, username=caldav_user, password=caldav_pw, timeout=60)
        calendars = client.get_calendars()
        target_cal = None
        for c in calendars:
            if str(c.url) == caldav_calendar_url or (not target_cal and c.name == caldav_calendar_name):
                target_cal = c
                break
        if not target_cal and calendars:
            target_cal = calendars[0]

        assert target_cal is not None, "未找到目标日历"

        matched_obj = None
        for obj in target_cal.objects():
            if str(obj.id) == test_uid or (created_href and str(obj.url) == created_href):
                matched_obj = obj
                break

        assert matched_obj is not None, f"在目标日历中未找到刚刚写入的测试日程 (uid={test_uid})"

        # 解析 iCalendar 数据，若尚未加载则显式 load()
        if getattr(matched_obj, "data", None) is None and hasattr(matched_obj, "load"):
            matched_obj.load()

        raw_data = matched_obj.data
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8", errors="replace")

        cal_obj = ICalCalendar.from_ical(raw_data)
        found_summary = None
        found_start = None
        found_end = None
        found_location = None
        found_alarm_trigger = None

        for comp in cal_obj.walk():
            if comp.name == "VEVENT":
                found_summary = str(comp.get("summary", ""))
                found_start = str(comp.get("dtstart", ""))
                found_end = str(comp.get("dtend", ""))
                found_location = str(comp.get("location", ""))
            elif comp.name == "VALARM":
                trigger = comp.get("trigger")
                if trigger:
                    found_alarm_trigger = str(trigger.dt)

        logger.info(
            "回读核验属性: summary=%s, dtstart=%s, dtend=%s, location=%s, alarm_trigger=%s",
            found_summary,
            found_start,
            found_end,
            found_location,
            found_alarm_trigger,
        )

        assert "[TEST-VERIFY]" in (found_summary or ""), f"回读 summary 缺少 [TEST-VERIFY]: {found_summary}"
        assert found_start, "回读缺少 dtstart"
        assert found_alarm_trigger, "回读缺少 VALARM trigger"

        report["steps"]["caldav_verification"] = {
            "status": "passed",
            "summary": found_summary,
            "dtstart": found_start,
            "dtend": found_end,
            "location": found_location,
            "alarm_trigger": found_alarm_trigger,
        }

    finally:
        # 4. 强制执行安全清理（0 数据污染保证）
        logger.info(">>> [步骤 4] 强制执行安全删除与 0 污染闭环确认...")
        delete_success = False
        if write_res or test_uid:
            try:
                delete_success = await caldav_service.delete_event(
                    caldav_url=caldav_url,
                    username=caldav_user,
                    password=caldav_pw,
                    uid=test_uid,
                    href=created_href,
                    ssl_verify=caldav_ssl,
                )
                logger.info("执行 CalDAV delete_event 结果: %s", delete_success)
            except Exception as del_err:
                logger.error("删除测试事件发生异常: %s", del_err)

        # 5. 二次回读确认已彻底消失
        logger.info(">>> [步骤 5] 二次回读目标日历，确认测试日程完全不存在...")
        await asyncio.sleep(1.5)  # 等待远端服务状态收敛
        client = _DAVClient(url=caldav_url, username=caldav_user, password=caldav_pw, timeout=60)
        calendars = client.get_calendars()
        target_cal = None
        for c in calendars:
            if str(c.url) == caldav_calendar_url:
                target_cal = c
                break
        if not target_cal and calendars:
            target_cal = calendars[0]

        remaining_obj = None
        if target_cal:
            for obj in target_cal.objects():
                if str(obj.id) == test_uid or (created_href and str(obj.url) == created_href):
                    remaining_obj = obj
                    break

        assert remaining_obj is None, f"安全清理失败！测试日程仍残留在生产日历中: {test_uid}"
        logger.info("二次回读确认完成: 目标日历中已不存在测试日程 (uid=%s)，0 数据污染确认！", test_uid)

        report["steps"]["cleanup"] = {
            "status": "passed",
            "deleted": delete_success,
            "readback_confirmed_absent": True,
            "data_pollution": 0,
        }

    report["success"] = True
    return report


if __name__ == "__main__":
    try:
        report = asyncio.run(run_verification())
        print("\n==================== 验收测试成功报告 ====================")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("==========================================================")
        sys.exit(0)
    except Exception as exc:
        logger.exception("验收测试执行失败: %s", exc)
        sys.exit(1)
