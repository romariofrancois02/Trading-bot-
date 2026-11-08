
import os
import re
import time
import math
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ==================================================
# ROMARIO TRADER PRO 4.3
# Pocket Option OTC - Alert Only
# ==================================================

VERSION = "ROMARIO TRADER PRO 4.3"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

MIN_PAYOUT = 79
MIN_SCORE = 6
EXPIRATION_SECONDS = 60
PREP_SECONDS = 30

LOCAL_TZ = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
    raise RuntimeError("Missing GitHub Secrets.")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "ROMARIO-TRADER-PRO/4.3"
}

session = requests.Session()
session.headers.update(HEADERS)

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD",
    "AUD", "NZD", "CNY", "HKD", "SGD", "MXN",
    "ZAR", "TRY", "NOK", "SEK", "DKK", "PLN",
    "HUF", "CZK", "ILS", "THB", "IDR", "MYR",
    "PHP", "INR", "KRW", "TWD", "AED", "SAR",
    "QAR", "KWD", "BHD", "OMR", "CLP", "COP",
    "ARS", "PEN", "EGP", "TND", "NGN", "PKR",
    "BDT", "VND", "UAH", "RON", "BGN", "ISK",
    "HRK", "RUB", "KES", "GHS", "MAD", "JOD",
    "LKR", "NPR"
}


# ==================================================
# TELEGRAM
# ==================================================

def telegram(message):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        },
        timeout=15
    )

    response.raise_for_status()


# ==================================================
# API
# ==================================================

def api_get(path, params=None):
    url = BASE_URL + path

    response = session.get(
        url,
        params=params,
        timeout=20
    )

    response.raise_for_status()
    return response.json()


def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    symbols = []

    for item in data.get("symbols", []):
        symbol = item.get("symbol", "")

        # Forex OTC only; exclude stocks and commodities.
        match = re.fullmatch(
            r"([A-Z]{3})([A-Z]{3})_otc",
            symbol
        )

        if not match:
            continue

        base, quote = match.groups()

        if base not in CURRENCIES:
            continue

        if quote not in CURRENCIES:
            continue

        if item.get("open") is not True:
            continue

        payout = item.get("payout")

        if payout is None:
            continue

        try:
            payout = float(payout)
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        symbols.append({
            "symbol": symbol,
            "name": item.get("name", symbol),
            "payout": payout
        })

    return symbols


def get_candles(symbol, timeframe):
    data = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": timeframe,
            "limit": 100
        }
    )

    candles = data.get("candles", [])

    if len(candles) < 60:
        return None

    df = pd.DataFrame(candles)

    required = ["open", "high", "low", "close"]

    if not all(column in df.columns for column in required):
        return None

    for column in required:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna(subset=required)

    # Exclude the newest candle to avoid using a
    # potentially unfinished candle.
    df = df.iloc[:-1].copy()

    if len(df) < 50:
        return None

    return df.reset_index(drop=True)


# ==================================================
# INDICATORS
# ==================================================

def add_indicators(df):
    df = df.copy()

    df["ema9"] = df["close"].ewm(
        span=9, adjust=False
    ).mean()

    df["ema20"] = df["close"].ewm(
        span=20, adjust=False
    ).mean()

    df["ema50"] = df["close"].ewm(
        span=50, adjust=False
    ).mean()

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

    df["rsi"] = 100 - (100 / (1 + rs))

    return df


# ==================================================
# SIGNAL ANALYSIS
# ==================================================

def analyze_pair(symbol, payout):
    m1 = get_candles(symbol, 60)
    m5 = get_candles(symbol, 300)

    if m1 is None or m5 is None:
        return None

    m1 = add_indicators(m1)
    m5 = add_indicators(m5)

    a = m1.iloc[-1]
    previous = m1.iloc[-2]
    b = m5.iloc[-1]

    score_call = 0
    score_put = 0

    # M1 trend
    if a["ema9"] > a["ema20"] > a["ema50"]:
        score_call += 2

    if a["ema9"] < a["ema20"] < a["ema50"]:
        score_put += 2

    # M5 trend confirmation
    if b["ema20"] > b["ema50"]:
        score_call += 2

    if b["ema20"] < b["ema50"]:
        score_put += 2

    # Candle momentum
    if a["close"] > a["open"]:
        score_call += 1

    if a["close"] < a["open"]:
        score_put += 1

    # Price relative to EMA9
    if a["close"] > a["ema9"]:
        score_call += 1

    if a["close"] < a["ema9"]:
        score_put += 1

    # RSI
    rsi = a["rsi"]

    if pd.isna(rsi):
        return None

    if 50 <= rsi <= 68:
        score_call += 1

    if 32 <= rsi <= 50:
        score_put += 1

    # Candle body strength
    candle_range = a["high"] - a["low"]

    if candle_range <= 0:
        return None

    body = abs(a["close"] - a["open"])
    body_ratio = body / candle_range

    if body_ratio < 0.35:
        return None

    # Avoid extreme RSI
    if rsi >= 75 or rsi <= 25:
        return None

    if score_call >= MIN_SCORE and score_call > score_put:
        return {
            "symbol": symbol,
            "name": symbol.replace("_otc", ""),
            "direction": "CALL",
            "emoji": "🟢",
            "trend": "BULLISH",
            "score": score_call,
            "payout": payout,
            "rsi": round(float(rsi), 2)
        }

    if score_put >= MIN_SCORE and score_put > score_call:
        return {
            "symbol": symbol,
            "name": symbol.replace("_otc", ""),
            "direction": "PUT",
            "emoji": "🔴",
            "trend": "BEARISH",
            "score": score_put,
            "payout": payout,
            "rsi": round(float(rsi), 2)
        }

    return None


