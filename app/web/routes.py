from collections.abc import Generator

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.db.models import EventRecord
from app.db.session import SessionLocal
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


def dashboard_stats(session: Session) -> dict[str, int]:
    total = session.scalar(select(func.count()).select_from(EventRecord)) or 0
    failed = session.scalar(select(func.count()).select_from(EventRecord).where(EventRecord.status == "failed")) or 0
    pending = session.scalar(select(func.count()).select_from(EventRecord).where(EventRecord.status == "pending")) or 0
    return {"total_events": total, "failed_events": failed, "pending_events": pending}


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
    ids_to_keep = select(EventRecord.id).order_by(EventRecord.created_at.desc()).limit(limit)
    session.execute(delete(EventRecord).where(EventRecord.id.not_in(ids_to_keep)))
    session.commit()
