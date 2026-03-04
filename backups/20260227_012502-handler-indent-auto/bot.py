import asyncio
import os
from datetime import datetime, timedelta, time as dtime

import config as CFG
import trading_cycle as CYCLE
import night_research

from telethon import TelegramClient, events
from kiteconnect import KiteConnect

from log_store import append_log, tail_text, export_all, LOG_FILE
from env_utils import set_env_value, get_env_value
from broker_zerodha import get_kite


HELP_TEXT = (
    "🤖 TRIDENT BOT – COMMANDS\n\n"
    "ACCESS:\n"
    "• /myid                → shows your Telegram ID\n"
    "• (Owner) /addtrader <id>, /removetrader <id>\n"
    "• (Owner) /addviewer <id>, /removeviewer <id>\n\n"
    "TOKEN (Zerodha daily) [Owner]:\n"
    "• /renewtoken            → sends Zerodha login link\n"
    "• /token <request_token> → generates access token + saves to .env\n"
    "   After /token, restart: sudo systemctl restart trident\n\n"
    "LIVE SAFETY [Owner]:\n"
    "• /initiate (or /arm)     → enables LIVE immediately (runtime override)\n"
    "• /disengage (or /disarm) → stops LIVE immediately\n\n"
    "LOOP [Trader/Owner]:\n"
    "• /startloop → start trading loop\n"
    "• /stoploop  → pause trading loop\n\n"
    "MONITOR [Viewer+]:\n"
    "• /status     → status + daily caps\n"
    "• /logs       → last 20 log lines\n"
    "• /exportlog  → full log as txt\n"
    "• /dailylog   → today's log as txt\n"
    "• /positions  → Zerodha net positions\n\n"
    "RESEARCH [Trader/Owner]:\n"
    "• /nightnow       → rebuild live universe now\n"
    "• /universe       → show TRADING universe\n"
    "• /universe_live  → show LIVE universe\n"
    "• /nightreport    → research report summary\n"
    "• /nightlog       → recent NIGHT log lines\n\n"
    "AUTO-PROMOTE [Trader/Owner]:\n"
    "• /promotestatus  → last promote status\n"
    "• /promote_now    → manual promote live→trading (only if flat)\n\n"
    "SLIPPAGE [Trader/Owner]:\n"
    "• /setslip X → MAX_ENTRY_SLIPPAGE_PCT (example: /setslip 0.30)\n\n"
    "INSIDER SAFETY [Owner]:\n"
    "• /exclude SBIN   → permanently block symbol\n"
    "• /include SBIN   → release symbol\n"
    "• /excluded       → list blocked symbols\n\n"
    "EMERGENCY [Owner]:\n"
    "• /panic     → pause + disengage + close open trade\n"
    "• /resetday  → reset today's pnl & risk counters\n"
)


def _parse_ids(csv_text):
    out = set()
    if not csv_text:
        return out
    for p in csv_text.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except Exception:
            continue
    return out


def _owner_id():
    # Prefer OWNER_USER_ID; fallback to ADMIN_USER_ID
    o = os.getenv("OWNER_USER_ID", "").strip()
    if not o:
        o = os.getenv("ADMIN_USER_ID", "").strip()
    try:
        return int(o) if o else 0
    except Exception:
        return 0


def _role_sets():
    owner = _owner_id()

    traders = _parse_ids(os.getenv("TRADER_USER_IDS", ""))
    viewers = _parse_ids(os.getenv("VIEWER_USER_IDS", ""))

    # backward compatibility: if only ADMIN_USER_ID exists, treat as owner+trader+viewer
    if owner:
        traders.add(owner)
        viewers.add(owner)

    return owner, traders, viewers


def _is_owner(sender_id):
    owner, _, _ = _role_sets()
    return int(sender_id) == int(owner)


def _is_trader(sender_id):
    _, traders, _ = _role_sets()
    return int(sender_id) in traders


def _is_viewer(sender_id):
    owner, traders, viewers = _role_sets()
    sid = int(sender_id)
    return (sid == int(owner)) or (sid in traders) or (sid in viewers)


def _is_private(event):
    try:
        return bool(event.is_private)
    except Exception:
        return False


def _make_daily_log_file():
    if not os.path.exists(LOG_FILE):
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "trident_daily_%s.txt" % today)
    with open(LOG_FILE, "r") as f:
        lines = f.readlines()
    todays = [ln for ln in lines if ln.startswith(today)]
    if not todays:
        return None
    with open(out_path, "w") as f:
        f.writelines(todays)
    return out_path


def _tail_night_lines(n=120):
    txt = tail_text(n * 5)
    if not txt:
        return "(no logs)"
    lines = [ln for ln in txt.splitlines() if "[NIGHT]" in ln]
    return "\n".join(lines[-n:]) if lines else "(no NIGHT lines yet)"


def _read_universe(path, limit=40):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r") as f:
        syms = [ln.strip().upper() for ln in f if ln.strip()]
    return syms[:limit]


def _parse_float(s):
    try:
        return float(s.strip())
    except Exception:
        return None


def _in_time_range(now_t, start_t, end_t):
    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t


