import asyncio

from app.main import health


def test_health_reports_status_and_version():
    payload = asyncio.run(health())

    assert payload["status"] == "ok"
    assert payload["version"].startswith("v")
