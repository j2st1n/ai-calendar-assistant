from fastapi.routing import APIRoute

from app.web.routes import require_admin, router


def _route(path: str, method: str) -> APIRoute:
    return next(
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and route.methods
        and method in route.methods
    )


def test_telegram_bind_status_requires_admin():
    route = _route("/console/telegram/bind/status", "GET")

    dependency_calls = [dep.call for dep in route.dependant.dependencies]

    assert require_admin in dependency_calls


def test_ai_and_caldav_probes_require_admin():
    for path, method in [
        ("/console/ai/test", "POST"),
        ("/console/ai/schema-test", "POST"),
        ("/console/caldav/test", "POST"),
        ("/console/caldav/write-test", "POST"),
        ("/console/caldav/calendars", "POST"),
    ]:
        route = _route(path, method)
        dependency_calls = [dep.call for dep in route.dependant.dependencies]
        assert require_admin in dependency_calls, f"{method} {path} must require admin"
