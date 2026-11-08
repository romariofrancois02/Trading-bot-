
import os
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

API_KEY = os.getenv("OTCHARTS_API_KEY")
BASE_URL = "https://otcharts.com"
PAIR = "EURUSD_otc"

TIMEFRAMES = {
    60: "M1",
    300: "M5"
}

# Timestamp adjustment used in our previous tests.
BROKER_OFFSET_HOURS = 2


def get_candles(timeframe):
    response = requests.get(
        BASE_URL + "/v1/candles",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json"
        },
        params={
            "venue": "otc",
            "symbol": PAIR,
            "tf": timeframe,
            "limit": 100
        },
        timeout=30
    )

    print("HTTP STATUS:", response.status_code)
    response.raise_for_status()

    candles = response.json().get("candles", [])

    if len(candles) < 60:
        raise ValueError("Not enough candles")

    df = pd.DataFrame(candles)

    required = ["time", "open", "high", "low", "close"]

    for field in required:
        if field not in df.columns:
            raise ValueError(f"Missing field: {field}")

        df[field] = pd.to_numeric(
            df[field],
            errors="coerce"
        )

    df = df.dropna(subset=required)
    df = df.sort_values("time")
    df = df.drop_duplicates(subset=["time"])
    df = df.reset_index(drop=True)

    # Convert broker timestamps to UTC using
    # the offset assumption from our previous tests.
    df["utc_time"] = pd.to_datetime(
        df["time"],
        unit="s",
        utc=True
    ) - pd.Timedelta(hours=BROKER_OFFSET_HOURS)

    now = datetime.now(timezone.utc)

    # Use completed candles only.
    candle_end = (
        df["utc_time"]
        + pd.to_timedelta(timeframe, unit="s")
    )

    df = df[candle_end <= pd.Timestamp(now)]

    if len(df) < 50:
        raise ValueError("Not enough completed candles")

    latest_end = (
        df.iloc[-1]["utc_time"]
        + timedelta(seconds=timeframe)
    )

    age = (now - latest_end).total_seconds() / 60

    print("COMPLETED CANDLES:", len(df))
    print("LATEST CLOSED CANDLE:", df.iloc[-1]["utc_time"])
    print("CANDLE AGE MINUTES:", round(age, 2))

    if age < 0 or age > 10:
        raise ValueError("Candle freshness not verified")

    return df.reset_index(drop=True)


def calculate_indicators(df):
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

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["atr10"] = tr.ewm(
        alpha=1 / 10,
        adjust=False
    ).mean()

    df["atr14"] = tr.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    df["kc_middle"] = df["ema20"]

    df["kc_upper"] = (
        df["ema20"] + 2 * df["atr10"]
    )

    df["kc_lower"] = (
        df["ema20"] - 2 * df["atr10"]
    )

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))

    df["rsi14"] = 100 - (100 / (1 + rs))

    # RSI smoothing is a preliminary filter,
    # not a complete QQE MOD implementation.
    df["rsi_smooth"] = df["rsi14"].ewm(
        span=5,
        adjust=False
    ).mean()

    return df


def trend(row):
    if row["ema20"] > row["ema50"]:
        return "BULLISH"

    if row["ema20"] < row["ema50"]:
        return "BEARISH"

    return "NEUTRAL"


def test_signal(m1, m5):
    current = m1.iloc[-1]
    previous = m1.iloc[-2]
    trend5 = m5.iloc[-1]

    m1_trend = trend(current)
    m5_trend = trend(trend5)

    print("\n========== SIGNAL ANALYSIS ==========")

    print("M1 TREND:", m1_trend)
    print("M5 TREND:", m5_trend)

    print("M1 CLOSE:", round(float(current["close"]), 6))
    print("M5 CLOSE:", round(float(trend5["close"]), 6))

    print("M1 RSI:", round(float(current["rsi14"]), 2))
    print(
        "M1 RSI SMOOTH:",
        round(float(current["rsi_smooth"]), 2)
    )

    print("M1 ATR14:", round(float(current["atr14"]), 6))

    # Previous candle must retest the EMA20 area.
    zone = float(previous["atr10"]) * 0.25

    ema_zone_low = float(previous["ema20"]) - zone
    ema_zone_high = float(previous["ema20"]) + zone

    retest = (
        float(previous["low"]) <= ema_zone_high
        and float(previous["high"]) >= ema_zone_low
    )

    bullish_confirmation = (
        current["close"] > current["open"]
        and current["close"] > previous["high"]
    )

    bearish_confirmation = (
        current["close"] < current["open"]
        and current["close"] < previous["low"]
    )

    rsi_rising = (
        current["rsi_smooth"] > previous["rsi_smooth"]
        and current["rsi_smooth"] > 50
    )

    rsi_falling = (
        current["rsi_smooth"] < previous["rsi_smooth"]
        and current["rsi_smooth"] < 50
    )

    m5_bullish = (
        m5_trend == "BULLISH"
        and trend5["close"] > trend5["ema20"]
    )

    m5_bearish = (
        m5_trend == "BEARISH"
        and trend5["close"] < trend5["ema20"]
    )

    atr_valid = (
        pd.notna(current["atr14"])
        and current["atr14"] > 0
    )

    print("\nCONDITIONS:")
    print("M1 RETEST:", retest)
    print("BULLISH CONFIRMATION:", bullish_confirmation)
    print("BEARISH CONFIRMATION:", bearish_confirmation)
    print("M5 BULLISH:", m5_bullish)
    print("M5 BEARISH:", m5_bearish)
    print("RSI RISING:", rsi_rising)
    print("RSI FALLING:", rsi_falling)
    print("ATR VALID:", atr_valid)

    call = (
        retest
        and bullish_confirmation
        and m1_trend == "BULLISH"
        and m5_bullish
        and rsi_rising
        and atr_valid
    )

    put = (
        retest
        and bearish_confirmation
        and m1_trend == "BEARISH"
        and m5_bearish
        and rsi_falling
        and atr_valid
    )

    print("\n========== TEST RESULT ==========")

    if call:
        print("CALL CANDIDATE")
        print("Direction: UP")
        print("Pair:", PAIR)

    elif put:
        print("PUT CANDIDATE")
        print("Direction: DOWN")
        print("Pair:", PAIR)

    else:
        print("WAIT")
        print("No confirmed setup.")

    print("\nTEST MODE ONLY")
    print("NO TELEGRAM ALERT SENT")
    print("NO TRADE EXECUTED")


def main():
    print("ROMARIO TRADER PRO 3.0")
    print("CALL / PUT SIGNAL LOGIC TEST")
    print("============================")

    if not API_KEY:
        raise SystemExit("ERROR: API KEY MISSING")

    try:
        m1 = calculate_indicators(
            get_candles(60)
        )

        m5 = calculate_indicators(
            get_candles(300)
        )

        test_signal(m1, m5)

    except Exception as error:
        print("\nTEST FAILED:", str(error))
        raise


if __name__ == "__main__":
    main()
