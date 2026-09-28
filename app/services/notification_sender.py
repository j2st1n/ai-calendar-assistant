"""Channel adapters. Only explicit positive API responses count as accepted sends."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret
from app.db.models import DiscordIdentity, NotificationTarget, TelegramIdentity
from app.services.notification_service import account_key
from app.services.settings_service import SettingsService


class DeliveryError(Exception):
    def __init__(self, message: str, *, retry: bool = False, uncertain: bool = False,
                 retry_after: float = 30):
        super().__init__(message)
        self.retry = retry
        self.uncertain = uncertain
        self.retry_after = max(1, retry_after)


@dataclass(frozen=True)
class Destination:
    channel: str
    token: str = field(repr=False)
    conversation_id: str
    thread_id: str
    context_token: str | None = field(repr=False)


def destination(session: Session, target: NotificationTarget) -> Destination:
    if account_key(session, target.channel) != target.account_key:
        raise DeliveryError("渠道账号已变更，请重新选择接收会话")
    if target.channel == "telegram":
        allowed = session.scalar(select(TelegramIdentity).where(
            TelegramIdentity.telegram_user_id == target.user_id, TelegramIdentity.enabled.is_(True)))
        if not allowed:
            raise DeliveryError("Telegram 接收用户已停用或移出白名单")
    if target.channel == "discord":
        allowed = session.scalar(select(DiscordIdentity).where(
            DiscordIdentity.discord_user_id == target.user_id, DiscordIdentity.enabled.is_(True)))
        if not allowed:
            raise DeliveryError("Discord 接收用户已停用或移出白名单")
    token = SettingsService(session).get(f"{target.channel}_bot_token") or ""
    return Destination(target.channel, token, target.conversation_id, target.thread_id,
                       decrypt_secret(target.context_token) if target.context_token else None)


async def send_notification(target: Destination, text: str) -> str | None:
    import httpx
    from app.integrations.ilink import ILinkClient, ILinkHttpError, ILinkProtocolError, ILinkTransportError

    try:
        if target.channel == "wechat":
            if not target.context_token:
                raise DeliveryError("微信缺少会话上下文，请向机器人发一条消息后重试")
            async with ILinkClient(target.token) as client:
                result = await client.send_message(target.conversation_id, text, target.context_token)
            if result.get("ret") != 0:
                raise DeliveryError("微信未返回明确成功状态，实际投递结果待核对", uncertain=True)
            # Reuse the channel's validated server-ID extraction; client_id is not a reply ID.
            from app.channels.wechat_handler import _bot_message_id
            return _bot_message_id(result)
        async with httpx.AsyncClient(timeout=15) as client:
            if target.channel == "telegram":
                payload: dict = {"chat_id": target.conversation_id, "text": text}
                if target.thread_id:
                    payload["message_thread_id"] = int(target.thread_id)
                response = await client.post(f"https://api.telegram.org/bot{target.token}/sendMessage", json=payload)
            elif target.channel == "discord":
                response = await client.post(
                    f"https://discord.com/api/v10/channels/{target.conversation_id}/messages",
                    headers={"Authorization": f"Bot {target.token}"},
                    json={"content": text[:2000], "allowed_mentions": {"parse": []}},
                )
            else:
                raise DeliveryError("不支持的通知渠道")
        if response.status_code == 429:
            try:
                body = response.json()
                delay = float(body.get("retry_after") or body.get("parameters", {}).get("retry_after") or 30)
            except (ValueError, TypeError, AttributeError):
                delay = 30
            raise DeliveryError("渠道限流，稍后重试", retry=True, retry_after=delay)
        if response.status_code >= 500:
            raise DeliveryError("渠道服务异常，投递结果待核对", uncertain=True)
        if response.status_code >= 400:
            raise DeliveryError(f"渠道拒绝发送（HTTP {response.status_code}），请检查账号与会话权限")
        body = response.json()
        accepted = body.get("ok") is True if target.channel == "telegram" else bool(body.get("id"))
        if not accepted:
            raise DeliveryError("渠道未确认发送成功，投递结果待核对", uncertain=True)
        message = body.get("result") if target.channel == "telegram" else body
        value = message.get("message_id" if target.channel == "telegram" else "id") if isinstance(message, dict) else None
        return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and value else None
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise DeliveryError("无法连接消息渠道，稍后重试", retry=True) from None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise DeliveryError("网络或响应异常，投递结果待核对", uncertain=True) from None
    except ILinkHttpError as exc:
        raise DeliveryError(f"微信发送异常（HTTP {exc.status_code}）",
                            retry=exc.status_code == 429, uncertain=exc.status_code >= 500) from None
    except ILinkTransportError as exc:
        # DNS failure is known not to have sent anything; other phases may have.
        raise DeliveryError("微信连接失败" if exc.category == "dns" else "微信网络异常，投递结果待核对",
                            retry=exc.category == "dns", uncertain=exc.category != "dns") from None
    except ILinkProtocolError as exc:
        if exc.ret is None and exc.errcode is None:
            raise DeliveryError("微信返回状态无法确认，投递结果待核对", uncertain=True) from None
        raise DeliveryError(f"微信拒绝发送（ret={exc.ret}, errcode={exc.errcode}），请发新消息刷新会话或检查登录状态") from None
