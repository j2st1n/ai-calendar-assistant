from __future__ import annotations

import asyncio
import logging
import secrets
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.message_processor import MessageProcessor
from app.core.crypto import mask_secret
from app.db.models import EventRecord, TelegramIdentity
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
        bot_error = _runtime._last_error if _runtime else ""

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
            "bot_error": bot_error,
            "allowed_users": allowed,
            "rejected_users": rejected,
        }

    def save_token(self, session: Session, token: str, username: str) -> None:
        settings_service = SettingsService(session)
        settings_service.set("telegram_bot_token", token, encrypted=True)
        settings_service.set("telegram_bot_username", username.strip().lstrip("@"))
        settings_service.commit()

    def reload_bot(self, token: str) -> str:
        global _runtime
        if _runtime is None:
            _runtime = TelegramBotRuntime()
        return _runtime.reload(token)

    def stop_bot(self) -> None:
        global _runtime
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
        self._task = None
        self.running = False
        self._last_error = ""

    def reload(self, token: str) -> str:
        self.stop()
        from telegram import Update
        from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler("start", _handle_start))
        app.add_handler(CommandHandler("help", _handle_help))
        app.add_handler(CommandHandler("list", _handle_list))
        app.add_handler(CommandHandler("latest", _handle_latest))
        app.add_handler(CommandHandler("status", _handle_status))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
        self._application = app
        self.running = True

        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._start_bot(app))
        logger.info("Telegram bot reload triggered")
        return "started"

    async def _start_bot(self, app) -> None:
        try:
            async with app:
                await app.updater.start_polling(drop_pending_updates=True)
                logger.info("Telegram bot started successfully")
                while True:
                    await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Telegram bot task cancelled")
            self.running = False
        except Exception as exc:
            logger.exception("Telegram bot failed to start")
            self.running = False
            self._last_error = str(exc)

    def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
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
        response, record_id = await processor.process(session, user_id, update.effective_message.text, reply_id)
        sent = await update.effective_message.reply_text(response)
        if record_id and sent:
            rec = session.get(EventRecord, record_id)
            if rec:
                rec.bot_message_id = str(sent.message_id)
                session.commit()


async def _handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        "命令：\n"
        "/start - 查看简介或绑定链接\n"
        "/help - 查看帮助\n"
        "/list [days] - 查看未来 N 天日程（默认 7 天，最大 30 天）\n"
        "/latest - 查看最近一条日程\n"
        "/status - 查看当前 AI / CalDAV / Bot 配置状态\n\n"
        "直接发送日程描述即可创建：\n"
        "明天下午3点和张三开会，地点会议室A"
    )


async def _handle_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    user_id = str(update.effective_user.id) if update.effective_user else ""
    days = 7
    if context.args:
        try:
            days = min(int(context.args[0]), 30)
        except ValueError:
            pass

    with SessionLocal() as session:
        service = TelegramService()
        if not service.is_user_allowed(session, user_id):
            await update.effective_message.reply_text("你没有权限使用此 Bot。")
            return

        from datetime import datetime as dt
        from sqlalchemy import select

        records = session.execute(
            select(EventRecord)
            .where(
                EventRecord.telegram_user_id == user_id,
                EventRecord.operation.in_(["create", "update"]),
                EventRecord.status == "success",
            )
            .order_by(EventRecord.created_at.desc())
            .limit(30)
        ).scalars().all()

        if not records:
            await update.effective_message.reply_text(f"未来 {days} 天暂无日程记录。")
            return

        lines = [f"最近 {days} 天的日程记录："]
        for rec in records[:days]:
            title = rec.title or "(无标题)"
            created = rec.created_at.strftime("%m-%d %H:%M") if rec.created_at else ""
            lines.append(f"- {created} {title}")
        await update.effective_message.reply_text("\n".join(lines))


async def _handle_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    user_id = str(update.effective_user.id) if update.effective_user else ""

    with SessionLocal() as session:
        service = TelegramService()
        if not service.is_user_allowed(session, user_id):
            await update.effective_message.reply_text("你没有权限使用此 Bot。")
            return

        from sqlalchemy import select

        rec = session.execute(
            select(EventRecord)
            .where(
                EventRecord.telegram_user_id == user_id,
                EventRecord.operation.in_(["create", "update"]),
                EventRecord.status == "success",
            )
            .order_by(EventRecord.created_at.desc())
        ).scalar()

        if rec is None:
            await update.effective_message.reply_text("暂无日程记录。")
            return

        event_json = rec.event_json or "{}"
        import json as _json
        data = _json.loads(event_json) if event_json else {}
        lines = ["最近一条日程：", ""]
        lines.append(f"📌 标题：{rec.title or '(无标题)'}")
        if data.get("start_time"):
            lines.append(f"🕒 时间：{data['start_time'][:16].replace('T', ' ')}")
        if data.get("location"):
            lines.append(f"📍 地点：{data['location']}")
        lines.append(f"📅 创建于：{rec.created_at.strftime('%Y-%m-%d %H:%M') if rec.created_at else ''}")
        lines.append("")
        lines.append("如需修改或删除，请直接回复这条消息。")
        lines.append('"时间改成下午4点"')
        lines.append('"删除这条"')
        msg = await update.effective_message.reply_text("\n".join(lines))
        rec.bot_message_id = str(msg.message_id)
        session.commit()


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    user_id = str(update.effective_user.id) if update.effective_user else ""

    with SessionLocal() as session:
        service = TelegramService()
        if not service.is_user_allowed(session, user_id):
            await update.effective_message.reply_text("你没有权限使用此 Bot。")
            return

        from app.services.settings_service import SettingsService
        svc = SettingsService(session)
        ai = f"{svc.get('ai_provider_name') or '未配置'} ({svc.get('ai_model') or '未选择'})"
        caldav = "已配置" if svc.get("caldav_url") else "未配置"
        tg = "已配置" if svc.get("telegram_bot_token") else "未配置"

        await update.effective_message.reply_text(
            f"状态：\n"
            f"AI：{ai}\n"
            f"CalDAV：{caldav}\n"
            f"Telegram Bot：{tg}"
        )
