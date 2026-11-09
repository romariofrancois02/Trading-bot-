
import os
import time
import logging
import requests
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

# ==========================================
# ROMARIO TRADER PRO 7.4
# ==========================================

VERSION = "ROMARIO TRADER PRO 7.4"

BASE_URL = "https://otcharts.com"
TIMEFRAME = 60
CANDLE_LIMIT = 150
OTC_CLOCK_OFFSET = 2 * 60 * 60

MIN_PAYOUT = 92.0
MIN_VOTES = 2
EXPIRATION = 60

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "X-API-Key": API_KEY,
    "Accept": "application/json"
})

FIAT = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD",
    "CAD", "NZD", "CNY", "HKD", "SGD", "INR",
    "MXN", "ZAR", "TRY", "NOK", "SEK", "DKK",
    "PLN", "CZK", "HUF", "RON", "BGN", "THB",
    "MYR", "IDR", "PHP", "VND", "KRW", "AED",
    "SAR", "QAR", "KWD", "JOD", "LBP"
}


def florida_time():
    return datetime.now(
        ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d %I:%M:%S %p")


def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY missing")

    last_error = None

    for attempt in range(3):
        try:
            response = session.get(
                BASE_URL + endpoint,
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
                "API attempt %s: %s",
                attempt + 1, exc
            )
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(str(last_error))


def extract_list(data, keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]

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
        try:
            value = float(row.get(key))
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

    return (
        len(pair) == 6
        and pair[:3] in FIAT
        and pair[3:] in FIAT
    )


def get_pairs():
    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    rows = extract_list(
        data, ("symbols", "data", "results")
    )

    pairs = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        symbol = row.get("symbol")
        payout = get_payout(row)

        if not is_forex_otc(symbol):
            continue

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
        data, ("candles", "data", "results", "history")
    )

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        timestamp = normalize_timestamp(
            row.get("time", row.get("timestamp"))
        )

        try:
            c = {
                "time": timestamp,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            }

            if timestamp is None:
                continue

            if c["high"] < c["low"]:
                continue

            if not (
                c["low"] <= c["open"] <= c["high"]
                and c["low"] <= c["close"] <= c["high"]
            ):
                continue

            candles.append(c)

        except (ValueError, TypeError, KeyError):
            continue

    unique = {c["time"]: c for c in candles}

    candles = sorted(
        unique.values(),
        key=lambda x: x["time"]
    )

    api_now = int(time.time()) + OTC_CLOCK_OFFSET

    completed = [
        c for c in candles
        if c["time"] + TIMEFRAME <= api_now
    ]

    logging.info(
        "%s raw=%d valid=%d completed=%d",
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

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calc(g, l):
        if l == 0:
            return 100
        return 100 - 100 / (1 + g / l)

    result = [calc(avg_gain, avg_loss)]

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

        result.append(calc(avg_gain, avg_loss))

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

    k = []

    for i in range(period - 1, len(candles)):
        section = candles[i-period+1:i+1]
        high = max(c["high"] for c in section)
        low = min(c["low"] for c in section)

        value = 50 if high == low else (
            100 * (candles[i]["close"] - low) / (high - low)
        )

        k.append(value)

    d = [
        statistics.mean(k[i-2:i+1])
        for i in range(2, len(k))
    ]

    return k, d


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    if not fast or not slow:
        return [], []

    line = [
        fast[i + 14] - slow[i]
        for i in range(len(slow))
    ]

    signal = ema(line, 9)

    if not signal:
        return [], []

    return line[-len(signal):], signal


def supertrend_direction(candles):
    ranges = []

    for i in range(1, len(candles)):
        c = candles[i]
        previous = candles[i - 1]["close"]

        ranges.append(max(
            c["high"] - c["low"],
            abs(c["high"] - previous),
            abs(c["low"] - previous)
        ))

    if len(ranges) < 10:
        return 0

    atr = statistics.mean(ranges[-10:])
    last = candles[-1]
    middle = (last["high"] + last["low"]) / 2

    if last["close"] > middle + 3 * atr:
        return 1

    if last["close"] < middle - 3 * atr:
        return -1

    if last["close"] > candles[-2]["close"] and last["close"] > middle:
        return 1

    if last["close"] < candles[-2]["close"] and last["close"] < middle:
        return -1

    return 0


def strategy_votes(candles):
    close = [c["close"] for c in candles]

    fast = ema(close, 9)
    mid = ema(close, 21)
    slow = ema(close, 50)
    rv = rsi(close)

    if not fast or not mid or not slow or len(rv) < 2:
        raise RuntimeError("Indicators incomplete")

    last = candles[-1]
    previous = candles[-2]

    call = 0
    put = 0

    bullish = last["close"] > last["open"]
    bearish = last["close"] < last["open"]

    # 1. RSI reversal
    if rv[-2] < 30 <= rv[-1]:
        call += 1

    if rv[-2] > 70 >= rv[-1]:
        put += 1

    # 2. EMA cross
    if fast[-2] <= mid[-2] and fast[-1] > mid[-1]:
        call += 1

    if fast[-2] >= mid[-2] and fast[-1] < mid[-1]:
        put += 1

    # 3. Bollinger bounce
    bands = bollinger(close)

    if bands:
        lower, middle, upper = bands

        if previous["close"] <= lower and last["close"] > lower:
            call += 1

        if previous["close"] >= upper and last["close"] < upper:
            put += 1

    # 4. MACD cross
    ml, ms = macd(close)

    if len(ml) >= 2 and len(ms) >= 2:
        if ml[-2] <= ms[-2] and ml[-1] > ms[-1]:
            call += 1

        if ml[-2] >= ms[-2] and ml[-1] < ms[-1]:
            put += 1

    # 5. Stochastic cross
    k, d = stochastic(candles)

    if len(k) >= 3 and len(d) >= 2:
        if k[-2] <= d[-2] and k[-1] > d[-1] and k[-1] < 35:
            call += 1

        if k[-2] >= d[-2] and k[-1] < d[-1] and k[-1] > 65:
            put += 1

    # 6. SuperTrend direction
    trend = supertrend_direction(candles)

    if trend == 1:
        call += 1

    if trend == -1:
        put += 1

    # 7. Combo
    if bands:
        if rv[-1] < 45 and last["close"] > bands[0] and fast[-1] > mid[-1]:
            call += 1

        if rv[-1] > 55 and last["close"] < bands[2] and fast[-1] < mid[-1]:
            put += 1

    # 8. Triple EMA
    if fast[-1] > mid[-1] > slow[-1]:
        call += 1

    if fast[-1] < mid[-1] < slow[-1]:
        put += 1

    # No mandatory EMA confirmation.
    # Require the latest candle to agree with direction.
    if call >= MIN_VOTES and call > put and bullish:
        return "CALL", call, put

    if put >= MIN_VOTES and put > call and bearish:
        return "PUT", call, put

    return None, call, put


def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        raise RuntimeError("Telegram secrets missing")

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": TG_CHAT,
            "text": message
        },
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(str(result))

    logging.info("Telegram sent")


