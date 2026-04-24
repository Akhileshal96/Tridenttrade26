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

# Runtime fingerprint for deployment verification.
RUNTIME_VERSION = _get_str("RUNTIME_VERSION", "2026.04.01-runtime-fix")
TELEGRAM_API_ID = _get_int("TELEGRAM_API_ID", "0")
TELEGRAM_API_HASH = _get_str("TELEGRAM_API_HASH", "")
ADMIN_USER_ID = _get_int("ADMIN_USER_ID", "0")

# ===== ZERODHA =====
KITE_API_KEY = _get_str("KITE_API_KEY", "")
KITE_API_SECRET = _get_str("KITE_API_SECRET", "")
KITE_ACCESS_TOKEN = _get_str("KITE_ACCESS_TOKEN", "")
KITE_LOGIN_URL = _get_str("KITE_LOGIN_URL", "")
KITE_STATIC_IP = _get_str("KITE_STATIC_IP", "")
KITE_IP_RECHECK_SEC = _get_int("KITE_IP_RECHECK_SEC", "180")
MARKET_PROTECTION = _get_float("MARKET_PROTECTION", "0.2")
ORDER_RATE_LIMIT_PER_SEC = _get_int("ORDER_RATE_LIMIT_PER_SEC", "10")

# ===== MODE & LOOP =====
IS_LIVE = _get_bool("IS_LIVE", "false")
# TRADING_MODE: INTRADAY (MIS only) | SWING (CNC longs only) | HYBRID (per-trade routing)
TRADING_MODE = _get_str("TRADING_MODE", "INTRADAY").upper()
# RISK_PROFILE: STANDARD (current safe behavior) | GOD (neutralizes bot-imposed soft caps
# but NEVER bypasses wallet/broker/affordability/market-protection/kill-switch)
RISK_PROFILE = _get_str("RISK_PROFILE", "STANDARD").upper()
TICK_SECONDS = _get_int("TICK_SECONDS", "20")

# ===== GOD PROFILE OVERRIDES =====
# Applied only when runtime risk_profile == "GOD". None of these bypass broker
# reality or hard safety (wallet check, market protection, daily kill switch).
GOD_MAX_EXPOSURE_PCT           = _get_float("GOD_MAX_EXPOSURE_PCT",           "95")
GOD_MAX_DEPLOYABLE_PCT         = _get_float("GOD_MAX_DEPLOYABLE_PCT",         "95")
GOD_MAX_SYMBOL_ALLOCATION_PCT  = _get_float("GOD_MAX_SYMBOL_ALLOCATION_PCT",  "40")
GOD_FULL_TIER_WEIGHT           = _get_float("GOD_FULL_TIER_WEIGHT",           "1.50")
GOD_REDUCED_TIER_WEIGHT        = _get_float("GOD_REDUCED_TIER_WEIGHT",        "1.20")
GOD_MICRO_TIER_WEIGHT          = _get_float("GOD_MICRO_TIER_WEIGHT",          "0.80")
GOD_WEAK_MARKET_SIZE_MULTIPLIER      = _get_float("GOD_WEAK_MARKET_SIZE_MULTIPLIER",      "0.90")
GOD_OPEN_MODERATE_SIZE_MULTIPLIER    = _get_float("GOD_OPEN_MODERATE_SIZE_MULTIPLIER",    "0.90")
GOD_OPEN_UNSAFE_SIZE_MULTIPLIER      = _get_float("GOD_OPEN_UNSAFE_SIZE_MULTIPLIER",      "0.60")
GOD_FALLBACK_SIZE_MULTIPLIER         = _get_float("GOD_FALLBACK_SIZE_MULTIPLIER",         "0.90")
# Bucket sizing — GOD gets larger per-trade allocation and higher ceiling
GOD_BUCKET_ALLOC_PCT                 = _get_float("GOD_BUCKET_ALLOC_PCT",                 "50")
GOD_BUCKET_CEIL_PCT                  = _get_float("GOD_BUCKET_CEIL_PCT",                  "70")
# More concurrent positions in GOD mode
GOD_MAX_CONCURRENT_TRADES            = _get_int(  "GOD_MAX_CONCURRENT_TRADES",            "50")
# Higher risk budget per trade
GOD_RISK_PER_TRADE_PCT               = _get_float("GOD_RISK_PER_TRADE_PCT",               "3.0")
# Re-entry cooldown: 5 min minimum even in GOD — prevents immediate re-entry into a
# stopped-out position before the setup has had time to resolve.
GOD_REENTRY_BLOCK_MINUTES            = _get_int(  "GOD_REENTRY_BLOCK_MINUTES",            "5")
# Profit drawdown guards relaxed (not removed) in GOD mode
GOD_DAY_PROFIT_GIVEBACK_HALT_PCT     = _get_float("GOD_DAY_PROFIT_GIVEBACK_HALT_PCT",     "75")
GOD_DAY_PROFIT_GIVEBACK_PAUSE_PCT    = _get_float("GOD_DAY_PROFIT_GIVEBACK_PAUSE_PCT",    "55")
GOD_DAY_PROFIT_GIVEBACK_REDUCE_PCT   = _get_float("GOD_DAY_PROFIT_GIVEBACK_REDUCE_PCT",   "35")
# Minimum peak profit (INR) before the giveback guard activates.
# Prevents tiny early gains from locking the bot for the rest of the day.
GOD_MIN_PEAK_FOR_GIVEBACK_INR        = _get_float("GOD_MIN_PEAK_FOR_GIVEBACK_INR",        "200")
MIN_PEAK_FOR_GIVEBACK_INR            = _get_float("MIN_PEAK_FOR_GIVEBACK_INR",            "150")
# Mean-reversion signals in SIDEWAYS with HTF_FAIL get MICRO size if score >= this.
# MR is counter-trend by design, so HTF alignment is less critical in SIDEWAYS.
MR_SIDEWAYS_HTF_FAIL_MIN_SCORE       = _get_float("MR_SIDEWAYS_HTF_FAIL_MIN_SCORE",       "25")

