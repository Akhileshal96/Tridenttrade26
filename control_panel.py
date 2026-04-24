"""Telegram inline-button control panel layer for Trident bot.

UI-only module: panel rendering + callback routing + safe handler dispatch.
"""

from telethon import events, Button


MAIN_TITLE = "⚡ **TRIDENT BOT CONTROL PANEL**"
RESEARCH_TITLE = "🌙 **RESEARCH PANEL**"
TOKEN_TITLE = "🔐 **TOKEN PANEL**"
LIVE_TITLE = "🛡 **LIVE & SAFETY PANEL**"
ADMIN_TITLE = "⚙ **ADMIN PANEL**"
LOGS_TITLE = "📜 **LOGS PANEL**"
ANALYTICS_TITLE = "📊 **ANALYTICS PANEL**"


def _dedupe_rows(rows):
    seen_keys = set()
    seen_labels = set()
    out = []
    for row in rows:
        new_row = []
        for btn in row:
            try:
                key = btn.data if isinstance(btn.data, bytes) else str(btn.data).encode()
            except Exception:
                key = str(getattr(btn, "text", "")).encode()
            label = str(getattr(btn, "text", "")).strip().lower()
            if key in seen_keys or (label and label in seen_labels):
                continue
            seen_keys.add(key)
            if label:
                seen_labels.add(label)
            new_row.append(btn)
        if new_row:
            out.append(new_row)
    return out


def _main_buttons(handlers=None):
    pnl_label = "💰 P/L So Far"
    try:
        provider = (handlers or {}).get("__pnl_so_far_label__")
        if callable(provider):
            pnl_label = str(provider() or pnl_label)
    except Exception:
        pass
    rows = [
        # ── Session ──────────────────────────────────────────────────────────
        [Button.inline("▶ Start", b"cp:cmd:startloop"), Button.inline("⏸ Stop", b"cp:cmd:stoploop"), Button.inline("📟 Status", b"cp:cmd:status")],
        # ── Portfolio ────────────────────────────────────────────────────────
        [Button.inline(pnl_label, b"cp:cmd:pnlsofar"), Button.inline("📍 Positions", b"cp:cmd:positions")],
        [Button.inline("📦 Holdings", b"cp:cmd:holdings"), Button.inline("📈 Trail", b"cp:cmd:trailstatus")],
        # ── Trading Mode ─────────────────────────────────────────────────────
        [Button.inline("⚡ Intraday (MIS)", b"cp:cmd:mode_intraday"), Button.inline("🌙 Swing (CNC)", b"cp:cmd:mode_swing"), Button.inline("🧬 Hybrid", b"cp:cmd:mode_hybrid")],
        # ── Risk Profile ─────────────────────────────────────────────────────
        [Button.inline("🟢 Standard", b"cp:cmd:risk_standard"), Button.inline("🔥 God Mode", b"cp:cmd:risk_god")],
        [Button.inline("✅ Confirm God", b"cp:cmd:risk_god_confirm"), Button.inline("❌ Cancel God", b"cp:cmd:risk_god_cancel")],
        # ── Panels ───────────────────────────────────────────────────────────
        [Button.inline("📊 Analytics", b"cp:panel:analytics"), Button.inline("🌙 Research", b"cp:panel:research"), Button.inline("📜 Logs", b"cp:panel:logs")],
        [Button.inline("🔐 Token", b"cp:panel:token"), Button.inline("🛡 Live & Safety", b"cp:panel:live"), Button.inline("⚙ Admin", b"cp:panel:admin")],
        [Button.inline("❓ Help", b"cp:cmd:help")],
    ]
    return _dedupe_rows(rows)


