from collections.abc import Generator
from datetime import date, datetime as dt, timedelta
import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.bootstrap import read_changes, read_version
from app.core.security import hash_password, verify_password
from app.services.telegram_service import get_telegram_bot_runtime
from app.ai.providers import CLAUDE_MODELS, PROVIDER_PRESETS
from app.db.models import EventRecord
from app.db.session import SessionLocal
from app.services.ai_provider_service import AIProviderConfig, AIProviderError, AIProviderService
from app.services.caldav_service import CalDAVService, CalDAVServiceError
from app.services.settings_service import SettingsService
from app.services.telegram_service import TelegramService


router = APIRouter(prefix="/console")
templates = Jinja2Templates(directory="app/web/templates")


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


def dashboard_stats(session: Session) -> dict[str, int]:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def count_since(since_date):
        return session.scalar(
            select(func.count()).select_from(EventRecord).where(
                EventRecord.operation == "create",
                EventRecord.status == "success",
                EventRecord.created_at >= since_date,
            )
        ) or 0

    return {
        "today_created": count_since(today),
        "week_created": count_since(week_start),
        "month_created": count_since(month_start),
    }


def status_context(session: Session, request) -> dict:
    settings_service = SettingsService(session)
    ai_name = settings_service.get("ai_provider_name") or ""
    ai_model = settings_service.get("ai_model") or ""
    ai_ok = bool(ai_name and ai_model)
    caldav_url = settings_service.get("caldav_url") or ""
    caldav_cal = settings_service.get("caldav_calendar_name") or ""
    caldav_ok = bool(caldav_url and caldav_cal)

    recent = session.execute(
        select(EventRecord).where(
            EventRecord.operation == "create",
            EventRecord.status == "success",
        ).order_by(EventRecord.updated_at.desc()).limit(5)
    ).scalars().all()

    events = []
    for rec in recent:
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
        })
        if rec.event_json:
            try:
                data = json.loads(rec.event_json)
                events[-1]["start"] = data.get("start_time", "")[:16].replace("T", " ") if data.get("start_time") else ""
                events[-1]["end"] = data.get("end_time", "")[:16].replace("T", " ") if data.get("end_time") else ""
                events[-1]["location"] = data.get("location") or ""
                events[-1]["description"] = data.get("description") or ""
                events[-1]["recurrence"] = str(data.get("recurrence", {}).get("frequency", "")) if data.get("recurrence") else ""
            except Exception:
                pass

    return {
        "ai_ok": ai_ok,
        "ai_name": f"{ai_name} / {ai_model}" if ai_ok else "",
        "caldav_ok": caldav_ok,
        "caldav_name": caldav_cal if caldav_ok else "",
        "tg_running": get_telegram_bot_runtime() is not None and get_telegram_bot_runtime().running,
        "recent_events": events,
        "version": read_version(),
        "changes": read_changes(),
    }


def ai_settings_payload(settings_service: SettingsService) -> dict[str, str | list[dict[str, str]]]:
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
    stats = dashboard_stats(session)
    ctx = status_context(session, request)
    ctx["stats"] = stats
    ctx["request"] = request
    ctx["message"] = get_flash(request) or request.query_params.get("message")
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
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
            return redirect_with_query("/console/system", message="首次登录，请修改管理员密码。")
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
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
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
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    })
    stored_models = request.session.get("ai_models", [])
    if stored_models:
        payload["available_models"] = stored_models
    return templates.TemplateResponse(request, "ai.html", payload)


