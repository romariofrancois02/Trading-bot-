
import os
import re
import time
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

# =====================================================
# ROMARIO TRADER PRO 10.6
# OTC | M30s | EXPIRATION 60s
# IMMEDIATE BEST SIGNAL
# =====================================================

VERSION = "ROMARIO TRADER PRO 10.6"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 30
EXPIRATION = 60
CANDLE_LIMIT = 150
MIN_CANDLES = 60

MIN_PAYOUT = 92.0
ADX_PERIOD = 14
ADX_MIN = 15.0
MIN_SCORE = 5

OTC_CLOCK_OFFSET = int(
    os.getenv("OTC_CLOCK_OFFSET", "7200")
)

FLORIDA = ZoneInfo("America/New_York")

FIAT_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF",
    "AUD", "NZD", "CAD", "CNY", "HKD",
    "SGD", "THB", "MXN", "BRL", "TRY",
    "ZAR", "NOK", "SEK"
}

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "ROMARIO-TRADER-PRO/10.6"
})


# =====================================================
# TIME
# =====================================================

def florida_time(timestamp=None):
    if timestamp is None:
        timestamp = time.time()

    return datetime.fromtimestamp(
        timestamp, FLORIDA
    ).strftime("%Y-%m-%d %I:%M:%S %p")


# =====================================================
# API
# =====================================================

def api_get(endpoint, params=None):
    response = session.get(
        BASE_URL + endpoint,
        params=params,
        timeout=25
    )

    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))

    return data


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
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
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
        raise RuntimeError("Telegram message failed")


# =====================================================
# OTC SYMBOLS
# =====================================================

def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    rows = extract_list(
        data,
        ("symbols", "data", "results")
    )

    eligible = []

    for item in rows:
        if not isinstance(item, dict):
            continue

        symbol = str(
            item.get("symbol", "")
        ).strip()

        match = re.fullmatch(
            r"([A-Z]{3})([A-Z]{3})_OTC",
            symbol,
            re.IGNORECASE
        )

        if not match:
            continue

        base = match.group(1).upper()
        quote = match.group(2).upper()

        if base not in FIAT_CODES:
            continue

        if quote not in FIAT_CODES:
            continue

        if item.get("isOtc") is False:
            continue

        if item.get("open") is False:
            continue

        try:
            payout = float(
                item.get(
                    "payout",
                    item.get("payoutPercentage", 0)
                )
            )
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    return eligible


# =====================================================
# CANDLES
# =====================================================

def normalize_timestamp(value):
    try:
        value = float(value)

        if value > 1000000000000:
            value /= 1000

        return value

    except (TypeError, ValueError):
        return None


def get_candles(symbol):
    data = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    if isinstance(data, dict):
        interval = data.get("interval")

        if interval and str(interval).lower() not in (
            "30s", "30", "30sec", "30seconds"
        ):
            raise RuntimeError(
                f"Unexpected interval: {interval}"
            )

        gaps = data.get("gaps")

        if isinstance(gaps, dict):
            if gaps.get("missingBars", 0) not in (0, None):
                raise RuntimeError(
                    f"{symbol}: missing candle bars"
                )

    rows = extract_list(
        data,
        ("candles", "data", "results")
    )

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        timestamp = normalize_timestamp(
            row.get("time", row.get("timestamp"))
        )

        if timestamp is None:
            continue

        try:
            candle = {
                "time": timestamp,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            }

            if candle["high"] < candle["low"]:
                continue

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    # Kenbe sèlman candle ki fini.
    adjusted_now = time.time() + OTC_CLOCK_OFFSET

    candles = [
        c for c in candles
        if c["time"] + TIMEFRAME <= adjusted_now
    ]

    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"{symbol}: not enough completed candles"
        )

    # Verifye dènye candle yo.
    recent = candles[-10:]

    for previous, current in zip(recent, recent[1:]):
        if abs(
            (current["time"] - previous["time"])
            - TIMEFRAME
        ) > 2:
            raise RuntimeError(
                f"{symbol}: candle continuity error"
            )

    return candles


# =====================================================
# EMA
# =====================================================

def calculate_ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    ema = sum(values[:period]) / period
    result = [None] * (period - 1) + [ema]

    for value in values[period:]:
        ema = (
            value * multiplier
            + ema * (1 - multiplier)
        )
        result.append(ema)

    return result


# =====================================================
# STOCHASTIC 14,3,3
# =====================================================

def calculate_stochastic(candles):
    raw = []

    for i in range(len(candles)):
        if i < 13:
            raw.append(None)
            continue

        window = candles[i - 13:i + 1]

        highest = max(x["high"] for x in window)
        lowest = min(x["low"] for x in window)

        if highest == lowest:
            raw.append(50.0)
        else:
            raw.append(
                100 * (
                    candles[i]["close"] - lowest
                ) / (highest - lowest)
            )

    k = [None] * len(candles)

    for i in range(15, len(candles)):
        values = raw[i - 2:i + 1]

        if all(v is not None for v in values):
            k[i] = sum(values) / 3

    d = [None] * len(candles)

    for i in range(17, len(candles)):
        values = k[i - 2:i + 1]

        if all(v is not None for v in values):
            d[i] = sum(values) / 3

    return k, d


