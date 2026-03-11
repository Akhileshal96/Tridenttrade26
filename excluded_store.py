import os
from typing import Set

import config as CFG

# Keep exclusions file aligned with trading_cycle/night_research defaults.
EXCL_PATH = getattr(CFG, "EXCLUSIONS_PATH", os.path.join(os.getcwd(), "data", "exclusions.txt"))
LEGACY_EXCL_PATH = os.path.join(os.getcwd(), "data", "excluded.txt")
os.makedirs(os.path.dirname(EXCL_PATH), exist_ok=True)


def _migrate_legacy_if_needed() -> None:
    """
    One-time migration: if legacy excluded.txt exists and new exclusions.txt does not,
    copy legacy contents so callers stay in sync.
    """
    if EXCL_PATH == LEGACY_EXCL_PATH:
        return
    if os.path.exists(EXCL_PATH):
        return
    if not os.path.exists(LEGACY_EXCL_PATH):
        return
    try:
        with open(LEGACY_EXCL_PATH, "r", encoding="utf-8", errors="ignore") as src:
            data = src.read()
        with open(EXCL_PATH, "w", encoding="utf-8") as dst:
            dst.write(data)
    except Exception:
        # Non-fatal: callers will still proceed with default behavior.
        pass


def load_excluded() -> Set[str]:
    _migrate_legacy_if_needed()
    if not os.path.exists(EXCL_PATH):
        return set()
    with open(EXCL_PATH, "r", encoding="utf-8", errors="ignore") as f:
        return {ln.strip().upper() for ln in f.read().splitlines() if ln.strip() and not ln.strip().startswith("#")}


def save_excluded(items: Set[str]) -> None:
    with open(EXCL_PATH, "w", encoding="utf-8") as f:
        for s in sorted(items):
            f.write(s + "\n")


def add_symbol(sym: str) -> bool:
    sym = (sym or "").strip().upper()
    if not sym:
        return False
    s = load_excluded()
    if sym in s:
        return False
    s.add(sym)
    save_excluded(s)
    return True


def remove_symbol(sym: str) -> bool:
    sym = (sym or "").strip().upper()
    if not sym:
        return False
    s = load_excluded()
    if sym not in s:
        return False
    s.remove(sym)
    save_excluded(s)
    return True
