
import os
import re
import json
import time
import logging
import requests

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# =====================================================
# ROMARIO TRADER PRO 11.9
# Pocket Option Forex OTC | M1 | Expiration 60s
# Telegram alerts only
# =====================================================

VERSION = "ROMARIO TRADER PRO 11.9"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 60
EXPIRATION = 60
CANDLE_LIMIT = 300
MIN_CANDLES = 80

MIN_PAYOUT = 92.0
MAX_SYMBOLS = 0

# Trend filters
ADX_PERIOD = 14
ADX_MIN = 19
MIN_DI_GAP = 3

# Fast indicators
RSI_PERIOD = 7
STOCH_PERIOD = 14

# Six confirmation conditions
MIN_SCORE = 5

# Signal timing
ENTRY_LEAD = 10
SCAN_INTERVAL = 45
MAX_SCAN_MINUTES = 300

API_ATTEMPTS = 3
API_TIMEOUT = 20

STATE_FILE = "state.json"
RECENT_SYMBOL_LIMIT = 2

OTCHARTS_TIMESTAMP_OFFSET = int(
    os.getenv("OTCHARTS_TIMESTAMP_OFFSET", "7200")
)

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


# =====================================================
# TIME
# =====================================================

def florida_time(timestamp=None):
    if timestamp is None:
        timestamp = time.time()

    return datetime.fromtimestamp(
        timestamp, FLORIDA
    ).strftime("%I:%M:%S %p").lstrip("0")


def next_entry_time():
    now = time.time()

    entry = (
        int(now // TIMEFRAME) + 1
    ) * TIMEFRAME

    if entry - now < ENTRY_LEAD:
        entry += TIMEFRAME

    return entry


# =====================================================
# API REQUEST
# =====================================================

def api_get(endpoint, params=None):
    for attempt in range(API_ATTEMPTS):
        try:
            response = session.get(
                BASE_URL + endpoint,
                params=params or {},
                timeout=API_TIMEOUT
            )

            if response.status_code in (
                429, 502, 503, 504
            ):
                logging.warning(
                    "Temporary API status: %s",
                    response.status_code
                )

                if attempt + 1 < API_ATTEMPTS:
                    time.sleep(3 + attempt * 3)

                continue

            response.raise_for_status()
            return response.json()

        except Exception as exc:
            logging.warning(
                "API attempt %s failed: %s",
                attempt + 1,
                exc
            )

            if attempt + 1 < API_ATTEMPTS:
                time.sleep(2 + attempt * 2)

    return None


def extract_list(data, keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)

            if isinstance(value, list):
                return value

        for value in data.values():
            if isinstance(value, (dict, list)):
                result = extract_list(value, keys)

                if result:
                    return result

    return []


# =====================================================
# FOREX OTC SYMBOL FILTER
# =====================================================

def is_forex_otc_symbol(symbol):
    if not symbol:
        return False

    symbol = str(symbol).strip().upper()

    if symbol.startswith("#"):
        return False

    return bool(
        re.fullmatch(r"[A-Z]{6}_OTC", symbol)
    )


# =====================================================
# GET SYMBOLS
# =====================================================

def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    if data is None:
        logging.error("Symbols API unavailable.")
        return None

    rows = extract_list(
        data,
        ("symbols", "data", "instruments", "results")
    )

    symbols = []
    seen = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        symbol = (
            row.get("symbol")
            or row.get("name")
            or row.get("ticker")
        )

        if not symbol:
            continue

        symbol = str(symbol).strip()

        if not is_forex_otc_symbol(symbol):
            continue

        if row.get("isOtc") is not True:
            continue

        if row.get("open") is not True:
            continue

        payout = row.get("payout")

        if payout is None:
            continue

        try:
            payout = float(
                str(payout).replace("%", "")
            )

            if 0 < payout <= 1:
                payout *= 100

        except (ValueError, TypeError):
            continue

        if payout < MIN_PAYOUT:
            continue

        symbol_key = symbol.upper()

        if symbol_key in seen:
            continue

        seen.add(symbol_key)

        symbols.append({
            "symbol": symbol,
            "payout": payout
        })

    symbols.sort(
        key=lambda item: item["payout"],
        reverse=True
    )

    if MAX_SYMBOLS > 0:
        symbols = symbols[:MAX_SYMBOLS]

    logging.info(
        "Eligible Forex OTC pairs: %s",
        len(symbols)
    )

    return symbols


# =====================================================
# CANDLE TIMESTAMP
# =====================================================

def parse_timestamp(value):
    if value is None:
        return None

    try:
        if isinstance(value, str):
            try:
                value = float(value)

            except ValueError:
                parsed = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )

                if parsed.tzinfo is None:
                    parsed = parsed.replace(
                        tzinfo=timezone.utc
                    )

                value = parsed.timestamp()

        value = float(value)

        if value > 100000000000:
            value /= 1000

        return (
            int(value)
            - OTCHARTS_TIMESTAMP_OFFSET
        )

    except (
        ValueError,
        TypeError,
        OverflowError
    ):
        return None


