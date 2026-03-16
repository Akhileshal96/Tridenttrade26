import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

import bot


class DummyEvent:
    def __init__(self):
        self.replies = []

    async def reply(self, message=None, **kwargs):
        if message is None:
            message = kwargs.get("message")
        self.replies.append(message)


class _FakeKiteClient:
    def profile(self):
        return {"user_id": "U123"}


class _FakeKiteConnect:
    def __init__(self, api_key):
        self.api_key = api_key

    def generate_session(self, request_token, api_secret):
        return {"access_token": f"tok_{request_token}_{api_secret}"}


def _patch_safe_runtime(monkeypatch):
    # role model for tests
    monkeypatch.setattr(bot, "_is_owner", lambda sid: int(sid) == 1001)
    monkeypatch.setattr(bot, "_is_trader", lambda sid: int(sid) in {1001, 2002})

    # side-effectful integrations
    monkeypatch.setattr(bot, "_update_id_list_env", lambda *a, **k: "1001,2002")
    monkeypatch.setattr(bot, "_restart_bot_process", lambda event: asyncio.sleep(0))
    monkeypatch.setattr(bot.os, "_exit", lambda code: None)
    monkeypatch.setattr(bot, "set_env_value", lambda *a, **k: None)
    monkeypatch.setattr(bot, "get_kite", lambda: _FakeKiteClient())
    monkeypatch.setattr(bot, "KiteConnect", _FakeKiteConnect)
    monkeypatch.setattr(bot, "run_nightly_maintenance", lambda *_a, **_k: None)

    # static outputs used by commands
    monkeypatch.setattr(bot, "tail_text", lambda n=20: "ok log")
    monkeypatch.setattr(bot, "export_all", lambda: None)
    monkeypatch.setattr(bot, "clear_logs", lambda: None)
    monkeypatch.setattr(bot, "_make_daily_log_file", lambda: None)

    monkeypatch.setattr(bot.CYCLE, "get_status_text", lambda: "status")
    monkeypatch.setattr(bot.CYCLE, "get_trailing_status_text", lambda: "trail")
    monkeypatch.setattr(bot.CYCLE, "get_positions_text", lambda: "positions")
    monkeypatch.setattr(bot.CYCLE, "load_universe_trading", lambda: ["ABC"])
    monkeypatch.setattr(bot.CYCLE, "load_universe_live", lambda: ["XYZ"])
    monkeypatch.setattr(bot.CYCLE, "promote_universe", lambda reason="MANUAL": True)
    monkeypatch.setattr(bot.CYCLE, "set_runtime_param", lambda *a, **k: None)
    monkeypatch.setattr(bot.CYCLE, "list_exclusions", lambda: "none")
    monkeypatch.setattr(bot.CYCLE, "exclude_symbol", lambda sym: f"excluded {sym}")
    monkeypatch.setattr(bot.CYCLE, "include_symbol", lambda sym: f"included {sym}")
    monkeypatch.setattr(bot.CYCLE, "_close_all_open_trades", lambda reason="PANIC": True)
    monkeypatch.setattr(bot.CYCLE, "manual_reset_day", lambda: None)

    monkeypatch.setenv("KITE_ACCESS_TOKEN", "present")
    monkeypatch.setenv("KITE_API_SECRET", "secret")
    monkeypatch.setattr(bot.CFG, "KITE_LOGIN_URL", "https://kite.example/login", raising=False)
    monkeypatch.setattr(bot.CFG, "KITE_API_KEY", "api_key", raising=False)

    bot.CYCLE.STATE["open_trades"] = {}


async def _run(cmd_word, cmd_arg, sender):
    ev = DummyEvent()
    handled = await bot._dispatch_command(ev, sender, cmd_word, cmd_arg)
    return handled, ev


def test_all_help_commands_are_dispatchable_for_expected_roles(monkeypatch):
    _patch_safe_runtime(monkeypatch)

    owner_commands = {
        "/addtrader": "123456",
        "/removetrader": "123456",
        "/addviewer": "123456",
        "/removeviewer": "123456",
        "/excluded": "",
        "/exclude": "SBIN",
        "/include": "SBIN",
        "/panic": "",
        "/resetday": "",
        "/renewtoken": "",
        "/tokenstatus": "",
        "/token": "request123",
        "/restart": "",
        "/initiate": "",
        "/disengage": "",
        "/resetlogs": "",
    }
    trader_commands = {
        "/startloop": "",
        "/stoploop": "",
        "/nightnow": "",
        "/promote_now": "",
        "/setslip": "0.3",
    }
    viewer_commands = {
        "/myid": "",
        "/help": "",
        "/commands": "",
        "/status": "",
        "/trailstatus": "",
        "/logs": "",
        "/logs20": "",
        "/logs30": "",
        "/exportlog": "",
        "/dailylog": "",
        "/positions": "",
        "/nightreport": "",
        "/nightlog": "",
        "/universe": "",
        "/universe_live": "",
        "/promotestatus": "",
    }

    for cmd, arg in owner_commands.items():
        handled, _ev = asyncio.run(_run(cmd, arg, 1001))
        assert handled is True, f"owner command not handled: {cmd}"

    for cmd, arg in trader_commands.items():
        handled, _ev = asyncio.run(_run(cmd, arg, 2002))
        assert handled is True, f"trader command not handled: {cmd}"

    for cmd, arg in viewer_commands.items():
        handled, _ev = asyncio.run(_run(cmd, arg, 3003))
        assert handled is True, f"viewer command not handled: {cmd}"


def test_owner_alias_commands_are_dispatchable(monkeypatch):
    _patch_safe_runtime(monkeypatch)

    for cmd in ("/arm", "/disarm"):
        handled, _ev = asyncio.run(_run(cmd, "", 1001))
        assert handled is True
