#!/usr/bin/env python3
"""Offline emulator for the Telegram bot command flow.

This script does not connect to Telegram or broker APIs.
It mirrors the command parsing + permission gates in bot.py so
we can dry-run how a message would be handled.
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot


@dataclass
class EmuState:
    paused: bool = False


def parse_command(raw_text: str) -> tuple[str, str]:
    cmd = (raw_text or "").strip()
    parts = cmd.split(maxsplit=1)
    cmd_word = parts[0].split("@", 1)[0].lower() if parts else ""
    cmd_arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd_word, cmd_arg


def emulate_message(sender: int, text: str, private: bool, state: EmuState) -> str:
    if not private:
        return "ignored (non-private chat)"

    cmd_word, cmd_arg = parse_command(text)

    if cmd_word == "/myid":
        return f"🆔 Your Telegram ID: {sender}"

    if not bot._is_viewer(sender):
        return "❌ Not permitted. Use /myid and ask owner to grant Viewer/Trader access."

    if cmd_word in ("/help", "/commands"):
        return "HELP_TEXT"

    if cmd_word == "/startloop":
        if not bot._is_trader(sender):
            return "❌ Not permitted (Trader/Owner only)."
        state.paused = False
        return "▶️ Loop Started"

    if cmd_word == "/stoploop":
        if not bot._is_trader(sender):
            return "❌ Not permitted (Trader/Owner only)."
        state.paused = True
        return "⏸️ Loop Paused"

    if cmd_word == "/excluded":
        if not bot._is_owner(sender):
            return "❌ Not permitted (Owner only)."
        return "(would return exclusions list)"

    if cmd_word == "/restart":
        if not bot._is_owner(sender):
            return "❌ Not permitted (Owner only)."
        return "♻️ Restart requested... (would attempt systemctl restart trident)"

    if cmd_word == "/token":
        if not bot._is_owner(sender):
            return "❌ Not permitted (Owner only)."
        if not cmd_arg:
            return "Usage error: missing command argument."
        return "✅ Token accepted (emulated), then restart requested"

    return "Unknown command. Use /help"


def main() -> None:
    # Demo identity map
    os.environ["OWNER_USER_ID"] = "1001"
    os.environ["TRADER_USER_IDS"] = "2002"
    os.environ["VIEWER_USER_IDS"] = "3003"

    state = EmuState(paused=False)

    cases = [
        (9999, "/status", True),
        (3003, "/startloop", True),
        (2002, "/startloop", True),
        (2002, "/stoploop", True),
        (3003, "/excluded", True),
        (1001, "/excluded", True),
        (1001, "/restart", True),
        (1001, "/token abc123", True),
    ]

    print("=== TRIDENT BOT FLOW EMULATION ===")
    for sender, text, private in cases:
        out = emulate_message(sender, text, private, state)
        print(f"[{sender}] {text} -> {out}")
    print(f"Final paused state: {state.paused}")


if __name__ == "__main__":
    main()