# =====================================================
# GET CANDLES
# =====================================================

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

    if data is None:
        return []

    rows = extract_list(
        data,
        ("candles", "data", "results", "history")
    )

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        raw_time = (
            row.get("time")
            or row.get("timestamp")
            or row.get("t")
            or row.get("open_time")
        )

        timestamp = parse_timestamp(raw_time)

        if timestamp is None:
            continue

        try:
            candle = {
                "time": timestamp,
                "open": float(
                    row.get("open", row.get("o"))
                ),
                "high": float(
                    row.get("high", row.get("h"))
                ),
                "low": float(
                    row.get("low", row.get("l"))
                ),
                "close": float(
                    row.get("close", row.get("c"))
                )
            }

            prices = (
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"]
            )

            if not all(value > 0 for value in prices):
                continue

            if candle["high"] < max(
                candle["open"],
                candle["close"]
            ):
                continue

            if candle["low"] > min(
                candle["open"],
                candle["close"]
            ):
                continue

            if candle["high"] < candle["low"]:
                continue

            candles.append(candle)

        except (ValueError, TypeError):
            continue

    unique = {
        candle["time"]: candle
        for candle in candles
    }

    candles = sorted(
        unique.values(),
        key=lambda item: item["time"]
    )

    # Completed candles only.
    candles = [
        candle for candle in candles
        if candle["time"] + TIMEFRAME <= time.time()
    ]

    # Keep consecutive candles only.
    continuous = []

    for candle in candles:
        if not continuous:
            continuous.append(candle)

        elif (
            candle["time"] - continuous[-1]["time"]
            == TIMEFRAME
        ):
            continuous.append(candle)

        else:
            continuous = [candle]

    return continuous[-CANDLE_LIMIT:]


# =====================================================
# EMA
# =====================================================

def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def ema_previous(values, period):
    if len(values) < period + 1:
        return None

    return ema(values[:-1], period)


# =====================================================
# RSI
# =====================================================

def calculate_rsi(values, period=7):
    if len(values) < period + 2:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [
        max(change, 0)
        for change in changes
    ]

    losses = [
        max(-change, 0)
        for change in changes
    ]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1)
            + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[i]
        ) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0

    if avg_loss == 0:
        return 100.0

    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# =====================================================
# STOCHASTIC
# =====================================================

def stochastic(candles, period=14):
    if len(candles) < period + 3:
        return None, None, None

    k_values = []

    for index in range(
        len(candles) - 3,
        len(candles)
    ):
        section = candles[
            index - period + 1:index + 1
        ]

        highest = max(
            c["high"] for c in section
        )

        lowest = min(
            c["low"] for c in section
        )

        if highest == lowest:
            value = 50.0

        else:
            value = (
                (
                    candles[index]["close"]
                    - lowest
                )
                / (highest - lowest)
            ) * 100

        k_values.append(
            max(0.0, min(100.0, value))
        )

    k = k_values[-1]
    d = sum(k_values[-3:]) / 3
    old_k = k_values[-2]

    return k, d, old_k


