import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

LOG_DIR = os.path.join(os.getcwd(), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "trident.log")

def _level():
    lvl = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, lvl, logging.INFO)

class ISTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

logger = logging.getLogger("Trident")
logger.setLevel(_level())

if not logger.handlers:
    fmt = ISTFormatter("%(asctime)s | %(levelname)s | %(message)s")

    # File handler
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(_level())
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler -> appears in `journalctl -u trident -f`
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(_level())
    ch.setFormatter(fmt)
    logger.addHandler(ch)

def append_log(level, tag, message):
    msg = f"[{tag}] {message}"
    level = (level or "INFO").upper()
    if level == "DEBUG":
        logger.debug(msg)
    elif level == "INFO":
        logger.info(msg)
    elif level in ("WARN", "WARNING"):
        logger.warning(msg)
    elif level == "ERROR":
        logger.error(msg)
    else:
        logger.info(msg)

def tail_text(n: int):
    if not os.path.exists(LOG_FILE):
        return "(no logs yet)"
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        return "".join(f.readlines()[-n:])

def export_all():
    return LOG_FILE

def tail_today():
    """Return today's log lines (IST) as text."""
    if not os.path.exists(LOG_FILE):
        return "(no logs yet)"
    today = datetime.now(IST).strftime("%Y-%m-%d")
    out = []
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            if ln.startswith(today):
                out.append(ln)
    return "".join(out) if out else "(no logs for today yet)"


def _parse_hhmm_to_minutes(value: str):
    """Parse HH:MM to absolute minutes (00:00 -> 0). Return None on invalid input."""
    try:
        hh_str, mm_str = str(value).strip().split(":", 1)
        hh = int(hh_str)
        mm = int(mm_str)
    except Exception:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def tail_trading_hours_today(start_hhmm: str = "09:15", end_hhmm: str = "15:30"):
    """Return today's log lines within trading hours (IST) as text."""
    if not os.path.exists(LOG_FILE):
        return "(no logs yet)"
    today = datetime.now(IST).strftime("%Y-%m-%d")
    smin = _parse_hhmm_to_minutes(start_hhmm)
    emin = _parse_hhmm_to_minutes(end_hhmm)
    if smin is None or emin is None:
        smin, emin = 9 * 60 + 15, 15 * 60 + 30
    elif smin > emin:
        smin, emin = emin, smin

    out = []
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            if not ln.startswith(today):
                continue
            # format: YYYY-MM-DD HH:MM:SS | LEVEL | [TAG] ...
            try:
                hh = int(ln[11:13])
                mm = int(ln[14:16])
                m = hh * 60 + mm
            except Exception:
                continue
            if smin <= m <= emin:
                out.append(ln)
    return "".join(out) if out else "(no trading-hour logs for today yet)"


def clear_logs():
    """Truncate main log file safely."""
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")
    return LOG_FILE
