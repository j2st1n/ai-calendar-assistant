from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.core.bootstrap import bootstrap_application
from app.web.routes import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_application()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="AI Calendar Assistant", lifespan=lifespan)
    app.include_router(web_router)
    return app


app = create_app()


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/admin")
