
import os
import re
import time
import html
import requests
import pandas as pd

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "ROMARIO TRADER PRO 4.0"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
LOCAL_TZ = ZoneInfo("America/New_York")

TIMEOUT = 20
MAX_WORKERS = 8

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
})


def log(message):
    print(message, flush=True)


def utc_now():
    return datetime.now(timezone.utc)


def next_minute(dt):
    return (
        dt.replace(second=0, microsecond=0)
        + timedelta(minutes=1)
    )


def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise ValueError("Telegram secrets missing")

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


def api_get(path, params=None):
    url = BASE_URL + path

    response = session.get(
        url,
        params=params,
        timeout=TIMEOUT
    )

    log(f"API URL: {response.url}")
    log(f"API STATUS: {response.status_code}")

    if not response.ok:
        log(f"API BODY: {response.text[:1000]}")
        response.raise_for_status()

    return response.json()


def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    symbols = data.get("symbols", [])

    if not isinstance(symbols, list):
        raise ValueError("Invalid symbols response")

    result = []

    for item in symbols:
        symbol = str(item.get("symbol", "")).strip()

        # Forex currency pairs only
        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        if item.get("open") is False:
            continue

        result.append({
            "symbol": symbol,
            "name": str(item.get("name", symbol))
        })

    log(f"FOREX OTC PAIRS: {len(result)}")

    return result


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

    candles = data.get(
        "candles",
        data.get("data", [])
    )

    if not isinstance(candles, list) or not candles:
        return None

    df = pd.DataFrame(candles)

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            return None

        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df = df.dropna(
        subset=["open", "high", "low", "close"]
    )

    if len(df) < 56:
        return None

    return df.reset_index(drop=True)


def indicators(df):
    df = df.copy()

    for period in [9, 20, 50]:
        df[f"ema{period}"] = df["close"].ewm(
            span=period,
            adjust=False
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

    rs = avg_gain / avg_loss.replace(
        0,
        float("nan")
    )

    df["rsi"] = 100 - (100 / (1 + rs))

    return df


def analyze(item):
    symbol = item["symbol"]

    try:
        m1 = get_candles(symbol, 60)
        m5 = get_candles(symbol, 300)

        if m1 is None or m5 is None:
            return None

        # Keep completed candles only.
        # This preserves the data handling used in PRO 3.9.
        m1 = indicators(m1.iloc[:-1].copy())
        m5 = indicators(m5.iloc[:-1].copy())

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

        # M1 EMA trend
        if last["ema9"] > last["ema20"]:
            buy += 1

        if last["ema20"] > last["ema50"]:
            buy += 1

        if last["ema9"] < last["ema20"]:
            sell += 1

        if last["ema20"] < last["ema50"]:
            sell += 1

        # M5 trend confirmation
        if trend["ema20"] > trend["ema50"]:
            buy += 2

        elif trend["ema20"] < trend["ema50"]:
            sell += 2

        # Latest completed candle momentum
        if last["close"] > last["open"]:
            buy += 1

        elif last["close"] < last["open"]:
            sell += 1

        # Previous candle momentum
        if previous["close"] > previous["open"]:
            buy += 1

        elif previous["close"] < previous["open"]:
            sell += 1

        # RSI filter
        if 50 <= rsi_value <= 68:
            buy += 1

        elif 32 <= rsi_value <= 50:
            sell += 1

        if rsi_value > 75 or rsi_value < 25:
            return None

        candle_range = float(
            last["high"] - last["low"]
        )

        if candle_range <= 0:
            return None

        body = abs(
            float(last["close"] - last["open"])
        )

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

        # Record when this pair's analysis finished.
        analyzed_at = utc_now()

        # Entry is the next full minute after analysis.
        entry_time = next_minute(analyzed_at)

        return {
            "symbol": symbol,
            "name": item["name"],
            "direction": direction,
            "score": score,
            "rsi": rsi_value,
            "reference": float(last["close"]),
            "analyzed_at": analyzed_at,
            "entry_time": entry_time
        }

    except Exception as error:
        log(
            f"PAIR ERROR {symbol}: "
            f"{type(error).__name__}: {error}"
        )

        return None


def signal_message(signal, total):
    entry_utc = signal["entry_time"]
    expiry_utc = entry_utc + timedelta(seconds=60)

    entry_local = entry_utc.astimezone(LOCAL_TZ)
    expiry_local = expiry_utc.astimezone(LOCAL_TZ)

    signal_local = utc_now().astimezone(LOCAL_TZ)

    if signal["direction"] == "BUY":
        title = "🟢 BUY / CALL"
        trend = "BULLISH"
    else:
        title = "🔴 SELL / PUT"
        trend = "BEARISH"

    safe_name = html.escape(signal["name"])

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"💱 PAIR: {safe_name}\n"
        f"📊 SYMBOL: {signal['symbol']}\n"
        f"📈 TREND: {trend}\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🕒 SIGNAL SENT: "
        f"{signal_local:%I:%M:%S %p}\n"
        f"🟢 <b>ENTRY TIME: "
        f"{entry_local:%I:%M:%S %p}</b>\n"
        f"⏰ ENTRY WINDOW: "
        f"{entry_local:%I:%M %p} - "
        f"{expiry_local:%I:%M:%S %p}\n"
        f"🏁 EXPIRATION TIME: "
        f"{expiry_local:%I:%M:%S %p}\n\n"
        f"📍 LAST CLOSED PRICE REFERENCE: "
        f"{signal['reference']:.6f}\n"
        f"📊 RSI: {signal['rsi']:.1f}\n"
        f"⭐ TECHNICAL SCORE: "
        f"{signal['score']}/7\n"
        f"💱 PAIRS SCANNED: {total}\n\n"
        f"💰 PAYOUT: ⚪ NOT VERIFIED\n"
        f"📉 VERIFIED WIN RATE: "
        f"⚪ NOT AVAILABLE\n\n"
        f"<i>Manual signal only. "
        f"Demo test recommended.</i>"
    )