# ==================================================
# ENTRY SCHEDULING
# ==================================================

def schedule_entry():
    now = time.time()

    # Entry must be at least 32 seconds away,
    # leaving time to send the alert 30 seconds early.
    entry_timestamp = math.ceil(
        (now + PREP_SECONDS + 2) / 60
    ) * 60

    signal_timestamp = entry_timestamp - PREP_SECONDS

    return signal_timestamp, entry_timestamp


def wait_until(timestamp):
    while True:
        remaining = timestamp - time.time()

        if remaining <= 0:
            return

        time.sleep(min(remaining, 1))


def local_time(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        timezone.utc
    ).astimezone(LOCAL_TZ).strftime("%I:%M:%S %p")


# ==================================================
# MESSAGE
# ==================================================

def signal_message(signal, entry_timestamp, scanned):
    return f"""
🤖 {VERSION}

{signal['emoji']} {signal['direction']}

💱 PAIR: {signal['name']} OTC
📊 SYMBOL: {signal['symbol']}

⏱ TIMEFRAME: M1
🎯 ENTRY TIME: {local_time(entry_timestamp)}
⌛ EXPIRATION: 60 SECONDS

💰 PAYOUT: {signal['payout']:.0f}%
📊 WIN RATE: NOT ENOUGH DATA

📈 TREND: {signal['trend']}
📉 RSI: {signal['rsi']}

🔍 TECHNICAL SCORE: {signal['score']}/7
📋 PAIRS SCANNED: {scanned}

🕒 TIMEZONE: Florida (ET)

⚠️ DEMO TESTING RECOMMENDED
""".strip()


def wait_message(scanned, reason):
    return f"""
🤖 {VERSION}

⚪ WAIT

No valid signal.

💰 MINIMUM PAYOUT: {MIN_PAYOUT}%
📋 PAIRS SCANNED: {scanned}

REASON: {reason}

No trade recommended.
""".strip()


# ==================================================
# MAIN
# ==================================================

def main():
    print("=" * 45)
    print(VERSION)
    print("POCKET OPTION OTC SCANNER")
    print("=" * 45)

    try:
        symbols = get_symbols()
    except Exception as error:
        print("SYMBOL API ERROR:", error)

        telegram(
            f"🤖 {VERSION}\n"
            "🔴 OTCHARTS API ERROR\n"
            f"{str(error)[:300]}"
        )
        return

    print("Pairs with payout >= 79%:", len(symbols))

    if not symbols:
        telegram(
            wait_message(
                0,
                "No open Forex OTC pairs with verified payout >= 79%."
            )
        )
        return

    candidates = []
    scanned = 0

    for item in symbols:
        symbol = item["symbol"]
        payout = item["payout"]

        scanned += 1

        print(
            f"Scanning {symbol} | "
            f"Payout: {payout}%"
        )

        try:
            result = analyze_pair(symbol, payout)

            if result:
                candidates.append(result)

                print(
                    "VALID:",
                    result["direction"],
                    result["symbol"],
                    result["score"]
                )

        except Exception as error:
            print("PAIR ERROR:", symbol, error)

        time.sleep(0.15)

    if not candidates:
        telegram(
            wait_message(
                scanned,
                "No valid EMA + RSI setup found."
            )
        )
        return

    # Select only the strongest candidate.
    candidates.sort(
        key=lambda x: (x["score"], x["payout"]),
        reverse=True
    )

    best = candidates[0]

    print("BEST SIGNAL:", best)

    # Schedule signal 30 seconds before entry.
    signal_timestamp, entry_timestamp = schedule_entry()

    print("ENTRY TIME:", local_time(entry_timestamp))
    print("WAITING FOR SIGNAL TIME...")

    # Refresh the selected pair shortly before the alert.
    wait_until(signal_timestamp - 7)

    if time.time() > signal_timestamp + 2:
        telegram(
            wait_message(
                scanned,
                "Timing missed. Late signals are not sent."
            )
        )
        return

    try:
        refreshed = analyze_pair(
            best["symbol"],
            best["payout"]
        )
    except Exception as error:
        print("FINAL CHECK ERROR:", error)
        refreshed = None

    if not refreshed:
        telegram(
            wait_message(
                scanned,
                "Selected setup did not pass the final check."
            )
        )
        return

    if refreshed["direction"] != best["direction"]:
        telegram(
            wait_message(
                scanned,
                "Market direction changed before the alert."
            )
        )
        return

    # Do not send a late alert.
    wait_until(signal_timestamp)

    if time.time() > signal_timestamp + 2:
        telegram(
            wait_message(
                scanned,
                "Signal timing missed. No late entry."
            )
        )
        return

    message = signal_message(
        refreshed,
        entry_timestamp,
        scanned
    )

    telegram(message)

    print("SIGNAL SENT SUCCESSFULLY")


if __name__ == "__main__":
    main()
