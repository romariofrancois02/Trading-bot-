
import os
import re
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ==========================================
# ROMARIO TRADER PRO 3.6
# MANUAL SIGNAL BOT - NO AUTO TRADING
# ==========================================

VERSION = "ROMARIO TRADER PRO 3.6"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.otcharts.com"

LOCAL_TZ = ZoneInfo("America/New_York")

TIMEOUT = 12
MAX_WORKERS = 12
MAX_CANDLES = 100

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
})


def log(message):
    print(message, flush=True)


def telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        log("Telegram secrets missing.")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TG_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=TIMEOUT
        )
        response.raise_for_status()
        log("Telegram message sent.")
    except Exception as error:
        log(f"Telegram error: {error}")


def get_symbols():
    url = f"{BASE_URL}/v1/symbols"

    response = session.get(
        url,
        params={"venue": "otc"},
        timeout=TIMEOUT
    )
    response.raise_for_status()

    data = response.json()
    symbols = data.get("symbols", [])

    # Forex-style six-letter currency symbols only
    result = []

    for item in symbols:
        symbol = str(item.get("symbol", "")).strip()
        name = str(item.get("name", "")).strip()

        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        if item.get("open") is False:
            continue

        result.append({
            "symbol": symbol,
            "name": name or symbol
        })

    return result


def get_candles(symbol, timeframe):
    url = f"{BASE_URL}/v1/candles"

    response = session.get(
        url,
        params={
            "venue": "otc",
            "symbol": symbol,
            "tf": timeframe,
            "limit": MAX_CANDLES
        },
        timeout=TIMEOUT
    )

    response.raise_for_status()
    data = response.json()

    candles = data.get("candles", data.get("data", []))

    if not candles:
        return None

    df = pd.DataFrame(candles)

    required = ["open", "high", "low", "close"]

    for column in required:
        if column not in df.columns:
            return None
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=required)

    if len(df) < 55:
        return None

    return df.reset_index(drop=True)


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - (100 / (1 + rs))


def analyze(symbol_data):
    symbol = symbol_data["symbol"]
    name = symbol_data["name"]

    try:
        m1 = get_candles(symbol, 60)
        m5 = get_candles(symbol, 300)

        if m1 is None or m5 is None:
            return None

        # Exclude the currently forming candle
        m1 = m1.iloc[:-1].copy()
        m5 = m5.iloc[:-1].copy()

        if len(m1) < 55 or len(m5) < 55:
            return None

        m1["ema9"] = ema(m1["close"], 9)
        m1["ema20"] = ema(m1["close"], 20)
        m1["ema50"] = ema(m1["close"], 50)
        m1["rsi"] = rsi(m1["close"])

        m5["ema20"] = ema(m5["close"], 20)
        m5["ema50"] = ema(m5["close"], 50)

        last = m1.iloc[-1]
        previous = m1.iloc[-2]
        trend = m5.iloc[-1]

        score_call = 0
        score_put = 0

        # M1 direction
        if last["ema9"] > last["ema20"]:
            score_call += 1

        if last["ema20"] > last["ema50"]:
            score_call += 1

        if last["ema9"] < last["ema20"]:
            score_put += 1

        if last["ema20"] < last["ema50"]:
            score_put += 1

        # M5 trend confirmation
        if trend["ema20"] > trend["ema50"]:
            score_call += 2

        if trend["ema20"] < trend["ema50"]:
            score_put += 2

        # Candle momentum
        if last["close"] > last["open"]:
            score_call += 1

        if last["close"] < last["open"]:
            score_put += 1

        # Previous candle confirmation
        if previous["close"] > previous["open"]:
            score_call += 1

        if previous["close"] < previous["open"]:
            score_put += 1

        # RSI filter
        current_rsi = float(last["rsi"])

        if 50 <= current_rsi <= 68:
            score_call += 1

        if 32 <= current_rsi <= 50:
            score_put += 1

        # Avoid extreme RSI
        if current_rsi > 75 or current_rsi < 25:
            return None

        # Avoid tiny candles and indecision
        candle_range = float(last["high"] - last["low"])

        if candle_range <= 0:
            return None

        body = abs(float(last["close"] - last["open"]))

        if body / candle_range < 0.25:
            return None

        # Select direction only when enough conditions agree
        if score_call >= 6 and score_call > score_put:
            direction = "CALL"
            score = score_call

        elif score_put >= 6 and score_put > score_call:
            direction = "PUT"
            score = score_put

        else:
            return None

        return {
            "symbol": symbol,
            "name": name,
            "direction": direction,
            "score": score,
            "rsi": current_rsi,
            "entry": float(last["close"]),
            "time": datetime.now(timezone.utc)
        }

    except Exception as error:
        log(f"SKIPPED {symbol}: {error}")
        return None