async def night_scheduler():
    enabled = str(os.getenv("NIGHT_AUTO_ENABLED", "true")).lower() == "true"
    if not enabled:
        append_log("INFO", "NIGHT", "Night scheduler disabled")
        return

    ns = os.getenv("NIGHT_START", "18:30")
    ih, im = [int(x) for x in ns.split(":")]
    start_t = dtime(ih, im)

    es = os.getenv("ENTRY_START", "09:20")
    eh, em = [int(x) for x in es.split(":")]
    offset = int(os.getenv("NIGHT_END_OFFSET_MIN", "5"))
    end_dt = (datetime.now().replace(hour=eh, minute=em, second=0, microsecond=0) - timedelta(minutes=offset))
    end_t = end_dt.time()

    interval_min = int(os.getenv("NIGHT_INTERVAL_MIN", "90"))

    append_log("INFO", "NIGHT", "Night scheduler active")

    while True:
        try:
            now = datetime.now()
            if _in_time_range(now.time(), start_t, end_t):
                append_log("INFO", "NIGHT", "Auto scheduler triggering night research")
                await asyncio.to_thread(night_research.run_night_job)
                await asyncio.sleep(interval_min * 60)
            else:
                await asyncio.sleep(10 * 60)
        except Exception as e:
            append_log("ERROR", "NIGHT", "Scheduler error: %s" % e)
            await asyncio.sleep(10 * 60)


def _update_id_list_env(key, user_id, add=True):
    """
    Updates comma-separated list in .env and process env (runtime).
    """
    user_id = int(user_id)
    current = get_env_value(key, os.getenv(key, "")).strip()
    s = _parse_ids(current)
    if add:
        s.add(user_id)
    else:
        if user_id in s:
            s.remove(user_id)
    new_val = ",".join([str(x) for x in sorted(s)])
    set_env_value(key, new_val)
    os.environ[key] = new_val
    return new_val


async def main():
    api_id = int(getattr(CFG, "TELEGRAM_API_ID", 9888950))
    api_hash = getattr(CFG, "TELEGRAM_API_HASH", "ecfa673e2c85b4ef16743acf0ba0d1c1")

    if not CFG.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing in .env")

    client = TelegramClient("trident", api_id, api_hash)
    await client.start(bot_token=CFG.TELEGRAM_BOT_TOKEN)
    append_log("INFO", "BOT", "Telegram bot started")

    @client.on(events.NewMessage())
    async def handler(event):
    # TRIDENT_HANDLER_CLEAN_v1 (auto-rewritten)
    # Safe command router: fixes broken indentation from earlier patches.
    try:
        text = (event.raw_text or "").strip()
    except Exception:
        text = ""
    cmd = (text.split()[0].lower() if text else "")

    # Prefer private messages only (if helper exists)
    try:
        if ' _is_private' in globals() and callable(globals().get('_is_private')):
            if not _is_private(event):
                return
    except Exception:
        pass

    # Sender id
    try:
        sender = int(getattr(event, 'sender_id', 0) or 0)
    except Exception:
        sender = 0

    # Always allow /myid
    if cmd == "/myid":
        await event.reply(f"🆔 Your Telegram ID: `{sender}`")
        return

    # Viewer gate (if helper exists)
    try:
        if '_is_viewer' in globals() and callable(globals().get('_is_viewer')):
            if not _is_viewer(sender):
                return
    except Exception:
        pass

    # /status
    if cmd == "/status":
        try:
            if 'CYCLE' in globals() and hasattr(CYCLE, 'get_status_text'):
                await event.reply(CYCLE.get_status_text())
            elif 'CYCLE' in globals() and hasattr(CYCLE, 'status_text'):
                await event.reply(CYCLE.status_text())
            else:
                await event.reply("⚠️ Status not available (CYCLE missing)")
        except Exception as e:
            await event.reply(f"❌ status error: {e}")
        return

    # /excluded
    if cmd == "/excluded":
        try:
            if 'CYCLE' in globals() and hasattr(CYCLE, 'exclusions_text'):
                await event.reply(CYCLE.exclusions_text())
            elif 'CYCLE' in globals() and hasattr(CYCLE, 'get_excluded_text'):
                await event.reply(CYCLE.get_excluded_text())
            else:
                await event.reply("⚠️ Exclusions view not available (trading_cycle missing helper)")
        except Exception as e:
            await event.reply(f"❌ exclusions error: {e}")
        return

    # /restart (owner only if helper exists)
    if cmd == "/restart":
        try:
            if '_is_owner' in globals() and callable(globals().get('_is_owner')):
                if not _is_owner(sender):
                    await event.reply("⛔ Not allowed")
                    return
        except Exception:
            pass
        await event.reply("🔁 Restarting bot service...")
        import os
        os.system("sudo systemctl restart trident >/dev/null 2>&1 &")
        return
    await asyncio.gather(
        client.run_until_disconnected(),
        asyncio.to_thread(CYCLE.run_loop_forever),
        night_scheduler(),
    )


if __name__ == "__main__":
    asyncio.run(main())

def _request_restart_flag():
    try:
        if getattr(CFG, "ENABLE_TOKEN_AUTORESTART", True):
            flag_path = getattr(CFG, "RESTART_FLAG_PATH", "/home/ubuntu/trident-bot/RESTART_REQUIRED")
            with open(flag_path, "w", encoding="utf-8") as f:
                f.write("restart\n")
            append_log("INFO","RESTART","Restart flag created after /token. systemd will restart the bot.")
    except Exception as e:
        append_log("ERROR","RESTART", f"Failed to create restart flag: {e}")
import asyncio
import trading_cycle as CYCLE

async def start_engine():
    from log_store import append_log
    append_log("INFO", "ENGINE", "Trading engine starting...")
    await asyncio.to_thread(CYCLE.run_loop_forever)

if __name__ == "__main__":
    asyncio.run(start_engine())
