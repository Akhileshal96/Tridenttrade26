import logging
import os
import datetime
import shutil

LOG_DIR = os.path.join(os.getcwd(), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "trident.log")

logger = logging.getLogger("Trident")
logger.setLevel(logging.INFO)

if not logger.handlers:
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
    logger.addHandler(fh)

def append_log(level, tag, message):
    msg = f"[{tag}] {message}"
    if level == "INFO":
        logger.info(msg)
    elif level == "WARN":
        logger.warning(msg)
    elif level == "ERROR":
        logger.error(msg)

def tail_text(n: int):
    if not os.path.exists(LOG_FILE):
        return "(no logs yet)"
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        return "".join(f.readlines()[-n:])

def export_all_txt():
    if not os.path.exists(LOG_FILE):
        return None
    txt_path = os.path.join(LOG_DIR, "trident_full.txt")
    shutil.copy(LOG_FILE, txt_path)
    return txt_path

def export_today_txt():
    if not os.path.exists(LOG_FILE):
        return None
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    txt_path = os.path.join(LOG_DIR, "trident_today.txt")
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as src, open(txt_path, "w", encoding="utf-8") as dst:
        for line in src:
            if line.startswith(today):
                dst.write(line)
    return txt_path
# --- PATCH V2 compatibility alias ---
def export_all():
    """
    Backward compatible export function.
    bot.py expects export_all().
    """
    try:
        return export_all_txt()
    except NameError:
        # If export_all_txt doesn't exist, fallback to raw LOG_FILE copy
        return LOG_FILE
