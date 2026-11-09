
import os
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ==================================================
# ROMARIO TRADER PRO 9.4
# OTC FOREX ONLY | EMA50 + STOCHASTIC 14,3
# MANUAL RUN | TELEGRAM ALERT ONLY
# ==================================================

BASE_URL = "https://otcharts.com"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEZONE = ZoneInfo("America/New_York")

TIMEFRAME = 60
EXPIRATION = 60
CANDLE_LIMIT = 300
MIN_PAYOUT = 92.0
OTC_OFFSET = 7200

RETRIES = 2
RETRY_DELAY = 2

# ISO currency codes: keeps forex pairs and excludes BTCUSD, etc.
CURRENCIES = {
    "AED", "ARS", "AUD", "BDT", "BHD", "BRL", "CAD",
    "CHF", "CLP", "CNY", "COP", "CZK", "DKK", "EGP",
    "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR",
    "IQD", "IRR", "ISK", "JOD", "JPY", "KES", "KRW",
    "KWD", "LBP", "LKR", "MAD", "MXN", "MYR", "NGN",
    "NOK", "NPR", "NZD", "OMR", "PEN", "PHP", "PKR",
    "PLN", "QAR", "RON", "RUB", "SAR", "SEK", "SGD",
    "THB", "TND", "TRY", "TWD", "UAH", "USD", "UYU",
    "VND", "XAF", "XOF", "YER", "ZAR"
}

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "ROMARIO-TRADER-PRO/9.4"
})


# ==================================================
# TIME AND LOGS
# ==================================================

def florida_time():
    return datetime.now(TIMEZONE)


def log(message):
    print(
        f"[{florida_time().strftime('%Y-%m-%d %I:%M:%S %p')}] "
        f"{message}",
        flush=True
    )


# ==================================================
# TELEGRAM
# ==================================================

def telegram_send(message):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

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


# ==================================================
# API HELPERS WITH RETRIES
# ==================================================

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


def api_get(endpoint, params=None):
    last_error = None

    for attempt in range(RETRIES + 1):
        try:
            response = SESSION.get(
                BASE_URL + endpoint,
                params=params,
                timeout=30
            )

            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as exc:
            last_error = exc

            status = (
                exc.response.status_code
                if exc.response is not None
                else None
            )

            if status not in (429, 500, 502, 503, 504):
                raise

        except requests.exceptions.RequestException as exc:
            last_error = exc

        if attempt < RETRIES:
            delay = RETRY_DELAY * (attempt + 1)
            log(
                f"Temporary API error. "
                f"Retry {attempt + 1}/{RETRIES} "
                f"in {delay}s."
            )
            time.sleep(delay)

    raise last_error


# ==================================================
# FIND QUALIFYING OTC FOREX PAIRS
# ==================================================

