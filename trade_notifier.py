import asyncio
from log_store import append_log

NOTIFY_Q = asyncio.Queue()
MAIN_LOOP = None


async def notification_worker(client, chat_id_getter):
    while True:
        text = await NOTIFY_Q.get()
        try:
            chat_id = int(chat_id_getter() or 0)
            if not chat_id:
                append_log("WARN", "NOTIFY", "admin chat id missing, dropping msg")
                continue
            await client.send_message(chat_id, text)
        except Exception as e:
            append_log("ERROR", "NOTIFY", f"send failed: {e}")


def setup_loop(loop):
    global MAIN_LOOP
    MAIN_LOOP = loop


def notify(text: str):
    if not MAIN_LOOP:
        append_log("WARN", "NOTIFY", "MAIN_LOOP not ready, dropping msg")
        return
    try:
        MAIN_LOOP.call_soon_threadsafe(NOTIFY_Q.put_nowait, text)
    except Exception as e:
        append_log("ERROR", "NOTIFY", f"queue failed: {e}")
