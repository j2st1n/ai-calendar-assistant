from collections.abc import Generator
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.ai.providers import CLAUDE_MODELS, PROVIDER_PRESETS
from app.db.models import EventRecord
from app.db.session import SessionLocal
from app.services.ai_provider_service import AIProviderConfig, AIProviderError, AIProviderService
from app.services.settings_service import SettingsService


router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/web/templates")


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def require_admin(request: Request) -> None:
    if not request.session.get("admin_authenticated"):
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/admin/login"})


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=status.HTTP_303_SEE_OTHER)


def redirect_with_query(path: str, **params: str) -> RedirectResponse:
    return redirect(f"{path}?{urlencode(params)}")


def dashboard_stats(session: Session) -> dict[str, int]:
    total = session.scalar(select(func.count()).select_from(EventRecord)) or 0
    failed = session.scalar(select(func.count()).select_from(EventRecord).where(EventRecord.status == "failed")) or 0
    pending = session.scalar(select(func.count()).select_from(EventRecord).where(EventRecord.status == "pending")) or 0
    return {"total_events": total, "failed_events": failed, "pending_events": pending}


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
    return templates.TemplateResponse(request, "dashboard.html", {"stats": dashboard_stats(session)})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if request.session.get("admin_authenticated"):
        return redirect("/admin")
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
        return redirect("/admin")
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "用户名或密码不正确。"},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return redirect("/admin/login")


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
    return redirect("/admin/ai?message=AI 设置已保存。")


@router.post("/ai/models")
async def pull_ai_models(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    config = current_ai_provider_config(settings_service)
    try:
        models = await AIProviderService().list_models(config)
    except AIProviderError as exc:
        return redirect_with_query("/admin/ai", error=str(exc))

    settings_service.set("ai_available_models", ",".join(models))
    if models and not settings_service.get("ai_model"):
        settings_service.set("ai_model", models[0])
    settings_service.commit()
    return redirect_with_query("/admin/ai", message=f"模型列表已更新，共 {len(models)} 个。")


@router.post("/ai/test")
async def test_ai_connection(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    settings_service = SettingsService(session)
    config = current_ai_provider_config(settings_service)
    try:
        await AIProviderService().test_connection(config)
    except AIProviderError as exc:
        return redirect_with_query("/admin/ai", error=str(exc))
    return redirect_with_query("/admin/ai", message="AI 连接测试成功。")


def current_ai_provider_config(settings_service: SettingsService) -> AIProviderConfig:
    return AIProviderConfig(
        provider_type=settings_service.get("ai_provider_type") or "openai_compatible",
        base_url=settings_service.get("ai_base_url") or "https://api.openai.com/v1",
        api_key=settings_service.get("ai_api_key"),
        model=settings_service.get("ai_model"),
    )


@router.post("/system")
async def update_system_settings(
    username: str = Form(...),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    session_days: int = Form(...),
    event_record_limit: int = Form(...),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    if session_days < 1 or session_days > 365:
        return redirect("/admin/system?error=Session 有效期必须在 1 到 365 天之间。")
    if event_record_limit < 1 or event_record_limit > 100000:
        return redirect("/admin/system?error=记录保留数量必须在 1 到 100000 之间。")

    settings_service = SettingsService(session)
    saved_password_hash = settings_service.get("admin_password_hash")
    if new_password or confirm_password:
        if new_password != confirm_password:
            return redirect("/admin/system?error=两次输入的新密码不一致。")
        if not saved_password_hash or not verify_password(current_password, saved_password_hash):
            return redirect("/admin/system?error=当前密码不正确。")
        settings_service.set("admin_password_hash", hash_password(new_password))

    settings_service.set("admin_username", username.strip() or "admin")
    settings_service.set("session_days", str(session_days))
    settings_service.set("event_record_limit", str(event_record_limit))
    settings_service.commit()
    prune_event_records(session, event_record_limit)
    return redirect("/admin/system?message=系统设置已保存。")


@router.post("/system/clear-events")
async def clear_event_records(
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> RedirectResponse:
    session.execute(delete(EventRecord))
    session.commit()
    return redirect("/admin/system?message=事件记录已清空。")


def prune_event_records(session: Session, limit: int) -> None:
    ids_to_keep = select(EventRecord.id).order_by(EventRecord.created_at.desc()).limit(limit).subquery()
    session.execute(delete(EventRecord).where(EventRecord.id.not_in(select(ids_to_keep.c.id))))
    session.commit()
