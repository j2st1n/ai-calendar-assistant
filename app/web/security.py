from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
from time import monotonic
from urllib.parse import urlsplit

import httpx
from fastapi import Request
from starlette.responses import JSONResponse

from app.core.config import settings


TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class SecurityHeadersMiddleware:
    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"same-origin"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (
                            b"content-security-policy",
                            (
                                b"default-src 'self'; script-src 'self' 'unsafe-inline' "
                                b"https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; "
                                b"connect-src 'self' https://challenges.cloudflare.com; "
                                b"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                b"font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
                                b"object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
                            ),
                        ),
                    ]
                )
                if settings.secure_cookies:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                if str(scope.get("path", "")).startswith("/console"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class SameOriginMiddleware:
    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        expected_origin = settings.public_origin.rstrip("/")
        if not expected_origin:
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            expected_origin = f"{scheme}://{request.headers.get('host', '')}"
        supplied_origin = request.headers.get("origin")
        if not supplied_origin:
            referer = request.headers.get("referer")
            if referer:
                parsed = urlsplit(referer)
                supplied_origin = f"{parsed.scheme}://{parsed.netloc}"
        if supplied_origin != expected_origin:
            response = JSONResponse({"detail": "Invalid request origin"}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


@dataclass
class LoginRateLimiter:
    limit: int = 5
    window_seconds: int = 300

    def __post_init__(self) -> None:
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._attempts[key]
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            _ = attempts.popleft()
        return attempts

    def blocked(self, key: str) -> bool:
        return len(self._prune(key, monotonic())) >= self.limit

    def failure(self, key: str) -> None:
        self._prune(key, monotonic()).append(monotonic())

    def record(self, key: str) -> None:
        self.failure(key)

    def success(self, key: str) -> None:
        _ = self._attempts.pop(key, None)


login_rate_limiter = LoginRateLimiter()
passkey_request_rate_limiter = LoginRateLimiter(limit=20)
passkey_failure_rate_limiter = LoginRateLimiter()


def client_ip(request: Request) -> str:
    if settings.trust_proxy_headers:
        for header in ("cf-connecting-ip", "x-forwarded-for"):
            value = request.headers.get(header, "").split(",", 1)[0].strip()
            if value:
                try:
                    return str(ip_address(value))
                except ValueError:
                    pass
    return request.client.host if request.client else "unknown"


async def verify_turnstile(
    token: str,
    secret_key: str,
    *,
    remote_ip: str | None,
    expected_hostname: str | None,
) -> bool:
    if not token:
        return False
    payload = {"secret": secret_key, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(TURNSTILE_VERIFY_URL, data=payload)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    if result.get("success") is not True or result.get("action") != "login":
        return False
    return not expected_hostname or result.get("hostname") == expected_hostname
