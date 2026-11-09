
import os
import time
import logging
import requests
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

# ==========================================
# ROMARIO TRADER PRO 7.3
# OTCharts + Telegram | OTC Forex | M1
# ==========================================

VERSION = "ROMARIO TRADER PRO 7.3"

BASE_URL = "https://otcharts.com"
TIMEFRAME = 60
CANDLE_LIMIT = 150
OTC_CLOCK_OFFSET = 2 * 60 * 60

MIN_PAYOUT = 92.0
MIN_VOTES = 2
EXPIRATION = 60

OTCHARTS_API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {OTCHARTS_API_KEY}",
    "X-API-Key": OTCHARTS_API_KEY,
    "Accept": "application/json"
})

FIAT = {
    "USD", "EUR", "GBP", "JPY", "CHF",
    "AUD", "CAD", "NZD", "CNY", "HKD",
    "SGD", "INR", "MXN", "ZAR", "TRY",
    "NOK", "SEK", "DKK", "PLN", "CZK",
    "HUF", "RON", "BGN", "THB", "MYR",
    "IDR", "PHP", "VND", "KRW", "AED",
    "SAR", "QAR", "KWD", "JOD", "LBP"
}


def florida_time():
    return datetime.now(
        ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d %I:%M:%S %p")


def api_get(endpoint, params=None):
    if not OTCHARTS_API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing")

    url = BASE_URL + endpoint

    last_error = None

    for attempt in range(3):
        try:
            response = session.get(
                url,
                params=params,
                timeout=25
            )

            if response.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(
                    f"HTTP {response.status_code}"
                )

            response.raise_for_status()
            return response.json()

        except Exception as exc:
            last_error = exc
            logging.warning(
                "API attempt %s failed: %s",
                attempt + 1, exc
            )
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"API failed: {last_error}")


def extract_list(data, keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def normalize_timestamp(value):
    try:
        value = float(value)

        if value > 100000000000:
            value /= 1000

        return int(value)
    except (ValueError, TypeError):
        return None


def get_payout(row):
    for key in ("payout", "payout_percent", "profit"):
        value = row.get(key)

        if value is None:
            continue

        try:
            value = float(value)

            if 0 < value <= 1:
                value *= 100

            if 0 < value <= 100:
                return value
        except (ValueError, TypeError):
            pass

    return None


def is_forex_otc(symbol):
    if not isinstance(symbol, str):
        return False

    if not symbol.lower().endswith("_otc"):
        return False

    pair = symbol[:-4].upper()

    if len(pair) != 6:
        return False

    return (
        pair[:3] in FIAT
        and pair[3:] in FIAT
    )


def get_pairs():
    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    rows = extract_list(
        data,
        ("symbols", "data", "results")
    )

    pairs = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        symbol = row.get("symbol")

        if not is_forex_otc(symbol):
            continue

        payout = get_payout(row)

        if payout is None or payout < MIN_PAYOUT:
            continue

        if row.get("open") is False:
            continue

        pairs.append({
            "symbol": symbol,
            "payout": payout
        })

    return pairs


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
        ("candles", "data", "results", "history")
    )

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        timestamp = normalize_timestamp(
            row.get("time", row.get("timestamp"))
        )

        try:
            candle = {
                "time": timestamp,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            }

            if timestamp is None:
                continue

            if candle["high"] < candle["low"]:
                continue

            if not (
                candle["low"] <= candle["open"]
                and candle["low"] <= candle["close"]
                and candle["high"] >= candle["open"]
                and candle["high"] >= candle["close"]
            ):
                continue

            candles.append(candle)

        except (ValueError, TypeError, KeyError):
            continue

    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = sorted(
        unique.values(),
        key=lambda x: x["time"]
    )

    api_clock_now = int(time.time()) + OTC_CLOCK_OFFSET

    completed = [
        c for c in candles
        if c["time"] + TIMEFRAME <= api_clock_now
    ]

    logging.info(
        "%s raw=%s valid=%s completed=%s",
        symbol, len(rows), len(candles), len(completed)
    )

    if len(completed) < 60:
        raise RuntimeError(
            f"Not enough completed candles: {len(completed)}"
        )

    return completed


