
import os
import re
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# =========================================================
# ROMARIO TRADER PRO 9.8
# POCKET OPTION OTC | EMA50 + STOCHASTIC 14,3
# M1 | 60 SECONDS | TELEGRAM ALERTS ONLY
# =========================================================

BOT_NAME = "ROMARIO TRADER PRO 9.8"

BASE_URL = "https://otcharts.com"
TIMEFRAME = 60
CANDLE_LIMIT = 300
MIN_PAYOUT = 92.0
EXPIRATION_SECONDS = 60
ENTRY_SECONDS_BEFORE = 15

# Pocket Option candles use UTC+2 clock
OTC_OFFSET = 7200

RETRIES = 5
REQUEST_TIMEOUT = 30

FLORIDA_TZ = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

session = requests.Session()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================================================
# TIME
# =========================================================

def florida_time(timestamp=None):
    if timestamp is None:
        dt = datetime.now(FLORIDA_TZ)
    else:
        dt = datetime.fromtimestamp(timestamp, FLORIDA_TZ)

    return dt.strftime("%Y-%m-%d %I:%M:%S %p")


# =========================================================
# TELEGRAM
# =========================================================

def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram secrets missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            logging.error("Telegram error: %s", result)
            return False

        return True

    except requests.RequestException as error:
        logging.error("Telegram request failed: %s", error)
        return False


# =========================================================
# API REQUEST
# =========================================================

