import asyncio
import os
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import config as CFG
import trading_cycle as CYCLE
from trading_cycle import STATE_LOCK
import night_research
from night_scheduler import run_nightly_maintenance
from kite_auto_login import auto_renew_kite_token

from telethon import TelegramClient, events
from kiteconnect import KiteConnect

from log_store import append_log, tail_text, export_all, LOG_FILE, clear_logs, tail_trading_hours_today
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
    "MODE [Trader/Owner]:\n"
    "• /mode           → show current trading mode\n"
    "• /mode intraday  → MIS orders, exit by 15:10, ₹20/order\n"
    "• /mode swing     → CNC longs only, hold overnight, ₹0 brokerage\n"
    "• /mode hybrid    → per-trade routing: intraday default + narrow swing lane\n\n"
    "RISK PROFILE [Trader/Owner]:\n"
    "• /riskprofile                → show current profile\n"
    "• /riskprofile standard       → safe default (current behaviour)\n"
    "• /riskprofile god            → request GOD (shows warning)\n"
    "• /riskprofile god confirm    → confirm + activate GOD\n"
    "• /riskprofile cancel         → cancel pending GOD confirmation\n\n"
    "MONITOR [Viewer+]:\n"
    "• /status     → status + daily caps\n"
    "• /pnl        → day P/L snapshot\n"
    "• /pnlsofar   → realized + unrealized + total P/L\n"
    "• /trailstatus → trailing lock details\n"
    "• /analytics  → runtime analytics panel\n"
    "• /research   → unified runtime research timeline\n"
    "• /universechanges → universe change timeline\n"
    "• /top3       → active top 3 strategies\n"
    "• /strategyscores → ranked strategy suitability\n"
    "• /regime     → regime/bias snapshot\n"
    "• /routestatus → route/top3 runtime state\n"
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
    "• /tradinglog → today's trading-hours log as txt\n"
    "• /resetlogs  → truncate all logs (Owner)\n"
    "• /positions  → Zerodha net positions\n\n"
    "• /ipstatus  → Kite static-IP compliance status\n\n"
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