@router.post("/ai")
async def update_ai_settings(
    provider_name: str = Form(...),
    provider_type: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(""),
    model: str = Form(""),
    available_models_raw: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    settings_service.set("ai_provider_name", provider_name)
    settings_service.set("ai_provider_type", provider_type)
    settings_service.set("ai_base_url", base_url)
    if api_key:
        settings_service.set("ai_api_key", api_key, encrypted=True)
    settings_service.set("ai_model", model)
    settings_service.set("ai_available_models", available_models_raw)
    settings_service.commit()
    return redirect("/console/ai?message=AI 设置已保存。")


@router.post("/ai/models")
async def pull_ai_models(
    request: Request,
    provider_type: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    provider_type = provider_type or settings_service.get("ai_provider_type") or "openai_compatible"
    base_url = base_url or settings_service.get("ai_base_url") or "https://api.openai.com/v1"
    api_key = api_key or settings_service.get("ai_api_key") or ""
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key)
    try:
        models = await AIProviderService().list_models(config)
    except AIProviderError as exc:
        return redirect_with_query("/console/ai", error=str(exc))

    settings_service.set("ai_available_models", ",".join(models))
    if models and not settings_service.get("ai_model"):
        settings_service.set("ai_model", models[0])
    settings_service.commit()
    request.session["ai_models"] = models
    return redirect_with_query("/console/ai", message=f"模型列表已更新，共 {len(models)} 个。")


@router.post("/ai/test")
async def test_ai_connection(
    provider_type: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    provider_type = provider_type or settings_service.get("ai_provider_type") or "openai_compatible"
    base_url = base_url or settings_service.get("ai_base_url") or "https://api.openai.com/v1"
    api_key = api_key or settings_service.get("ai_api_key") or ""
    model = model or settings_service.get("ai_model") or ""
    config = AIProviderConfig(provider_type=provider_type, base_url=base_url, api_key=api_key, model=model)
    try:
        await AIProviderService().test_connection(config)
    except AIProviderError as exc:
        return redirect_with_query("/console/ai", error=str(exc))
    return redirect_with_query("/console/ai", message="AI 连接测试成功。")


def current_ai_provider_config(settings_service: SettingsService) -> AIProviderConfig:
    return AIProviderConfig(
        provider_type=settings_service.get("ai_provider_type") or "openai_compatible",
        base_url=settings_service.get("ai_base_url") or "https://api.openai.com/v1",
        api_key=settings_service.get("ai_api_key"),
        model=settings_service.get("ai_model"),
    )


def caldav_payload(settings_service: SettingsService) -> dict:
    return {
        "caldav_url": settings_service.get("caldav_url") or "",
        "caldav_username": settings_service.get("caldav_username") or "",
        "caldav_password_masked": settings_service.get_masked("caldav_password"),
        "caldav_calendar_url": settings_service.get("caldav_calendar_url") or "",
        "caldav_calendar_name": settings_service.get("caldav_calendar_name") or "",
        "caldav_timezone": settings_service.get("caldav_timezone") or "Asia/Shanghai",
        "caldav_reminder_minutes": settings_service.get("caldav_reminder_minutes") or "30",
        "caldav_default_duration": settings_service.get("caldav_default_duration") or "60",
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
    payload["message"] = request.query_params.get("message")
    payload["error"] = request.query_params.get("error")
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
    caldav_url: str = Form(""),
    caldav_username: str = Form(""),
    caldav_password: str = Form(""),
    caldav_calendar_url: str = Form(""),
    caldav_calendar_name: str = Form(""),
    caldav_timezone: str = Form("Asia/Shanghai"),
    caldav_reminder_minutes: str = Form("30"),
    caldav_default_duration: str = Form("60"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    settings_service.set("caldav_url", caldav_url.strip())
    settings_service.set("caldav_username", caldav_username.strip())
    if caldav_password:
        settings_service.set("caldav_password", caldav_password, encrypted=True)
    settings_service.set("caldav_calendar_url", caldav_calendar_url.strip())
    settings_service.set("caldav_calendar_name", caldav_calendar_name.strip())
    settings_service.set("caldav_timezone", caldav_timezone.strip())
    settings_service.set("caldav_reminder_minutes", caldav_reminder_minutes.strip())
    settings_service.set("caldav_default_duration", caldav_default_duration.strip())
    settings_service.commit()
    return redirect_with_query("/console/caldav", message="CalDAV 设置已保存。")


@router.post("/caldav/test")
async def test_caldav_connection(
    caldav_url: str = Form(""),
    caldav_username: str = Form(""),
    caldav_password: str = Form(""),
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
    if not url:
        return redirect_with_query("/console/caldav", error="请填写 CalDAV Server URL。")
    try:
        await CalDAVService().test_connection(url, username, password)
    except CalDAVServiceError as exc:
        error_msg = str(exc)
        if "405" in error_msg or "Not Allowed" in error_msg or "nginx" in error_msg:
            error_msg = f"连接失败：URL 可能不是 CalDAV 端点。请确认填的是 CalDAV 地址，不是网站首页。\n常见：iCloud: https://caldav.icloud.com，Nextcloud: https://your.domain/remote.php/dav/"
        return redirect_with_query("/console/caldav", error=error_msg)
    except CalDAVServiceError as exc:
        return redirect_with_query("/console/caldav", error=str(exc))
    return redirect_with_query("/console/caldav", message="连接测试成功。")


@router.post("/caldav/calendars")
async def list_caldav_calendars(
    request: Request,
    caldav_url: str = Form(""),
    caldav_username: str = Form(""),
    caldav_password: str = Form(""),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    url = caldav_url.strip() or settings_service.get("caldav_url") or ""
    username = caldav_username.strip() or settings_service.get("caldav_username") or ""
    password = caldav_password.strip() or settings_service.get("caldav_password") or ""
    if not url:
        return redirect_with_query("/console/caldav", error="请填写 CalDAV Server URL。")
    try:
        calendars = await CalDAVService().list_calendars(url, username, password)
    except CalDAVServiceError as exc:
        return redirect_with_query("/console/caldav", error=str(exc))

    request.session["caldav_calendars"] = calendars
    cal_names = [cal["name"] for cal in calendars]
    msg = f"发现 {len(calendars)} 个日历：{'、'.join(cal_names)}"
    if len(calendars) == 1:
        return redirect_with_query(
            "/console/caldav",
            message=msg,
            cal_url=calendars[0]["url"],
            cal_name=calendars[0]["name"],
        )
    return redirect_with_query("/console/caldav", message=msg)


@router.get("/telegram", response_class=HTMLResponse)
async def telegram_settings(
    request: Request,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    service = TelegramService()
    payload = service.config_summary(session)
    payload["request"] = request
    payload["message"] = request.query_params.get("message")
    payload["error"] = request.query_params.get("error")
    payload["bind_link"] = request.query_params.get("bind_link")
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
        service.reload_bot(token)
        target = redirect_path or "/console/telegram"
        set_flash(request, "Telegram Bot 已保存并重载。")
        return redirect(target)
    return redirect_with_query("/console/telegram", message="请填写 Bot Token。")


@router.post("/telegram/bind")
async def generate_bind_link(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    bot_username = settings_service.get("telegram_bot_username") or ""
    if not bot_username:
        return redirect_with_query("/console/telegram", error="请先配置 Bot Username。")
    service = TelegramService()
    link = service.generate_bind_link(bot_username)
    return redirect_with_query("/console/telegram", bind_link=link, message="绑定链接已生成。")


@router.post("/telegram/users/add")
async def add_telegram_user(
    user_id: str = Form(...),
    username: str = Form(""),
    display_name: str = Form(""),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    service = TelegramService()
    service.add_user(session, user_id.strip(), username.strip(), display_name.strip())
    return redirect_with_query("/console/telegram", message=f"已添加用户 {user_id}。")


@router.post("/telegram/users/disable")
async def disable_telegram_user(
    user_id: str = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    service = TelegramService()
    service.disable_user(session, user_id.strip())
    return redirect_with_query("/console/telegram", message=f"已禁用用户 {user_id}。")


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
        pattern = f"%{search.strip()}%"
        query = select(EventRecord).where(
            EventRecord.original_text.contains(search.strip()) | EventRecord.title.contains(search.strip())
        ).order_by(EventRecord.created_at.desc()).limit(100)
        if status_filter and status_filter != "all":
            query = select(EventRecord).where(
                (EventRecord.original_text.contains(search.strip()) | EventRecord.title.contains(search.strip()))
                & (EventRecord.status == status_filter)
            ).order_by(EventRecord.created_at.desc()).limit(100)

    records = session.execute(query).scalars().all()
    events = []
    for rec in records:
        events.append({
            "id": rec.id,
            "time": rec.created_at.strftime("%Y-%m-%d %H:%M") if rec.created_at else "",
            "source": rec.source or "",
            "user": rec.telegram_user_id or "",
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
            "statuses": ["all", "success", "failed", "pending"],
        },
    )


@router.post("/system")
async def update_system_settings(
    username: str = Form(...),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    session_days: int = Form(7),
    event_record_limit: int = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    if session_days < 1 or session_days > 365:
        return redirect("/console/system?error=Session 有效期必须在 1 到 365 天之间。")
    if event_record_limit < 1 or event_record_limit > 100000:
        return redirect("/console/system?error=记录保留数量必须在 1 到 100000 之间。")

    settings_service = SettingsService(session)
    saved_password_hash = settings_service.get("admin_password_hash")
    if new_password or confirm_password:
        if new_password != confirm_password:
            return redirect("/console/system?error=两次输入的新密码不一致。")
        if not saved_password_hash or not verify_password(current_password, saved_password_hash):
            return redirect("/console/system?error=当前密码不正确。")
        settings_service.set("admin_password_hash", hash_password(new_password))
        settings_service.set("admin_password_changed", "true")

    settings_service.set("admin_username", username.strip() or "admin")
    settings_service.set("session_days", str(session_days))
    settings_service.set("event_record_limit", str(event_record_limit))
    settings_service.commit()
    prune_event_records(session, event_record_limit)
    return redirect("/console/system?message=系统设置已保存。")


@router.post("/events/clear")
async def clear_event_records(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    session.execute(delete(EventRecord))
    session.commit()
    return redirect_with_query("/console/events", message="事件记录已清空。")


@router.get("/system/backup")
async def download_backup(request: Request, _: None = Depends(require_admin)):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for fn in ["app.db", "secrets.json"]:
            path = Path("data") / fn
            if path.exists():
                zf.write(path, fn)
    buf.seek(0)
    from datetime import date
    from starlette.responses import StreamingResponse
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=backup-{date.today()}.zip"})


def prune_event_records(session: Session, limit: int) -> None:
    ids_to_keep = select(EventRecord.id).order_by(EventRecord.created_at.desc()).limit(limit).subquery()
    session.execute(delete(EventRecord).where(EventRecord.id.not_in(select(ids_to_keep.c.id))))
    session.commit()
