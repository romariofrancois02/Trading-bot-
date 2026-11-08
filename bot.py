
import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# ==========================================
# ROMARIO TRADER PRO 3.2
# LIQUIDITY SWEEP + BOS + RETEST
# DEMO TELEGRAM ALERTS ONLY
# ==========================================

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
SYMBOL = "EURUSD_otc"
VENUE = "otc"

M1_TIMEFRAME = 60
M5_TIMEFRAME = 300

M1_LIMIT = 300
M5_LIMIT = 100

SWEEP_LOOKBACK = 10
BROKER_OFFSET_HOURS = 2

# ==========================================
# FETCH MARKET DATA
# ==========================================

def convert_time(value):
    if isinstance(value, (int, float)):
        unit = "ms" if value > 100000000000 else "s"
        result = pd.to_datetime(value, unit=unit, utc=True)
        return result - timedelta(hours=BROKER_OFFSET_HOURS)

    return pd.to_datetime(value, utc=True)


def fetch_candles(timeframe, limit):
    url = f"{BASE_URL}/v1/candles"

    params = {
        "venue": VENUE,
        "symbol": SYMBOL,
        "tf": timeframe,
        "limit": limit
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "User-Agent": "ROMARIO-TRADER-PRO-3.2"
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=30
    )

    print("HTTP STATUS:", response.status_code)
    response.raise_for_status()

    data = response.json()
    candles = data.get("candles", [])

    if not candles:
        raise ValueError("No candles received.")

    df = pd.DataFrame(candles)

    required = ["time", "open", "high", "low", "close"]

    for column in required:
        if column not in df.columns:
            raise ValueError(f"Missing field: {column}")

    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(
            df[column], errors="coerce"
        )

    df["time"] = df["time"].apply(convert_time)

    df = df.dropna(subset=required)
    df = df.sort_values("time")
    df = df.drop_duplicates(subset=["time"])
    df = df.reset_index(drop=True)

    missing = data.get("gaps", {}).get("missingBars", 0)

    print("TIMEFRAME:", timeframe)
    print("CANDLES:", len(df))
    print("MISSING BARS:", missing)

    if missing:
        raise ValueError("Missing candles detected.")

    return df


# ==========================================
# INDICATORS
# ==========================================

def add_indicators(df):
    df = df.copy()

    df["ema20"] = df["close"].ewm(
        span=20, adjust=False
    ).mean()

    df["ema50"] = df["close"].ewm(
        span=50, adjust=False
    ).mean()

    previous_close = df["close"].shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous_close).abs(),
        (df["low"] - previous_close).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.rolling(14).mean()

    return df


# ==========================================
# COMPLETED CANDLES
# ==========================================

def completed_candles(df, timeframe, now):
    result = df[
        (df["time"] + timedelta(seconds=timeframe)) <= now
    ].copy()

    return result.reset_index(drop=True)


# ==========================================
# M5 TREND CONFIRMATION
# ==========================================

def get_m5_trend(m5, signal_close_time):
    available = m5[
        (m5["time"] + timedelta(seconds=M5_TIMEFRAME))
        <= signal_close_time
    ]

    if available.empty:
        return None

    return available.iloc[-1]


# ==========================================
# LIQUIDITY SWEEP + BOS + RETEST
# ==========================================

def detect_signal(m1, m5):
    if len(m1) < SWEEP_LOOKBACK + 5:
        return None

    i = len(m1) - 1

    sweep = m1.iloc[i - 2]
    bos = m1.iloc[i - 1]
    retest = m1.iloc[i]

    signal_close_time = (
        retest["time"] + timedelta(seconds=M1_TIMEFRAME)
    )

    trend = get_m5_trend(m5, signal_close_time)

    if trend is None:
        return None

    if not np.isfinite(sweep["atr"]) or sweep["atr"] <= 0:
        return None

    history = m1.iloc[
        i - 2 - SWEEP_LOOKBACK:i - 2
    ]

    if len(history) < SWEEP_LOOKBACK:
        return None

    old_low = history["low"].min()
    old_high = history["high"].max()

    # Bullish liquidity sweep
    bullish_sweep = (
        sweep["low"] < old_low
        and sweep["close"] > old_low
    )

    # Bearish liquidity sweep
    bearish_sweep = (
        sweep["high"] > old_high
        and sweep["close"] < old_high
    )

    # Break of structure
    bullish_bos = (
        bullish_sweep
        and bos["close"] > sweep["high"]
        and bos["close"] > bos["open"]
    )

    bearish_bos = (
        bearish_sweep
        and bos["close"] < sweep["low"]
        and bos["close"] < bos["open"]
    )

    tolerance = sweep["atr"] * 0.15

    # Retest confirmation
    bullish_retest = (
        bullish_bos
        and retest["low"] <= sweep["high"] + tolerance
        and retest["close"] > sweep["high"]
        and retest["close"] > retest["open"]
    )

    bearish_retest = (
        bearish_bos
        and retest["high"] >= sweep["low"] - tolerance
        and retest["close"] < sweep["low"]
        and retest["close"] < retest["open"]
    )

    # M5 direction
    m5_bullish = (
        trend["ema20"] > trend["ema50"]
        and trend["close"] > trend["ema20"]
    )

    m5_bearish = (
        trend["ema20"] < trend["ema50"]
        and trend["close"] < trend["ema20"]
    )

    if bullish_retest and m5_bullish:
        return {
            "direction": "CALL",
            "price": float(retest["close"]),
            "time": signal_close_time
        }

    if bearish_retest and m5_bearish:
        return {
            "direction": "PUT",
            "price": float(retest["close"]),
            "time": signal_close_time
        }

    return None