def wait_until_entry(entry_time):
    log(
        "WAITING FOR ENTRY: "
        + entry_time.astimezone(LOCAL_TZ).strftime(
            "%I:%M:%S %p"
        )
    )

    while True:
        remaining = (
            entry_time - utc_now()
        ).total_seconds()

        if remaining <= 0:
            break

        time.sleep(min(0.2, remaining))

    log("ENTRY TIME REACHED.")


def main():
    start = time.time()

    log("=" * 45)
    log(VERSION)
    log("NEXT M1 CANDLE ENTRY SCANNER")
    log("=" * 45)

    if not API_KEY:
        log("ERROR: OTCHARTS_API_KEY missing")
        return

    if not TG_TOKEN or not TG_CHAT_ID:
        log("ERROR: Telegram secrets missing")
        return

    try:
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"🟡 ANALYZING FOREX OTC MARKETS..."
        )

    except Exception as error:
        log(f"TELEGRAM ERROR: {error}")
        return

    try:
        symbols = get_symbols()

    except Exception as error:
        log(f"SYMBOL API ERROR: {error}")

        send_telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"🔴 <b>OTCHARTS API ERROR</b>\n"
            f"<code>{html.escape(str(error)[:500])}</code>"
        )

        return

    if not symbols:
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"⚪ WAIT\n"
            f"No open Forex OTC pairs found."
        )

        return

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

    log(f"PAIRS FOUND: {len(symbols)}")
    log(f"VALID CANDIDATES: {len(candidates)}")
    log(f"TOTAL TIME: {elapsed:.1f}s")

    if not candidates:
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            f"⚪ <b>WAIT</b>\n"
            f"No confirmed BUY or SELL setup.\n\n"
            f"💱 PAIRS SCANNED: {len(symbols)}\n"
            f"⏱ ANALYSIS: {elapsed:.1f}s"
        )

        return

    # Highest score first, then newest analysis.
    candidates.sort(
        key=lambda x: (
            x["score"],
            x["analyzed_at"]
        ),
        reverse=True
    )

    # Never send a signal whose entry minute has passed.
    now = utc_now()

    valid_candidates = [
        item for item in candidates
        if item["entry_time"] > now
    ]

    if not valid_candidates:
        log("ALL SIGNAL ENTRY WINDOWS EXPIRED.")

        send_telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            f"⚪ <b>WAIT</b>\n"
            f"Scan finished too late for the next "
            f"M1 entry window.\n"
            f"No expired signal will be sent.\n\n"
            f"💱 PAIRS SCANNED: {len(symbols)}\n"
            f"⏱ ANALYSIS: {elapsed:.1f}s"
        )

        return

    best = valid_candidates[0]

    log(
        f"SELECTED: {best['direction']} "
        f"{best['symbol']} "
        f"SCORE {best['score']}"
    )

    log(
        "ENTRY TIME: "
        + best["entry_time"].astimezone(
            LOCAL_TZ
        ).strftime("%I:%M:%S %p")
    )

    # Wait until the next full M1 minute.
    wait_until_entry(best["entry_time"])

    # Do not send if the target minute was missed.
    if utc_now() >= (
        best["entry_time"] + timedelta(seconds=60)
    ):
        log("ENTRY WINDOW EXPIRED. SIGNAL CANCELLED.")
        return

    send_telegram(
        signal_message(best, len(symbols))
    )

    log("SIGNAL SENT SUCCESSFULLY.")


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        log(
            f"FATAL ERROR: "
            f"{type(error).__name__}: {error}"
        )

        raise
