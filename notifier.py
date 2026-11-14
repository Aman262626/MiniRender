import os
import html
import asyncio

TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
_bot_instance = None
_main_loop = None


def init_notifier(bot, loop):
    global _bot_instance, _main_loop
    _bot_instance = bot
    _main_loop = loop


async def send_alert(message: str):
    if not _bot_instance or not TELEGRAM_CHANNEL_ID:
        return
    try:
        await _bot_instance.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"Channel alert failed: {e}")


def send_alert_sync(message: str):
    if _main_loop and _bot_instance and TELEGRAM_CHANNEL_ID:
        asyncio.run_coroutine_threadsafe(send_alert(message), _main_loop)


def esc(text: str) -> str:
    return html.escape(str(text))
