
import os
import time
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# =====================================================
# ROMARIO TRADER PRO 10.0
# OTC ONLY | M30 | 60 SECOND EXPIRATION
# =====================================================

BASE_URL = "https://otcharts.com"

TIMEFRAME = 30
CANDLE_LIMIT = 150
MIN_PAYOUT = 92.0
TOLERANCE = 10.0
EXPIRATION = 60
SIGNAL_LEAD_SECONDS = 15

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

FLORIDA = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "ROMARIO-TRADER-PRO/10.0"
})

# =====================================================
# STOCHASTIC REFERENCE PAIRS
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


# =====================================================
# TIME
# =====================================================

def format_time(timestamp):
    return datetime.fromtimestamp(
        timestamp, FLORIDA
    ).strftime("%Y-%m-%d %I:%M:%S %p")


# =====================================================
# API REQUEST
# =====================================================

def api_get(endpoint, params=None):
    for attempt in range(3):
        try:
            response = SESSION.get(
                BASE_URL + endpoint,
                params=params,
                timeout=20
            )

            response.raise_for_status()
            data = response.json()

            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(str(data["error"]))

            return data

        except Exception as exc:
            logging.warning(
                "API attempt %s failed: %s",
                attempt + 1, exc
            )
            time.sleep(2)

    return None


# =====================================================
# GET OTC SYMBOLS
# =====================================================

def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    if not data:
        return []

    symbols = data.get("symbols", [])

    if not isinstance(symbols, list):
        return []

    eligible = []

    for item in symbols:
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", ""))

        if not symbol.lower().endswith("_otc"):
            continue

        payout = item.get(
            "payout",
            item.get("payout_percent", 0)
        )

        try:
            payout = float(
                str(payout).replace("%", "")
            )
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        if item.get("open", True) is False:
            continue

        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    return eligible


# =====================================================
# GET 30 SECOND CANDLES
# =====================================================

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

    if not data:
        return []

    rows = data.get("candles")

    if not isinstance(rows, list):
        rows = data.get("data", [])

    if not isinstance(rows, list):
        return []

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            candle = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            }

            raw_time = row.get(
                "time",
                row.get("timestamp")
            )

            if raw_time is not None:
                ts = float(raw_time)

                if ts > 100000000000:
                    ts /= 1000

                candle["time"] = ts

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    if len(candles) < 60:
        return []

    # Keep only completed candles.
    if "time" in candles[-1]:
        now = time.time()

        candles = [
            candle for candle in candles
            if candle["time"] + TIMEFRAME <= now
        ]
    else:
        # Without timestamps, exclude the newest candle
        # to reduce the risk of using an unfinished candle.
        candles = candles[:-1]

    return candles


# =====================================================
# EMA
# =====================================================

def calculate_ema(values, period):
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

    return result


# =====================================================
# STOCHASTIC 14,3,3
# =====================================================

def calculate_stochastic(candles, period=14, smooth=3):
    if len(candles) < period + smooth:
        return None, None

    raw_k = []

    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1:i + 1]

        highest = max(x["high"] for x in window)
        lowest = min(x["low"] for x in window)
        close = candles[i]["close"]

        if highest == lowest:
            raw_k.append(50.0)
        else:
            raw_k.append(
                (close - lowest) /
                (highest - lowest) * 100
            )

    k_values = []

    for i in range(smooth - 1, len(raw_k)):
        k_values.append(
            sum(raw_k[i - smooth + 1:i + 1]) / smooth
        )

    if len(k_values) < smooth:
        return None, None

    d_values = []

    for i in range(smooth - 1, len(k_values)):
        d_values.append(
            sum(k_values[i - smooth + 1:i + 1]) / smooth
        )

    if not d_values:
        return None, None

    return k_values[-1], d_values[-1]


# =====================================================
# STOCHASTIC PAIR MATCH
# =====================================================

def match_pair(k, d, direction):
    pairs = (
        CALL_PAIRS
        if direction == "CALL"
        else PUT_PAIRS
    )

    best_pair = None
    best_distance = None

    for target_k, target_d in pairs:
        distance_k = abs(k - target_k)
        distance_d = abs(d - target_d)

        if (
            distance_k <= TOLERANCE
            and distance_d <= TOLERANCE
        ):
            total_distance = distance_k + distance_d

            if (
                best_distance is None
                or total_distance < best_distance
            ):
                best_distance = total_distance
                best_pair = (target_k, target_d)

    if best_pair is None:
        return False, None, None

    return True, best_pair, best_distance


# =====================================================
# ANALYZE EACH PAIR
# =====================================================