# ===== SIGNAL DATA =====
HIST_INTERVAL = _get_str("HIST_INTERVAL", "15minute")
HIST_DAYS = _get_int("HIST_DAYS", "10")

# ===== MARKET =====
EXCHANGE = "NSE"
PRODUCT = "MIS"

# ===== CAPITAL / RISK =====
USE_WALLET_BALANCE = _get_bool("USE_WALLET_BALANCE", "true")
CAPITAL_INR = _get_float("CAPITAL_INR", "1000")
RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", "2")
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
MAX_EXPOSURE_PCT = _get_float("MAX_EXPOSURE_PCT", "75")
USE_BUCKET_SLABS = _get_bool("USE_BUCKET_SLABS", "true")
MAX_DEPLOYABLE_PCT = _get_float("MAX_DEPLOYABLE_PCT", "75")
MAX_CONCURRENT_TRADES = _get_int("MAX_CONCURRENT_TRADES", "0")  # 0 = auto-scale with wallet size
FULL_TIER_WEIGHT = _get_float("FULL_TIER_WEIGHT", "1.25")
REDUCED_TIER_WEIGHT = _get_float("REDUCED_TIER_WEIGHT", "1.00")
MICRO_TIER_WEIGHT = _get_float("MICRO_TIER_WEIGHT", "0.60")
MAX_SYMBOL_ALLOCATION_PCT = _get_float("MAX_SYMBOL_ALLOCATION_PCT", "20")

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
DAILY_LOSS_CAP_INR = _get_float("DAILY_LOSS_CAP_INR", "300")
DAILY_PROFIT_TARGET_INR = _get_float("DAILY_PROFIT_TARGET_INR", "200")
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
CANDIDATE_SYMBOLS = _get_str("CANDIDATE_SYMBOLS", "")
AUTO_CANDIDATE_DISCOVERY = _get_bool("AUTO_CANDIDATE_DISCOVERY", "true")
CANDIDATE_DISCOVERY_MAX = _get_int("CANDIDATE_DISCOVERY_MAX", "300")
CANDIDATE_DISCOVERY_TARGET = _get_int("CANDIDATE_DISCOVERY_TARGET", "120")

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
NIGHT_START = _get_str("NIGHT_START", "23:00")
NIGHT_INTERVAL_MIN = _get_int("NIGHT_INTERVAL_MIN", 90)
NIGHT_END_OFFSET_MIN = _get_int("NIGHT_END_OFFSET_MIN", 5)


