
import os
import re
import time
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "ROMARIO TRADER PRO 3.8"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.otcharts.com"
LOCAL_TZ = ZoneInfo("America/New_York")
TIMEOUT = 15
MAX_WORKERS = 12

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
})


def log(message):
    print(message, flush=True)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        },
        timeout=TIMEOUT
    )

    log(f"TELEGRAM STATUS: {response.status_code}")
    response.raise_for_status()


def get_symbols():
    response = session.get(
        f"{BASE_URL}/v1/symbols",
        params={"venue": "otc"},
        timeout=TIMEOUT
    )

    log(f"SYMBOL API STATUS: {response.status_code}")
    response.raise_for_status()

    data = response.json()
    result = []

    for item in data.get("symbols", []):
        symbol = str(item.get("symbol", "")).strip()

        if re.fullmatch(r"[A-Z]{6}_otc", symbol):
            if item.get("open") is not False:
                result.append({
                    "symbol": symbol,
                    "name": item.get("name", symbol)
                })

    return result


def get_candles(symbol, timeframe):
    response = session.get(
        f"{BASE_URL}/v1/candles",
        params={
            "venue": "otc",
            "symbol": symbol,
            "tf": timeframe,
            "limit": 100
        },
        timeout=TIMEOUT
    )

    response.raise_for_status()

    data = response.json()
    candles = data.get("candles", data.get("data", []))

    if not candles:
        return None

    df = pd.DataFrame(candles)

    for column in ["open", "high", "low", "close"]:
        if column not in df.columns:
            return None
        df[column] = pd.to_numeric(
            df[column], errors="coerce"
        )

    df = df.dropna()

    if len(df) < 55:
        return None

    return df.reset_index(drop=True)


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
        alpha=1/14, adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1/14, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))

    df["rsi"] = 100 - (100 / (1 + rs))

    return df


def analyze(item):
    symbol = item["symbol"]

    try:
        m1 = get_candles(symbol, 60)
        m5 = get_candles(symbol, 300)

        if m1 is None or m5 is None:
            return None

        # Ignore the currently forming candle
        m1 = add_indicators(m1.iloc[:-1].copy())
        m5 = add_indicators(m5.iloc[:-1].copy())

        if len(m1) < 55 or len(m5) < 55:
            return None

        last = m1.iloc[-1]
        previous = m1.iloc[-2]
        trend = m5.iloc[-1]

        rsi_value = float(last["rsi"])

        if pd.isna(rsi_value):
            return None

        buy = 0
        sell = 0

        # M1 trend
        if last["ema9"] > last["ema20"]:
            buy += 1
        if last["ema20"] > last["ema50"]:
            buy += 1

        if last["ema9"] < last["ema20"]:
            sell += 1
        if last["ema20"] < last["ema50"]:
            sell += 1

        # M5 confirmation
        if trend["ema20"] > trend["ema50"]:
            buy += 2
        if trend["ema20"] < trend["ema50"]:
            sell += 2

        # Candle direction
        if last["close"] > last["open"]:
            buy += 1
        if last["close"] < last["open"]:
            sell += 1

        if previous["close"] > previous["open"]:
            buy += 1
        if previous["close"] < previous["open"]:
            sell += 1

        # RSI confirmation
        if 50 <= rsi_value <= 68:
            buy += 1

        if 32 <= rsi_value <= 50:
            sell += 1

        # Avoid extreme conditions
        if rsi_value > 75 or rsi_value < 25:
            return None

        candle_range = last["high"] - last["low"]

        if candle_range <= 0:
            return None

        body = abs(last["close"] - last["open"])

        if body / candle_range < 0.25:
            return None

        if buy >= 6 and buy > sell:
            direction = "BUY"
            score = buy

        elif sell >= 6 and sell > buy:
            direction = "SELL"
            score = sell

        else:
            return None

        return {
            "symbol": symbol,
            "name": item["name"],
            "direction": direction,
            "score": score,
            "rsi": rsi_value,
            "entry": float(last["close"]),
            "time": datetime.now(timezone.utc)
        }

    except Exception as error:
        log(f"SKIPPED {symbol}: {error}")
        return None


def format_signal(signal, total):
    local_time = signal["time"].astimezone(LOCAL_TZ)

    if signal["direction"] == "BUY":
        title = "🟢 BUY / CALL"
        confirmation = "🟢 BULLISH TREND"
    else:
        title = "🔴 SELL / PUT"
        confirmation = "🔴 BEARISH TREND"

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"💱 PAIR: {signal['name']}\n"
        f"📊 SYMBOL: {signal['symbol']}\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n"
        f"📍 ENTRY REFERENCE: {signal['entry']:.6f}\n"
        f"📈 RSI: {signal['rsi']:.1f}\n"
        f"⭐ TECHNICAL SCORE: {signal['score']}/7\n"
        f"{confirmation}\n"
        f"🕒 FLORIDA TIME: {local_time:%I:%M:%S %p}\n\n"
        f"💰 PAYOUT: ⚪ NOT VERIFIED\n"
        f"📉 VERIFIED WIN RATE: ⚪ NOT AVAILABLE\n\n"
        f"💱 PAIRS ANALYZED: {total}\n"
        f"<i>Manual signal. Test on demo first.</i>"
    )


def main():
    log("=" * 40)
    log(VERSION)
    log("BUY / SELL FOREX OTC SCANNER")
    log("=" * 40)

    if not API_KEY or not TG_TOKEN or not TG_CHAT_ID:
        log("ERROR: Missing GitHub Secrets.")
        return

    start = time.time()

    try:
        symbols = get_symbols()
    except Exception as error:
        log(f"API ERROR: {error}")
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"🔴 API ERROR\n"
            f"Could not retrieve Forex OTC pairs."
        )
        return

    log(f"PAIRS FOUND: {len(symbols)}")

    candidates = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = [
            executor.submit(analyze, item)
            for item in symbols
        ]

        for future in as_completed(futures):
            result = future.result()

            if result:
                candidates.append(result)

    elapsed = time.time() - start

    log(f"VALID SIGNALS: {len(candidates)}")
    log(f"RUNTIME: {elapsed:.1f} seconds")

    if not candidates:
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            f"⚪ <b>WAIT</b>\n"
            f"No confirmed BUY or SELL setup.\n\n"
            f"💱 PAIRS SCANNED: {len(symbols)}\n"
            f"⏱ RUNTIME: {elapsed:.1f}s"
        )
        return

    candidates.sort(
        key=lambda x: (x["score"], x["time"]),
        reverse=True
    )

    best = candidates[0]

    log(
        f"SELECTED: {best['direction']} "
        f"{best['symbol']} SCORE {best['score']}"
    )

    send_telegram(format_signal(best, len(symbols)))


if __name__ == "__main__":
    main()
