import asyncio

from starlette.responses import JSONResponse

from app.core.config import settings
from fastapi import Request

from app.web.security import LoginRateLimiter, SameOriginMiddleware, SecurityHeadersMiddleware, client_ip, verify_turnstile


def _request(method: str, origin: str | None = None) -> tuple[int, dict[str, str]]:
    messages: list[dict] = []

    async def endpoint(scope, receive, send) -> None:
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    headers = [(b"host", b"cal.example")]
    if origin:
        headers.append((b"origin", origin.encode()))
    scope = {
        "type": "http",
        "method": method,
        "scheme": "https",
        "path": "/console/submit",
        "query_string": b"",
        "headers": headers,
        "server": ("cal.example", 443),
        "client": ("127.0.0.1", 12345),
    }
    app = SecurityHeadersMiddleware(SameOriginMiddleware(endpoint))
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_headers = {key.decode(): value.decode() for key, value in start["headers"]}
    return start["status"], response_headers


def test_same_origin_rejects_cross_site_post() -> None:
    previous = settings.public_origin
    settings.public_origin = "https://cal.example"
    try:
        status_code, _headers = _request("POST", "https://evil.example")
        assert status_code == 403
    finally:
        settings.public_origin = previous


def test_same_origin_accepts_configured_origin_and_safe_requests() -> None:
    previous = settings.public_origin
    settings.public_origin = "https://cal.example"
    try:
        post_status, _ = _request("POST", "https://cal.example")
        get_status, _ = _request("GET")
        assert post_status == 200
        assert get_status == 200
    finally:
        settings.public_origin = previous


def test_security_headers_are_present() -> None:
    _status, headers = _request("GET")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"


def test_login_rate_limiter_blocks_and_resets() -> None:
    limiter = LoginRateLimiter(limit=2, window_seconds=300)
    assert limiter.blocked("admin") is False
    limiter.failure("admin")
    limiter.failure("admin")
    assert limiter.blocked("admin") is True
    limiter.success("admin")
    assert limiter.blocked("admin") is False


def _resolve_client_ip(headers: list[tuple[bytes, bytes]], *, trust_proxy: bool) -> str:
    previous = settings.trust_proxy_headers
    settings.trust_proxy_headers = trust_proxy
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/console/login/passkey/options",
            "headers": headers,
            "client": ("192.0.2.10", 12345),
        }
    )
    try:
        return client_ip(request)
    finally:
        settings.trust_proxy_headers = previous


def test_client_ip_prefers_valid_cloudflare_header() -> None:
    assert _resolve_client_ip([(b"cf-connecting-ip", b"203.0.113.8")], trust_proxy=True) == "203.0.113.8"


def test_client_ip_ignores_invalid_forwarded_headers() -> None:
    assert (
        _resolve_client_ip(
            [(b"cf-connecting-ip", b"invalid"), (b"x-forwarded-for", b"also-invalid")],
            trust_proxy=True,
        )
        == "192.0.2.10"
    )


def test_client_ip_ignores_forwarded_headers_when_proxy_trust_is_disabled() -> None:
    assert _resolve_client_ip([(b"cf-connecting-ip", b"203.0.113.8")], trust_proxy=False) == "192.0.2.10"


def test_turnstile_requires_matching_action_and_hostname(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"success": True, "action": "login", "hostname": "cal.example"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("app.web.security.httpx.AsyncClient", lambda **_kwargs: FakeClient())
    assert asyncio.run(
        verify_turnstile("token", "secret", remote_ip=None, expected_hostname="cal.example")
    ) is True
    assert asyncio.run(
        verify_turnstile("token", "secret", remote_ip=None, expected_hostname="other.example")
    ) is False
