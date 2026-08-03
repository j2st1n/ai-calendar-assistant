from app.services.telegram_service import TelegramBotRuntime


async def start_bot(token: str) -> TelegramBotRuntime:
    runtime = TelegramBotRuntime()
    _ = await runtime.reload(token)
    return runtime
