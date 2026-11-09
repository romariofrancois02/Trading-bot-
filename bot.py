
import os
import re
import time
import html
import requests
import pandas as pd

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "ROMARIO TRADER PRO 4.2"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
LOCAL_TZ = ZoneInfo("America/New_York")

TIMEOUT = 20
MAX_WORKERS = 8
MIN_PREP_SECONDS = 30
MAX_SIGNAL_DELAY = 2

# ISO currency codes: excludes commodities such as XNG (Natural Gas).
CURRENCIES = set("""
USD EUR GBP JPY CHF CAD AUD NZD
CNY HKD SGD MXN ZAR TRY NOK SEK DKK
PLN HUF CZK ILS THB IDR MYR PHP INR
KRW TWD AED SAR QAR KWD BHD OMR
CLP COP ARS PEN EGP TND NGN PKR
BDT VND UAH RON BGN ISK HRK RUB
KES GHS MAD JOD LKR NPR
""".split())

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
})


def log(message):
    print(message, flush=True)


def utc_now():
    return datetime.now(timezone.utc)


def schedule_entry(now):
    """
    Select an M1 minute opening at least 30 seconds
    after analysis is complete.

    Signal is scheduled for exactly 30 seconds
    before that entry time.
    """
    earliest_entry = now + timedelta(
        seconds=MIN_PREP_SECONDS
    )

    entry = now.replace(
        second=0,
        microsecond=0
    ) + timedelta(minutes=1)

    while entry < earliest_entry:
        entry += timedelta(minutes=1)

    signal_time = entry - timedelta(
        seconds=MIN_PREP_SECONDS
    )

    return signal_time, entry


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

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram did not confirm message delivery"
        )

    return result


