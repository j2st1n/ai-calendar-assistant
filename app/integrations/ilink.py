from __future__ import annotations

import base64
import hashlib
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx

ILINK_BASE = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.17.0"
BOT_AGENT = "ai-calendar-assistant/1.17.0"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_API_TIMEOUT_SECONDS = 15.0
STALE_TOKEN_ERRCODE = -14
SESSION_PAUSE_SECONDS = 60 * 60

logger = logging.getLogger(__name__)

_pause_until_by_session: dict[str, float] = {}


class ILinkError(Exception):
    """Base error for the documented iLink HTTP/JSON protocol."""


class ILinkAuthError(ILinkError):
    """Local configuration error: an operation was attempted without a token."""


class ILinkHttpError(ILinkError):
    def __init__(
        self,
        status_code: int,
        *,
        ret: int | None = None,
        errcode: int | None = None,
        errmsg: str = "",
    ) -> None:
        self.status_code = status_code
        self.ret = ret
        self.errcode = errcode
        self.errmsg = errmsg
        detail = (
            f" ret={ret} errcode={errcode} errmsg={errmsg}"
            if errmsg or ret is not None or errcode is not None
            else ""
        )
        super().__init__(f"iLink request failed: HTTP {status_code}{detail}")


class ILinkProtocolError(ILinkError):
    def __init__(self, message: str, *, ret: int | None = None, errcode: int | None = None) -> None:
        self.ret = ret
        self.errcode = errcode
        super().__init__(message)


class ILinkStaleTokenError(ILinkProtocolError):
    """Official -14 signal; callers must cool down, not force a QR login."""


TransportCategory = Literal["dns", "tcp", "tls", "timeout", "unknown"]


class ILinkTransportError(ILinkError):
    def __init__(self, category: TransportCategory, message: str, *, code: str = "") -> None:
        self.category = category
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class GetUpdatesResult:
    messages: list[dict[str, Any]]
    cursor: str
    next_timeout_ms: int | None
    ret: int = 0
    errcode: int | None = None
    errmsg: str = ""


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _classify_transport_error(exc: httpx.HTTPError) -> tuple[TransportCategory, str]:
    text = f"{type(exc).__name__} {exc}".upper()
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", "request timeout"
    if "DNS" in text or "NAME OR SERVICE" in text or "GETADDRINFO" in text:
        return "dns", "DNS resolution failed"
    if "SSL" in text or "TLS" in text or "CERTIFICATE" in text:
        return "tls", "TLS handshake failed"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "tcp", "network connection failed"
    return "unknown", "network request failed"


