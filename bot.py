
import os
import json
import time
import math
import logging
import threading
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo


# =========================================================
# ROMARIO TRADER PRO 15.0
# OTCharts OTC | S30 | Expiration 60s
# 120-second scan | One signal per run
# =========================================================

VERSION = "ROMARIO TRADER PRO 15.0"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 30

CANDLE_LIMIT = 300
MIN_CANDLES = 100

MIN_PAYOUT = 92.0
MAX_LIVE_SYMBOLS = 5
MAX_HISTORY_WORKERS = 5
MAX_SCAN_SECONDS = 120
MAX_RECONNECTS = 2

BB_PERIOD = 20
BB_STD = 2.2

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 7
ATR_PERIOD = 14
ADX_PERIOD = 14

ADX_MIN = 12.0
MIN_DI_GAP = 2.0
MIN_STRATEGY_AGREEMENT = 2
MIN_CONFIRMATIONS = 2
MIN_SIGNAL_SCORE = 6

MIN_CANDLE_ATR_RATIO = 0.20
MAX_CANDLE_ATR_RATIO = 2.20

ENTRY_GRACE_SECONDS = 3
EXPIRATION_SECONDS = 60

API_CONNECT_TIMEOUT = 5
API_READ_TIMEOUT = 12

TELEGRAM_ATTEMPTS = 4
TELEGRAM_TIMEOUT = (2, 3)

TIMEZONE = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

_thread_local = threading.local()


# =========================================================
# CUSTOM ERRORS
# =========================================================

class StreamRateLimitError(Exception):

    def __init__(self, message, retry_after=65):
        super().__init__(message)
        self.retry_after = retry_after


# =========================================================
# SESSION
# =========================================================

def log(message):
    logging.info(message)


def get_session():

    if not hasattr(_thread_local, "session"):

        _thread_local.session = requests.Session()

        _thread_local.session.headers.update({
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "RomarioTraderPro/15.0",
            "Accept": "application/json"
        })

    return _thread_local.session


def make_entry_time(signal_time):
    return (math.floor(signal_time / 10) + 1) * 10


def format_price(value):
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


# =========================================================
# OTCHARTS API
# =========================================================

def api_get(path, params=None, stream=False):

    url = BASE_URL + path
    attempts = 1 if stream else 2

    for attempt in range(1, attempts + 1):

        response = None

        try:

            response = get_session().get(
                url,
                params=params,
                timeout=(
                    API_CONNECT_TIMEOUT,
                    None if stream else API_READ_TIMEOUT
                ),
                stream=stream
            )

            if response.status_code == 429:

                if stream:

                    retry_after = response.headers.get(
                        "Retry-After",
                        "65"
                    )

                    try:
                        retry_after = max(
                            5,
                            int(float(retry_after))
                        )
                    except (TypeError, ValueError):
                        retry_after = 65

                    details = response.text[:300]
                    response.close()

                    raise StreamRateLimitError(
                        f"OTCharts stream rate limit: {details}",
                        retry_after
                    )

                details = response.text[:300]
                response.close()

                raise RuntimeError(
                    f"OTCharts API rate limit: {details}"
                )

            if response.status_code in (401, 402, 403):

                details = response.text[:300]
                response.close()

                raise RuntimeError(
                    f"OTCharts access error "
                    f"{response.status_code}: {details}"
                )

            if response.status_code in (500, 502, 503, 504):

                if attempt < attempts:

                    log(
                        f"OTCharts server error "
                        f"{response.status_code}. Retrying."
                    )

                    response.close()
                    time.sleep(0.5)
                    continue

            response.raise_for_status()

            if stream:
                return response

            return response.json()

        except StreamRateLimitError:
            raise

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError
        ) as exc:

            if response is not None:
                response.close()

            if attempt >= attempts:
                raise

            log(f"API retry {attempt}: {exc}")
            time.sleep(0.5)

    raise RuntimeError("OTCharts request failed.")


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials missing.")
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    for attempt in range(1, TELEGRAM_ATTEMPTS + 1):

        response = None

        try:

            response = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message
                },
                timeout=TELEGRAM_TIMEOUT
            )

            try:
                result = response.json()
            except ValueError:
                result = {}

            if response.ok and result.get("ok") is True:

                log(f"Telegram delivered on attempt {attempt}.")
                return True

            error_code = result.get("error_code")
            description = result.get(
                "description",
                response.text[:200]
            )

            if error_code in (400, 401, 403, 404):

                log(
                    f"Telegram permanent error: "
                    f"{error_code}: {description}"
                )

                return False

            log(f"Telegram temporary error: {description}")

        except requests.exceptions.RequestException as exc:
            log(f"Telegram error: {exc}")

        finally:

            if response is not None:
                response.close()

        if attempt < TELEGRAM_ATTEMPTS:
            time.sleep(0.5)

    return False


