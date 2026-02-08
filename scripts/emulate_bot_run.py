import datetime
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Lightweight stubs so emulation can run in restricted environments without pip installs.
if "pytz" not in sys.modules:
    pytz_stub = types.SimpleNamespace(timezone=lambda _name: datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    sys.modules["pytz"] = pytz_stub


if "dotenv" not in sys.modules:
    dotenv_stub = types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None)
    sys.modules["dotenv"] = dotenv_stub

if "kiteconnect" not in sys.modules:
    class _DummyKiteConnect:
        def __init__(self, *args, **kwargs):
            pass

    kiteconnect_stub = types.SimpleNamespace(KiteConnect=_DummyKiteConnect)
    sys.modules["kiteconnect"] = kiteconnect_stub

from zerodha_bot import trading_bot

BASE_DIR = ROOT / "zerodha_bot"


class FakeKite:
    VARIETY_REGULAR = "regular"
    EXCHANGE_NSE = "NSE"
    ORDER_TYPE_MARKET = "MARKET"
    PRODUCT_MIS = "MIS"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self):
        self._prices = {
            "NSE:RELIANCE": [100.0, 102.5, 103.0],
            "NSE:TCS": [50.0, 49.0, 48.5],
        }

    def margins(self, segment="equity"):
        return {"net": 10000.0}

    def ltp(self, instruments):
        out = {}
        for instrument in instruments:
            series = self._prices.get(instrument, [100.0])
            price = series.pop(0) if len(series) > 1 else series[0]
            self._prices[instrument] = series
            out[instrument] = {"last_price": price}
        return out

    def place_order(self, **kwargs):
        return f"FAKE-{kwargs['transaction_type']}-{kwargs['tradingsymbol']}-{kwargs['quantity']}"


def main():
    (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "predictions.json").write_text(json.dumps({"date": "demo", "stocks": ["RELIANCE", "TCS"]}))
    (BASE_DIR / "excluded.json").write_text(json.dumps({"stocks": ["TCS"]}))

    now_values = [
        datetime.datetime(2025, 1, 1, 10, 0, tzinfo=trading_bot.tz),
        datetime.datetime(2025, 1, 1, 10, 2, tzinfo=trading_bot.tz),
        datetime.datetime(2025, 1, 1, 10, 4, tzinfo=trading_bot.tz),
    ]

    def now_fn():
        if now_values:
            return now_values.pop(0)
        return datetime.datetime(2025, 1, 1, 10, 5, tzinfo=trading_bot.tz)

    trading_bot.run_trading_loop(
        kite=FakeKite(),
        poll_seconds=1,
        max_cycles=3,
        dry_run=True,
        now_fn=now_fn,
    )

    log_path = BASE_DIR / "logs" / "trades.jsonl"
    if log_path.exists():
        print("Emulation complete. Trades logged:")
        print(log_path.read_text())
    else:
        print("Emulation complete. No trades logged.")


if __name__ == "__main__":
    main()