def get_pairs():
    data = api_get(
        "/v1/symbols",
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

        base = name[:-4].upper()

        if len(base) != 6:
            continue

        # Both sides must be recognized currencies
        if base[:3] not in CURRENCIES:
            continue

        if base[3:] not in CURRENCIES:
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
            payout = float(
                str(payout_value).replace("%", "")
            )
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

    return pairs


# ==================================================
# GET COMPLETED CANDLES
# ==================================================

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

    unique = {
        candle["time"]: candle
        for candle in candles
    }

    candles = sorted(
        unique.values(),
        key=lambda candle: candle["time"]
    )

    now = time.time()

    return [
        candle for candle in candles
        if candle["time"] - OTC_OFFSET + TIMEFRAME <= now
    ]


# ==================================================
# EMA 50
# ==================================================

def calculate_ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    ema = [
        sum(values[:period]) / period
    ]

    for value in values[period:]:
        ema.append(
            value * multiplier
            + ema[-1] * (1 - multiplier)
        )

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
# STRATEGY 9.4
# NO CROSSOVER REQUIRED
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

    current = len(candles) - 1
    previous = current - 1
    older = current - 2

    close = closes[current]
    ema = ema50[current]
    previous_ema = ema50[previous]

    k = k_values[current]
    d = d_values[current]

    previous_k = k_values[previous]
    previous_d = d_values[previous]

    older_k = k_values[older]

    values = [
        close, ema, previous_ema,
        k, d, previous_k, previous_d, older_k
    ]

    if any(value is None for value in values):
        return result

    candle = candles[current]

    # Trend direction
    uptrend = (
        close > ema
        and ema > previous_ema
    )

    downtrend = (
        close < ema
        and ema < previous_ema
    )

    # Stochastic direction
    bullish_stochastic = (
        k > d
        and k > previous_k
        and d >= previous_d
        and k > older_k
    )

    bearish_stochastic = (
        k < d
        and k < previous_k
        and d <= previous_d
        and k < older_k
    )

    # Last completed candle direction
    bullish_candle = candle["close"] > candle["open"]
    bearish_candle = candle["close"] < candle["open"]

    # Avoid extreme stochastic readings
    normal_buy_zone = 20 <= k <= 80
    normal_sell_zone = 20 <= k <= 80

    # CALL
    if (
        uptrend
        and bullish_stochastic
        and bullish_candle
        and normal_buy_zone
    ):
        result["CALL"] = 1

        result["score"] = (
            3
            + min(abs(k - d) / 10, 1)
            + min(abs(close - ema) / max(ema, 0.000001) * 100, 1)
        )

        result["call_reasons"] = [
            "EMA50 bullish trend",
            f"Stochastic bullish: K {k:.1f} / D {d:.1f}",
            "Last completed candle bullish"
        ]

    # PUT
    elif (
        downtrend
        and bearish_stochastic
        and bearish_candle
        and normal_sell_zone
    ):
        result["PUT"] = 1

        result["score"] = (
            3
            + min(abs(k - d) / 10, 1)
            + min(abs(close - ema) / max(ema, 0.000001) * 100, 1)
        )

        result["put_reasons"] = [
            "EMA50 bearish trend",
            f"Stochastic bearish: K {k:.1f} / D {d:.1f}",
            "Last completed candle bearish"
        ]

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

def wait_for_entry():
    now = florida_time()

    entry = (
        now.replace(second=0, microsecond=0)
        + timedelta(minutes=1)
    )

    send_at = entry - timedelta(seconds=15)

    if send_at <= now:
        entry += timedelta(minutes=1)
        send_at = entry - timedelta(seconds=15)

    log(
        "Signal selected. Waiting for "
        + send_at.strftime("%I:%M:%S %p")
        + " Florida time."
    )

    while True:
        remaining = (
            send_at - florida_time()
        ).total_seconds()

        if remaining <= 0:
            break

        time.sleep(min(remaining, 0.25))

    return entry


# ==================================================
# SIGNAL MESSAGE
# ==================================================

def signal_message(pair, direction, votes, entry):
    emoji = "🟢" if direction == "CALL" else "🔴"

    reasons = (
        votes["call_reasons"]
        if direction == "CALL"
        else votes["put_reasons"]
    )

    sent_time = florida_time()

    return (
        "🤖 <b>ROMARIO TRADER PRO 9.4</b>\n\n"
        f"{emoji} <b>{direction}</b>\n\n"
        f"💱 PAIR: <b>{pair['symbol']}</b>\n"
        f"💰 PAYOUT: <b>{pair['payout']:.1f}%</b>\n"
        "📊 TIMEFRAME: <b>M1</b>\n"
        "⌛ EXPIRATION: <b>60 SECONDS</b>\n\n"
        f"🎯 ENTRY TIME: <b>{entry.strftime('%I:%M:%S %p')}</b>\n"
        f"⚡ SIGNAL SENT: <b>{sent_time.strftime('%I:%M:%S %p')}</b>\n\n"
        "📌 STRATEGY:\n"
        + "\n".join(reasons)
        + "\n\n"
        + f"🕒 FLORIDA TIME: "
        + sent_time.strftime("%Y-%m-%d %I:%M:%S %p")
    )


# ==================================================
# NO SIGNAL
# ==================================================

def no_signal_message(scanned, errors):
    return (
        "🤖 <b>ROMARIO TRADER PRO 9.4</b>\n\n"
        "⚪ <b>NO VALID SIGNAL</b>\n"
        "No pair passed the strategy rules.\n\n"
        f"Pairs scanned: {scanned}\n"
        f"Minimum payout: {MIN_PAYOUT:.1f}%\n"
        "Market: OTC Forex only\n"
        "Strategy: EMA50 + Stochastic 14,3\n"
        "Candle confirmation: ON\n"
        "Crossover required: NO\n"
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

    log("ROMARIO TRADER PRO 9.4 starting...")

    pairs = get_pairs()

    if not pairs:
        telegram_send(no_signal_message(0, 0))
        log("No eligible OTC forex pairs.")
        return

    log(f"Eligible OTC forex pairs found: {len(pairs)}")

    candidates = []
    errors = 0
    scanned = 0

    for pair in pairs:
        scanned += 1
        symbol = pair["symbol"]

        log(
            f"Scanning {symbol} "
            f"({scanned}/{len(pairs)})..."
        )

        try:
            candles = get_candles(symbol)

            if len(candles) < 60:
                log(f"{symbol}: insufficient candles.")
                continue

            votes = strategy_votes(candles)
            direction = get_direction(votes)

            if direction:
                candidates.append({
                    "pair": pair,
                    "direction": direction,
                    "votes": votes
                })

                log(
                    f"{symbol}: {direction} setup found. "
                    f"Score: {votes['score']:.2f}"
                )
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

    candidates.sort(
        key=lambda item: (
            item["votes"]["score"],
            item["pair"]["payout"]
        ),
        reverse=True
    )

    best = candidates[0]

    log(
        f"Best setup: {best['pair']['symbol']} "
        f"{best['direction']}"
    )

    entry = wait_for_entry()

    message = signal_message(
        best["pair"],
        best["direction"],
        best["votes"],
        entry
    )

    telegram_send(message)

    log(
        f"SIGNAL SENT: {best['pair']['symbol']} "
        f"{best['direction']} "
        f"ENTRY: {entry.strftime('%I:%M:%S %p')}"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        log(f"FATAL ERROR: {exc}")

        try:
            telegram_send(
                "⚠️ <b>ROMARIO TRADER PRO 9.4 ERROR</b>\n\n"
                f"<code>{str(exc)[:800]}</code>\n\n"
                f"🕒 {florida_time().strftime('%Y-%m-%d %I:%M:%S %p')}"
            )
        except Exception:
            pass

        raise
