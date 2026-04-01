import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

import bot


class DummyEvent:
    def __init__(self):
        self.replies = []

    async def reply(self, message=None, **kwargs):
        if message is None:
            message = kwargs.get("message")
        self.replies.append(message)


def test_research_panel_buttons_are_mapped_to_handlers():
    cp = Path("control_panel.py").read_text(encoding="utf-8")
    bot_src = Path("bot.py").read_text(encoding="utf-8")

    # Restrict to _research_buttons block to validate research panel wiring.
    block = re.search(r"def _research_buttons\(\):\n(.*?)\n\n\ndef _token_buttons", cp, re.S)
    assert block, "_research_buttons block missing"
    research_cmds = set(re.findall(r"cp:cmd:([a-z_]+)", block.group(1)))

    pairs = re.findall(r'"([a-z_]+)": _mk_panel_handler\("([a-z_]+)"\)', bot_src)
    mapped = {k for k, v in pairs if k == v}

    missing = sorted(c for c in research_cmds if c not in mapped)
    assert not missing, f"Research panel commands missing handler wiring: {missing}"


async def _dispatch(sender, cmd, arg=""):
    ev = DummyEvent()
    handled = await bot._dispatch_command(ev, sender, cmd, arg)
    return handled, ev


def test_nightnow_forces_run_even_if_already_done_today(monkeypatch):
    monkeypatch.setattr(bot, "_is_trader", lambda _sid: True)

    called = {"force": None}

    def fake_run(state, force=False):
        called["force"] = force
        return None

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(bot, "run_nightly_maintenance", fake_run)
    monkeypatch.setattr(bot.asyncio, "to_thread", fake_to_thread)

    handled, ev = asyncio.run(_dispatch(2002, "/nightnow"))

    assert handled is True
    assert called["force"] is True
    assert ev.replies[0].startswith("🌙 Running night research now")
    assert "✅ Night research done." in ev.replies[-1]
