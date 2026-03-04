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
        if not _is_private(event):
            return

        sender = int(event.sender_id)
        cmd = (event.raw_text or "").strip()
       raw = cmd.lower()

        if raw == "/excluded":
            try:
                if hasattr(CYCLE, "exclusions_text"):
                    await event.reply(CYCLE.exclusions_text())
                else:
                    await event.reply("⚠️ exclusions view unavailable")
            except Exception as e:
                await event.reply(f"❌ exclusions error: {e}")
            return

        if raw == "/restart":
            await event.reply("🔁 Restarting service…")
            import os
            os.system("sudo systemctl restart trident &")
            return

        # Always allow /myid
        if cmd == "/myid":
            await event.reply(f"🆔 Your Telegram ID: `{sender}`")
            return

        # Viewer gate for everything else
        if not _is_viewer(sender):
            return

        if cmd in ("/help", "/commands"):
            await event.reply(HELP_TEXT)
            return

        if cmd == "/status":
            await event.reply(CYCLE.get_status_text())
            return

        # ===== Owner user management =====
        if cmd.startswith("/addtrader ") and _is_owner(sender):
            uid = cmd.split(maxsplit=1)[1].strip()
            if uid.isdigit():
                newv = _update_id_list_env("TRADER_USER_IDS", int(uid), add=True)
                await event.reply(f"✅ Added trader {uid}\nTRADER_USER_IDS={newv}\n(Changes apply immediately)")
            else:
                await event.reply("Usage: /addtrader 123456789")
            return

        if cmd.startswith("/removetrader ") and _is_owner(sender):
            uid = cmd.split(maxsplit=1)[1].strip()
            if uid.isdigit():
                newv = _update_id_list_env("TRADER_USER_IDS", int(uid), add=False)
                await event.reply(f"✅ Removed trader {uid}\nTRADER_USER_IDS={newv}")
            else:
                await event.reply("Usage: /removetrader 123456789")
            return

        if cmd.startswith("/addviewer ") and _is_owner(sender):
            uid = cmd.split(maxsplit=1)[1].strip()
            if uid.isdigit():
                newv = _update_id_list_env("VIEWER_USER_IDS", int(uid), add=True)
                await event.reply(f"✅ Added viewer {uid}\nVIEWER_USER_IDS={newv}\n(Changes apply immediately)")
            else:
                await event.reply("Usage: /addviewer 123456789")
            return

        if cmd.startswith("/removeviewer ") and _is_owner(sender):
            uid = cmd.split(maxsplit=1)[1].strip()
            if uid.isdigit():
                newv = _update_id_list_env("VIEWER_USER_IDS", int(uid), add=False)
                await event.reply(f"✅ Removed viewer {uid}\nVIEWER_USER_IDS={newv}")
            else:
                await event.reply("Usage: /removeviewer 123456789")
            return

        # ===== Trader gated commands =====
        if cmd == "/startloop":
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            CYCLE.STATE["paused"] = False
            await event.reply("▶️ Loop Started")
            return

        if cmd == "/stoploop":
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            CYCLE.STATE["paused"] = True
            await event.reply("⏸️ Loop Paused")
            return

        # ===== Owner-only LIVE safety =====
        if cmd in ("/initiate", "/arm"):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            CYCLE.STATE["initiated"] = True
            CYCLE.STATE["live_override"] = True
            await event.reply("🟢 LIVE INITIATED (runtime override enabled). Use /disengage to stop.")
            return

        if cmd in ("/disengage", "/disarm"):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            CYCLE.STATE["initiated"] = False
            CYCLE.STATE["live_override"] = False
            await event.reply("🔴 DISENGAGED (runtime override disabled). Orders blocked.")
            return

        # ===== Logs (Viewer+) =====
        if cmd == "/logs":
            await event.reply(tail_text(20) or "(no logs)")
            return

        if cmd == "/exportlog":
            fp = export_all()
            if not fp or not os.path.exists(fp):
                await event.reply("No log file found.")
                return
            await event.reply("📤 Sending full log (txt)…")
            await client.send_file(event.chat_id, fp)
            return

        if cmd == "/dailylog":
            fp = _make_daily_log_file()
            if not fp:
                await event.reply("No logs for today yet.")
                return
            await event.reply("📤 Sending today's log (txt)…")
            await client.send_file(event.chat_id, fp)
            return

        # ===== Positions (Viewer+) =====
        if cmd == "/positions":
            try:
                kite = get_kite()
                pos = kite.positions() or {}
                net = pos.get("net", []) or []
                rows = []
                for p in net:
                    qty = int(p.get("quantity") or 0)
                    if qty == 0:
                        continue
                    rows.append(
                        "%s:%s qty=%s avg=%.2f pnl=%.2f" % (
                            p.get("exchange"),
                            p.get("tradingsymbol"),
                            qty,
                            float(p.get("average_price") or 0.0),
                            float(p.get("pnl") or 0.0),
                        )
                    )
                await event.reply("📊 Net Positions\n\n" + ("\n".join(rows) if rows else "None"))
            except Exception as e:
                await event.reply("❌ Positions failed: %s" % e)
            return

        # ===== Research (Trader/Owner) =====
        if cmd == "/nightnow":
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            await event.reply("🌙 Running Night Research…")
            try:
                await asyncio.to_thread(night_research.run_night_job)
                await event.reply("✅ Done. Use /universe_live or /nightreport.")
            except Exception as e:
                await event.reply("❌ Night research failed: %s" % e)
            return

        if cmd == "/nightlog":
            await event.reply("🌙 Night Logs (recent)\n\n" + _tail_night_lines(120))
            return

        if cmd == "/nightreport":
            rpt = os.path.join(os.getcwd(), "logs", "night_research_report.txt")
            if os.path.exists(rpt):
                with open(rpt, "r") as f:
                    txt = f.read()
                await event.reply(txt[-3500:])
            else:
                await event.reply("No night report yet. Run /nightnow")
            return

        if cmd == "/universe":
            trade_path = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt"))
            syms = _read_universe(trade_path, 50)
            await event.reply("📌 TRADING Universe (%d)\n\n%s" % (len(syms), "\n".join(syms) if syms else "(empty)"))
            return

        if cmd == "/universe_live":
            live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))
            syms = _read_universe(live_path, 50)
            await event.reply("📈 LIVE Universe (%d)\n\n%s" % (len(syms), "\n".join(syms) if syms else "(empty)"))
            return

        # ===== Promote (Trader/Owner) =====
        if cmd == "/promotestatus":
            msg = "Last promote: %s" % (CYCLE.STATE.get("last_promote_msg") or "N/A")
            await event.reply("🔄 Promote Status\n\n" + msg)
            return

        if cmd == "/promote_now":
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            if CYCLE.STATE.get("open_trade"):
                await event.reply("❌ Cannot promote while in open trade.")
                return
            ok = CYCLE.promote_universe(reason="MANUAL")
            await event.reply("✅ Promoted live→trading" if ok else ("❌ Promote blocked: " + (CYCLE.STATE.get("last_promote_msg") or "")))
            return

        # ===== Slippage (Trader/Owner) =====
        if cmd.startswith("/setslip "):
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            v = _parse_float(cmd.split(maxsplit=1)[1])
            if v is None or v < 0:
                await event.reply("Usage: /setslip 0.30")
                return
            set_env_value("MAX_ENTRY_SLIPPAGE_PCT", str(v))
            os.environ["MAX_ENTRY_SLIPPAGE_PCT"] = str(v)
            CYCLE.set_runtime_param("MAX_ENTRY_SLIPPAGE_PCT", float(v))
            await event.reply("✅ MAX_ENTRY_SLIPPAGE_PCT set to %s (restart optional)" % v)
            return

        # ===== Insider safety (Owner only) =====
        if cmd == "/excluded":
            await event.reply(CYCLE.list_exclusions())
            return

        if cmd.startswith("/exclude "):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            sym = cmd.split(maxsplit=1)[1].strip().upper()
            await event.reply(CYCLE.exclude_symbol(sym))
            return

        if cmd.startswith("/include "):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            sym = cmd.split(maxsplit=1)[1].strip().upper()
            await event.reply(CYCLE.include_symbol(sym))
            return

        # ===== Emergency (Owner only) =====
        if cmd == "/panic":
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            CYCLE.STATE["paused"] = True
            CYCLE.STATE["initiated"] = False
            CYCLE.STATE["live_override"] = False
            CYCLE._close_open_trade("PANIC")
            await event.reply("🛑 PANIC done: paused + disengaged + attempted close.")
            return

        if cmd == "/resetday":
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            CYCLE.manual_reset_day()
            await event.reply("✅ Day reset done.")
            return

        # ===== Token flow (Owner only) =====
        if cmd == "/renewtoken":
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            if not getattr(CFG, "KITE_LOGIN_URL", ""):
                await event.reply("❌ KITE_LOGIN_URL missing in .env")
                return
            await event.reply(
                "🔑 Renew Zerodha Session\n\n"
                "1) Open this link & login:\n%s\n\n"
                "2) Copy request_token from redirect URL\n"
                "3) Send:\n/token YOUR_REQUEST_TOKEN" % CFG.KITE_LOGIN_URL
            )
            return

        if cmd.startswith("/token "):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            req_token = cmd.split(" ", 1)[1].strip()
            if not getattr(CFG, "KITE_API_KEY", ""):
                await event.reply("❌ KITE_API_KEY missing in .env")
                return
            api_secret = os.getenv("KITE_API_SECRET", "").strip()
            if not api_secret:
                await event.reply("❌ KITE_API_SECRET missing in .env (add it and restart)")
                return
            try:
                kite = KiteConnect(api_key=CFG.KITE_API_KEY)
                data = kite.generate_session(req_token, api_secret=api_secret)
                access = data["access_token"]
                set_env_value("KITE_ACCESS_TOKEN", access)
                await event.reply("✅ Access token updated in .env. Now run: sudo systemctl restart trident")
            except Exception as e:
                await event.reply("❌ Token update failed: %s" % e)
            return

        await event.reply("Unknown command. Use /help")

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
