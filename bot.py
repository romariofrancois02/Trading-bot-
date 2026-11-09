
import os
import re
import time
import html
import requests
import pandas as pd

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "ROMARIO TRADER PRO 4.3"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
LOCAL_TZ = ZoneInfo("America/New_York")

TIMEOUT = 20
MAX_WORKERS = 8
MIN_PREP_SECONDS = 30
MAX_SIGNAL_DELAY = 2
MIN_PAYOUT = 80.0

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


def api_get(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=TIMEOUT
    )

    log(f"API {path}: HTTP {response.status_code}")

    if not response.ok:
        log(response.text[:500])
        response.raise_for_status()

    return response.json()


def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise ValueError("Telegram secrets missing")

    response = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        },
        timeout=TIMEOUT
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram delivery not confirmed")

    return result


def get_payout(symbol):
    """
    Fail closed: no payout is accepted unless a verified
    payout endpoint and response field are configured.

    Do not invent an endpoint or assume payout from a symbol.
    """
    payout_endpoint = os.getenv("OTCHARTS_PAYOUT_ENDPOINT")
    payout_field = os.getenv("OTCHARTS_PAYOUT_FIELD")

    if not payout_endpoint or not payout_field:
        log(f"PAYOUT NOT CONFIGURED: {symbol}")
        return None

    try:
        data = api_get(
            payout_endpoint,
            {"venue": "otc", "symbol": symbol}
        )

        value = data

        for key in payout_field.split("."):
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]

        payout = float(value)

        if 0 < payout <= 1:
            payout *= 100

        if not 0 < payout <= 100:
            return None

        return payout

    except Exception as error:
        log(f"PAYOUT ERROR {symbol}: {error}")
        return None


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

        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        if symbol[:3] not in CURRENCIES:
            continue

        if symbol[3:6] not in CURRENCIES:
            continue

        if item.get("open") is False:
            continue

        result.append({
            "symbol": symbol,
            "name": str(item.get("name", symbol))
        })

    log(f"FOREX OTC PAIRS FOUND: {len(result)}")
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

    candles = data.get("candles", data.get("data", []))

    if not isinstance(candles, list) or not candles:
        return None

    df = pd.DataFrame(candles)

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            return None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])

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

    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    return df


def analyze(item):
    symbol = item["symbol"]

    try:
        payout = get_payout(symbol)

        if payout is None or payout < MIN_PAYOUT:
            log(f"SKIP {symbol}: payout unavailable or below 80%")
            return None

        m1 = get_candles(symbol, 60)
        m5 = get_candles(symbol, 300)

        if m1 is None or m5 is None:
            return None

        m1 = indicators(m1.iloc[:-1].copy())
        m5 = indicators(m5.iloc[:-1].copy())

        if len(m1) < 55 or len(m5) < 55:
            return None

        last = m1.iloc[-1]
        previous = m1.iloc[-2]
        trend = m5.iloc[-1]

        rsi = float(last["rsi"])

        if pd.isna(rsi) or rsi > 75 or rsi < 25:
            return None

        candle_range = float(last["high"] - last["low"])

        if candle_range <= 0:
            return None

        body = abs(float(last["close"] - last["open"]))

        if body / candle_range < 0.25:
            return None

        buy = 0
        sell = 0

        if last["ema9"] > last["ema20"]:
            buy += 1
        if last["ema20"] > last["ema50"]:
            buy += 1

        if last["ema9"] < last["ema20"]:
            sell += 1
        if last["ema20"] < last["ema50"]:
            sell += 1

        if trend["ema20"] > trend["ema50"]:
            buy += 2
        elif trend["ema20"] < trend["ema50"]:
            sell += 2

        if last["close"] > last["open"]:
            buy += 1
        elif last["close"] < last["open"]:
            sell += 1

        if previous["close"] > previous["open"]:
            buy += 1
        elif previous["close"] < previous["open"]:
            sell += 1

        if 50 <= rsi <= 68:
            buy += 1
        elif 32 <= rsi <= 50:
            sell += 1

        if buy >= 6 and buy > sell:
            direction, score = "BUY", buy
        elif sell >= 6 and sell > buy:
            direction, score = "SELL", sell
        else:
            return None

        return {
            "symbol": symbol,
            "name": item["name"],
            "direction": direction,
            "score": score,
            "rsi": rsi,
            "payout": payout,
            "reference": float(last["close"]),
            "analyzed_at": utc_now()
        }

    except Exception as error:
        log(f"PAIR ERROR {symbol}: {error}")
        return None