def _make_trading_hour_log_file():
    out_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(out_dir, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = os.path.join(out_dir, f"trident_trading_hours_{today}.txt")
    txt = tail_trading_hours_today(
        start_hhmm=str(getattr(CFG, "OPEN_FILTER_START", "09:15") or "09:15"),
        end_hhmm=str(getattr(CFG, "FORCE_EXIT", "15:10") or "15:10"),
    )
    if not txt or txt.startswith("(no "):
        return None
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(txt)
    return out_path


def _tail_night_lines(n=120):
    txt = tail_text(n * 5)
    if not txt:
        return "(no logs)"
    lines = [ln for ln in txt.splitlines() if "[NIGHT]" in ln]
    return "\n".join(lines[-n:]) if lines else "(no NIGHT lines yet)"


def _latest_ops_summary(n=180):
    txt = tail_text(n) or ""
    if not txt:
        return "(no logs)"
    keep = []
    tags = ("[ENTRY]", "[EXIT]", "[ORDER]", "[FILL]", "[ROUTE]", "[UNIV]", "[UNIV_CHANGE]", "[RECON]", "[RISK]")
    for ln in txt.splitlines():
        if any(t in ln for t in tags):
            keep.append(ln)
    if not keep:
        return "(no execution/route/universe events yet)"
    return "\n".join(keep[-20:])


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


async def token_renewal_scheduler(client):
    """Auto-renew Kite access token daily via TOTP with retry.

    Schedule:
      - First attempt at 06:15 IST.
      - On failure: retry every 5 minutes until 08:30 IST (before market open).
      - On success: stop retrying for the day.
    Only runs if KITE_TOTP_SECRET, KITE_USER_ID, and KITE_PASSWORD are set.
    Falls back gracefully — sends Telegram alert on failure so manual /token
    is still available as a backup.
    """
    IST = ZoneInfo("Asia/Kolkata")
    required = ("KITE_TOTP_SECRET", "KITE_USER_ID", "KITE_PASSWORD")
    if not all(os.getenv(k, "").strip() for k in required):
        append_log("INFO", "AUTH", "TOTP auto-renewal disabled (KITE_TOTP_SECRET/USER_ID/PASSWORD not set)")
        return

    append_log("INFO", "AUTH", "TOTP token renewal scheduler active — runs daily 06:15-08:30 IST with retry")
    last_success_day = ""
    last_attempt_ts = 0.0  # epoch seconds of last attempt

    while True:
        try:
            now = datetime.now(IST)
            today = now.strftime("%Y-%m-%d")
            in_window = dtime(6, 15) <= now.time() <= dtime(8, 30)
            already_succeeded = last_success_day == today
            retry_cooldown_ok = (now.timestamp() - last_attempt_ts) >= 300  # 5 min between retries

            if in_window and not already_succeeded and retry_cooldown_ok:
                last_attempt_ts = now.timestamp()
                attempt_num = int(getattr(token_renewal_scheduler, '_attempts', 0)) + 1
                token_renewal_scheduler._attempts = attempt_num
                append_log("INFO", "AUTH", f"Starting TOTP auto token renewal (attempt {attempt_num})")
                ok, result = await asyncio.to_thread(auto_renew_kite_token)
                owner = int(os.getenv("OWNER_USER_ID", "0") or 0)
                if ok:
                    last_success_day = today
                    token_renewal_scheduler._attempts = 0
                    try:
                        margins = await asyncio.to_thread(lambda: get_kite().margins())
                        wallet = float((margins or {}).get("equity", {}).get("net") or 0.0)
                        msg = f"🔑 Kite token auto-renewed via TOTP ✅\nAttempt: {attempt_num}\nWallet: ₹{wallet:.2f}\nBot is ready for today's session."
                        append_log("INFO", "AUTH", f"TOTP renewal succeeded attempt={attempt_num} wallet={wallet:.2f}")
                    except Exception as _ve:
                        msg = f"🔑 Kite token auto-renewed via TOTP ✅\nAttempt: {attempt_num}\nBot is ready for today's session."
                        append_log("WARN", "AUTH", f"Post-renewal wallet fetch failed (token OK): {_ve}")
                else:
                    remaining_min = max(0, (dtime(8, 30).hour * 60 + dtime(8, 30).minute) - (now.hour * 60 + now.minute))
                    msg = (
                        f"⚠️ Kite token auto-renewal FAILED (attempt {attempt_num})\n\n"
                        f"Reason: {result}\n"
                        f"Retrying in 5 min (window closes in ~{remaining_min} min)\n\n"
                        f"If all retries fail, renew manually:\n"
                        f"1. Send /renewtoken\n"
                        f"2. Login and send /token <request_token>"
                    )
                    append_log("ERROR", "AUTH", f"TOTP renewal failed attempt={attempt_num}: {result}")
                if owner:
                    try:
                        await client.send_message(owner, msg)
                    except Exception as _e:
                        append_log("WARN", "AUTH", f"Could not send token renewal alert: {_e}")

            # Reset attempt counter on new day
            if today != getattr(token_renewal_scheduler, '_last_day', ''):
                token_renewal_scheduler._attempts = 0
                token_renewal_scheduler._last_day = today

            await asyncio.sleep(30)
        except Exception as e:
            append_log("ERROR", "AUTH", f"token_renewal_scheduler error: {e}")
            await asyncio.sleep(60)


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
    try:
        CYCLE._save_state_snapshot()
        append_log("INFO", "BOT", "State snapshot saved before process exit")
    except Exception as snap_err:
        append_log("WARN", "BOT", f"State snapshot failed before exit: {snap_err}")
    os._exit(0)


async def _dispatch_command(event, sender, cmd_word, cmd_arg):
    append_log("INFO", "BOT", f"command={cmd_word} sender={sender}")
    if cmd_word == "/myid":
        await event.reply(f"🆔 Your Telegram ID: `{int(sender)}`")
        return True

    if cmd_word in ("/help", "/commands"):
        await event.reply(HELP_TEXT)
        return True

    if cmd_word == "/status":
        await event.reply(CYCLE.get_status_text())
        return True

    if cmd_word == "/pnl":
        day_pnl = float(CYCLE.STATE.get("today_pnl") or 0.0)
        wallet = float(CYCLE.STATE.get("wallet_net_inr") or CYCLE.STATE.get("last_wallet") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
        pct = (day_pnl / wallet * 100.0) if wallet > 0 else 0.0
        icon = "🟢" if day_pnl >= 0 else "🔴"
        await event.reply(f"{icon} Day P/L: ₹{day_pnl:.2f} ({pct:+.2f}%)")
        return True
    if cmd_word == "/pnlsofar":
        await event.reply(CYCLE.get_pnl_so_far_text())
        return True

    if cmd_word == "/trailstatus":
        await event.reply(CYCLE.get_trailing_status_text())
        return True

    if cmd_word == "/top3":
        append_log("INFO", "BOT", "command=/top3")
        await event.reply(CYCLE.get_top3_text())
        return True

    if cmd_word == "/strategyscores":
        append_log("INFO", "BOT", "command=/strategyscores")
        await event.reply(CYCLE.get_strategy_scores_text())
        return True

    if cmd_word == "/regime":
        append_log("INFO", "BOT", "command=/regime")
        await event.reply(CYCLE.get_regime_text())
        return True

    if cmd_word == "/routestatus":
        append_log("INFO", "BOT", "command=/routestatus")
        await event.reply(CYCLE.get_route_status_text())
        return True
    if cmd_word == "/analytics":
        append_log("INFO", "BOT", "command=/analytics")
        await event.reply(CYCLE.get_analytics_text())
        return True
    if cmd_word == "/research":
        append_log("INFO", "BOT", "command=/research")
        await event.reply(CYCLE.get_research_text())
        return True
    if cmd_word == "/universechanges":
        append_log("INFO", "BOT", "command=/universechanges")
        await event.reply(CYCLE.get_universe_changes_text())
        return True

    if cmd_word == "/strategyreport":
        await event.reply(CYCLE.get_strategy_selector_text() + "\n\n" + SA.strategy_report_text())
        return True

    if cmd_word == "/beststrategy":
        try:
            best, _ = SA.best_worst_strategy()
            await event.reply(best)
        except Exception as e:
            append_log("WARN", "BOT", f"beststrategy failed: {e}")
            await event.reply("🏆 Best Strategy\n\nNo strategy stats yet.")
        return True

    if cmd_word == "/worststrategy":
        try:
            _, worst = SA.best_worst_strategy()
            await event.reply(worst)
        except Exception as e:
            append_log("WARN", "BOT", f"worststrategy failed: {e}")
            await event.reply("⚠️ Worst Strategy\n\nNo strategy stats yet.")
        return True

    if cmd_word == "/regimereport":
        try:
            await event.reply(SA.regime_report_text())
        except Exception as e:
            append_log("WARN", "BOT", f"regimereport failed: {e}")
            await event.reply("📈 Regime Report\n\nNo regime stats yet.")
        return True

    if cmd_word == "/sectorreport":
        try:
            await event.reply(SA.sector_report_text())
        except Exception as e:
            append_log("WARN", "BOT", f"sectorreport failed: {e}")
            await event.reply("🏭 Sector Report\n\nNo sector stats yet.")
        return True

    if cmd_word == "/ipstatus":
        await event.reply(CYCLE.get_ip_status_text())
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
        with CYCLE.STATE_LOCK:
            CYCLE.STATE["paused"] = False
        await event.reply("▶️ Loop Started")
        return True

    if cmd_word == "/stoploop":
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        with CYCLE.STATE_LOCK:
            CYCLE.STATE["paused"] = True
        await event.reply("⏸️ Loop Paused")
        return True

    # ===== Trading Mode Switch (button + command) =====
    if cmd_word in ("/mode_intraday", "/mode_swing", "/mode_hybrid"):
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        if "intraday" in cmd_word:
            cmd_arg = "INTRADAY"
        elif "hybrid" in cmd_word:
            cmd_arg = "HYBRID"
        else:
            cmd_arg = "SWING"
        # Fall through to /mode handler below

    if cmd_word == "/mode" or cmd_word in ("/mode_intraday", "/mode_swing", "/mode_hybrid"):
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        arg = str(cmd_arg or "").strip().upper()
        if arg in ("INTRADAY", "MIS"):
            ok, norm = CYCLE.set_trading_mode("INTRADAY")
            await event.reply(
                "📊 Mode: **INTRADAY** (MIS)\n"
                "• Orders: MIS (margin intraday)\n"
                "• Auto exit: 15:10\n"
                "• Brokerage: ₹20/order\n"
                "• Stoploss: 2%"
            )
        elif arg in ("SWING", "CNC", "DELIVERY"):
            ok, norm = CYCLE.set_trading_mode("SWING")
            await event.reply(
                "📈 Mode: **SWING** (CNC)\n"
                "• Longs only → CNC delivery, hold overnight, ₹0 brokerage\n"
                "• Shorts → remain INTRADAY/MIS (never routed to swing)\n"
                "• Force-exit at 15:10 skipped for CNC positions"
            )
        elif arg in ("HYBRID", "HYB"):
            ok, norm = CYCLE.set_trading_mode("HYBRID")
            await event.reply(
                "🧬 Mode: **HYBRID**\n"
                "• Intraday engine preserved (default routing = MIS)\n"
                "• Narrow swing lane for strongest long continuation trades:\n"
                "  strategy=`mtf_confirmed_long`, tier=FULL,\n"
                "  regime=TRENDING_UP/TRENDING, trend_direction=UP\n"
                "• All shorts remain INTRADAY/MIS\n"
                "• Weak-market / MR / fallback trades stay INTRADAY"
            )
        else:
            current = CYCLE.current_trading_mode()
            await event.reply(
                f"Current mode: **{current}**\n\n"
                "Usage:\n"
                "`/mode intraday` — MIS, exit by 15:10, ₹20/order\n"
                "`/mode swing`    — CNC longs only, hold overnight, ₹0 brokerage\n"
                "`/mode hybrid`   — per-trade routing (intraday + narrow swing lane)"
            )
        return True

    # ===== Risk Profile (STANDARD / GOD with confirmation) =====
    if cmd_word in ("/riskprofile_standard", "/riskprofile_god", "/riskprofile_god_confirm", "/riskprofile_god_cancel"):
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        if cmd_word == "/riskprofile_standard":
            cmd_arg = "standard"
        elif cmd_word == "/riskprofile_god":
            cmd_arg = "god"
        elif cmd_word == "/riskprofile_god_confirm":
            cmd_arg = "god confirm"
        else:
            cmd_arg = "cancel"
        # Fall through to /riskprofile handler

    if cmd_word == "/riskprofile" or cmd_word in (
        "/riskprofile_standard", "/riskprofile_god",
        "/riskprofile_god_confirm", "/riskprofile_god_cancel",
    ):
        if not _is_trader(sender):
            await event.reply("❌ Not permitted (Trader/Owner only).")
            return True
        raw = str(cmd_arg or "").strip().lower()
        if raw in ("cancel", "god_cancel", "abort"):
            cleared = CYCLE.cancel_god_confirmation()
            msg = "🟢 GOD confirmation cancelled — profile unchanged." if cleared else "ℹ️ No pending GOD confirmation."
            cur = CYCLE.current_risk_profile()
            await event.reply(f"{msg}\nCurrent risk profile: **{cur}**")
            return True
        if raw in ("standard", "safe", "default"):
            ok, norm = CYCLE.set_risk_profile("STANDARD")
            await event.reply(
                "🟢 Risk Profile: **STANDARD**\n"
                "• Preserves current wallet/exposure/symbol caps and sizing.\n"
                "• Any pending GOD confirmation cleared."
            )
            return True
        if raw in ("god confirm", "god_confirm", "confirm god", "confirm"):
            ok, norm = CYCLE.confirm_god_mode()
            if ok:
                await event.reply(
                    "🔥 Risk Profile: **GOD** (ACTIVATED)\n"
                    "• Bot-imposed soft caps neutralized (deployable %, exposure %, per-symbol %, tier weights).\n"
                    "• Still enforced: wallet_available, broker margin / CNC affordability, market protection, panic, daily loss kill switch.\n"
                    "• To disable: `/riskprofile standard`"
                )
            else:
                await event.reply(
                    "⚠️ No pending GOD confirmation.\n"
                    "Run `/riskprofile god` first, then `/riskprofile god confirm`."
                )
            return True
        if raw in ("god",):
            warning = CYCLE.request_god_confirmation()
            await event.reply(warning)
            return True
        # bare /riskprofile → show current
        cur = CYCLE.current_risk_profile()
        pending = CYCLE.STATE.get("pending_risk_profile_confirmation")
        pending_note = f"\nPending confirmation: **{pending}**" if pending else ""
        await event.reply(
            f"Current risk profile: **{cur}**{pending_note}\n\n"
            "Usage:\n"
            "`/riskprofile standard`         — safe default (current behaviour)\n"
            "`/riskprofile god`              — request GOD activation (shows warning)\n"
            "`/riskprofile god confirm`      — confirm + activate GOD\n"
            "`/riskprofile cancel`           — cancel a pending GOD confirmation"
        )
        return True

    # ===== Owner-only LIVE safety =====
    if cmd_word in ("/initiate", "/arm"):
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        with CYCLE.STATE_LOCK:
            CYCLE.STATE["initiated"] = True
            CYCLE.STATE["live_override"] = True
        ok, msg = CYCLE.request_live_rearm()
        if ok:
            await event.reply("🟢 LIVE INITIATED (runtime override enabled). Use /disengage to stop.")
        else:
            await event.reply(f"🟠 LIVE initiate set, but order placement remains blocked.\n{msg}")
        return True

    if cmd_word in ("/disengage", "/disarm"):
        if not _is_owner(sender):
            await event.reply("❌ Not permitted (Owner only).")
            return True
        with CYCLE.STATE_LOCK:
            CYCLE.STATE["initiated"] = False
            CYCLE.STATE["live_override"] = False
        await event.reply("🔴 DISENGAGED (runtime override disabled). Orders blocked.")
        return True

    # ===== Logs (Viewer+) =====
    if cmd_word in ("/logs", "/log"):
        ops = _latest_ops_summary()
        await event.reply(
            "📜 Log Options + Latest Ops\n\n"
            f"{ops}\n\n"
            "• /dailylog -> today's logs\n"
            "• /logs20 -> last 20 lines\n"
            "• /logs30 -> last 30 lines\n"
            "• /exportlog -> full log file\n"
            "• /tradinglog -> trading-hours logs\n"
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

    if cmd_word == "/tradinglog":
        fp = _make_trading_hour_log_file()
        if not fp or not os.path.exists(fp):
            await event.reply("(no trading-hour logs for today)")
            return True
        await event.reply(file=fp, message="🕘 Trading-hours log export")
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
        with CYCLE.STATE_LOCK:
            CYCLE.STATE["paused"] = True
        close_ok = CYCLE._close_all_open_trades("PANIC")
        with CYCLE.STATE_LOCK:
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
            from broker_zerodha import invalidate_kite
            invalidate_kite()
            await event.reply("✅ Token updated. Restarting bot.")
            append_log("INFO", "BOT", "Token updated; restarting bot process")
            try:
                CYCLE._save_state_snapshot()
            except Exception:
                pass
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
            sender = int(getattr(message_event, "sender_id", 0) or 0)
            dispatch_event = message_event
            if not hasattr(message_event, "reply") and hasattr(message_event, "respond"):
                class _PanelDispatchEvent:
                    def __init__(self, ev):
                        self._ev = ev
                        self.sender_id = getattr(ev, "sender_id", None)

                    async def reply(self, text):
                        return await self._ev.respond(text)

                dispatch_event = _PanelDispatchEvent(message_event)
            await _dispatch_command(dispatch_event, sender, f"/{command_name}", "")
        return _run

    def _pnl_so_far_button_label():
        day_pnl = float(CYCLE.STATE.get("today_pnl") or 0.0)
        wallet = float(CYCLE.STATE.get("wallet_net_inr") or CYCLE.STATE.get("last_wallet") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
        pct = (day_pnl / wallet * 100.0) if wallet > 0 else 0.0
        icon = "🟢" if pct >= 0 else "🔴"
        return f"{icon} P/L So Far {pct:+.2f}%"

    def _analytics_summary():
        txt = CYCLE.get_analytics_text()
        parts = txt.split("\n", 2)
        return parts[2] if len(parts) > 2 else txt

    panel_handlers = {
        "__pnl_so_far_label__": _pnl_so_far_button_label,
        "__analytics_summary__": _analytics_summary,
        "myid": _mk_panel_handler("myid"),
        "help": _mk_panel_handler("help"),
        "commands": _mk_panel_handler("commands"),
        "startloop": _mk_panel_handler("startloop"),
        "stoploop": _mk_panel_handler("stoploop"),
        "status": _mk_panel_handler("status"),
        "pnl": _mk_panel_handler("pnl"),
        "pnlsofar": _mk_panel_handler("pnlsofar"),
        "trailstatus": _mk_panel_handler("trailstatus"),
        "analytics": _mk_panel_handler("analytics"),
        "research": _mk_panel_handler("research"),
        "universechanges": _mk_panel_handler("universechanges"),
        "top3": _mk_panel_handler("top3"),
        "strategyscores": _mk_panel_handler("strategyscores"),
        "regime": _mk_panel_handler("regime"),
        "routestatus": _mk_panel_handler("routestatus"),
        "strategyreport": _mk_panel_handler("strategyreport"),
        "beststrategy": _mk_panel_handler("beststrategy"),
        "worststrategy": _mk_panel_handler("worststrategy"),
        "regimereport": _mk_panel_handler("regimereport"),
        "sectorreport": _mk_panel_handler("sectorreport"),
        "ipstatus": _mk_panel_handler("ipstatus"),
        "logs": _mk_panel_handler("logs"),
        "logs20": _mk_panel_handler("logs20"),
        "logs30": _mk_panel_handler("logs30"),
        "dailylog": _mk_panel_handler("dailylog"),
        "tradinglog": _mk_panel_handler("tradinglog"),
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
        "mode_intraday": _mk_panel_handler("mode_intraday"),
        "mode_swing": _mk_panel_handler("mode_swing"),
        "mode_hybrid": _mk_panel_handler("mode_hybrid"),
        "risk_standard": _mk_panel_handler("riskprofile_standard"),
        "risk_god": _mk_panel_handler("riskprofile_god"),
        "risk_god_confirm": _mk_panel_handler("riskprofile_god_confirm"),
        "risk_god_cancel": _mk_panel_handler("riskprofile_god_cancel"),
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

        try:
            handled = await _dispatch_command(event, sender, cmd_word, cmd_arg)
        except Exception as e:
            append_log("ERROR", "BOT", f"command_failed={cmd_word} err={e}")
            await event.reply(f"❌ Command failed: {cmd_word}\n{e}")
            return
        if not handled:
            await event.reply("Unknown command. Use /help")


    await asyncio.gather(
        client.run_until_disconnected(),
        asyncio.to_thread(CYCLE.run_loop_forever),
        night_scheduler(),
        token_renewal_scheduler(client),
    )


if __name__ == "__main__":
    asyncio.run(main())
