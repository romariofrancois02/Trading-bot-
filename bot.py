
import os
import json
import time
import logging
import requests

from zoneinfo import ZoneInfo
from datetime import datetime

# =====================================================
# ROMARIO TRADER PRO 11.1
# OTC | M1 | EXPIRATION 60 SECONDS
# TELEGRAM ALERTS ONLY
# =====================================================

VERSION = "ROMARIO TRADER PRO 11.1"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 60
EXPIRATION = 60
CANDLE_LIMIT = 300
MIN_CANDLES = 80

MIN_PAYOUT = 92.0

ADX_PERIOD = 14
ADX_MIN = 16
MIN_DI_GAP = 3

RSI_PERIOD = 7
STOCH_PERIOD = 14

MIN_SCORE = 2

ENTRY_LEAD = 10
SCAN_INTERVAL = 3
MAX_SCAN_MINUTES = 300

API_ATTEMPTS = 4
STATE_FILE = "state.json"

OTC_CLOCK_OFFSET = int(
    os.getenv("OTC_CLOCK_OFFSET", "7200")
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

def otc_now():
    return time.time() + OTC_CLOCK_OFFSET


def florida_time(timestamp=None, otc_timestamp=False):
    if timestamp is None:
        timestamp = time.time()

    if otc_timestamp:
        timestamp -= OTC_CLOCK_OFFSET

    return datetime.fromtimestamp(
        timestamp, FLORIDA
    ).strftime("%I:%M:%S %p").lstrip("0")


def next_entry_time():
    now = otc_now()
    entry = (int(now // TIMEFRAME) + 1) * TIMEFRAME

    if entry - now < ENTRY_LEAD:
        entry += TIMEFRAME

    return entry


# =====================================================
# API
# =====================================================

def api_get(endpoint, params=None):
    for attempt in range(API_ATTEMPTS):
        try:
            response = session.get(
                BASE_URL + endpoint,
                params=params or {},
                timeout=20
            )

            if response.status_code in (429, 502, 503, 504):
                time.sleep(2 + attempt * 2)
                continue

            response.raise_for_status()
            return response.json()

        except Exception as exc:
            logging.warning(
                "API attempt %s: %s",
                attempt + 1,
                exc
            )
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


def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    if not data:
        return []

    rows = extract_list(
        data,
        ("symbols", "data", "instruments", "results")
    )

    symbols = []

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
        except (ValueError, TypeError):
            payout = 0

        if payout >= MIN_PAYOUT:
            symbols.append({
                "symbol": symbol,
                "payout": payout
            })

    return symbols


def parse_timestamp(value):
    try:
        value = float(value)

        if value > 100000000000:
            value /= 1000

        return int(value)

    except (ValueError, TypeError):
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

        except (ValueError, TypeError):
            continue

    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = sorted(
        unique.values(),
        key=lambda x: x["time"]
    )

    candles = [
        c for c in candles
        if c["time"] + TIMEFRAME <= otc_now()
    ]

    continuous = []

    for candle in candles:
        if not continuous:
            continuous.append(candle)
        elif candle["time"] - continuous[-1]["time"] == TIMEFRAME:
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
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# =====================================================
# STOCHASTIC
# =====================================================

def stochastic(candles, period=14):
    if len(candles) < period + 3:
        return None, None, None

    k_values = []

    for index in range(len(candles) - 3, len(candles)):
        section = candles[index - period + 1:index + 1]

        highest = max(c["high"] for c in section)
        lowest = min(c["low"] for c in section)

        if highest == lowest:
            k = 50.0
        else:
            k = (
                (candles[index]["close"] - lowest)
                / (highest - lowest)
            ) * 100

        k_values.append(k)

    k = k_values[-1]
    d = sum(k_values[-3:]) / 3

    return k, d, k_values[-2]


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

        up = current["high"] - previous["high"]
        down = previous["low"] - current["low"]

        plus_dm.append(
            up if up > down and up > 0 else 0
        )

        minus_dm.append(
            down if down > up and down > 0 else 0
        )

        tr_values.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))

    def smooth(values):
        result = [sum(values[:period])]

        for value in values[period:]:
            result.append(
                result[-1] - result[-1] / period + value
            )

        return result

    tr = smooth(tr_values)
    plus = smooth(plus_dm)
    minus = smooth(minus_dm)

    dx_values = []
    last_plus = 0
    last_minus = 0

    for i in range(min(len(tr), len(plus), len(minus))):
        if tr[i] <= 0:
            continue

        last_plus = 100 * plus[i] / tr[i]
        last_minus = 100 * minus[i] / tr[i]

        total = last_plus + last_minus

        dx = (
            100 * abs(last_plus - last_minus) / total
            if total > 0 else 0
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None, None, None

    adx = sum(dx_values[-period:]) / period

    return adx, last_plus, last_minus


# =====================================================
# CANDLE QUALITY
# =====================================================

def candle_quality(candle, direction):
    candle_range = candle["high"] - candle["low"]

    if candle_range <= 0:
        return False

    body = abs(candle["close"] - candle["open"])
    body_ratio = body / candle_range

    close_position = (
        candle["close"] - candle["low"]
    ) / candle_range

    if direction == "CALL":
        return (
            candle["close"] > candle["open"]
            and body_ratio >= 0.30
            and close_position >= 0.55
        )

    return (
        candle["close"] < candle["open"]
        and body_ratio >= 0.30
        and close_position <= 0.45
    )


# =====================================================
# STRATEGY
# =====================================================

def signal_at(candles):
    if len(candles) < MIN_CANDLES:
        return None

    closes = [c["close"] for c in candles]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)

    old_ema9 = ema_previous(closes, 9)
    old_ema21 = ema_previous(closes, 21)

    rsi = calculate_rsi(closes, RSI_PERIOD)

    k, d, old_k = stochastic(
        candles,
        STOCH_PERIOD
    )

    adx, plus_di, minus_di = adx_values(
        candles,
        ADX_PERIOD
    )

    if None in (
        ema9, ema21, ema50,
        old_ema9, old_ema21,
        rsi, k, d, old_k, adx
    ):
        return None

    last = candles[-1]
    close = last["close"]

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

    if call_trend:
        score = 0

        if k > d and k > old_k:
            score += 1

        if 50 <= rsi <= 72:
            score += 1

        if adx >= ADX_MIN and plus_di > minus_di:
            score += 1

        if (
            plus_di - minus_di >= MIN_DI_GAP
            and adx >= ADX_MIN
        ):
            score += 1

        if candle_quality(last, "CALL"):
            score += 1

        if ema9 > old_ema9 and ema21 > old_ema21:
            score += 1

        if score < MIN_SCORE:
            return None

        direction = "CALL"

    else:
        score = 0

        if k < d and k < old_k:
            score += 1

        if 28 <= rsi <= 50:
            score += 1

        if adx >= ADX_MIN and minus_di > plus_di:
            score += 1

        if (
            minus_di - plus_di >= MIN_DI_GAP
            and adx >= ADX_MIN
        ):
            score += 1

        if candle_quality(last, "PUT"):
            score += 1

        if ema9 < old_ema9 and ema21 < old_ema21:
            score += 1

        if score < MIN_SCORE:
            return None

        direction = "PUT"

    return {
        "direction": direction,
        "score": score,
        "rsi": rsi,
        "k": k,
        "d": d,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "candle_time": last["time"]
    }


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

        return bool(response.json().get("ok"))

    except Exception as exc:
        logging.error("Telegram error: %s", exc)
        return False


# =====================================================
# STATE
# =====================================================

def load_state():
    default = {
        "pending": None,
        "last_result": "NO PREVIOUS RESULT"
    }

    if not os.path.exists(STATE_FILE):
        return default

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        state.setdefault("pending", None)
        state.setdefault("last_result", "NO PREVIOUS RESULT")

        return state

    except Exception:
        return default


def save_state(state):
    temporary = STATE_FILE + ".tmp"

    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)

    os.replace(temporary, STATE_FILE)