def main():
    logging.info("Starting %s", VERSION)

    try:
        pairs = get_pairs()

        logging.info(
            "Forex OTC pairs payout >= %.1f%%: %d",
            MIN_PAYOUT, len(pairs)
        )

        candidates = []

        for pair in pairs:
            symbol = pair["symbol"]
            payout = pair["payout"]

            logging.info(
                "Scanning %s payout %.1f%%",
                symbol, payout
            )

            try:
                candles = get_candles(symbol)
                direction, call, put = strategy_votes(candles)

                logging.info(
                    "%s CALL=%d PUT=%d result=%s",
                    symbol, call, put, direction
                )

                if direction:
                    candidates.append({
                        "symbol": symbol,
                        "payout": payout,
                        "direction": direction,
                        "votes": max(call, put),
                        "call": call,
                        "put": put
                    })

            except Exception as exc:
                logging.warning("%s skipped: %s", symbol, exc)

        if candidates:
            candidates.sort(
                key=lambda x: (x["votes"], x["payout"]),
                reverse=True
            )

            best = candidates[0]

            emoji = "🟢" if best["direction"] == "CALL" else "🔴"

            message = (
                f"🤖 {VERSION}\n\n"
                f"{emoji} {best['direction']}\n\n"
                f"💱 PAIR: {best['symbol']}\n"
                f"💰 PAYOUT: {best['payout']:.1f}%\n"
                f"📊 SCORE: {best['votes']}/8\n"
                f"🟢 CALL votes: {best['call']}\n"
                f"🔴 PUT votes: {best['put']}\n\n"
                f"⏱ TIMEFRAME: M1\n"
                f"⌛ EXPIRATION: 60 SECONDS\n"
                f"🕒 FLORIDA TIME: {florida_time()}\n\n"
                f"⚠️ Verify live entry before trading."
            )

        else:
            message = (
                f"🤖 {VERSION}\n\n"
                f"⚪ NO VALID SIGNAL\n"
                f"No pair passed the minimum vote rules.\n\n"
                f"Pairs checked: {len(pairs)}\n"
                f"Minimum payout: {MIN_PAYOUT:.1f}%\n"
                f"🕒 FLORIDA TIME: {florida_time()}"
            )

        send_telegram(message)

    except Exception as exc:
        logging.exception("BOT FAILED")

        try:
            send_telegram(
                f"🚨 {VERSION} ERROR\n\n{str(exc)[:2000]}"
            )
        except Exception:
            logging.exception("Telegram error notification failed")

        raise


if __name__ == "__main__":
    main()