# =====================================================
# ADX / DI
# =====================================================

def adx_values(candles, period=14):
    if len(candles) < period * 2 + 2:
        return None, None, None

    plus_dm = []
    minus_dm = []
    tr_values = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up = (
            current["high"]
            - previous["high"]
        )

        down = (
            previous["low"]
            - current["low"]
        )

        plus_dm.append(
            up if up > down and up > 0 else 0.0
        )

        minus_dm.append(
            down if down > up and down > 0 else 0.0
        )

        tr_values.append(max(
            current["high"] - current["low"],
            abs(
                current["high"] - previous["close"]
            ),
            abs(
                current["low"] - previous["close"]
            )
        ))

    if len(tr_values) < period * 2:
        return None, None, None

    smoothed_tr = sum(tr_values[:period])
    smoothed_plus = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])

    dx_values = []
    last_plus = None
    last_minus = None

    for i in range(period - 1, len(tr_values)):

        if i > period - 1:
            smoothed_tr = (
                smoothed_tr
                - smoothed_tr / period
                + tr_values[i]
            )

            smoothed_plus = (
                smoothed_plus
                - smoothed_plus / period
                + plus_dm[i]
            )

            smoothed_minus = (
                smoothed_minus
                - smoothed_minus / period
                + minus_dm[i]
            )

        if smoothed_tr <= 0:
            continue

        last_plus = (
            100 * smoothed_plus / smoothed_tr
        )

        last_minus = (
            100 * smoothed_minus / smoothed_tr
        )

        total = last_plus + last_minus

        dx = (
            100 * abs(last_plus - last_minus) / total
            if total > 0 else 0.0
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None, None, None

    adx = sum(dx_values[:period]) / period

    for dx in dx_values[period:]:
        adx = (
            adx * (period - 1) + dx
        ) / period

    return adx, last_plus, last_minus


# =====================================================
# CANDLE QUALITY
# =====================================================

def candle_quality(candle, direction):
    candle_range = (
        candle["high"] - candle["low"]
    )

    if candle_range <= 0:
        return False

    body = abs(
        candle["close"] - candle["open"]
    )

    body_ratio = body / candle_range

    close_position = (
        candle["close"] - candle["low"]
    ) / candle_range

    if direction == "CALL":
        return (
            candle["close"] > candle["open"]
            and body_ratio >= 0.30
            and close_position >= 0.60
        )

    return (
        candle["close"] < candle["open"]
        and body_ratio >= 0.30
        and close_position <= 0.40
    )


# =====================================================
# STRATEGY ANALYSIS
# Exactly SIX score conditions:
# 1 Stochastic
# 2 RSI
# 3 ADX
# 4 DI gap
# 5 Candle quality
# 6 EMA slope
# =====================================================

def analyze_candles(candles):
    if len(candles) < MIN_CANDLES:
        return None

    closes = [
        candle["close"]
        for candle in candles
    ]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)

    old_ema9 = ema_previous(closes, 9)
    old_ema21 = ema_previous(closes, 21)

    rsi = calculate_rsi(
        closes,
        RSI_PERIOD
    )

    k, d, old_k = stochastic(
        candles,
        STOCH_PERIOD
    )

    adx, plus_di, minus_di = adx_values(
        candles,
        ADX_PERIOD
    )

    values = (
        ema9, ema21, ema50,
        old_ema9, old_ema21,
        rsi, k, d, old_k,
        adx, plus_di, minus_di
    )

    if any(value is None for value in values):
        return None

    last = candles[-1]
    close = last["close"]

    # Trend direction
    call_trend = (
        ema9 > ema21
        and ema21 > ema50
        and close > ema21
    )

    put_trend = (
        ema9 < ema21
        and ema21 < ema50
        and close < ema21
    )

    if not call_trend and not put_trend:
        return None

    # ---------------- CALL ----------------

    if call_trend:

        if plus_di <= minus_di or k <= d:
            return None

        if not (
            20 <= k <= 80
        ) or not (
            45 <= rsi <= 68
        ):
            return None

        direction = "CALL"

        stoch_ok = (
            k > d
            and k > old_k
            and 20 <= k <= 80
        )

        rsi_ok = (
            45 <= rsi <= 68
        )

        adx_ok = (
            adx >= ADX_MIN
        )

        di_ok = (
            plus_di - minus_di >= MIN_DI_GAP
        )

        candle_ok = candle_quality(
            last,
            "CALL"
        )

        ema_slope_ok = (
            ema9 > old_ema9
            and ema21 > old_ema21
        )

    # ---------------- PUT ----------------

    else:

        if minus_di <= plus_di or k >= d:
            return None

        if not (
            22 <= k <= 80
        ) or not (
            34 <= rsi <= 55
        ):
            return None

        direction = "PUT"

        stoch_ok = (
            k < d
            and k < old_k
            and 22 <= k <= 80
        )

        rsi_ok = (
            34 <= rsi <= 55
        )

        adx_ok = (
            adx >= ADX_MIN
        )

        di_ok = (
            minus_di - plus_di >= MIN_DI_GAP
        )

        candle_ok = candle_quality(
            last,
            "PUT"
        )

        ema_slope_ok = (
            ema9 < old_ema9
            and ema21 < old_ema21
        )

    # Six conditions only.
    score = sum([
        int(stoch_ok),
        int(rsi_ok),
        int(adx_ok),
        int(di_ok),
        int(candle_ok),
        int(ema_slope_ok)
    ])

    if score < MIN_SCORE:
        return None

    return {
        "direction": direction,
        "score": score,
        "rsi": rsi,
        "k": k,
        "d": d,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "ema9": ema9,
        "ema21": ema21,
        "ema50": ema50,
        "close": close,
        "candle_time": last["time"]
    }


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

        return bool(
            response.json().get("ok")
        )

    except Exception as exc:
        logging.error(
            "Telegram error: %s",
            exc
        )
        return False


