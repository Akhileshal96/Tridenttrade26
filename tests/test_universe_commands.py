import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

import bot


class DummyEvent:
    def __init__(self):
        self.replies = []

    async def reply(self, message=None, **kwargs):
        self.replies.append(message if message is not None else kwargs.get("message"))


async def _dispatch(sender, cmd, arg=""):
    ev = DummyEvent()
    handled = await bot._dispatch_command(ev, sender, cmd, arg)
    return handled, ev


def _patch_roles(monkeypatch):
    monkeypatch.setattr(bot, "_is_owner", lambda sid: int(sid) == 1001)
    monkeypatch.setattr(bot, "_is_trader", lambda sid: int(sid) in {1001, 2002})


def test_universe_commands_render_symbols(monkeypatch):
    _patch_roles(monkeypatch)
    monkeypatch.setattr(bot.CYCLE, "load_universe_trading", lambda: ["RELIANCE", "TCS"])
    monkeypatch.setattr(bot.CYCLE, "load_universe_live", lambda: ["INFY"])

    handled_t, ev_t = asyncio.run(_dispatch(3003, "/universe"))
    handled_l, ev_l = asyncio.run(_dispatch(3003, "/universe_live"))

    assert handled_t is True
    assert "TRADING Universe (2)" in ev_t.replies[-1]
    assert "RELIANCE" in ev_t.replies[-1]

    assert handled_l is True
    assert "LIVE Universe (1)" in ev_l.replies[-1]
    assert "INFY" in ev_l.replies[-1]


def test_promote_subcommands_status_and_guard(monkeypatch):
    _patch_roles(monkeypatch)
    bot.CYCLE.STATE["last_promote_msg"] = "LAST_OK"

    handled_s, ev_s = asyncio.run(_dispatch(3003, "/promotestatus"))
    assert handled_s is True
    assert "Last promote: LAST_OK" in ev_s.replies[-1]

    bot.CYCLE.STATE["open_trades"] = {"ABC": {"quantity": 1}}
    handled_block, ev_block = asyncio.run(_dispatch(2002, "/promote_now"))
    assert handled_block is True
    assert "Cannot promote while in open positions" in ev_block.replies[-1]


def test_promote_now_success_and_failure(monkeypatch):
    _patch_roles(monkeypatch)
    bot.CYCLE.STATE["open_trades"] = {}

    monkeypatch.setattr(bot.CYCLE, "promote_universe", lambda reason="MANUAL": True)
    handled_ok, ev_ok = asyncio.run(_dispatch(2002, "/promote_now"))
    assert handled_ok is True
    assert "Promoted live→trading" in ev_ok.replies[-1]

    bot.CYCLE.STATE["last_promote_msg"] = "flat-only"
    monkeypatch.setattr(bot.CYCLE, "promote_universe", lambda reason="MANUAL": False)
    handled_fail, ev_fail = asyncio.run(_dispatch(2002, "/promote_now"))
    assert handled_fail is True
    assert "Promote blocked: flat-only" in ev_fail.replies[-1]