# =====================================================
# PREVIOUS RESULT
# =====================================================

def verify_pending_result(state):
    pending = state.get("pending")

    if not pending:
        return True

    entry_time = int(pending["entry_timestamp"])
    expiry_time = entry_time + EXPIRATION

    if otc_now() < expiry_time + 5:
        return False

    candles = get_candles(pending["symbol"])

    if not candles:
        return False

    entry_candle = next(
        (c for c in candles if c["time"] == entry_time),
        None
    )

    if not entry_candle:
        return False

    entry_price = entry_candle["open"]
    exit_price = entry_candle["close"]

    if exit_price == entry_price:
        result = "TIE"

    elif pending["direction"] == "CALL":
        result = (
            "WIN" if exit_price > entry_price else "LOSS"
        )

    else:
        result = (
            "WIN" if exit_price < entry_price else "LOSS"
        )

    state["last_result"] = (
        f"{result} | {pending['symbol']} | "
        f"{pending['direction']}"
    )

    state["pending"] = None

    save_state(state)

    logging.info("Previous result: %s", state["last_result"])

    return True


# =====================================================
# TELEGRAM MESSAGE
# =====================================================

def build_message(symbol, payout, signal, entry_time, state):
    direction = signal["direction"]
    emoji = "🟢" if direction == "CALL" else "🔴"

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
        f"⭐ SIGNAL QUALITY: {signal['score']}/6\n\n"
        f"🕒 SIGNAL TIME: {florida_time()}\n"
        f"🎯 ENTRY TIME: {florida_time(entry_time, True)}"
    )


