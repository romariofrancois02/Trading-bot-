
import os
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ==================================================
# ROMARIO TRADER PRO 9.0
# EMA 50 + STOCHASTIC 14,3
# ALERT ONLY - NO AUTOMATIC TRADING
# ==================================================

BASE_URL = "https://otcharts.com"
SYMBOLS_ENDPOINT = "/v1/symbols"
CANDLES_ENDPOINT = "/v1/candles"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEZONE = ZoneInfo("America/New_York")

TIMEFRAME = 60
EXPIRATION = 60
CANDLE_LIMIT = 300
MIN_PAYOUT = 92.0
MAX_PAIRS = 12

# OTCharts candle timestamps are adjusted by this
# offset in the existing bot configuration.
OTC_OFFSET = 7200

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "ROMARIO-TRADER-PRO/9.0"
})


# ==================================================
# GENERAL HELPERS
# ==================================================

def florida_time():
    return datetime.now(TIMEZONE)


def log(message):
    print(
        f"[{florida_time().strftime('%Y-%m-%d %I:%M:%S %p')}] "
        f"{message}",
        flush=True
    )


def extract_list(data, keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)

            if isinstance(value, list):
                return value

        for value in data.values():
            if isinstance(value, dict):
                result = extract_list(value, keys)
                if result:
                    return result

    return []


def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials are missing.")
        return

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

    except Exception as exc:
        log(f"Telegram error: {exc}")


def api_get(endpoint, params=None):
    url = BASE_URL + endpoint

    response = SESSION.get(
        url,
        params=params,
        timeout=30
    )

    response.raise_for_status()
    return response.json()


# ==================================================
# SYMBOLS / PAYOUT
# ==================================================

def get_payout(symbol):
    for item in symbol:
        pass


def get_pairs():
    data = api_get(
        SYMBOLS_ENDPOINT,
        {"venue": "otc"}
    )

    symbols = extract_list(
        data,
        ("symbols", "data", "result")
    )

    pairs = []

    for item in symbols:
        if not isinstance(item, dict):
            continue

        name = str(
            item.get("symbol")
            or item.get("name")
            or item.get("ticker")
            or ""
        )

        if not name.lower().endswith("_otc"):
            continue

        # OTC forex pairs only: six currency letters.
        base = name[:-4]

        if len(base) != 6 or not base.isalpha():
            continue

        if item.get("isOtc") is not True:
            continue

        if item.get("open") is not True:
            continue

        payout_value = (
            item.get("payout")
            or item.get("payoutRate")
            or item.get("profit")
            or 0
        )

        try:
            payout = float(str(payout_value).replace("%", ""))
        except (ValueError, TypeError):
            continue

        if payout < MIN_PAYOUT:
            continue

        pairs.append({
            "symbol": name,
            "payout": payout
        })

    pairs.sort(
        key=lambda pair: pair["payout"],
        reverse=True
    )

    return pairs[:MAX_PAIRS]


# ==================================================
# CANDLE DATA
# ==================================================

