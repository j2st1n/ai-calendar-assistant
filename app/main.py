from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.bootstrap import bootstrap_application
from app.core.bootstrap import read_version
from app.core.config import settings
from app.db.session import SessionLocal
from app.services.settings_service import SettingsService
from app.web.routes import router as web_router
from app.web.security import SameOriginMiddleware, SecurityHeadersMiddleware


async def auto_start_bots() -> None:
    with SessionLocal() as session:
        service = SettingsService(session)
        tg_token = service.get("telegram_bot_token")
        dc_token = service.get("discord_bot_token")
        wx_token = service.get("wechat_bot_token")
    if tg_token:
        from app.services.telegram_service import TelegramService

        _ = await TelegramService().reload_bot(tg_token)
    if dc_token:
        from app.services.discord_service import DiscordService

        _ = await DiscordService().reload_bot(dc_token)
    if wx_token:
        from app.services.wechat_service import WechatService

        _ = await WechatService().reload_bot(wx_token)


def create_app() -> FastAPI:
    bootstrap_application()
    app = FastAPI(title="AI Calendar Assistant")
    app.add_middleware(SameOriginMiddleware)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[host.strip() for host in settings.trusted_hosts.split(",") if host.strip()],
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key or "development-only-secret",
        max_age=settings.session_days * 24 * 60 * 60,
        same_site="strict",
        https_only=settings.secure_cookies,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
    app.include_router(web_router)

    @app.on_event("startup")
    async def start_configured_bots() -> None:
        await auto_start_bots()

    return app


app = create_app()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": read_version()}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse("app/web/static/favicon.ico", media_type="image/vnd.microsoft.icon")


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/console")
