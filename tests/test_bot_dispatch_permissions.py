import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

import bot


class DummyEvent:
    def __init__(self):
        self.messages = []

    async def reply(self, message=None, **kwargs):
        self.messages.append(message if message is not None else kwargs.get("message"))


def test_addtrader_non_owner_gets_permission_error(monkeypatch):
    ev = DummyEvent()
    monkeypatch.setattr(bot, "_is_owner", lambda _sid: False)

    handled = asyncio.run(bot._dispatch_command(ev, 2002, "/addtrader", "1234"))

    assert handled is True
    assert ev.messages
    assert "Not permitted" in ev.messages[-1]


def test_removeviewer_non_owner_gets_permission_error(monkeypatch):
    ev = DummyEvent()
    monkeypatch.setattr(bot, "_is_owner", lambda _sid: False)

    handled = asyncio.run(bot._dispatch_command(ev, 2002, "/removeviewer", "1234"))

    assert handled is True
    assert ev.messages
    assert "Not permitted" in ev.messages[-1]
