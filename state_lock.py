import threading


STATE_LOCK = threading.RLock()


def safe_update(state: dict, key: str, updater_fn):
    with STATE_LOCK:
        old = state.get(key)
        new_val = updater_fn(old)
        state[key] = new_val
        return new_val


def safe_set(state: dict, key: str, value):
    with STATE_LOCK:
        state[key] = value
        return value


class PositionManager:
    def __init__(self, state: dict, key: str = "positions"):
        self._state = state
        self._key = key
        with STATE_LOCK:
            self._state.setdefault(self._key, {})

    def get(self, symbol: str, default=None):
        with STATE_LOCK:
            return self._state.setdefault(self._key, {}).get(symbol, default)

    def set(self, symbol: str, value: dict):
        with STATE_LOCK:
            self._state.setdefault(self._key, {})[symbol] = value
            self._state["open_trades"] = self._state[self._key]
            return value

    def remove(self, symbol: str):
        with STATE_LOCK:
            out = self._state.setdefault(self._key, {}).pop(symbol, None)
            self._state["open_trades"] = self._state[self._key]
            return out

    def contains(self, symbol: str) -> bool:
        with STATE_LOCK:
            return symbol in self._state.setdefault(self._key, {})

    def count(self) -> int:
        with STATE_LOCK:
            return len(self._state.setdefault(self._key, {}))

    def snapshot(self) -> dict:
        with STATE_LOCK:
            return dict(self._state.setdefault(self._key, {}))

    def clear(self):
        with STATE_LOCK:
            self._state.setdefault(self._key, {}).clear()
            self._state["open_trades"] = self._state[self._key]

