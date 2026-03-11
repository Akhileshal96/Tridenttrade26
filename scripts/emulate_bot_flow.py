#!/usr/bin/env python3
"""Offline emulator for Telegram command flow using bot._dispatch_command.

This does not connect to Telegram or broker APIs. It reuses the real parser,
permission gates and command dispatcher to reduce drift.
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot


class FakeEvent:
    def __init__(self, sender_id: int, raw_text: str, private: bool = True):
        self.sender_id = sender_id
        self.raw_text = raw_text
        self.is_private = private
        self.replies = []

    async def reply(self, message=None, **kwargs):
        if message is None and "message" in kwargs:
            message = kwargs.get("message")
        self.replies.append(str(message or ""))



def parse_command(raw_text: str) -> tuple[str, str]:
    cmd = (raw_text or "").strip()
    parts = cmd.split(maxsplit=1)
    cmd_word = parts[0].split("@", 1)[0].lower() if parts else ""
    cmd_arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd_word, cmd_arg


async def emulate_message(sender: int, text: str, private: bool) -> str:
    event = FakeEvent(sender, text, private=private)
    if not private:
        return "ignored (non-private chat)"

    cmd_word, cmd_arg = parse_command(text)

    if cmd_word == "/myid":
        await event.reply(f"🆔 Your Telegram ID: `{sender}`")
        return event.replies[-1]

    if cmd_word == "/start":
        return "(handled by control panel /start renderer)"

    if not bot._is_viewer(sender):
        return "❌ Not permitted. Use /myid and ask owner to grant Viewer/Trader access."

    handled = await bot._dispatch_command(event, sender, cmd_word, cmd_arg)
    if not handled:
        return "Unknown command. Use /help"
    return event.replies[-1] if event.replies else "(handled with no direct text reply)"


async def main() -> None:
    os.environ["OWNER_USER_ID"] = "1001"

    # prevent emulator from mutating local .env or restarting process
    bot.set_env_value = lambda *args, **kwargs: None
    async def _noop_restart(*args, **kwargs):
        return None
    bot._restart_bot_process = _noop_restart
    os.environ["TRADER_USER_IDS"] = "2002"
    os.environ["VIEWER_USER_IDS"] = "3003"

    cases = [
        (9999, "/status", True),
        (3003, "/startloop", True),
        (2002, "/startloop", True),
        (2002, "/stoploop", True),
        (3003, "/excluded", True),
        (1001, "/excluded", True),
        (1001, "/setslip 0.25", True),
        (1001, "/logs20", True),
    ]

    print("=== TRIDENT BOT FLOW EMULATION (dispatcher-backed) ===")
    for sender, text, private in cases:
        out = await emulate_message(sender, text, private)
        print(f"[{sender}] {text} -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
