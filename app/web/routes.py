from __future__ import annotations

import asyncio
from collections.abc import Generator, MutableMapping
from datetime import datetime, date, timedelta, timezone
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import base64
import hashlib
import importlib
import inspect
from io import BytesIO
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode
from urllib.parse import urlsplit
import zipfile

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.bootstrap import read_changes, read_version
from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.ai.providers import PROVIDER_PRESETS
from app.db.models import EventRecord, PasskeyCredential
from app.db.session import SessionLocal
from app.services.settings_service import SettingsService
from app.web.event_presenter import event_feedback
from app.web.connection_checks import revision, save_check, check_summary
from app.web.security import (
    client_ip,
    login_rate_limiter,
    passkey_failure_rate_limiter,
    passkey_request_rate_limiter,
    verify_turnstile,
)

if TYPE_CHECKING:
    from app.services.ai_provider_service import AIProviderConfig


router = APIRouter(prefix="/console")
templates = Jinja2Templates(directory="app/web/templates")
template_globals = cast(MutableMapping[str, object], templates.env.globals)
template_globals["app_version"] = read_version

_LAZY_COMPONENTS = {
    "dispatch_wechat_message": ("app.channels.wechat_handler", "dispatch_wechat_message"),
    "get_telegram_bot_runtime": ("app.services.telegram_service", "get_telegram_bot_runtime"),
    "get_discord_bot_runtime": ("app.services.discord_service", "get_discord_bot_runtime"),
    "get_wechat_bot_runtime": ("app.services.wechat_service", "get_wechat_bot_runtime"),
    "TelegramService": ("app.services.telegram_service", "TelegramService"),
    "DiscordService": ("app.services.discord_service", "DiscordService"),
    "WechatService": ("app.services.wechat_service", "WechatService"),
    "AIProviderConfig": ("app.services.ai_provider_service", "AIProviderConfig"),
    "AIProviderError": ("app.services.ai_provider_service", "AIProviderError"),
    "AIProviderService": ("app.services.ai_provider_service", "AIProviderService"),
    "CalDAVService": ("app.services.caldav_service", "CalDAVService"),
    "CalDAVServiceError": ("app.services.caldav_service", "CalDAVServiceError"),
    "ILinkClient": ("app.integrations.ilink", "ILinkClient"),
    "ILinkError": ("app.integrations.ilink", "ILinkError"),
}


def _load_component(name: str) -> Any:
    existing = globals().get(name)
    if existing is not None:
        return existing
    module_name, attribute = _LAZY_COMPONENTS[name]
    value = getattr(importlib.import_module(module_name), attribute)
    globals()[name] = value
    return value


def __getattr__(name: str) -> Any:
    if name in _LAZY_COMPONENTS:
        return _load_component(name)
    raise AttributeError(name)


def _telegram_service() -> Any:
    return _load_component("TelegramService")()


def _discord_service() -> Any:
    return _load_component("DiscordService")()


def _wechat_service() -> Any:
    return _load_component("WechatService")()


async def _close_ilink_client(client: object) -> None:
    close = getattr(client, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _unpack_ilink_updates(result: object) -> tuple[list[dict[str, object]], str]:
    if isinstance(result, tuple):
        messages, cursor = result
    else:
        messages = getattr(result, "messages", [])
        cursor = getattr(result, "cursor", "")
    typed_messages = [item for item in messages if isinstance(item, dict)]
    return cast(list[dict[str, object]], typed_messages), str(cursor or "")


def _wechat_probe_is_blocked() -> bool:
    runtime = _load_component("get_wechat_bot_runtime")()
    if runtime is None:
        return False
    return bool(getattr(runtime, "task_alive", getattr(runtime, "running", False)))


def _ai_components() -> tuple[Any, type[Exception], Any]:
    return (
        _load_component("AIProviderConfig"),
        _load_component("AIProviderError"),
        _load_component("AIProviderService"),
    )


def _caldav_components() -> tuple[Any, type[Exception]]:
    return _load_component("CalDAVService"), _load_component("CalDAVServiceError")


def _runtime_if_loaded(component: str) -> Any | None:
    existing = globals().get(component)
    if existing is not None:
        return existing()
    module_name, attribute = _LAZY_COMPONENTS[component]
    module = sys.modules.get(module_name)
    return getattr(module, attribute)() if module is not None else None


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def require_admin(request: Request) -> None:
    if not request.session.get("admin_authenticated"):
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/console/login"})


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=status.HTTP_303_SEE_OTHER)


def redirect_with_query(path: str, **params: str) -> RedirectResponse:
    return redirect(f"{path}?{urlencode(params)}")


def get_flash(request: Request) -> str | None:
    msg = request.session.get("flash")
    if msg:
        del request.session["flash"]
    return msg


def set_flash(request: Request, msg: str) -> None:
    request.session["flash"] = msg


def set_error_flash(request: Request, msg: str) -> None:
    request.session["error_flash"] = msg


def get_error_flash(request: Request) -> str | None:
    msg = request.session.get("error_flash")
    if msg:
        del request.session["error_flash"]
    return msg


def _verify_totp(settings_service: SettingsService, code: str) -> bool:
    secret = settings_service.get("admin_totp_secret")
    if not secret or not code.isdigit():
        return False
    pyotp = importlib.import_module("pyotp")
    totp = pyotp.TOTP(secret)
    current_counter = int(time.time()) // 30
    last_counter = int(settings_service.get("admin_totp_last_counter") or "-1")
    for offset in (-1, 0, 1):
        counter = current_counter + offset
        if counter > last_counter and secrets.compare_digest(totp.at(counter * 30), code):
            settings_service.set("admin_totp_last_counter", str(counter))
            settings_service.commit()
            return True
    return False


def _consume_recovery_code(settings_service: SettingsService, code: str) -> bool:
    normalized = code.strip().lower().replace(" ", "")
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    try:
        stored = json.loads(settings_service.get("admin_recovery_code_hashes") or "[]")
    except json.JSONDecodeError:
        return False
    matched = next((item for item in stored if secrets.compare_digest(item, digest)), None)
    if matched is None:
        return False
    stored.remove(matched)
    settings_service.set("admin_recovery_code_hashes", json.dumps(stored))
    settings_service.commit()
    return True


def _generate_recovery_codes() -> list[str]:
    return [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(8)]


def _passkey_config() -> tuple[str, str]:
    rp_id = settings.webauthn_rp_id.strip()
    origin = settings.public_origin.rstrip("/")
    if not rp_id or not origin:
        raise HTTPException(status_code=503, detail="通行密钥尚未配置公网域名。")
    return rp_id, origin


def _sanitize_probe_value(value: object) -> object:
    if isinstance(value, str):
        return value.replace("<", "").replace(">", "").replace('"', "").replace("'", "")[:500]
    if isinstance(value, list):
        return [_sanitize_probe_value(item) for item in value[:50]]
    if isinstance(value, dict):
        return {str(key)[:80]: _sanitize_probe_value(item) for key, item in list(value.items())[:80]}
    return value


def _sanitize_probe_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    return [cast(dict[str, object], _sanitize_probe_value(message)) for message in messages[:50]]


def _qr_image_data_url(payload: str) -> str | None:
    try:
        qrcode_module = importlib.import_module("qrcode")
    except ImportError:
        return None
    make_qr = cast(Any, qrcode_module).make
    image = make_qr(payload)
    buffer = BytesIO()
    save_image = getattr(image, "save")
    save_image(buffer, "PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{encoded}"


def _parse_event_date(start_time: str | None, tz: ZoneInfo) -> date | None:
    """Parse event start_time ISO string and return its date in the configured tz."""
    if not start_time:
        return None
    try:
        dt = datetime.fromisoformat(start_time)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).date()


def _parse_week_start(val: str) -> int:
    """Parse week_start_day setting: 0=Sunday, 1=Monday (default)."""
    return 0 if val == "0" else 1


