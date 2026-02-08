import datetime
import json
from pathlib import Path

import pandas as pd
import yfinance as yf
from sklearn.ensemble import RandomForestClassifier

BASE_DIR = Path(__file__).resolve().parent

# List of NIFTY symbols (Yahoo Finance format with .NS suffix)
SYMBOLS = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "HDFC.NS",
    "KOTAKBANK.NS", "AXISBANK.NS", "HINDUNILVR.NS", "ITC.NS", "BHARTIARTL.NS", "LT.NS",
    "BAJFINANCE.NS", "BAJAJ-AUTO.NS", "WIPRO.NS", "SUNPHARMA.NS", "TITAN.NS", "ONGC.NS",
    "EICHERMOT.NS", "MARUTI.NS", "HCLTECH.NS", "POWERGRID.NS", "ADANIPORTS.NS", "TECHM.NS",
    "NESTLEIND.NS", "DIVISLAB.NS", "COALINDIA.NS", "NTPC.NS", "ULTRACEMCO.NS", "BPCL.NS",
    "GRASIM.NS", "INDUSINDBK.NS", "JSWSTEEL.NS", "CIPLA.NS", "IOC.NS", "HEROMOTOCO.NS",
    "SBILIFE.NS", "BRITANNIA.NS", "DRREDDY.NS", "ADANIGREEN.NS", "HAVELLS.NS", "ICICIPRULI.NS",
    "TATASTEEL.NS", "VEDL.NS", "DLF.NS",
]


def main():
    volatility: dict[str, float] = {}
    historical_data: dict[str, pd.DataFrame] = {}

    print("Downloading data for symbols...")
    for symbol in SYMBOLS:
        try:
            data = yf.download(symbol, period="1y", interval="1d", progress=False, auto_adjust=True)
            if data is None or data.empty or "Close" not in data.columns:
                volatility[symbol] = 0.0
                continue

            df = data[["Close"]].copy()
            df["Return"] = df["Close"].pct_change()
            df.dropna(subset=["Return"], inplace=True)
            if df.empty:
                volatility[symbol] = 0.0
                continue

            vol = float(df["Return"].std(ddof=0))
            volatility[symbol] = vol
            historical_data[symbol] = df
        except Exception as exc:
            volatility[symbol] = 0.0
            print(f"Failed to get data for {symbol}: {exc}")

    top5 = sorted(volatility, key=volatility.get, reverse=True)
    top5 = [sym for sym in top5 if sym in historical_data][:5]
    print(f"Top 5 volatile symbols: {top5}")

    models: dict[str, RandomForestClassifier] = {}
    predictions: list[str] = []
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    for symbol in top5:
        df = historical_data[symbol].copy()
        if df.empty or len(df) < 10:
            continue

        for lag in range(1, 6):
            df[f"Return_lag{lag}"] = df["Return"].shift(lag)

        features = [f"Return_lag{lag}" for lag in range(1, 6)]

        latest_features = df[features].iloc[-1]
        if latest_features.isna().any():
            continue

        train_df = df.copy()
        next_return = train_df["Return"].shift(-1)
        train_df["Target"] = (next_return > 0).where(next_return.notna())
        train_df.dropna(subset=features + ["Target"], inplace=True)
        if train_df.empty:
            continue

        train_df["Target"] = train_df["Target"].astype(int)
        X_train = train_df[features]
        y_train = train_df["Target"]

        if len(y_train.unique()) < 2:
            continue

        model = RandomForestClassifier(n_estimators=100, random_state=42)
        model.fit(X_train, y_train)
        models[symbol] = model

        pred = model.predict(latest_features.values.reshape(1, -1))[0]
        if pred == 1:
            predictions.append(symbol.replace(".NS", ""))

    with open(BASE_DIR / "models.pkl", "wb") as file_obj:
        pd.to_pickle(models, file_obj)

    output = {"date": today_str, "stocks": predictions}
    with open(BASE_DIR / "predictions.json", "w", encoding="utf-8") as file_obj:
        json.dump(output, file_obj, indent=2)

    print(f"Predicted symbols for trading: {predictions}")


if __name__ == "__main__":
    main()
