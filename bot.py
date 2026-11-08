
import os
import requests
import pandas as pd
import numpy as np

BASE_URL = "https://otcharts.com"
API_KEY = os.getenv("OTCHARTS_API_KEY")

SYMBOL = "EURUSD_otc"
VENUE = "otc"

M1_LIMIT = 5000
M5_LIMIT = 1000

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "ROMARIO-TRADER-PRO-3.0"
}


def get_candles(timeframe, limit):
    url = f"{BASE_URL}/v1/candles"

    params = {
        "venue": VENUE,
        "symbol": SYMBOL,
        "tf": timeframe,
        "limit": limit
    }

    response = requests.get(
        url,
        headers=HEADERS,
        params=params,
        timeout=30
    )

    print(f"HTTP STATUS {timeframe}s:", response.status_code)

    response.raise_for_status()
    data = response.json()

    candles = data.get("candles", [])

    if not candles:
        raise ValueError(f"Pa gen bouji pou timeframe {timeframe}")

    df = pd.DataFrame(candles)

    required = ["open", "high", "low", "close", "time"]

    for column in required:
        if column not in df.columns:
            raise ValueError(f"Done manke kolòn: {column}")

    df = df[required].copy()

    for column in ["open", "high", "low", "close", "time"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna()
    df = df.sort_values("time")
    df = df.drop_duplicates(subset=["time"])
    df = df.reset_index(drop=True)

    gaps = data.get("gaps", {})
    missing = gaps.get("missingBars", 0)

    print("CANDLES:", len(df))
    print("MISSING BARS:", missing)

    if missing:
        raise ValueError(
            f"Done gen {missing} bouji ki manke. "
            "Nou pap fè backtest sou done sa yo."
        )

    return df


def calculate_indicators(df):
    df = df.copy()

    df["ema20"] = df["close"].ewm(
        span=20, adjust=False
    ).mean()

    df["ema50"] = df["close"].ewm(
        span=50, adjust=False
    ).mean()

    previous_close = df["close"].shift(1)

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - previous_close).abs()
    tr3 = (df["low"] - previous_close).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3], axis=1
    ).max(axis=1)

    df["atr10"] = true_range.rolling(10).mean()
    df["atr14"] = true_range.rolling(14).mean()

    change = df["close"].diff()

    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14, adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["rsi"] = 100 - (100 / (1 + rs))

    df["rsi_smooth"] = df["rsi"].ewm(
        span=5, adjust=False
    ).mean()

    df["kc_middle"] = df["ema20"]
    df["kc_upper"] = df["ema20"] + (2 * df["atr10"])
    df["kc_lower"] = df["ema20"] - (2 * df["atr10"])

    return df


def get_m5_confirmation(m5, signal_close_time):
    completed = m5[
        (m5["time"] + 300) <= signal_close_time
    ]

    if completed.empty:
        return None

    return completed.iloc[-1]