def _calendar_timezone(settings_service: SettingsService) -> ZoneInfo:
    name = (settings_service.get("caldav_timezone") or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return ZoneInfo("Asia/Shanghai")


def _record_time(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return ""
    # SQLite returns UTC timestamps without tzinfo.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def dashboard_stats(session: Session, settings_service: SettingsService) -> dict[str, int | None]:
    tz = _calendar_timezone(settings_service)

    now = datetime.now(tz)
    today = now.date()

    week_start_day = _parse_week_start(settings_service.get("week_start_day") or "1")
    if week_start_day == 0:  # Sunday
        days_since_start = (today.weekday() + 1) % 7
    else:  # Monday
        days_since_start = today.weekday()

    week_start = today - timedelta(days=days_since_start)
    week_end = week_start + timedelta(days=6)
    month_start = today.replace(day=1)

    # SQLite stores UTC timestamps without tzinfo; compare against local-day UTC bounds.
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    day_end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)

    def count_records(*conditions: Any) -> int:
        return session.scalar(
            select(func.count()).select_from(EventRecord).where(
                EventRecord.created_at >= day_start,
                EventRecord.created_at < day_end,
                *conditions,
            )
        ) or 0

    def count_since(since_date: date) -> int:
        deleted_uids = select(EventRecord.caldav_uid).where(
            EventRecord.operation == "delete",
            EventRecord.caldav_uid.isnot(None),
        )
        return session.scalar(
            select(func.count()).select_from(EventRecord).where(
                EventRecord.operation == "create",
                EventRecord.status == "success",
                EventRecord.created_at >= datetime.combine(since_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None),
                EventRecord.created_at < day_end,
                or_(
                    EventRecord.caldav_uid.is_(None),
                    ~EventRecord.caldav_uid.in_(deleted_uids),
                ),
            )
        ) or 0

    all_records = session.execute(
        select(EventRecord).where(
            EventRecord.operation.in_(["create", "update", "delete"]),
            EventRecord.status == "success",
            EventRecord.start_time != "",
        ).order_by(EventRecord.created_at.desc())
    ).scalars().all()

    seen_events: set[str] = set()
    today_events = week_events = month_events = 0
    for rec in all_records:
        event_key = _event_key(rec)
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        if rec.operation == "delete":
            continue
        event_date = _parse_event_date(rec.start_time, tz)
        if event_date is None:
            continue
        if event_date == today:
            today_events += 1
        if week_start <= event_date <= week_end:
            week_events += 1
        if event_date.year == month_start.year and event_date.month == month_start.month:
            month_events += 1

    today_processed = count_records()
    today_failed = count_records(EventRecord.status == "failed")
    today_success = count_records(EventRecord.status == "success")
    today_finished = today_success + today_failed
    return {
        "today_created": count_since(today),
        "week_created": count_since(week_start),
        "month_created": count_since(month_start),
        "today_events": today_events,
        "week_events": week_events,
        "month_events": month_events,
        "today_processed": today_processed,
        "today_failed": today_failed,
        "today_no_event": count_records(EventRecord.operation == "no_event"),
        "today_quote_failures": count_records(EventRecord.operation == "quote_not_found"),
        "today_success_rate": (
            round(today_success * 100 / today_finished)
            if today_finished
            else None
        ),
    }


def status_context(session: Session) -> dict[str, object]:
    settings_service = SettingsService(session)
    ai_name = settings_service.get("ai_provider_name") or ""
    ai_model = settings_service.get("ai_model") or ""
    ai_ok = bool(ai_name and ai_model)
    vision_name = settings_service.get("ai_vision_provider_name") or ""
    vision_model = settings_service.get("ai_vision_model") or ""
    vision_use_main = settings_service.get("ai_vision_use_main") or "true"
    if vision_use_main != "false" and ai_ok:
        vision_label = "共用主模型"
    else:
        vision_label = f"{vision_name} / {vision_model}" if (vision_name and vision_model) else "共用主模型"
    caldav_url = settings_service.get("caldav_url") or ""
    caldav_cal = settings_service.get("caldav_calendar_name") or ""
    caldav_ok = bool(caldav_url and caldav_cal)
    caldav_source = ""
    if caldav_ok:
        from urllib.parse import urlparse
        host = urlparse(caldav_url).hostname or ""
        caldav_source = host.removeprefix("caldav.").removeprefix("dav.")

    activity_tz = _calendar_timezone(settings_service)
    recent = session.execute(
        select(EventRecord)
        .order_by(EventRecord.created_at.desc(), EventRecord.id.desc())
        .limit(5)
    ).scalars().all()
    activity = []
    for rec in recent:
        feedback = event_feedback(rec)
        activity.append({
            "id": rec.id,
            "title": rec.title or "未命名记录",
            "source": rec.source or "未知来源",
            "status": rec.status,
            "time": _record_time(rec.created_at, activity_tz),
            "original_text": rec.original_text or "",
            "failure_phase": rec.failure_phase or feedback.get("failure_phase", ""),
            "failure_phase_label": feedback.get("failure_phase_label", ""),
            "retry_count": rec.retry_count or 0,
            "can_retry": feedback.get("can_retry", False),
            **feedback,
        })

    connection_checks = {}
    for kind in ("ai", "caldav"):
        check = check_summary(session, kind)
        check["time"] = _record_time(check["at"], activity_tz)
        connection_checks[kind] = check

    current_version = settings_service.get_config_version()
    last_current_success = session.scalar(
        select(EventRecord.created_at).where(
            EventRecord.status == "success",
            EventRecord.operation.in_(["create", "update", "delete"]),
            EventRecord.config_version == current_version,
        ).order_by(EventRecord.created_at.desc()).limit(1)
    )
    last_legacy_success = session.scalar(
        select(EventRecord.created_at).where(
            EventRecord.status == "success",
            EventRecord.operation.in_(["create", "update", "delete"]),
            or_(
                EventRecord.config_version != current_version,
                EventRecord.config_version.is_(None),
            ),
        ).order_by(EventRecord.created_at.desc()).limit(1)
    )
    last_calendar_success_current = _record_time(last_current_success, activity_tz)
    last_calendar_success_legacy = _record_time(last_legacy_success, activity_tz)

    return {
        "connection_checks": connection_checks,
        "last_calendar_success": last_calendar_success_current or last_calendar_success_legacy,
        "last_calendar_success_current": last_calendar_success_current,
        "last_calendar_success_legacy": last_calendar_success_legacy,
        "current_config_version": current_version,
        "ai_ok": ai_ok,
        "ai_name": f"{ai_name} / {ai_model}" if ai_ok else "未配置",
        "vision_label": vision_label,
        "caldav_ok": caldav_ok,
        "caldav_name": caldav_cal if caldav_ok else "",
        "caldav_source": caldav_source,
        "tg_running": (
            (tg_runtime := _runtime_if_loaded("get_telegram_bot_runtime")) is not None
            and tg_runtime.running
        ),
        "dc_running": (
            (dc_runtime := _runtime_if_loaded("get_discord_bot_runtime")) is not None
            and dc_runtime.running
        ),
        "wechat_running": (
            (wechat_runtime := _runtime_if_loaded("get_wechat_bot_runtime")) is not None
            and wechat_runtime.running
        ),
        "recent_activity": activity,
        "activity_timezone": str(activity_tz),
        "version": read_version(),
        "changes": read_changes(),
    }


def _event_key(rec: EventRecord) -> str:
    return rec.event_id or rec.caldav_uid or f"_{rec.id}"


def ai_settings_payload(settings_service: SettingsService) -> dict[str, object]:
    provider_name = settings_service.get("ai_provider_name") or "OpenAI"
    provider_type = settings_service.get("ai_provider_type") or "openai_compatible"
    base_url = settings_service.get("ai_base_url") or next(
        (preset.base_url for preset in PROVIDER_PRESETS if preset.name == provider_name),
        "https://api.openai.com/v1",
    )
    model = settings_service.get("ai_model") or ""
    api_key_masked = settings_service.get_masked("ai_api_key")
    available_models = settings_service.get("ai_available_models") or ""
    return {
        "provider_name": provider_name,
        "provider_type": provider_type,
        "base_url": base_url,
        "model": model,
        "api_key_masked": api_key_masked,
        "available_models": [item for item in available_models.split(",") if item],
        "providers": [
            {"name": preset.name, "provider_type": preset.provider_type, "base_url": preset.base_url}
            for preset in PROVIDER_PRESETS
        ],
    }


def _probe_api_key(
    settings_service: SettingsService,
    submitted_key: str,
    submitted_provider_name: str,
    provider_setting: str,
    key_setting: str,
    clear_requested: str = "",
    submitted_provider_type: str = "",
    type_setting: str = "",
) -> str:
    if submitted_key:
        return submitted_key
    if clear_requested == "1":
        return ""
    stored_provider = settings_service.get(provider_setting) or ""
    if submitted_provider_name and submitted_provider_name != stored_provider:
        return ""
    stored_type = settings_service.get(type_setting) if type_setting else ""
    if submitted_provider_type and stored_type and submitted_provider_type != stored_type:
        return ""
    return settings_service.get(key_setting) or ""


def _model_list_value(raw: str) -> str:
    return ",".join(item.strip() for item in raw.split(",") if item.strip())


def _validated_provider_type(value: str, provider_name: str = "") -> str:
    preset = next((item for item in PROVIDER_PRESETS if item.name == provider_name), None)
    if preset is not None and preset.name != "Custom":
        return preset.provider_type
    if value not in {"openai_compatible", "anthropic"}:
        raise ValueError("不支持的 Provider 类型。")
    return value


def _probe_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, session: Session = Depends(get_db), _: None = Depends(require_admin)) -> HTMLResponse:
    settings_service = SettingsService(session)
    stats = dashboard_stats(session, settings_service)
    ctx = status_context(session)
    ctx["stats"] = stats
    ctx["request"] = request
    ctx["message"] = get_flash(request) or request.query_params.get("message")
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/wizard", response_class=HTMLResponse)
async def setup_wizard(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    settings_service = SettingsService(session)
    status_ctx = status_context(session)
    ai_data = ai_settings_payload(settings_service)
    caldav_data = caldav_payload(settings_service)
    tg_running = bool(status_ctx.get("tg_running"))
    dc_running = bool(status_ctx.get("dc_running"))
    wechat_running = bool(status_ctx.get("wechat_running"))

    payload = {
        "request": request,
        "ai": ai_data,
        "caldav": caldav_data,
        "status": status_ctx,
        "ai_ok": status_ctx.get("ai_ok"),
        "caldav_ok": status_ctx.get("caldav_ok"),
        "telegram_running": tg_running,
        "discord_running": dc_running,
        "wechat_running": wechat_running,
        "wechat_state": "running" if wechat_running else "stopped",
        "telegram_bot_token_masked": settings_service.get_masked("telegram_bot_token"),
        "discord_bot_token_masked": settings_service.get_masked("discord_bot_token"),
        "wechat_token_saved": bool(settings_service.get("wechat_bot_token")),
        "message": get_flash(request) or request.query_params.get("message"),
        "error": get_error_flash(request) or request.query_params.get("error"),
    }
    return templates.TemplateResponse(request, "wizard.html", payload)


@router.post("/wizard/test-event")
async def test_wizard_event(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    content = ""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            content = body.get("text", "")
        except Exception:
            content = ""
    else:
        try:
            form = await request.form()
            content = form.get("text", "")
        except Exception:
            content = ""
    content = str(content or "").strip()
    if not content:
        return JSONResponse({"ok": False, "error": "请输入测试日程文本（例如「明天下午3点项目进度会议」）。"}, status_code=400)
    try:
        from app.channels.message_processor import MessageProcessor
        processor = MessageProcessor()
        results = await processor.process(session, user_id="admin_wizard", text=content, source="wizard")
        if not results:
            return JSONResponse({"ok": True, "message": "消息已处理，但未提取到日程或无回复消息。", "data": {}})
        reply_texts = [r[0] for r in results]
        first_reply, rec_id = results[0]
        data_payload = {
            "record_id": rec_id,
            "reply": first_reply,
            "title": "",
            "source": "wizard",
        }
        if rec_id:
            rec = session.get(EventRecord, rec_id)
            if rec:
                data_payload.update({
                    "title": rec.title or "",
                    "source": rec.source or "wizard",
                    "status": rec.status or "",
                    "caldav_uid": rec.caldav_uid or "",
                })
        return JSONResponse({
            "ok": True,
            "message": "\n\n".join(reply_texts),
            "data": data_payload,
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"测试日程处理失败：{exc}"}, status_code=500)


@router.post("/connections/{kind}/test")
async def test_saved_connection(
    kind: str, session: Session = Depends(get_db), _: None = Depends(require_admin),
) -> JSONResponse:
    if kind not in {"ai", "caldav"}:
        raise HTTPException(status_code=404)
    service = SettingsService(session)
    signature = revision(session, kind)
    if kind == "ai":
        Config, ProviderError, Provider = _ai_components()
        model = service.get("ai_model") or ""
        if not model:
            return _probe_error("请先保存主模型配置。")
        config = Config(provider_type=service.get("ai_provider_type") or "openai_compatible",
            base_url=service.get("ai_base_url") or "https://api.openai.com/v1",
            api_key=service.get("ai_api_key") or "", model=model)
        try:
            await Provider().test_connection(config)
            ok = True
        except ProviderError:
            ok = False
    else:
        Provider, ProviderError = _caldav_components()
        url = service.get("caldav_url") or ""
        if not url:
            return _probe_error("请先保存日历连接配置。")
        try:
            await Provider().test_connection(url, service.get("caldav_username") or "",
                service.get("caldav_password") or "", ssl_verify=_caldav_ssl_from_settings(service))
            ok = True
        except ProviderError:
            ok = False
    save_check(session, kind, signature, ok)
    # A concurrent configuration change makes this result stale, never current.
    session.expire_all()
    summary = check_summary(session, kind)
    summary["time"] = _record_time(summary.pop("at"), _calendar_timezone(service))
    return JSONResponse({"ok": ok, "check": summary})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if request.session.get("admin_authenticated"):
        return redirect("/console")
    with SessionLocal() as session:
        settings_service = SettingsService(session)
        site_key = settings_service.get("turnstile_site_key") or ""
        enabled = bool(site_key and settings_service.get("turnstile_secret_key"))
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "turnstile_site_key": site_key if enabled else ""},
    )


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    turnstile_response: str = Form("", alias="cf-turnstile-response"),
    session: Session = Depends(get_db),
):
    settings_service = SettingsService(session)
    limiter_key = username.strip().lower()
    site_key = settings_service.get("turnstile_site_key") or ""
    turnstile_secret = settings_service.get("turnstile_secret_key") or ""
    turnstile_enabled = bool(site_key and turnstile_secret)
    login_context = {"turnstile_site_key": site_key if turnstile_enabled else ""}
    if login_rate_limiter.blocked(limiter_key):
        return templates.TemplateResponse(
            request,
            "login.html",
            {**login_context, "error": "登录失败次数过多，请 5 分钟后重试。"},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    if turnstile_enabled:
        public_hostname = urlsplit(settings.public_origin).hostname if settings.public_origin else None
        remote_ip = request.client.host if request.client else None
        if not await verify_turnstile(
            turnstile_response,
            turnstile_secret,
            remote_ip=remote_ip,
            expected_hostname=public_hostname,
        ):
            return templates.TemplateResponse(
                request,
                "login.html",
                {**login_context, "error": "人机验证失败，请刷新后重试。"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    saved_username = settings_service.get("admin_username")
    saved_password_hash = settings_service.get("admin_password_hash")
    if saved_username == username and saved_password_hash and verify_password(password, saved_password_hash):
        login_rate_limiter.success(limiter_key)
        request.session.clear()
        if settings_service.get("admin_totp_enabled") == "true":
            request.session["pending_admin_username"] = username
            request.session["pending_admin_started_at"] = int(time.time())
            return redirect("/console/login/2fa")
        request.session["admin_authenticated"] = True
        request.session["admin_username"] = username
        if settings_service.get("admin_password_changed") == "false":
            set_flash(request, "首次登录，请修改管理员密码。")
            return redirect("/console/system")
        return redirect("/console")
    login_rate_limiter.failure(limiter_key)
    return templates.TemplateResponse(
        request,
        "login.html",
        {**login_context, "error": "用户名或密码不正确。"},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@router.get("/login/2fa", response_class=HTMLResponse)
async def login_2fa_page(request: Request) -> Response:
    started_at = int(request.session.get("pending_admin_started_at") or 0)
    if not request.session.get("pending_admin_username") or time.time() - started_at > 300:
        request.session.clear()
        return redirect("/console/login")
    return templates.TemplateResponse(request, "login_2fa.html", {"error": None})


@router.post("/login/2fa")
async def login_2fa(
    request: Request,
    code: str = Form(...),
    session: Session = Depends(get_db),
) -> Response:
    username = request.session.get("pending_admin_username")
    started_at = int(request.session.get("pending_admin_started_at") or 0)
    if not username or time.time() - started_at > 300:
        request.session.clear()
        return redirect("/console/login")
    settings_service = SettingsService(session)
    limiter_key = f"2fa:{username}"
    if login_rate_limiter.blocked(limiter_key):
        return templates.TemplateResponse(
            request,
            "login_2fa.html",
            {"error": "验证失败次数过多，请 5 分钟后重新登录。"},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    if not (_verify_totp(settings_service, code.strip()) or _consume_recovery_code(settings_service, code)):
        login_rate_limiter.failure(limiter_key)
        return templates.TemplateResponse(
            request,
            "login_2fa.html",
            {"error": "验证码或恢复码不正确。"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    login_rate_limiter.success(limiter_key)
    request.session.clear()
    request.session["admin_authenticated"] = True
    request.session["admin_username"] = username
    return redirect("/console")


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return redirect("/console/login")


@router.get("/security", response_class=HTMLResponse)
async def security_settings(
    _request: Request,
    _: None = Depends(require_admin),
) -> RedirectResponse:
    return redirect("/console/system#security")


@router.post("/security/turnstile")
async def update_turnstile_settings(
    request: Request,
    site_key: str = Form(""),
    secret_key: str = Form(""),
    clear_secret: bool = Form(False),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    settings_service.set("turnstile_site_key", site_key.strip())
    if clear_secret:
        settings_service.set("turnstile_secret_key", None, encrypted=True)
    elif secret_key.strip():
        settings_service.set("turnstile_secret_key", secret_key.strip(), encrypted=True)
    settings_service.commit()
    set_flash(request, "Turnstile 配置已保存。")
    return redirect("/console/system#security")


@router.post("/security/totp/setup")
async def setup_totp(
    request: Request,
    current_password: str = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    settings_service = SettingsService(session)
    password_hash = settings_service.get("admin_password_hash")
    if not password_hash or not verify_password(current_password, password_hash):
        raise HTTPException(status_code=403, detail="当前密码不正确。")
    if settings_service.get("admin_totp_enabled") == "true":
        raise HTTPException(status_code=409, detail="两步验证已经启用。")
    pyotp = importlib.import_module("pyotp")
    secret = pyotp.random_base32()
    username = settings_service.get("admin_username") or "admin"
    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(
        name=username,
        issuer_name="AI Calendar Assistant",
    )
    request.session["pending_totp_secret"] = secret
    request.session["pending_totp_started_at"] = int(time.time())
    return JSONResponse({"secret": secret, "qr_image": _qr_image_data_url(provisioning_uri)})


@router.post("/security/totp/enable")
async def enable_totp(
    request: Request,
    code: str = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    secret = request.session.get("pending_totp_secret")
    started_at = int(request.session.get("pending_totp_started_at") or 0)
    if not secret or time.time() - started_at > 300:
        request.session.pop("pending_totp_secret", None)
        request.session.pop("pending_totp_started_at", None)
        set_error_flash(request, "TOTP 设置请求已过期，请重新验证当前密码。")
        return redirect("/console/system#security")
    pyotp = importlib.import_module("pyotp")
    if not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        set_error_flash(request, "动态验证码不正确。")
        return redirect("/console/system#security")
    recovery_codes = _generate_recovery_codes()
    hashes = [hashlib.sha256(item.encode()).hexdigest() for item in recovery_codes]
    settings_service.set("admin_totp_secret", secret, encrypted=True)
    settings_service.set("admin_totp_enabled", "true")
    settings_service.set("admin_totp_last_counter", "-1")
    settings_service.set("admin_recovery_code_hashes", json.dumps(hashes))
    settings_service.commit()
    request.session.pop("pending_totp_secret", None)
    request.session.pop("pending_totp_started_at", None)
    request.session["new_recovery_codes"] = recovery_codes
    set_flash(request, "两步验证已启用。请立即保存恢复码。")
    return redirect("/console/system#security")


@router.post("/security/totp/disable")
async def disable_totp(
    request: Request,
    current_password: str = Form(...),
    code: str = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    password_hash = settings_service.get("admin_password_hash")
    if not password_hash or not verify_password(current_password, password_hash):
        set_error_flash(request, "当前密码不正确。")
        return redirect("/console/system#security")
    if not (_verify_totp(settings_service, code.strip()) or _consume_recovery_code(settings_service, code)):
        set_error_flash(request, "验证码或恢复码不正确。")
        return redirect("/console/system#security")
    settings_service.set("admin_totp_secret", None, encrypted=True)
    settings_service.set("admin_totp_enabled", "false")
    settings_service.set("admin_totp_last_counter", "-1")
    settings_service.set("admin_recovery_code_hashes", "[]")
    settings_service.commit()
    set_flash(request, "两步验证已停用。")
    return redirect("/console/system#security")


@router.get("/security/passkeys")
async def list_passkeys(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> list[dict[str, object]]:
    rows = session.execute(select(PasskeyCredential).order_by(PasskeyCredential.created_at.desc())).scalars().all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "created_at": row.created_at.isoformat(),
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        }
        for row in rows
    ]


@router.post("/security/passkeys/register/options")
async def passkey_registration_options(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Response:
    rp_id, _origin = _passkey_config()
    body = await request.json()
    current_password = str(body.get("current_password") or "")
    settings_service = SettingsService(session)
    password_hash = settings_service.get("admin_password_hash")
    if not password_hash or not verify_password(current_password, password_hash):
        raise HTTPException(status_code=403, detail="当前密码不正确。")
    webauthn = importlib.import_module("webauthn")
    webauthn_helpers = importlib.import_module("webauthn.helpers")
    structs = importlib.import_module("webauthn.helpers.structs")
    rows = session.execute(select(PasskeyCredential)).scalars().all()
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name="AI Calendar Assistant",
        user_name=settings_service.get("admin_username") or "admin",
        user_id=hashlib.sha256((settings_service.get("admin_username") or "admin").encode()).digest(),
        authenticator_selection=structs.AuthenticatorSelectionCriteria(
            resident_key=structs.ResidentKeyRequirement.PREFERRED,
            user_verification=structs.UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            structs.PublicKeyCredentialDescriptor(id=webauthn_helpers.base64url_to_bytes(row.credential_id))
            for row in rows
        ],
    )
    request.session["passkey_registration_challenge"] = webauthn_helpers.bytes_to_base64url(options.challenge)
    request.session["passkey_registration_name"] = str(body.get("name") or "我的通行密钥").strip()[:100]
    request.session["passkey_registration_started_at"] = int(time.time())
    return Response(webauthn.options_to_json(options), media_type="application/json")


@router.post("/security/passkeys/register/verify")
async def verify_passkey_registration(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    rp_id, origin = _passkey_config()
    challenge = request.session.pop("passkey_registration_challenge", None)
    name = request.session.pop("passkey_registration_name", "我的通行密钥")
    started_at = int(request.session.pop("passkey_registration_started_at", 0))
    if not challenge or time.time() - started_at > 300:
        raise HTTPException(status_code=400, detail="注册请求已过期，请重试。")
    webauthn = importlib.import_module("webauthn")
    webauthn_helpers = importlib.import_module("webauthn.helpers")
    body = await request.json()
    try:
        verified = webauthn.verify_registration_response(
            credential=body,
            expected_challenge=webauthn_helpers.base64url_to_bytes(challenge),
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail="通行密钥验证失败。") from exc
    credential_id = webauthn_helpers.bytes_to_base64url(verified.credential_id)
    if session.scalar(select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id)):
        raise HTTPException(status_code=409, detail="此通行密钥已注册。")
    response_payload = body.get("response") if isinstance(body, dict) else None
    raw_transports = response_payload.get("transports") if isinstance(response_payload, dict) else None
    transports = [item for item in raw_transports if isinstance(item, str)][:10] if isinstance(raw_transports, list) else []
    session.add(
        PasskeyCredential(
            name=name or "我的通行密钥",
            credential_id=credential_id,
            public_key=webauthn_helpers.bytes_to_base64url(verified.credential_public_key),
            sign_count=verified.sign_count,
            transports=json.dumps(transports),
        )
    )
    session.commit()
    return JSONResponse({"ok": True})


@router.post("/security/passkeys/{credential_id}/delete")
async def delete_passkey(
    credential_id: int,
    current_password: str = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    settings_service = SettingsService(session)
    password_hash = settings_service.get("admin_password_hash")
    if not password_hash or not verify_password(current_password, password_hash):
        raise HTTPException(status_code=403, detail="当前密码不正确。")
    row = session.get(PasskeyCredential, credential_id)
    if row is None:
        raise HTTPException(status_code=404, detail="通行密钥不存在。")
    session.delete(row)
    session.commit()
    return JSONResponse({"ok": True})


@router.post("/login/passkey/options")
async def passkey_authentication_options(request: Request, session: Session = Depends(get_db)) -> Response:
    limiter_key = client_ip(request)
    if passkey_request_rate_limiter.blocked(limiter_key) or passkey_failure_rate_limiter.blocked(limiter_key):
        raise HTTPException(status_code=429, detail="通行密钥登录尝试过于频繁，请 5 分钟后重试。")
    passkey_request_rate_limiter.record(limiter_key)
    rp_id, _origin = _passkey_config()
    webauthn = importlib.import_module("webauthn")
    webauthn_helpers = importlib.import_module("webauthn.helpers")
    structs = importlib.import_module("webauthn.helpers.structs")
    rows = session.execute(select(PasskeyCredential)).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="尚未注册通行密钥。")
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        user_verification=structs.UserVerificationRequirement.REQUIRED,
        allow_credentials=[
            structs.PublicKeyCredentialDescriptor(id=webauthn_helpers.base64url_to_bytes(row.credential_id))
            for row in rows
        ],
    )
    request.session["passkey_authentication_challenge"] = webauthn_helpers.bytes_to_base64url(options.challenge)
    request.session["passkey_authentication_started_at"] = int(time.time())
    return Response(webauthn.options_to_json(options), media_type="application/json")


@router.post("/login/passkey/verify")
async def verify_passkey_authentication(request: Request, session: Session = Depends(get_db)) -> JSONResponse:
    limiter_key = client_ip(request)
    if passkey_request_rate_limiter.blocked(limiter_key) or passkey_failure_rate_limiter.blocked(limiter_key):
        raise HTTPException(status_code=429, detail="通行密钥登录尝试过于频繁，请 5 分钟后重试。")
    passkey_request_rate_limiter.record(limiter_key)
    rp_id, origin = _passkey_config()
    challenge = request.session.pop("passkey_authentication_challenge", None)
    started_at = int(request.session.pop("passkey_authentication_started_at", 0))
    if not challenge or time.time() - started_at > 300:
        passkey_failure_rate_limiter.record(limiter_key)
        raise HTTPException(status_code=400, detail="登录请求已过期，请重试。")
    body = await request.json()
    credential_id = str(body.get("id") or "")
    row = session.scalar(select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id))
    if row is None:
        passkey_failure_rate_limiter.record(limiter_key)
        raise HTTPException(status_code=401, detail="未知的通行密钥。")
    webauthn = importlib.import_module("webauthn")
    webauthn_helpers = importlib.import_module("webauthn.helpers")
    try:
        verified = webauthn.verify_authentication_response(
            credential=body,
            expected_challenge=webauthn_helpers.base64url_to_bytes(challenge),
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=webauthn_helpers.base64url_to_bytes(row.public_key),
            credential_current_sign_count=row.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        passkey_failure_rate_limiter.record(limiter_key)
        raise HTTPException(status_code=401, detail="通行密钥验证失败。") from exc
    row.sign_count = verified.new_sign_count
    row.last_used_at = datetime.now().astimezone()
    session.commit()
    passkey_failure_rate_limiter.success(limiter_key)
    settings_service = SettingsService(session)
    request.session.clear()
    request.session["admin_authenticated"] = True
    request.session["admin_username"] = settings_service.get("admin_username") or "admin"
    return JSONResponse({"ok": True})


@router.get("/system", response_class=HTMLResponse)
async def system_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    settings_service = SettingsService(session)
    recovery_codes = request.session.pop("new_recovery_codes", None)
    return templates.TemplateResponse(
        request,
        "system.html",
        {
            "username": settings_service.get("admin_username") or "admin",
            "session_days": settings_service.get("session_days") or "7",
            "event_record_limit": settings_service.get("event_record_limit") or "500",
            "week_start_day": settings_service.get("week_start_day") or "1",
            "totp_enabled": settings_service.get("admin_totp_enabled") == "true",
            "recovery_codes": recovery_codes,
            "turnstile_site_key": settings_service.get("turnstile_site_key") or "",
            "turnstile_secret_masked": settings_service.get_masked("turnstile_secret_key"),
            "public_origin": settings.public_origin or "未配置",
            "webauthn_rp_id": settings.webauthn_rp_id or "未配置",
            "secure_cookies": settings.secure_cookies,
            "message": get_flash(request) or request.query_params.get("message"),
            "error": get_error_flash(request) or request.query_params.get("error"),
        },
    )


@router.get("/ai", response_class=HTMLResponse)
async def ai_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    settings_service = SettingsService(session)
    payload = ai_settings_payload(settings_service)
    payload.update({
        "request": request,
        "message": get_flash(request) or request.query_params.get("message"),
        "error": get_error_flash(request) or request.query_params.get("error"),
    })
    payload["vision_use_main"] = settings_service.get("ai_vision_use_main") or "true"
    payload["vision_provider_name"] = settings_service.get("ai_vision_provider_name") or ""
    payload["vision_provider_type"] = settings_service.get("ai_vision_provider_type") or "openai_compatible"
    payload["vision_base_url"] = settings_service.get("ai_vision_base_url") or ""
    payload["vision_api_key_masked"] = settings_service.get_masked("ai_vision_api_key")
    payload["vision_model"] = settings_service.get("ai_vision_model") or ""
    vision_models = settings_service.get("ai_vision_available_models") or ""
    payload["vision_models"] = [item for item in vision_models.split(",") if item]
    return templates.TemplateResponse(request, "ai.html", payload)


@router.post("/ai")
async def update_ai_settings(
    request: Request,
    provider_name: str = Form(...),
    provider_type: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(""),
    clear_api_key: str = Form(""),
    model: str = Form(""),
    available_models_raw: str = Form(""),
    vision_use_main: str = Form("false"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    try:
        if not provider_name.strip():
            raise ValueError("请选择供应商。")
        if not model.strip():
            raise ValueError("请选择或输入模型。")
        provider_type = _validated_provider_type(provider_type, provider_name)
        normalized_base_url = _normalize_url(base_url)
    except ValueError as exc:
        set_error_flash(request, str(exc))
        return redirect("/console/ai")

    previous_provider = settings_service.get("ai_provider_name") or ""
    previous_provider_type = settings_service.get("ai_provider_type") or ""
    settings_service.set("ai_provider_name", provider_name)
    settings_service.set("ai_provider_type", provider_type)
    settings_service.set("ai_base_url", normalized_base_url)
    if api_key:
        settings_service.set("ai_api_key", api_key, encrypted=True)
    elif (
        clear_api_key == "1"
        or (previous_provider and previous_provider != provider_name)
        or (previous_provider_type and previous_provider_type != provider_type)
    ):
        settings_service.set("ai_api_key", None, encrypted=True)
    settings_service.set("ai_model", model)
    settings_service.set("ai_available_models", _model_list_value(available_models_raw))
    settings_service.set("ai_vision_use_main", "true" if vision_use_main == "1" else "false")
    settings_service.commit()
    set_flash(request, "AI 设置已保存。")
    return redirect("/console/ai")


@router.post("/ai/vision")
async def update_vision_settings(
    request: Request,
    vision_use_main: str = Form("1"),
    vision_provider_name: str = Form(""),
    vision_provider_type: str = Form(""),
    vision_base_url: str = Form(""),
    vision_api_key: str = Form(""),
    clear_vision_api_key: str = Form(""),
    vision_model: str = Form(""),
    vision_available_models_raw: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    if vision_use_main != "1":
        try:
            if not vision_provider_name.strip():
                raise ValueError("请选择识图供应商。")
            if not vision_model.strip():
                raise ValueError("请选择或输入识图模型。")
            vision_provider_type = _validated_provider_type(vision_provider_type, vision_provider_name)
            normalized_base_url = _normalize_url(vision_base_url)
        except ValueError as exc:
            set_error_flash(request, str(exc))
            return redirect("/console/ai")

        previous_provider = settings_service.get("ai_vision_provider_name") or ""
        previous_provider_type = settings_service.get("ai_vision_provider_type") or ""
        settings_service.set("ai_vision_provider_name", vision_provider_name)
        settings_service.set("ai_vision_provider_type", vision_provider_type)
        settings_service.set("ai_vision_base_url", normalized_base_url)
        if vision_api_key:
            settings_service.set("ai_vision_api_key", vision_api_key, encrypted=True)
        elif (
            clear_vision_api_key == "1"
            or (previous_provider and previous_provider != vision_provider_name)
            or (previous_provider_type and previous_provider_type != vision_provider_type)
        ):
            settings_service.set("ai_vision_api_key", None, encrypted=True)
        settings_service.set("ai_vision_model", vision_model)
        settings_service.set("ai_vision_available_models", _model_list_value(vision_available_models_raw))
    settings_service.set("ai_vision_use_main", "true" if vision_use_main == "1" else "false")
    settings_service.commit()
    set_flash(request, "识图模型设置已保存。")
    return redirect("/console/ai")


@router.post("/ai/models")
async def pull_ai_models(
    request: Request,
    provider_name: str = Form(""),
    provider_type: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    settings_service = SettingsService(session)
    try:
        provider_type = _validated_provider_type(
            provider_type or settings_service.get("ai_provider_type") or "openai_compatible",
            provider_name,
        )
        base_url = _normalize_url(
            base_url or settings_service.get("ai_base_url") or "https://api.openai.com/v1"
        )
    except ValueError as exc:
        return _probe_error(str(exc))
    api_key = _probe_api_key(
        settings_service,
        api_key,
        provider_name,
        "ai_provider_name",
        "ai_api_key",
        clear_api_key,
        provider_type,
        "ai_provider_type",
    )
    AIProviderConfig, AIProviderError, AIProviderService = _ai_components()
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key)
    try:
        models = await AIProviderService().list_models(config)
    except AIProviderError as exc:
        return _probe_error(str(exc), 502)
    return JSONResponse({"ok": True, "models": models, "message": f"模型列表已更新，共 {len(models)} 个。"})


@router.post("/ai/vision-models")
async def pull_vision_models(
    request: Request,
    vision_provider_name: str = Form(""),
    vision_provider_type: str = Form(""),
    vision_base_url: str = Form(""),
    vision_api_key: str = Form(""),
    clear_vision_api_key: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    settings_service = SettingsService(session)
    try:
        provider_type = _validated_provider_type(
            vision_provider_type or settings_service.get("ai_vision_provider_type") or "openai_compatible",
            vision_provider_name,
        )
        base_url = _normalize_url(
            vision_base_url
            or settings_service.get("ai_vision_base_url")
            or "https://api.openai.com/v1"
        )
    except ValueError as exc:
        return _probe_error(str(exc))
    api_key = _probe_api_key(
        settings_service,
        vision_api_key,
        vision_provider_name,
        "ai_vision_provider_name",
        "ai_vision_api_key",
        clear_vision_api_key,
        provider_type,
        "ai_vision_provider_type",
    )
    AIProviderConfig, AIProviderError, AIProviderService = _ai_components()
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key)
    try:
        models = await AIProviderService().list_models(config)
    except AIProviderError as exc:
        return _probe_error(str(exc), 502)
    return JSONResponse({"ok": True, "models": models, "message": f"识图模型列表已更新，共 {len(models)} 个。"})


@router.post("/ai/test")
async def test_ai_connection(
    request: Request,
    provider_name: str = Form(""),
    provider_type: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: str = Form(""),
    model: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    settings_service = SettingsService(session)
    try:
        provider_type = _validated_provider_type(
            provider_type or settings_service.get("ai_provider_type") or "openai_compatible",
            provider_name,
        )
        base_url = _normalize_url(
            base_url or settings_service.get("ai_base_url") or "https://api.openai.com/v1"
        )
    except ValueError as exc:
        return _probe_error(str(exc))
    api_key = _probe_api_key(
        settings_service,
        api_key,
        provider_name,
        "ai_provider_name",
        "ai_api_key",
        clear_api_key,
        provider_type,
        "ai_provider_type",
    )
    model = model or settings_service.get("ai_model") or ""
    AIProviderConfig, AIProviderError, AIProviderService = _ai_components()
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key, model=model)
    try:
        await AIProviderService().test_connection(config)
    except AIProviderError as exc:
        return _probe_error(str(exc), 502)
    return JSONResponse({"ok": True, "message": "AI 连接测试成功。"})


@router.post("/ai/schema-test")
async def test_ai_schema_compliance(
    request: Request,
    provider_name: str = Form(""),
    provider_type: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: str = Form(""),
    model: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    settings_service = SettingsService(session)
    try:
        provider_type = _validated_provider_type(
            provider_type or settings_service.get("ai_provider_type") or "openai_compatible",
            provider_name,
        )
        base_url = _normalize_url(
            base_url or settings_service.get("ai_base_url") or "https://api.openai.com/v1"
        )
    except ValueError as exc:
        return _probe_error(str(exc))
    api_key = _probe_api_key(
        settings_service,
        api_key,
        provider_name,
        "ai_provider_name",
        "ai_api_key",
        clear_api_key,
        provider_type,
        "ai_provider_type",
    )
    model = model or settings_service.get("ai_model") or ""
    AIProviderConfig, AIProviderError, AIProviderService = _ai_components()
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key, model=model)
    try:
        res = await AIProviderService().probe_schema_compliance(config)
    except AIProviderError as exc:
        return _probe_error(str(exc), 502)
    return JSONResponse({
        "ok": True,
        "message": f"结构化日程 Schema 探针验证成功（成功解析测试日程「{res.get('title')}」）。",
        "data": res,
    })


@router.post("/ai/vision-test")
async def test_vision_connection(
    request: Request,
    vision_provider_name: str = Form(""),
    vision_provider_type: str = Form(""),
    vision_base_url: str = Form(""),
    vision_api_key: str = Form(""),
    clear_vision_api_key: str = Form(""),
    vision_model: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    settings_service = SettingsService(session)
    try:
        provider_type = _validated_provider_type(
            vision_provider_type or settings_service.get("ai_vision_provider_type") or "openai_compatible",
            vision_provider_name,
        )
        base_url = _normalize_url(
            vision_base_url
            or settings_service.get("ai_vision_base_url")
            or "https://api.openai.com/v1"
        )
    except ValueError as exc:
        return _probe_error(str(exc))
    api_key = _probe_api_key(
        settings_service,
        vision_api_key,
        vision_provider_name,
        "ai_vision_provider_name",
        "ai_vision_api_key",
        clear_vision_api_key,
        provider_type,
        "ai_vision_provider_type",
    )
    model = vision_model or settings_service.get("ai_vision_model") or ""
    AIProviderConfig, AIProviderError, AIProviderService = _ai_components()
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key, model=model)
    try:
        await AIProviderService().test_connection(config)
    except AIProviderError as exc:
        return _probe_error(str(exc), 502)
    return JSONResponse({"ok": True, "message": "识图模型连接测试成功。"})


def current_ai_provider_config(settings_service: SettingsService) -> AIProviderConfig:
    AIProviderConfig = _load_component("AIProviderConfig")
    return AIProviderConfig(
        provider_type=settings_service.get("ai_provider_type") or "openai_compatible",
        base_url=_normalize_url(settings_service.get("ai_base_url") or "https://api.openai.com/v1"),
        api_key=settings_service.get("ai_api_key"),
        model=settings_service.get("ai_model"),
    )


def _normalize_url(url: str) -> str:
    normalized = url.strip()
    if not normalized:
        raise ValueError("请输入 Base URL。")
    if "://" not in normalized:
        normalized = "https://" + normalized
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Base URL 必须是有效的 HTTP 或 HTTPS 地址。")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Base URL 包含无效端口。") from exc
    if parsed.username or parsed.password:
        raise ValueError("Base URL 不能包含用户名或密码。")
    return normalized.rstrip("/")


def _normalize_caldav_int_setting(value: str, default: int, minimum: int, integer_error: str, minimum_error: str) -> tuple[int | None, str | None]:
    raw = value.strip() or str(default)
    try:
        parsed = int(raw)
    except ValueError:
        return None, integer_error
    if parsed < minimum:
        return None, minimum_error
    return parsed, None


def _caldav_ssl_from_settings(settings_service: SettingsService) -> bool:
    saved = settings_service.get("caldav_ssl_verify")
    return saved != "false"


def _caldav_ssl_from_form(value: str) -> bool:
    return value.strip().lower() == "true"


def _available_iana_timezones() -> list[str]:
    import zoneinfo
    try:
        return sorted(zoneinfo.available_timezones())
    except Exception:
        return ["Asia/Shanghai", "UTC"]


def caldav_payload(settings_service: SettingsService) -> dict[str, object]:
    return {
        "caldav_url": settings_service.get("caldav_url") or "",
        "caldav_username": settings_service.get("caldav_username") or "",
        "caldav_password_masked": settings_service.get_masked("caldav_password"),
        "caldav_calendar_url": settings_service.get("caldav_calendar_url") or "",
        "caldav_calendar_name": settings_service.get("caldav_calendar_name") or "",
        "caldav_timezone": settings_service.get("caldav_timezone") or "Asia/Shanghai",
        "caldav_reminder_minutes": settings_service.get("caldav_reminder_minutes") or "30",
        "caldav_default_duration": settings_service.get("caldav_default_duration") or "60",
        "caldav_ssl_verify": "true" if _caldav_ssl_from_settings(settings_service) else "false",
        "timezones": _available_iana_timezones(),
    }


@router.get("/caldav", response_class=HTMLResponse)
async def caldav_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    settings_service = SettingsService(session)
    payload = caldav_payload(settings_service)
    payload["request"] = request
    payload["message"] = get_flash(request) or request.query_params.get("message")
    payload["error"] = get_error_flash(request) or request.query_params.get("error")
    request.session.pop("caldav_calendars", None)
    return templates.TemplateResponse(request, "caldav.html", payload)


@router.post("/caldav")
async def update_caldav_settings(
    request: Request,
    caldav_url: str = Form(""),
    caldav_username: str = Form(""),
    caldav_password: str = Form(""),
    caldav_calendar_url: str = Form(""),
    caldav_calendar_name: str = Form(""),
    caldav_timezone: str = Form("Asia/Shanghai"),
    caldav_reminder_minutes: str = Form("30"),
    caldav_default_duration: str = Form("60"),
    caldav_ssl_verify: str = Form("false"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Response:
    reminder_val, error = _normalize_caldav_int_setting(
        caldav_reminder_minutes, 30, 0, "提醒分钟数必须是整数。", "提醒分钟数不能为负数。"
    )
    if error is not None or reminder_val is None:
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": False, "error": error or "提醒分钟数无效。"}, status_code=400)
        set_error_flash(request, error or "提醒分钟数无效。")
        return redirect("/console/caldav")

    duration_val, error = _normalize_caldav_int_setting(
        caldav_default_duration, 60, 5, "默认持续时间必须是整数。", "默认持续时间不能少于 5 分钟。"
    )
    if error is not None or duration_val is None:
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": False, "error": error or "默认持续时间无效。"}, status_code=400)
        set_error_flash(request, error or "默认持续时间无效。")
        return redirect("/console/caldav")

    settings_service = SettingsService(session)
    if not caldav_password and settings_service.get("caldav_password") and (
        caldav_url.strip().rstrip("/") != (settings_service.get("caldav_url") or "").rstrip("/")
        or caldav_username.strip() != (settings_service.get("caldav_username") or "")
    ):
        error = "服务器或用户名已更改，请填写对应的应用密码。"
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": False, "error": error}, status_code=400)
        set_error_flash(request, error)
        return redirect("/console/caldav")
    settings_service.set("caldav_url", caldav_url.strip())
    settings_service.set("caldav_username", caldav_username.strip())
    if caldav_password:
        settings_service.set("caldav_password", caldav_password, encrypted=True)
    settings_service.set("caldav_calendar_url", caldav_calendar_url.strip())
    settings_service.set("caldav_calendar_name", caldav_calendar_name.strip())
    settings_service.set("caldav_timezone", caldav_timezone.strip())
    settings_service.set("caldav_reminder_minutes", str(reminder_val))
    settings_service.set("caldav_default_duration", str(duration_val))
    settings_service.set("caldav_ssl_verify", "true" if _caldav_ssl_from_form(caldav_ssl_verify) else "false")
    settings_service.commit()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True, "message": "日历设置已保存。"})
    set_flash(request, "CalDAV 设置已保存。")
    return redirect("/console/caldav")


def _caldav_probe_credentials(service: SettingsService, url: str, username: str, password: str) -> tuple[str, str, str]:
    url, username = url.strip(), username.strip()
    if not url or not username:
        raise ValueError("请填写日历服务器地址和用户名。")
    try:
        _normalize_url(url)
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("请填写以 HTTP 或 HTTPS 开头的日历服务器地址。")
    except ValueError as exc:
        raise ValueError(str(exc).replace("Base URL", "日历服务器地址")) from exc
    if not password:
        same_account = (
            url.rstrip("/") == (service.get("caldav_url") or "").rstrip("/")
            and username == (service.get("caldav_username") or "")
        )
        if not same_account:
            raise ValueError("服务器或用户名已更改，请填写对应的应用密码。")
        password = service.get("caldav_password") or ""
    return url, username, password


@router.post("/caldav/test")
async def test_caldav_connection(
    request: Request,
    caldav_url: str = Form(""),
    caldav_username: str = Form(""),
    caldav_password: str = Form(""),
    caldav_ssl_verify: str = Form("false"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    return await _probe_caldav(session, caldav_url, caldav_username, caldav_password, caldav_ssl_verify, False)


@router.post("/caldav/calendars")
async def list_caldav_calendars(
    request: Request,
    caldav_url: str = Form(""),
    caldav_username: str = Form(""),
    caldav_password: str = Form(""),
    caldav_ssl_verify: str = Form("false"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    return await _probe_caldav(session, caldav_url, caldav_username, caldav_password, caldav_ssl_verify, True)


@router.post("/caldav/write-test")
async def test_caldav_write_permission(
    request: Request,
    caldav_url: str = Form(""),
    caldav_username: str = Form(""),
    caldav_password: str = Form(""),
    caldav_calendar_url: str = Form(""),
    caldav_ssl_verify: str = Form("false"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    try:
        url, username, password = _caldav_probe_credentials(
            SettingsService(session), caldav_url, caldav_username, caldav_password
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    CalDAVService, CalDAVServiceError = _caldav_components()
    try:
        service = CalDAVService()
        cal_url = caldav_calendar_url.strip() or None
        res = await service.probe_write(
            url,
            username,
            password,
            calendar_url=cal_url,
            ssl_verify=_caldav_ssl_from_form(caldav_ssl_verify),
        )
        return JSONResponse({
            "ok": True,
            "message": "日历写入探针成功：已创建临时测试日程并安全回滚清理，未污染日历数据。",
            "data": res,
        })
    except CalDAVServiceError as exc:
        return JSONResponse({"ok": False, "error": f"日历写入权限验证失败：{exc}"}, status_code=400)


async def _probe_caldav(session: Session, url: str, username: str, password: str, ssl: str, list_calendars: bool) -> JSONResponse:
    try:
        url, username, password = _caldav_probe_credentials(SettingsService(session), url, username, password)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    CalDAVService, CalDAVServiceError = _caldav_components()
    try:
        service = CalDAVService()
        if list_calendars:
            calendars = await service.list_calendars(url, username, password, ssl_verify=_caldav_ssl_from_form(ssl))
            return JSONResponse({"ok": True, "calendars": calendars, "message": f"发现 {len(calendars)} 个日历，请选择目标后保存。"})
        await service.test_connection(url, username, password, ssl_verify=_caldav_ssl_from_form(ssl))
        return JSONResponse({"ok": True, "message": "连接测试成功，尚未保存配置。此测试不验证日历写入权限。"})
    except CalDAVServiceError:
        return JSONResponse({"ok": False, "error": "无法连接日历服务，请检查服务器地址、应用密码和网络后重试。当前填写内容已保留。"}, status_code=400)


@router.get("/telegram", response_class=HTMLResponse)
async def telegram_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    service = _telegram_service()
    payload = service.config_summary(session)
    payload["request"] = request
    payload["message"] = get_flash(request) or request.query_params.get("message")
    payload["error"] = get_error_flash(request) or request.query_params.get("error")
    payload["bind_link"] = request.query_params.get("bind_link")
    payload["bind_token"] = request.query_params.get("bind_token")
    return templates.TemplateResponse(request, "telegram.html", payload)


@router.post("/telegram")
async def update_telegram_settings(
    request: Request,
    bot_token: str = Form(""),
    bot_username: str = Form(""),
    redirect_path: str = Form("", alias="redirect"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    token = bot_token.strip() or settings_service.get("telegram_bot_token") or ""
    username = bot_username.strip() or settings_service.get("telegram_bot_username") or ""
    if token:
        service = _telegram_service()
        service.save_token(session, token, username)
        if await service.reload_bot(token):
            pass
        target = redirect_path or "/console/telegram"
        set_flash(request, "Telegram Bot 已保存并重载。")
        return redirect(target)
    set_error_flash(request, "请填写 Bot Token。")
    return redirect("/console/telegram")


@router.post("/telegram/bind")
async def generate_bind_link(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    bot_username = settings_service.get("telegram_bot_username") or ""
    if not bot_username:
        set_error_flash(request, "请先配置 Bot Username。")
        return redirect("/console/telegram")
    service = _telegram_service()
    link, token = service.generate_bind_link(bot_username)
    set_flash(request, "绑定链接已生成。")
    return redirect_with_query("/console/telegram", bind_link=link, bind_token=token)


@router.get("/telegram/bind/status")
async def check_bind_status(token: str = "", _: None = Depends(require_admin)):
    if not token:
        return {"status": "expired"}
    service = _telegram_service()
    return {"status": service.check_bind_status(token)}


@router.post("/telegram/users/add")
async def add_telegram_user(
    request: Request,
    user_id: str = Form(...),
    username: str = Form(""),
    display_name: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    service = _telegram_service()
    service.add_user(session, user_id.strip(), username.strip(), display_name.strip())
    set_flash(request, f"已添加用户 {user_id}。")
    return redirect("/console/telegram")


@router.post("/telegram/users/remove")
async def remove_telegram_user(
    request: Request,
    user_id: str = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    service = _telegram_service()
    service.remove_user(session, user_id.strip())
    set_flash(request, f"已删除用户 {user_id}。")
    return redirect("/console/telegram")


@router.get("/discord", response_class=HTMLResponse)
async def discord_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    service = _discord_service()
    payload = service.config_summary(session)
    payload["request"] = request
    payload["message"] = get_flash(request) or request.query_params.get("message")
    payload["error"] = get_error_flash(request) or request.query_params.get("error")
    return templates.TemplateResponse(request, "discord.html", payload)


@router.post("/discord")
async def update_discord_settings(
    request: Request,
    bot_token: str = Form(""),
    application_id: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    token = bot_token.strip() or settings_service.get("discord_bot_token") or ""
    if not token:
        set_error_flash(request, "请填写 Bot Token。")
        return redirect("/console/discord")
    service = _discord_service()
    service.save_token(session, token, application_id.strip())
    if await service.reload_bot(token):
        pass
    set_flash(request, "Discord Bot 已保存并重载。")
    return redirect("/console/discord")


@router.post("/discord/users/add")
async def add_discord_user(
    request: Request,
    user_id: str = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    from app.db.models import DiscordIdentity
    uid = user_id.strip()
    existing = session.scalar(
        select(DiscordIdentity).where(DiscordIdentity.discord_user_id == uid)
    )
    if existing:
        existing.enabled = True
    else:
        session.add(DiscordIdentity(discord_user_id=uid, enabled=True))
    session.commit()
    set_flash(request, f"已授权用户 {uid}。")
    return redirect("/console/discord")


@router.post("/discord/users/remove")
async def remove_discord_user(
    request: Request,
    user_id: str = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    from app.db.models import DiscordIdentity
    ident = session.scalar(
        select(DiscordIdentity).where(DiscordIdentity.discord_user_id == user_id.strip())
    )
    if ident:
        ident.enabled = False
        session.commit()
    set_flash(request, f"已移除用户 {user_id}。")
    return redirect("/console/discord")


@router.get("/wechat", response_class=HTMLResponse)
async def wechat_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    service = _wechat_service()
    payload = service.config_summary(session)
    payload.setdefault("wechat_task_alive", payload.get("wechat_running", False))
    payload.setdefault("wechat_state", "polling" if payload["wechat_task_alive"] else "stopped")
    payload.setdefault("wechat_state_label", "在线" if payload["wechat_task_alive"] else "已停止")
    payload.setdefault("wechat_current_error", {"message": payload.get("wechat_error", "")} if payload.get("wechat_error") else None)
    payload.setdefault("wechat_last_error", None)
    payload.setdefault("wechat_consecutive_failures", 0)
    payload.setdefault("wechat_last_poll_success_at", None)
    payload.setdefault("wechat_last_message_at", None)
    payload.setdefault("wechat_next_retry_at", None)
    payload.setdefault("wechat_pause_until", None)
    payload["wechat_configured"] = payload["wechat_token_set"]
    payload["request"] = request
    payload["message"] = get_flash(request) or request.query_params.get("message")
    payload["error"] = get_error_flash(request) or request.query_params.get("error")
    return templates.TemplateResponse(request, "wechat.html", payload)


@router.get("/wechat/status")
async def wechat_status_json(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, object]:
    service = _wechat_service()
    summary = service.config_summary(session)
    task_alive = bool(summary.get("wechat_task_alive", summary.get("wechat_running", False)))
    state = str(summary.get("wechat_state") or ("polling" if task_alive else "stopped"))
    legacy_error = str(summary.get("wechat_error") or "")
    return {
        "configured": summary["wechat_token_set"],
        "running": summary["wechat_running"],
        "task_alive": task_alive,
        "state": state,
        "state_label": summary.get("wechat_state_label") or ("在线" if task_alive else "已停止"),
        "consecutive_failures": summary.get("wechat_consecutive_failures", 0),
        "last_poll_success_at": summary.get("wechat_last_poll_success_at"),
        "last_message_at": summary.get("wechat_last_message_at"),
        "next_retry_at": summary.get("wechat_next_retry_at"),
        "pause_until": summary.get("wechat_pause_until"),
        "current_error": summary.get("wechat_current_error") or ({"message": legacy_error} if legacy_error else None),
        "last_error": summary.get("wechat_last_error", legacy_error),
        "cursor_length": summary["wechat_cursor_length"],
    }


@router.post("/wechat/start")
async def start_wechat_runtime(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    token = settings_service.get("wechat_bot_token")
    if not token:
        set_error_flash(request, "WeChat Bot Token 未配置，请先扫码登录。")
        return redirect("/console/wechat")
    service = _wechat_service()
    __ = await service.reload_bot(token)
    set_flash(request, "WeChat Bot 已启动。")
    return redirect("/console/wechat")


@router.post("/wechat/stop")
async def stop_wechat_runtime(
    request: Request,
    _: None = Depends(require_admin),
) -> RedirectResponse:
    service = _wechat_service()
    await service.stop_bot()
    set_flash(request, "WeChat Bot 已停止。")
    return redirect("/console/wechat")


@router.post("/wechat/qr")
async def fetch_wechat_qr(
    _: None = Depends(require_admin),
) -> dict[str, object]:
    ILinkClient = _load_component("ILinkClient")
    ILinkError = _load_component("ILinkError")
    client = ILinkClient()
    try:
        detail = await client.get_qrcode_detail()
        qr_token = detail.get("qrcode")
        qr_payload = detail.get("qrcode_img_content") or detail.get("qrcode_url") or detail.get("url")
        if not isinstance(qr_token, str) or not qr_token:
            return {"error": "iLink 未返回 qrcode 轮询标识"}
        if not isinstance(qr_payload, str) or not qr_payload:
            qr_payload = qr_token
        return {"qr_payload": qr_payload, "qrcode": qr_token, "qr_image": _qr_image_data_url(qr_payload)}
    except ILinkError as exc:
        return {"error": str(exc)}
    finally:
        await _close_ilink_client(client)


@router.get("/wechat/qr/status")
async def wechat_qr_status(
    qrcode: str = "",
    _: None = Depends(require_admin),
) -> dict[str, object]:
    if not qrcode:
        return {"status": "expired"}
    ILinkClient = _load_component("ILinkClient")
    ILinkError = _load_component("ILinkError")
    client = ILinkClient()
    try:
        detail = await client.get_qrcode_status_detail(qrcode)
        status = detail.get("status") or detail.get("qrcode_status") or detail.get("state") or "unknown"
        if isinstance(status, str):
            status_str = status
        elif status is not None:
            status_str = str(status)
        else:
            status_str = "unknown"
        has_token = bool(detail.get("bot_token") or detail.get("token"))
        return {"status": status_str, "has_token": has_token, "qrcode": qrcode}
    except ILinkError:
        return {"status": "unknown", "qrcode": qrcode}
    finally:
        await _close_ilink_client(client)


@router.post("/wechat/save")
async def save_wechat_token(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, object]:
    try:
        body = await request.json()
    except Exception:
        return {"error": "无效的请求"}
    qrcode = body.get("qrcode", "")
    if not qrcode:
        return {"error": "缺少 qrcode 参数"}
    ILinkClient = _load_component("ILinkClient")
    ILinkError = _load_component("ILinkError")
    client = ILinkClient()
    try:
        detail = await client.get_qrcode_status_detail(qrcode)
        token = detail.get("bot_token") or detail.get("token") or ""
        if not token:
            return {"error": "未获取到 Bot Token"}
        settings_service = SettingsService(session)
        settings_service.set("wechat_bot_token", token, encrypted=True)
        settings_service.set("wechat_updates_buf", None)
        settings_service.commit()
        __ = await _wechat_service().reload_bot(token)
        return {"ok": True}
    except ILinkError as exc:
        return {"error": str(exc)}
    finally:
        await _close_ilink_client(client)


@router.post("/wechat/clear")
async def clear_wechat_token(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    await _wechat_service().stop_bot()
    settings_service = SettingsService(session)
    settings_service.set("wechat_bot_token", None)
    settings_service.set("wechat_updates_buf", None)
    settings_service.commit()
    set_flash(request, "WeChat Bot Token 已清除。")
    return redirect("/console/wechat")


@router.post("/wechat/probe")
async def probe_wechat_messages(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, object]:
    if _wechat_probe_is_blocked():
        return {"error": "WeChat Bot 运行中，不能启动第二个消息轮询。"}
    settings_service = SettingsService(session)
    token = settings_service.get("wechat_bot_token") or ""
    if not token:
        return {"error": "WeChat Bot Token 未配置，请先扫码登录。"}
    cursor = settings_service.get("wechat_updates_buf") or ""
    ILinkClient = _load_component("ILinkClient")
    ILinkError = _load_component("ILinkError")
    client = ILinkClient(token)
    try:
        result = await client.get_updates(cursor)
        messages, new_cursor = _unpack_ilink_updates(result)
    except ILinkError as exc:
        return {"error": str(exc)}
    finally:
        await _close_ilink_client(client)
    settings_service.set("wechat_updates_buf", new_cursor)
    settings_service.commit()
    return {
        "messages": _sanitize_probe_messages(cast(list[dict[str, object]], messages)),
        "cursor": new_cursor,
        "count": len(messages),
    }


@router.post("/wechat/probe/process")
async def probe_and_process_wechat_messages(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, object]:
    if _wechat_probe_is_blocked():
        return {"error": "WeChat Bot 运行中，不能启动第二个消息轮询。"}
    settings_service = SettingsService(session)
    token = settings_service.get("wechat_bot_token") or ""
    if not token:
        return {"error": "WeChat Bot Token 未配置，请先扫码登录。"}
    cursor = settings_service.get("wechat_updates_buf") or ""
    ILinkClient = _load_component("ILinkClient")
    ILinkError = _load_component("ILinkError")
    dispatch_wechat_message = _load_component("dispatch_wechat_message")
    client = ILinkClient(token)
    try:
        result = await client.get_updates(cursor)
        messages, new_cursor = _unpack_ilink_updates(result)
    except ILinkError as exc:
        await _close_ilink_client(client)
        return {"error": str(exc)}

    try:
        processed: list[dict[str, object]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            replies = await dispatch_wechat_message(cast(dict[str, object], message), session, client)
            processed.append({
                "message_id": str(message.get("message_id") or ""),
                "from_user_id": str(message.get("from_user_id") or ""),
                "replies": replies,
            })
        settings_service.set("wechat_updates_buf", new_cursor)
        settings_service.commit()
        return {"processed": processed, "cursor": new_cursor, "count": len(processed)}
    finally:
        await _close_ilink_client(client)


@router.post("/wechat/probe/clear-cursor")
async def clear_wechat_probe_cursor(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict[str, object]:
    settings_service = SettingsService(session)
    settings_service.set("wechat_updates_buf", None)
    settings_service.commit()
    return {"ok": True}


@router.get("/events", response_class=HTMLResponse)
async def event_records(
    request: Request,
    status_filter: str = "",
    search: str = "",
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
    page: int = 1,
    source: str = "",
    date_from: str = "",
    date_to: str = "",
) -> HTMLResponse:
    status_filter = status_filter if status_filter in {"all", "success", "failed"} else "all"
    search = search.strip()
    source = source if source in {"wechat", "telegram", "discord", "wizard"} else ""
    activity_tz = _calendar_timezone(SettingsService(session))
    filters = []
    filter_error = ""
    parsed_dates = []
    for raw in (date_from, date_to):
        try:
            value = date.fromisoformat(raw) if raw else None
            if value and value.year in (1, 9999):
                raise ValueError("date boundary")
            parsed_dates.append(value)
        except ValueError:
            parsed_dates.append(None)
            filter_error = "请输入有效日期。"
    start, end = parsed_dates
    if start and end and start > end:
        filter_error = "开始日期不能晚于结束日期。"
    if status_filter != "all":
        filters.append(EventRecord.status == status_filter)
    if search:
        filters.append(or_(EventRecord.original_text.contains(search), EventRecord.title.contains(search)))
    if source:
        filters.append(EventRecord.source == source)
    if start:
        filters.append(EventRecord.created_at >= datetime.combine(start, datetime.min.time(), tzinfo=activity_tz).astimezone(timezone.utc).replace(tzinfo=None))
    if end:
        # Inclusive date without overflowing on 9999-12-31.
        filters.append(EventRecord.created_at <= datetime.combine(end, datetime.max.time(), tzinfo=activity_tz).astimezone(timezone.utc).replace(tzinfo=None))
    if filter_error:
        filters.append(EventRecord.id < 0)
    total = session.scalar(select(func.count(EventRecord.id)).where(*filters)) or 0
    page_size = 25
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    records = session.execute(select(EventRecord).where(*filters)
        .order_by(EventRecord.created_at.desc(), EventRecord.id.desc())
        .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    def page_url(number: int) -> str:
        return "/console/events?" + urlencode(dict(status_filter=status_filter, search=search,
            source=source, date_from=date_from, date_to=date_to, page=number))
    events: list[dict[str, object]] = []
    for rec in records:
        feedback = event_feedback(rec)
        events.append({
            "id": rec.id,
            "time": _record_time(rec.created_at, activity_tz),
            "source": rec.source or "",
            "user": rec.source_user_id or rec.telegram_user_id or "",
            "conversation": rec.conversation_id or "",
            "operation": rec.operation or "",
            "title": rec.title or "",
            "start_time": rec.start_time or "",
            "is_recurring": "🔁" if rec.is_recurring else "",
            **feedback,
            "status": rec.status or "",
            "error": rec.error_message or "",
            "original_text": rec.original_text or "",
            "event_json": rec.event_json or "",
            "caldav_href": rec.caldav_href or "",
            "caldav_uid": rec.caldav_uid or "",
            "config_version": rec.config_version,
            "ai_config_hash": rec.ai_config_hash,
            "caldav_config_hash": rec.caldav_config_hash,
            "failure_phase": rec.failure_phase or feedback.get("failure_phase", ""),
            "failure_phase_label": feedback.get("failure_phase_label", ""),
            "retry_count": rec.retry_count or 0,
            "can_retry": feedback.get("can_retry", False),
        })

    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "events": events,
            "source": source, "date_from": date_from, "date_to": date_to,
            "filter_error": filter_error, "total": total, "page": page, "pages": pages,
            "first_record": (page - 1) * page_size + 1 if total else 0,
            "last_record": min(page * page_size, total),
            "previous_url": page_url(page - 1) if page > 1 else "",
            "next_url": page_url(page + 1) if page < pages else "",
            "activity_timezone": str(activity_tz),
            "status_filter": status_filter,
            "search": search,
            "statuses": [("all", "全部"), ("success", "成功"), ("failed", "失败")],
            "message": get_flash(request) or request.query_params.get("message"),
        },
    )


@router.post("/system/admin")
async def update_admin_settings(
    request: Request,
    username: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    saved_password_hash = settings_service.get("admin_password_hash")
    if new_password or confirm_password:
        if new_password != confirm_password:
            set_error_flash(request, "两次输入的新密码不一致。")
            return redirect("/console/system")
        if not saved_password_hash or not verify_password(current_password, saved_password_hash):
            set_error_flash(request, "当前密码不正确。")
            return redirect("/console/system")
        settings_service.set("admin_password_hash", hash_password(new_password))
        settings_service.set("admin_password_changed", "true")

    settings_service.set("admin_username", username.strip() or "admin")
    settings_service.commit()
    set_flash(request, "管理员设置已保存。")
    return redirect("/console/system")


@router.post("/system/data")
async def update_data_settings(
    request: Request,
    event_record_limit: int = Form(...),
    week_start_day: str | None = Form(None),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    if event_record_limit < 1 or event_record_limit > 100000:
        set_error_flash(request, "记录保留数量必须在 1 到 100000 之间。")
        return redirect("/console/system")

    if week_start_day is not None and week_start_day not in ("0", "1"):
        set_error_flash(request, "每周起始日必须是周日或周一。")
        return redirect("/console/system")

    settings_service = SettingsService(session)
    if week_start_day is not None:
        settings_service.set("week_start_day", week_start_day)
    settings_service.set("event_record_limit", str(event_record_limit))
    settings_service.commit()
    prune_event_records(session, event_record_limit)
    set_flash(request, "系统设置已保存。")
    return redirect("/console/system")


@router.post("/system/preferences")
async def update_preferences(
    request: Request,
    week_start_day: str = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    if week_start_day not in ("0", "1"):
        set_error_flash(request, "每周起始日必须是周日或周一。")
        return redirect("/console/system")
    service = SettingsService(session)
    service.set("week_start_day", week_start_day)
    service.commit()
    set_flash(request, "日程偏好已保存。")
    return redirect("/console/system")


@router.post("/events/clear")
async def clear_event_records(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    result = session.execute(delete(EventRecord))
    result.close()
    session.commit()
    set_flash(request, "事件记录已清空。")
    return redirect("/console/events")


_retry_locks: set[int] = set()
_retry_mutex = asyncio.Lock()


@router.post("/events/{event_id}/retry")
async def retry_event(
    request: Request,
    event_id: int,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> JSONResponse:
    async with _retry_mutex:
        if event_id in _retry_locks:
            return JSONResponse(
                {"ok": False, "error": "该日程正在重试写入中，请勿重复操作", "record_id": event_id},
                status_code=409,
            )
        _retry_locks.add(event_id)

    try:
        rec = session.get(EventRecord, event_id)
        if not rec:
            return JSONResponse(
                {"ok": False, "error": "未找到指定的事件记录", "record_id": event_id},
                status_code=404,
            )

        if rec.status == "success":
            return JSONResponse(
                {"ok": False, "error": "该日程已成功写入日历，无需重试", "record_id": event_id},
                status_code=400,
            )

        phase = rec.failure_phase or ("write" if rec.operation in ("create", "update") else "other")
        if phase != "write" or rec.operation not in ("create", "update"):
            return JSONResponse(
                {"ok": False, "error": "仅支持日历写入阶段失败的记录进行重试", "record_id": event_id},
                status_code=400,
            )

        if not rec.event_json:
            return JSONResponse(
                {"ok": False, "error": "缺少有效的日程数据，无法重试", "record_id": event_id},
                status_code=400,
            )

        try:
            event_payload = json.loads(rec.event_json)
            if not isinstance(event_payload, dict) or not event_payload.get("title"):
                return JSONResponse(
                    {"ok": False, "error": "日程数据格式无效或缺少标题，无法重试", "record_id": event_id},
                    status_code=400,
                )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"解析日程数据失败：{exc}", "record_id": event_id},
                status_code=400,
            )

        settings_service = SettingsService(session)
        caldav_url = settings_service.get("caldav_url") or ""
        caldav_user = settings_service.get("caldav_username") or ""
        caldav_pw = settings_service.get("caldav_password") or ""
        caldav_calendar_url = settings_service.get("caldav_calendar_url") or ""
        caldav_ssl = _caldav_ssl_from_settings(settings_service)

        if not caldav_url or not caldav_user:
            return JSONResponse(
                {"ok": False, "error": "尚未配置有效的日历连接，请先在日历设置中配置", "record_id": event_id},
                status_code=400,
            )

        # Idempotent caldav_uid: reuse existing UID to prevent duplicate calendar events
        target_uid = rec.caldav_uid or f"retry-{uuid.uuid4().hex}"
        if not rec.caldav_uid:
            rec.caldav_uid = target_uid
            session.flush()

        CalDAVService_cls, CalDAVServiceError_cls = _caldav_components()
        caldav_service = CalDAVService_cls()

        # Extract & format reminders
        reminders_data = event_payload.get("reminders")
        formatted_reminders = []
        if reminders_data:
            for r in reminders_data:
                if isinstance(r, dict) and "minutes_before" in r:
                    formatted_reminders.append({"minutes_before": int(r["minutes_before"])})
                elif hasattr(r, "minutes_before"):
                    formatted_reminders.append({"minutes_before": int(r.minutes_before)})
                elif isinstance(r, int):
                    formatted_reminders.append({"minutes_before": r})

        try:
            write_res = await caldav_service.create_event(
                caldav_url=caldav_url,
                username=caldav_user,
                password=caldav_pw,
                calendar_url=caldav_calendar_url or None,
                title=event_payload.get("title", "日程"),
                start_time=event_payload.get("start_time", ""),
                end_time=event_payload.get("end_time"),
                timezone_str=event_payload.get("timezone", "Asia/Shanghai"),
                location=event_payload.get("location"),
                description=event_payload.get("description"),
                reminders=formatted_reminders if formatted_reminders else None,
                recurrence=event_payload.get("recurrence"),
                is_all_day=bool(event_payload.get("is_all_day", False)),
                ssl_verify=caldav_ssl,
                uid=target_uid,
            )
        except CalDAVServiceError_cls as exc:
            rec.retry_count = (rec.retry_count or 0) + 1
            rec.error_message = f"重试失败：{exc}"
            rec.failure_phase = "write"
            rec.updated_at = datetime.now(timezone.utc)
            session.commit()
            return JSONResponse(
                {"ok": False, "error": f"重试写入日历失败：{exc}", "record_id": event_id, "retry_count": rec.retry_count},
                status_code=400,
            )
        except Exception as exc:
            rec.retry_count = (rec.retry_count or 0) + 1
            rec.error_message = f"重试异常：{exc}"
            rec.failure_phase = "write"
            rec.updated_at = datetime.now(timezone.utc)
            session.commit()
            return JSONResponse(
                {"ok": False, "error": f"重试写入日历异常：{exc}", "record_id": event_id, "retry_count": rec.retry_count},
                status_code=500,
            )

        # Success!
        rec.status = "success"
        rec.failure_phase = None
        rec.error_message = None
        rec.caldav_uid = write_res.get("uid", target_uid)
        rec.caldav_href = write_res.get("href")
        rec.retry_count = (rec.retry_count or 0) + 1
        rec.updated_at = datetime.now(timezone.utc)
        try:
            rec.config_version = settings_service.get_config_version()
            rec.caldav_config_hash = settings_service.get_caldav_config_hash()
        except Exception:
            pass
        session.commit()

        return JSONResponse({
            "ok": True,
            "message": "日程重试写入成功！",
            "data": {
                "id": rec.id,
                "status": "success",
                "caldav_uid": rec.caldav_uid,
                "caldav_href": rec.caldav_href,
                "retry_count": rec.retry_count,
            },
        })
    finally:
        async with _retry_mutex:
            _retry_locks.discard(event_id)


@router.get("/system/backup")
async def download_backup(_request: Request, _: None = Depends(require_admin)):
    buf = BytesIO(_create_backup_archive(Path("data")))
    from datetime import date
    from starlette.responses import StreamingResponse
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=backup-{date.today()}.zip"})


def _create_backup_archive(data_dir: Path) -> bytes:
    buffer = BytesIO()
    with tempfile.TemporaryDirectory(prefix="ai-calendar-backup-") as tmp:
        snapshot_path = Path(tmp) / "app.db"
        database_path = data_dir / "app.db"
        if database_path.exists():
            source = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True)
            target = sqlite3.connect(snapshot_path)
            try:
                source.backup(target)
                integrity = target.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise RuntimeError("SQLite backup integrity check failed")
            finally:
                target.close()
                source.close()

        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if snapshot_path.exists():
                archive.write(snapshot_path, "app.db")
            secrets_path = data_dir / "secrets.json"
            if secrets_path.exists():
                archive.write(secrets_path, "secrets.json")
    return buffer.getvalue()


def prune_event_records(session: Session, limit: int) -> None:
    ids_to_keep = select(EventRecord.id).order_by(EventRecord.created_at.desc()).limit(limit).subquery()
    result = session.execute(delete(EventRecord).where(EventRecord.id.not_in(select(ids_to_keep.c.id))))
    result.close()
    session.commit()
