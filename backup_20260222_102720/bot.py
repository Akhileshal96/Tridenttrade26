import asyncio
import os
from datetime import datetime, timedelta, time as dtime

import config as CFG
import trading_cycle as CYCLE
import night_research

from telethon import TelegramClient, events
from kiteconnect import KiteConnect

from log_store import append_log, tail_text, export_all, LOG_FILE
from env_utils import set_env_value
from broker_zerodha import get_kite


HELP_TEXT = (
    "🤖 TRIDENT BOT – COMMANDS\n\n"
    "TOKEN (Zerodha daily):\n"
    "• /renewtoken            → sends Zerodha login link\n"
    "• /token <request_token> → generates access token + saves to .env\n"
    "   After /token, restart: sudo systemctl restart trident\n\n"
    "LIVE SAFETY:\n"
    "• /initiate (or /arm)     → enables LIVE immediately (runtime override)\n"
    "• /disengage (or /disarm) → stops LIVE immediately\n\n"
    "LOOP:\n"
    "• /startloop → start trading loop\n"
    "• /stoploop  → pause trading loop\n\n"
    "MONITOR:\n"
    "• /status     → status + daily caps\n"
    "• /logs       → last 20 log lines\n"
    "• /exportlog  → full log as txt\n"
    "• /dailylog   → today's log as txt\n"
    "• /positions  → Zerodha net positions\n\n"
    "RESEARCH:\n"
    "• /nightnow       → rebuild live universe now\n"
    "• /universe       → show TRADING universe\n"
    "• /universe_live  → show LIVE universe\n"
    "• /nightreport    → research report summary\n"
    "• /nightlog       → recent NIGHT log lines\n\n"
    "AUTO-PROMOTE:\n"
    "• /promotestatus  → last promote status\n"
    "• /promote_now    → manual promote live→trading (only if flat)\n\n"
    "SLIPPAGE:\n"
    "• /setslip X → MAX_ENTRY_SLIPPAGE_PCT (example: /setslip 0.30)\n\n"
    "INSIDER SAFETY:\n"
    "• /exclude SBIN   → permanently block symbol\n"
    "• /include SBIN   → release symbol\n"
    "• /excluded       → list blocked symbols\n\n"
    "EMERGENCY:\n"
    "• /panic     → pause + disengage + close open trade\n"
    "• /resetday  → reset today's pnl & risk counters\n"
)


def _is_admin(event):
    try:
        return event.is_private and int(event.sender_id) == int(CFG.ADMIN_USER_ID)
    except Exception:
        return False


def _parse_float(s):
    try:
        return float(s.strip())
    except Exception:
        return None


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


def _in_time_range(now_t, start_t, end_t):
    # supports overnight range
    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t