def format_signal(signal):
    local_time = signal["time"].astimezone(LOCAL_TZ)

    if signal["direction"] == "CALL":
        direction = "🟢 CALL"
        confirmation = "🟢 Bullish momentum"
    else:
        direction = "🔴 PUT"
        confirmation = "🔴 Bearish momentum"

    return (
        f"<b>🤖 {VERSION}</b>\n\n"
        f"<b>{direction}</b>\n"
        f"💱 PAIR: {signal['name']}\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n"
        f"📍 ENTRY REFERENCE: {signal['entry']:.6f}\n"
        f"📊 RSI: {signal['rsi']:.1f}\n"
        f"⭐ TECHNICAL SCORE: {signal['score']}/7\n"
        f"{confirmation}\n"
        f"🕒 FLORIDA TIME: {local_time.strftime('%I:%M:%S %p')}\n\n"
        f"💰 PAYOUT: ⚪ NOT VERIFIED\n"
        f"📈 WIN RATE: ⚪ NOT VERIFIED\n\n"
        f"<i>Manual signal only. Verify on demo first.</i>"
    )


def main():
    log("=" * 40)
    log(VERSION)
    log("FOREX OTC SCANNER")
    log("=" * 40)

    if not API_KEY or not TG_TOKEN or not TG_CHAT_ID:
        log("ERROR: GitHub Secrets missing.")
        return

    start = time.time()

    try:
        symbols = get_symbols()
    except Exception as error:
        log(f"Symbol API error: {error}")
        telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"🔴 API ERROR\n"
            f"Could not retrieve OTC pairs."
        )
        return

    log(f"Forex OTC pairs found: {len(symbols)}")

    if not symbols:
        telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"⚪ WAIT\n"
            f"No Forex OTC pairs found."
        )
        return

    # Scan pairs concurrently to reduce runtime
    from concurrent.futures import ThreadPoolExecutor, as_completed

    candidates = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(analyze, item)
            for item in symbols
        ]

        for future in as_completed(futures):
            result = future.result()

            if result:
                candidates.append(result)

    elapsed = time.time() - start

    log(f"Scanned: {len(symbols)}")
    log(f"Valid candidates: {len(candidates)}")
    log(f"Runtime: {elapsed:.1f} seconds")

    if not candidates:
        log("STATUS: WAIT")

        telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            f"⚪ <b>WAIT</b>\n"
            f"No valid setup found.\n\n"
            f"💱 Pairs scanned: {len(symbols)}\n"
            f"⏱ Runtime: {elapsed:.1f}s\n\n"
            f"<i>Waiting for a better opportunity.</i>"
        )
        return

    # One best signal only
    candidates.sort(
        key=lambda x: (
            x["score"],
            x["time"]
        ),
        reverse=True
    )

    best = candidates[0]

    log(
        f"BEST SIGNAL: {best['direction']} "
        f"{best['symbol']} SCORE {best['score']}"
    )

    telegram(format_signal(best))


if __name__ == "__main__":
    main()