def _research_buttons():
    return [
        [Button.inline("🌃 Night Now", b"cp:cmd:nightnow"), Button.inline("📦 Universe", b"cp:cmd:universe")],
        [Button.inline("📡 Universe Live", b"cp:cmd:universe_live"), Button.inline("🌌 Changes", b"cp:cmd:universechanges")],
        [Button.inline("🧪 Night Report", b"cp:cmd:nightreport"), Button.inline("📝 Night Log", b"cp:cmd:nightlog")],
        [Button.inline("🔬 Research", b"cp:cmd:research"), Button.inline("🔄 Promote Status", b"cp:cmd:promotestatus")],
        [Button.inline("⬆ Promote Now", b"cp:cmd:promote_now")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


def _token_buttons():
    return [
        [Button.inline("🔗 Renew Token", b"cp:cmd:renewtoken"), Button.inline("✅ Token Status", b"cp:cmd:tokenstatus")],
        [Button.inline("🔁 Restart Bot", b"cp:cmd:restart")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


def _live_buttons():
    """Merged Live Safety + Emergency into one panel."""
    return [
        [Button.inline("✅ Arm (Go Live)", b"cp:cmd:arm"), Button.inline("🛑 Disarm (Stop)", b"cp:cmd:disarm")],
        [Button.inline("🚨 Panic Close All", b"cp:cmd:panic"), Button.inline("♻ Reset Day", b"cp:cmd:resetday")],
        [Button.inline("🌐 IP Status", b"cp:cmd:ipstatus")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


def _admin_buttons():
    return [
        [Button.inline("🆔 My ID", b"cp:cmd:myid"), Button.inline("🚫 Exclusions", b"cp:cmd:excluded")],
        [Button.inline("👤 Add Trader", b"cp:hint:addtrader"), Button.inline("❌ Remove Trader", b"cp:hint:removetrader")],
        [Button.inline("👁 Add Viewer", b"cp:hint:addviewer"), Button.inline("🗑 Remove Viewer", b"cp:hint:removeviewer")],
        [Button.inline("🚫 Exclude", b"cp:hint:exclude"), Button.inline("✅ Include", b"cp:hint:include")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


def _logs_buttons():
    return [
        [Button.inline("📅 Daily", b"cp:cmd:dailylog"), Button.inline("🕘 Trading Hours", b"cp:cmd:tradinglog")],
        [Button.inline("📜 Last 20", b"cp:cmd:logs20"), Button.inline("📜 Last 30", b"cp:cmd:logs30")],
        [Button.inline("📦 Export All", b"cp:cmd:exportlog"), Button.inline("🧹 Reset", b"cp:cmd:resetlogs")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


def _analytics_buttons():
    return [
        [Button.inline("📌 Top 3", b"cp:cmd:top3"), Button.inline("🧠 Scores", b"cp:cmd:strategyscores"), Button.inline("📊 Strategy Report", b"cp:cmd:strategyreport")],
        [Button.inline("🏆 Best", b"cp:cmd:beststrategy"), Button.inline("⚠ Worst", b"cp:cmd:worststrategy")],
        [Button.inline("🌐 Regime", b"cp:cmd:regime"), Button.inline("🔀 Route", b"cp:cmd:routestatus")],
        [Button.inline("📈 Regime PnL", b"cp:cmd:regimereport"), Button.inline("🏭 Sector PnL", b"cp:cmd:sectorreport")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


def _analytics_panel_title(handlers):
    provider = (handlers or {}).get("__analytics_summary__")
    if callable(provider):
        try:
            body = provider()
            if body:
                return f"{ANALYTICS_TITLE}\n\n{body}"
        except Exception:
            pass
    return ANALYTICS_TITLE


_PANEL_MAP = {
    "main": (MAIN_TITLE, _main_buttons),
    "research": (RESEARCH_TITLE, _research_buttons),
    "token": (TOKEN_TITLE, _token_buttons),
    "live": (LIVE_TITLE, _live_buttons),
    "emergency": (LIVE_TITLE, _live_buttons),  # legacy alias
    "admin": (ADMIN_TITLE, _admin_buttons),
    "help": (MAIN_TITLE, _main_buttons),  # no separate help panel needed
    "logs": (LOGS_TITLE, _logs_buttons),
    "analytics": (_analytics_panel_title, _analytics_buttons),
}

_HINTS = {
    "addtrader": "Use: /addtrader <telegram_id>",
    "removetrader": "Use: /removetrader <telegram_id>",
    "addviewer": "Use: /addviewer <telegram_id>",
    "removeviewer": "Use: /removeviewer <telegram_id>",
    "setslip": "Use: /setslip 0.30",
    "exclude": "Use: /exclude SBIN",
    "include": "Use: /include SBIN",
}


async def _popup(event, text):
    try:
        await event.answer(text, alert=False)
    except Exception:
        pass


async def _safe_invoke(handler_name, event, handlers):
    fn = (handlers or {}).get(handler_name)
    if not fn:
        await _popup(event, f"Handler not configured: {handler_name}")
        return
    try:
        out = fn(event)
        if hasattr(out, "__await__"):
            await out
    except Exception as e:
        await _popup(event, f"Handler failed: {handler_name} ({e})")
        return
    try:
        await event.answer()
    except Exception:
        pass


def register_control_panel(client, handlers):
    async def _render_panel(event, panel_name: str, edit=False):
        title, button_fn = _PANEL_MAP.get(panel_name, _PANEL_MAP["main"])
        raw_btns = button_fn(handlers) if panel_name == "main" else button_fn()
        title_text = title(handlers) if callable(title) else title
        btns = _dedupe_rows(raw_btns)
        if edit:
            await event.edit(title_text, buttons=btns)
        else:
            await event.respond(title_text, buttons=btns)

    @client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def _start_panel(event):
        await _render_panel(event, "main", edit=False)

    @client.on(events.CallbackQuery())
    async def _callback_router(event):
        try:
            data = event.data.decode()
        except Exception:
            return
        if not data.startswith("cp:"):
            return

        parts = data.split(":", 2)
        if len(parts) < 3:
            await _popup(event, "Invalid control action")
            return
        kind, key = parts[1], parts[2]

        if kind == "panel":
            await _render_panel(event, key, edit=True)
            return

        if kind == "hint":
            await _popup(event, _HINTS.get(key, "Usage hint unavailable"))
            return

        if kind == "cmd":
            await _safe_invoke(key, event, handlers)
            return

        await _popup(event, "Unknown control action")
