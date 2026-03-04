import os
from dotenv import load_dotenv

load_dotenv()

def _get_bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() == "true"

def _get_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def _get_int(key: str, default: str = "0") -> int:
    v = os.getenv(key, default)
    try:
        return int(str(v).strip())
    except Exception:
        return int(default)

def _get_float(key: str, default: str = "0") -> float:
    v = os.getenv(key, default)
    try:
        return float(str(v).strip())
    except Exception:
        return float(default)

# ===== TELEGRAM =====
TELEGRAM_BOT_TOKEN = _get_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_ID = _get_int("TELEGRAM_API_ID", "0")
TELEGRAM_API_HASH = _get_str("TELEGRAM_API_HASH", "")
ADMIN_USER_ID = _get_int("ADMIN_USER_ID", "0")

# ===== ZERODHA =====
KITE_API_KEY = _get_str("KITE_API_KEY", "")
KITE_API_SECRET = _get_str("KITE_API_SECRET", "")
KITE_ACCESS_TOKEN = _get_str("KITE_ACCESS_TOKEN", "")
KITE_LOGIN_URL = _get_str("KITE_LOGIN_URL", "")

# ===== MODE & LOOP =====
IS_LIVE = _get_bool("IS_LIVE", "false")
TICK_SECONDS = _get_int("TICK_SECONDS", "20")

# ===== SIGNAL DATA =====
HIST_INTERVAL = _get_str("HIST_INTERVAL", "15minute")
HIST_DAYS = _get_int("HIST_DAYS", "10")

# ===== MARKET =====
EXCHANGE = "NSE"
PRODUCT = "MIS"

# ===== CAPITAL / RISK =====
USE_WALLET_BALANCE = _get_bool("USE_WALLET_BALANCE", "true")
CAPITAL_INR = _get_float("CAPITAL_INR", "1000")
RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", "1")
STOPLOSS_PCT = _get_float("STOPLOSS_PCT", "2")

# ===== PROFIT LOCK =====
PROFIT_LOCK_ACTIVATE_PCT = _get_float("PROFIT_LOCK_ACTIVATE_PCT", "1.5")
PROFIT_LOCK_TRAIL_PCT = _get_float("PROFIT_LOCK_TRAIL_PCT", "2")
BREAKEVEN_BUFFER_PCT = _get_float("BREAKEVEN_BUFFER_PCT", "0.15")

# ===== SLIPPAGE GUARD =====
MAX_ENTRY_SLIPPAGE_PCT = _get_float("MAX_ENTRY_SLIPPAGE_PCT", "0.30")

# ===== DAILY GUARDS =====
AUTO_DAILY_GUARDS = _get_bool("AUTO_DAILY_GUARDS", "true")
DAILY_LOSS_CAP_INR = _get_float("DAILY_LOSS_CAP_INR", "200")
DAILY_PROFIT_TARGET_INR = _get_float("DAILY_PROFIT_TARGET_INR", "90")
DAILY_LOSS_R_MULT = _get_float("DAILY_LOSS_R_MULT", "3")
DAILY_PROFIT_R_MULT = _get_float("DAILY_PROFIT_R_MULT", "2")
DAILY_RESET_TIME = _get_str("DAILY_RESET_TIME", "09:00")

# ===== TIME & PATHS =====
ENTRY_START = _get_str("ENTRY_START", "09:20")
ENTRY_END = _get_str("ENTRY_END", "14:30")
FORCE_EXIT = _get_str("FORCE_EXIT", "15:10")
UNIVERSE_SIZE = _get_int("UNIVERSE_SIZE", "30")
UNIVERSE_PATH = _get_str("UNIVERSE_PATH", "./data/universe.txt")
CANDIDATES_PATH = _get_str("CANDIDATES_PATH", "./data/candidates.txt")

# ===== INSIDER SAFETY LIST =====
EXCLUSIONS_PATH = _get_str("EXCLUSIONS_PATH", "./data/exclusions.txt")
