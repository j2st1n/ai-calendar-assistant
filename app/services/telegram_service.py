from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

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

        identities = session.scalars(
            select(TelegramIdentity).where(TelegramIdentity.enabled.is_(True))
        ).all()

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

    async def reload_bot(self, token: str) -> str:
        global _runtime
        if _runtime is None:
            _runtime = TelegramBotRuntime()
        return await _runtime.reload(token)

    def stop_bot(self) -> None:
        global _runtime
        if _runtime is not None:
            _runtime.stop()

    def add_user(self, session: Session, user_id: str, username: str, display_name: str) -> None:
        ident = session.scalar(
            select(TelegramIdentity).where(TelegramIdentity.telegram_user_id == user_id)
        )
        if ident is None:
            ident = TelegramIdentity(telegram_user_id=user_id, username=username, display_name=display_name)
            session.add(ident)
        else:
            ident.username = username or ident.username
            ident.display_name = display_name or ident.display_name
            ident.enabled = True
        session.commit()
        _rejected_users[:] = [r for r in _rejected_users if r[0] != user_id]

    def remove_user(self, session: Session, user_id: str) -> None:
        ident = session.scalar(
            select(TelegramIdentity).where(TelegramIdentity.telegram_user_id == user_id)
        )
        if ident:
            session.delete(ident)
            session.commit()

    def is_user_allowed(self, session: Session, user_id: str) -> bool:
        ident = session.scalar(
            select(TelegramIdentity).where(TelegramIdentity.telegram_user_id == user_id)
        )
        return ident is not None and ident.enabled

    def generate_bind_link(self, bot_username: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(12)
        _bind_tokens[token] = time.time() + BIND_TOKEN_LIFETIME
        _clean_bind_tokens()
        return f"https://t.me/{bot_username}?start=bind_{token}", token

    def validate_bind_token(self, token: str) -> bool:
        _clean_bind_tokens()
        expiry = _bind_tokens.pop(token, None)
        return expiry is not None and expiry >= time.time()

    def check_bind_status(self, token: str) -> str:
        _clean_bind_tokens()
        if token not in _bind_tokens:
            return "used"
        if _bind_tokens[token] < time.time():
            return "expired"
        return "pending"

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

    async def reload(self, token: str) -> str:
        old_task = self._task

        if old_task is not None and not old_task.done():
            old_task.cancel()
            await asyncio.sleep(1.5)

        self._task = None
        self._application = None
        self.running = False
        self._last_error = ""

        from telegram import Update
        from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

        app = Application.builder().token(token).build()
        from telegram.ext import filters as ptb_filters

        app.add_handler(CommandHandler("start", _handle_start))
        app.add_handler(CommandHandler("help", _handle_help))
        app.add_handler(CommandHandler("upcoming", _handle_upcoming))
        app.add_handler(CommandHandler("latest", _handle_latest))
        app.add_handler(CommandHandler("status", _handle_status))
        app.add_handler(MessageHandler(ptb_filters.PHOTO, _handle_photo))
        app.add_handler(MessageHandler(~ptb_filters.COMMAND, _handle_message))
        self._application = app
        self.running = True

        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._start_bot(app))
        return "started"

    async def _start_bot(self, app) -> None:
        try:
            await app.initialize()
            await app.start()
            from telegram import BotCommand
            await app.bot.set_my_commands([
                BotCommand("start", "开始使用"),
                BotCommand("help", "使用帮助"),
                BotCommand("upcoming", "未来日程"),
                BotCommand("latest", "最近一条"),
                BotCommand("status", "配置状态"),
            ])
            await app.updater.start_polling(drop_pending_updates=True)
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.running = False
        except Exception as exc:
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
            await update.effective_message.reply_text("❌ 绑定码无效或已过期，请在控制台重新生成。")
            return

    with SessionLocal() as session:
        service = TelegramService()
        if not service.is_user_allowed(session, user_id):
            await update.effective_message.reply_text(
                f"👋 你还未授权使用此 Bot\n\n"
                f"你的 user_id：{user_id}\n"
                f"请在控制台 → Telegram → 绑定授权中授权。"
            )
            return

    await update.effective_message.reply_text(
        "👋 我是 AI 日程助手\n\n"
        "直接发消息给我，我会自动识别日程并写入日历：\n"
        "• 明天下午 3 点和张三开会\n"
        "• 下周三上午体检，记得带报告\n"
        "• 每周一早上 9 点站会\n\n"
        "回复日程消息可以修改或删除。\n"
        "支持图片识别（需配置）。"
    )


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None or update.effective_message.text is None:
        return
    user_id = str(update.effective_user.id) if update.effective_user else ""

    await update.effective_chat.send_chat_action(action="typing")

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

        try:
            processor = MessageProcessor()
            reply_id = str(update.effective_message.reply_to_message.message_id) if update.effective_message.reply_to_message else None
            replies = await processor.process(session, user_id, update.effective_message.text, reply_id)
            for response, record_id in replies:
                sent = await update.effective_message.reply_text(response)
                if record_id and sent:
                    rec = session.get(EventRecord, record_id)
                    if rec:
                        rec.bot_message_id = str(sent.message_id)
            session.commit()
        except Exception as exc:
            logger.exception("Message processing failed")
            await update.effective_message.reply_text(f"处理消息时出错：{exc}")