# ==========================================
# TELEGRAM
# ==========================================

def send_telegram(message):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )

    print("TELEGRAM STATUS:", response.status_code)
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise ValueError("Telegram rejected the message.")

    print("TELEGRAM MESSAGE SENT")


# ==========================================
# MAIN
# ==========================================

def main():
    print("ROMARIO TRADER PRO 3.2")
    print("DEMO SIGNAL MODE")
    print("==============================")

    if not API_KEY:
        raise ValueError("OTCHARTS_API_KEY missing.")

    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise ValueError("Telegram secrets missing.")

    now = datetime.now(timezone.utc)

    m1 = fetch_candles(M1_TIMEFRAME, M1_LIMIT)
    m5 = fetch_candles(M5_TIMEFRAME, M5_LIMIT)

    m1 = add_indicators(m1)
    m5 = add_indicators(m5)

    m1 = completed_candles(m1, M1_TIMEFRAME, now)
    m5 = completed_candles(m5, M5_TIMEFRAME, now)

    if m1.empty or m5.empty:
        raise ValueError("No completed candles available.")

    last_m1 = m1.iloc[-1]
    last_m5 = m5.iloc[-1]

    m1_age = (
        now - (last_m1["time"] + timedelta(seconds=60))
    ).total_seconds()

    m5_age = (
        now - (last_m5["time"] + timedelta(seconds=300))
    ).total_seconds()

    print("M1 LAST CLOSED:", last_m1["time"])
    print("M1 AGE SECONDS:", round(m1_age, 1))
    print("M5 LAST CLOSED:", last_m5["time"])
    print("M5 AGE SECONDS:", round(m5_age, 1))

    # Reject stale data
    if m1_age < 0 or m1_age > 180:
        raise ValueError("M1 data is stale or timestamp is incorrect.")

    if m5_age < 0 or m5_age > 600:
        raise ValueError("M5 data is stale or timestamp is incorrect.")

    print("M1 PRICE:", last_m1["close"])

    signal = detect_signal(m1, m5)

    if signal is None:
        message = (
            "ROMARIO TRADER PRO 3.2\n"
            "STATUS: WAIT\n"
            "PAIR: EUR/USD OTC\n"
            "TIMEFRAME: M1\n"
            "EXPIRATION: 60 SECONDS\n\n"
            "No valid setup found.\n"
            "No trade recommended."
        )

        print("RESULT: WAIT")

    else:
        message = (
            "ROMARIO TRADER PRO 3.2\n"
            "DEMO CANDIDATE SIGNAL\n"
            "----------------------\n"
            f"DIRECTION: {signal['direction']}\n"
            "PAIR: EUR/USD OTC\n"
            "TIMEFRAME: M1\n"
            "EXPIRATION: 60 SECONDS\n"
            f"OTCHARTS CLOSE: {signal['price']:.5f}\n"
            f"TIME UTC: {signal['time']}\n\n"
            "Strategy: Liquidity Sweep + BOS + Retest\n"
            "M5 trend confirmation: PASSED\n\n"
            "WARNING: OTCHARTS PRICE NOT VERIFIED "
            "AGAINST POCKET OPTION.\n"
            "DEMO ONLY - NOT A LIVE TRADE."
        )

        print("RESULT:", signal["direction"])

    send_telegram(message)

    print("RUN COMPLETE")


if __name__ == "__main__":
    main()
