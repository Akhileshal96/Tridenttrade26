from kiteconnect import KiteConnect
import config as CFG
import os

def get_kite() -> KiteConnect:

    if not CFG.KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY missing")

    # Always fetch latest token from environment
    token = os.getenv("KITE_ACCESS_TOKEN") or CFG.KITE_ACCESS_TOKEN

    if not token:
        raise RuntimeError("KITE_ACCESS_TOKEN missing")

    kite = KiteConnect(api_key=CFG.KITE_API_KEY)
    kite.set_access_token(token)

    return kite
