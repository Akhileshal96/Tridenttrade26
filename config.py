import os
from dotenv import load_dotenv

load_dotenv()

def _get_bool(key: str, default="false") -> bool:
    # default might be bool/int/str depending on older code/patches
    d = default
    if isinstance(d, bool):
        d = "true" if d else "false"
    else:
        d = str(d)
    return os.getenv(key, d).strip().lower() == "true"

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

# ===== TRAILING / EXIT DEFAULTS =====
PROFIT_LOCK_ACTIVATE_PCT = _get_float("PROFIT_LOCK_ACTIVATE_PCT", "0.8")
TRAIL_PCT = _get_float("TRAIL_PCT", "0.4")
BUFFER_PCT = _get_float("BUFFER_PCT", "0.05")

# ===== ADAPTIVE TRAILING (INR-BASED) =====
MIN_TRAIL_ACTIVATE_INR = _get_float("MIN_TRAIL_ACTIVATE_INR", "8")
TRAIL_ACTIVATE_PCT_OF_POSITION = _get_float("TRAIL_ACTIVATE_PCT_OF_POSITION", "0.4")
TRAIL_LOCK_RATIO = _get_float("TRAIL_LOCK_RATIO", "0.5")
TRAIL_BUFFER_INR = _get_float("TRAIL_BUFFER_INR", "1")

# ===== BUCKET / EXPOSURE =====
BUCKET_MODE = _get_str("BUCKET_MODE", "PCT").upper()
BUCKET_PCT = _get_float("BUCKET_PCT", "10")
BUCKET_INR = _get_float("BUCKET_INR", "1000")
BUCKET_MIN_INR = _get_float("BUCKET_MIN_INR", "1000")
BUCKET_MAX_INR = _get_float("BUCKET_MAX_INR", "5000")
MAX_EXPOSURE_PCT = _get_float("MAX_EXPOSURE_PCT", "60")
USE_BUCKET_SLABS = _get_bool("USE_BUCKET_SLABS", "true")

# ===== ENTRY SAFETY =====
COOLDOWN_SECONDS = _get_int("COOLDOWN_SECONDS", "120")
REENTRY_BLOCK_MINUTES = _get_int("REENTRY_BLOCK_MINUTES", "30")
# ===== SLIPPAGE GUARD =====
MAX_ENTRY_SLIPPAGE_PCT = _get_float("MAX_ENTRY_SLIPPAGE_PCT", "0.30")

# ===== WALLET SYNC SAFETY =====
WALLET_SYNC_INTERVAL_SEC = _get_int("WALLET_SYNC_INTERVAL_SEC", "120")
WALLET_NIGHT_SYNC_INTERVAL_SEC = _get_int("WALLET_NIGHT_SYNC_INTERVAL_SEC", "900")
WALLET_SYNC_RETRIES = _get_int("WALLET_SYNC_RETRIES", "3")
WALLET_RETRY_BASE_SEC = _get_float("WALLET_RETRY_BASE_SEC", "1.5")

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

# ===== Trident Upgrade: Universe + Auto Promote + Night Scheduler =====
UNIVERSE_LIVE_PATH = _get_str("UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))
UNIVERSE_TRADING_PATH = _get_str("UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt"))

AUTO_PROMOTE_ENABLED = _get_bool("AUTO_PROMOTE_ENABLED", True)
PROMOTE_COOLDOWN_MIN = _get_int("PROMOTE_COOLDOWN_MIN", 60)
PROMOTE_WINDOWS = _get_str("PROMOTE_WINDOWS", "10:00-10:10,12:00-12:10,13:30-13:40")
PROMOTE_TOP10_OVERLAP_MIN = float(os.getenv("PROMOTE_TOP10_OVERLAP_MIN", "0.60"))
STABILITY_ATR_PCT_MAX = float(os.getenv("STABILITY_ATR_PCT_MAX", "0.35"))
STABILITY_SYMBOL = _get_str("STABILITY_SYMBOL", "NIFTYBEES")

NIGHT_AUTO_ENABLED = _get_bool("NIGHT_AUTO_ENABLED", True)
NIGHT_START = _get_str("NIGHT_START", "18:30")
NIGHT_INTERVAL_MIN = _get_int("NIGHT_INTERVAL_MIN", 90)
NIGHT_END_OFFSET_MIN = _get_int("NIGHT_END_OFFSET_MIN", 5)
