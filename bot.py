
import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# ==========================================
# ROMARIO TRADER PRO 3.3
# ALL POCKET OPTION OTC SCANNER
# MANUAL SCAN - TELEGRAM ALERTS ONLY
# ==========================================

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
VENUE = "otc"

M1_TF = 60
M5_TF = 300

M1_LIMIT = 100
M5_LIMIT = 100

SWEEP_LOOKBACK = 10
BROKER_OFFSET_HOURS = 2

REQUEST_DELAY = 0.12
MAX_MESSAGE_LENGTH = 3500

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "ROMARIO-TRADER-PRO-3.3"
})


# ==========================================
# API REQUEST
# ==========================================

def api_get(endpoint, params=None):
    url = BASE_URL + endpoint

    response = session.get(
        url,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    return response.json()


# ==========================================
# TIME CONVERSION
# ==========================================

def convert_time(value):
    if isinstance(value, (int, float)):
        unit = "ms" if value > 100000000000 else "s"

        result = pd.to_datetime(
            value,
            unit=unit,
            utc=True
        )

        return result - timedelta(
            hours=BROKER_OFFSET_HOURS
        )

    result = pd.to_datetime(value, utc=True)

    return result - timedelta(
        hours=BROKER_OFFSET_HOURS
    )


# ==========================================
# GET ALL OTC INSTRUMENTS
# ==========================================

def get_otc_symbols():
    print("\nGETTING OTC SYMBOLS...")

    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    if isinstance(data, list):
        instruments = data

    else:
        instruments = (
            data.get("symbols")
            or data.get("instruments")
            or data.get("data")
            or []
        )

    if isinstance(instruments, dict):
        instruments = (
            instruments.get("symbols")
            or instruments.get("instruments")
            or []
        )

    if not isinstance(instruments, list):
        raise ValueError(
            "Unexpected symbols API response."
        )

    symbols = []

    for item in instruments:

        if not isinstance(item, dict):
            continue

        symbol = item.get("symbol")

        if not symbol:
            continue

        name = (
            item.get("name")
            or item.get("displayName")
            or symbol
        )

        is_open = item.get("open")

        if is_open is False:
            continue

        if isinstance(is_open, str):
            if is_open.lower() in (
                "false", "closed", "0"
            ):
                continue

        symbols.append({
            "symbol": str(symbol),
            "name": str(name)
        })

    unique = {}

    for item in symbols:
        unique[item["symbol"]] = item

    symbols = list(unique.values())

    if not symbols:
        raise ValueError(
            "No open OTC instruments found."
        )

    print("OPEN OTC INSTRUMENTS:", len(symbols))

    return symbols


# ==========================================
# GET CANDLES
# ==========================================

def fetch_candles(symbol, timeframe, limit):

    data = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": timeframe,
            "limit": limit
        }
    )

    candles = data.get("candles", [])

    if not candles:
        raise ValueError("No candles returned.")

    missing = (
        data.get("gaps", {}).get("missingBars", 0)
    )

    if missing:
        raise ValueError(
            f"Missing candles: {missing}"
        )

    df = pd.DataFrame(candles)

    required = [
        "time",
        "open",
        "high",
        "low",
        "close"
    ]

    for column in required:
        if column not in df.columns:
            raise ValueError(
                f"Missing candle field: {column}"
            )

    for column in [
        "open", "high", "low", "close"
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df["time"] = df["time"].apply(convert_time)

    df = df.dropna(subset=required)

    df = df.sort_values("time")

    df = df.drop_duplicates(
        subset=["time"]
    )

    return df.reset_index(drop=True)


# ==========================================
# INDICATORS
# ==========================================

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

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["atr"] = tr.rolling(14).mean()

    return df


# ==========================================
# COMPLETED CANDLES ONLY
# ==========================================

def get_completed(df, timeframe, now):

    completed = df[
        (
            df["time"]
            + timedelta(seconds=timeframe)
        ) <= now
    ].copy()

    return completed.reset_index(drop=True)


# ==========================================
# M5 TREND
# ==========================================

def get_m5_trend(m5, signal_time):

    available = m5[
        (
            m5["time"]
            + timedelta(seconds=M5_TF)
        ) <= signal_time
    ]

    if available.empty:
        return None

    return available.iloc[-1]


# ==========================================
# LIQUIDITY SWEEP + BOS + RETEST
# ==========================================

def detect_signal(m1, m5):

    if len(m1) < 40 or len(m5) < 30:
        return None

    i = len(m1) - 1

    sweep = m1.iloc[i - 2]
    bos = m1.iloc[i - 1]
    retest = m1.iloc[i]

    signal_time = (
        retest["time"]
        + timedelta(seconds=M1_TF)
    )

    trend = get_m5_trend(
        m5,
        signal_time
    )

    if trend is None:
        return None

    if not np.isfinite(sweep["atr"]):
        return None

    if sweep["atr"] <= 0:
        return None

    history = m1.iloc[
        i - 2 - SWEEP_LOOKBACK:i - 2
    ]

    if len(history) < SWEEP_LOOKBACK:
        return None

    old_low = history["low"].min()
    old_high = history["high"].max()

    # BULLISH LIQUIDITY SWEEP

    bullish_sweep = (
        sweep["low"] < old_low
        and sweep["close"] > old_low
    )

    # BEARISH LIQUIDITY SWEEP

    bearish_sweep = (
        sweep["high"] > old_high
        and sweep["close"] < old_high
    )

    # BREAK OF STRUCTURE

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

    # RETEST

    bullish_retest = (
        bullish_bos
        and retest["low"]
        <= sweep["high"] + tolerance
        and retest["close"] > sweep["high"]
        and retest["close"] > retest["open"]
    )

    bearish_retest = (
        bearish_bos
        and retest["high"]
        >= sweep["low"] - tolerance
        and retest["close"] < sweep["low"]
        and retest["close"] < retest["open"]
    )

    # M5 CONFIRMATION

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
            "time": signal_time
        }

    if bearish_retest and m5_bearish:

        return {
            "direction": "PUT",
            "price": float(retest["close"]),
            "time": signal_time
        }

    return None