# =========================================================
# ACCOUNT AND S30 CHECK
# =========================================================

def check_account():

    usage = api_get("/v1/usage")

    plan = str(
        usage.get(
            "planName",
            usage.get("plan", "Unknown")
        )
    )

    requests_data = usage.get("requests", {})
    streams_data = usage.get("streams", {})

    remaining = int(requests_data.get("remaining", 0))
    quota = int(requests_data.get("quota", 0))

    stream_limit = int(streams_data.get("limit", 0))

    instruments_limit = int(
        streams_data.get("instrumentsPerStream") or 0
    )

    log(
        f"OTCharts plan={plan} | "
        f"remaining={remaining}/{quota} | "
        f"streams={stream_limit} | "
        f"instruments={instruments_limit}"
    )

    if stream_limit < 1:
        raise RuntimeError("No live stream available.")

    if instruments_limit < 1:
        raise RuntimeError("No live instruments available.")

    venues_data = api_get("/v1/venues")
    venues = venues_data.get("venues", [])

    otc = next(
        (
            item for item in venues
            if item.get("id") == VENUE
        ),
        None
    )

    if not otc or otc.get("open") is not True:
        raise RuntimeError("OTC access not confirmed.")

    available = otc.get("timeframes", [])

    timeframes = set()

    for value in available:

        try:
            timeframes.add(int(value))
        except (TypeError, ValueError):
            continue

    log(f"OTC timeframes available: {sorted(timeframes)}")

    if TIMEFRAME not in timeframes:
        raise RuntimeError(
            f"S30 not confirmed. Available: {available}"
        )

    if remaining < 20:
        raise RuntimeError("Insufficient API requests.")

    log("S30 confirmed by OTCharts.")

    return {
        "remaining": remaining,
        "stream_limit": stream_limit,
        "instruments_limit": instruments_limit
    }


# =========================================================
# FOREX OTC SYMBOLS
# =========================================================

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD",
    "AUD", "NZD", "NOK", "SEK", "DKK", "PLN",
    "CZK", "HUF", "TRY", "ZAR", "MXN", "BRL",
    "INR", "SGD", "HKD", "THB", "MYR", "IDR",
    "PHP", "KRW", "TWD", "SAR", "AED", "QAR",
    "KWD", "EGP", "NGN", "ILS", "CLP", "COP",
    "PEN", "ARS", "RUB", "UAH"
}


def is_forex_pair(symbol):

    if not isinstance(symbol, str):
        return False

    if not symbol.endswith("_otc"):
        return False

    pair = symbol[:-4]

    if len(pair) != 6 or not pair.isalpha():
        return False

    base = pair[:3].upper()
    quote = pair[3:].upper()

    return (
        base in CURRENCIES
        and quote in CURRENCIES
        and base != quote
    )


