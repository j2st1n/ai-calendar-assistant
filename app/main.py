from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.bootstrap import bootstrap_application
from app.core.config import settings
from app.db.session import SessionLocal
from app.services.settings_service import SettingsService
from app.services.telegram_service import TelegramService
from app.services.discord_service import DiscordService
from app.web.routes import router as web_router


def create_app() -> FastAPI:
    bootstrap_application()
    app = FastAPI(title="AI Calendar Assistant")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key or "development-only-secret",
        max_age=settings.session_days * 24 * 60 * 60,
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
    app.include_router(web_router)

    @app.on_event("startup")
    async def auto_start_bots():
        with SessionLocal() as session:
            s = SettingsService(session)
            tg_token = s.get("telegram_bot_token")
            dc_token = s.get("discord_bot_token")
        if tg_token:
            await TelegramService().reload_bot(tg_token)
        if dc_token:
            await DiscordService().reload_bot(dc_token)

    return app


app = create_app()


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/console")
