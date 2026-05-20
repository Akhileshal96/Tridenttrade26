"""Audit fix (2026-05-21): Telegram client auto-reconnect wrapper.

Root cause:
  Telethon's internal _recv_loop dies with TypeNotFoundError when Telegram
  pushes a TLObject type the installed Telethon version can't parse
  (constructor 3ae56482 — unparseable even on the latest Telethon 1.43.2,
  and there is no Telethon 2.x). When _recv_loop dies,
  run_until_disconnected() returns. Under the old
  `asyncio.gather(client.run_until_disconnected(), trading_loop, ...)`
  the bot PROCESS kept running (trading-loop thread alive) but the Telegram
  interface was dead — user saw "bot stopped", had to manually restart.
  systemd never saw a crash (NRestarts stayed 0).

Fix:
  bot._telegram_client_forever(client) wraps run_until_disconnected in a
  reconnect-forever loop with bounded exponential backoff. A recv-loop death
  now only blips Telegram for a few seconds; the trading loop keeps running.

These tests exercise the wrapper's control flow with a mock client. We break
the infinite loop with a sentinel exception after a couple iterations.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.getcwd())


class _StopTest(BaseException):
    """Sentinel to break the wrapper's infinite while-loop in tests.
    Subclasses BaseException so the wrapper's `except Exception` doesn't
    swallow it."""


def _import_wrapper():
    """Import bot._telegram_client_forever without triggering bot startup.
    bot.py only runs main() under `if __name__ == '__main__'`, so import is safe.
    """
    import bot
    return bot._telegram_client_forever


# ----------------------------------------------------------------------------
# Mock client
# ----------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, disconnect_pattern):
        """disconnect_pattern: list of behaviors for successive
        run_until_disconnected() calls. Each item is either:
          - "return" → simulate a clean disconnect (returns)
          - an Exception instance → simulate recv-loop crash (raises)
        After the list is exhausted, raise _StopTest to end the test loop.
        """
        self._pattern = list(disconnect_pattern)
        self._call = 0
        self.connect_calls = 0
        self.run_calls = 0
        self._connected = True

    async def run_until_disconnected(self):
        self.run_calls += 1
        if self._call >= len(self._pattern):
            raise _StopTest()
        behavior = self._pattern[self._call]
        self._call += 1
        self._connected = False  # disconnected after the loop ends
        if isinstance(behavior, Exception):
            raise behavior
        # "return" → clean disconnect

    def is_connected(self):
        return self._connected

    async def connect(self):
        self.connect_calls += 1
        self._connected = True


async def _run_wrapper(client, monkeypatch):
    """Run the wrapper until it raises _StopTest. Patch asyncio.sleep to
    a no-op so the test doesn't actually wait through the backoff."""
    wrapper = _import_wrapper()

    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    with pytest.raises(_StopTest):
        await wrapper(client)


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

def test_reconnects_after_clean_disconnect(monkeypatch):
    """run_until_disconnected returns (clean disconnect) → wrapper reconnects."""
    client = _FakeClient(disconnect_pattern=["return"])
    asyncio.run(_run_wrapper(client, monkeypatch))
    # Wrapper should have attempted to reconnect at least once.
    assert client.connect_calls >= 1


def test_reconnects_after_recv_loop_crash(monkeypatch):
    """The actual bug: recv loop raises TypeNotFoundError-like exception.
    Wrapper must catch it and reconnect, NOT propagate it (which would kill
    the gather and the whole process's Telegram task)."""
    boom = Exception("TypeNotFoundError: constructor 3ae56482")
    client = _FakeClient(disconnect_pattern=[boom])
    asyncio.run(_run_wrapper(client, monkeypatch))
    assert client.connect_calls >= 1


def test_survives_multiple_consecutive_crashes(monkeypatch):
    """Three crashes in a row → three reconnect attempts, wrapper never dies."""
    crashes = [
        Exception("TypeNotFoundError 1"),
        Exception("TypeNotFoundError 2"),
        Exception("TypeNotFoundError 3"),
    ]
    client = _FakeClient(disconnect_pattern=crashes)
    asyncio.run(_run_wrapper(client, monkeypatch))
    assert client.connect_calls >= 3
    assert client.run_calls >= 3


def test_does_not_reconnect_when_already_connected(monkeypatch):
    """If the client reports still-connected after a clean return, the wrapper
    must not force a redundant connect() (avoids churning the session)."""
    wrapper = _import_wrapper()

    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    class _AlwaysConnected:
        def __init__(self):
            self.run_calls = 0
            self.connect_calls = 0
        async def run_until_disconnected(self):
            self.run_calls += 1
            if self.run_calls >= 2:
                raise _StopTest()
            # returns "cleanly" but stays connected
        def is_connected(self):
            return True
        async def connect(self):
            self.connect_calls += 1

    client = _AlwaysConnected()
    with pytest.raises(_StopTest):
        asyncio.run(wrapper(client))
    # is_connected() True → connect() should be skipped
    assert client.connect_calls == 0


def test_reconnect_failure_does_not_kill_wrapper(monkeypatch):
    """If connect() itself fails, the wrapper must keep looping (backoff),
    not propagate the error."""
    wrapper = _import_wrapper()

    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    class _ReconnectFails:
        def __init__(self):
            self.run_calls = 0
            self.connect_calls = 0
        async def run_until_disconnected(self):
            self.run_calls += 1
            if self.run_calls >= 3:
                raise _StopTest()
            raise Exception("recv crash")
        def is_connected(self):
            return False
        async def connect(self):
            self.connect_calls += 1
            raise Exception("network down, reconnect failed")

    client = _ReconnectFails()
    # Wrapper must survive connect() failures and keep trying until _StopTest.
    with pytest.raises(_StopTest):
        asyncio.run(wrapper(client))
    assert client.connect_calls >= 2  # kept trying despite failures