def ema(values, period):
    if len(values) < period:
        return []

    result = []
    multiplier = 2 / (period + 1)

    current = sum(values[:period]) / period
    result.append(current)

    for value in values[period:]:
        current = (
            value * multiplier
            + current * (1 - multiplier)
        )
        result.append(current)

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return []

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = []

    def calculate(gain, loss):
        if loss == 0:
            return 100
        return 100 - 100 / (1 + gain / loss)

    result.append(calculate(avg_gain, avg_loss))

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

        result.append(calculate(avg_gain, avg_loss))

    return result


def bollinger(values, period=20):
    if len(values) < period:
        return None

    recent = values[-period:]
    middle = statistics.mean(recent)
    deviation = statistics.pstdev(recent)

    return (
        middle - 2 * deviation,
        middle,
        middle + 2 * deviation
    )


def stochastic(candles, period=14):
    if len(candles) < period + 3:
        return [], []

    raw = []

    for i in range(period - 1, len(candles)):
        section = candles[i - period + 1:i + 1]

        highest = max(c["high"] for c in section)
        lowest = min(c["low"] for c in section)

        if highest == lowest:
            raw.append(50)
        else:
            raw.append(
                100 * (
                    candles[i]["close"] - lowest
                ) / (highest - lowest)
            )

    k = raw
    d = []

    for i in range(2, len(k)):
        d.append(statistics.mean(k[i - 2:i + 1]))

    return k, d


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    if not fast or not slow:
        return [], []

    offset = 26 - 12
    line = [
        fast[i + offset] - slow[i]
        for i in range(len(slow))
    ]

    signal = ema(line, 9)

    if not signal:
        return [], []

    return line[-len(signal):], signal


def supertrend_direction(candles):
    if len(candles) < 20:
        return 0

    closes = [c["close"] for c in candles]
    true_ranges = []

    for i in range(1, len(candles)):
        c = candles[i]
        previous = candles[i - 1]["close"]

        true_ranges.append(max(
            c["high"] - c["low"],
            abs(c["high"] - previous),
            abs(c["low"] - previous)
        ))

    atr = statistics.mean(true_ranges[-10:])

    last = candles[-1]
    previous = candles[-2]

    middle = (last["high"] + last["low"]) / 2

    upper = middle + 3 * atr
    lower = middle - 3 * atr

    if last["close"] > upper:
        return 1

    if last["close"] < lower:
        return -1

    if last["close"] > previous["close"] and last["close"] > middle:
        return 1

    if last["close"] < previous["close"] and last["close"] < middle:
        return -1

    return 0


def strategy_votes(candles):
    closes = [c["close"] for c in candles]

    fast = ema(closes, 9)
    mid = ema(closes, 21)
    slow = ema(closes, 50)
    rsi_values = rsi(closes)

    if not fast or not mid or not slow or len(rsi_values) < 2:
        raise RuntimeError("Indicator data incomplete")

    last = candles[-1]
    previous = candles[-2]

    close = last["close"]
    bullish = close > last["open"]
    bearish = close < last["open"]

    call = 0
    put = 0

    # 1. RSI Reversal
    if rsi_values[-2] < 30 and rsi_values[-1] >= 30:
        call += 1

    if rsi_values[-2] > 70 and rsi_values[-1] <= 70:
        put += 1

    # 2. EMA Cross
    if fast[-2] <= mid[-2] and fast[-1] > mid[-1]:
        call += 1

    if fast[-2] >= mid[-2] and fast[-1] < mid[-1]:
        put += 1

    # 3. Bollinger Bounce
    bands = bollinger(closes)

    if bands:
        lower, middle, upper = bands

        if previous["close"] <= lower and close > lower:
            call += 1

        if previous["close"] >= upper and close < upper:
            put += 1

    # 4. MACD Cross
    macd_line, macd_signal = macd(closes)

    if len(macd_line) >= 2 and len(macd_signal) >= 2:
        if (
            macd_line[-2] <= macd_signal[-2]
            and macd_line[-1] > macd_signal[-1]
        ):
            call += 1

        if (
            macd_line[-2] >= macd_signal[-2]
            and macd_line[-1] < macd_signal[-1]
        ):
            put += 1

    # 5. Stochastic Cross
    k, d = stochastic(candles)

    if len(k) >= 3 and len(d) >= 2:
        if k[-2] <= d[-1] and k[-1] > d[-1] and k[-1] < 35:
            call += 1

        if k[-2] >= d[-1] and k[-1] < d[-1] and k[-1] > 65:
            put += 1

    # 6. SuperTrend direction
    trend = supertrend_direction(candles)

    if trend == 1:
        call += 1

    if trend == -1:
        put += 1

    # 7. Combo
    if (
        rsi_values[-1] < 45
        and bands
        and close > bands[0]
        and fast[-1] > mid[-1]
    ):
        call += 1

    if (
        rsi_values[-1] > 55
        and bands
        and close < bands[2]
        and fast[-1] < mid[-1]
    ):
        put += 1

    # 8. Triple EMA alignment
    if fast[-1] > mid[-1] > slow[-1]:
        call += 1

    if fast[-1] < mid[-1] < slow[-1]:
        put += 1

    # Direction confirmation
    call_confirmed = (
        fast[-1] > mid[-1] > slow[-1]
        and bullish
    )

    put_confirmed = (
        fast[-1] < mid[-1] < slow[-1]
        and bearish
    )

    if call >= MIN_VOTES and call > put and call_confirmed:
        return "CALL", call, put

    if put >= MIN_VOTES and put > call and put_confirmed:
        return "PUT", call, put

    return None, call, put


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram secrets are missing")

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        },
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram rejected message: {result}"
        )

    logging.info("Telegram message sent successfully")


