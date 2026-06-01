from __future__ import annotations

import base64
import logging
import random
import uuid
from typing import Any

import httpx

ILINK_BASE = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.2"

logger = logging.getLogger(__name__)


class ILinkError(Exception):
    pass


class ILinkAuthError(ILinkError):
    pass


class ILinkClient:
    def __init__(self, bot_token: str = "", base_url: str = ILINK_BASE) -> None:
        self.bot_token = bot_token
        self.base_url = base_url.rstrip("/")

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
        raise ILinkError("iLink response did not include a qrcode value")

    async def get_qrcode_status(self, qrcode: str) -> str:
        if not qrcode:
            raise ILinkError("qrcode is required")
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
        """Return the full QR-status payload (status + optional bot_token)."""
        if not qrcode:
            raise ILinkError("qrcode is required")
        return await self._request(
            "GET", "/ilink/bot/get_qrcode_status", params={"qrcode": qrcode}, requires_token=False
        )

    async def get_updates(self, buf: str = "") -> tuple[list[dict[str, Any]], str]:
        self._require_token("get_updates")
        data = await self._request(
            "POST",
            "/ilink/bot/getupdates",
            json={
                "get_updates_buf": buf,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            timeout=45.0,
        )
        msgs = data.get("msgs") or data.get("messages") or []
        if not isinstance(msgs, list):
            raise ILinkError("iLink getupdates returned a non-list msgs field")
        typed_msgs = [msg for msg in msgs if isinstance(msg, dict)]
        new_buf = data.get("get_updates_buf")
        return typed_msgs, new_buf if isinstance(new_buf, str) else buf

    async def send_message(self, to_user_id: str, text: str, context_token: str) -> dict[str, Any]:
        self._require_token("send_message")
        if not to_user_id:
            raise ILinkError("to_user_id is required")
        if not context_token:
            raise ILinkError("context_token is required")
        if not text:
            raise ILinkError("text is required")
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
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
        )

    async def get_typing_ticket(self, ilink_user_id: str) -> str:
        self._require_token("get_typing_ticket")
        if not ilink_user_id:
            raise ILinkError("ilink_user_id is required")
        data = await self._request(
            "POST",
            "/ilink/bot/getconfig",
            json={
                "ilink_user_id": ilink_user_id,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
        )
        ticket = data.get("typing_ticket")
        if not isinstance(ticket, str) or not ticket:
            raise ILinkError("iLink response did not include a valid typing_ticket")
        return ticket

    async def send_typing(
        self, ilink_user_id: str, typing_ticket: str, status: int
    ) -> dict[str, Any]:
        self._require_token("send_typing")
        if not ilink_user_id:
            raise ILinkError("ilink_user_id is required")
        if not typing_ticket:
            raise ILinkError("typing_ticket is required")
        return await self._request(
            "POST",
            "/ilink/bot/sendtyping",
            json={
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": {"channel_version": CHANNEL_VERSION},
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
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        if requires_token and not self.bot_token:
            raise ILinkAuthError("Bot token is required")
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, params=params, json=json, headers=self._headers())
        logger.debug("iLink request completed method=%s path=%s status=%s", method, path, response.status_code)
        if response.status_code in (401, 403):
            raise ILinkAuthError(f"iLink auth failed: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ILinkError(f"iLink request failed: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ILinkError("iLink returned malformed JSON") from exc
        if not isinstance(data, dict):
            raise ILinkError("iLink returned non-object JSON")
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
