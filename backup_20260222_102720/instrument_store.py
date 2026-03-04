import os
import pandas as pd
from broker_zerodha import get_kite
from log_store import append_log

INSTR_PATH = os.path.join(os.getcwd(), "cache", "instruments.csv")
os.makedirs(os.path.dirname(INSTR_PATH), exist_ok=True)

def refresh_instruments() -> pd.DataFrame:
    append_log("INFO", "INSTR", "Refreshing instruments from Zerodha…")
    df = pd.DataFrame(get_kite().instruments("NSE"))
    df.to_csv(INSTR_PATH, index=False)
    append_log("INFO", "INSTR", f"Instruments cached: {len(df)} rows")
    return df

def _load_df() -> pd.DataFrame:
    if os.path.exists(INSTR_PATH):
        return pd.read_csv(INSTR_PATH)
    return refresh_instruments()

def token_for_symbol(symbol: str) -> int:
    df = _load_df()
    row = df[df["tradingsymbol"] == symbol]
    if row.empty:
        raise RuntimeError(f"Symbol not found in instruments: {symbol}")
    return int(row.iloc[0]["instrument_token"])
