
import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ==========================================
# ROMARIO TRADER PRO 4.3 - CHECKUP
# ==========================================

BASE_URL = "https://otcharts.com"
API_KEY = os.getenv("OTCHARTS_API_KEY")

SYMBOL = os.getenv("CHECK_SYMBOL", "USDIDR_otc")

if not API_KEY:
    raise RuntimeError("OTCHARTS_API_KEY is missing")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "ROMARIO-CHECKUP/4.3"
}

session = requests.Session()
session.headers.update(HEADERS)


def api_get(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=20
    )

    print("HTTP STATUS:", response.status_code)
    response.raise_for_status()

    return response.json()


def get_payout():
    print("\n========== PAYOUT CHECK ==========")

    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    symbols = data.get("symbols", [])

    print("TOTAL SYMBOLS:", len(symbols))

    found = None

    for item in symbols:
        if item.get("symbol") == SYMBOL:
            found = item
            break

    if not found:
        print("SYMBOL NOT FOUND:", SYMBOL)
        return None

    print("SYMBOL:", found.get("symbol"))
    print("OPEN:", found.get("open"))
    print("PAYOUT:", found.get("payout"))

    try:
        payout = float(found.get("payout"))
    except (TypeError, ValueError):
        payout = None

    if payout is None:
        print("RESULT: PAYOUT NOT AVAILABLE")
    elif payout >= 79:
        print("RESULT: PAYOUT PASSED")
    else:
        print("RESULT: PAYOUT BELOW 79%")

    return found


def get_candles(symbol, timeframe):
    print(f"\n========== M{timeframe // 60} CANDLE CHECK ==========")

    data = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": timeframe,
            "limit": 100
        }
    )

    candles = data.get("candles", [])

    print("CANDLES RECEIVED:", len(candles))

    if not candles:
        print("NO CANDLES RETURNED")
        print("RESPONSE KEYS:", list(data.keys()))
        return None

    if not isinstance(candles[0], dict):
        print("UNEXPECTED CANDLE FORMAT")
        print("SAMPLE:", str(candles[0])[:500])
        return None

    print("CANDLE FIELDS:", list(candles[0].keys()))
    print("LATEST RAW CANDLE:", candles[-1])

    df = pd.DataFrame(candles)

    required = ["open", "high", "low", "close"]

    if not all(column in df.columns for column in required):
        print("MISSING OHLC FIELDS")
        return None

    for column in required:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna(subset=required).reset_index(drop=True)

    print("VALID CANDLES:", len(df))

    timestamp_fields = [
        "timestamp", "time", "datetime",
        "date", "t", "from", "at"
    ]

    time_field = next(
        (x for x in timestamp_fields if x in df.columns),
        None
    )

    if time_field:
        print("TIMESTAMP FIELD:", time_field)
        print("LATEST TIMESTAMP:", df[time_field].iloc[-1])
        print("PREVIOUS TIMESTAMP:", df[time_field].iloc[-2])
    else:
        print("WARNING: NO RECOGNIZED TIMESTAMP FIELD")

    return df


def calculate_adx_atr(df):
    print("\n========== ADX / ATR CHECK ==========")

    high = df["high"]
    low = df["low"]
    close = df["close"]

    previous_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where(
        (plus_dm > minus_dm) & (plus_dm > 0),
        0
    )

    minus_dm = minus_dm.where(
        (minus_dm > plus_dm) & (minus_dm > 0),
        0
    )

    atr = tr.ewm(
        alpha=1/14,
        adjust=False
    ).mean()

    plus_di = (
        100 * plus_dm.ewm(
            alpha=1/14,
            adjust=False
        ).mean() / atr
    )

    minus_di = (
        100 * minus_dm.ewm(
            alpha=1/14,
            adjust=False
        ).mean() / atr
    )

    denominator = (plus_di + minus_di).replace(0, np.nan)

    dx = (
        100 * (plus_di - minus_di).abs()
        / denominator
    )

    adx = dx.ewm(
        alpha=1/14,
        adjust=False
    ).mean()

    latest_adx = adx.iloc[-1]
    latest_atr = atr.iloc[-1]
    latest_close = close.iloc[-1]

    print("LATEST CLOSE:", latest_close)
    print("ATR:", latest_atr)
    print("ATR %:", round(latest_atr / latest_close * 100, 5))
    print("ADX:", round(float(latest_adx), 2))

    if pd.isna(latest_adx):
        print("ADX RESULT: NOT ENOUGH DATA")
    elif latest_adx >= 25:
        print("ADX RESULT: STRONG TREND")
    elif latest_adx >= 20:
        print("ADX RESULT: MODERATE TREND")
    else:
        print("ADX RESULT: WEAK TREND")

    if pd.isna(latest_atr) or latest_atr <= 0:
        print("ATR RESULT: INVALID")
    else:
        print("ATR RESULT: CALCULATED")

    return latest_adx, latest_atr


def main():
    print("=" * 45)
    print("ROMARIO TRADER PRO 4.3 CHECKUP")
    print("=" * 45)

    print(
        "UTC NOW:",
        datetime.now(timezone.utc).isoformat()
    )

    try:
        symbol_info = get_payout()

        if not symbol_info:
            return

        if symbol_info.get("open") is not True:
            print("CHECKUP STOPPED: SYMBOL IS CLOSED")
            return

        payout = float(symbol_info.get("payout"))

        if payout < 79:
            print("CHECKUP STOPPED: PAYOUT BELOW 79%")
            return

        for timeframe in [60, 300]:
            df = get_candles(SYMBOL, timeframe)

            if df is None or len(df) < 50:
                print("INSUFFICIENT CANDLE DATA")
                continue

            calculate_adx_atr(df)

        print("\n========== CHECKUP COMPLETE ==========")
        print("SYMBOL:", SYMBOL)
        print("PAYOUT:", payout)
        print("NO SIGNAL SENT")
        print("NO TRADE EXECUTED")

    except Exception as error:
        print("\nCHECKUP ERROR:", repr(error))


if __name__ == "__main__":
    main()
