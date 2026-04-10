import threading
import os
from kiteconnect import KiteConnect
import config as CFG

_kite_lock = threading.Lock()
_kite_instance: KiteConnect | None = None
_kite_token: str | None = None


def get_kite() -> KiteConnect:
    """Return a cached KiteConnect instance, recreating only when the token changes."""
    global _kite_instance, _kite_token

    if not CFG.KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY missing")

    # Always re-read token from env so a runtime token refresh is picked up.
    token = os.getenv("KITE_ACCESS_TOKEN") or CFG.KITE_ACCESS_TOKEN
    if not token:
        raise RuntimeError("KITE_ACCESS_TOKEN missing")

    with _kite_lock:
        if _kite_instance is None or _kite_token != token:
            _kite_instance = KiteConnect(api_key=CFG.KITE_API_KEY)
            _kite_instance.set_access_token(token)
            _kite_token = token
        return _kite_instance


def invalidate_kite() -> None:
    """Force the next get_kite() call to create a fresh instance.

    Call this after programmatically refreshing the access token.
    """
    global _kite_instance, _kite_token
    with _kite_lock:
        _kite_instance = None
        _kite_token = None
