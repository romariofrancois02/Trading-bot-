
import os
import requests
import pandas as pd
import numpy as np

API_KEY = os.getenv("OTCHARTS_API_KEY")

BASE_URL = "https://otcharts.com"
SYMBOL = "EURUSD_otc"
VENUE = "otc"

M1_LIMIT = 5000
M5_LIMIT = 1000

SWEEP_LOOKBACK = 10
STRUCTURE_LOOKBACK = 5
ATR_PERIOD = 14


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
        "User-Agent": "ROMARIO-TRADER-PRO-3.1"
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=30
    )

    print(f"HTTP STATUS {timeframe}s:", response.status_code)
    response.raise_for_status()

    data = response.json()
    candles = data.get("candles", [])

    if not candles:
        raise ValueError(f"No candles returned for {timeframe}s")

    df = pd.DataFrame(candles)

    required = ["time", "open", "high", "low", "close"]

    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing candle field: {col}")

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required)
    df = df.sort_values("time")
    df = df.drop_duplicates(subset=["time"])
    df = df.reset_index(drop=True)

    gaps = data.get("gaps", {})
    missing = gaps.get("missingBars", 0)

    print("CANDLES:", len(df))
    print("MISSING BARS:", missing)

    if missing:
        raise ValueError("Missing candles detected. Backtest stopped.")

    return df


def add_indicators(df):
    df = df.copy()

    df["ema20"] = df["close"].ewm(
        span=20,
        adjust=False
    ).mean()

    df["ema50"] = df["close"].ewm(
        span=50,
        adjust=False
    ).mean()

    previous_close = df["close"].shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous_close).abs(),
        (df["low"] - previous_close).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    return df


def get_m5_trend(m5, signal_close_time):
    completed = m5[
        (m5["time"] + 300) <= signal_close_time
    ]

    if completed.empty:
        return None

    return completed.iloc[-1]


def find_signal(m1, m5, i):
    sweep = m1.iloc[i - 2]
    bos = m1.iloc[i - 1]
    retest = m1.iloc[i]

    signal_close_time = retest["time"] + 60

    trend = get_m5_trend(m5, signal_close_time)

    if trend is None:
        return None

    values = [
        sweep["atr"],
        trend["ema20"],
        trend["ema50"]
    ]

    if not all(np.isfinite(x) for x in values):
        return None

    if sweep["atr"] <= 0:
        return None

    # Previous structure before the sweep candle
    history = m1.iloc[i - 2 - SWEEP_LOOKBACK:i - 2]

    if len(history) < SWEEP_LOOKBACK:
        return None

    old_low = history["low"].min()
    old_high = history["high"].max()

    # Bullish liquidity sweep:
    # Price takes previous lows, then closes back above them.
    bullish_sweep = (
        sweep["low"] < old_low
        and sweep["close"] > old_low
    )

    # Bearish liquidity sweep:
    # Price takes previous highs, then closes back below them.
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

    # Retest of the broken structure level
    tolerance = sweep["atr"] * 0.15

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

    m5_bullish = (
        trend["ema20"] > trend["ema50"]
        and trend["close"] > trend["ema20"]
    )

    m5_bearish = (
        trend["ema20"] < trend["ema50"]
        and trend["close"] < trend["ema20"]
    )

    if bullish_retest and m5_bullish:
        return "CALL"

    if bearish_retest and m5_bearish:
        return "PUT"

    return None


def run_backtest(m1, m5):
    results = []

    start = max(
        SWEEP_LOOKBACK + 2,
        STRUCTURE_LOOKBACK + 2
    )

    for i in range(start, len(m1) - 1):
        direction = find_signal(m1, m5, i)

        if direction is None:
            continue

        # Entry at next candle open.
        # Expiry at next candle close.
        next_candle = m1.iloc[i + 1]

        entry = next_candle["open"]
        exit_price = next_candle["close"]

        if direction == "CALL":
            if exit_price > entry:
                outcome = "WIN"
            elif exit_price < entry:
                outcome = "LOSS"
            else:
                outcome = "TIE"

        else:
            if exit_price < entry:
                outcome = "WIN"
            elif exit_price > entry:
                outcome = "LOSS"
            else:
                outcome = "TIE"

        results.append({
            "time": m1.iloc[i]["time"],
            "direction": direction,
            "entry": entry,
            "exit": exit_price,
            "result": outcome
        })

    return results


def print_group(name, trades):
    wins = sum(t["result"] == "WIN" for t in trades)
    losses = sum(t["result"] == "LOSS" for t in trades)
    ties = sum(t["result"] == "TIE" for t in trades)

    decided = wins + losses

    print(f"\n{name}")
    print("--------------------")
    print("TOTAL:", len(trades))
    print("WINS:", wins)
    print("LOSSES:", losses)
    print("TIES:", ties)

    if decided:
        rate = wins / decided * 100
        print(f"WIN RATE: {rate:.2f}%")
    else:
        print("WIN RATE: NOT AVAILABLE")


def main():
    print("ROMARIO TRADER PRO 3.1")
    print("LIQUIDITY SWEEP + BOS + RETEST")
    print("HISTORICAL BACKTEST")
    print("==============================")

    if not API_KEY:
        raise ValueError("OTCHARTS_API_KEY is missing.")

    m1 = fetch_candles(60, M1_LIMIT)
    m5 = fetch_candles(300, M5_LIMIT)

    m1 = add_indicators(m1)
    m5 = add_indicators(m5)

    print("\nRUNNING BACKTEST...")

    results = run_backtest(m1, m5)

    calls = [
        t for t in results
        if t["direction"] == "CALL"
    ]

    puts = [
        t for t in results
        if t["direction"] == "PUT"
    ]

    print("\n========== OVERALL RESULTS ==========")

    print("M1 CANDLES:", len(m1))
    print("M5 CANDLES:", len(m5))
    print("TOTAL SIGNALS:", len(results))

    print_group("CALL RESULTS", calls)
    print_group("PUT RESULTS", puts)
    print_group("ALL SIGNALS", results)

    print("\nLAST 15 SIGNALS")
    print("--------------------")

    for trade in results[-15:]:
        print(
            trade["direction"],
            "|",
            trade["result"],
            "| Entry:",
            trade["entry"],
            "| Exit:",
            trade["exit"]
        )

    print("\nBACKTEST COMPLETE")
    print("No real trades were placed.")


if __name__ == "__main__":
    main()
