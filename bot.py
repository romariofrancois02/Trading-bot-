
import os
import re
import time
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

# =====================================================
# ROMARIO TRADER PRO 10.5
# Pocket Option OTC | M30s | Expiration 60s
# Telegram alerts only
# EMA + STOCHASTIC + ADX + DI
# =====================================================

VERSION = "ROMARIO TRADER PRO 10.5"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 30
EXPIRATION = 60
CANDLE_LIMIT = 150
MIN_CANDLES = 60

MIN_PAYOUT = 92.0
ENTRY_LEAD = 15

ADX_PERIOD = 14
ADX_MIN = 18.0

STOCH_TOLERANCE = 10
MIN_SCORE = 5

# Kenbe 7200 sèlman si timestamp OTCharts yo bezwen ajisteman sa a.
# Si lè candle yo deja UTC nòmal, mete OTC_CLOCK_OFFSET=0 nan GitHub Secrets.
OTC_CLOCK_OFFSET = int(os.getenv("OTC_CLOCK_OFFSET", "7200"))

FIAT_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD",
    "NZD", "CAD", "CNY", "HKD", "SGD", "THB",
    "MXN", "BRL", "TRY", "ZAR", "NOK", "SEK"
}

FLORIDA = ZoneInfo("America/New_York")

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
    "User-Agent": "ROMARIO-TRADER-PRO/10.5"
})


# =====================================================
# GENERAL HELPERS
# =====================================================

def florida_time(timestamp=None):
    if timestamp is None:
        timestamp = time.time()

    return datetime.fromtimestamp(
        timestamp, FLORIDA
    ).strftime("%Y-%m-%d %I:%M:%S %p")


def api_get(endpoint, params=None):
    url = BASE_URL + endpoint

    response = session.get(
        url,
        params=params,
        timeout=25
    )

    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict):
        if data.get("error"):
            raise RuntimeError(str(data["error"]))

    return data


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
        raise RuntimeError("Telegram could not send message")