# ===== RESEARCH/UNIVERSE QUALITY =====
RESEARCH_UNIVERSE_SIZE = _get_int("RESEARCH_UNIVERSE_SIZE", "20")
SECTOR_MAX_IN_UNIVERSE = _get_int("SECTOR_MAX_IN_UNIVERSE", "3")
MARKET_REFRESH_MIN = _get_int("MARKET_REFRESH_MIN", "10")
INTRADAY_DYNAMIC_REFRESH = _get_bool("INTRADAY_DYNAMIC_REFRESH", "true")
INTRADAY_REFRESH_MAX_SWAPS = _get_int("INTRADAY_REFRESH_MAX_SWAPS", "3")
INTRADAY_HEAVY_REFRESH_MIN = _get_int("INTRADAY_HEAVY_REFRESH_MIN", "30")
UNIVERSE_CACHE_TTL_SEC = _get_int("UNIVERSE_CACHE_TTL_SEC", "600")
UNIVERSE_LOOKBACK_PERIOD = _get_str("UNIVERSE_LOOKBACK_PERIOD", "1y")
BLOCK_ON_UNKNOWN_MARKET_REGIME = _get_bool("BLOCK_ON_UNKNOWN_MARKET_REGIME", "false")
WEAK_MARKET_TOP_N = _get_int("WEAK_MARKET_TOP_N", "10")
WEAK_MARKET_MIN_SCORE = _get_float("WEAK_MARKET_MIN_SCORE", "0.75")
WEAK_MARKET_MIN_VOLUME_SCORE = _get_float("WEAK_MARKET_MIN_VOLUME_SCORE", "1.0")
WEAK_MARKET_SIZE_MULTIPLIER = _get_float("WEAK_MARKET_SIZE_MULTIPLIER", "0.5")
MARKET_WEAK_COOLDOWN_MIN = _get_int("MARKET_WEAK_COOLDOWN_MIN", "3")
ENABLE_SHORT_MODE = _get_bool("ENABLE_SHORT_MODE", "true")
MAX_SHORT_POSITIONS = _get_int("MAX_SHORT_POSITIONS", "2")
SHORT_SIZE_MULTIPLIER = _get_float("SHORT_SIZE_MULTIPLIER", "0.75")
SHORT_STOPLOSS_PCT = _get_float("SHORT_STOPLOSS_PCT", "1.2")
SHORT_MIN_VOLUME_SCORE = _get_float("SHORT_MIN_VOLUME_SCORE", "1.2")
USE_MTF_CONFIRMATION = _get_bool("USE_MTF_CONFIRMATION", "true")
HTF_INTERVAL = _get_str("HTF_INTERVAL", "60minute")
HTF_CONFIRM_MA = _get_int("HTF_CONFIRM_MA", "20")
HTF_CONFIRM_RSI = _get_bool("HTF_CONFIRM_RSI", "false")
HTF_LONG_MIN_RSI = _get_float("HTF_LONG_MIN_RSI", "52")
HTF_SHORT_MAX_RSI = _get_float("HTF_SHORT_MAX_RSI", "48")
# VWAP + EMA Strategy
VWAP_EMA_FAST = _get_int("VWAP_EMA_FAST", "9")
VWAP_EMA_SLOW = _get_int("VWAP_EMA_SLOW", "21")
VWAP_EMA_MIN_VOL_SCORE = _get_float("VWAP_EMA_MIN_VOL_SCORE", "1.5")
VWAP_EMA_MIN_SCORE = _get_float("VWAP_EMA_MIN_SCORE", "0.40")
SMA20_ENTRY_BUFFER_PCT = _get_float("SMA20_ENTRY_BUFFER_PCT", "0.1")
SMA20_ENTRY_BUFFER_ATR_MULT = _get_float("SMA20_ENTRY_BUFFER_ATR_MULT", "0.0")
VOLATILE_HTF_MIN_VOL_SCORE = _get_float("VOLATILE_HTF_MIN_VOL_SCORE", "1.2")
FALLBACK_TRIGGER_CYCLES = _get_int("FALLBACK_TRIGGER_CYCLES", "5")
FALLBACK_MIN_VOLUME_SCORE = _get_float("FALLBACK_MIN_VOLUME_SCORE", "1.2")
FALLBACK_TOP_N = _get_int("FALLBACK_TOP_N", "10")
FALLBACK_SIZE_MULTIPLIER = _get_float("FALLBACK_SIZE_MULTIPLIER", "0.5")
STRATEGY_MIN_ACTIVE_SCORE = _get_int("STRATEGY_MIN_ACTIVE_SCORE", "40")
STRATEGY_SELECTION_REFRESH_MINUTES = _get_int("STRATEGY_SELECTION_REFRESH_MINUTES", "10")
TOP3_DRY_CYCLE_THRESHOLD = _get_int("TOP3_DRY_CYCLE_THRESHOLD", "5")
SHORT_RS_MAX_VS_NIFTY = _get_float("SHORT_RS_MAX_VS_NIFTY", "-0.2")
LOSS_STREAK_HALT_THRESHOLD = _get_int("LOSS_STREAK_HALT_THRESHOLD", "4")
MIN_TRADES_FOR_ALLOCATION = _get_int("MIN_TRADES_FOR_ALLOCATION", "20")
STRATEGY_STATS_LOOKBACK_DAYS = _get_int("STRATEGY_STATS_LOOKBACK_DAYS", "90")
EXPECTANCY_FULL_SIZE = _get_float("EXPECTANCY_FULL_SIZE", "50")
EXPECTANCY_HALF_SIZE = _get_float("EXPECTANCY_HALF_SIZE", "10")
DISABLE_NEGATIVE_LAST_N = _get_int("DISABLE_NEGATIVE_LAST_N", "10")
USE_OPTIMAL_F = _get_bool("USE_OPTIMAL_F", "true")
OPTIMAL_F_FRACTION = _get_float("OPTIMAL_F_FRACTION", "0.25")
OPTIMAL_F_MAX_MULTIPLIER = _get_float("OPTIMAL_F_MAX_MULTIPLIER", "1.25")
OPTIMAL_F_MIN_MULTIPLIER = _get_float("OPTIMAL_F_MIN_MULTIPLIER", "0.25")
MIN_TRADES_FOR_OPTIMAL_F = _get_int("MIN_TRADES_FOR_OPTIMAL_F", "30")
EOD_REPORT_TIME = _get_str("EOD_REPORT_TIME", "15:16")
ACTIVE_UNIVERSE_SIZE = _get_int("ACTIVE_UNIVERSE_SIZE", "8")
ACTIVE_UNIVERSE_REFRESH_MINUTES = _get_int("ACTIVE_UNIVERSE_REFRESH_MINUTES", "10")
ACTIVE_UNIVERSE_EXPAND_CYCLES = _get_int("ACTIVE_UNIVERSE_EXPAND_CYCLES", "3")
USE_ADAPTIVE_OPEN_FILTER = _get_bool("USE_ADAPTIVE_OPEN_FILTER", "true")
OPEN_FILTER_START = _get_str("OPEN_FILTER_START", "09:15")
OPEN_FILTER_END = _get_str("OPEN_FILTER_END", "09:30")
MAX_SAFE_GAP_PCT = _get_float("MAX_SAFE_GAP_PCT", "0.8")
MAX_SAFE_FIRST_5M_RANGE_PCT = _get_float("MAX_SAFE_FIRST_5M_RANGE_PCT", "1.2")
OPEN_UNSAFE_SIZE_MULTIPLIER = _get_float("OPEN_UNSAFE_SIZE_MULTIPLIER", "0.25")
OPEN_UNSAFE_TOP_N = _get_int("OPEN_UNSAFE_TOP_N", "5")
OPEN_UNSAFE_MIN_SCORE = _get_float("OPEN_UNSAFE_MIN_SCORE", "0.90")
OPEN_MODERATE_TOP_N = _get_int("OPEN_MODERATE_TOP_N", "10")
OPEN_MODERATE_SIZE_MULTIPLIER = _get_float("OPEN_MODERATE_SIZE_MULTIPLIER", "0.5")
OPEN_MIN_TRADE_AFTER_NO_EXEC_CYCLES = _get_int("OPEN_MIN_TRADE_AFTER_NO_EXEC_CYCLES", "8")
OPEN_CLEAN_SIZE_MULTIPLIER = _get_float("OPEN_CLEAN_SIZE_MULTIPLIER", "1.0")

