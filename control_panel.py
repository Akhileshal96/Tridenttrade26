"""Telegram inline-button control panel layer for Trident bot.

UI-only module: panel rendering + callback routing + safe handler dispatch.
"""

from telethon import events, Button


MAIN_TITLE = "⚡ **TRIDENT BOT CONTROL PANEL**"
RESEARCH_TITLE = "🌙 **RESEARCH PANEL**"
TOKEN_TITLE = "🔐 **TOKEN PANEL**"
LIVE_TITLE = "🛡 **LIVE SAFETY PANEL**"
EMERGENCY_TITLE = "🚨 **EMERGENCY PANEL**"
ADMIN_TITLE = "⚙ **ADMIN PANEL**"
HELP_TITLE = "🆘 **HELP PANEL**"
LOGS_TITLE = "📜 **LOGS PANEL**"
ANALYTICS_TITLE = "📊 **ANALYTICS PANEL**"


def _dedupe_rows(rows):
    seen = set()
    out = []
    for row in rows:
        new_row = []
        for btn in row:
            try:
                key = btn.data if isinstance(btn.data, bytes) else str(btn.data).encode()
            except Exception:
                key = str(getattr(btn, "text", "")).encode()
            if key in seen:
                continue
            seen.add(key)
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
        # Core runtime actions
        [Button.inline("▶ Start Loop", b"cp:cmd:startloop"), Button.inline("⏸ Stop Loop", b"cp:cmd:stoploop")],
        [Button.inline("📊 Status", b"cp:cmd:status"), Button.inline("📍 Positions", b"cp:cmd:positions")],
        [Button.inline(pnl_label, b"cp:cmd:pnlsofar"), Button.inline("📈 Trail", b"cp:cmd:trailstatus")],
        # Category panels
        [Button.inline("📊 Analytics", b"cp:panel:analytics"), Button.inline("🌙 Research", b"cp:panel:research")],
        [Button.inline("📜 Logs", b"cp:panel:logs"), Button.inline("🔐 Token", b"cp:panel:token")],
        [Button.inline("🛡 Live Safety", b"cp:panel:live"), Button.inline("🚨 Emergency", b"cp:panel:emergency")],
        [Button.inline("⚙ Admin", b"cp:panel:admin"), Button.inline("🆘 Help", b"cp:panel:help")],
    ]
    return _dedupe_rows(rows)


def _research_buttons():
    return [
        [Button.inline("🌃 Night Now", b"cp:cmd:nightnow"), Button.inline("📦 Universe", b"cp:cmd:universe")],
        [Button.inline("📡 Universe Live", b"cp:cmd:universe_live"), Button.inline("🧪 Night Report", b"cp:cmd:nightreport")],
        [Button.inline("📝 Night Log", b"cp:cmd:nightlog"), Button.inline("🔄 Promote Status", b"cp:cmd:promotestatus")],
        [Button.inline("🔬 Research", b"cp:cmd:research"), Button.inline("🌌 Universe Changes", b"cp:cmd:universechanges")],
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
    return [
        [Button.inline("✅ Initiate", b"cp:cmd:initiate"), Button.inline("⚡ Arm", b"cp:cmd:arm")],
        [Button.inline("🛑 Disengage", b"cp:cmd:disengage"), Button.inline("🔒 Disarm", b"cp:cmd:disarm")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


def _emergency_buttons():
    return [
        [Button.inline("🚨 Panic", b"cp:cmd:panic"), Button.inline("♻ Reset Day", b"cp:cmd:resetday")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


def _admin_buttons():
    return [
        [Button.inline("🆔 My ID", b"cp:cmd:myid"), Button.inline("🚫 Excluded Symbols", b"cp:cmd:excluded")],
        [Button.inline("👤 Add Trader", b"cp:hint:addtrader"), Button.inline("❌ Remove Trader", b"cp:hint:removetrader")],
        [Button.inline("👁 Add Viewer", b"cp:hint:addviewer"), Button.inline("🗑 Remove Viewer", b"cp:hint:removeviewer")],
        [Button.inline("⚙ Set Slip", b"cp:hint:setslip"), Button.inline("🚫 Exclude Symbol", b"cp:hint:exclude")],
        [Button.inline("✅ Include Symbol", b"cp:hint:include")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]



def _logs_buttons():
    return [
        [Button.inline("📅 Daily Log", b"cp:cmd:dailylog"), Button.inline("📜 Last 20", b"cp:cmd:logs20")],
        [Button.inline("🕘 Trading Hours", b"cp:cmd:tradinglog"), Button.inline("📜 Last 30", b"cp:cmd:logs30")],
        [Button.inline("📦 Export All", b"cp:cmd:exportlog")],
        [Button.inline("🧹 Reset Logs", b"cp:cmd:resetlogs")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]

def _help_buttons():
    return [
        [Button.inline("📘 Help", b"cp:cmd:help"), Button.inline("📋 Commands", b"cp:cmd:commands")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


def _analytics_buttons():
    return [
        [Button.inline("📌 Top 3", b"cp:cmd:top3"), Button.inline("🧠 Strategy Scores", b"cp:cmd:strategyscores")],
        [Button.inline("🌐 Regime", b"cp:cmd:regime"), Button.inline("🌐 Route Status", b"cp:cmd:routestatus")],
        [Button.inline("📊 Strategy Report", b"cp:cmd:strategyreport")],
        [Button.inline("🏆 Best Strategy", b"cp:cmd:beststrategy"), Button.inline("⚠ Worst Strategy", b"cp:cmd:worststrategy")],
        [Button.inline("📈 Regime Report", b"cp:cmd:regimereport"), Button.inline("🏭 Sector Report", b"cp:cmd:sectorreport")],
        [Button.inline("⬅ Back", b"cp:panel:main")],
    ]


_PANEL_MAP = {
    "main": (MAIN_TITLE, _main_buttons),
    "research": (RESEARCH_TITLE, _research_buttons),
    "token": (TOKEN_TITLE, _token_buttons),
    "live": (LIVE_TITLE, _live_buttons),
    "emergency": (EMERGENCY_TITLE, _emergency_buttons),
    "admin": (ADMIN_TITLE, _admin_buttons),
    "help": (HELP_TITLE, _help_buttons),
    "logs": (LOGS_TITLE, _logs_buttons),
    "analytics": (ANALYTICS_TITLE, _analytics_buttons),
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


def register_control_panel(client, handlers):
    async def _render_panel(event, panel_name: str, edit=False):
        title, button_fn = _PANEL_MAP.get(panel_name, _PANEL_MAP["main"])
        raw_btns = button_fn(handlers) if panel_name == "main" else button_fn()
        btns = _dedupe_rows(raw_btns)
        if edit:
            await event.edit(title, buttons=btns)
        else:
            await event.respond(title, buttons=btns)

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