def extract_list(data, possible_keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in possible_keys:
            value = data.get(key)

            if isinstance(value, list):
                return value

        for value in data.values():
            if isinstance(value, dict):
                result = extract_list(value, possible_keys)

                if result:
                    return result

    return []


# =====================================================
# OTC PAIRS
# =====================================================

def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    symbols = extract_list(
        data,
        ("symbols", "data", "results")
    )

    eligible = []

    for item in symbols:
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

        if base not in FIAT_CODES or quote not in FIAT_CODES:
            continue

        is_otc = item.get("isOtc", True)
        is_open = item.get("open", True)

        if is_otc is False or is_open is False:
            continue

        try:
            payout = float(
                item.get("payout", item.get("payoutPercentage", 0))
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
# CANDLE DATA
# =====================================================

def normalize_timestamp(value):
    try:
        value = float(value)

        if value > 1000000000000:
            value = value / 1000

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
                f"Unexpected candle interval: {interval}"
            )

        gaps = data.get("gaps")

        if isinstance(gaps, dict):
            missing = gaps.get("missingBars", 0)

            if missing not in (0, None):
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

            if (
                candle["high"] < candle["low"]
                or candle["high"] < max(
                    candle["open"], candle["close"]
                )
                or candle["low"] > min(
                    candle["open"], candle["close"]
                )
            ):
                continue

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    # Retire candle ki poko fini.
    adjusted_now = time.time() + OTC_CLOCK_OFFSET

    candles = [
        candle for candle in candles
        if candle["time"] + TIMEFRAME <= adjusted_now
    ]

    # Verifye kontinwite dènye candle yo.
    if len(candles) >= 10:
        recent = candles[-10:]

        for previous, current in zip(recent, recent[1:]):
            difference = current["time"] - previous["time"]

            if abs(difference - TIMEFRAME) > 2:
                raise RuntimeError(
                    f"{symbol}: candle continuity problem"
                )

    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"{symbol}: not enough completed candles"
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
    raw_k = []

    for i in range(len(candles)):
        if i < 13:
            raw_k.append(None)
            continue

        window = candles[i - 13:i + 1]

        highest = max(x["high"] for x in window)
        lowest = min(x["low"] for x in window)

        if highest == lowest:
            raw_k.append(50.0)
        else:
            value = (
                (candles[i]["close"] - lowest)
                / (highest - lowest)
            ) * 100

            raw_k.append(value)

    smooth_k = [None] * len(candles)

    for i in range(len(candles)):
        if i < 15:
            continue

        values = raw_k[i - 2:i + 1]

        if all(v is not None for v in values):
            smooth_k[i] = sum(values) / 3

    smooth_d = [None] * len(candles)

    for i in range(len(candles)):
        if i < 17:
            continue

        values = smooth_k[i - 2:i + 1]

        if all(v is not None for v in values):
            smooth_d[i] = sum(values) / 3

    return smooth_k, smooth_d


# =====================================================
# ADX 14 + DI+ / DI-
# Wilder smoothing
# =====================================================

def calculate_adx(candles, period=14):
    length = len(candles)

    tr = [None] * length
    plus_dm = [None] * length
    minus_dm = [None] * length

    for i in range(1, length):
        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        plus_dm[i] = (
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus_dm[i] = (
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

        tr[i] = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

    smoothed_tr = [None] * length
    smoothed_plus = [None] * length
    smoothed_minus = [None] * length

    if length <= period:
        return [None] * length, [None] * length, [None] * length

    smoothed_tr[period] = sum(
        tr[1:period + 1]
    )

    smoothed_plus[period] = sum(
        plus_dm[1:period + 1]
    )

    smoothed_minus[period] = sum(
        minus_dm[1:period + 1]
    )

    for i in range(period + 1, length):
        smoothed_tr[i] = (
            smoothed_tr[i - 1]
            - smoothed_tr[i - 1] / period
            + tr[i]
        )

        smoothed_plus[i] = (
            smoothed_plus[i - 1]
            - smoothed_plus[i - 1] / period
            + plus_dm[i]
        )

        smoothed_minus[i] = (
            smoothed_minus[i - 1]
            - smoothed_minus[i - 1] / period
            + minus_dm[i]
        )

    plus_di = [None] * length
    minus_di = [None] * length
    dx = [None] * length

    for i in range(period, length):
        if not smoothed_tr[i]:
            continue

        plus_di[i] = (
            100 * smoothed_plus[i] / smoothed_tr[i]
        )

        minus_di[i] = (
            100 * smoothed_minus[i] / smoothed_tr[i]
        )

        denominator = plus_di[i] + minus_di[i]

        if denominator:
            dx[i] = (
                100
                * abs(plus_di[i] - minus_di[i])
                / denominator
            )

    adx = [None] * length

    first_adx_index = period * 2

    if first_adx_index >= length:
        return adx, plus_di, minus_di

    initial_dx = [
        dx[i]
        for i in range(period, first_adx_index + 1)
        if dx[i] is not None
    ]

    if len(initial_dx) < period:
        return adx, plus_di, minus_di

    adx[first_adx_index] = sum(initial_dx[-period:]) / period

    for i in range(first_adx_index + 1, length):
        if dx[i] is None:
            continue

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

    body = abs(candle["close"] - candle["open"])
    body_ratio = body / candle_range

    if body_ratio < 0.40:
        return False

    close_position = (
        candle["close"] - candle["low"]
    ) / candle_range

    if direction == "CALL":
        return (
            candle["close"] > candle["open"]
            and close_position >= 0.65
        )

    return (
        candle["close"] < candle["open"]
        and close_position <= 0.35
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
    pairs = CALL_PAIRS if direction == "CALL" else PUT_PAIRS

    for ref_k, ref_d in pairs:
        if (
            abs(k - ref_k) <= STOCH_TOLERANCE
            and abs(d - ref_d) <= STOCH_TOLERANCE
        ):
            return True

    return False


# =====================================================
# SIGNAL ANALYSIS
# =====================================================

def analyze_pair(pair):
    symbol = pair["symbol"]
    payout = pair["payout"]

    candles = get_candles(symbol)

    closes = [c["close"] for c in candles]

    ema9 = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)
    ema50 = calculate_ema(closes, 50)

    stoch_k, stoch_d = calculate_stochastic(candles)

    adx, plus_di, minus_di = calculate_adx(
        candles, ADX_PERIOD
    )

    i = len(candles) - 1
    previous = i - 1

    required = [
        ema9[i], ema21[i], ema50[i],
        ema9[previous], ema21[previous],
        stoch_k[i], stoch_d[i],
        stoch_k[previous], stoch_d[previous],
        adx[i], plus_di[i], minus_di[i]
    ]

    if any(value is None for value in required):
        return None

    k = stoch_k[i]
    d = stoch_d[i]

    score = 0
    direction = None

    # ---------------- CALL ----------------

    if (
        ema9[i] > ema21[i]
        and closes[i] > ema21[i]
        and k > d
        and adx[i] >= ADX_MIN
        and plus_di[i] > minus_di[i]
    ):
        direction = "CALL"
        score = 5

        if ema9[i] > ema9[previous]:
            score += 1

        if ema21[i] > ema21[previous]:
            score += 1

        if k > stoch_k[previous]:
            score += 1

        if ema9[i] > ema50[i]:
            score += 1

        if adx[i] > adx[previous]:
            score += 1

        if candle_quality(candles[i], "CALL"):
            score += 1

        if reference_match(k, d, "CALL"):
            score += 1

    # ---------------- PUT ----------------

    elif (
        ema9[i] < ema21[i]
        and closes[i] < ema21[i]
        and k < d
        and adx[i] >= ADX_MIN
        and minus_di[i] > plus_di[i]
    ):
        direction = "PUT"
        score = 5

        if ema9[i] < ema9[previous]:
            score += 1

        if ema21[i] < ema21[previous]:
            score += 1

        if k < stoch_k[previous]:
            score += 1

        if ema9[i] < ema50[i]:
            score += 1

        if adx[i] > adx[previous]:
            score += 1

        if candle_quality(candles[i], "PUT"):
            score += 1

        if reference_match(k, d, "PUT"):
            score += 1

    if direction is None or score < MIN_SCORE:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "payout": payout,
        "score": score,
        "k": k,
        "d": d,
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

    for number, pair in enumerate(pairs, start=1):
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
                    "VALID %s | %s | SCORE %s | ADX %.2f",
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
# ENTRY TIME
# =====================================================

def prepare_entry():
    now = time.time()

    next_boundary = (
        int(now // TIMEFRAME) + 1
    ) * TIMEFRAME

    entry_time = next_boundary
    send_time = entry_time - ENTRY_LEAD

    # Si fenèt 15 segonn lan prèske pase,
    # chwazi pwochen candle la.
    if send_time <= now + 2:
        entry_time += TIMEFRAME
        send_time += TIMEFRAME

    delay = send_time - time.time()

    if delay > 0:
        logging.info(
            "Waiting %.1f seconds for signal window",
            delay
        )
        time.sleep(delay)

    if time.time() > send_time + 2:
        logging.warning("Entry window missed. Cancelled.")
        return None

    return entry_time


# =====================================================
# TELEGRAM MESSAGE
# =====================================================

def build_signal_message(signal, entry_time):
    signal_time = time.time()

    entry_text = florida_time(entry_time)
    signal_text = florida_time(signal_time)

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

⭐ STRATEGY SCORE: {signal['score']}

🕒 SIGNAL TIME: {signal_text}
🎯 ENTRY TIME: {entry_text}

⚠️ Telegram alert only.
Verify the entry on Pocket Option.
""".strip()


def no_signal_message(total, errors):
    return f"""
🤖 {VERSION}

⚪ NO VALID SIGNAL

No pair passed all strategy filters.

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

    best = candidates[0]

    logging.info(
        "BEST SIGNAL: %s %s | SCORE %s",
        best["direction"],
        best["symbol"],
        best["score"]
    )

    entry_time = prepare_entry()

    if entry_time is None:
        telegram_send(
            "🤖 ROMARIO TRADER PRO 10.5\n"
            "⚪ SIGNAL CANCELLED\n"
            "Entry window missed. No late signal sent."
        )
        return

    # Pa voye signal si done candle la twò ansyen.
    adjusted_now = time.time() + OTC_CLOCK_OFFSET

    candle_age = (
        adjusted_now - best["candle_time"] - TIMEFRAME
    )

    if candle_age > TIMEFRAME + 5:
        telegram_send(
            "🤖 ROMARIO TRADER PRO 10.5\n"
            "⚪ SIGNAL CANCELLED\n"
            "Market data is too old. No late entry."
        )
        return

    message = build_signal_message(
        best,
        entry_time
    )

    telegram_send(message)

    logging.info(
        "Signal sent: %s %s",
        best["direction"],
        best["symbol"]
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        logging.exception("BOT FAILED: %s", error)

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            try:
                telegram_send(
                    f"🔴 {VERSION} ERROR\n{str(error)[:800]}"
                )
            except Exception:
                logging.exception("Telegram error notification failed")

        raise
