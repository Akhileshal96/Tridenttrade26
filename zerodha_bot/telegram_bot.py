import json
import logging
from pathlib import Path

import pandas as pd
from kiteconnect import KiteConnect
from telethon import TelegramClient, events

from zerodha_bot.config import (
    KITE_ACCESS_TOKEN,
    KITE_API_KEY,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_TOKEN,
)

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

kite = KiteConnect(api_key=KITE_API_KEY)
if KITE_ACCESS_TOKEN:
    kite.set_access_token(KITE_ACCESS_TOKEN)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is required for telegram_bot.py")
if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
    raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required for Telethon")

client = TelegramClient(
    session=str(BASE_DIR / "telethon_bot"),
    api_id=int(TELEGRAM_API_ID),
    api_hash=TELEGRAM_API_HASH,
).start(bot_token=TELEGRAM_TOKEN)


def load_excluded() -> set[str]:
    try:
        with open(BASE_DIR / "excluded.json", "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
        return set(data.get("stocks", []))
    except Exception:
        return set()


def save_excluded(excluded_set: set[str]):
    with open(BASE_DIR / "excluded.json", "w", encoding="utf-8") as file_obj:
        json.dump({"stocks": sorted(list(excluded_set))}, file_obj, indent=2)


HELP_TEXT = (
    "/help - List commands\n"
    "/status - Account balance and positions\n"
    "/summary - Trade summary (P/L)\n"
    "/exclude SYMBOL - Exclude stock\n"
    "/include SYMBOL - Include stock\n"
    "/tokenlink - Get Kite login URL\n"
    "/log - Show recent log lines\n"
    "/exportlog - Export log file\n"
    "/exporttrades - Export trade history\n"
    "/health - Bot health check"
)


@client.on(events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
async def cmd_help(event):
    await event.reply(HELP_TEXT)


@client.on(events.NewMessage(pattern=r"^/status(?:@\w+)?$"))
async def cmd_status(event):
    try:
        margins = kite.margins(segment="equity")
        net_balance = margins.get("net")
        text = f"💰 Available Margin: {net_balance}\n\n"
    except Exception:
        await event.reply("Error retrieving account margins.")
        return

    try:
        positions = kite.positions().get("net", [])
        if positions:
            text += "Open Positions:\n"
            for pos in positions:
                sym = pos.get("tradingsymbol")
                qty = pos.get("quantity")
                pnl = pos.get("pnl")
                if qty != 0:
                    text += f" - {sym}: Qty {qty}, P&L {pnl}\n"
        else:
            text += "No open positions."
    except Exception:
        text += "No open positions."

    await event.reply(text)


@client.on(events.NewMessage(pattern=r"^/summary(?:@\w+)?$"))
async def cmd_summary(event):
    try:
        df = pd.read_json(LOG_DIR / "trades.jsonl", lines=True)
        total_trades = df.shape[0]
        profit = 0.0
        for _, row in df.iterrows():
            if row["action"] == "BUY":
                profit -= row["price"] * row["quantity"]
            elif row["action"] == "SELL":
                profit += row["price"] * row["quantity"]
        text = f"📈 Trade Summary:\nTotal Trades: {total_trades}\nNet P/L: {profit:.2f}"
    except Exception:
        text = "No trades logged yet."
    await event.reply(text)


@client.on(events.NewMessage(pattern=r"^/exclude(?:@\w+)?(?:\s+(.+))?$"))
async def cmd_exclude(event):
    symbol = (event.pattern_match.group(1) or "").strip().upper()
    if not symbol:
        await event.reply("Usage: /exclude SYMBOL")
        return

    excluded = load_excluded()
    if symbol in excluded:
        await event.reply(f"{symbol} is already excluded.")
        return

    excluded.add(symbol)
    save_excluded(excluded)
    await event.reply(f"{symbol} has been excluded from trading.")


@client.on(events.NewMessage(pattern=r"^/include(?:@\w+)?(?:\s+(.+))?$"))
async def cmd_include(event):
    symbol = (event.pattern_match.group(1) or "").strip().upper()
    if not symbol:
        await event.reply("Usage: /include SYMBOL")
        return

    excluded = load_excluded()
    if symbol in excluded:
        excluded.remove(symbol)
        save_excluded(excluded)
        await event.reply(f"{symbol} has been included for trading.")
    else:
        await event.reply(f"{symbol} was not in the exclude list.")


@client.on(events.NewMessage(pattern=r"^/tokenlink(?:@\w+)?$"))
async def cmd_tokenlink(event):
    try:
        await event.reply(f"Login here: {kite.login_url()}")
    except Exception:
        await event.reply("Error generating token link.")


@client.on(events.NewMessage(pattern=r"^/log(?:@\w+)?$"))
async def cmd_log(event):
    try:
        with open(LOG_DIR / "app.log", "r", encoding="utf-8") as file_obj:
            lines = file_obj.readlines()[-20:]
        log_text = "".join(lines) or "Log is empty."
        await event.reply(f"```\n{log_text}\n```")
    except Exception:
        await event.reply("Could not read log file.")


@client.on(events.NewMessage(pattern=r"^/exportlog(?:@\w+)?$"))
async def cmd_exportlog(event):
    log_path = LOG_DIR / "app.log"
    if not log_path.exists():
        await event.reply("Failed to export log file.")
        return
    await client.send_file(event.chat_id, str(log_path))


@client.on(events.NewMessage(pattern=r"^/exporttrades(?:@\w+)?$"))
async def cmd_exporttrades(event):
    try:
        trade_path = LOG_DIR / "trades.jsonl"
        df = pd.read_json(trade_path, lines=True)
        excel_path = LOG_DIR / "trades.xlsx"
        df.to_excel(excel_path, index=False)
        await client.send_file(event.chat_id, [str(trade_path), str(excel_path)])
    except Exception:
        await event.reply("Failed to export trades.")


@client.on(events.NewMessage(pattern=r"^/health(?:@\w+)?$"))
async def cmd_health(event):
    await event.reply("🤖 Bot is running and healthy.")


logger.info("Telethon bot polling...")
client.run_until_disconnected()
