
import os
import time
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# =====================================================
# ROMARIO TRADER PRO 10.1
# OTC ONLY | 30 SECOND CANDLES | 60 SECOND EXPIRY
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
    "User-Agent": "ROMARIO-TRADER-PRO/10.1"
})

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
# API
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
            result = response.json()

            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(str(result["error"]))

            return result

        except Exception as exc:
            logging.warning(
                "API attempt %s failed: %s",
                attempt + 1, exc
            )
            time.sleep(2)

    return None


def extract_list(data, keys):
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in keys:
        value = data.get(key)

        if isinstance(value, list):
            return value

    nested = data.get("data")

    if isinstance(nested, dict):
        for key in keys:
            value = nested.get(key)

            if isinstance(value, list):
                return value

    return []


# =====================================================
# SYMBOLS
# =====================================================

def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    rows = extract_list(
        data,
        ("symbols", "data", "items", "results")
    )

    eligible = []

    for item in rows:
        if not isinstance(item, dict):
            continue

        symbol = str(
            item.get("symbol", item.get("name", ""))
        )

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

    # Remove duplicate symbols.
    unique = {}

    for item in eligible:
        unique[item["symbol"]] = item

    return list(unique.values())


# =====================================================
# CANDLES
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

    rows = extract_list(
        data,
        ("candles", "data", "items", "results")
    )

    if not rows:
        logging.warning(
            "%s: API returned no candle list",
            symbol
        )
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

    candles.sort(
        key=lambda x: x.get("time", 0)
    )

    if len(candles) < 60:
        logging.warning(
            "%s: only %s usable candles",
            symbol, len(candles)
        )
        return []

    # Exclude unfinished candle when timestamps exist.
    if "time" in candles[-1]:
        now = time.time()

        candles = [
            c for c in candles
            if c["time"] + TIMEFRAME <= now
        ]
    else:
        # No timestamp: safest option is to remove
        # the newest candle.
        candles = candles[:-1]

    if len(candles) < 60:
        return []

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

def calculate_stochastic(candles):
    period = 14
    smooth = 3

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
                (close - lowest)
                / (highest - lowest) * 100
            )

    k_values = []

    for i in range(smooth - 1, len(raw_k)):
        k_values.append(
            sum(raw_k[i - smooth + 1:i + 1])
            / smooth
        )

    d_values = []

    for i in range(smooth - 1, len(k_values)):
        d_values.append(
            sum(k_values[i - smooth + 1:i + 1])
            / smooth
        )

    if not d_values:
        return None, None

    return k_values[-1], d_values[-1]


# =====================================================
# MATCH STOCHASTIC PAIRS
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
            total = distance_k + distance_d

            if (
                best_distance is None
                or total < best_distance
            ):
                best_distance = total
                best_pair = (target_k, target_d)

    return (
        best_pair is not None,
        best_pair,
        best_distance
    )


# =====================================================
# ANALYZE ONE SYMBOL
# =====================================================

def analyze_symbol(item):
    symbol = item["symbol"]
    payout = item["payout"]

    candles = get_candles(symbol)

    if len(candles) < 60:
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

    # Less restrictive trend confirmation:
    # EMA 9 and EMA 21 direction, price location,
    # and candle direction must agree.
    call_trend = (
        ema9[-1] > ema21[-1]
        and ema9[-1] > ema9[-2]
        and last["close"] > ema21[-1]
        and last["close"] > last["open"]
    )

    put_trend = (
        ema9[-1] < ema21[-1]
        and ema9[-1] < ema9[-2]
        and last["close"] < ema21[-1]
        and last["close"] < last["open"]
    )

    call_match, call_ref, call_dist = match_pair(
        k, d, "CALL"
    )

    put_match, put_ref, put_dist = match_pair(
        k, d, "PUT"
    )

    logging.info(
        "%s | K=%.2f D=%.2f | "
        "CALL_MATCH=%s PUT_MATCH=%s | "
        "CALL_TREND=%s PUT_TREND=%s",
        symbol, k, d,
        call_match, put_match,
        call_trend, put_trend
    )

    if call_match and call_trend:
        direction = "CALL"
        reference = call_ref
        distance = call_dist

    elif put_match and put_trend:
        direction = "PUT"
        reference = put_ref
        distance = put_dist

    else:
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
# ENTRY TIMING
# =====================================================

