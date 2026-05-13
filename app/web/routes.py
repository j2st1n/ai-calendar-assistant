from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse)
async def dashboard() -> str:
    return "<h1>AI Calendar Assistant</h1><p>Configuration UI scaffold is ready.</p>"