def api_get(path, params=None):
    url = BASE_URL + path

    response = session.get(
        url,
        params=params,
        timeout=TIMEOUT
    )

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
        raise ValueError(
            "Invalid OTCharts symbols response"
        )

    result = []

    for item in symbols:
        symbol = str(
            item.get("symbol", "")
        ).strip()

        if not re.fullmatch(
            r"[A-Z]{6}_otc",
            symbol
        ):
            continue

        base = symbol[:3]
        quote = symbol[3:6]

        # Both sides must be recognized currencies.
        if base not in CURRENCIES:
            continue

        if quote not in CURRENCIES:
            continue

        if item.get("open") is False:
            continue

        result.append({
            "symbol": symbol,
            "name": str(
                item.get("name", symbol)
            )
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

    df["rsi"] = 100 - (
        100 / (1 + rs)
    )

    return df


def analyze(item):
    symbol = item["symbol"]

    try:
        m1 = get_candles(symbol, 60)
        m5 = get_candles(symbol, 300)

        if m1 is None or m5 is None:
            return None

        # Use completed candles only.
        m1 = indicators(
            m1.iloc[:-1].copy()
        )

        m5 = indicators(
            m5.iloc[:-1].copy()
        )

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

        # M1 EMA direction.
        if last["ema9"] > last["ema20"]:
            buy += 1

        if last["ema20"] > last["ema50"]:
            buy += 1

        if last["ema9"] < last["ema20"]:
            sell += 1

        if last["ema20"] < last["ema50"]:
            sell += 1

        # M5 trend confirmation.
        if trend["ema20"] > trend["ema50"]:
            buy += 2

        elif trend["ema20"] < trend["ema50"]:
            sell += 2

        # Latest completed candle momentum.
        if last["close"] > last["open"]:
            buy += 1

        elif last["close"] < last["open"]:
            sell += 1

        # Previous candle momentum.
        if previous["close"] > previous["open"]:
            buy += 1

        elif previous["close"] < previous["open"]:
            sell += 1

        # RSI confirmation.
        if 50 <= rsi_value <= 68:
            buy += 1

        elif 32 <= rsi_value <= 50:
            sell += 1

        # Avoid extreme RSI.
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

        return {
            "symbol": symbol,
            "name": item["name"],
            "direction": direction,
            "score": score,
            "rsi": rsi_value,
            "reference": float(last["close"]),
            "analyzed_at": utc_now()
        }

    except Exception as error:
        log(
            f"PAIR ERROR {symbol}: "
            f"{type(error).__name__}: {error}"
        )

        return None


def signal_message(
    signal,
    total,
    signal_time,
    entry_time
):
    expiry_time = entry_time + timedelta(
        seconds=60
    )

    signal_local = signal_time.astimezone(
        LOCAL_TZ
    )

    entry_local = entry_time.astimezone(
        LOCAL_TZ
    )

    expiry_local = expiry_time.astimezone(
        LOCAL_TZ
    )

    if signal["direction"] == "BUY":
        title = "🟢 BUY / CALL"
        trend = "BULLISH"
    else:
        title = "🔴 SELL / PUT"
        trend = "BEARISH"

    safe_name = html.escape(
        signal["name"]
    )

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"💱 PAIR: {safe_name}\n"
        f"📊 SYMBOL: {signal['symbol']}\n"
        f"📈 TREND: {trend}\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🕒 SIGNAL TIME: "
        f"{signal_local:%I:%M:%S %p}\n"
        f"🟢 <b>ENTRY TIME: "
        f"{entry_local:%I:%M:%S %p}</b>\n"
        f"⏰ PREPARATION TIME: "
        f"{MIN_PREP_SECONDS} SECONDS\n"
        f"🏁 EXPIRATION TIME: "
        f"{expiry_local:%I:%M:%S %p}\n\n"
        f"📍 LAST CLOSED PRICE REFERENCE: "
        f"{signal['reference']:.6f}\n"
        f"📊 RSI: {signal['rsi']:.1f}\n"
        f"⭐ TECHNICAL SCORE: "
        f"{signal['score']}/7\n"
        f"💱 PAIRS SCANNED: {total}\n\n"
        f"📉 HISTORICAL WIN RATE: "
        f"⚪ NOT AVAILABLE\n"
        f"💰 PAYOUT: ⚪ NOT VERIFIED\n\n"
        f"<i>Manual signal only. "
        f"Demo testing recommended.</i>"
    )


def main():
    start = time.time()

    log("=" * 45)
    log(VERSION)
    log("SCHEDULED SIGNAL: 30 SECONDS BEFORE M1")
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
            f"No recognized open Forex OTC pairs found."
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

    # Select strongest candidate.
    candidates.sort(
        key=lambda x: (
            x["score"],
            x["analyzed_at"]
        ),
        reverse=True
    )

    best = candidates[0]

    # Schedule the next M1 minute with at least
    # 30 seconds preparation after analysis.
    schedule_time = utc_now()

    signal_time, entry_time = schedule_entry(
        schedule_time
    )

    log(
        f"SELECTED: {best['direction']} "
        f"{best['symbol']} SCORE {best['score']}"
    )

    log(
        "SCHEDULED SIGNAL TIME: "
        + signal_time.astimezone(
            LOCAL_TZ
        ).strftime("%I:%M:%S %p")
    )

    log(
        "ENTRY TIME: "
        + entry_time.astimezone(
            LOCAL_TZ
        ).strftime("%I:%M:%S %p")
    )

    # Wait until exactly 30 seconds before entry.
    wait_seconds = (
        signal_time - utc_now()
    ).total_seconds()

    if wait_seconds > 0:
        log(
            f"WAITING {wait_seconds:.1f} "
            f"SECONDS BEFORE TELEGRAM SIGNAL"
        )

        time.sleep(wait_seconds)

    # Do not send a late signal.
    actual_send_time = utc_now()

    delay = (
        actual_send_time - signal_time
    ).total_seconds()

    remaining = (
        entry_time - actual_send_time
    ).total_seconds()

    log(f"SIGNAL SCHEDULE DELAY: {delay:.2f}s")
    log(f"ENTRY COUNTDOWN: {remaining:.2f}s")

    if delay > MAX_SIGNAL_DELAY or remaining < 28:
        log("SIGNAL MISSED. NOT SENDING LATE ENTRY.")

        send_telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"⚪ <b>WAIT</b>\n\n"
            f"Signal timing was missed.\n"
            f"No late entry recommended."
        )

        return

    message = signal_message(
        best,
        len(symbols),
        signal_time,
        entry_time
    )

    send_telegram(message)

    log("SIGNAL SENT.")
    log("BOT FINISHED.")


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        log(
            f"FATAL ERROR: "
            f"{type(error).__name__}: {error}"
        )
        raise