def main():
    logging.info("Starting %s", VERSION)

    try:
        pairs = get_pairs()

        logging.info(
            "Forex OTC pairs with payout >= %.1f%%: %s",
            MIN_PAYOUT, len(pairs)
        )

        candidates = []

        for pair in pairs:
            symbol = pair["symbol"]
            payout = pair["payout"]

            logging.info(
                "Scanning %s | payout %.1f%%",
                symbol, payout
            )

            try:
                candles = get_candles(symbol)

                direction, call, put = strategy_votes(candles)

                logging.info(
                    "%s CALL=%s PUT=%s direction=%s",
                    symbol, call, put, direction
                )

                if direction:
                    candidates.append({
                        "symbol": symbol,
                        "payout": payout,
                        "direction": direction,
                        "votes": max(call, put),
                        "call": call,
                        "put": put,
                        "last_candle": candles[-1]["time"]
                    })

            except Exception as exc:
                logging.warning(
                    "%s skipped: %s",
                    symbol, exc
                )

        logging.info(
            "Total valid candidates: %s",
            len(candidates)
        )

        if candidates:
            candidates.sort(
                key=lambda x: (
                    x["votes"],
                    x["payout"]
                ),
                reverse=True
            )

            best = candidates[0]

            emoji = (
                "🟢" if best["direction"] == "CALL"
                else "🔴"
            )

            message = (
                f"🤖 {VERSION}\n\n"
                f"{emoji} {best['direction']}\n\n"
                f"💱 PAIR: {best['symbol']}\n"
                f"💰 PAYOUT: {best['payout']:.1f}%\n"
                f"📊 STRATEGY SCORE: "
                f"{best['votes']}/8\n"
                f"🟢 CALL votes: {best['call']}\n"
                f"🔴 PUT votes: {best['put']}\n\n"
                f"⏱ TIMEFRAME: M1\n"
                f"⌛ EXPIRATION: 60 SECONDS\n"
                f"🕒 FLORIDA TIME: {florida_time()}\n\n"
                f"⚠️ Verify the live entry before trading."
            )

        else:
            message = (
                f"🤖 {VERSION}\n\n"
                f"⚪ NO VALID SIGNAL\n"
                f"No pair passed the strategy "
                f"and direction confirmation rules.\n\n"
                f"Pairs checked: {len(pairs)}\n"
                f"Minimum payout: {MIN_PAYOUT:.1f}%\n"
                f"🕒 FLORIDA TIME: {florida_time()}\n"
            )

        send_telegram(message)

    except Exception as exc:
        logging.exception("BOT FAILED")

        try:
            send_telegram(
                f"🚨 {VERSION} ERROR\n\n"
                f"{str(exc)[:2500]}\n"
                f"🕒 {florida_time()}"
            )
        except Exception:
            logging.exception("Could not send error to Telegram")

        raise


if __name__ == "__main__":
    main()
