import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

import log_store as ls


def test_tail_trading_hours_today_filters_window(tmp_path, monkeypatch):
    log_path = tmp_path / "trident.log"
    day = "2026-03-17"
    log_path.write_text(
        "\n".join(
            [
                f"{day} 08:59:59 | INFO | [X] pre",
                f"{day} 09:15:00 | INFO | [X] open",
                f"{day} 10:00:00 | INFO | [X] mid",
                f"{day} 15:10:00 | INFO | [X] cutoff",
                f"{day} 15:30:01 | INFO | [X] post",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeDT:
        @classmethod
        def now(cls, _tz=None):
            class D:
                def strftime(self, _fmt):
                    return day

            return D()

    monkeypatch.setattr(ls, "LOG_FILE", str(log_path), raising=False)
    monkeypatch.setattr(ls, "datetime", FakeDT, raising=False)

    out = ls.tail_trading_hours_today("09:15", "15:10")
    assert "open" in out
    assert "mid" in out
    assert "cutoff" in out
    assert "pre" not in out
    assert "post" not in out


def test_tail_trading_hours_today_swaps_reversed_window(tmp_path, monkeypatch):
    log_path = tmp_path / "trident.log"
    day = "2026-03-17"
    log_path.write_text(
        "\n".join(
            [
                f"{day} 09:10:00 | INFO | [X] pre",
                f"{day} 09:20:00 | INFO | [X] include",
                f"{day} 15:20:00 | INFO | [X] include2",
                f"{day} 15:40:00 | INFO | [X] post",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeDT:
        @classmethod
        def now(cls, _tz=None):
            class D:
                def strftime(self, _fmt):
                    return day

            return D()

    monkeypatch.setattr(ls, "LOG_FILE", str(log_path), raising=False)
    monkeypatch.setattr(ls, "datetime", FakeDT, raising=False)

    out = ls.tail_trading_hours_today("15:30", "09:15")
    assert "include" in out
    assert "include2" in out
    assert "pre" not in out
    assert "post" not in out


def test_tail_trading_hours_today_invalid_window_uses_default(tmp_path, monkeypatch):
    log_path = tmp_path / "trident.log"
    day = "2026-03-17"
    log_path.write_text(
        "\n".join(
            [
                f"{day} 09:10:00 | INFO | [X] pre",
                f"{day} 09:20:00 | INFO | [X] include",
                f"{day} 15:20:00 | INFO | [X] include2",
                f"{day} 15:40:00 | INFO | [X] post",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeDT:
        @classmethod
        def now(cls, _tz=None):
            class D:
                def strftime(self, _fmt):
                    return day

            return D()

    monkeypatch.setattr(ls, "LOG_FILE", str(log_path), raising=False)
    monkeypatch.setattr(ls, "datetime", FakeDT, raising=False)

    out = ls.tail_trading_hours_today("99:99", "bad")
    assert "include" in out
    assert "include2" in out
    assert "pre" not in out
    assert "post" not in out
