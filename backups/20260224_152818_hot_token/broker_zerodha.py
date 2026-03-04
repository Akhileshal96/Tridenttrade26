from kiteconnect import KiteConnect
import config as CFG

def get_kite() -> KiteConnect:
    if not CFG.KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY missing")
    if not CFG.KITE_ACCESS_TOKEN:
        raise RuntimeError("KITE_ACCESS_TOKEN missing")

    kite = KiteConnect(api_key=CFG.KITE_API_KEY)
    kite.set_access_token(CFG.KITE_ACCESS_TOKEN)
    return kite
