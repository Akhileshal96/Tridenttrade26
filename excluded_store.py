import os
from typing import Set

EXCL_PATH = os.path.join(os.getcwd(), "data", "excluded.txt")
os.makedirs(os.path.dirname(EXCL_PATH), exist_ok=True)

def load_excluded() -> Set[str]:
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