# =====================================================
# SCANNER
# =====================================================

def scan_market(symbols):
    best = None

    for index, item in enumerate(symbols, start=1):
        symbol = item["symbol"]

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
            "payout": item["payout"],
            "signal": signal
        }

        if (
            best is None
            or signal["score"] > best["signal"]["score"]
        ):
            best = candidate

    return best


# =====================================================
# MAIN
# =====================================================

def main():
    logging.info("%s starting...", VERSION)

    if not API_KEY:
        logging.error("OTCHARTS_API_KEY missing.")
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram credentials missing.")
        return

    state = load_state()

    while state.get("pending"):
        if verify_pending_result(state):
            break

        logging.info("Checking previous signal result...")
        time.sleep(10)

    symbols = get_symbols()

    if not symbols:
        logging.error("Could not retrieve eligible OTC pairs.")
        return

    logging.info("Eligible OTC pairs: %s", len(symbols))

    start = time.time()

    while time.time() - start < MAX_SCAN_MINUTES * 60:
        candidate = scan_market(symbols)

        if not candidate:
            time.sleep(SCAN_INTERVAL)
            continue

        entry_time = next_entry_time()
        signal_time = entry_time - ENTRY_LEAD

        logging.info(
            "Candidate %s %s | Entry %s",
            candidate["symbol"],
            candidate["signal"]["direction"],
            florida_time(entry_time, True)
        )

        while otc_now() < signal_time:
            remaining = signal_time - otc_now()
            time.sleep(min(max(remaining, 0.2), 2))

        candles = get_candles(candidate["symbol"])

        if len(candles) < MIN_CANDLES:
            continue

        fresh_signal = signal_at(candles)

        if not fresh_signal:
            continue

        if (
            fresh_signal["direction"]
            != candidate["signal"]["direction"]
        ):
            continue

        if otc_now() > signal_time + 2:
            continue

        message = build_message(
            candidate["symbol"],
            candidate["payout"],
            fresh_signal,
            entry_time,
            state
        )

        if not send_telegram(message):
            logging.error("Telegram delivery failed.")
            return

        state["pending"] = {
            "symbol": candidate["symbol"],
            "direction": fresh_signal["direction"],
            "entry_timestamp": int(entry_time),
            "payout": candidate["payout"]
        }

        save_state(state)

        logging.info(
            "Signal sent: %s %s",
            candidate["symbol"],
            fresh_signal["direction"]
        )

        return

    logging.warning("Scan time limit reached without a valid setup.")


if __name__ == "__main__":
    main()