# =====================================================
# ADX + DI
# =====================================================

def calculate_adx(candles, period=14):
    length = len(candles)

    tr = [None] * length
    plus_dm = [None] * length
    minus_dm = [None] * length

    for i in range(1, length):
        current = candles[i]
        previous = candles[i - 1]

        up = current["high"] - previous["high"]
        down = previous["low"] - current["low"]

        plus_dm[i] = (
            up if up > down and up > 0 else 0.0
        )

        minus_dm[i] = (
            down if down > up and down > 0 else 0.0
        )

        tr[i] = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

    smooth_tr = [None] * length
    smooth_plus = [None] * length
    smooth_minus = [None] * length

    if length <= period * 2:
        return (
            [None] * length,
            [None] * length,
            [None] * length
        )

    smooth_tr[period] = sum(tr[1:period + 1])
    smooth_plus[period] = sum(plus_dm[1:period + 1])
    smooth_minus[period] = sum(minus_dm[1:period + 1])

    for i in range(period + 1, length):
        smooth_tr[i] = (
            smooth_tr[i - 1]
            - smooth_tr[i - 1] / period
            + tr[i]
        )

        smooth_plus[i] = (
            smooth_plus[i - 1]
            - smooth_plus[i - 1] / period
            + plus_dm[i]
        )

        smooth_minus[i] = (
            smooth_minus[i - 1]
            - smooth_minus[i - 1] / period
            + minus_dm[i]
        )

    plus_di = [None] * length
    minus_di = [None] * length
    dx = [None] * length
    adx = [None] * length

    for i in range(period, length):
        if not smooth_tr[i]:
            continue

        plus_di[i] = (
            100 * smooth_plus[i] / smooth_tr[i]
        )

        minus_di[i] = (
            100 * smooth_minus[i] / smooth_tr[i]
        )

        denominator = plus_di[i] + minus_di[i]

        if denominator:
            dx[i] = (
                100 * abs(plus_di[i] - minus_di[i])
                / denominator
            )

    first = period * 2

    if first >= length:
        return adx, plus_di, minus_di

    initial = [
        dx[i]
        for i in range(period, first + 1)
        if dx[i] is not None
    ]

    if len(initial) < period:
        return adx, plus_di, minus_di

    adx[first] = sum(initial[-period:]) / period

    for i in range(first + 1, length):
        if dx[i] is not None:
            adx[i] = (
                adx[i - 1] * (period - 1) + dx[i]
            ) / period

    return adx, plus_di, minus_di


# =====================================================
# CANDLE QUALITY
# =====================================================

def candle_quality(candle, direction):
    candle_range = candle["high"] - candle["low"]

    if candle_range <= 0:
        return False

    body = abs(
        candle["close"] - candle["open"]
    )

    if body / candle_range < 0.40:
        return False

    position = (
        candle["close"] - candle["low"]
    ) / candle_range

    if direction == "CALL":
        return (
            candle["close"] > candle["open"]
            and position >= 0.65
        )

    return (
        candle["close"] < candle["open"]
        and position <= 0.35
    )


# =====================================================
# STOCHASTIC REFERENCE BONUS
# =====================================================

CALL_PAIRS = [
    (50, 63), (73, 63), (27, 43),
    (24, 45), (37, 45), (43, 36),
    (49, 35), (42, 18), (44, 16),
    (78, 76), (42, 21), (20, 31),
    (42, 63)
]

PUT_PAIRS = [
    (54, 70), (51, 56), (74, 81),
    (48, 71), (28, 41), (78, 53),
    (70, 53), (70, 57), (68, 37),
    (51, 44), (69, 54)
]


def reference_match(k, d, direction):
    pairs = (
        CALL_PAIRS if direction == "CALL"
        else PUT_PAIRS
    )

    return any(
        abs(k - ref_k) <= 10
        and abs(d - ref_d) <= 10
        for ref_k, ref_d in pairs
    )


# =====================================================
# ANALYZE ONE PAIR
# =====================================================

