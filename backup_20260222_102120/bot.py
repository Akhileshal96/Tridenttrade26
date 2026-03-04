import asyncio
import os
from datetime import datetime

import config as CFG
import trading_cycle as CYCLE
import night_research

from telethon import TelegramClient, events
from kiteconnect import KiteConnect

from log_store import append_log, tail_text, export_all_txt, LOG_FILE
from env_utils import set_env_value
from broker_zerodha import get_kite


HELP_TEXT = (
    "🤖 TRIDENT BOT – COMMANDS\n\n"
    "TOKEN (Zerodha daily):\n"
    "• /renewtoken           → sends Zerodha login link\n"
    "• /token <request_token>→ generates access token + saves to .env\n"
    "   After /token, restart: sudo systemctl restart trident\n\n"
    "LIVE SAFETY:\n"
    "• /initiate (or /arm)     → enables LIVE immediately (runtime override)\n"
    "• /disengage (or /disarm) → stops LIVE immediately\n\n"
    "LOOP:\n"
    "• /startloop → start trading loop\n"
    "• /stoploop  → pause trading loop\n\n"
    "MONITOR:\n"
    "• /status    → status + daily caps\n"
    "• /logs      → last 20 log lines\n"
    "• /exportlog → full log as txt\n"
    "• /dailylog  → today's log as txt\n"
    "• /positions → Zerodha net positions\n\n"
    "RESEARCH:\n"
    "• /nightnow   → rebuild universe\n"
    "• /universe   → show current universe symbols\n"
    "• /nightlog   → show recent log lines (night)\n"
    "• /nightreport→ universe summary\n\n"
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

def _is_admin(event) -> bool:
    try:
        return event.is_private and int(event.sender_id) == int(CFG.ADMIN_USER_ID)
    except Exception:
        return False

def _parse_float(s: str):
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
    out_path = os.path.join(out_dir, f"trident_daily_{today}.txt")
    with open(LOG_FILE, "r") as f:
        lines = f.readlines()
    todays = [ln for ln in lines if ln.startswith(today)]
    if not todays:
        return None
    with open(out_path, "w") as f:
        f.writelines(todays)
    return out_path

def _safe_call(obj, fn_name: str, *args, **kwargs):
    fn = getattr(obj, fn_name, None)
    if callable(fn):
        return fn(*args, **kwargs)
    return None

async def main():
    # Telegram credentials: read from config if present, else fallback
    api_id = getattr(CFG, "TELEGRAM_API_ID", 9888950)
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

        # HELP
        if cmd in ("/help", "/commands"):
            await event.reply(HELP_TEXT)
            return

        # STATUS
        if cmd == "/status":
            try:
                await event.reply(CYCLE.get_status_text())
            except Exception:
                # Fallback if your trading_cycle doesn't have nice formatter
                await event.reply(f"Paused={CYCLE.STATE.get('paused')} Open={CYCLE.STATE.get('open_trade')}")
            return

        # LOOP CONTROLS
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
            # runtime override flags (safe even if cycle ignores them)
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
            await event.reply(tail_text(20))
            return

        if cmd == "/exportlog":
            fp = export_all_txt()
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
                        f"{p.get('exchange')}:{p.get('tradingsymbol')} "
                        f"qty={qty} avg={float(p.get('average_price') or 0.0):.2f} "
                        f"pnl={float(p.get('pnl') or 0.0):.2f}"
                    )
                await event.reply("📊 Net Positions\n\n" + ("\n".join(rows) if rows else "None"))
            except Exception as e:
                await event.reply(f"❌ Positions failed: {e}")
            return

        # RESEARCH
        if cmd == "/nightnow":
            await event.reply("🌙 Running Research…")
            try:
                await asyncio.to_thread(night_research.run_night_job)
                await event.reply("✅ Done. Use /universe or /nightreport.")
            except Exception as e:
                await event.reply(f"❌ Night research failed: {e}")
            return

        if cmd == "/universe":
            try:
                if not os.path.exists(CFG.UNIVERSE_PATH):
                    await event.reply("Universe not generated yet. Run /nightnow")
                    return
                with open(CFG.UNIVERSE_PATH, "r") as f:
                    syms = [l.strip() for l in f if l.strip()]
                if not syms:
                    await event.reply("Universe is empty.")
                    return
                msg = f"📊 Current Universe ({len(syms)} symbols)\n\n" + "\n".join(syms[:40])
                if len(syms) > 40:
                    msg += "\n…"
                await event.reply(msg)
            except Exception as e:
                await event.reply(f"❌ Failed: {e}")
            return

        if cmd == "/nightlog":
            logs = tail_text(120)
            await event.reply("🌙 Recent Logs\n\n" + (logs[-3500:] if logs else "(no logs)"))
            return

        if cmd == "/nightreport":
            try:
                if not os.path.exists(CFG.UNIVERSE_PATH):
                    await event.reply("Run /nightnow first.")
                    return
                with open(CFG.UNIVERSE_PATH, "r") as f:
                    syms = [l.strip() for l in f if l.strip()]
                msg = (
                    "🌙 Night Research Report\n\n"
                    f"Universe size: {len(syms)}\n\n"
                    "Top candidates:\n"
                    + ("\n".join(syms[:10]) if syms else "(none)")
                )
                await event.reply(msg)
            except Exception as e:
                await event.reply(f"❌ Failed: {e}")
            return

        # SLIPPAGE
        if cmd.startswith("/setslip "):
            v = _parse_float(cmd.split(maxsplit=1)[1])
            if v is None or v < 0:
                await event.reply("Usage: /setslip 0.30")
                return
            set_env_value("MAX_ENTRY_SLIPPAGE_PCT", str(v))
            # Optional runtime update if your cycle supports it
            _safe_call(CYCLE, "set_runtime_param", "MAX_ENTRY_SLIPPAGE_PCT", float(v))
            await event.reply(f"✅ MAX_ENTRY_SLIPPAGE_PCT set to {v}\nRestart recommended for full effect.")
            return

        # INSIDER SAFETY: delegate to trading_cycle if implemented
        if cmd == "/excluded":
            res = _safe_call(CYCLE, "list_exclusions")
            await event.reply(res if res else "No exclusions feature in trading_cycle.py yet.")
            return

        if cmd.startswith("/exclude "):
            sym = cmd.split(maxsplit=1)[1].strip().upper()
            res = _safe_call(CYCLE, "exclude_symbol", sym)
            await event.reply(res if res else "Exclusions not implemented in trading_cycle.py yet.")
            return

        if cmd.startswith("/include "):
            sym = cmd.split(maxsplit=1)[1].strip().upper()
            res = _safe_call(CYCLE, "include_symbol", sym)
            await event.reply(res if res else "Exclusions not implemented in trading_cycle.py yet.")
            return

        # EMERGENCY
        if cmd == "/panic":
            CYCLE.STATE["paused"] = True
            CYCLE.STATE["initiated"] = False
            CYCLE.STATE["live_override"] = False

            closed = _safe_call(CYCLE, "_close_open_trade", "PANIC")
            if closed is None:
                # Fallback: just clear trade marker
                CYCLE.STATE["open_trade"] = None

            await event.reply("🛑 PANIC done: paused + disengaged + attempted close.")
            return

        if cmd == "/resetday":
            ok = _safe_call(CYCLE, "manual_reset_day")
            if ok is None:
                # Fallback resets
                CYCLE.STATE["today_pnl"] = 0.0
                await event.reply("✅ Day reset done (fallback).")
            else:
                await event.reply("✅ Day reset done.")
            return

        # TOKEN LINK
        if cmd == "/renewtoken":
            if not getattr(CFG, "KITE_LOGIN_URL", ""):
                await event.reply("❌ KITE_LOGIN_URL missing in .env")
                return
            await event.reply(
                "🔑 Renew Zerodha Session\n\n"
                f"1) Open this link & login:\n{CFG.KITE_LOGIN_URL}\n\n"
                "2) After login, copy request_token from redirect URL\n"
                "(looks like: ...?request_token=XXXX&action=login)\n\n"
                "3) Send it here as:\n/token YOUR_REQUEST_TOKEN"
            )
            return

        # TOKEN EXCHANGE
        if cmd.startswith("/token "):
            req_token = cmd.split(" ", 1)[1].strip()
            if not getattr(CFG, "KITE_API_KEY", ""):
                await event.reply("❌ KITE_API_KEY missing in .env")
                return
            if not getattr(CFG, "KITE_API_SECRET", ""):
                await event.reply("❌ KITE_API_SECRET missing in .env (add it and restart)")
                return
            if not req_token:
                await event.reply("Usage: /token YOUR_REQUEST_TOKEN")
                return

            try:
                kite = KiteConnect(api_key=CFG.KITE_API_KEY)
                data = kite.generate_session(req_token, api_secret=CFG.KITE_API_SECRET)
                access = data["access_token"]
                set_env_value("KITE_ACCESS_TOKEN", access)
                await event.reply(
                    "✅ Access token updated in .env.\n\n"
                    "Now run on EC2:\n"
                    "sudo systemctl restart trident"
                )
            except Exception as e:
                await event.reply(f"❌ Token update failed: {e}")
            return

        await event.reply("Unknown command. Use /help")

    await asyncio.gather(
        client.run_until_disconnected(),
        asyncio.to_thread(CYCLE.run_loop_forever),
    )

if __name__ == "__main__":
    asyncio.run(main())
