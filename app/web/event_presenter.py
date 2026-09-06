"""Read-only presentation of current and legacy event records."""
import json
import re

from app.db.models import EventRecord


def event_feedback(record: EventRecord) -> dict[str, object]:
    operation = record.operation or ""
    result: dict[str, object] = {
        "operation_label": {"create": "创建", "update": "修改", "delete": "删除", "no_event": "识别", "quote_not_found": "引用定位"}.get(operation, "处理"),
        "result_label": "成功" if record.status == "success" else "待处理",
        "reason": "", "suggestion": "", "action_url": "", "action_label": "",
        "failure_phase": getattr(record, "failure_phase", None),
        "failure_phase_label": None,
        "failure_stage": getattr(record, "failure_phase", None),
        "failure_stage_label": None,
        "can_retry": False,
    }
    if record.status != "failed":
        return result
    try:
        payload = json.loads(record.event_json or "{}")
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    missing = payload.get("missing_fields")
    details = " ".join(str(x) for x in missing) if isinstance(missing, list) else ""
    error = (record.error_message or "") + " " + details
    lower = error.lower()

    explicit_phase = getattr(record, "failure_phase", None)
    if explicit_phase == "write" or (not explicit_phase and operation in {"create", "update", "delete"}):
        stage = "write"
        stage_label = "日历写入失败"
        can_retry = bool(record.event_json and operation in {"create", "update"})
    elif explicit_phase == "validation" or (not explicit_phase and (missing or "缺少字段" in error or payload.get("unsupported_reason") or error.startswith("不支持") or operation == "quote_not_found")):
        stage = "validation"
        stage_label = "校验失败"
        can_retry = False
    else:
        stage = "extraction"
        stage_label = "提取失败"
        can_retry = False

    result.update({
        "failure_phase": stage,
        "failure_phase_label": stage_label,
        "failure_stage": stage,
        "failure_stage_label": stage_label,
        "can_retry": can_retry,
    })

    def feedback(label: str, reason: str, suggestion: str, target: str = "") -> dict[str, str]:
        result.update(result_label=label, reason=reason, suggestion=suggestion)
        if target:
            result.update(action_url=f"/console/{target}", action_label="查看 AI 设置" if target == "ai" else "查看日历设置")
        return result

    if operation == "quote_not_found":
        return feedback("引用未找到", "没有找到被引用消息对应的日程。", "在聊天中重新引用助手发出的日程消息，再补充修改要求。")
    # Write failures must never be diagnosed as AI errors based on an HTTP code alone.
    if operation in {"create", "update", "delete"}:
        return feedback("日历操作失败", "这次日历操作未能确认成功。", "检查日历连接，并核对目标日历是否已发生变化，再决定是否重新操作。", "caldav")
    system_error = bool(payload.get("error_type")) or "ai 调用失败" in lower
    if system_error:
        if any(x in lower for x in ("usage limit", "insufficient_quota", "quota exceeded", "额度", "余额不足")):
            return feedback("AI 额度不足", "模型服务报告额度不足或使用上限已达到。", "检查供应商额度，或切换可用模型。恢复后先核对是否已有对应日程，再重新发送原始消息。", "ai")
        if re.search(r"\b429\b", lower) or "rate_limit" in lower or "rate limit" in lower:
            return feedback("AI 请求受限", "模型服务限制了这次请求。", "查看供应商限额并稍后再试；单凭 429 无法确定是频率还是额度限制。", "ai")
        if re.search(r"\b(401|403)\b", lower) or "authentication" in lower:
            return feedback("AI 访问受限", "模型服务拒绝了这次请求。", "检查 API Key、模型权限和网关访问规则。", "ai")
        if any(x in lower for x in ("timeout", "timed out", "connection", "超时", "连接")):
            return feedback("AI 连接异常", "请求超时或无法连接模型服务。", "检查服务地址与网络，恢复后核对日程再重新发送。", "ai")
        return feedback("AI 处理失败", "模型调用或结果解析未完成。", "检查模型设置及连接测试；可展开技术详情查看原始错误。", "ai")
    if missing or "缺少字段" in error:
        return feedback("待补充信息", "这条输入缺少创建日程所需的信息。", "回到原聊天补充日期、时间和事项；间隔较久时重新发送完整描述。")
    if payload.get("unsupported_reason") or error.startswith("不支持"):
        return feedback("暂不支持", "这条请求包含暂不支持的日程要求。", "尝试拆分为简单日程，并在原聊天中重新描述。")
    if operation == "no_event" and (payload.get("intent") == "no_event" or "未识别到日程" in error):
        return feedback("未识别到日程", "这条输入没有识别出明确的日程。", "如果需要安排日程，请补充日期、时间和具体事项。")
    return feedback("处理失败", "这条记录未能处理完成，现有信息不足以确定原因。", "展开技术详情查看原始信息，再检查相关配置。")
