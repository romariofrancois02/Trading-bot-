
import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_KEY = os.getenv("OTCHARTS_API_KEY")
BASE_URL = "https://otcharts.com"
PAIR = "EURUSD_otc"

TIMEFRAMES = {
    60: "M1",
    300: "M5"
}

session = requests.Session()

retry = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[502, 503, 504],
    allowed_methods=["GET"]
)

adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)

session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
})


def get_candles(timeframe):
    response = session.get(
        BASE_URL + "/v1/candles",
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

    data = response.json()
    candles = data.get("candles", [])

    if len(candles) < 50:
        raise ValueError("Not enough candles")

    df = pd.DataFrame(candles)

    required = ["time", "open", "high", "low", "close"]

    for field in required:
        if field not in df.columns:
            raise ValueError(f"Missing field: {field}")

    for field in ["time", "open", "high", "low", "close"]:
        df[field] = pd.to_numeric(
            df[field],
            errors="coerce"
        )

    df = df.dropna(subset=required)
    df = df.sort_values("time")
    df = df.drop_duplicates(subset=["time"])
    df = df.reset_index(drop=True)

    if len(df) < 50:
        raise ValueError("Not enough valid candles")

    return df


def check_freshness(df, label):
    now = datetime.now(timezone.utc)

    latest = datetime.fromtimestamp(
        int(df.iloc[-1]["time"]),
        timezone.utc
    ) - timedelta(hours=2)

    age = (now - latest).total_seconds() / 60

    print(f"\n{label} FRESHNESS CHECK")
    print("CURRENT UTC:", now.isoformat())
    print("LATEST ADJUSTED UTC:", latest.isoformat())
    print("AGE MINUTES:", round(age, 2))

    if age < 0 or age > 10:
        raise ValueError(
            f"{label}: candle freshness not verified"
        )


def calculate_indicators(df):
    df = df.copy()

    # EMA 20 and EMA 50
    df["ema20"] = df["close"].ewm(
        span=20,
        adjust=False
    ).mean()

    df["ema50"] = df["close"].ewm(
        span=50,
        adjust=False
    ).mean()

    # True Range
    previous_close = df["close"].shift(1)

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - previous_close).abs()
    tr3 = (df["low"] - previous_close).abs()

    df["tr"] = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    # ATR 10 and ATR 14
    df["atr10"] = df["tr"].ewm(
        alpha=1 / 10,
        adjust=False
    ).mean()

    df["atr14"] = df["tr"].ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    # Keltner Channel
    df["kc_middle"] = df["ema20"]
    df["kc_upper"] = (
        df["ema20"] + 2 * df["atr10"]
    )
    df["kc_lower"] = (
        df["ema20"] - 2 * df["atr10"]
    )

    # RSI 14
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

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["rsi14"] = 100 - (
        100 / (1 + rs)
    )

    # Preliminary QQE-style calculation.
    # This is NOT the complete QQE MOD indicator.
    df["rsi_smooth"] = df["rsi14"].ewm(
        span=5,
        adjust=False
    ).mean()

    rsi_change = df["rsi_smooth"].diff().abs()

    rsi_atr = rsi_change.ewm(
        span=27,
        adjust=False
    ).mean()

    df["qqe_upper"] = (
        df["rsi_smooth"] + 4 * rsi_atr
    )

    df["qqe_lower"] = (
        df["rsi_smooth"] - 4 * rsi_atr
    )

    # VWAP requires real volume data.
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(
            df["volume"],
            errors="coerce"
        )

        valid_volume = (
            df["volume"].notna().all()
            and (df["volume"] > 0).all()
        )

        if valid_volume:
            typical_price = (
                df["high"] +
                df["low"] +
                df["close"]
            ) / 3

            df["vwap"] = (
                (typical_price * df["volume"]).cumsum()
                / df["volume"].cumsum()
            )
        else:
            df["vwap"] = np.nan
    else:
        df["vwap"] = np.nan

    return df


def show_results(df, label):
    last = df.iloc[-1]

    print(f"\n========== {label} INDICATORS ==========")

    indicators = [
        "ema20",
        "ema50",
        "atr10",
        "atr14",
        "kc_middle",
        "kc_upper",
        "kc_lower",
        "rsi14",
        "rsi_smooth",
        "qqe_upper",
        "qqe_lower"
    ]

    for name in indicators:
        value = last[name]

        if pd.isna(value):
            print(name.upper(), ": UNAVAILABLE")
        else:
            print(
                name.upper(),
                ":",
                round(float(value), 6)
            )

    if pd.isna(last["vwap"]):
        print("VWAP: UNAVAILABLE - NO VERIFIED VOLUME")
    else:
        print(
            "VWAP:",
            round(float(last["vwap"]), 6)
        )

    if last["ema20"] > last["ema50"]:
        print("EMA TREND: BULLISH")
    elif last["ema20"] < last["ema50"]:
        print("EMA TREND: BEARISH")
    else:
        print("EMA TREND: NEUTRAL")

    print("INDICATOR CALCULATION: COMPLETED")


def main():
    print("ROMARIO TRADER PRO 3.0")
    print("INDICATOR CALCULATION TEST")
    print("==========================")

    if not API_KEY:
        raise SystemExit("ERROR: API KEY MISSING")

    all_passed = True

    for timeframe, label in TIMEFRAMES.items():
        try:
            print(f"\nFETCHING {label} DATA")

            df = get_candles(timeframe)

            check_freshness(df, label)

            df = calculate_indicators(df)

            show_results(df, label)

        except Exception as error:
            all_passed = False
            print(f"\n{label} TEST FAILED:", str(error))

    print("\n==========================")

    if all_passed:
        print("M1 AND M5 CALCULATIONS COMPLETED")
        print("INDICATOR TEST PASSED")
    else:
        print("INDICATOR TEST FAILED")
        print("CHECK THE ERRORS ABOVE")

    print("NO SIGNALS SENT")
    print("NO TRADES EXECUTED")


if __name__ == "__main__":
    main()
