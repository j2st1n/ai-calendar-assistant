import asyncio

from app.main import favicon, health


def test_health_reports_status_and_version():
    payload = asyncio.run(health())

    assert payload["status"] == "ok"
    assert payload["version"].startswith("v")


def test_favicon_uses_the_existing_icon_file():
    response = asyncio.run(favicon())

    assert response.path == "app/web/static/favicon.ico"
    assert response.media_type == "image/vnd.microsoft.icon"