# ==========================================
# TELEGRAM
# ==========================================

def send_telegram(message):

    url = (
        "https://api.telegram.org/"
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

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise ValueError(
            "Telegram message was rejected."
        )

    print("TELEGRAM MESSAGE SENT")


def send_long_message(message):

    lines = message.split("\n")

    chunks = []
    current = ""

    for line in lines:

        if (
            len(current) + len(line) + 1
            > MAX_MESSAGE_LENGTH
        ):

            chunks.append(current)
            current = ""

        current += line + "\n"

    if current:
        chunks.append(current)

    for chunk in chunks:
        send_telegram(chunk)


# ==========================================
# SCAN ONE INSTRUMENT
# ==========================================

def scan_symbol(instrument, now):

    symbol = instrument["symbol"]
    name = instrument["name"]

    try:

        m1 = fetch_candles(
            symbol,
            M1_TF,
            M1_LIMIT
        )

        time.sleep(REQUEST_DELAY)

        m5 = fetch_candles(
            symbol,
            M5_TF,
            M5_LIMIT
        )

        m1 = add_indicators(m1)
        m5 = add_indicators(m5)

        m1 = get_completed(
            m1,
            M1_TF,
            now
        )

        m5 = get_completed(
            m5,
            M5_TF,
            now
        )

        if len(m1) < 40 or len(m5) < 30:
            raise ValueError(
                "Not enough completed candles."
            )

        last_m1 = m1.iloc[-1]
        last_m5 = m5.iloc[-1]

        m1_age = (
            now
            - (
                last_m1["time"]
                + timedelta(seconds=M1_TF)
            )
        ).total_seconds()

        m5_age = (
            now
            - (
                last_m5["time"]
                + timedelta(seconds=M5_TF)
            )
        ).total_seconds()

        if m1_age < 0 or m1_age > 180:
            raise ValueError("M1 data is stale.")

        if m5_age < 0 or m5_age > 600:
            raise ValueError("M5 data is stale.")

        signal = detect_signal(m1, m5)

        if signal:

            signal["symbol"] = symbol
            signal["name"] = name

            print(
                "CANDIDATE:",
                name,
                signal["direction"]
            )

            return signal

        print("WAIT:", name)

        return None

    except Exception as error:

        print(
            "SKIPPED:",
            symbol,
            "|",
            str(error)
        )

        return None


# ==========================================
# MAIN SCANNER
# ==========================================

def main():

    print("\nROMARIO TRADER PRO 3.3")
    print("ALL OTC SCANNER")
    print("MANUAL DEMO MODE")
    print("==============================")

    if not API_KEY:
        raise ValueError(
            "OTCHARTS_API_KEY is missing."
        )

    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise ValueError(
            "Telegram secrets are missing."
        )

    now = datetime.now(timezone.utc)

    instruments = get_otc_symbols()

    signals = []
    errors = 0

    total = len(instruments)

    for index, instrument in enumerate(
        instruments,
        start=1
    ):

        print(
            f"\nSCANNING {index}/{total}:",
            instrument["name"]
        )

        signal = scan_symbol(
            instrument,
            now
        )

        if signal:
            signals.append(signal)

        time.sleep(REQUEST_DELAY)

    # ======================================
    # REPORT
    # ======================================

    print("\n========== SCAN RESULTS ==========")

    print("INSTRUMENTS FOUND:", total)
    print("SIGNALS FOUND:", len(signals))

    calls = [
        s for s in signals
        if s["direction"] == "CALL"
    ]

    puts = [
        s for s in signals
        if s["direction"] == "PUT"
    ]

    print("CALL:", len(calls))
    print("PUT:", len(puts))

    report = [
        "ROMARIO TRADER PRO 3.3",
        "ALL OTC MARKET SCAN",
        "--------------------------",
        f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"OTC INSTRUMENTS: {total}",
        f"CALL CANDIDATES: {len(calls)}",
        f"PUT CANDIDATES: {len(puts)}",
        "TIMEFRAME: M1",
        "M5 CONFIRMATION",
        "EXPIRATION: 60 SECONDS",
        "",
        "DEMO CANDIDATES ONLY",
        "--------------------------"
    ]

    if not signals:

        report.extend([
            "STATUS: WAIT",
            "No valid setup found.",
            "No trade recommended."
        ])

    else:

        for signal in signals:

            report.extend([
                "",
                f"PAIR: {signal['name']}",
                f"SYMBOL: {signal['symbol']}",
                f"DIRECTION: {signal['direction']}",
                f"OTCHARTS CLOSE: {signal['price']}",
                f"TIME UTC: {signal['time']}",
                "EXPIRATION: 60 SECONDS",
                "--------------------------"
            ])

    report.extend([
        "",
        "Strategy: Liquidity Sweep + BOS + Retest",
        "M5 trend confirmation required.",
        "OTCHARTS prices are not independently",
        "verified against Pocket Option quotes.",
        "DEMO ONLY - NO AUTOMATIC TRADES."
    ])

    send_long_message(
        "\n".join(report)
    )

    print("\nSCAN COMPLETE")
    print("NO REAL TRADES EXECUTED.")


if __name__ == "__main__":
    main()