def schedule_entry(now):
    earliest = now + timedelta(seconds=MIN_PREP_SECONDS)

    entry = now.replace(second=0, microsecond=0) + timedelta(minutes=1)

    while entry < earliest:
        entry += timedelta(minutes=1)

    return entry - timedelta(seconds=MIN_PREP_SECONDS), entry


def signal_message(signal, total, signal_time, entry_time):
    expiry = entry_time + timedelta(seconds=60)

    signal_local = signal_time.astimezone(LOCAL_TZ)
    entry_local = entry_time.astimezone(LOCAL_TZ)
    expiry_local = expiry.astimezone(LOCAL_TZ)

    if signal["direction"] == "BUY":
        title = "🟢 BUY / CALL"
        trend = "BULLISH"
    else:
        title = "🔴 SELL / PUT"
        trend = "BEARISH"

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"💱 PAIR: {html.escape(signal['name'])}\n"
        f"📊 SYMBOL: {signal['symbol']}\n"
        f"📈 TREND: {trend}\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🕒 SIGNAL TIME: {signal_local:%I:%M:%S %p}\n"
        f"🟢 <b>ENTRY TIME: {entry_local:%I:%M:%S %p}</b>\n"
        f"⏰ PREPARATION: 30 SECONDS\n"
        f"🏁 EXPIRATION: {expiry_local:%I:%M:%S %p}\n\n"
        f"📍 REFERENCE PRICE: {signal['reference']:.6f}\n"
        f"📊 RSI: {signal['rsi']:.1f}\n"
        f"⭐ TECHNICAL SCORE: {signal['score']}/7\n"
        f"💰 VERIFIED PAYOUT: {signal['payout']:.1f}%\n"
        f"💱 PAIRS SCANNED: {total}\n"
        f"📉 HISTORICAL WIN RATE: NOT AVAILABLE\n\n"
        f"<i>Manual signal only. Demo testing recommended.</i>"
    )


def main():
    started = time.time()

    log("=" * 45)
    log(VERSION)
    log("MINIMUM VERIFIED PAYOUT: 80%")
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

        symbols = get_symbols()

    except Exception as error:
        log(f"STARTUP ERROR: {error}")
        try:
            send_telegram(
                f"🤖 <b>{VERSION}</b>\n"
                f"🔴 API ERROR\n"
                f"<code>{html.escape(str(error)[:400])}</code>"
            )
        except Exception:
            pass
        return

    if not symbols:
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"⚪ WAIT\nNo recognized Forex OTC pairs."
        )
        return

    candidates = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(analyze, item) for item in symbols]

        for future in as_completed(futures):
            result = future.result()
            if result:
                candidates.append(result)

    elapsed = time.time() - started

    log(f"PAIRS FOUND: {len(symbols)}")
    log(f"VALID CANDIDATES: {len(candidates)}")
    log(f"ANALYSIS TIME: {elapsed:.1f}s")

    if not candidates:
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            f"⚪ <b>WAIT</b>\n"
            f"No signal with verified payout of at least 80%.\n"
            f"💱 PAIRS SCANNED: {len(symbols)}"
        )
        return

    candidates.sort(
        key=lambda x: (x["score"], x["payout"], x["analyzed_at"]),
        reverse=True
    )

    best = candidates[0]

    signal_time, entry_time = schedule_entry(utc_now())

    wait_seconds = (signal_time - utc_now()).total_seconds()

    if wait_seconds > 0:
        log(f"WAITING {wait_seconds:.1f}s")
        time.sleep(wait_seconds)

    actual_send = utc_now()
    delay = (actual_send - signal_time).total_seconds()
    remaining = (entry_time - actual_send).total_seconds()

    if delay > MAX_SIGNAL_DELAY or remaining < 28:
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"⚪ <b>WAIT</b>\n"
            f"Signal timing missed. No late entry."
        )
        return

    send_telegram(
        signal_message(best, len(symbols), signal_time, entry_time)
    )

    log("SIGNAL SENT.")


if __name__ == "__main__":
    main()
