
import os
import re
import time
import html
import requests
import pandas as pd

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "ROMARIO TRADER PRO 6.2"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
LOCAL_TZ = ZoneInfo("America/New_York")

TIMEOUT = 20
MAX_WORKERS = 8
MIN_PREP_SECONDS = 30
MAX_SIGNAL_DELAY = 2

MIN_PAYOUT = 0.92

SR_LOOKBACK = 20
STOCH_PERIOD = 14
STOCH_SMOOTH = 3

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
    return (dt or utc_now()).astimezone(LOCAL_TZ)


def api_get(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def extract_list(data, keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
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
            value /= 100

        if not 0 < value <= 1:
            return None

        return value

    except (ValueError, TypeError):
        return None


def get_symbols():
    data = api_get("/v1/symbols", {"venue": "otc"})
    items = extract_list(data, ["symbols", "data"])

    results = []

    for item in items:
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

        payout = normalize_payout(
            item.get("payout", item.get("profit"))
        )

        if payout is None or payout < MIN_PAYOUT:
            continue

        results.append({
            "symbol": symbol,
            "name": str(item.get("name", symbol)),
            "payout": payout
        })

    log(f"FOREX OTC PAIRS WITH PAYOUT >= 92%: {len(results)}")
    return results


def get_candles(symbol):
    data = api_get("/v1/candles", {
        "venue": "otc",
        "symbol": symbol,
        "tf": 60,
        "limit": 100
    })

    candles = extract_list(data, ["candles", "data"])

    if not candles:
        return None

    df = pd.DataFrame(candles)

    required = ["open", "high", "low", "close"]

    for col in required:
        if col not in df.columns:
            log(f"{symbol}: missing {col} candle field")
            return None

        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required).reset_index(drop=True)

    if len(df) < 40:
        return None

    return df


def calculate_stochastic(df):
    lowest = df["low"].rolling(STOCH_PERIOD).min()
    highest = df["high"].rolling(STOCH_PERIOD).max()

    difference = (highest - lowest).replace(0, float("nan"))

    raw_k = 100 * (df["close"] - lowest) / difference

    df["stoch_k"] = raw_k
    df["stoch_d"] = raw_k.rolling(STOCH_SMOOTH).mean()

    return df


def candle_patterns(previous, current):
    body = abs(current["close"] - current["open"])
    full_range = max(current["high"] - current["low"], 1e-10)

    upper_wick = current["high"] - max(
        current["open"], current["close"]
    )

    lower_wick = min(
        current["open"], current["close"]
    ) - current["low"]

    bullish = (
        current["close"] > current["open"]
        and (
            lower_wick >= body * 2
            or (
                previous["close"] < previous["open"]
                and current["close"] > current["open"]
                and current["open"] <= previous["close"]
                and current["close"] >= previous["open"]
            )
        )
    )

    bearish = (
        current["close"] < current["open"]
        and (
            upper_wick >= body * 2
            or (
                previous["close"] > previous["open"]
                and current["close"] < current["open"]
                and current["open"] >= previous["close"]
                and current["close"] <= previous["open"]
            )
        )
    )

    return bullish, bearish, upper_wick, lower_wick, full_range


def analyze(item):
    symbol = item["symbol"]

    try:
        raw = get_candles(symbol)

        if raw is None:
            return None

        # Assumption: API returns candles oldest to newest,
        # with the final row being the currently forming candle.
        # Confirm this against actual OTCharts timestamps.
        df = raw.iloc[:-1].copy().reset_index(drop=True)

        if len(df) < 40:
            return None

        df = calculate_stochastic(df)

        current = df.iloc[-1]
        previous = df.iloc[-2]

        k = float(current["stoch_k"])
        d = float(current["stoch_d"])

        if pd.isna(k) or pd.isna(d):
            return None

        # Levels use earlier candles, excluding current candle.
        history = df.iloc[-(SR_LOOKBACK + 1):-1]

        support = float(history["low"].min())
        resistance = float(history["high"].max())

        close = float(current["close"])
        open_price = float(current["open"])
        high = float(current["high"])
        low = float(current["low"])

        price_range = max(high - low, 1e-10)

        # Tolerance: 15% of the recent average candle range.
        average_range = float(
            (history["high"] - history["low"]).mean()
        )

        tolerance = max(
            average_range * 0.15,
            close * 0.00005
        )

        near_support = abs(low - support) <= tolerance
        near_resistance = abs(high - resistance) <= tolerance

        bullish_pattern, bearish_pattern, upper_wick, lower_wick, candle_range = (
            candle_patterns(previous, current)
        )

        bullish_close = close > open_price
        bearish_close = close < open_price

        # Six CALL checks.
        call_checks = [
            near_support,
            k <= 25,
            k > d,
            bullish_pattern,
            bullish_close,
            close > support
        ]

        # Six PUT checks.
        put_checks = [
            near_resistance,
            k >= 75,
            k < d,
            bearish_pattern,
            bearish_close,
            close < resistance
        ]

        call_score = sum(bool(x) for x in call_checks)
        put_score = sum(bool(x) for x in put_checks)

        # Choose the strongest reversal candidate.
        if call_score > put_score:
            direction = "BUY"
            score = call_score

        elif put_score > call_score:
            direction = "SELL"
            score = put_score

        else:
            # Fallback: choose the nearest level.
            # This is not a confirmed reversal.
            support_distance = abs(close - support)
            resistance_distance = abs(resistance - close)

            if support_distance <= resistance_distance:
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
            "rsi_k": k,
            "rsi_d": d,
            "support": support,
            "resistance": resistance,
            "reference": close,
            "analyzed_at": utc_now()
        }

    except Exception as error:
        log(
            f"PAIR ERROR {symbol}: "
            f"{type(error).__name__}: {error}"
        )
        return None