# ===== ADAPTIVE CONFIRMATION ENGINE =====
CONFIRM_WEIGHT_LTF = _get_int("CONFIRM_WEIGHT_LTF", "30")
CONFIRM_WEIGHT_HTF = _get_int("CONFIRM_WEIGHT_HTF", "25")
CONFIRM_WEIGHT_REGIME = _get_int("CONFIRM_WEIGHT_REGIME", "15")
CONFIRM_WEIGHT_RANK = _get_int("CONFIRM_WEIGHT_RANK", "15")
CONFIRM_WEIGHT_SECTOR = _get_int("CONFIRM_WEIGHT_SECTOR", "10")
CONFIRM_WEIGHT_VOLUME = _get_int("CONFIRM_WEIGHT_VOLUME", "5")

ENTRY_FULL_MIN_SCORE = _get_int("ENTRY_FULL_MIN_SCORE", "80")
ENTRY_REDUCED_MIN_SCORE = _get_int("ENTRY_REDUCED_MIN_SCORE", "60")
ENTRY_MICRO_MIN_SCORE = _get_int("ENTRY_MICRO_MIN_SCORE", "35")

ENTRY_FULL_SIZE_MULTIPLIER = _get_float("ENTRY_FULL_SIZE_MULTIPLIER", "1.0")
ENTRY_REDUCED_SIZE_MULTIPLIER = _get_float("ENTRY_REDUCED_SIZE_MULTIPLIER", "0.5")
ENTRY_MICRO_SIZE_MULTIPLIER = _get_float("ENTRY_MICRO_SIZE_MULTIPLIER", "0.25")
ENTRY_MICRO_TOP_N = _get_int("ENTRY_MICRO_TOP_N", "5")

