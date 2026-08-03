import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app, favicon, health


def test_health_reports_status_and_version():
    payload = asyncio.run(health())

    assert payload["status"] == "ok"
    assert payload["version"].startswith("v")


def test_favicon_uses_the_existing_icon_file():
    response = asyncio.run(favicon())

    assert response.path == "app/web/static/favicon.ico"
    assert response.media_type == "image/vnd.microsoft.icon"


def test_application_startup_serves_health():
    with patch("app.main.auto_start_bots", new=AsyncMock()) as start_bots:
        with TestClient(app) as client:
            response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    start_bots.assert_awaited_once()