def get_candles(symbol):
    data = api_get(
        CANDLES_ENDPOINT,
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    rows = extract_list(
        data,
        ("candles", "data", "result")
    )

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            timestamp = float(
                row.get("time")
                or row.get("timestamp")
                or row.get("t")
            )

            # Convert milliseconds to seconds if necessary.
            if timestamp > 100000000000:
                timestamp /= 1000

            candle = {
                "time": timestamp,
                "open": float(row.get("open", row.get("o"))),
                "high": float(row.get("high", row.get("h"))),
                "low": float(row.get("low", row.get("l"))),
                "close": float(row.get("close", row.get("c")))
            }

            candles.append(candle)

        except (ValueError, TypeError):
            continue

    # Remove duplicate timestamps.
    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = sorted(
        unique.values(),
        key=lambda candle: candle["time"]
    )

    # Keep completed candles only.
    now = time.time()

    completed = [
        candle for candle in candles
        if candle["time"] - OTC_OFFSET + TIMEFRAME <= now
    ]

    return completed


# ==================================================
# EMA 50
# ==================================================

def calculate_ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    ema = [sum(values[:period]) / period]

    for value in values[period:]:
        next_value = (
            value * multiplier
            + ema[-1] * (1 - multiplier)
        )

        ema.append(next_value)

    return [None] * (period - 1) + ema


# ==================================================
# STOCHASTIC 14,3
# ==================================================

def calculate_stochastic(candles):
    k_values = []

    for i in range(len(candles)):
        if i < 13:
            k_values.append(None)
            continue

        window = candles[i - 13:i + 1]

        lowest = min(c["low"] for c in window)
        highest = max(c["high"] for c in window)

        difference = highest - lowest

        if difference == 0:
            k_values.append(None)
            continue

        k = (
            100
            * (candles[i]["close"] - lowest)
            / difference
        )

        k_values.append(k)

    d_values = []

    for i in range(len(k_values)):
        if i < 2:
            d_values.append(None)
            continue

        values = k_values[i - 2:i + 1]

        if any(value is None for value in values):
            d_values.append(None)
        else:
            d_values.append(sum(values) / 3)

    return k_values, d_values


# ==================================================
# STRATEGY
# ==================================================

def strategy_votes(candles):
    result = {
        "CALL": 0,
        "PUT": 0,
        "call_reasons": [],
        "put_reasons": [],
        "score": 0
    }

    if len(candles) < 60:
        return result

    closes = [c["close"] for c in candles]

    ema50 = calculate_ema(closes, 50)

    k_values, d_values = calculate_stochastic(candles)

    current = -1
    previous = -2

    close = closes[current]
    ema = ema50[current]

    k = k_values[current]
    d = d_values[current]

    previous_k = k_values[previous]
    previous_d = d_values[previous]

    values = [close, ema, k, d, previous_k, previous_d]

    if any(value is None for value in values):
        return result

    if k > previous_k and d > previous_d:
        bullish_momentum = True
    else:
        bullish_momentum = False

    if k < previous_k and d < previous_d:
        bearish_momentum = True
    else:
        bearish_momentum = False

    uptrend = close > ema
    downtrend = close < ema

    crossed_up = previous_k <= previous_d and k > d
    crossed_down = previous_k >= previous_d and k < d

    # CALL setup
    if uptrend and crossed_up and k < 30 and bullish_momentum:
        result["CALL"] = 1
        result["score"] = 1 + (30 - k) / 30
        result["call_reasons"].append(
            f"EMA50 uptrend + Stochastic bullish cross ({k:.1f})"
        )

    # PUT setup
    elif downtrend and crossed_down and k > 70 and bearish_momentum:
        result["PUT"] = 1
        result["score"] = 1 + (k - 70) / 30
        result["put_reasons"].append(
            f"EMA50 downtrend + Stochastic bearish cross ({k:.1f})"
        )

    return result


def get_direction(votes):
    if votes["CALL"] == 1:
        return "CALL"

    if votes["PUT"] == 1:
        return "PUT"

    return None


# ==================================================
# ENTRY TIME
# ==================================================

def entry_time():
    now = florida_time()

    next_minute = (
        now.replace(second=0, microsecond=0)
        + timedelta(minutes=1)
    )

    return next_minute.strftime("%I:%M:%S %p")


# ==================================================
# SIGNAL MESSAGE
# ==================================================

def signal_message(pair, direction, votes):
    emoji = "🟢" if direction == "CALL" else "🔴"

    reasons = (
        votes["call_reasons"]
        if direction == "CALL"
        else votes["put_reasons"]
    )

    reason_text = "\n".join(reasons)

    return (
        "🤖 <b>ROMARIO TRADER PRO 9.0</b>\n\n"
        f"{emoji} <b>{direction}</b>\n\n"
        f"💱 PAIR: <b>{pair['symbol']}</b>\n"
        f"💰 PAYOUT: <b>{pair['payout']:.1f}%</b>\n"
        f"📊 TIMEFRAME: <b>M1</b>\n"
        f"⌛ EXPIRATION: <b>60 SECONDS</b>\n"
        f"🎯 ENTRY TIME: <b>{entry_time()} Florida</b>\n\n"
        f"📌 STRATEGY:\n{reason_text}\n\n"
        f"🕒 FLORIDA TIME: "
        f"{florida_time().strftime('%Y-%m-%d %I:%M:%S %p')}"
    )


# ==================================================
# NO SIGNAL MESSAGE
# ==================================================

def no_signal_message(scanned, errors):
    return (
        "🤖 <b>ROMARIO TRADER PRO 9.0</b>\n\n"
        "⚪ <b>NO VALID SIGNAL</b>\n"
        "No pair passed the EMA50 + Stochastic rules.\n\n"
        f"Pairs scanned: {scanned}\n"
        f"Minimum payout: {MIN_PAYOUT:.1f}%\n"
        "Strategy: EMA50 + Stochastic 14,3\n"
        "Expiration: 60 seconds\n"
        f"Data errors: {errors}\n\n"
        f"🕒 FLORIDA TIME: "
        f"{florida_time().strftime('%Y-%m-%d %I:%M:%S %p')}"
    )


# ==================================================
# MAIN
# ==================================================

def main():
    if not API_KEY:
        raise RuntimeError("Missing OTCHARTS_API_KEY")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing Telegram credentials")

    log("ROMARIO TRADER PRO 9.0 starting...")

    pairs = get_pairs()

    if not pairs:
        telegram_send(
            no_signal_message(0, 0)
        )
        log("No eligible OTC forex pairs.")
        return

    candidates = []
    errors = 0
    scanned = 0

    for pair in pairs:
        scanned += 1
        symbol = pair["symbol"]

        log(f"Scanning {symbol}...")

        try:
            candles = get_candles(symbol)

            if len(candles) < 60:
                log(f"{symbol}: insufficient completed candles.")
                continue

            votes = strategy_votes(candles)
            direction = get_direction(votes)

            if direction:
                candidates.append({
                    "pair": pair,
                    "direction": direction,
                    "votes": votes
                })

                log(f"{symbol}: {direction} setup found.")
            else:
                log(f"{symbol}: no setup.")

        except Exception as exc:
            errors += 1
            log(f"{symbol}: ERROR: {exc}")

    if not candidates:
        telegram_send(
            no_signal_message(scanned, errors)
        )
        log("No valid signal.")
        return

    # Select only the strongest qualifying setup.
    candidates.sort(
        key=lambda item: (
            item["votes"]["score"],
            item["pair"]["payout"]
        ),
        reverse=True
    )

    best = candidates[0]

    message = signal_message(
        best["pair"],
        best["direction"],
        best["votes"]
    )

    telegram_send(message)

    log(
        f"SIGNAL SENT: {best['pair']['symbol']} "
        f"{best['direction']}"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        log(f"FATAL ERROR: {exc}")

        telegram_send(
            "⚠️ <b>ROMARIO TRADER PRO 9.0 ERROR</b>\n\n"
            f"<code>{str(exc)[:800]}</code>\n\n"
            f"🕒 {florida_time().strftime('%Y-%m-%d %I:%M:%S %p')}"
        )

        raise