def run_backtest(m1, m5):
    results = []

    for i in range(50, len(m1) - 1):
        previous = m1.iloc[i - 1]
        current = m1.iloc[i]
        next_candle = m1.iloc[i + 1]

        signal_close_time = current["time"] + 60

        m5_candle = get_m5_confirmation(
            m5, signal_close_time
        )

        if m5_candle is None:
            continue

        values = [
            current["ema20"],
            current["ema50"],
            current["atr10"],
            current["atr14"],
            current["rsi_smooth"],
            previous["ema20"],
            previous["atr10"],
            m5_candle["ema20"],
            m5_candle["ema50"]
        ]

        if not all(np.isfinite(x) for x in values):
            continue

        atr = current["atr10"]

        if atr <= 0 or current["atr14"] <= 0:
            continue

        # Retest EMA20 zone on previous candle
        zone = previous["atr10"] * 0.25

        retest_call = (
            previous["low"] <= previous["ema20"] + zone
            and previous["high"] >= previous["ema20"] - zone
        )

        retest_put = (
            previous["high"] >= previous["ema20"] - zone
            and previous["low"] <= previous["ema20"] + zone
        )

        bullish_confirmation = (
            current["close"] > current["open"]
            and current["close"] > previous["high"]
        )

        bearish_confirmation = (
            current["close"] < current["open"]
            and current["close"] < previous["low"]
        )

        m1_bullish = (
            current["ema20"] > current["ema50"]
            and current["close"] > current["ema20"]
        )

        m1_bearish = (
            current["ema20"] < current["ema50"]
            and current["close"] < current["ema20"]
        )

        m5_bullish = (
            m5_candle["ema20"] > m5_candle["ema50"]
            and m5_candle["close"] > m5_candle["ema20"]
        )

        m5_bearish = (
            m5_candle["ema20"] < m5_candle["ema50"]
            and m5_candle["close"] < m5_candle["ema20"]
        )

        rsi_rising = (
            current["rsi_smooth"] > previous["rsi_smooth"]
            and current["rsi_smooth"] > 50
        )

        rsi_falling = (
            current["rsi_smooth"] < previous["rsi_smooth"]
            and current["rsi_smooth"] < 50
        )

        call_signal = (
            retest_call
            and bullish_confirmation
            and m1_bullish
            and m5_bullish
            and rsi_rising
        )

        put_signal = (
            retest_put
            and bearish_confirmation
            and m1_bearish
            and m5_bearish
            and rsi_falling
        )

        if not call_signal and not put_signal:
            continue

        # Approximation:
        # Enter at next M1 candle open.
        # Expire at that candle's close.
        entry = next_candle["open"]
        exit_price = next_candle["close"]

        if call_signal:
            direction = "CALL"
            if exit_price > entry:
                outcome = "WIN"
            elif exit_price < entry:
                outcome = "LOSS"
            else:
                outcome = "TIE"

        else:
            direction = "PUT"
            if exit_price < entry:
                outcome = "WIN"
            elif exit_price > entry:
                outcome = "LOSS"
            else:
                outcome = "TIE"

        results.append({
            "time": current["time"],
            "direction": direction,
            "entry": entry,
            "exit": exit_price,
            "result": outcome
        })

    return results


def main():
    print("ROMARIO TRADER PRO 3.0")
    print("HISTORICAL BACKTEST")
    print("==============================")

    if not API_KEY:
        raise ValueError(
            "OTCHARTS_API_KEY pa jwenn nan GitHub Secrets."
        )

    m1 = get_candles(60, M1_LIMIT)
    m5 = get_candles(300, M5_LIMIT)

    m1 = calculate_indicators(m1)
    m5 = calculate_indicators(m5)

    print("\nSTARTING BACKTEST...")

    results = run_backtest(m1, m5)

    total = len(results)

    wins = sum(
        1 for item in results
        if item["result"] == "WIN"
    )

    losses = sum(
        1 for item in results
        if item["result"] == "LOSS"
    )

    ties = sum(
        1 for item in results
        if item["result"] == "TIE"
    )

    calls = sum(
        1 for item in results
        if item["direction"] == "CALL"
    )

    puts = sum(
        1 for item in results
        if item["direction"] == "PUT"
    )

    decided = wins + losses

    print("\n========== RESULTS ==========")
    print("M1 CANDLES:", len(m1))
    print("M5 CANDLES:", len(m5))
    print("TOTAL SIGNALS:", total)
    print("CALL SIGNALS:", calls)
    print("PUT SIGNALS:", puts)
    print("WINS:", wins)
    print("LOSSES:", losses)
    print("TIES:", ties)

    if decided:
        win_rate = (wins / decided) * 100
        print(f"WIN RATE: {win_rate:.2f}%")
    else:
        print("WIN RATE: NOT AVAILABLE")

    print("\nLAST 10 SIGNALS:")

    for item in results[-10:]:
        print(
            item["direction"],
            "|",
            item["result"],
            "| Entry:",
            item["entry"],
            "| Exit:",
            item["exit"]
        )

    print("\nBACKTEST COMPLETE")
    print("No real trades were placed.")


if __name__ == "__main__":
    main()