# =====================================================
# STATE
# =====================================================

def load_state():
    default = {
        "pending": None,
        "last_signal_key": None,
        "last_result": None,
        "recent_symbols": []
    }

    if not os.path.exists(STATE_FILE):
        return default

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:
            state = json.load(file)

        for key, value in default.items():
            state.setdefault(key, value)

        if not isinstance(
            state["recent_symbols"], list
        ):
            state["recent_symbols"] = []

        return state

    except Exception as exc:
        logging.warning(
            "State file error: %s",
            exc
        )
        return default


def save_state(state):
    temporary = STATE_FILE + ".tmp"

    with open(
        temporary,
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            state,
            file,
            indent=4
        )

    os.replace(
        temporary,
        STATE_FILE
    )


# =====================================================
# VERIFY PREVIOUS RESULT
# =====================================================

def verify_pending_result(state):
    pending = state.get("pending")

    if not pending:
        return True

    entry_time = int(
        pending["entry_timestamp"]
    )

    expiry_time = (
        entry_time + EXPIRATION
    )

    if time.time() < expiry_time + 5:
        return False

    candles = get_candles(
        pending["symbol"]
    )

    if not candles:
        return False

    entry_candle = next(
        (
            candle for candle in candles
            if candle["time"] == entry_time
        ),
        None
    )

    if not entry_candle:
        logging.warning(
            "Entry candle not found. Result unverified."
        )
        return False

    entry_price = entry_candle["open"]
    exit_price = entry_candle["close"]

    if exit_price == entry_price:
        result = "TIE"

    elif pending["direction"] == "CALL":
        result = (
            "WIN"
            if exit_price > entry_price
            else "LOSS"
        )

    else:
        result = (
            "WIN"
            if exit_price < entry_price
            else "LOSS"
        )

    state["last_result"] = (
        f"{result} | {pending['symbol']} | "
        f"{pending['direction']}"
    )

    state["pending"] = None

    save_state(state)

    logging.info(
        "OTCharts candle-based result: %s",
        state["last_result"]
    )

    return True