def analyze_symbol(item):
    symbol = item["symbol"]
    payout = item["payout"]

    candles = get_candles(symbol)

    if len(candles) < 60:
        logging.info("%s: insufficient candles", symbol)
        return None

    closes = [c["close"] for c in candles]

    ema9 = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)
    ema50 = calculate_ema(closes, 50)

    if not ema9 or not ema21 or not ema50:
        return None

    k, d = calculate_stochastic(candles)

    if k is None or d is None:
        return None

    last = candles[-1]

    # CALL: bullish trend and bullish candle.
    call_trend = (
        ema9[-1] > ema21[-1] > ema50[-1]
        and ema9[-1] > ema9[-2]
        and last["close"] > ema9[-1]
        and last["close"] > last["open"]
    )

    # PUT: bearish trend and bearish candle.
    put_trend = (
        ema9[-1] < ema21[-1] < ema50[-1]
        and ema9[-1] < ema9[-2]
        and last["close"] < ema9[-1]
        and last["close"] < last["open"]
    )

    call_match, call_ref, call_distance = match_pair(
        k, d, "CALL"
    )

    put_match, put_ref, put_distance = match_pair(
        k, d, "PUT"
    )

    if call_match and call_trend:
        direction = "CALL"
        reference = call_ref
        distance = call_distance

    elif put_match and put_trend:
        direction = "PUT"
        reference = put_ref
        distance = put_distance

    else:
        logging.info(
            "%s | K %.2f | D %.2f | no valid signal",
            symbol, k, d
        )
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "payout": payout,
        "k": k,
        "d": d,
        "reference": reference,
        "distance": distance
    }


# =====================================================
# ENTRY SCHEDULER - 30 SECOND CANDLES
# =====================================================

def wait_for_entry():
    now = time.time()

    entry_open = (
        (int(now // TIMEFRAME) + 1) * TIMEFRAME
    )

    send_at = entry_open - SIGNAL_LEAD_SECONDS

    # If scanning took too long, use the following candle.
    if send_at <= now:
        entry_open += TIMEFRAME
        send_at += TIMEFRAME

    delay = send_at - time.time()

    if delay > 0:
        logging.info(
            "Waiting %.1f seconds for alert time",
            delay
        )
        time.sleep(delay)

    if time.time() >= entry_open:
        logging.warning("Entry time passed. Signal skipped.")
        return None

    return entry_open


# =====================================================
# TELEGRAM
# =====================================================

def send_telegram(message):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
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
        return result.get("ok", False)

    except Exception as exc:
        logging.error("Telegram error: %s", exc)
        return False


# =====================================================
# SIGNAL MESSAGE
# =====================================================

def build_message(signal, entry_open):
    direction = signal["direction"]

    emoji = (
        "🟢" if direction == "CALL"
        else "🔴"
    )

    signal_time = format_time(time.time())
    entry_time = format_time(entry_open)

    return f"""
🤖 ROMARIO TRADER PRO 10.0

{emoji} {direction}

💱 PAIR: {signal['symbol']}
💰 PAYOUT: {signal['payout']:.1f}%

📊 STOCH K: {signal['k']:.2f}
📊 STOCH D: {signal['d']:.2f}

🎯 MATCHED PAIR: {signal['reference'][0]} / {signal['reference'][1]}
📏 DISTANCE: {signal['distance']:.2f}

⏱ TIMEFRAME: 30 SECONDS
⌛ EXPIRATION: 60 SECONDS

📨 SIGNAL TIME: {signal_time}
🎯 ENTRY TIME: {entry_time}

⚠️ TEST ON DEMO FIRST.
""".strip()


# =====================================================
# MAIN
# =====================================================

def main():
    logging.info("ROMARIO TRADER PRO 10.0 starting...")

    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram secrets are missing")

    symbols = get_symbols()

    logging.info(
        "Eligible OTC pairs with payout >= %.1f%%: %s",
        MIN_PAYOUT, len(symbols)
    )

    if not symbols:
        send_telegram(
            "🤖 ROMARIO TRADER PRO 10.0\n\n"
            "⚪ NO VALID SIGNAL\n"
            "No eligible OTC pairs returned."
        )
        return

    candidates = []

    for index, item in enumerate(symbols, start=1):
        logging.info(
            "Scanning %s (%s/%s)",
            item["symbol"], index, len(symbols)
        )

        try:
            signal = analyze_symbol(item)

            if signal:
                candidates.append(signal)

                logging.info(
                    "Candidate: %s %s | K %.2f D %.2f",
                    signal["direction"],
                    signal["symbol"],
                    signal["k"],
                    signal["d"]
                )

        except Exception:
            logging.exception(
                "Error analyzing %s",
                item["symbol"]
            )

    if not candidates:
        logging.info("No signal passed all filters.")

        send_telegram(
            "🤖 ROMARIO TRADER PRO 10.0\n\n"
            "⚪ NO VALID SIGNAL\n"
            "No pair passed the STOCHASTIC and EMA "
            "confirmation rules.\n\n"
            "No signal was forced."
        )
        return

    candidates.sort(
        key=lambda x: (
            x["distance"],
            -x["payout"]
        )
    )

    best = candidates[0]

    logging.info(
        "Best signal: %s %s",
        best["direction"],
        best["symbol"]
    )

    entry_open = wait_for_entry()

    if entry_open is None:
        logging.warning("Signal skipped.")
        return

    message = build_message(best, entry_open)

    if send_telegram(message):
        logging.info("Signal sent successfully.")
    else:
        logging.error("Signal sending failed.")


if __name__ == "__main__":
    main()