def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing.")

    url = BASE_URL + endpoint

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "RomarioTraderPro/9.8"
    }

    for attempt in range(RETRIES + 1):
        try:
            response = session.get(
                url,
                headers=headers,
                params=params or {},
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code in (429, 500, 502, 503, 504):
                if attempt >= RETRIES:
                    response.raise_for_status()

                delay = min(5 * (2 ** attempt), 30)

                logging.warning(
                    "API HTTP %s. Retry in %s seconds.",
                    response.status_code,
                    delay
                )

                time.sleep(delay)
                continue

            response.raise_for_status()
            return response.json()

        except requests.RequestException:
            if attempt >= RETRIES:
                raise

            delay = min(5 * (2 ** attempt), 30)
            logging.warning("API connection retry in %s seconds.", delay)
            time.sleep(delay)

    raise RuntimeError("OTCharts API failed.")


# =========================================================
# HELPERS
# =========================================================

def number(value, default=None):
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace("%", "").strip()

        return float(value)

    except (ValueError, TypeError):
        return default


def parse_timestamp(value):
    try:
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                dt = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                return dt.timestamp()

        value = float(value)

        if value > 10_000_000_000:
            value /= 1000

        return value

    except (ValueError, TypeError, OverflowError):
        return None


# =========================================================
# GET OTC SYMBOLS
# =========================================================

def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    symbols = data.get("symbols", [])

    if not isinstance(symbols, list) or not symbols:
        raise RuntimeError("No symbols returned by OTCharts.")

    eligible = []
    seen = set()

    for item in symbols:
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", "")).strip()

        # Forex OTC pairs only; preserve exact API spelling.
        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        if symbol in seen:
            continue

        seen.add(symbol)

        payout = number(item.get("payout"))
        is_open = item.get("open")
        is_otc = item.get("isOtc")

        if payout is None:
            continue

        if 0 < payout <= 1:
            payout *= 100

        if payout < MIN_PAYOUT:
            continue

        if is_open is not True:
            continue

        if is_otc is not True:
            continue

        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    return eligible


# =========================================================
# CANDLE PARSING
# =========================================================

def parse_candle(item):
    if not isinstance(item, dict):
        return None

    timestamp = parse_timestamp(
        item.get("time", item.get("timestamp"))
    )

    open_price = number(item.get("open"))
    high_price = number(item.get("high"))
    low_price = number(item.get("low"))
    close_price = number(item.get("close"))

    if any(v is None for v in (
        timestamp,
        open_price,
        high_price,
        low_price,
        close_price
    )):
        return None

    return {
        "time": timestamp,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price
    }


# =========================================================
# GET COMPLETED CANDLES
# =========================================================

def get_candles(symbol):
    data = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    if data.get("open") is False:
        raise ValueError(f"{symbol} is closed.")

    gaps = data.get("gaps", {})

    if isinstance(gaps, dict):
        missing = int(gaps.get("missingBars", 0) or 0)

        if missing > 0:
            raise ValueError(
                f"{symbol} has {missing} missing candle bars."
            )

    raw = data.get("candles", [])

    if not isinstance(raw, list):
        raise ValueError("Invalid candle response.")

    candles = []

    for item in raw:
        candle = parse_candle(item)

        if candle:
            candles.append(candle)

    unique = {
        candle["time"]: candle
        for candle in candles
    }

    candles = sorted(
        unique.values(),
        key=lambda item: item["time"]
    )

    now = time.time()

    completed = [
        candle for candle in candles
        if candle["time"] - OTC_OFFSET + TIMEFRAME <= now
    ]

    if len(completed) < 60:
        raise ValueError(
            f"Only {len(completed)} completed candles for {symbol}."
        )

    return completed


# =========================================================
# EMA 50
# =========================================================

def calculate_ema(values, period=50):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    result = [
        sum(values[:period]) / period
    ]

    for value in values[period:]:
        result.append(
            value * multiplier
            + result[-1] * (1 - multiplier)
        )

    return [None] * (period - 1) + result


# =========================================================
# STOCHASTIC 14,3
# =========================================================

def calculate_stochastic(candles, period=14):
    k_values = []

    for i in range(len(candles)):
        if i < period - 1:
            k_values.append(None)
            continue

        window = candles[i - period + 1:i + 1]

        highest = max(x["high"] for x in window)
        lowest = min(x["low"] for x in window)
        close = candles[i]["close"]

        if highest == lowest:
            k_values.append(50.0)
        else:
            k_values.append(
                (close - lowest) / (highest - lowest) * 100
            )

    d_values = []

    for i in range(len(k_values)):
        if i < 2:
            d_values.append(None)
            continue

        window = k_values[i - 2:i + 1]

        if any(x is None for x in window):
            d_values.append(None)
        else:
            d_values.append(sum(window) / 3)

    return k_values, d_values


# =========================================================
# STRATEGY — DIRECTIONS ARE NEVER REVERSED
# =========================================================

def analyze_pair(pair):
    symbol = pair["symbol"]
    payout = pair["payout"]

    candles = get_candles(symbol)

    closes = [c["close"] for c in candles]

    ema = calculate_ema(closes, 50)
    k_values, d_values = calculate_stochastic(candles)

    i = len(candles) - 1

    ema_now = ema[i]
    ema_prev = ema[i - 1]

    k_now = k_values[i]
    k_prev = k_values[i - 1]
    k_two_back = k_values[i - 2]

    d_now = d_values[i]
    d_prev = d_values[i - 1]

    required = (
        ema_now, ema_prev,
        k_now, k_prev, k_two_back,
        d_now, d_prev
    )

    if any(x is None for x in required):
        return None

    candle = candles[i]

    bullish = candle["close"] > candle["open"]
    bearish = candle["close"] < candle["open"]

    # CALL conditions: upward trend and upward momentum.
    call_conditions = [
        candle["close"] > ema_now,
        ema_now > ema_prev,
        k_now > d_now,
        k_now > k_prev,
        d_now >= d_prev,
        k_now > k_two_back,
        bullish,
        20 < k_now < 80
    ]

    # PUT conditions: downward trend and downward momentum.
    put_conditions = [
        candle["close"] < ema_now,
        ema_now < ema_prev,
        k_now < d_now,
        k_now < k_prev,
        d_now <= d_prev,
        k_now < k_two_back,
        bearish,
        20 < k_now < 80
    ]

    call_score = sum(call_conditions)
    put_score = sum(put_conditions)

    logging.info(
        "%s | CALL=%s/8 | PUT=%s/8 | K=%.2f | D=%.2f",
        symbol,
        call_score,
        put_score,
        k_now,
        d_now
    )

    # Explicit direction mapping. No inversion.
    if call_score == 8:
        direction = "CALL"

    elif put_score == 8:
        direction = "PUT"

    else:
        return None

    distance = (
        abs(candle["close"] - ema_now) / ema_now
        if ema_now else 0
    )

    return {
        "symbol": symbol,
        "direction": direction,
        "payout": payout,
        "score": 8,
        "distance": distance,
        "k": k_now,
        "d": d_now
    }


# =========================================================
# ENTRY TIME
# =========================================================

def wait_for_entry():
    now = time.time()

    next_minute = (int(now // 60) + 1) * 60
    target = next_minute - ENTRY_SECONDS_BEFORE

    if target <= now:
        target += 60

    delay = target - time.time()

    if delay > 0:
        logging.info("Waiting %.1f seconds for entry.", delay)
        time.sleep(delay)

    return target


# =========================================================
# TELEGRAM MESSAGES
# =========================================================

def send_signal(signal, entry_timestamp):
    direction = signal["direction"]

    # Final safety check: only these two directions are allowed.
    if direction not in ("CALL", "PUT"):
        raise ValueError("Invalid signal direction.")

    message = (
        f"🤖 <b>{BOT_NAME}</b>\n\n"
        f"{'🟢' if direction == 'CALL' else '🔴'} "
        f"<b>{direction}</b>\n\n"
        f"💱 PAIR: <b>{signal['symbol']}</b>\n"
        f"💰 PAYOUT: <b>{signal['payout']:.1f}%</b>\n"
        f"📊 SETUP: <b>{signal['score']}/8</b>\n\n"
        f"⏱ TIMEFRAME: <b>M1</b>\n"
        f"⌛ EXPIRATION: <b>{EXPIRATION_SECONDS} SECONDS</b>\n"
        f"🎯 ENTRY TIME: <b>{florida_time(entry_timestamp)}</b>\n\n"
        f"📈 STOCH K: {signal['k']:.2f}\n"
        f"📉 STOCH D: {signal['d']:.2f}\n\n"
        f"🕒 FLORIDA TIME: {florida_time()}\n"
        f"⚠️ Alert only — verify before entering."
    )

    telegram_send(message)


def send_no_signal(pair_count):
    message = (
        f"🤖 <b>{BOT_NAME}</b>\n\n"
        f"⚪ <b>NO VALID SIGNAL</b>\n\n"
        f"Pairs checked: {pair_count}\n"
        f"Minimum payout: {MIN_PAYOUT:.1f}%\n"
        f"Timeframe: M1\n\n"
        f"No pair passed all 8 conditions.\n"
        f"No signal was forced.\n\n"
        f"🕒 FLORIDA TIME: {florida_time()}"
    )

    telegram_send(message)


def send_api_error(error):
    message = (
        f"⚠️ <b>{BOT_NAME} API ERROR</b>\n\n"
        f"No signal generated.\n"
        f"Error: <code>{str(error)[:250]}</code>\n\n"
        f"🕒 FLORIDA TIME: {florida_time()}"
    )

    telegram_send(message)


# =========================================================
# MAIN
# =========================================================

def main():
    logging.info("%s starting...", BOT_NAME)

    if not API_KEY:
        send_api_error("OTCHARTS_API_KEY is missing.")
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram secrets are missing.")
        return

    try:
        pairs = get_symbols()
    except Exception as error:
        logging.exception("Symbol discovery failed.")
        send_api_error(error)
        return

    logging.info("Eligible OTC forex pairs: %s", len(pairs))

    if not pairs:
        send_no_signal(0)
        return

    signals = []
    errors = 0

    for count, pair in enumerate(pairs, start=1):
        symbol = pair["symbol"]

        logging.info(
            "Scanning %s (%s/%s)",
            symbol,
            count,
            len(pairs)
        )

        try:
            signal = analyze_pair(pair)

            if signal:
                signals.append(signal)

                logging.info(
                    "VALID %s | %s | payout %.1f%%",
                    signal["direction"],
                    symbol,
                    signal["payout"]
                )

        except Exception as error:
            errors += 1
            logging.warning(
                "Skipped %s: %s",
                symbol,
                error
            )

    if errors == len(pairs):
        send_api_error(
            "Candle data failed for every eligible pair."
        )
        return

    if not signals:
        send_no_signal(len(pairs))
        return

    # Choose the signal closest to EMA, then highest payout.
    signals.sort(
        key=lambda x: (x["distance"], -x["payout"])
    )

    best = signals[0]

    logging.info(
        "SELECTED DIRECTION: %s | PAIR: %s",
        best["direction"],
        best["symbol"]
    )

    entry_timestamp = wait_for_entry()

    send_signal(best, entry_timestamp)

    logging.info("Bot finished.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.exception("Unexpected error.")
        send_api_error(error)
