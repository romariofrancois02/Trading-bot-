
import os
import re
import json
import time
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

# =========================================================
# ROMARIO TRADER PRO 11.0
# Pocket Option OTC | M1 | Expiration 120 seconds
# Telegram alerts only - NO automatic trading
# =========================================================

VERSION = "ROMARIO TRADER PRO 11.0"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 60
EXPIRATION = 120
CANDLE_LIMIT = 300
MIN_CANDLES = 80

MIN_PAYOUT = 92.0
ADX_MIN = 18.0
MIN_DI_GAP = 5.0
MIN_SCORE = 2

ENTRY_LEAD = 10
SCAN_INTERVAL = 10
MAX_SCAN_MINUTES = 300

API_ATTEMPTS = 4
STATE_FILE = "state.json"

# Verify this offset against the timestamps returned by OTCharts.
# Set OTC_CLOCK_OFFSET=0 in GitHub Secrets if timestamps are UTC.
OTC_CLOCK_OFFSET = int(os.getenv("OTC_CLOCK_OFFSET", "7200"))

FLORIDA = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": VERSION
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


# =========================================================
# TIME
# =========================================================

def florida_now():
    return datetime.now(FLORIDA)


def florida_time(timestamp=None):
    if timestamp is None:
        timestamp = time.time()

    return datetime.fromtimestamp(
        timestamp, FLORIDA
    ).strftime("%I:%M:%S %p").lstrip("0")


def otc_now():
    return time.time() + OTC_CLOCK_OFFSET