async def night_scheduler():
    """
    Runs night research repeatedly between NIGHT_START and (ENTRY_START - offset).
    """
    enabled = str(getattr(CFG, "NIGHT_AUTO_ENABLED", "true")).lower() == "true"
    if not enabled:
        append_log("INFO", "NIGHT", "Night scheduler disabled")
        return

    # Default: 18:30 to (ENTRY_START - 5min)
    ns = getattr(CFG, "NIGHT_START", "18:30")
    ih, im = [int(x) for x in ns.split(":")]
    start_t = dtime(ih, im)

    # compute end time from ENTRY_START - offset
    es = getattr(CFG, "ENTRY_START", "09:20")
    eh, em = [int(x) for x in es.split(":")]
    offset = int(getattr(CFG, "NIGHT_END_OFFSET_MIN", 5))
    end_dt = (datetime.now().replace(hour=eh, minute=em, second=0, microsecond=0) - timedelta(minutes=offset))
    end_t = end_dt.time()

    interval_min = int(getattr(CFG, "NIGHT_INTERVAL_MIN", 90))

    append_log("INFO", "NIGHT", f"Night scheduler active: {start_t} -> {end_t} every {interval_min}min")

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
            append_log("ERROR", "NIGHT", f"Scheduler error: {e}")
            await asyncio.sleep(10 * 60)


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
        if not _is_admin(event):
            return

        cmd = (event.raw_text or "").strip()

        if cmd in ("/help", "/commands"):
            await event.reply(HELP_TEXT)
            return

        if cmd == "/status":
            await event.reply(CYCLE.get_status_text())
            return

        # LOOP
        if cmd == "/startloop":
            CYCLE.STATE["paused"] = False
            await event.reply("▶️ Loop Started")
            return

        if cmd == "/stoploop":
            CYCLE.STATE["paused"] = True
            await event.reply("⏸️ Loop Paused")
            return

        # LIVE SAFETY
        if cmd in ("/initiate", "/arm"):
            CYCLE.STATE["initiated"] = True
            CYCLE.STATE["live_override"] = True
            await event.reply("🟢 LIVE INITIATED (runtime override enabled). Use /disengage to stop.")
            return

        if cmd in ("/disengage", "/disarm"):
            CYCLE.STATE["initiated"] = False
            CYCLE.STATE["live_override"] = False
            await event.reply("🔴 DISENGAGED (runtime override disabled). Orders blocked.")
            return

        # LOGS
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

        # POSITIONS
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

        # RESEARCH
        if cmd == "/nightnow":
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

        # AUTO-PROMOTE
        if cmd == "/promotestatus":
            msg = "Last promote: %s" % (CYCLE.STATE.get("last_promote_msg") or "N/A")
            await event.reply("🔄 Promote Status\n\n" + msg)
            return

        if cmd == "/promote_now":
            if CYCLE.STATE.get("open_trade"):
                await event.reply("❌ Cannot promote while in open trade.")
                return
            ok = CYCLE.promote_universe(reason="MANUAL")
            await event.reply("✅ Promoted live→trading" if ok else ("❌ Promote blocked: " + (CYCLE.STATE.get("last_promote_msg") or "")))
            return

        # SLIPPAGE
        if cmd.startswith("/setslip "):
            v = _parse_float(cmd.split(maxsplit=1)[1])
            if v is None or v < 0:
                await event.reply("Usage: /setslip 0.30")
                return
            set_env_value("MAX_ENTRY_SLIPPAGE_PCT", str(v))
            CYCLE.set_runtime_param("MAX_ENTRY_SLIPPAGE_PCT", float(v))
            await event.reply("✅ MAX_ENTRY_SLIPPAGE_PCT set to %s (restart optional)" % v)
            return

        # INSIDER SAFETY
        if cmd == "/excluded":
            await event.reply(CYCLE.list_exclusions())
            return

        if cmd.startswith("/exclude "):
            sym = cmd.split(maxsplit=1)[1].strip().upper()
            await event.reply(CYCLE.exclude_symbol(sym))
            return

        if cmd.startswith("/include "):
            sym = cmd.split(maxsplit=1)[1].strip().upper()
            await event.reply(CYCLE.include_symbol(sym))
            return

        # EMERGENCY
        if cmd == "/panic":
            CYCLE.STATE["paused"] = True
            CYCLE.STATE["initiated"] = False
            CYCLE.STATE["live_override"] = False
            CYCLE._close_open_trade("PANIC")
            await event.reply("🛑 PANIC done: paused + disengaged + attempted close.")
            return

        if cmd == "/resetday":
            CYCLE.manual_reset_day()
            await event.reply("✅ Day reset done.")
            return

        # TOKEN FLOW
        if cmd == "/renewtoken":
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
            req_token = cmd.split(" ", 1)[1].strip()
            if not getattr(CFG, "KITE_API_KEY", ""):
                await event.reply("❌ KITE_API_KEY missing in .env")
                return
            if not getattr(CFG, "KITE_API_SECRET", ""):
                await event.reply("❌ KITE_API_SECRET missing in .env (add it and restart)")
                return

            try:
                kite = KiteConnect(api_key=CFG.KITE_API_KEY)
                data = kite.generate_session(req_token, api_secret=CFG.KITE_API_SECRET)
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