def wait_for_entry():
    now = time.time()

    entry_open = (
        (int(now // TIMEFRAME) + 1)
        * TIMEFRAME
    )

    send_at = entry_open - SIGNAL_LEAD_SECONDS

    if send_at <= now:
        entry_open += TIMEFRAME
        send_at += TIMEFRAME

    delay = send_at - time.time()

    if delay > 0:
        logging.info(
            "Waiting %.2f seconds for alert",
            delay
        )
        time.sleep(delay)

    if time.time() >= entry_open:
        logging.warning(
            "Entry candle already opened. Skipping."
        )
        return None

    return entry_open


# =====================================================
# TELEGRAM
# =====================================================

def send_telegram(message):
    url = (
        "https://api.telegram.org/"
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
        logging.error(
            "Telegram error: %s",
            exc
        )
        return False


# =====================================================
# MESSAGE
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
🤖 ROMARIO TRADER PRO 10.1

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

⚠️ DEMO TEST FIRST
""".strip()


# =====================================================
# MAIN
# =====================================================

def main():
    logging.info(
        "ROMARIO TRADER PRO 10.1 STARTING"
    )

    if not API_KEY:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing"
        )

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Telegram secrets are missing"
        )

    symbols = get_symbols()

    logging.info(
        "OTC pairs with payout >= %.1f%%: %s",
        MIN_PAYOUT, len(symbols)
    )

    if not symbols:
        logging.error(
            "No eligible OTC symbols returned."
        )

        send_telegram(
            "🤖 ROMARIO TRADER PRO 10.1\n\n"
            "⚪ NO VALID SIGNAL\n"
            "No eligible OTC symbols were returned. "
            "Check OTCharts API response and payout fields."
        )
        return

    candidates = []
    scanned = 0
    candle_failures = 0

    for index, item in enumerate(symbols, start=1):
        logging.info(
            "Scanning %s (%s/%s)",
            item["symbol"],
            index,
            len(symbols)
        )

        scanned += 1

        try:
            signal = analyze_symbol(item)

            if signal:
                candidates.append(signal)

                logging.info(
                    "VALID CANDIDATE: %s %s | "
                    "K %.2f D %.2f | payout %.1f",
                    signal["direction"],
                    signal["symbol"],
                    signal["k"],
                    signal["d"],
                    signal["payout"]
                )

        except Exception:
            candle_failures += 1
            logging.exception(
                "Analysis failed for %s",
                item["symbol"]
            )

    logging.info(
        "SCAN COMPLETE | scanned=%s | candidates=%s | errors=%s",
        scanned, len(candidates), candle_failures
    )

    if not candidates:
        send_telegram(
            "🤖 ROMARIO TRADER PRO 10.1\n\n"
            "⚪ NO VALID SIGNAL\n\n"
            f"OTC PAIRS SCANNED: {scanned}\n"
            f"ANALYSIS ERRORS: {candle_failures}\n\n"
            "Check GitHub logs for K/D matches and EMA trend.\n"
            "No signal was forced."
        )
        return

    # Best match first, then highest payout.
    candidates.sort(
        key=lambda x: (
            x["distance"],
            -x["payout"]
        )
    )

    best = candidates[0]

    logging.info(
        "SELECTED: %s %s",
        best["direction"],
        best["symbol"]
    )

    entry_open = wait_for_entry()

    if entry_open is None:
        logging.warning(
            "Selected signal skipped due to timing."
        )
        return

    message = build_message(
        best,
        entry_open
    )

    if send_telegram(message):
        logging.info(
            "Telegram signal sent successfully."
        )
    else:
        logging.error(
            "Telegram message failed."
        )


if __name__ == "__main__":
    main()
