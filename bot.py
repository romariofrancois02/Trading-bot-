
import os
import re
import time
import html
import requests
import pandas as pd

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "ROMARIO TRADER PRO 6.1"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
LOCAL_TZ = ZoneInfo("America/New_York")

TIMEOUT = 20
MAX_WORKERS = 8

MIN_PREP_SECONDS = 30
MAX_SIGNAL_DELAY = 2

# 92% minimum payout
MIN_PAYOUT = 0.92

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


def florida_time(dt=None):
    if dt is None:
        dt = utc_now()
    return dt.astimezone(LOCAL_TZ)


def api_get(path, params=None):
    url = BASE_URL + path
    response = session.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def extract_list(data, possible_keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in possible_keys:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def normalize_payout(raw):
    if raw is None:
        return None

    try:
        value = float(str(raw).replace("%", "").strip())

        if value > 1:
            value /= 100.0

        if value <= 0 or value > 1:
            return None

        return value

    except (ValueError, TypeError):
        return None


def get_symbols():
    data = api_get("/v1/symbols", {"venue": "otc"})

    symbols = extract_list(data, ["symbols", "data"])

    if not symbols:
        log("WARNING: No symbols returned by OTCharts.")
        if isinstance(data, dict):
            log(f"API response keys: {list(data.keys())}")
        return []

    result = []

    for item in symbols:
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", "")).strip()

        if not re.fullmatch(
            r"[A-Z]{6}[-_]otc",
            symbol,
            re.IGNORECASE
        ):
            continue

        base = symbol[:3].upper()
        quote = symbol[3:6].upper()

        if base not in CURRENCIES or quote not in CURRENCIES:
            continue

        if item.get("open") is False:
            continue

        # Never invent a payout if API does not provide one.
        raw_payout = item.get("payout")

        if raw_payout is None:
            raw_payout = item.get("profit")

        payout = normalize_payout(raw_payout)

        if payout is None:
            continue

        if payout < MIN_PAYOUT:
            continue

        result.append({
            "symbol": symbol,
            "name": str(item.get("name", symbol)),
            "payout": payout
        })

    log(f"FOREX OTC PAIRS WITH PAYOUT >= 92%: {len(result)}")

    return result


def get_candles(symbol, timeframe):
    data = api_get("/v1/candles", {
        "venue": "otc",
        "symbol": symbol,
        "tf": timeframe,
        "limit": 100
    })

    candles = extract_list(data, ["candles", "data"])

    if not candles:
        return None

    df = pd.DataFrame(candles)

    required = ["open", "high", "low", "close"]

    for col in required:
        if col not in df.columns:
            log(f"{symbol}: Missing candle field {col}")
            return None

        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required).reset_index(drop=True)

    if len(df) < 60:
        return None

    return df


def calculate_indicators(df):
    df = df.copy()

    # EMA 50
    df["ema50"] = df["close"].ewm(
        span=50,
        adjust=False
    ).mean()

    # Bollinger Bands: 20 / 2
    df["bb_mid"] = df["close"].rolling(20).mean()

    bb_std = df["close"].rolling(20).std()

    df["bb_upper"] = df["bb_mid"] + (bb_std * 2)
    df["bb_lower"] = df["bb_mid"] - (bb_std * 2)

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

    rs = avg_gain / avg_loss.replace(0, float("nan"))

    df["rsi"] = 100 - (100 / (1 + rs))

    return df


def analyze(item):
    symbol = item["symbol"]

    try:
        raw = get_candles(symbol, 60)

        if raw is None:
            return None

        # Retain the original strategy's approach:
        # assume the final API row is the currently forming candle.
        # This depends on OTCharts returning candles oldest to newest.
        closed = raw.iloc[:-1].copy()

        if len(closed) < 55:
            return None

        df = calculate_indicators(closed)

        last = df.iloc[-1]

        values = [
            last["open"],
            last["high"],
            last["low"],
            last["close"],
            last["rsi"],
            last["ema50"],
            last["bb_upper"],
            last["bb_lower"]
        ]

        if any(pd.isna(value) for value in values):
            return None

        open_price = float(last["open"])
        high = float(last["high"])
        low = float(last["low"])
        close = float(last["close"])
        rsi = float(last["rsi"])
        ema50 = float(last["ema50"])
        upper = float(last["bb_upper"])
        lower = float(last["bb_lower"])

        # Original 3-step strategy.
        # PUT: Upper Band + RSI >= 70 + price below EMA50.
        put_checks = [
            high >= upper,
            rsi >= 70,
            close < ema50
        ]

        # CALL: Lower Band + RSI <= 30 + price above EMA50.
        call_checks = [
            low <= lower,
            rsi <= 30,
            close > ema50
        ]

        put_score = sum(bool(x) for x in put_checks)
        call_score = sum(bool(x) for x in call_checks)

        # Select the strongest direction.
        if put_score > call_score:
            direction = "SELL"
            score = put_score

        elif call_score > put_score:
            direction = "BUY"
            score = call_score

        else:
            # Tie-breaker: use EMA50 direction.
            # This is a fallback, not a confirmed 3/3 setup.
            if close > ema50:
                direction = "BUY"
                score = call_score
            else:
                direction = "SELL"
                score = put_score

        return {
            "symbol": symbol,
            "name": item["name"],
            "payout": item["payout"],
            "direction": direction,
            "score": score,
            "rsi": rsi,
            "reference": close,
            "ema50": ema50,
            "upper": upper,
            "lower": lower,
            "analyzed_at": utc_now()
        }

    except Exception as error:
        log(
            f"PAIR ERROR {symbol}: "
            f"{type(error).__name__}: {error}"
        )
        return None