def next_entry_time():
    now = otc_now()
    boundary = (int(now // TIMEFRAME) + 1) * TIMEFRAME

    if boundary - now < ENTRY_LEAD:
        boundary += TIMEFRAME

    return boundary


# =========================================================
# API
# =========================================================

def api_get(endpoint, params=None):
    url = BASE_URL + endpoint

    for attempt in range(API_ATTEMPTS):
        try:
            response = session.get(
                url,
                params=params or {},
                timeout=20
            )

            if response.status_code in (429, 502, 503, 504):
                logging.warning(
                    "API temporary status: %s",
                    response.status_code
                )
                time.sleep(2 + attempt * 2)
                continue

            response.raise_for_status()
            return response.json()

        except Exception as exc:
            logging.warning(
                "API attempt %s failed: %s",
                attempt + 1,
                exc
            )
            time.sleep(2 + attempt * 2)

    return None


def extract_list(data, possible_keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in possible_keys:
            value = data.get(key)

            if isinstance(value, list):
                return value

        for value in data.values():
            if isinstance(value, (dict, list)):
                result = extract_list(value, possible_keys)

                if result:
                    return result

    return []


def get_symbols():
    data = api_get("/v1/symbols", {"venue": VENUE})

    if not data:
        return []

    rows = extract_list(
        data,
        ("symbols", "data", "instruments", "results")
    )

    output = []

    for row in rows:
        if isinstance(row, str):
            symbol = row
            payout = 0
        elif isinstance(row, dict):
            symbol = (
                row.get("symbol")
                or row.get("name")
                or row.get("ticker")
            )

            payout = (
                row.get("payout")
                or row.get("payout_percent")
                or row.get("return")
                or 0
            )
        else:
            continue

        if not symbol:
            continue

        symbol = str(symbol)

        if "otc" not in symbol.lower():
            continue

        try:
            payout = float(
                str(payout).replace("%", "")
            )
        except (TypeError, ValueError):
            payout = 0

        if payout >= MIN_PAYOUT:
            output.append({
                "symbol": symbol,
                "payout": payout
            })

    return output


def parse_timestamp(value):
    try:
        number = float(value)

        if number > 100000000000:
            number /= 1000

        return int(number)

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

    if not data:
        return []

    rows = extract_list(
        data,
        ("candles", "data", "results", "history")
    )

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        timestamp = parse_timestamp(
            row.get("timestamp")
            or row.get("time")
            or row.get("t")
            or row.get("open_time")
        )

        if timestamp is None:
            continue

        try:
            candle = {
                "time": timestamp,
                "open": float(row.get("open", row.get("o"))),
                "high": float(row.get("high", row.get("h"))),
                "low": float(row.get("low", row.get("l"))),
                "close": float(row.get("close", row.get("c")))
            }

            if candle["high"] < candle["low"]:
                continue

            candles.append(candle)

        except (TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    # Keep only completed candles.
    current_time = otc_now()

    candles = [
        c for c in candles
        if c["time"] + TIMEFRAME <= current_time
    ]

    # Remove duplicate timestamps.
    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = sorted(
        unique.values(),
        key=lambda x: x["time"]
    )

    # Check candle continuity.
    if len(candles) >= 2:
        continuous = []

        for candle in candles:
            if not continuous:
                continuous.append(candle)
                continue

            difference = (
                candle["time"] - continuous[-1]["time"]
            )

            if difference == TIMEFRAME:
                continuous.append(candle)

        candles = continuous

    return candles[-CANDLE_LIMIT:]


# =========================================================
# INDICATORS
# =========================================================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def stochastic(candles, period=14):
    if len(candles) < period + 3:
        return None, None

    k_values = []

    for index in range(len(candles) - 3, len(candles)):
        section = candles[index - period + 1:index + 1]

        highest = max(c["high"] for c in section)
        lowest = min(c["low"] for c in section)
        close = candles[index]["close"]

        if highest == lowest:
            k = 50.0
        else:
            k = (
                (close - lowest)
                / (highest - lowest)
            ) * 100

        k_values.append(k)

    if len(k_values) < 3:
        return None, None

    current_k = k_values[-1]
    current_d = sum(k_values[-3:]) / 3

    return current_k, current_d


def adx_values(candles, period=14):
    if len(candles) < period * 2 + 2:
        return None, None, None

    plus_dm = []
    minus_dm = []
    tr_values = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        plus_dm.append(
            up_move if up_move > down_move and up_move > 0 else 0
        )

        minus_dm.append(
            down_move if down_move > up_move and down_move > 0 else 0
        )

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        tr_values.append(tr)

    def wilder(values, length):
        if len(values) < length:
            return []

        current = sum(values[:length])
        output = [current]

        for value in values[length:]:
            current = current - current / length + value
            output.append(current)

        return output

    smooth_tr = wilder(tr_values, period)
    smooth_plus = wilder(plus_dm, period)
    smooth_minus = wilder(minus_dm, period)

    size = min(
        len(smooth_tr),
        len(smooth_plus),
        len(smooth_minus)
    )

    dx_values = []
    latest_plus = 0
    latest_minus = 0

    for i in range(size):
        tr = smooth_tr[i]

        if tr <= 0:
            continue

        plus_di = 100 * smooth_plus[i] / tr
        minus_di = 100 * smooth_minus[i] / tr

        denominator = plus_di + minus_di

        dx = (
            100 * abs(plus_di - minus_di) / denominator
            if denominator > 0 else 0
        )

        dx_values.append(dx)
        latest_plus = plus_di
        latest_minus = minus_di

    if len(dx_values) < period:
        return None, None, None

    adx = sum(dx_values[-period:]) / period

    return adx, latest_plus, latest_minus


# =========================================================
# CANDLE QUALITY
# =========================================================

def candle_quality(candle, direction):
    candle_range = candle["high"] - candle["low"]

    if candle_range <= 0:
        return False

    body = abs(candle["close"] - candle["open"])
    body_ratio = body / candle_range

    close_position = (
        (candle["close"] - candle["low"])
        / candle_range
    )

    if direction == "CALL":
        return (
            candle["close"] > candle["open"]
            and body_ratio >= 0.40
            and close_position >= 0.65
        )

    return (
        candle["close"] < candle["open"]
        and body_ratio >= 0.40
        and close_position <= 0.35
    )


# =========================================================
# STRATEGY
# =========================================================

def signal_at(candles):
    if len(candles) < MIN_CANDLES:
        return None

    closes = [c["close"] for c in candles]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)

    if None in (ema9, ema21, ema50):
        return None

    k, d = stochastic(candles)

    if k is None or d is None:
        return None

    adx, plus_di, minus_di = adx_values(candles)

    if adx is None:
        return None

    last = candles[-1]
    previous = candles[-2]

    close = last["close"]

    # ---------------- CALL ----------------

    call_conditions = (
        ema9 > ema21 > ema50
        and close > ema21
        and k > d
        and k > 50
        and k > previous["close"] * 0 + k
        and adx >= ADX_MIN
        and plus_di > minus_di
        and (plus_di - minus_di) >= MIN_DI_GAP
        and candle_quality(last, "CALL")
    )

    # ---------------- PUT -----------------

    put_conditions = (
        ema9 < ema21 < ema50
        and close < ema21
        and k < d
        and k < 50
        and adx >= ADX_MIN
        and minus_di > plus_di
        and (minus_di - plus_di) >= MIN_DI_GAP
        and candle_quality(last, "PUT")
    )

    if not call_conditions and not put_conditions:
        return None

    direction = "CALL" if call_conditions else "PUT"

    score = 0

    if adx >= 22:
        score += 1

    if abs(plus_di - minus_di) >= 8:
        score += 1

    if abs(k - d) >= 5:
        score += 1

    reference_pairs = (
        CALL_PAIRS if direction == "CALL" else PUT_PAIRS
    )

    pair_match = any(
        abs(k - pair[0]) <= 8
        and abs(d - pair[1]) <= 8
        for pair in reference_pairs
    )

    if pair_match:
        score += 1

    if score < MIN_SCORE:
        return None

    return {
        "direction": direction,
        "score": score,
        "k": k,
        "d": d,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "candle_time": last["time"]
    }


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram credentials are missing.")
        return False

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

        return bool(result.get("ok"))

    except Exception as exc:
        logging.error("Telegram error: %s", exc)
        return False


# =========================================================
# STATE
# =========================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "pending": None,
            "last_result": "NO PREVIOUS RESULT"
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        state.setdefault("pending", None)
        state.setdefault("last_result", "NO PREVIOUS RESULT")

        return state

    except Exception:
        return {
            "pending": None,
            "last_result": "NO PREVIOUS RESULT"
        }


def save_state(state):
    temporary = STATE_FILE + ".tmp"

    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=4)

    os.replace(temporary, STATE_FILE)


# =========================================================
# VERIFY PREVIOUS SIGNAL
# =========================================================

def verify_pending_result(state):
    pending = state.get("pending")

    if not pending:
        return True

    entry_time = int(pending["entry_timestamp"])
    expiry_time = entry_time + EXPIRATION

    if otc_now() < expiry_time + 5:
        logging.info("Previous signal has not expired yet.")
        return False

    candles = get_candles(pending["symbol"])

    if not candles:
        logging.warning("Cannot verify previous signal yet.")
        return False

    entry_candle = next(
        (c for c in candles if c["time"] == entry_time),
        None
    )

    # With candles timestamped at their opening time,
    # the candle beginning at entry + 60 closes at entry + 120.
    expiry_candle = next(
        (c for c in candles if c["time"] == entry_time + TIMEFRAME),
        None
    )

    if not entry_candle or not expiry_candle:
        logging.warning("Required result candles are not available.")
        return False

    entry_price = entry_candle["open"]
    exit_price = expiry_candle["close"]

    direction = pending["direction"]

    if direction == "CALL":
        result = "WIN" if exit_price > entry_price else "LOSS"
    else:
        result = "WIN" if exit_price < entry_price else "LOSS"

    state["last_result"] = (
        f"{result} | {pending['symbol']} | {direction}"
    )

    state["pending"] = None
    save_state(state)

    logging.info("Previous result: %s", state["last_result"])

    return True


# =========================================================
# SIGNAL MESSAGE
# =========================================================

def build_message(symbol, payout, signal, entry_timestamp, state):
    direction = signal["direction"]

    emoji = "🟢" if direction == "CALL" else "🔴"

    previous_result = state.get(
        "last_result",
        "NO PREVIOUS RESULT"
    )

    return (
        f"🤖 {VERSION}\n\n"
        f"{emoji} {direction}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"💰 PAYOUT: {payout:.1f}%\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 2 MINUTES\n\n"
        f"📊 STOCHASTIC K: {signal['k']:.2f}\n"
        f"📊 STOCHASTIC D: {signal['d']:.2f}\n"
        f"📈 ADX: {signal['adx']:.2f}\n"
        f"DI+: {signal['plus_di']:.2f}\n"
        f"DI-: {signal['minus_di']:.2f}\n\n"
        f"⭐ SIGNAL QUALITY: {signal['score']}/4\n"
        f"⚠️ Quality score is not a win probability.\n\n"
        f"🕒 SIGNAL TIME: {florida_time()}\n"
        f"🎯 ENTRY TIME: {florida_time(entry_timestamp)}\n\n"
        f"📋 PREVIOUS RESULT:\n{previous_result}"
    )


# =========================================================
# MARKET SCANNER
# =========================================================

def scan_market(symbols):
    best = None

    for index, item in enumerate(symbols, start=1):
        symbol = item["symbol"]
        payout = item["payout"]

        logging.info(
            "Scanning %s (%s/%s)",
            symbol,
            index,
            len(symbols)
        )

        candles = get_candles(symbol)

        if len(candles) < MIN_CANDLES:
            continue

        signal = signal_at(candles)

        if not signal:
            continue

        candidate = {
            "symbol": symbol,
            "payout": payout,
            "signal": signal
        }

        if (
            best is None
            or signal["score"] > best["signal"]["score"]
        ):
            best = candidate

    return best


# =========================================================
# MAIN
# =========================================================

def main():
    logging.info("%s starting...", VERSION)

    if not API_KEY:
        logging.error("OTCHARTS_API_KEY is missing.")
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram secrets are missing.")
        return

    state = load_state()

    # Never overwrite an unresolved previous signal.
    if not verify_pending_result(state):
        logging.info(
            "Waiting for previous signal result. No new signal sent."
        )
        return

    symbols = get_symbols()

    if not symbols:
        logging.error("No eligible OTC pairs found.")
        send_telegram(
            f"🤖 {VERSION}\n\n"
            "⚠️ NO OTC PAIRS FOUND\n"
            "Check OTCharts API response and payout data."
        )
        return

    logging.info(
        "Eligible OTC pairs found: %s",
        len(symbols)
    )

    start_time = time.time()

    while (time.time() - start_time) < MAX_SCAN_MINUTES * 60:
        candidate = scan_market(symbols)

        if not candidate:
            logging.info("NO VALID SIGNAL. Continuing scan...")
            time.sleep(SCAN_INTERVAL)
            continue

        entry_timestamp = next_entry_time()
        signal_timestamp = entry_timestamp - ENTRY_LEAD

        logging.info(
            "Candidate: %s %s | Entry: %s",
            candidate["symbol"],
            candidate["signal"]["direction"],
            florida_time(entry_timestamp)
        )

        # Wait until exactly 10 seconds before entry.
        while otc_now() < signal_timestamp:
            remaining = signal_timestamp - otc_now()
            time.sleep(min(max(remaining, 0.2), 2))

        # Recheck the same pair before sending.
        fresh_candles = get_candles(candidate["symbol"])

        if len(fresh_candles) < MIN_CANDLES:
            logging.info("Candidate expired. Rescanning.")
            continue

        fresh_signal = signal_at(fresh_candles)

        if not fresh_signal:
            logging.info("Setup no longer valid. Rescanning.")
            continue

        if fresh_signal["direction"] != candidate["signal"]["direction"]:
            logging.info("Direction changed. Rescanning.")
            continue

        # Do not send if the 10-second entry window was missed.
        if otc_now() > entry_timestamp - ENTRY_LEAD + 2:
            logging.info("Entry timing missed. Rescanning.")
            continue

        message = build_message(
            candidate["symbol"],
            candidate["payout"],
            fresh_signal,
            entry_timestamp,
            state
        )

        if not send_telegram(message):
            logging.error("Signal could not be delivered.")
            return

        state["pending"] = {
            "symbol": candidate["symbol"],
            "direction": fresh_signal["direction"],
            "entry_timestamp": int(entry_timestamp),
            "payout": candidate["payout"],
            "signal_timestamp": int(time.time())
        }

        save_state(state)

        logging.info(
            "Signal sent. Entry: %s",
            florida_time(entry_timestamp)
        )

        return

    logging.info("Scan time limit reached. No valid signal.")


if __name__ == "__main__":
    main()
