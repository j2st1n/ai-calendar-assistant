from __future__ import annotations

import logging
import secrets
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.message_processor import MessageProcessor
from app.core.crypto import mask_secret
from app.db.models import TelegramIdentity
from app.db.session import SessionLocal
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

BIND_TOKEN_LIFETIME = 600
MAX_REJECTED_LOG = 50

_rejected_users: list[tuple[str, str, str]] = []
_bind_tokens: dict[str, float] = {}


def get_telegram_bot_runtime():
    global _runtime
    return _runtime


def record_rejected_user(user_id: str, username: str, display_name: str) -> None:
    entry = (user_id, username or "", display_name or "")
    _rejected_users.append(entry)
    while len(_rejected_users) > MAX_REJECTED_LOG:
        _rejected_users.pop(0)


def recent_rejected_users() -> list[tuple[str, str, str]]:
    return list(_rejected_users)


class TelegramService:
    def config_summary(self, session: Session) -> dict:
        settings_service = SettingsService(session)
        token = settings_service.get("telegram_bot_token")
        token_masked = settings_service.get_masked("telegram_bot_token")
        bot_username = settings_service.get("telegram_bot_username") or ""
        bot_running = _runtime is not None and _runtime.running

        identities = session.execute(
            select(TelegramIdentity).where(TelegramIdentity.enabled.is_(True))
        ).scalars().all()

        allowed = [
            {"id": ident.id, "user_id": ident.telegram_user_id, "username": ident.username or ""}
            for ident in identities
        ]

        rejected = [{"user_id": uid, "username": uname, "display_name": dname}
                     for uid, uname, dname in recent_rejected_users()]

        return {
            "bot_token_masked": token_masked,
            "bot_token_set": bool(token),
            "bot_username": bot_username,
            "bot_running": bot_running,
            "allowed_users": allowed,
            "rejected_users": rejected,
        }

    def save_token(self, session: Session, token: str, username: str) -> None:
        settings_service = SettingsService(session)
        settings_service.set("telegram_bot_token", token, encrypted=True)
        settings_service.set("telegram_bot_username", username.strip().lstrip("@"))
        settings_service.commit()

    def reload_bot(self, token: str) -> str:
        if _runtime is None:
            _runtime = TelegramBotRuntime()
        return _runtime.reload(token)

    def stop_bot(self) -> None:
        if _runtime is not None:
            _runtime.stop()

    def add_user(self, session: Session, user_id: str, username: str, display_name: str) -> None:
        ident = session.get(TelegramIdentity, user_id) or session.execute(
            select(TelegramIdentity).where(TelegramIdentity.telegram_user_id == user_id)
        ).scalar()
        if ident is None:
            ident = TelegramIdentity(telegram_user_id=user_id, username=username, display_name=display_name)
            session.add(ident)
        else:
            ident.username = username or ident.username
            ident.display_name = display_name or ident.display_name
            ident.enabled = True
        session.commit()
        _rejected_users[:] = [r for r in _rejected_users if r[0] != user_id]

    def disable_user(self, session: Session, user_id: str) -> None:
        ident = session.get(TelegramIdentity, user_id)
        if ident:
            ident.enabled = False
            session.commit()

    def is_user_allowed(self, session: Session, user_id: str) -> bool:
        ident = session.get(TelegramIdentity, f"{user_id}")
        return ident is not None and ident.enabled

    def generate_bind_link(self, bot_username: str) -> str:
        token = secrets.token_urlsafe(12)
        _bind_tokens[token] = time.time() + BIND_TOKEN_LIFETIME
        _clean_bind_tokens()
        return f"https://t.me/{bot_username}?start=bind_{token}"

    def validate_bind_token(self, token: str) -> bool:
        _clean_bind_tokens()
        expiry = _bind_tokens.pop(token, None)
        return expiry is not None and expiry >= time.time()

    def set_bot_running_username(self, username: str) -> None:
        pass


def _clean_bind_tokens() -> None:
    now = time.time()
    expired = [k for k, v in _bind_tokens.items() if v < now]
    for k in expired:
        _bind_tokens.pop(k, None)


_runtime: "TelegramBotRuntime | None" = None


class TelegramBotRuntime:
    def __init__(self) -> None:
        self._application = None
        self.running = False

    def reload(self, token: str) -> str:
        self.stop()
        from telegram import Update
        from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler("start", _handle_start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
        self._application = app
        self.running = True
        logger.info("Telegram bot initialized")
        return "started"

    def stop(self) -> None:
        self.running = False
        self._application = None


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    text = update.effective_message.text or ""
    user_id = str(update.effective_user.id) if update.effective_user else ""

    if "bind_" in text:
        token = text.split("bind_", 1)[-1].split()[0].strip()
        service = TelegramService()
        with SessionLocal() as session:
            if service.validate_bind_token(token):
                username = update.effective_user.username or ""
                display_name = update.effective_user.full_name if update.effective_user else ""
                service.add_user(session, user_id, username, display_name)
                await update.effective_message.reply_text("✅ 绑定成功，你现在可以使用此 Bot。")
                return
            await update.effective_message.reply_text("❌ 绑定码无效或已过期，请在 Console 重新生成。")
            return

    await update.effective_message.reply_text(
        "你好！我是 AI Calendar Assistant。\n"
        "发送一条包含日程的自然语言消息，我会自动提取并写入日历。\n\n"
        "示例：明天下午3点和张三开会，地点会议室A"
    )


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None or update.effective_message.text is None:
        return
    user_id = str(update.effective_user.id) if update.effective_user else ""

    with SessionLocal() as session:
        service = TelegramService()
        if not service.is_user_allowed(session, user_id):
            username = update.effective_user.username or ""
            display_name = update.effective_user.full_name if update.effective_user else ""
            record_rejected_user(user_id, username, display_name)
            await update.effective_message.reply_text(
                f"你没有权限使用此 Bot。\n"
                f"你的 Telegram user_id 是：{user_id}\n"
                f"请联系管理员加入白名单。"
            )
            return

        processor = MessageProcessor()
        reply_id = str(update.effective_message.reply_to_message.message_id) if update.effective_message.reply_to_message else None
        response = await processor.process(session, user_id, update.effective_message.text, reply_id)
        await update.effective_message.reply_text(response)