async def _handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        "👋 使用方式\n\n"
        "直接发消息即可创建日程：\n"
        "明天下午 3 点和张三开会，地点会议室 A\n\n"
        "回复日程消息可以修改或删除。\n"
        "支持图片识别（需配置）。\n\n"
        "/start    — 开始使用\n"
        "/upcoming — 未来日程（/upcoming 7 = 未来 7 天，最多 14 天）\n"
        "/latest   — 最近一条日程\n"
        "/status   — 配置状态\n"
        "/help     — 使用帮助"
    )


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    user_id = str(update.effective_user.id) if update.effective_user else ""

    with SessionLocal() as session:
        service = TelegramService()
        if not service.is_user_allowed(session, user_id):
            await update.effective_message.reply_text("你没有权限使用此 Bot。")
            return

        await update.effective_chat.send_chat_action(action="typing")

        photo = update.effective_message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()

        settings_service = SettingsService(session)
        use_main = settings_service.get("ai_vision_use_main") or "true"
        if use_main != "false":
            from app.services.ai_provider_service import AIProviderConfig, AIProviderService as AISvc
            config = AIProviderConfig(
                provider_type=settings_service.get("ai_provider_type") or "openai_compatible",
                base_url=settings_service.get("ai_base_url") or "https://api.openai.com/v1",
                api_key=settings_service.get("ai_api_key"),
                model=settings_service.get("ai_model"),
            )
        else:
            if not settings_service.get("ai_vision_model"):
                await update.effective_message.reply_text(
                    "📸 未配置识图模型，请先在控制台 AI 设置中配置。"
                )
                return
            from app.services.ai_provider_service import AIProviderConfig, AIProviderService as AISvc
            config = AIProviderConfig(
                provider_type=settings_service.get("ai_vision_provider_type") or "openai_compatible",
                base_url=settings_service.get("ai_vision_base_url") or "https://api.openai.com/v1",
                api_key=settings_service.get("ai_vision_api_key"),
                model=settings_service.get("ai_vision_model"),
            )

        import base64
        img_b64 = base64.b64encode(bytes(img_bytes)).decode()
        try:
            text = await AISvc().vision_completion(config, img_b64)
        except Exception as exc:
            await update.effective_message.reply_text(f"图片识别失败：{exc}")
            return

        processor = MessageProcessor()
        replies = await processor.process(session, user_id, text)
        for response, _ in replies:
            await update.effective_message.reply_text(response)


async def _handle_upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    user_id = str(update.effective_user.id) if update.effective_user else ""
    days = 7
    if context.args:
        try:
            days = min(int(context.args[0]), 14)
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
                EventRecord.operation.in_(["create", "update", "delete"]),
                EventRecord.status == "success",
            )
            .order_by(EventRecord.created_at.desc())
        ).scalars().all()

        if not records:
            await update.effective_message.reply_text(f"📅 未来 {days} 天暂无日程")
            return

        import json as _json
        seen = set()
        active = []
        for rec in records:
            uid = rec.caldav_uid or f"_{rec.id}"
            if uid in seen:
                continue
            seen.add(uid)
            if rec.operation == "delete":
                continue
            active.append(rec)

        from datetime import date as dt_date, timedelta

        cutoff = dt_date.today().isoformat()
        end = (dt_date.today() + timedelta(days=days)).isoformat()
        active = [r for r in active if cutoff <= _get_start(r, _json) < end]

        if not active:
            await update.effective_message.reply_text(f"📅 未来 {days} 天暂无生效日程")
            return

        active.sort(key=lambda r: _get_start(r, _json))

        groups: dict[str, list] = {}
        for rec in active:
            st = _get_start(rec, _json)
            key = st[:10]
            groups.setdefault(key, []).append(rec)

        lines = [f"📅 未来 {days} 天日程", ""]
        for d in sorted(groups):
            lines.append(d)
            evts = groups[d]
            for rec in evts:
                title = rec.title or "(无标题)"
                time_part = _get_start(rec, _json)[11:16]
                lines.append(f"🕒 {time_part}  {title}")
            lines.append("")
        await update.effective_message.reply_text("\n".join(lines).strip())


def _get_start(rec, json_mod) -> str:
    if rec.event_json:
        try:
            return json_mod.loads(rec.event_json).get("start_time", "") or "9"
        except Exception:
            return "9"
    return "9"


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

        records = session.execute(
            select(EventRecord)
            .where(
                EventRecord.telegram_user_id == user_id,
                EventRecord.operation.in_(["create", "update", "delete"]),
                EventRecord.status == "success",
            )
            .order_by(EventRecord.created_at.desc())
        ).scalars().all()

        rec = None
        seen = set()
        for r in records:
            uid = r.caldav_uid or f"_{r.id}"
            if uid in seen:
                continue
            seen.add(uid)
            if r.operation != "delete":
                rec = r
                break

        if rec is None:
            await update.effective_message.reply_text("📌 暂无日程记录")
            return

        event_json = rec.event_json or "{}"
        import json as _json
        data = _json.loads(event_json) if event_json else {}
        lines = ["📌 最近日程", ""]
        lines.append(f"{rec.title or '(无标题)'}")
        if data.get("start_time"):
            lines.append(f"🕒 {data['start_time'][:16].replace('T', ' ')}")
        if data.get("location"):
            lines.append(f"📍 {data['location']}")
        lines.append("")
        lines.append("回复这条消息可修改或删除。")
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
        ai_vendor = svc.get("ai_provider_name") or "未配置"
        ai_model = svc.get("ai_model") or "未配置"
        caldav_name = svc.get("caldav_calendar_name") or "未配置"

        await update.effective_message.reply_text(
            f"🤖 AI：{ai_vendor} / {ai_model}\n"
            f"📆 日历：{caldav_name}"
        )
