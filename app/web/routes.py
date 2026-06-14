from collections.abc import Generator, MutableMapping
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import base64
import importlib
from io import BytesIO
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.channels.wechat_handler import dispatch_wechat_message
from app.core.bootstrap import read_changes, read_version
from app.core.security import hash_password, verify_password
from app.services.telegram_service import get_telegram_bot_runtime
from app.services.discord_service import get_discord_bot_runtime, DiscordService
from app.ai.providers import CLAUDE_MODELS, PROVIDER_PRESETS
from app.db.models import EventRecord
from app.db.session import SessionLocal
from app.services.ai_provider_service import AIProviderConfig, AIProviderError, AIProviderService
from app.services.caldav_service import CalDAVService, CalDAVServiceError
from app.integrations.ilink import ILinkAuthError, ILinkClient, ILinkError
from app.services.settings_service import SettingsService
from app.services.telegram_service import TelegramService
from app.services.wechat_service import WechatService, get_wechat_bot_runtime


router = APIRouter(prefix="/console")
templates = Jinja2Templates(directory="app/web/templates")
template_globals = cast(MutableMapping[str, object], templates.env.globals)
template_globals["app_version"] = read_version


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


def dashboard_stats(session: Session, settings_service: SettingsService) -> dict[str, int]:
    tz_name = (settings_service.get("caldav_timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("Asia/Shanghai")

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

    def count_since(since_date: date) -> int:
        deleted_uids = select(EventRecord.caldav_uid).where(
            EventRecord.operation == "delete",
            EventRecord.caldav_uid.isnot(None),
        )
        return session.scalar(
            select(func.count()).select_from(EventRecord).where(
                EventRecord.operation == "create",
                EventRecord.status == "success",
                EventRecord.created_at >= since_date,
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

    return {
        "today_created": count_since(today),
        "week_created": count_since(week_start),
        "month_created": count_since(month_start),
        "today_events": today_events,
        "week_events": week_events,
        "month_events": month_events,
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

    recent = session.execute(
        select(EventRecord).where(
            EventRecord.operation.in_(["create", "update", "delete"]),
            EventRecord.status == "success",
        ).order_by(EventRecord.created_at.desc())
    ).scalars().all()

    seen: set[str] = set()
    deduped: list[EventRecord] = []
    for rec in recent:
        event_key = _event_key(rec)
        if event_key not in seen:
            seen.add(event_key)
            if rec.operation != "delete":
                deduped.append(rec)
            if len(deduped) >= 5:
                break

    events = []
    for rec in deduped:
        events.append({
            "time": rec.created_at.strftime("%m-%d %H:%M") if rec.created_at else "",
            "operation": rec.operation or "",
            "title": rec.title or "",
            "status": rec.status or "",
            "start": rec.start_time or "",
            "end": "",
            "location": "",
            "description": "",
            "recurrence": "",
            "reminder": "",
        })
        if rec.event_json:
            try:
                data = json.loads(rec.event_json)
                start = data.get("start_time", "")
                end = data.get("end_time", "")
                events[-1]["start"] = start[:16].replace("T", " ") if start else ""
                if end and start and start[:10] == end[:10]:
                    events[-1]["end"] = end[11:16]
                else:
                    events[-1]["end"] = end[:16].replace("T", " ") if end else ""
                events[-1]["location"] = data.get("location") or ""
                events[-1]["description"] = data.get("description") or ""
                events[-1]["recurrence"] = str(data.get("recurrence", {}).get("frequency", "")) if data.get("recurrence") else ""
                reminders = data.get("reminders") or []
                if reminders and reminders[0].get("minutes_before"):
                    events[-1]["reminder"] = str(reminders[0]["minutes_before"])
            except Exception:
                pass

    return {
        "ai_ok": ai_ok,
        "ai_name": f"{ai_name} / {ai_model}" if ai_ok else "未配置",
        "vision_label": vision_label,
        "caldav_ok": caldav_ok,
        "caldav_name": caldav_cal if caldav_ok else "",
        "caldav_source": caldav_source,
        "tg_running": (tg_runtime := get_telegram_bot_runtime()) is not None and tg_runtime.running,
        "dc_running": (dc_runtime := get_discord_bot_runtime()) is not None and dc_runtime.running,
        "wechat_running": (wechat_runtime := get_wechat_bot_runtime()) is not None and wechat_runtime.running,
        "recent_events": events,
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
    available_models = settings_service.get("ai_available_models") or (
        ",".join(CLAUDE_MODELS) if provider_type == "anthropic" else ""
    )
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


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, session: Session = Depends(get_db), _: None = Depends(require_admin)) -> HTMLResponse:
    settings_service = SettingsService(session)
    stats = dashboard_stats(session, settings_service)
    ctx = status_context(session)
    ctx["stats"] = stats
    ctx["request"] = request
    ctx["message"] = get_flash(request) or request.query_params.get("message")
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if request.session.get("admin_authenticated"):
        return redirect("/console")
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_db),
):
    settings_service = SettingsService(session)
    saved_username = settings_service.get("admin_username")
    saved_password_hash = settings_service.get("admin_password_hash")
    if saved_username == username and saved_password_hash and verify_password(password, saved_password_hash):
        request.session.clear()
        request.session["admin_authenticated"] = True
        request.session["admin_username"] = username
        if settings_service.get("admin_password_changed") == "false":
            set_flash(request, "首次登录，请修改管理员密码。")
            return redirect("/console/system")
        return redirect("/console")
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "用户名或密码不正确。"},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return redirect("/console/login")


@router.get("/system", response_class=HTMLResponse)
async def system_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    settings_service = SettingsService(session)
    return templates.TemplateResponse(
        request,
        "system.html",
        {
            "username": settings_service.get("admin_username") or "admin",
            "session_days": settings_service.get("session_days") or "7",
            "event_record_limit": settings_service.get("event_record_limit") or "500",
            "week_start_day": settings_service.get("week_start_day") or "1",
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
    stored_models = request.session.get("ai_models", [])
    if stored_models:
        payload["available_models"] = stored_models
    vision_models = request.session.get("ai_vision_models", [])
    if vision_models:
        payload["vision_models"] = vision_models
    payload["vision_use_main"] = settings_service.get("ai_vision_use_main") or "true"
    payload["vision_provider_name"] = settings_service.get("ai_vision_provider_name") or ""
    payload["vision_provider_type"] = settings_service.get("ai_vision_provider_type") or "openai_compatible"
    payload["vision_base_url"] = settings_service.get("ai_vision_base_url") or ""
    payload["vision_api_key_masked"] = settings_service.get_masked("ai_vision_api_key")
    payload["vision_model"] = settings_service.get("ai_vision_model") or ""
    return templates.TemplateResponse(request, "ai.html", payload)


@router.post("/ai")
async def update_ai_settings(
    request: Request,
    provider_name: str = Form(...),
    provider_type: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(""),
    model: str = Form(""),
    available_models_raw: str = Form(""),
    vision_use_main: str = Form("1"),
    vision_provider_name: str = Form(""),
    vision_provider_type: str = Form(""),
    vision_base_url: str = Form(""),
    vision_api_key: str = Form(""),
    vision_model: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    settings_service.set("ai_provider_name", provider_name)
    settings_service.set("ai_provider_type", provider_type)
    settings_service.set("ai_base_url", _normalize_url(base_url))
    if api_key:
        settings_service.set("ai_api_key", api_key, encrypted=True)
    settings_service.set("ai_model", model)
    settings_service.set("ai_available_models", available_models_raw)

    settings_service.set("ai_vision_use_main", vision_use_main)
    if vision_use_main != "1":
        settings_service.set("ai_vision_provider_name", vision_provider_name)
        settings_service.set("ai_vision_provider_type", vision_provider_type)
        settings_service.set("ai_vision_base_url", _normalize_url(vision_base_url))
        if vision_api_key:
            settings_service.set("ai_vision_api_key", vision_api_key, encrypted=True)
        settings_service.set("ai_vision_model", vision_model)
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
    vision_model: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    settings_service.set("ai_vision_use_main", vision_use_main)
    if vision_use_main != "1":
        settings_service.set("ai_vision_provider_name", vision_provider_name)
        settings_service.set("ai_vision_provider_type", vision_provider_type)
        settings_service.set("ai_vision_base_url", _normalize_url(vision_base_url))
        if vision_api_key:
            settings_service.set("ai_vision_api_key", vision_api_key, encrypted=True)
        settings_service.set("ai_vision_model", vision_model)
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
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    if provider_name:
        settings_service.set("ai_provider_name", provider_name)
    if provider_type:
        settings_service.set("ai_provider_type", provider_type)
    if base_url:
        settings_service.set("ai_base_url", _normalize_url(base_url))
    if api_key:
        settings_service.set("ai_api_key", api_key, encrypted=True)
    settings_service.commit()

    provider_type = provider_type or settings_service.get("ai_provider_type") or "openai_compatible"
    base_url = _normalize_url(base_url or settings_service.get("ai_base_url") or "https://api.openai.com/v1")
    api_key = api_key or settings_service.get("ai_api_key") or ""
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key)
    try:
        models = await AIProviderService().list_models(config)
    except AIProviderError as exc:
        set_error_flash(request, str(exc))
        return redirect("/console/ai")

    settings_service.set("ai_available_models", ",".join(models))
    if models and not settings_service.get("ai_model"):
        settings_service.set("ai_model", models[0])
    settings_service.commit()
    request.session["ai_models"] = models
    set_flash(request, f"模型列表已更新，共 {len(models)} 个。")
    return redirect("/console/ai")


@router.post("/ai/vision-models")
async def pull_vision_models(
    request: Request,
    vision_provider_name: str = Form(""),
    vision_provider_type: str = Form(""),
    vision_base_url: str = Form(""),
    vision_api_key: str = Form(""),
    vision_use_main: str = Form("1"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    settings_service.set("ai_vision_use_main", vision_use_main)
    if vision_provider_name:
        settings_service.set("ai_vision_provider_name", vision_provider_name)
    if vision_provider_type:
        settings_service.set("ai_vision_provider_type", vision_provider_type)
    if vision_base_url:
        settings_service.set("ai_vision_base_url", _normalize_url(vision_base_url))
    if vision_api_key:
        settings_service.set("ai_vision_api_key", vision_api_key, encrypted=True)
    settings_service.commit()

    provider_type = vision_provider_type or settings_service.get("ai_vision_provider_type") or "openai_compatible"
    base_url = _normalize_url(vision_base_url or settings_service.get("ai_vision_base_url") or "https://api.openai.com/v1")
    api_key = vision_api_key or settings_service.get("ai_vision_api_key") or ""
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key)
    try:
        models = await AIProviderService().list_models(config)
    except AIProviderError as exc:
        set_error_flash(request, str(exc))
        return redirect("/console/ai")

    settings_service.set("ai_vision_available_models", ",".join(models))
    if models and not settings_service.get("ai_vision_model"):
        settings_service.set("ai_vision_model", models[0])
    settings_service.commit()
    request.session["ai_vision_models"] = models
    set_flash(request, f"识图模型列表已更新，共 {len(models)} 个。")
    return redirect("/console/ai")


@router.post("/ai/test")
async def test_ai_connection(
    request: Request,
    provider_type: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    provider_type = provider_type or settings_service.get("ai_provider_type") or "openai_compatible"
    base_url = _normalize_url(base_url or settings_service.get("ai_base_url") or "https://api.openai.com/v1")
    api_key = api_key or settings_service.get("ai_api_key") or ""
    model = model or settings_service.get("ai_model") or ""
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key, model=model)
    try:
        await AIProviderService().test_connection(config)
    except AIProviderError as exc:
        set_error_flash(request, str(exc))
        return redirect("/console/ai")
    set_flash(request, "AI 连接测试成功。")
    return redirect("/console/ai")


@router.post("/ai/vision-test")
async def test_vision_connection(
    request: Request,
    vision_use_main: str = Form("1"),
    vision_provider_type: str = Form(""),
    vision_base_url: str = Form(""),
    vision_api_key: str = Form(""),
    vision_model: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    settings_service.set("ai_vision_use_main", vision_use_main)
    settings_service.commit()
    provider_type = vision_provider_type or settings_service.get("ai_vision_provider_type") or "openai_compatible"
    base_url = _normalize_url(vision_base_url or settings_service.get("ai_vision_base_url") or "https://api.openai.com/v1")
    api_key = vision_api_key or settings_service.get("ai_vision_api_key") or ""
    model = vision_model or settings_service.get("ai_vision_model") or ""
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key, model=model)
    try:
        await AIProviderService().test_connection(config)
    except AIProviderError as exc:
        set_error_flash(request, str(exc))
        return redirect("/console/ai")
    set_flash(request, "识图模型连接测试成功。")
    return redirect("/console/ai")


def current_ai_provider_config(settings_service: SettingsService) -> AIProviderConfig:
    return AIProviderConfig(
        provider_type=settings_service.get("ai_provider_type") or "openai_compatible",
        base_url=_normalize_url(settings_service.get("ai_base_url") or "https://api.openai.com/v1"),
        api_key=settings_service.get("ai_api_key"),
        model=settings_service.get("ai_model"),
    )


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


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
    cal_url = request.query_params.get("cal_url")
    cal_name = request.query_params.get("cal_name")
    if cal_url and not payload.get("caldav_calendar_url"):
        payload["caldav_calendar_url"] = cal_url
    if cal_name and not payload.get("caldav_calendar_name"):
        payload["caldav_calendar_name"] = cal_name
    cal_urls = request.query_params.get("cal_urls")
    if cal_urls:
        payload["cal_urls"] = cal_urls

    stored_calendars = request.session.get("caldav_calendars", [])
    if stored_calendars:
        payload["calendars"] = stored_calendars
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
) -> RedirectResponse:
    reminder_val, error = _normalize_caldav_int_setting(
        caldav_reminder_minutes, 30, 0, "提醒分钟数必须是整数。", "提醒分钟数不能为负数。"
    )
    if error is not None or reminder_val is None:
        set_error_flash(request, error or "提醒分钟数无效。")
        return redirect("/console/caldav")

    duration_val, error = _normalize_caldav_int_setting(
        caldav_default_duration, 60, 5, "默认持续时间必须是整数。", "默认持续时间不能少于 5 分钟。"
    )
    if error is not None or duration_val is None:
        set_error_flash(request, error or "默认持续时间无效。")
        return redirect("/console/caldav")

    settings_service = SettingsService(session)
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
    set_flash(request, "CalDAV 设置已保存。")
    return redirect("/console/caldav")


@router.post("/caldav/test")
async def test_caldav_connection(
    request: Request,
    caldav_url: str = Form(""),
    caldav_username: str = Form(""),
    caldav_password: str = Form(""),
    caldav_ssl_verify: str = Form("false"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    url = caldav_url.strip()
    username = caldav_username.strip()
    password = caldav_password.strip()
    settings_service = SettingsService(session)

    url = url or settings_service.get("caldav_url") or ""
    username = username or settings_service.get("caldav_username") or ""
    password = password or settings_service.get("caldav_password") or ""
    ssl_verify = _caldav_ssl_from_form(caldav_ssl_verify)
    if not url:
        set_error_flash(request, "请填写 CalDAV Server URL。")
        return redirect("/console/caldav")
    try:
        await CalDAVService().test_connection(url, username, password, ssl_verify=ssl_verify)
    except CalDAVServiceError as exc:
        error_msg = str(exc)
        if "405" in error_msg or "Not Allowed" in error_msg or "nginx" in error_msg:
            error_msg = f"连接失败：URL 可能不是 CalDAV 端点。请确认填的是 CalDAV 地址，不是网站首页。\n常见：iCloud: https://caldav.icloud.com，Nextcloud: https://your.domain/remote.php/dav/"
        set_error_flash(request, error_msg)
        return redirect("/console/caldav")
    set_flash(request, "连接测试成功。")
    return redirect("/console/caldav")


@router.post("/caldav/calendars")
async def list_caldav_calendars(
    request: Request,
    caldav_url: str = Form(""),
    caldav_username: str = Form(""),
    caldav_password: str = Form(""),
    caldav_ssl_verify: str = Form("false"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    url = caldav_url.strip() or settings_service.get("caldav_url") or ""
    username = caldav_username.strip() or settings_service.get("caldav_username") or ""
    password = caldav_password.strip() or settings_service.get("caldav_password") or ""
    ssl_verify = _caldav_ssl_from_form(caldav_ssl_verify)
    if not url:
        set_error_flash(request, "请填写 CalDAV Server URL。")
        return redirect("/console/caldav")
    try:
        calendars = await CalDAVService().list_calendars(url, username, password, ssl_verify=ssl_verify)
    except CalDAVServiceError as exc:
        set_error_flash(request, str(exc))
        return redirect("/console/caldav")

    if url:
        settings_service.set("caldav_url", url)
    if username:
        settings_service.set("caldav_username", username)
    if password:
        settings_service.set("caldav_password", password, encrypted=True)
    settings_service.commit()

    request.session["caldav_calendars"] = calendars
    cal_names = [cal["name"] for cal in calendars]
    msg = f"发现 {len(calendars)} 个日历：{'、'.join(cal_names)}"
    set_flash(request, msg)
    if len(calendars) == 1:
        return redirect_with_query(
            "/console/caldav",
            cal_url=calendars[0]["url"],
            cal_name=calendars[0]["name"],
        )
    return redirect("/console/caldav")


@router.get("/telegram", response_class=HTMLResponse)
async def telegram_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    service = TelegramService()
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
        service = TelegramService()
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
    service = TelegramService()
    link, token = service.generate_bind_link(bot_username)
    set_flash(request, "绑定链接已生成。")
    return redirect_with_query("/console/telegram", bind_link=link, bind_token=token)


@router.get("/telegram/bind/status")
async def check_bind_status(token: str = ""):
    if not token:
        return {"status": "expired"}
    service = TelegramService()
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
    service = TelegramService()
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
    service = TelegramService()
    service.remove_user(session, user_id.strip())
    set_flash(request, f"已删除用户 {user_id}。")
    return redirect("/console/telegram")


@router.get("/discord", response_class=HTMLResponse)
async def discord_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    service = DiscordService()
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
    service = DiscordService()
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
    service = WechatService()
    payload = service.config_summary(session)
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
    service = WechatService()
    summary = service.config_summary(session)
    return {
        "running": summary["wechat_running"],
        "last_error": summary["wechat_error"],
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
    service = WechatService()
    __ = await service.reload_bot(token)
    set_flash(request, "WeChat Bot 已启动。")
    return redirect("/console/wechat")


@router.post("/wechat/stop")
async def stop_wechat_runtime(
    request: Request,
    _: None = Depends(require_admin),
) -> RedirectResponse:
    service = WechatService()
    await service.stop_bot()
    set_flash(request, "WeChat Bot 已停止。")
    return redirect("/console/wechat")


@router.post("/wechat/qr")
async def fetch_wechat_qr(
    _: None = Depends(require_admin),
) -> dict[str, object]:
    try:
        client = ILinkClient()
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


@router.get("/wechat/qr/status")
async def wechat_qr_status(
    qrcode: str = "",
    _: None = Depends(require_admin),
) -> dict[str, object]:
    if not qrcode:
        return {"status": "expired"}
    try:
        client = ILinkClient()
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
    try:
        client = ILinkClient()
        detail = await client.get_qrcode_status_detail(qrcode)
        token = detail.get("bot_token") or detail.get("token") or ""
        if not token:
            return {"error": "未获取到 Bot Token"}
        settings_service = SettingsService(session)
        settings_service.set("wechat_bot_token", token, encrypted=True)
        settings_service.set("wechat_updates_buf", None)
        settings_service.commit()
        __ = await WechatService().reload_bot(token)
        return {"ok": True}
    except ILinkError as exc:
        return {"error": str(exc)}


@router.post("/wechat/clear")
async def clear_wechat_token(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    await WechatService().stop_bot()
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
    settings_service = SettingsService(session)
    token = settings_service.get("wechat_bot_token") or ""
    if not token:
        return {"error": "WeChat Bot Token 未配置，请先扫码登录。"}
    cursor = settings_service.get("wechat_updates_buf") or ""
    try:
        messages, new_cursor = await ILinkClient(token).get_updates(cursor)
    except ILinkAuthError:
        return {"error": "Token 无效或已过期，请重新扫码。"}
    except ILinkError as exc:
        return {"error": str(exc)}
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
    settings_service = SettingsService(session)
    token = settings_service.get("wechat_bot_token") or ""
    if not token:
        return {"error": "WeChat Bot Token 未配置，请先扫码登录。"}
    cursor = settings_service.get("wechat_updates_buf") or ""
    client = ILinkClient(token)
    try:
        messages, new_cursor = await client.get_updates(cursor)
    except ILinkAuthError:
        return {"error": "Token 无效或已过期，请重新扫码。"}
    except ILinkError as exc:
        return {"error": str(exc)}

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
) -> HTMLResponse:
    query = select(EventRecord).order_by(EventRecord.created_at.desc()).limit(100)
    if status_filter and status_filter != "all":
        query = select(EventRecord).where(EventRecord.status == status_filter).order_by(EventRecord.created_at.desc()).limit(100)
    if search:
        query = select(EventRecord).where(
            EventRecord.original_text.contains(search.strip()) | EventRecord.title.contains(search.strip())
        ).order_by(EventRecord.created_at.desc()).limit(100)
        if status_filter and status_filter != "all":
            query = select(EventRecord).where(
                (EventRecord.original_text.contains(search.strip()) | EventRecord.title.contains(search.strip()))
                & (EventRecord.status == status_filter)
            ).order_by(EventRecord.created_at.desc()).limit(100)

    records = session.execute(query).scalars().all()
    events: list[dict[str, object]] = []
    for rec in records:
        events.append({
            "id": rec.id,
            "time": rec.created_at.strftime("%Y-%m-%d %H:%M") if rec.created_at else "",
            "source": rec.source or "",
            "user": rec.source_user_id or rec.telegram_user_id or "",
            "conversation": rec.conversation_id or "",
            "operation": rec.operation or "",
            "title": rec.title or "",
            "start_time": rec.start_time or "",
            "is_recurring": "🔁" if rec.is_recurring else "",
            "status": rec.status or "",
            "error": rec.error_message or "",
            "original_text": rec.original_text or "",
            "event_json": rec.event_json or "",
            "caldav_href": rec.caldav_href or "",
            "caldav_uid": rec.caldav_uid or "",
        })

    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "events": events,
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
    week_start_day: str = Form("1"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    if event_record_limit < 1 or event_record_limit > 100000:
        set_error_flash(request, "记录保留数量必须在 1 到 100000 之间。")
        return redirect("/console/system")

    if week_start_day not in ("0", "1"):
        set_error_flash(request, "每周起始日必须是周日或周一。")
        return redirect("/console/system")

    settings_service = SettingsService(session)
    settings_service.set("week_start_day", week_start_day)
    settings_service.set("event_record_limit", str(event_record_limit))
    settings_service.commit()
    prune_event_records(session, event_record_limit)
    set_flash(request, "系统设置已保存。")
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


@router.get("/system/backup")
async def download_backup(_request: Request, _: None = Depends(require_admin)):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for fn in ["app.db", "secrets.json"]:
            path = Path("data") / fn
            if path.exists():
                zf.write(path, fn)
    if buf.seek(0) != 0:
        raise RuntimeError("failed to rewind backup buffer")
    from datetime import date
    from starlette.responses import StreamingResponse
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=backup-{date.today()}.zip"})


def prune_event_records(session: Session, limit: int) -> None:
    ids_to_keep = select(EventRecord.id).order_by(EventRecord.created_at.desc()).limit(limit).subquery()
    result = session.execute(delete(EventRecord).where(EventRecord.id.not_in(select(ids_to_keep.c.id))))
    result.close()
    session.commit()