def schedule_entry(now):
    earliest_entry = now + timedelta(seconds=MIN_PREP_SECONDS)

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


def signal_message(signal, total, signal_time, entry_time):
    expiry_time = entry_time + timedelta(minutes=1)

    signal_local = florida_time(signal_time)
    entry_local = florida_time(entry_time)
    expiry_local = florida_time(expiry_time)

    if signal["direction"] == "BUY":
        title = "🟢 CALL / BUY"
        setup = "Lower Band + RSI Oversold + Above EMA50"
    else:
        title = "🔴 PUT / SELL"
        setup = "Upper Band + RSI Overbought + Below EMA50"

    score = signal["score"]

    if score == 3:
        quality = "3/3 - FULL CONFIRMATION"
    elif score == 2:
        quality = "2/3 - PARTIAL CONFIRMATION"
    elif score == 1:
        quality = "1/3 - WEAK SETUP"
    else:
        quality = "0/3 - FALLBACK DIRECTION"

    safe_name = html.escape(signal["name"])
    payout_percent = round(signal["payout"] * 100)

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"💱 PAIR: {safe_name}\n"
        f"📊 SYMBOL: {signal['symbol']}\n"
        f"📈 STRATEGY: BB + RSI + EMA50\n"
        f"🧩 SETUP: {setup}\n\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"⭐ SETUP SCORE: {quality}\n"
        f"📊 RSI: {signal['rsi']:.1f}\n"
        f"💰 BROKER PAYOUT: {payout_percent}%\n"
        f"📍 REFERENCE PRICE: {signal['reference']:.6f}\n\n"
        f"🕒 SIGNAL TIME: {signal_local:%I:%M:%S %p}\n"
        f"🟢 <b>ENTRY TIME: {entry_local:%I:%M:%S %p}</b>\n"
        f"🏁 EXPIRATION: {expiry_local:%I:%M:%S %p}\n\n"
        f"🌎 TIMEZONE: FLORIDA\n"
        f"🔎 QUALIFIED PAIRS: {total}\n\n"
        f"<i>⚠️ Best available candidate. "
        f"Score is not a win probability. "
        f"No Martingale.</i>"
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

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm message delivery")

    return result


def main():
    start = time.time()

    log("=" * 50)
    log(VERSION)
    log("BOLLINGER BANDS + RSI + EMA50")
    log("MINIMUM PAYOUT: 92%")
    log("=" * 50)

    if not API_KEY or not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Missing GitHub Secrets")

    try:
        symbols = get_symbols()
    except Exception as error:
        log(f"SYMBOL API ERROR: {error}")
        raise

    if not symbols:
        log("No Forex OTC pairs with verified payout >= 92%.")
        return

    candidates = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(analyze, item)
            for item in symbols
        ]

        for future in as_completed(futures):
            try:
                result = future.result()

                if result:
                    candidates.append(result)

            except Exception as error:
                log(f"WORKER ERROR: {error}")

    elapsed = time.time() - start

    log(f"PAIRS SCANNED: {len(symbols)}")
    log(f"VALID CANDIDATES: {len(candidates)}")
    log(f"SCAN TIME: {elapsed:.1f} seconds")

    if not candidates:
        log("No usable candle data. Cannot safely create a signal.")
        return

    # Highest score first, then highest payout.
    candidates.sort(
        key=lambda x: (
            x["score"],
            x["payout"],
            x["analyzed_at"]
        ),
        reverse=True
    )

    best = candidates[0]

    log(
        f"SELECTED: {best['symbol']} | "
        f"{best['direction']} | "
        f"SCORE {best['score']}/3 | "
        f"PAYOUT {best['payout'] * 100:.1f}%"
    )

    signal_time, entry_time = schedule_entry(utc_now())

    wait_seconds = (
        signal_time - utc_now()
    ).total_seconds()

    if wait_seconds > 0:
        log(f"Waiting {wait_seconds:.1f}s for signal time.")
        time.sleep(wait_seconds)

    actual_send_time = utc_now()

    delay = (
        actual_send_time - signal_time
    ).total_seconds()

    remaining = (
        entry_time - actual_send_time
    ).total_seconds()

    if delay > MAX_SIGNAL_DELAY or remaining < 28:
        log("Signal timing missed. Skipping stale entry.")
        return

    message = signal_message(
        best,
        len(symbols),
        signal_time,
        entry_time
    )

    send_telegram(message)

    log("SIGNAL SENT SUCCESSFULLY.")
    log(f"TOTAL RUNTIME: {time.time() - start:.1f}s")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"FATAL ERROR: {type(error).__name__}: {error}")
        raise