def get_eligible_symbols():

    data = api_get(
        "/v1/symbols",
        params={"venue": VENUE}
    )

    eligible = []

    for item in data.get("symbols", []):

        symbol = item.get("symbol")

        if not is_forex_pair(symbol):
            continue

        if item.get("isOtc") is not True:
            continue

        if item.get("open") is not True:
            continue

        try:
            payout = float(item.get("payout"))
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    eligible.sort(
        key=lambda x: (-x["payout"], x["symbol"])
    )

    log(f"Eligible Forex OTC pairs: {len(eligible)}")

    return eligible


# =========================================================
# HISTORICAL CANDLES
# =========================================================

def get_candles(symbol):

    data = api_get(
        "/v1/candles",
        params={
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    candles = data.get("candles", [])
    gaps = data.get("gaps", {})

    if gaps.get("missingBars", 0) > 0:
        log(f"{symbol}: historical missing bars.")
        return []

    parsed = []

    for candle in candles:

        try:

            parsed.append({
                "time": int(candle["time"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"])
            })

        except (KeyError, TypeError, ValueError):
            continue

    parsed.sort(key=lambda x: x["time"])

    unique = {}

    for candle in parsed:
        unique[candle["time"]] = candle

    parsed = [
        unique[key]
        for key in sorted(unique)
    ]

    if len(parsed) < MIN_CANDLES:
        return []

    for i in range(1, len(parsed)):

        if (
            parsed[i]["time"]
            - parsed[i - 1]["time"]
        ) != TIMEFRAME:

            log(f"{symbol}: historical candle gap.")
            return []

    return parsed[-CANDLE_LIMIT:]


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


def bollinger_bands(closes, period=20, std_dev=2.2):

    if len(closes) < period:
        return None, None, None

    window = closes[-period:]
    middle = sum(window) / period

    variance = sum(
        (x - middle) ** 2
        for x in window
    ) / period

    sd = math.sqrt(variance)

    return (
        middle + std_dev * sd,
        middle,
        middle - std_dev * sd
    )


def calculate_rsi(closes, period=7):

    if len(closes) <= period:
        return None

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    ranges = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        ranges.append(tr)

    return sum(ranges[-period:]) / period


def calculate_adx(candles, period=14):

    if len(candles) < (period * 2) + 2:
        return None, None, None

    true_ranges = []
    plus_moves = []
    minus_moves = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        plus_dm = (
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus_dm = (
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        true_ranges.append(tr)
        plus_moves.append(plus_dm)
        minus_moves.append(minus_dm)

    dx_values = []

    start = max(period, len(true_ranges) - period)

    for end in range(start, len(true_ranges) + 1):

        tr_sum = sum(true_ranges[end - period:end])
        plus_sum = sum(plus_moves[end - period:end])
        minus_sum = sum(minus_moves[end - period:end])

        if tr_sum <= 0:
            continue

        plus_di = 100 * plus_sum / tr_sum
        minus_di = 100 * minus_sum / tr_sum

        denominator = plus_di + minus_di

        if denominator <= 0:
            continue

        dx_values.append(
            100 * abs(plus_di - minus_di) / denominator
        )

    if not dx_values:
        return None, None, None

    adx = sum(dx_values) / len(dx_values)

    tr_sum = sum(true_ranges[-period:])
    plus_sum = sum(plus_moves[-period:])
    minus_sum = sum(minus_moves[-period:])

    if tr_sum <= 0:
        return None, None, None

    plus_di = 100 * plus_sum / tr_sum
    minus_di = 100 * minus_sum / tr_sum

    return adx, plus_di, minus_di


# =========================================================
# CANDLE REJECTION
# =========================================================

def wick_rejection(candle, direction):

    o = candle["open"]
    c = candle["close"]
    h = candle["high"]
    l = candle["low"]

    body = abs(c - o)
    candle_range = h - l

    if candle_range <= 0:
        return False

    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    minimum = max(
        body * 1.2,
        candle_range * 0.30
    )

    if direction == "CALL":
        return lower_wick >= minimum

    if direction == "PUT":
        return upper_wick >= minimum

    return False


# =========================================================
# STRATEGY 1: BOLLINGER REVERSAL
# =========================================================

def strategy_bollinger(
    candles,
    upper,
    middle,
    lower,
    prev_upper,
    prev_lower,
    rsi
):

    prev = candles[-2]
    last = candles[-1]

    call = (
        (
            prev["close"] <= prev_lower
            or last["low"] <= lower
        )
        and last["close"] > last["open"]
        and 25 <= rsi <= 40
        and last["close"] < middle
        and wick_rejection(last, "CALL")
    )

    if call:
        return {
            "direction": "CALL",
            "strategy": "BOLLINGER REVERSAL"
        }

    put = (
        (
            prev["close"] >= prev_upper
            or last["high"] >= upper
        )
        and last["close"] < last["open"]
        and 60 <= rsi <= 75
        and last["close"] > middle
        and wick_rejection(last, "PUT")
    )

    if put:
        return {
            "direction": "PUT",
            "strategy": "BOLLINGER REVERSAL"
        }

    return None


# =========================================================
# STRATEGY 2: EMA PULLBACK
# =========================================================

def strategy_ema(
    prev,
    last,
    ema9,
    ema21,
    ema50,
    ema9_prev,
    rsi
):

    if (
        ema9 > ema21 > ema50
        and prev["close"] < prev["open"]
        and prev["low"] <= ema9_prev
        and last["close"] > last["open"]
        and last["close"] > ema9
        and 43 <= rsi <= 67
    ):

        return {
            "direction": "CALL",
            "strategy": "EMA 9/21/50 PULLBACK"
        }

    if (
        ema9 < ema21 < ema50
        and prev["close"] > prev["open"]
        and prev["high"] >= ema9_prev
        and last["close"] < last["open"]
        and last["close"] < ema9
        and 33 <= rsi <= 57
    ):

        return {
            "direction": "PUT",
            "strategy": "EMA 9/21/50 PULLBACK"
        }

    return None


# =========================================================
# STRATEGY 3: ENGULFING
# =========================================================

def strategy_engulfing(prev, last, rsi):

    bullish = (
        prev["close"] < prev["open"]
        and last["close"] > last["open"]
        and last["close"] > prev["open"]
        and last["open"] <= prev["close"]
        and 40 <= rsi <= 65
    )

    if bullish:
        return {
            "direction": "CALL",
            "strategy": "BULLISH ENGULFING"
        }

    bearish = (
        prev["close"] > prev["open"]
        and last["close"] < last["open"]
        and last["close"] < prev["open"]
        and last["open"] >= prev["close"]
        and 35 <= rsi <= 60
    )

    if bearish:
        return {
            "direction": "PUT",
            "strategy": "BEARISH ENGULFING"
        }

    return None


# =========================================================
# SIGNAL ANALYZER
# =========================================================

def analyze_candles(candles):

    if len(candles) < MIN_CANDLES:
        return None

    closes = [float(c["close"]) for c in candles]

    upper, middle, lower = bollinger_bands(
        closes,
        BB_PERIOD,
        BB_STD
    )

    prev_upper, _, prev_lower = bollinger_bands(
        closes[:-1],
        BB_PERIOD,
        BB_STD
    )

    ema9 = ema(closes, EMA_FAST)
    ema21 = ema(closes, EMA_MID)
    ema50 = ema(closes, EMA_SLOW)
    ema9_prev = ema(closes[:-1], EMA_FAST)

    rsi = calculate_rsi(closes, RSI_PERIOD)
    atr = calculate_atr(candles, ATR_PERIOD)

    adx, plus_di, minus_di = calculate_adx(
        candles,
        ADX_PERIOD
    )

    values = (
        upper, middle, lower,
        prev_upper, prev_lower,
        ema9, ema21, ema50, ema9_prev,
        rsi, atr, adx, plus_di, minus_di
    )

    if any(value is None for value in values):
        return None

    if atr <= 0 or adx < ADX_MIN:
        return None

    prev = candles[-2]
    last = candles[-1]

    candle_range = last["high"] - last["low"]

    if candle_range <= 0:
        return None

    ratio = candle_range / atr

    if not MIN_CANDLE_ATR_RATIO <= ratio <= MAX_CANDLE_ATR_RATIO:
        return None

    results = [
        strategy_bollinger(
            candles,
            upper,
            middle,
            lower,
            prev_upper,
            prev_lower,
            rsi
        ),
        strategy_ema(
            prev,
            last,
            ema9,
            ema21,
            ema50,
            ema9_prev,
            rsi
        ),
        strategy_engulfing(prev, last, rsi)
    ]

    candidates = [item for item in results if item is not None]

    if len(candidates) < MIN_STRATEGY_AGREEMENT:
        return None

    directions = {item["direction"] for item in candidates}

    if len(directions) != 1:
        return None

    direction = candidates[0]["direction"]
    confirmations = []

    if direction == "CALL":

        if ema9 > ema21:
            confirmations.append("EMA BULLISH")

        if ema21 > ema50:
            confirmations.append("EMA50 TREND")

        if last["close"] > ema9:
            confirmations.append("PRICE ABOVE EMA9")

        if last["close"] > last["open"]:
            confirmations.append("BULLISH CANDLE")

        if plus_di > minus_di and plus_di - minus_di >= MIN_DI_GAP:
            confirmations.append("BUYERS STRONGER")

        if plus_di <= minus_di:
            return None

    else:

        if ema9 < ema21:
            confirmations.append("EMA BEARISH")

        if ema21 < ema50:
            confirmations.append("EMA50 TREND")

        if last["close"] < ema9:
            confirmations.append("PRICE BELOW EMA9")

        if last["close"] < last["open"]:
            confirmations.append("BEARISH CANDLE")

        if minus_di > plus_di and minus_di - plus_di >= MIN_DI_GAP:
            confirmations.append("SELLERS STRONGER")

        if minus_di <= plus_di:
            return None

    if len(confirmations) < MIN_CONFIRMATIONS:
        return None

    score = min(
        8,
        4 + (len(candidates) - 2) + min(len(confirmations), 4)
    )

    if score < MIN_SIGNAL_SCORE:
        return None

    return {
        "direction": direction,
        "strategies": [x["strategy"] for x in candidates],
        "strategies_agree": len(candidates),
        "score": score,
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "plus_di": round(plus_di, 2),
        "minus_di": round(minus_di, 2),
        "close": last["close"],
        "confirmations": confirmations
    }


# =========================================================
# SIGNAL MESSAGE
# =========================================================

def send_signal(
    symbol,
    payout,
    signal,
    detection_time,
    entry_time
):

    signal_dt = datetime.fromtimestamp(
        detection_time,
        TIMEZONE
    )

    entry_dt = datetime.fromtimestamp(
        entry_time,
        TIMEZONE
    )

    direction = signal["direction"]
    arrow = "🟢" if direction == "CALL" else "🔴"

    strategies = "\n".join(
        f"• {name}" for name in signal["strategies"]
    )

    confirmations = "\n".join(
        f"✓ {name}" for name in signal["confirmations"]
    )

    message = (
        f"🤖 {VERSION}\n\n"
        f"{arrow} {direction}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"💰 PAYOUT: {payout:.1f}%\n"
        f"⏱ TIMEFRAME: S30\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🧠 STRATEGIES:\n{strategies}\n\n"
        f"🤝 AGREEMENT: {signal['strategies_agree']}/3\n"
        f"⭐ QUALITY SCORE: {signal['score']}/8\n"
        f"📊 RSI 7: {signal['rsi']}\n"
        f"📊 ADX 14: {signal['adx']}\n"
        f"📈 +DI: {signal['plus_di']}\n"
        f"📉 -DI: {signal['minus_di']}\n\n"
        f"🔎 CONFIRMATIONS:\n{confirmations}\n\n"
        f"⏰ SIGNAL: {signal_dt.strftime('%I:%M:%S %p')}\n"
        f"🎯 MANUAL ENTRY: {entry_dt.strftime('%I:%M:%S %p')}\n\n"
        f"⚠️ If entry is missed by more than "
        f"{ENTRY_GRACE_SECONDS} seconds, skip it."
    )

    return send_telegram(message)


# =========================================================
# RESULT ESTIMATE
# =========================================================

def send_result(symbol, pending, exit_price):

    entry_price = pending["entry_price"]
    direction = pending["direction"]

    if direction == "CALL":

        if exit_price > entry_price:
            result = "WIN (OTCHARTS ESTIMATE)"
            emoji = "🟢"
        elif exit_price < entry_price:
            result = "LOSS (OTCHARTS ESTIMATE)"
            emoji = "🔴"
        else:
            result = "DRAW (OTCHARTS ESTIMATE)"
            emoji = "🟡"

    else:

        if exit_price < entry_price:
            result = "WIN (OTCHARTS ESTIMATE)"
            emoji = "🟢"
        elif exit_price > entry_price:
            result = "LOSS (OTCHARTS ESTIMATE)"
            emoji = "🔴"
        else:
            result = "DRAW (OTCHARTS ESTIMATE)"
            emoji = "🟡"

    difference = exit_price - entry_price

    message = (
        f"📊 {VERSION} - RESULT\n\n"
        f"{emoji} {result}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"📍 DIRECTION: {direction}\n"
        f"🎯 ENTRY PRICE: {format_price(entry_price)}\n"
        f"🏁 EXIT PRICE: {format_price(exit_price)}\n"
        f"📐 PRICE CHANGE: {difference:+.8f}\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"ℹ️ OTCharts estimate only. "
        f"Not a verified Pocket Option result."
    )

    send_telegram(message)


# =========================================================
# LIVE STREAM
# =========================================================

def run_live_stream(monitored, histories, deadline):

    symbols = [item["symbol"] for item in monitored]

    params = {
        "venue": VENUE,
        "symbol": ",".join(symbols)
    }

    log(f"Opening S30 stream for {len(symbols)} pairs.")

    response = api_get(
        "/v1/stream",
        params=params,
        stream=True
    )

    current_bars = {}
    bar_complete = {}
    connected_symbols = set()

    pending_signal = None
    event_name = ""

    try:

        for raw_line in response.iter_lines(
            decode_unicode=True,
            chunk_size=1
        ):

            now = time.time()

            if now >= deadline:
                log("120-second scan window completed.")
                return

            if not raw_line:
                event_name = ""
                continue

            if isinstance(raw_line, bytes):
                line = raw_line.decode(
                    "utf-8",
                    errors="replace"
                ).strip()
            else:
                line = raw_line.strip()

            if not line or line.startswith(":"):
                continue

            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue

            if not line.startswith("data:"):
                continue

            try:
                data = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue

            if event_name == "connected":

                connected_symbols = set(
                    data.get("symbols", [])
                )

                log(
                    "Connected symbols: "
                    + ", ".join(sorted(connected_symbols))
                )

                if not connected_symbols:
                    log("No symbols confirmed by stream.")
                    return

                continue

            if event_name in ("replaced", "dropped", "closed"):

                log(f"Stream event {event_name}: {data}")
                return

            if event_name != "tick":
                continue

            symbol = data.get("symbol")

            if symbol not in connected_symbols:
                continue

            try:
                price = float(data["price"])
                feed_time = int(data["time"])
            except (KeyError, TypeError, ValueError):
                continue

            now = time.time()

            # ---------------------------------------------
            # HANDLE ACTIVE SIGNAL
            # ---------------------------------------------

            if pending_signal is not None:

                if symbol == pending_signal["symbol"]:

                    pending = pending_signal

                    if pending["entry_price"] is None:

                        if now > pending["entry_time"] + ENTRY_GRACE_SECONDS:

                            log(f"{symbol}: entry missed.")

                            send_telegram(
                                f"⚠️ {VERSION}\n\n"
                                f"{symbol}: ENTRY MISSED. "
                                f"Skip this signal."
                            )

                            return

                        if now >= pending["entry_time"]:

                            pending["entry_price"] = price

                            log(
                                f"{symbol}: reference entry price "
                                f"{format_price(price)}"
                            )

                    if (
                        pending["entry_price"] is not None
                        and now >= pending["expiry_time"]
                    ):

                        send_result(
                            symbol,
                            pending,
                            price
                        )

                        log("Expiration completed. Run finished.")
                        return

                continue

            # ---------------------------------------------
            # BUILD S30 CANDLES
            # ---------------------------------------------

            bucket = (feed_time // TIMEFRAME) * TIMEFRAME

            if symbol not in current_bars:

                history = [
                    candle
                    for candle in histories.get(symbol, [])
                    if candle["time"] < bucket
                ]

                if history and bucket - history[-1]["time"] != TIMEFRAME:

                    log(f"{symbol}: history/live gap. Skipping pair.")
                    connected_symbols.discard(symbol)
                    continue

                histories[symbol] = history

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

                # First observed bar may be incomplete.
                bar_complete[symbol] = False
                continue

            current = current_bars[symbol]
            new_closed_candle = False

            if bucket > current["time"]:

                if bucket - current["time"] != TIMEFRAME:

                    log(f"{symbol}: live candle gap. Skipping pair.")
                    connected_symbols.discard(symbol)
                    continue

                if bar_complete.get(symbol, False):

                    histories[symbol].append(current)
                    histories[symbol] = histories[symbol][-CANDLE_LIMIT:]
                    new_closed_candle = True

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

                bar_complete[symbol] = (
                    feed_time - bucket <= 5
                )

            elif bucket == current["time"]:

                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price

            else:
                continue

            # ---------------------------------------------
            # ANALYZE NEW CLOSED CANDLE
            # ---------------------------------------------

            if not new_closed_candle:
                continue

            closed_candles = histories.get(symbol, [])

            if len(closed_candles) < MIN_CANDLES:
                continue

            signal = analyze_candles(closed_candles)

            if signal is None:
                continue

            pair_info = next(
                (
                    item for item in monitored
                    if item["symbol"] == symbol
                ),
                None
            )

            if pair_info is None:
                continue

            detection_time = time.time()
            entry_time = make_entry_time(detection_time)

            if entry_time + ENTRY_GRACE_SECONDS >= deadline:
                log(f"{symbol}: not enough scan time for safe entry.")
                return

            log(
                f"SIGNAL {signal['direction']} {symbol} | "
                f"agreement={signal['strategies_agree']}/3 | "
                f"score={signal['score']}/8 | "
                f"ADX={signal['adx']}"
            )

            delivered = send_signal(
                symbol,
                pair_info["payout"],
                signal,
                detection_time,
                entry_time
            )

            if not delivered:
                log("Telegram delivery failed. Signal not activated.")
                return

            if time.time() > entry_time + ENTRY_GRACE_SECONDS:

                log(f"{symbol}: signal delivery was too late.")

                send_telegram(
                    f"⚠️ {VERSION}\n\n"
                    f"{symbol}: signal cancelled. "
                    f"Entry window already passed."
                )

                return

            pending_signal = {
                "symbol": symbol,
                "direction": signal["direction"],
                "entry_time": entry_time,
                "expiry_time": entry_time + EXPIRATION_SECONDS,
                "entry_price": None
            }

            log(
                f"{symbol}: signal activated. "
                f"Waiting for entry and 60-second result."
            )

    finally:
        response.close()


# =========================================================
# PARALLEL HISTORY
# =========================================================

def download_all_histories(eligible):

    histories = {}
    ranked = []

    total = len(eligible)

    log(
        f"FAST HISTORY DOWNLOAD: {total} pairs | "
        f"{MAX_HISTORY_WORKERS} workers."
    )

    def fetch_history(item):
        return item, get_candles(item["symbol"])

    workers = min(MAX_HISTORY_WORKERS, total)

    with ThreadPoolExecutor(
        max_workers=max(1, workers)
    ) as executor:

        futures = {
            executor.submit(fetch_history, item): item
            for item in eligible
        }

        completed = 0

        for future in as_completed(futures):

            completed += 1
            item = futures[future]
            symbol = item["symbol"]

            try:

                returned_item, candles = future.result()

                if not candles:
                    log(f"[{completed}/{total}] {symbol}: no usable history.")
                    continue

                histories[symbol] = candles

                ranked.append({
                    **returned_item,
                    "history_count": len(candles)
                })

                log(
                    f"[{completed}/{total}] History ready: "
                    f"{symbol} | payout={item['payout']}% | "
                    f"candles={len(candles)}"
                )

            except Exception as exc:

                log(
                    f"[{completed}/{total}] "
                    f"{symbol}: history error: {exc}"
                )

    ranked.sort(
        key=lambda x: (-x["payout"], x["symbol"])
    )

    log(
        f"History complete: {len(ranked)}/{total} pairs ready."
    )

    return histories, ranked


# =========================================================
# MAIN
# =========================================================

def main():

    log(f"{VERSION} starting...")

    log(
        "SETTINGS: S30 | EXPIRATION 60s | "
        "120-SECOND SCAN | PAYOUT 92%+ | "
        "5 LIVE PAIRS | ONE SIGNAL PER RUN"
    )

    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing.")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram credentials missing.")

    account = check_account()

    eligible = get_eligible_symbols()

    if not eligible:
        log("No eligible Forex OTC pairs.")
        return

    estimated_requests = len(eligible) + 4

    if account["remaining"] < estimated_requests:
        raise RuntimeError("Not enough API requests.")

    histories, ranked = download_all_histories(eligible)

    if not ranked:
        log("No pairs have sufficient S30 history.")
        return

    live_limit = min(
        MAX_LIVE_SYMBOLS,
        account["instruments_limit"]
    )

    monitored = ranked[:live_limit]

    log(
        f"Pairs ready: {len(ranked)} | "
        f"Live monitored: {len(monitored)}"
    )

    log(
        "Watchlist: "
        + ", ".join(item["symbol"] for item in monitored)
    )

    # The 120-second clock starts after history is ready.
    deadline = time.time() + MAX_SCAN_SECONDS

    reconnects = 0

    while time.time() < deadline:

        try:

            run_live_stream(
                monitored,
                histories,
                deadline
            )

            break

        except StreamRateLimitError as exc:

            reconnects += 1

            wait_time = max(65, exc.retry_after + 5)

            remaining_time = deadline - time.time()

            log(
                f"OTCharts stream rate limit "
                f"{reconnects}/{MAX_RECONNECTS}. "
                f"Retry-After={exc.retry_after}s."
            )

            if reconnects > MAX_RECONNECTS:
                log("Maximum reconnect attempts reached.")
                break

            if remaining_time <= wait_time:
                log(
                    "Not enough time left in the 120-second "
                    "window to reconnect safely."
                )
                break

            log(f"Waiting {wait_time} seconds before reconnect.")
            time.sleep(wait_time)

        except requests.exceptions.RequestException as exc:

            reconnects += 1

            log(
                f"Stream connection error "
                f"{reconnects}/{MAX_RECONNECTS}: {exc}"
            )

            if reconnects > MAX_RECONNECTS:
                break

            remaining_time = deadline - time.time()

            if remaining_time <= 5:
                break

            wait_time = min(5, remaining_time)
            log(f"Retrying stream in {wait_time:.0f} seconds.")
            time.sleep(wait_time)

        except Exception as exc:

            log(
                f"Bot stopped: "
                f"{type(exc).__name__}: {exc}"
            )

            raise

    log(f"{VERSION} finished.")


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