class ILinkClient:
    def __init__(self, bot_token: str = "", base_url: str = ILINK_BASE) -> None:
        self.bot_token = bot_token
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._session_key = hashlib.sha256(bot_token.encode()).hexdigest() if bot_token else ""

    async def __aenter__(self) -> ILinkClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def pause_until(self) -> float | None:
        if not self._session_key:
            return None
        pause_until = _pause_until_by_session.get(self._session_key)
        if pause_until is not None and pause_until <= time.time():
            _pause_until_by_session.pop(self._session_key, None)
            return None
        return pause_until

    def pause_session(self, seconds: float = SESSION_PAUSE_SECONDS) -> float:
        pause_until = time.time() + seconds
        if self._session_key:
            _pause_until_by_session[self._session_key] = pause_until
        return pause_until

    def _assert_session_active(self) -> None:
        pause_until = self.pause_until
        if pause_until is not None:
            remaining = max(0, int(pause_until - time.time()))
            raise ILinkStaleTokenError(
                f"iLink session cooling down ({remaining}s remaining)",
                errcode=STALE_TOKEN_ERRCODE,
            )

    def _base_info(self) -> dict[str, str]:
        return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}

    async def get_qrcode_detail(self) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            json={"local_token_list": []},
            requires_token=False,
        )

    async def get_qrcode(self) -> str:
        data = await self.get_qrcode_detail()
        for key in ("qrcode_img_content", "qrcode_url", "url", "qr_code_url"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        value = data.get("qrcode")
        if isinstance(value, str) and value:
            return value
        raise ILinkProtocolError("iLink response did not include a qrcode value")

    async def get_qrcode_status(self, qrcode: str) -> str:
        if not qrcode:
            raise ILinkProtocolError("qrcode is required")
        data = await self._request(
            "GET", "/ilink/bot/get_qrcode_status", params={"qrcode": qrcode}, requires_token=False
        )
        status = data.get("status") or data.get("qrcode_status") or data.get("state")
        if isinstance(status, str):
            return status
        if status is not None:
            return str(status)
        return "unknown"

    async def get_qrcode_status_detail(self, qrcode: str) -> dict[str, Any]:
        if not qrcode:
            raise ILinkProtocolError("qrcode is required")
        return await self._request(
            "GET", "/ilink/bot/get_qrcode_status", params={"qrcode": qrcode}, requires_token=False
        )

    async def get_updates(
        self,
        buf: str = "",
        *,
        timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS,
    ) -> GetUpdatesResult:
        self._require_token("get_updates")
        data = await self._request(
            "POST",
            "/ilink/bot/getupdates",
            json={"get_updates_buf": buf, "base_info": self._base_info()},
            timeout=max(timeout_ms, 1) / 1000,
            normal_read_timeout=True,
        )
        msgs = data.get("msgs") or []
        if not isinstance(msgs, list):
            raise ILinkProtocolError("iLink getupdates returned a non-list msgs field")
        typed_msgs = [msg for msg in msgs if isinstance(msg, dict)]
        new_buf = data.get("get_updates_buf")
        suggested_timeout = _as_int(data.get("longpolling_timeout_ms"))
        return GetUpdatesResult(
            messages=typed_msgs,
            cursor=new_buf if isinstance(new_buf, str) and new_buf else buf,
            next_timeout_ms=suggested_timeout if suggested_timeout and suggested_timeout > 0 else None,
            ret=_as_int(data.get("ret")) or 0,
            errcode=_as_int(data.get("errcode")),
            errmsg=str(data.get("errmsg") or ""),
        )

    async def send_message(self, to_user_id: str, text: str, context_token: str) -> dict[str, Any]:
        self._require_token("send_message")
        if not to_user_id:
            raise ILinkProtocolError("to_user_id is required")
        if not context_token:
            raise ILinkProtocolError("context_token is required")
        if not text:
            raise ILinkProtocolError("text is required")
        return await self._request(
            "POST",
            "/ilink/bot/sendmessage",
            json={
                "msg": {
                    "client_id": str(uuid.uuid4()),
                    "to_user_id": to_user_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": self._base_info(),
            },
        )

    async def get_typing_ticket(self, ilink_user_id: str, context_token: str = "") -> str:
        self._require_token("get_typing_ticket")
        if not ilink_user_id:
            raise ILinkProtocolError("ilink_user_id is required")
        body: dict[str, Any] = {
            "ilink_user_id": ilink_user_id,
            "base_info": self._base_info(),
        }
        if context_token:
            body["context_token"] = context_token
        data = await self._request("POST", "/ilink/bot/getconfig", json=body)
        ticket = data.get("typing_ticket")
        if not isinstance(ticket, str) or not ticket:
            raise ILinkProtocolError("iLink response did not include a valid typing_ticket")
        return ticket

    async def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int) -> dict[str, Any]:
        self._require_token("send_typing")
        if not ilink_user_id:
            raise ILinkProtocolError("ilink_user_id is required")
        if not typing_ticket:
            raise ILinkProtocolError("typing_ticket is required")
        return await self._request(
            "POST",
            "/ilink/bot/sendtyping",
            json={
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": self._base_info(),
            },
        )

    def _require_token(self, operation: str) -> None:
        if not self.bot_token:
            raise ILinkAuthError(f"Bot token is required for {operation}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        requires_token: bool = True,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = DEFAULT_API_TIMEOUT_SECONDS,
        normal_read_timeout: bool = False,
    ) -> dict[str, Any]:
        if requires_token:
            self._require_token(path)
            self._assert_session_active()
        if self._client is None:
            self._client = httpx.AsyncClient()
        url = f"{self.base_url}{path}"
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json,
                headers=self._headers(),
                timeout=timeout,
            )
        except httpx.ReadTimeout as exc:
            if normal_read_timeout:
                logger.debug("iLink long poll reached client timeout path=%s", path)
                return {"ret": 0, "msgs": []}
            raise ILinkTransportError("timeout", "request timeout") from exc
        except httpx.HTTPError as exc:
            category, description = _classify_transport_error(exc)
            code = str(getattr(exc, "errno", "") or "")
            raise ILinkTransportError(category, description, code=code) from exc

        logger.debug(
            "iLink request completed method=%s path=%s status=%s",
            method,
            path,
            response.status_code,
        )
        data: dict[str, Any] | None = None
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = None

        ret = _as_int(data.get("ret")) if data else None
        errcode = _as_int(data.get("errcode")) if data else None
        errmsg = str(data.get("errmsg") or "") if data else ""
        if response.status_code >= 400:
            raise ILinkHttpError(
                response.status_code,
                ret=ret,
                errcode=errcode,
                errmsg=errmsg,
            )
        if data is None:
            raise ILinkProtocolError("iLink returned malformed or non-object JSON")
        if ret == STALE_TOKEN_ERRCODE or errcode == STALE_TOKEN_ERRCODE:
            self.pause_session()
            raise ILinkStaleTokenError(
                errmsg or "iLink token is stale; session paused",
                ret=ret,
                errcode=errcode,
            )
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise ILinkProtocolError(
                errmsg or f"iLink API error ret={ret} errcode={errcode}",
                ret=ret,
                errcode=errcode,
            )
        return data

    def _headers(self) -> dict[str, str]:
        random_uin = str(random.randint(0, 2**32 - 1)).encode()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(random_uin).decode(),
        }
        if self.bot_token:
            headers["Authorization"] = f"Bearer {self.bot_token}"
        return headers


def _reset_session_pauses_for_test() -> None:
    _pause_until_by_session.clear()