# =====================================================
# MESSAGE FORMAT
# =====================================================

def build_message(
    symbol,
    payout,
    signal,
    entry_time
):
    direction = signal["direction"]

    emoji = (
        "🟢"
        if direction == "CALL"
        else "🔴"
    )

    return (
        f"🤖 {VERSION}\n\n"
        f"{emoji} {direction}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"💰 PAYOUT: {payout:.1f}%\n\n"
        f"📊 STOCH K: {signal['k']:.2f}\n"
        f"📊 STOCH D: {signal['d']:.2f}\n"
        f"📈 RSI 7: {signal['rsi']:.2f}\n"
        f"📈 ADX: {signal['adx']:.2f}\n"
        f"DI+: {signal['plus_di']:.2f}\n"
        f"DI-: {signal['minus_di']:.2f}\n\n"
        f"⭐ SIGNAL QUALITY: "
        f"{signal['score']}/6\n\n"
        f"🕒 SIGNAL TIME: {florida_time()}\n"
        f"🎯 ENTRY TIME: {florida_time(entry_time)}"
    )


# =====================================================
# SCAN ALL ELIGIBLE PAIRS
# =====================================================

def scan_market(symbols, recent_symbols):
    candidates = []
    checked = 0
    valid_setups = 0

    recent_set = {
        str(symbol).upper()
        for symbol in recent_symbols
    }

    for index, item in enumerate(
        symbols,
        start=1
    ):
        symbol = item["symbol"]

        logging.info(
            "Scanning %s (%s/%s)",
            symbol,
            index,
            len(symbols)
        )

        candles = get_candles(symbol)

        if len(candles) < MIN_CANDLES:
            logging.info(
                "%s skipped: only %s candles",
                symbol,
                len(candles)
            )
            continue

        checked += 1

        signal = analyze_candles(candles)

        if not signal:
            continue

        valid_setups += 1

        logging.info(
            "Setup: %s %s | Score %s/6 | Payout %.1f%%",
            symbol,
            signal["direction"],
            signal["score"],
            item["payout"]
        )

        candidates.append({
            "symbol": symbol,
            "payout": item["payout"],
            "signal": signal,
            "recent": (
                symbol.upper() in recent_set
            )
        })

    logging.info(
        "Scan completed | Checked: %s/%s | Valid setups: %s",
        checked,
        len(symbols),
        valid_setups
    )

    if not candidates:
        return None

    # Prefer pairs not used in the last two signals.
    fresh_candidates = [
        candidate
        for candidate in candidates
        if not candidate["recent"]
    ]

    if fresh_candidates:
        candidates = fresh_candidates

    candidates.sort(
        key=lambda candidate: (
            candidate["signal"]["score"],
            candidate["payout"]
        ),
        reverse=True
    )

    best = candidates[0]

    logging.info(
        "Selected candidate: %s | Score %s/6 | Payout %.1f%%",
        best["symbol"],
        best["signal"]["score"],
        best["payout"]
    )

    return best


# =====================================================
# MAIN
# =====================================================