def schedule_entry(now):
    earliest = now + timedelta(seconds=MIN_PREP_SECONDS)

    entry = now.replace(
        second=0,
        microsecond=0
    ) + timedelta(minutes=1)

    while entry < earliest:
        entry += timedelta(minutes=1)

    signal_time = entry - timedelta(
        seconds=MIN_PREP_SECONDS
    )

    return signal_time, entry


def signal_message(signal, total, signal_time, entry_time):
    expiry = entry_time + timedelta(minutes=1)

    signal_local = florida_time(signal_time)
    entry_local = florida_time(entry_time)
    expiry_local = florida_time(expiry)

    if signal["direction"] == "BUY":
        title = "🟢 CALL / BUY"
        setup = "SUPPORT REVERSAL"
        level = signal["support"]
    else:
        title = "🔴 PUT / SELL"
        setup = "RESISTANCE REVERSAL"
        level = signal["resistance"]

    score = signal["score"]

    if score == 6:
        quality = "6/6 - FULL CONFIRMATION"
    elif score >= 4:
        quality = f"{score}/6 - PARTIAL CONFIRMATION"
    elif score >= 1:
        quality = f"{score}/6 - LOW CONFIRMATION"
    else:
        quality = "0/6 - FALLBACK DIRECTION"

    payout = round(signal["payout"] * 100)

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"💱 PAIR: {html.escape(signal['name'])}\n"
        f"📊 SYMBOL: {signal['symbol']}\n"
        f"📈 STRATEGY: SUPPORT-RESISTANCE REVERSAL\n"
        f"🧩 SETUP: {setup}\n\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n"
        f"⭐ SETUP SCORE: {quality}\n\n"
        f"📊 STOCH K: {signal['rsi_k']:.1f}\n"
        f"📊 STOCH D: {signal['rsi_d']:.1f}\n"
        f"📍 REFERENCE PRICE: {signal['reference']:.6f}\n"
        f"🎯 KEY LEVEL: {level:.6f}\n"
        f"💰 BROKER PAYOUT: {payout}%\n\n"
        f"🕒 SIGNAL TIME: {signal_local:%I:%M:%S %p}\n"
        f"🟢 <b>ENTRY TIME: {entry_local:%I:%M:%S %p}</b>\n"
        f"🏁 EXPIRATION: {expiry_local:%I:%M:%S %p}\n\n"
        f"🌎 TIMEZONE: FLORIDA\n"
        f"🔎 QUALIFIED PAIRS: {total}\n\n"
        f"<i>⚠️ Best available candidate. "
        f"Score is not a win probability. "
        f"Fallback signals may be weak. No Martingale.</i>"
    )


def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

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
        raise RuntimeError("Telegram did not confirm delivery")

    return result


def main():
    started = time.time()

    log("=" * 48)
    log(VERSION)
    log("SUPPORT + RESISTANCE + STOCHASTIC + CANDLESTICKS")
    log("MINIMUM PAYOUT: 92%")
    log("=" * 48)

    if not API_KEY or not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Missing GitHub Secrets")

    symbols = get_symbols()

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
            result = future.result()

            if result:
                candidates.append(result)

    log(f"PAIRS SCANNED: {len(symbols)}")
    log(f"VALID CANDIDATES: {len(candidates)}")
    log(f"SCAN DURATION: {time.time() - started:.1f}s")

    if not candidates:
        log("No usable candle data. No signal can be generated.")
        return

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
        f"SELECTED: {best['symbol']} "
        f"{best['direction']} "
        f"SCORE {best['score']}/6 "
        f"PAYOUT {best['payout'] * 100:.1f}%"
    )

    signal_time, entry_time = schedule_entry(utc_now())

    wait_seconds = (
        signal_time - utc_now()
    ).total_seconds()

    if wait_seconds > 0:
        log(f"Waiting {wait_seconds:.1f} seconds.")
        time.sleep(wait_seconds)

    actual_send = utc_now()

    delay = (actual_send - signal_time).total_seconds()
    remaining = (entry_time - actual_send).total_seconds()

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
    log(f"TOTAL RUNTIME: {time.time() - started:.1f}s")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"FATAL ERROR: {type(error).__name__}: {error}")
        raise
