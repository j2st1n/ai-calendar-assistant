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