def analyze_pair(pair):
    symbol = pair["symbol"]
    candles = get_candles(symbol)

    closes = [c["close"] for c in candles]

    ema9 = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)
    ema50 = calculate_ema(closes, 50)

    k, d = calculate_stochastic(candles)

    adx, plus_di, minus_di = calculate_adx(
        candles, ADX_PERIOD
    )

    i = len(candles) - 1
    p = i - 1

    values = [
        ema9[i], ema21[i], ema50[i],
        ema9[p], ema21[p],
        k[i], d[i], k[p], d[p],
        adx[i], plus_di[i], minus_di[i]
    ]

    if any(v is None for v in values):
        return None

    direction = None
    score = 0

    # CALL
    if (
        ema9[i] > ema21[i]
        and closes[i] > ema21[i]
        and k[i] > d[i]
        and adx[i] >= ADX_MIN
        and plus_di[i] > minus_di[i]
    ):
        direction = "CALL"
        score = 5

        if ema9[i] > ema9[p]:
            score += 1

        if ema21[i] > ema21[p]:
            score += 1

        if k[i] > k[p]:
            score += 1

        if ema9[i] > ema50[i]:
            score += 1

        if adx[i] > adx[p]:
            score += 1

        if candle_quality(candles[i], "CALL"):
            score += 1

        if reference_match(k[i], d[i], "CALL"):
            score += 1

    # PUT
    elif (
        ema9[i] < ema21[i]
        and closes[i] < ema21[i]
        and k[i] < d[i]
        and adx[i] >= ADX_MIN
        and minus_di[i] > plus_di[i]
    ):
        direction = "PUT"
        score = 5

        if ema9[i] < ema9[p]:
            score += 1

        if ema21[i] < ema21[p]:
            score += 1

        if k[i] < k[p]:
            score += 1

        if ema9[i] < ema50[i]:
            score += 1

        if adx[i] > adx[p]:
            score += 1

        if candle_quality(candles[i], "PUT"):
            score += 1

        if reference_match(k[i], d[i], "PUT"):
            score += 1

    if direction is None or score < MIN_SCORE:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "payout": pair["payout"],
        "score": score,
        "k": k[i],
        "d": d[i],
        "adx": adx[i],
        "plus_di": plus_di[i],
        "minus_di": minus_di[i],
        "candle_time": candles[i]["time"]
    }


# =====================================================
# SCAN ALL PAIRS
# =====================================================

def scan_market():
    pairs = get_symbols()

    logging.info(
        "Eligible Forex OTC pairs: %s",
        len(pairs)
    )

    candidates = []
    errors = 0

    for number, pair in enumerate(pairs, 1):
        symbol = pair["symbol"]

        logging.info(
            "Scanning %s (%s/%s)",
            symbol, number, len(pairs)
        )

        try:
            result = analyze_pair(pair)

            if result:
                candidates.append(result)

                logging.info(
                    "VALID %s %s | SCORE %s | ADX %.2f",
                    symbol,
                    result["direction"],
                    result["score"],
                    result["adx"]
                )

        except Exception as error:
            errors += 1

            logging.warning(
                "%s error: %s",
                symbol, error
            )

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["adx"],
            x["payout"]
        ),
        reverse=True
    )

    return candidates, len(pairs), errors


# =====================================================
# MESSAGE
# =====================================================

def build_signal_message(signal):
    now = time.time()

    return f"""
🤖 {VERSION}

🟢 {signal['direction']}

💱 PAIR: {signal['symbol']}
💰 PAYOUT: {signal['payout']:.1f}%

⏱ TIMEFRAME: 30 SECONDS
⌛ EXPIRATION: 60 SECONDS

📊 STOCH K: {signal['k']:.2f}
📊 STOCH D: {signal['d']:.2f}

📈 ADX: {signal['adx']:.2f}
➕ DI+: {signal['plus_di']:.2f}
➖ DI-: {signal['minus_di']:.2f}

⭐ SCORE: {signal['score']}

🕒 SIGNAL TIME: {florida_time(now)}
🎯 ENTRY TIME: IMMEDIATE

⚠️ Check the live chart before entering.
Telegram alert only.
""".strip()


def no_signal_message(total, errors):
    return f"""
🤖 {VERSION}

⚪ NO VALID SIGNAL

No pair passed the strategy filters.

Pairs checked: {total}
Minimum payout: {MIN_PAYOUT}%
Timeframe: 30 seconds
Expiration: 60 seconds
ADX minimum: {ADX_MIN}

Errors: {errors}

No signal was forced.
""".strip()


# =====================================================
# MAIN
# =====================================================

def main():
    logging.info("%s starting...", VERSION)

    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing")

    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is missing")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is missing")

    candidates, total, errors = scan_market()

    if not candidates:
        telegram_send(
            no_signal_message(total, errors)
        )

        logging.info("No valid signal.")
        return

    # Verifye pi bon kandida yo ankò ak done ki pi resan.
    best = None

    for candidate in candidates:
        pair = {
            "symbol": candidate["symbol"],
            "payout": candidate["payout"]
        }

        try:
            refreshed = analyze_pair(pair)

            if refreshed:
                best = refreshed
                break

        except Exception as error:
            logging.warning(
                "Refresh failed for %s: %s",
                candidate["symbol"],
                error
            )

    if best is None:
        telegram_send(
            no_signal_message(total, errors)
        )
        return

    logging.info(
        "BEST SIGNAL: %s %s | SCORE %s",
        best["direction"],
        best["symbol"],
        best["score"]
    )

    # Pa gen WAIT. Voye pi bon signal la touswit.
    telegram_send(
        build_signal_message(best)
    )

    logging.info(
        "SIGNAL SENT: %s %s",
        best["direction"],
        best["symbol"]
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        logging.exception(
            "BOT FAILED: %s",
            error
        )

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            try:
                telegram_send(
                    f"🔴 {VERSION} ERROR\n{str(error)[:800]}"
                )
            except Exception:
                logging.exception(
                    "Telegram error notification failed"
                )

        raise