OVERFILTER_SIGNAL_THRESHOLD = _get_int("OVERFILTER_SIGNAL_THRESHOLD", "8")
OVERFILTER_NO_ENTRY_CYCLES = _get_int("OVERFILTER_NO_ENTRY_CYCLES", "6")

MICRO_MODE_SIGNAL_THRESHOLD = _get_int("MICRO_MODE_SIGNAL_THRESHOLD", "5")
MICRO_MODE_LOOKBACK_MINUTES = _get_int("MICRO_MODE_LOOKBACK_MINUTES", "45")
MICRO_MODE_MAX_TRADES = _get_int("MICRO_MODE_MAX_TRADES", "2")
MICRO_MODE_SIZE_MULTIPLIER = _get_float("MICRO_MODE_SIZE_MULTIPLIER", "0.25")
MICRO_MODE_MIN_SCORE = _get_int("MICRO_MODE_MIN_SCORE", "35")

# ===== ATR-BASED ADAPTIVE STOPLOSS =====
USE_ATR_STOPLOSS = _get_bool("USE_ATR_STOPLOSS", "true")
ATR_STOPLOSS_MULT = _get_float("ATR_STOPLOSS_MULT", "1.5")
ATR_STOPLOSS_SHORT_MULT = _get_float("ATR_STOPLOSS_SHORT_MULT", "1.2")
ATR_STOPLOSS_MAX_PCT = _get_float("ATR_STOPLOSS_MAX_PCT", "4.0")

# ===== HIGH-PROFIT TRAIL LOCK =====
# When peak P&L >= TRAIL_HIGH_PROFIT_INR, lock LOCK_PCT and allow only GIVEBACK_PCT pullback.
TRAIL_HIGH_PROFIT_INR          = _get_float("TRAIL_HIGH_PROFIT_INR",          "100")
TRAIL_HIGH_PROFIT_LOCK_PCT     = _get_float("TRAIL_HIGH_PROFIT_LOCK_PCT",     "0.90")
TRAIL_HIGH_PROFIT_GIVEBACK_PCT = _get_float("TRAIL_HIGH_PROFIT_GIVEBACK_PCT", "0.10")

# ===== TRAIL RE-ENTRY =====
# After a TRAIL exit, allow re-entry if price moves favorably past exit price + buffer.
TRAIL_REENTRY_EXPIRE_MINUTES = _get_int(  "TRAIL_REENTRY_EXPIRE_MINUTES", "15")
TRAIL_REENTRY_BUFFER_PCT     = _get_float("TRAIL_REENTRY_BUFFER_PCT",     "0.2")

# ===== PER-TRADE PROFIT TARGET =====
# Hard exit at PROFIT_TARGET_R × risk (e.g. 2R = 2× the stoploss distance).
# Guarantees locking a portion of a big winner before a reversal can give it back.
USE_PROFIT_TARGET  = _get_bool( "USE_PROFIT_TARGET",  "true")
PROFIT_TARGET_R    = _get_float("PROFIT_TARGET_R",    "2.0")

# ===== TIME-DECAY EXIT =====
USE_TIME_DECAY_EXIT = _get_bool("USE_TIME_DECAY_EXIT", "true")
TIME_DECAY_MINUTES = _get_int("TIME_DECAY_MINUTES", "90")
TIME_DECAY_MAX_PNL_PCT = _get_float("TIME_DECAY_MAX_PNL_PCT", "0.3")
# Positions bleeding below this % after TIME_DECAY_MINUTES are also exited — slow
# bleeders that haven't hit SL but have clearly failed should not be held open.
TIME_DECAY_BLEED_FLOOR_PCT = _get_float("TIME_DECAY_BLEED_FLOOR_PCT", "-1.5")

# ===== ADX TREND STRENGTH FILTER =====
USE_ADX_FILTER = _get_bool("USE_ADX_FILTER", "true")
ADX_MIN_TREND = _get_float("ADX_MIN_TREND", "20.0")
