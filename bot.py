import asyncio
import os
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import config as CFG
import trading_cycle as CYCLE
import night_research
from night_scheduler import run_nightly_maintenance

from telethon import TelegramClient, events
from kiteconnect import KiteConnect

from log_store import append_log, tail_text, export_all, LOG_FILE, clear_logs
from env_utils import set_env_value, get_env_value
from broker_zerodha import get_kite
from trade_notifier import notify, notification_worker, setup_loop
from control_panel import register_control_panel
import strategy_analytics as SA

IST = ZoneInfo("Asia/Kolkata")

HELP_TEXT = (
    "🤖 TRIDENT BOT – COMMANDS\n\n"
    "ACCESS:\n"
    "• /myid                → shows your Telegram ID\n"
    "• /help                → show this command list\n"
    "• /commands            → alias for /help\n"
    "• /addtrader <id>      (Owner)\n"
    "• /removetrader <id>   (Owner)\n"
    "• /addviewer <id>      (Owner)\n"
    "• /removeviewer <id>   (Owner)\n\n"
    "TOKEN (Zerodha daily) [Owner]:\n"
    "• /renewtoken            → sends Zerodha login link\n"
    "• /token <request_token> → generates access token + saves to .env\n"
    "• /tokenstatus           → check current token validity\n"
    "   /token updates token and restarts bot process\n"
    "• /restart               → restart bot process\n\n"
    "LIVE SAFETY [Owner]:\n"
    "• /initiate (or /arm)     → enables LIVE immediately (runtime override)\n"
    "• /disengage (or /disarm) → stops LIVE immediately\n\n"
    "LOOP [Trader/Owner]:\n"
    "• /startloop → start trading loop\n"
    "• /stoploop  → pause trading loop\n\n"
    "MONITOR [Viewer+]:\n"
    "• /status     → status + daily caps\n"
    "• /trailstatus → trailing lock details\n"
    "• /strategyreport → strategy analytics summary\n"
    "• /beststrategy → top strategy by net pnl\n"
    "• /worststrategy → worst strategy by net pnl\n"
    "• /regimereport → pnl/win by market regime\n"
    "• /sectorreport → pnl/win by sector\n"
    "• /logs       → log menu (daily/20/30/all/reset)\n"
    "• /logs20     → last 20 log lines\n"
    "• /logs30     → last 30 log lines\n"
    "• /exportlog  → full log as txt\n"
    "• /dailylog   → today's log as txt\n"
    "• /resetlogs  → truncate all logs (Owner)\n"
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
    "• /panic     → pause + disengage + close all open positions\n"
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

    # Full night research should run once per day (default 23:00 IST).
    ns = os.getenv("NIGHT_START", "23:00")
    ih, im = [int(x) for x in ns.split(":")]

    append_log("INFO", "NIGHT", "Night scheduler active (once per day)")

    while True:
        try:
            now = datetime.now(IST)
            run_key = now.strftime("%Y-%m-%d")
            target = now.replace(hour=ih, minute=im, second=0, microsecond=0)

            already = str(CYCLE.STATE.get("last_night_research_day") or "") == run_key
            if (now >= target) and (not already):
                append_log("INFO", "NIGHT", "Auto scheduler triggering nightly maintenance")
                await asyncio.to_thread(run_nightly_maintenance, CYCLE.STATE, True)

            await asyncio.sleep(60)
        except Exception as e:
            append_log("ERROR", "NIGHT", "Scheduler error: %s" % e)
            await asyncio.sleep(60)


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


async def _auto_restart_trident_service() -> tuple[bool, str]:
    """
    Try to restart trident service after token rotation.
    Expected deployment has passwordless sudo for this unit.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "restart", "trident",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=20)
        out = (out_b or b"").decode("utf-8", errors="ignore").strip()
        err = (err_b or b"").decode("utf-8", errors="ignore").strip()
        if proc.returncode == 0:
            return True, (out or "service restart command succeeded")
        msg = err or out or f"systemctl exit={proc.returncode}"
        return False, msg
    except Exception as e:
        return False, str(e)


async def _restart_bot_process(event) -> None:
    """Prefer service restart in managed deployments; fallback to process exit."""
    ok, msg = await _auto_restart_trident_service()
    if ok:
        await event.reply("♻️ Service restart requested successfully.")
        append_log("INFO", "BOT", f"Service restart requested: {msg}")
        return

    append_log("WARN", "BOT", f"Service restart unavailable ({msg}); falling back to process exit")
    await event.reply(f"⚠️ Service restart unavailable ({msg}). Falling back to process exit.")
    os._exit(0)


async def _dispatch_command(event, sender, cmd_word, cmd_arg):
    if cmd_word == "/myid":
        await event.reply(f"🆔 Your Telegram ID: `{int(sender)}`")
        return True

    if cmd_word in ("/help", "/commands"):
        await event.reply(HELP_TEXT)
        return True

    if cmd_word == "/status":
        await event.reply(CYCLE.get_status_text())
        return True

    if cmd_word == "/trailstatus":
        await event.reply(CYCLE.get_trailing_status_text())
        return True

    if cmd_word == "/strategyreport":
        await event.reply(SA.strategy_report_text())
        return True

    if cmd_word == "/beststrategy":
        best, _ = SA.best_worst_strategy()
        await event.reply(best)
        return True

    if cmd_word == "/worststrategy":
        _, worst = SA.best_worst_strategy()
        await event.reply(worst)
        return True

    if cmd_word == "/regimereport":
        await event.reply(SA.regime_report_text())
        return True

    if cmd_word == "/sectorreport":
        await event.reply(SA.sector_report_text())
        return True

    if cmd_word in ("/addtrader", "/removetrader", "/addviewer", "/removeviewer", "/setslip", "/exclude", "/include", "/token") and not cmd_arg:
        await event.reply("Usage error: missing command argument.")
        return True

    # ===== Owner user management =====
    if cmd_word == "/addtrader":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        uid = cmd_arg
        if uid.isdigit():
            newv = _update_id_list_env("TRADER_USER_IDS", int(uid), add=True)
            await event.reply(f"✅ Added trader {uid}\nTRADER_USER_IDS={newv}\n(Changes apply immediately)")
        else:
            await event.reply("Usage: /addtrader 123456789")
        return True

    if cmd_word == "/removetrader":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        uid = cmd_arg
        if uid.isdigit():
            newv = _update_id_list_env("TRADER_USER_IDS", int(uid), add=False)
            await event.reply(f"✅ Removed trader {uid}\nTRADER_USER_IDS={newv}")
        else:
            await event.reply("Usage: /removetrader 123456789")
        return True

    if cmd_word == "/addviewer":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        uid = cmd_arg
        if uid.isdigit():
            newv = _update_id_list_env("VIEWER_USER_IDS", int(uid), add=True)
            await event.reply(f"✅ Added viewer {uid}\nVIEWER_USER_IDS={newv}\n(Changes apply immediately)")
        else:
            await event.reply("Usage: /addviewer 123456789")
        return True

    if cmd_word == "/removeviewer":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        uid = cmd_arg
        if uid.isdigit():
            newv = _update_id_list_env("VIEWER_USER_IDS", int(uid), add=False)
            await event.reply(f"✅ Removed viewer {uid}\nVIEWER_USER_IDS={newv}")
        else:
            await event.reply("Usage: /removeviewer 123456789")
        return True

    # ===== Trader gated commands =====
    if cmd_word == "/startloop":
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        CYCLE.STATE["paused"] = False
        await event.reply("▶️ Loop Started")
        return True

    if cmd_word == "/stoploop":
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        CYCLE.STATE["paused"] = True
        await event.reply("⏸️ Loop Paused")
        return True

    # ===== Owner-only LIVE safety =====
    if cmd_word in ("/initiate", "/arm"):
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        CYCLE.STATE["initiated"] = True
        CYCLE.STATE["live_override"] = True
        await event.reply("🟢 LIVE INITIATED (runtime override enabled). Use /disengage to stop.")
        return True

    if cmd_word in ("/disengage", "/disarm"):
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        CYCLE.STATE["initiated"] = False
        CYCLE.STATE["live_override"] = False
        await event.reply("🔴 DISENGAGED (runtime override disabled). Orders blocked.")
        return True

    # ===== Logs (Viewer+) =====
    if cmd_word == "/logs":
        await event.reply(
            "📜 Log Options\n\n"
            "• /dailylog -> today's logs\n"
            "• /logs20 -> last 20 lines\n"
            "• /logs30 -> last 30 lines\n"
            "• /exportlog -> full log file\n"
            "• /resetlogs -> truncate logs (owner)"
        )
        return True

    if cmd_word in ("/logs20", "/logs30"):
        n = 20 if cmd_word == "/logs20" else 30
        try:
            txt = tail_text(n) or "(no logs)"
            if len(txt) <= 3500:
                await event.reply(txt)
            else:
                for i in range(0, len(txt), 3500):
                    await event.reply(txt[i:i + 3500])
        except Exception as e:
            append_log("ERROR", "BOT", f"{cmd_word} reply failed: {e}")
            await event.reply("❌ Failed to send logs. Try /exportlog")
        return True

    if cmd_word == "/exportlog":
        fp = export_all()
        if not fp or not os.path.exists(fp):
            await event.reply("(no logs)")
            return True
        await event.reply(file=fp, message="📦 Full log export")
        return True

    if cmd_word == "/dailylog":
        fp = _make_daily_log_file()
        if not fp or not os.path.exists(fp):
            await event.reply("(no logs for today)")
            return True
        await event.reply(file=fp, message="📅 Today's log export")
        return True

    if cmd_word == "/resetlogs":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        clear_logs()
        await event.reply("✅ Logs cleared.")
        return True

    if cmd_word == "/positions":
        try:
            txt = CYCLE.get_positions_text()
        except Exception as e:
            txt = f"❌ Failed to fetch positions: {e}"
        await event.reply(txt)
        return True

    # ===== Research (Trader/Owner) =====
    if cmd_word == "/nightnow":
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        await event.reply("🌙 Running night research now...")
        try:
            await asyncio.to_thread(run_nightly_maintenance, CYCLE.STATE, True)
            await event.reply("✅ Night research done.")
        except Exception as e:
            await event.reply("❌ Night research failed: %s" % e)
        return True

    if cmd_word == "/nightreport":
        txt = night_research.last_report_summary()
        if len(txt) <= 3500:
            await event.reply("🧪 Night Report\n\n" + (txt or "(empty)"))
        else:
            for i in range(0, len(txt), 3500):
                await event.reply(txt[i:i + 3500])
        return True

    if cmd_word == "/nightlog":
        txt = night_research.read_night_log_tail(120)
        if len(txt) <= 3500:
            await event.reply("📝 NIGHT RESEARCH LOG\n\n" + txt)
        else:
            for i in range(0, len(txt), 3500):
                await event.reply(txt[i:i + 3500])
        return True

    if cmd_word == "/universe":
        syms = CYCLE.load_universe_trading()
        await event.reply("📦 TRADING Universe (%d)\n\n%s" % (len(syms), "\n".join(syms[:50]) if syms else "(empty)"))
        return True

    if cmd_word == "/universe_live":
        syms = CYCLE.load_universe_live()
        await event.reply("📈 LIVE Universe (%d)\n\n%s" % (len(syms), "\n".join(syms[:50]) if syms else "(empty)"))
        return True

    # ===== Promote (Trader/Owner) =====
    if cmd_word == "/promotestatus":
        msg = "Last promote: %s" % (CYCLE.STATE.get("last_promote_msg") or "N/A")
        await event.reply("🔄 Promote Status\n\n" + msg)
        return True

    if cmd_word == "/promote_now":
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        if CYCLE.STATE.get("open_trades"):
            await event.reply("❌ Cannot promote while in open positions.")
            return True
        ok = CYCLE.promote_universe(reason="MANUAL")
        await event.reply("✅ Promoted live→trading" if ok else ("❌ Promote blocked: " + (CYCLE.STATE.get("last_promote_msg") or "")))
        return True

    # ===== Slippage (Trader/Owner) =====
    if cmd_word == "/setslip":
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        v = _parse_float(cmd_arg)
        if v is None or v < 0:
            await event.reply("Usage: /setslip 0.30")
            return True
        set_env_value("MAX_ENTRY_SLIPPAGE_PCT", str(v))
        os.environ["MAX_ENTRY_SLIPPAGE_PCT"] = str(v)
        CYCLE.set_runtime_param("MAX_ENTRY_SLIPPAGE_PCT", float(v))
        await event.reply("✅ MAX_ENTRY_SLIPPAGE_PCT set to %s (restart optional)" % v)
        return True

    # ===== Insider safety (Owner only) =====
    if cmd_word == "/excluded":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        await event.reply(CYCLE.list_exclusions())
        return True

    if cmd_word == "/exclude":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        sym = cmd_arg.strip().upper()
        await event.reply(CYCLE.exclude_symbol(sym))
        return True

    if cmd_word == "/include":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        sym = cmd_arg.strip().upper()
        await event.reply(CYCLE.include_symbol(sym))
        return True

    # ===== Emergency (Owner only) =====
    if cmd_word == "/panic":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        CYCLE.STATE["paused"] = True
        close_ok = CYCLE._close_all_open_trades("PANIC")
        CYCLE.STATE["initiated"] = False
        CYCLE.STATE["live_override"] = False
        if close_ok:
            await event.reply("🛑 PANIC done: paused + disengaged + close-all attempted.")
        else:
            await event.reply("🛑 PANIC done: paused + disengaged; one or more close actions failed.")
        return True

    if cmd_word == "/restart":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        await event.reply("♻️ Restart requested...")
        await _restart_bot_process(event)
        return True

    if cmd_word == "/resetday":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        CYCLE.manual_reset_day()
        await event.reply("✅ Day reset done.")
        return True

    # ===== Token flow (Owner only) =====
    if cmd_word == "/renewtoken":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        if not getattr(CFG, "KITE_LOGIN_URL", ""):
            await event.reply("❌ KITE_LOGIN_URL missing in .env")
            return True
        await event.reply(
            "🔑 Renew Zerodha Session\n\n"
            "1) Open this link & login:\n%s\n\n"
            "2) Copy request_token from redirect URL\n"
            "3) Send:\n/token YOUR_REQUEST_TOKEN" % CFG.KITE_LOGIN_URL
        )
        return True

    if cmd_word == "/tokenstatus":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        token_now = os.getenv("KITE_ACCESS_TOKEN", "").strip() or getattr(CFG, "KITE_ACCESS_TOKEN", "")
        if not token_now:
            await event.reply("❌ Token status: missing (no KITE_ACCESS_TOKEN set)")
            return True
        try:
            kite = get_kite()
            profile = kite.profile() or {}
            user = profile.get("user_name") or profile.get("user_id") or "unknown"
            await event.reply(f"✅ Token status: valid (user={user})")
        except Exception as e:
            await event.reply(f"❌ Token status: invalid/expired ({e})")
        return True

    if cmd_word == "/token":
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        req_token = cmd_arg.strip()
        if not getattr(CFG, "KITE_API_KEY", ""):
            await event.reply("❌ KITE_API_KEY missing in .env")
            return True
        api_secret = os.getenv("KITE_API_SECRET", "").strip()
        if not api_secret:
            await event.reply("❌ KITE_API_SECRET missing in .env (add it and restart)")
            return True
        try:
            kite = KiteConnect(api_key=CFG.KITE_API_KEY)
            data = kite.generate_session(req_token, api_secret=api_secret)
            access = data["access_token"]
            set_env_value("KITE_ACCESS_TOKEN", access)
            os.environ["KITE_ACCESS_TOKEN"] = access
            await event.reply("✅ Token updated. Restarting bot.")
            append_log("INFO", "BOT", "Token updated; restarting bot process")
            os._exit(0)
        except Exception as e:
            await event.reply("❌ Token update failed: %s" % e)
        return True

    return False


def _time_greeting(now=None):
    now = now or datetime.now(IST)
    h = int(now.hour)
    if h < 12:
        return "Good morning"
    if h < 17:
        return "Good afternoon"
    return "Good evening"


async def _send_startup_trade_message(client):
    owner = int(getattr(CFG, "ADMIN_USER_ID", 0) or _owner_id() or 0)
    if owner <= 0:
        append_log("WARN", "BOT", "Startup greeting skipped: owner/admin id missing")
        return
    try:
        ent = await client.get_entity(owner)
        name = (getattr(ent, "first_name", None) or getattr(ent, "username", None) or "there").strip()
        msg = (
            f"👋 Hi {name},\n"
            f"{_time_greeting()}\n"
            "We're starting with the trade."
        )
        await client.send_message(owner, msg)
        append_log("INFO", "BOT", f"Startup greeting sent to {owner}")
    except Exception as e:
        append_log("WARN", "BOT", f"Startup greeting failed: {e}")


async def main():
    api_id = int(getattr(CFG, "TELEGRAM_API_ID", 9888950))
    api_hash = getattr(CFG, "TELEGRAM_API_HASH", "ecfa673e2c85b4ef16743acf0ba0d1c1")

    if not CFG.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing in .env")

    client = TelegramClient("trident", api_id, api_hash)
    await client.start(bot_token=CFG.TELEGRAM_BOT_TOKEN)
    append_log("INFO", "BOT", "Telegram bot started")

    setup_loop(asyncio.get_running_loop())
    asyncio.create_task(notification_worker(client, lambda: int(getattr(CFG, "ADMIN_USER_ID", 0) or _owner_id() or 0)))

    CYCLE.set_notifier(notify)
    await _send_startup_trade_message(client)

    def _mk_panel_handler(command_name):
        async def _run(message_event):
            sender = int(message_event.sender_id)
            await _dispatch_command(message_event, sender, f"/{command_name}", "")
        return _run

    panel_handlers = {
        "myid": _mk_panel_handler("myid"),
        "help": _mk_panel_handler("help"),
        "commands": _mk_panel_handler("commands"),
        "startloop": _mk_panel_handler("startloop"),
        "stoploop": _mk_panel_handler("stoploop"),
        "status": _mk_panel_handler("status"),
        "trailstatus": _mk_panel_handler("trailstatus"),
        "logs": _mk_panel_handler("logs"),
        "logs20": _mk_panel_handler("logs20"),
        "logs30": _mk_panel_handler("logs30"),
        "dailylog": _mk_panel_handler("dailylog"),
        "exportlog": _mk_panel_handler("exportlog"),
        "resetlogs": _mk_panel_handler("resetlogs"),
        "positions": _mk_panel_handler("positions"),
        "nightnow": _mk_panel_handler("nightnow"),
        "universe": _mk_panel_handler("universe"),
        "universe_live": _mk_panel_handler("universe_live"),
        "nightreport": _mk_panel_handler("nightreport"),
        "nightlog": _mk_panel_handler("nightlog"),
        "promotestatus": _mk_panel_handler("promotestatus"),
        "promote_now": _mk_panel_handler("promote_now"),
        "renewtoken": _mk_panel_handler("renewtoken"),
        "tokenstatus": _mk_panel_handler("tokenstatus"),
        "restart": _mk_panel_handler("restart"),
        "initiate": _mk_panel_handler("initiate"),
        "arm": _mk_panel_handler("arm"),
        "disengage": _mk_panel_handler("disengage"),
        "disarm": _mk_panel_handler("disarm"),
        "panic": _mk_panel_handler("panic"),
        "resetday": _mk_panel_handler("resetday"),
        "excluded": _mk_panel_handler("excluded"),
    }
    register_control_panel(client, panel_handlers)

    @client.on(events.NewMessage())
    async def handler(event):
        if not _is_private(event):
            return

        sender = int(event.sender_id)
        cmd = (event.raw_text or "").strip()
        parts = cmd.split(maxsplit=1)
        cmd_word = (parts[0].split("@", 1)[0].lower() if parts else "")
        cmd_arg = (parts[1].strip() if len(parts) > 1 else "")

        # Always allow /myid and /start
        if cmd_word == "/myid":
            await event.reply(f"🆔 Your Telegram ID: `{sender}`")
            return
        if cmd_word == "/start":
            # Control panel module handles rendering /start.
            return

        # Viewer gate for everything else
        if not _is_viewer(sender):
            append_log("WARN", "AUTH", f"Denied command from {sender}: {cmd_word}")
            await event.reply("❌ Not permitted. Use /myid and ask owner to grant Viewer/Trader access.")
            return

        handled = await _dispatch_command(event, sender, cmd_word, cmd_arg)
        if not handled:
            await event.reply("Unknown command. Use /help")


    await asyncio.gather(
        client.run_until_disconnected(),
        asyncio.to_thread(CYCLE.run_loop_forever),
        night_scheduler(),
    )


if __name__ == "__main__":
    asyncio.run(main())
