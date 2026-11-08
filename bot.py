
import os
import re
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

VERSION = "ROMARIO TRADER PRO 4.3"

BASE_URL = "https://otcharts.com"
API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TZ = ZoneInfo("America/New_York")

CURRENCIES = {
    "USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD",
    "CNY","HKD","SGD","MXN","ZAR","TRY","NOK","SEK",
    "DKK","PLN","HUF","CZK","ILS","THB","IDR","MYR",
    "PHP","INR","KRW","TWD","AED","SAR","QAR","KWD",
    "BHD","OMR","CLP","COP","ARS","PEN","EGP","TND",
    "NGN","PKR","BDT","VND","UAH","RON","BGN","ISK",
    "RUB","KES","GHS","MAD","JOD","LKR","NPR"
}

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
}

def now_local():
    return datetime.now(TZ)

def telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Telegram secrets missing")
        return False

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={"chat_id": TG_CHAT_ID, "text": message},
            timeout=15
        )
        response.raise_for_status()
        print("Telegram message sent")
        return True
    except requests.RequestException as e:
        print("Telegram error:", e)
        return False

def api_get(path, params):
    response = requests.get(
        BASE_URL + path,
        headers=HEADERS,
        params=params,
        timeout=20
    )
    response.raise_for_status()
    return response.json()

def get_symbols():
    data = api_get("/v1/symbols", {"venue": "otc"})

    if isinstance(data, dict):
        items = data.get("data", data.get("symbols", []))
    else:
        items = data

    symbols = []

    for item in items:
        if isinstance(item, str):
            symbol = item
        elif isinstance(item, dict):
            symbol = (
                item.get("symbol")
                or item.get("ticker")
                or item.get("name")
            )
        else:
            continue

        if not symbol:
            continue

        match = re.fullmatch(r"([A-Z]{3})([A-Z]{3})_otc", symbol)

        if not match:
            continue

        base, quote = match.groups()

        if base in CURRENCIES and quote in CURRENCIES:
            symbols.append(symbol)

    return sorted(set(symbols))

def get_candles(symbol, timeframe):
    data = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": timeframe,
            "limit": 100
        }
    )

    if isinstance(data, dict):
        candles = data.get(
            "data",
            data.get("candles", data.get("results", []))
        )
    else:
        candles = data

    if not candles or len(candles) < 55:
        return None

    df = pd.DataFrame(candles)

    aliases = {
        "open": ["open", "o"],
        "high": ["high", "h"],
        "low": ["low", "l"],
        "close": ["close", "c"]
    }

    rename = {}

    for standard, possibilities in aliases.items():
        for name in possibilities:
            if name in df.columns:
                rename[name] = standard
                break

    df = df.rename(columns=rename)

    required = ["open", "high", "low", "close"]

    if not all(col in df.columns for col in required):
        return None

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required)

    if len(df) < 55:
        return None

    return df.reset_index(drop=True)

def add_indicators(df):
    df = df.copy()

    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    return df

def analyze(symbol):
    try:
        m1 = get_candles(symbol, 60)
        m5 = get_candles(symbol, 300)

        if m1 is None or m5 is None:
            return None

        m1 = add_indicators(m1)
        m5 = add_indicators(m5)

        # Use the latest returned candle.
        a = m1.iloc[-1]
        previous = m1.iloc[-2]
        trend = m5.iloc[-1]

        if pd.isna(a["rsi"]) or pd.isna(trend["ema50"]):
            return None

        body = abs(a["close"] - a["open"])
        candle_range = a["high"] - a["low"]

        if candle_range <= 0:
            return None

        body_ratio = body / candle_range

        call_score = 0
        put_score = 0

        if a["ema9"] > a["ema20"]:
            call_score += 2
        if a["ema20"] > a["ema50"]:
            call_score += 1
        if a["close"] > a["ema20"]:
            call_score += 1
        if a["close"] > a["open"]:
            call_score += 1
        if a["close"] > previous["close"]:
            call_score += 1
        if trend["ema20"] > trend["ema50"]:
            call_score += 2
        if trend["close"] > trend["ema20"]:
            call_score += 1

        if a["ema9"] < a["ema20"]:
            put_score += 2
        if a["ema20"] < a["ema50"]:
            put_score += 1
        if a["close"] < a["ema20"]:
            put_score += 1
        if a["close"] < a["open"]:
            put_score += 1
        if a["close"] < previous["close"]:
            put_score += 1
        if trend["ema20"] < trend["ema50"]:
            put_score += 2
        if trend["close"] < trend["ema20"]:
            put_score += 1

        if body_ratio < 0.35:
            return None

        if call_score >= 6 and 35 <= a["rsi"] <= 70:
            return {
                "symbol": symbol,
                "direction": "CALL",
                "score": call_score,
                "price": a["close"],
                "rsi": a["rsi"]
            }

        if put_score >= 6 and 30 <= a["rsi"] <= 65:
            return {
                "symbol": symbol,
                "direction": "PUT",
                "score": put_score,
                "price": a["close"],
                "rsi": a["rsi"]
            }

        return None

    except Exception as e:
        print(f"Analysis error {symbol}: {e}")
        return None

def next_entry_time():
    now = now_local()
    entry = now.replace(second=0, microsecond=0) + timedelta(minutes=1)

    if (entry - now).total_seconds() < 35:
        entry += timedelta(minutes=1)

    return entry

def main():
    print(VERSION)
    print("POCKET OPTION OTC SCANNER")
    print("=" * 40)

    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY missing")

    symbols = get_symbols()
    print(f"Forex OTC pairs found: {len(symbols)}")

    candidates = []

    for index, symbol in enumerate(symbols, 1):
        print(f"[{index}/{len(symbols)}] Scanning {symbol}")

        result = analyze(symbol)

        if result:
            candidates.append(result)
            print(
                f"VALID: {result['direction']} "
                f"{symbol} SCORE {result['score']}"
            )

    if not candidates:
        telegram(
            f"⚪ WAIT\n"
            f"{VERSION}\n"
            f"No valid setup found."
        )
        print("No valid setup")
        return

    best = max(candidates, key=lambda x: x["score"])

    entry = next_entry_time()
    signal_time = entry - timedelta(seconds=30)

    wait_seconds = (signal_time - now_local()).total_seconds()

    if wait_seconds < 0:
        telegram("⚪ WAIT\nSignal timing missed. No late entry.")
        return

    print(f"Waiting {wait_seconds:.1f} seconds for signal time")

    time.sleep(wait_seconds)

    current = now_local()

    if abs((current - signal_time).total_seconds()) > 2:
        telegram("⚪ WAIT\nSignal timing missed. No late entry.")
        return

    emoji = "🟢" if best["direction"] == "CALL" else "🔴"

    message = (
        f"{emoji} {best['direction']}\n"
        f"{best['symbol']}\n"
        f"M1 | 60s\n"
        f"ENTRY: {entry.strftime('%I:%M:%S %p')}"
    )

    telegram(message)

    print(message)
    print("Win rate: NOT AVAILABLE")

if __name__ == "__main__":
    main()