def main():
    logging.info(
        "%s starting...",
        VERSION
    )

    if not API_KEY:
        logging.error(
            "OTCHARTS_API_KEY missing."
        )
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error(
            "Telegram credentials missing."
        )
        return

    state = load_state()

    # Verify any previous pending result.
    if state.get("pending"):
        start_wait = time.time()

        while state.get("pending"):
            if verify_pending_result(state):
                break

            if time.time() - start_wait > 180:
                logging.error(
                    "Previous result could not be verified. "
                    "No new signal will be sent."
                )
                return

            logging.info(
                "Waiting for previous result..."
            )

            time.sleep(10)

    symbols = get_symbols()

    if symbols is None:
        logging.error(
            "Symbols API failed. Scan stopped."
        )
        return

    if not symbols:
        logging.error(
            "No Forex OTC pairs found with payout >= %.1f%%.",
            MIN_PAYOUT
        )
        logging.error(
            "Check OTCharts response and symbol fields."
        )
        return

    logging.info(
        "Forex OTC pairs selected: %s",
        len(symbols)
    )

    logging.info(
        "ADX minimum: %s | DI gap: %s | Minimum score: %s/6",
        ADX_MIN,
        MIN_DI_GAP,
        MIN_SCORE
    )

    logging.info(
        "Scan interval: %s seconds",
        SCAN_INTERVAL
    )

    start = time.time()

    while (
        time.time() - start
        < MAX_SCAN_MINUTES * 60
    ):

        candidate = scan_market(
            symbols,
            state.get("recent_symbols", [])
        )

        if not candidate:
            logging.info(
                "No valid setup. Next scan in %s seconds.",
                SCAN_INTERVAL
            )

            time.sleep(SCAN_INTERVAL)
            continue

        entry_time = next_entry_time()
        signal_time = entry_time - ENTRY_LEAD

        if time.time() > signal_time + 2:
            logging.info(
                "Candidate arrived too late. Rescanning."
            )
            time.sleep(3)
            continue

        logging.info(
            "Selected: %s %s | Score %s/6 | Entry: %s",
            candidate["symbol"],
            candidate["signal"]["direction"],
            candidate["signal"]["score"],
            florida_time(entry_time)
        )

        # Wait for 10 seconds before entry.
        while time.time() < signal_time:
            remaining = signal_time - time.time()

            time.sleep(
                min(
                    max(remaining, 0.2),
                    2
                )
            )

        # Recheck the selected pair before sending.
        candles = get_candles(
            candidate["symbol"]
        )

        if len(candles) < MIN_CANDLES:
            logging.warning(
                "Not enough candles at confirmation."
            )
            continue

        fresh_signal = analyze_candles(candles)

        if not fresh_signal:
            logging.info(
                "Setup no longer valid. Signal cancelled."
            )
            continue

        if (
            fresh_signal["direction"]
            != candidate["signal"]["direction"]
        ):
            logging.info(
                "Direction changed. Signal cancelled."
            )
            continue

        if fresh_signal["score"] < MIN_SCORE:
            logging.info(
                "Score no longer qualifies."
            )
            continue

        if time.time() > signal_time + 2:
            logging.info(
                "Confirmation arrived too late."
            )
            continue

        signal_key = (
            f"{candidate['symbol']}|"
            f"{fresh_signal['direction']}|"
            f"{entry_time}"
        )

        if (
            state.get("last_signal_key")
            == signal_key
        ):
            logging.info(
                "Duplicate signal blocked."
            )
            continue

        message = build_message(
            candidate["symbol"],
            candidate["payout"],
            fresh_signal,
            entry_time
        )

        if not send_telegram(message):
            logging.error(
                "Telegram delivery failed."
            )
            return

        state["last_signal_key"] = signal_key

        state["pending"] = {
            "symbol": candidate["symbol"],
            "direction": fresh_signal["direction"],
            "entry_timestamp": int(entry_time),
            "payout": candidate["payout"]
        }

        recent = state.get(
            "recent_symbols",
            []
        )

        recent.insert(
            0,
            candidate["symbol"]
        )

        state["recent_symbols"] = recent[
            :RECENT_SYMBOL_LIMIT
        ]

        save_state(state)

        logging.info(
            "Signal sent: %s %s",
            candidate["symbol"],
            fresh_signal["direction"]
        )

        # Wait for the trade result.
        result_wait_start = time.time()

        while state.get("pending"):
            if verify_pending_result(state):
                break

            if (
                time.time() - result_wait_start
                > 180
            ):
                logging.error(
                    "Result unverified. Pending state saved."
                )
                return

            time.sleep(10)

        logging.info(
            "Result verification finished."
        )

        time.sleep(3)

    logging.warning(
        "Scan time limit reached."
    )


if __name__ == "__main__":
    main()
