import argparse
import datetime
import json
import logging
import os
import time
from pathlib import Path

import pytz
from kiteconnect import KiteConnect

from zerodha_bot.config import KITE_ACCESS_TOKEN, KITE_API_KEY

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

tz = pytz.timezone("Asia/Kolkata")


def create_kite_client() -> KiteConnect:
    kite = KiteConnect(api_key=KITE_API_KEY)
    if KITE_ACCESS_TOKEN:
        kite.set_access_token(KITE_ACCESS_TOKEN)
        return kite

    login_url = kite.login_url()
    print(f"Please login to Kite using this URL: {login_url}")
    logger.error("No access token found. Obtain token from login URL.")
    raise SystemExit(1)


def load_predictions() -> list[str]:
    predictions_file = BASE_DIR / "predictions.json"
    if not predictions_file.exists():
        return []
    try:
        with open(predictions_file, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
        return data.get("stocks", [])
    except Exception:
        return []


def load_excluded() -> list[str]:
    exclude_file = BASE_DIR / "excluded.json"
    if not exclude_file.exists():
        return []
    try:
        with open(exclude_file, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
        return data.get("stocks", [])
    except Exception:
        return []


def log_trade(action, symbol, quantity, price):
    entry = {
        "timestamp": datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "price": price,
    }
    with open(LOG_DIR / "trades.jsonl", "a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(entry) + "\n")


def place_market_order(kite, symbol: str, quantity: int, transaction_type: str, dry_run: bool = False) -> str:
    if dry_run:
        return f"DRYRUN-{transaction_type}-{symbol}-{quantity}"

    return kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NSE,
        tradingsymbol=symbol,
        transaction_type=transaction_type,
        quantity=quantity,
        order_type=kite.ORDER_TYPE_MARKET,
        product=kite.PRODUCT_MIS,
    )


def run_trading_loop(kite, poll_seconds: int = 30, max_cycles: int | None = None, dry_run: bool = False, now_fn=None):
    symbols_to_trade = load_predictions()
    excluded_stocks = set(load_excluded())

    logger.info("Symbols to trade (from predictions): %s", symbols_to_trade)
    logger.info("Excluded symbols: %s", list(excluded_stocks))

    profit_target = 0.02
    stop_loss = 0.01
    open_trades = {}

    if now_fn is None:
        now_fn = lambda: datetime.datetime.now(tz)

    cycle_count = 0
    logger.info("Starting trading loop...")

    while True:
        now = now_fn()
        current_time = now.time()

        if datetime.time(9, 30) <= current_time <= datetime.time(15, 15):
            try:
                margins = kite.margins(segment="equity")
                wallet = float(margins.get("net", 0))
            except Exception as exc:
                logger.error("Failed to fetch margins: %s", exc)
                wallet = 0.0

            for symbol in symbols_to_trade:
                if symbol in excluded_stocks or symbol in open_trades:
                    continue

                instrument = f"NSE:{symbol}"
                try:
                    quote = kite.ltp([instrument])
                    last_price = float(quote[instrument]["last_price"])
                except Exception as exc:
                    logger.error("Failed to fetch price for %s: %s", symbol, exc)
                    continue

                if wallet < last_price or last_price <= 0:
                    continue

                qty = int(wallet // last_price)
                if qty < 1:
                    continue

                try:
                    order_id = place_market_order(
                        kite=kite,
                        symbol=symbol,
                        quantity=qty,
                        transaction_type=kite.TRANSACTION_TYPE_BUY,
                        dry_run=dry_run,
                    )
                    logger.info("BUY %s: qty=%s at %s (Order ID: %s)", symbol, qty, last_price, order_id)
                    log_trade("BUY", symbol, qty, last_price)

                    open_trades[symbol] = {
                        "quantity": qty,
                        "buy_price": last_price,
                        "target": last_price * (1 + profit_target),
                        "stop": last_price * (1 - stop_loss),
                    }
                    wallet -= qty * last_price
                except Exception as exc:
                    logger.error("Error placing BUY order for %s: %s", symbol, exc)

            for symbol, trade_info in list(open_trades.items()):
                qty = trade_info["quantity"]
                target = trade_info["target"]
                stop = trade_info["stop"]

                instrument = f"NSE:{symbol}"
                try:
                    quote = kite.ltp([instrument])
                    last_price = float(quote[instrument]["last_price"])
                except Exception as exc:
                    logger.error("Failed to fetch price for %s (exit check): %s", symbol, exc)
                    continue

                should_exit = last_price >= target or last_price <= stop or current_time >= datetime.time(15, 10)
                if not should_exit:
                    continue

                try:
                    order_id = place_market_order(
                        kite=kite,
                        symbol=symbol,
                        quantity=qty,
                        transaction_type=kite.TRANSACTION_TYPE_SELL,
                        dry_run=dry_run,
                    )
                    logger.info("SELL %s: qty=%s at %s (Order ID: %s)", symbol, qty, last_price, order_id)
                    log_trade("SELL", symbol, qty, last_price)
                    open_trades.pop(symbol, None)
                except Exception as exc:
                    logger.error("Error placing SELL order for %s: %s", symbol, exc)

            time.sleep(max(1, poll_seconds))
        else:
            time.sleep(max(15, poll_seconds))

        cycle_count += 1
        if max_cycles is not None and cycle_count >= max_cycles:
            logger.info("Max cycles reached (%s), exiting trading loop", max_cycles)
            break


def parse_args():
    parser = argparse.ArgumentParser(description="Run Zerodha intraday trading bot")
    parser.add_argument("--dry-run", action="store_true", help="Do not place real orders")
    parser.add_argument("--poll-seconds", type=int, default=int(os.getenv("POLL_SECONDS", "30")), help="Polling interval in seconds")
    parser.add_argument("--max-cycles", type=int, default=None, help="Optional max loop cycles before exit")
    return parser.parse_args()


def main():
    args = parse_args()
    kite = create_kite_client()
    try:
        run_trading_loop(
            kite=kite,
            poll_seconds=max(5, args.poll_seconds),
            max_cycles=args.max_cycles,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        logger.info("Trading bot stopped by user.")
    except Exception as exc:
        logger.exception("Unexpected error in trading loop: %s", exc)


if __name__ == "__main__":
    main()
