
import os
import json
import time
import math
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo


# =========================================================
# ROMARIO TRADER PRO 14.0
# S30 | Expiration 60s | Manual Telegram Alerts
# Bollinger Reversal + EMA Pullback + Engulfing
# OTCharts Lite
# =========================================================

VERSION = "ROMARIO TRADER PRO 14.0"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

# S30 = 30 seconds
TIMEFRAME = 30

CANDLE_LIMIT = 300
MIN_CANDLES = 100

MIN_PAYOUT = 92.0
MAX_LIVE_SYMBOLS = 5
MAX_HISTORY_REQUESTS_PER_RUN = 100

BB_PERIOD = 20
BB_STD = 2.2

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 7

# At least 2 of the 3 strategies must agree.
MIN_STRATEGY_AGREEMENT = 2

# Manual entry timing
ENTRY_DELAY_SECONDS = 10
ENTRY_GRACE_SECONDS = 3
EXPIRATION_SECONDS = 60

# Avoid signal flooding
GLOBAL_COOLDOWN_SECONDS = 60
PAIR_COOLDOWN_SECONDS = 180

MAX_SCAN_MINUTES = 300
MAX_RECONNECTS = 3

API_TIMEOUT = 30

TELEGRAM_ATTEMPTS = 5
TELEGRAM_TIMEOUT = (2, 3)
TELEGRAM_RETRY_DELAYS = [0.3, 0.5, 0.8, 1.0]

TIMEZONE = ZoneInfo("America/New_York")

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
    "User-Agent": "RomarioTraderPro/14.0",
    "Accept": "application/json"
})


# =========================================================
# GENERAL
# =========================================================

def log(message):
    logging.info(message)


def local_now():
    return datetime.now(TIMEZONE)


def make_entry_time(signal_time):
    return signal_time + ENTRY_DELAY_SECONDS


# =========================================================
# OTCHARTS API
# =========================================================

def api_get(path, params=None, stream=False):

    url = BASE_URL + path
    attempts = 1 if stream else 5

    for attempt in range(1, attempts + 1):

        response = None

        try:
            response = session.get(
                url,
                params=params,
                timeout=(
                    15,
                    None if stream else API_TIMEOUT
                ),
                stream=stream
            )

            if response.status_code == 429:
                raise RuntimeError(
                    "OTCharts rate limit reached."
                )

            if response.status_code in (401, 402, 403):
                raise RuntimeError(
                    f"OTCharts access error "
                    f"{response.status_code}: "
                    f"{response.text[:300]}"
                )

            if response.status_code in (500, 502, 503, 504):

                if attempt < attempts:

                    wait = min(
                        3 * (2 ** (attempt - 1)),
                        30
                    )

                    log(
                        f"Server error {response.status_code}. "
                        f"Retry in {wait}s."
                    )

                    response.close()
                    time.sleep(wait)
                    continue

            response.raise_for_status()

            if stream:
                return response

            return response.json()

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError
        ) as exc:

            if attempt >= attempts:
                raise RuntimeError(
                    f"OTCharts connection failed: {exc}"
                )

            wait = min(
                3 * (2 ** (attempt - 1)),
                30
            )

            log(f"API retry {attempt}: {exc}")

            if response is not None:
                response.close()

            time.sleep(wait)

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
                log(
                    f"Telegram delivered "
                    f"{attempt}/{TELEGRAM_ATTEMPTS}"
                )
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
            wait = TELEGRAM_RETRY_DELAYS[
                min(
                    attempt - 1,
                    len(TELEGRAM_RETRY_DELAYS) - 1
                )
            ]
            time.sleep(wait)

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

    available_timeframes = otc.get("timeframes", [])

    normalized_timeframes = set()

    for value in available_timeframes:
        try:
            normalized_timeframes.add(int(value))
        except (TypeError, ValueError):
            continue

    log(
        f"OTC timeframes available: "
        f"{sorted(normalized_timeframes)}"
    )

    if TIMEFRAME not in normalized_timeframes:
        raise RuntimeError(
            "S30 NOT CONFIRMED by OTCharts. "
            f"Available timeframes: "
            f"{available_timeframes}. "
            "Bot stopped to avoid using the wrong timeframe."
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
# FOREX PAIRS
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


# =========================================================
# ELIGIBLE OTC SYMBOLS
# =========================================================

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
            "payout": payout,
            "name": item.get("name", symbol)
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
        log(f"{symbol}: missing historical bars.")
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


def bollinger_bands(
    closes,
    period=20,
    std_dev=2.2
):

    if len(closes) < period:
        return None, None, None

    window = closes[-period:]
    sma = sum(window) / period

    variance = sum(
        (x - sma) ** 2
        for x in window
    ) / period

    sd = math.sqrt(variance)

    return (
        sma + std_dev * sd,
        sma,
        sma - std_dev * sd
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


# =========================================================
# CANDLE CONFIRMATION
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
        and 25 <= rsi <= 38
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
        and 62 <= rsi <= 75
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
# STRATEGY 2: EMA 9/21/50 PULLBACK
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

    # Bullish trend pullback
    if (
        ema9 > ema21 > ema50
        and prev["close"] < prev["open"]
        and prev["low"] <= ema9_prev
        and last["close"] > last["open"]
        and last["close"] > ema9
        and 45 <= rsi <= 65
    ):
        return {
            "direction": "CALL",
            "strategy": "EMA 9/21/50 PULLBACK"
        }

    # Bearish trend pullback
    if (
        ema9 < ema21 < ema50
        and prev["close"] > prev["open"]
        and prev["high"] >= ema9_prev
        and last["close"] < last["open"]
        and last["close"] < ema9
        and 35 <= rsi <= 55
    ):
        return {
            "direction": "PUT",
            "strategy": "EMA 9/21/50 PULLBACK"
        }

    return None


# =========================================================
# STRATEGY 3: ENGULFING MOMENTUM
# =========================================================

def strategy_engulfing(prev, last, rsi):

    bullish = (
        prev["close"] < prev["open"]
        and last["close"] > last["open"]
        and last["close"] > prev["open"]
        and last["open"] <= prev["close"]
        and 42 <= rsi <= 62
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
        and 38 <= rsi <= 58
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
    atr = calculate_atr(candles)

    values = (
        upper, middle, lower,
        prev_upper, prev_lower,
        ema9, ema21, ema50,
        ema9_prev, rsi, atr
    )

    if any(value is None for value in values):
        return None

    if atr <= 0:
        return None

    prev = candles[-2]
    last = candles[-1]

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

        strategy_engulfing(
            prev,
            last,
            rsi
        )
    ]

    candidates = [
        result for result in results
        if result is not None
    ]

    if len(candidates) < MIN_STRATEGY_AGREEMENT:
        return None

    directions = {
        item["direction"]
        for item in candidates
    }

    # Never trade if strategies disagree.
    if len(directions) != 1:
        return None

    direction = candidates[0]["direction"]

    # Additional trend confirmation.
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

    else:

        if ema9 < ema21:
            confirmations.append("EMA BEARISH")

        if ema21 < ema50:
            confirmations.append("EMA50 TREND")

        if last["close"] < ema9:
            confirmations.append("PRICE BELOW EMA9")

        if last["close"] < last["open"]:
            confirmations.append("BEARISH CANDLE")

    # Select the actual strategies that agreed.
    strategy_names = [
        item["strategy"]
        for item in candidates
    ]

    score = min(
        8,
        4
        + (len(candidates) - 2)
        + min(len(confirmations), 2)
    )

    if score < 5:
        return None

    return {
        "direction": direction,
        "strategies": strategy_names,
        "strategies_agree": len(candidates),
        "score": score,
        "rsi": round(rsi, 2),
        "ema9": ema9,
        "ema21": ema21,
        "ema50": ema50,
        "atr": atr,
        "close": last["close"],
        "confirmations": confirmations,
        "candle_time": last["time"]
    }


# =========================================================
# TELEGRAM SIGNAL
# =========================================================

def send_signal(
    symbol,
    payout,
    signal,
    detection_time,
    entry_time
):

    signal_datetime = datetime.fromtimestamp(
        detection_time,
        TIMEZONE
    )

    entry_datetime = datetime.fromtimestamp(
        entry_time,
        TIMEZONE
    )

    direction = signal["direction"]

    arrow = "🟢" if direction == "CALL" else "🔴"

    strategy_text = "\n".join(
        f"• {name}"
        for name in signal["strategies"]
    )

    message = (
        f"🤖 {VERSION}\n\n"
        f"{arrow} {direction}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"💰 PAYOUT: {payout:.1f}%\n"
        f"⏱ TIMEFRAME: S30\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🧠 STRATEGIES:\n"
        f"{strategy_text}\n\n"
        f"🤝 AGREEMENT: "
        f"{signal['strategies_agree']}/3\n"
        f"⭐ QUALITY SCORE: "
        f"{signal['score']}/8\n"
        f"📊 RSI 7: {signal['rsi']}\n"
        f"📈 EMA 9: {signal['ema9']:.8f}\n"
        f"📈 EMA 21: {signal['ema21']:.8f}\n"
        f"📈 EMA 50: {signal['ema50']:.8f}\n\n"
        f"⏰ SIGNAL: "
        f"{signal_datetime.strftime('%I:%M:%S %p')}\n"
        f"🎯 MANUAL ENTRY: "
        f"{entry_datetime.strftime('%I:%M:%S %p')}\n\n"
        f"⚠️ If entry is missed by more than "
        f"{ENTRY_GRACE_SECONDS} seconds, skip it."
    )

    return send_telegram(message)


# =========================================================
# RESULT ESTIMATE
# =========================================================

def send_result(symbol, pending, exit_price):

    entry_price = pending["entry_price"]

    tolerance = max(
        abs(entry_price) * 1e-10,
        1e-12
    )

    difference = exit_price - entry_price

    if abs(difference) <= tolerance:
        result = "TIE"

    elif pending["direction"] == "CALL":
        result = "WIN" if difference > 0 else "LOSS"

    else:
        result = "WIN" if difference < 0 else "LOSS"

    message = (
        f"🤖 {VERSION}\n\n"
        f"📊 OTCHARTS RESULT ESTIMATE\n"
        f"💱 PAIR: {symbol}\n"
        f"📍 SIGNAL: {pending['direction']}\n"
        f"📈 ESTIMATE: {result}\n\n"
        f"Entry: {entry_price}\n"
        f"Exit: {exit_price}\n\n"
        f"⚠️ This is only an OTCharts price estimate. "
        f"It is NOT a verified Pocket Option result."
    )

    send_telegram(message)

    log(f"{symbol}: estimated result={result}")


# =========================================================
# LIVE STREAM
# =========================================================

def run_live_stream(monitored, histories):

    symbols = [
        item["symbol"]
        for item in monitored
    ]

    params = {
        "venue": VENUE,
        "symbol": ",".join(symbols)
    }

    log(
        f"Opening S30 stream for "
        f"{len(symbols)} pairs."
    )

    response = api_get(
        "/v1/stream",
        params=params,
        stream=True
    )

    current_bars = {}
    bar_complete = {}
    last_analyzed_bar = {}

    connected_symbols = set()

    # ONE global pending signal only.
    pending_signal = None

    last_global_signal_time = 0
    last_pair_signal_time = {}

    started_at = time.time()
    event_name = ""

    try:

        for raw_line in response.iter_lines(
            decode_unicode=True,
            chunk_size=1
        ):

            now = time.time()

            if now - started_at > MAX_SCAN_MINUTES * 60:
                log("Maximum scan duration reached.")
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

            # ---------------------------------------------
            # CONNECTED
            # ---------------------------------------------

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

            if event_name == "closed":
                log(f"Stream closed: {data}")
                continue

            if event_name == "replaced":
                log("Stream replaced.")
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

            receive_time = time.time()

            bucket = (
                feed_time // TIMEFRAME
            ) * TIMEFRAME

            # ---------------------------------------------
            # INITIAL LIVE BAR
            # ---------------------------------------------

            if symbol not in current_bars:

                history = histories.get(symbol, [])

                history = [
                    candle for candle in history
                    if candle["time"] < bucket
                ]

                if history:
                    if (
                        bucket - history[-1]["time"]
                    ) != TIMEFRAME:

                        log(
                            f"{symbol}: history/live gap. "
                            "Skipping pair."
                        )

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

                # First live candle is incomplete.
                bar_complete[symbol] = False
                continue

            current = current_bars[symbol]

            # ---------------------------------------------
            # NEW S30 CANDLE
            # ---------------------------------------------

            new_closed_candle = False

            if bucket > current["time"]:

                gap = bucket - current["time"]

                if gap != TIMEFRAME:

                    log(
                        f"{symbol}: live S30 candle gap. "
                        "Skipping pair."
                    )

                    connected_symbols.discard(symbol)
                    continue

                if bar_complete.get(symbol, False):

                    histories[symbol].append(current)

                    histories[symbol] = (
                        histories[symbol][-CANDLE_LIMIT:]
                    )

                    new_closed_candle = True

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

                bar_complete[symbol] = True

            elif bucket == current["time"]:

                current["high"] = max(
                    current["high"],
                    price
                )

                current["low"] = min(
                    current["low"],
                    price
                )

                current["close"] = price

            else:
                continue

            # ---------------------------------------------
            # PENDING SIGNAL: ENTRY AND EXPIRATION
            # ---------------------------------------------

            if pending_signal is not None:

                pending = pending_signal

                if pending["entry_price"] is None:

                    if receive_time > (
                        pending["entry_time"]
                        + ENTRY_GRACE_SECONDS
                    ):

                        log(
                            f"{pending['symbol']}: "
                            "manual entry window missed."
                        )

                        send_telegram(
                            f"⚠️ {VERSION}\n\n"
                            f"{pending['symbol']}: "
                            f"ENTRY MISSED. Skip this signal."
                        )

                        pending_signal = None
                        pending = None

                    elif receive_time >= pending["entry_time"]:

                        pending["entry_price"] = price

                        log(
                            f"{pending['symbol']}: "
                            f"reference entry price={price}"
                        )

                if (
                    pending is not None
                    and pending["entry_price"] is not None
                    and receive_time >= pending["expiry_time"]
                ):

                    send_result(
                        pending["symbol"],
                        pending,
                        price
                    )

                    pending_signal = None

            # ---------------------------------------------
            # ONE SIGNAL AT A TIME
            # ---------------------------------------------

            if pending_signal is not None:
                continue

            # Analyze only after a candle has closed.
            if not new_closed_candle:
                continue

            if len(histories.get(symbol, [])) < MIN_CANDLES:
                continue

            closed_candles = histories[symbol]

            candle_key = closed_candles[-1]["time"]

            if last_analyzed_bar.get(symbol) == candle_key:
                continue

            last_analyzed_bar[symbol] = candle_key

            # Global cooldown
            if (
                receive_time - last_global_signal_time
                < GLOBAL_COOLDOWN_SECONDS
            ):
                continue

            # Pair cooldown
            if (
                receive_time
                - last_pair_signal_time.get(symbol, 0)
                < PAIR_COOLDOWN_SECONDS
            ):
                continue

            signal = analyze_candles(closed_candles)

            if not signal:
                continue

            pair_info = next(
                (
                    item for item in monitored
                    if item["symbol"] == symbol
                ),
                None
            )

            if not pair_info:
                continue

            # ---------------------------------------------
            # SCHEDULE MANUAL ENTRY
            # ---------------------------------------------

            detection_time = time.time()

            entry_time = make_entry_time(
                detection_time
            )

            expiry_time = (
                entry_time + EXPIRATION_SECONDS
            )

            log(
                f"S30 SIGNAL {signal['direction']} "
                f"{symbol} | "
                f"strategies={signal['strategies_agree']}/3 | "
                f"score={signal['score']}/8 | "
                f"entry in {ENTRY_DELAY_SECONDS}s"
            )

            delivered = send_signal(
                symbol,
                pair_info["payout"],
                signal,
                detection_time,
                entry_time
            )

            if not delivered:

                log(
                    f"{symbol}: Telegram failed. "
                    "Signal not activated."
                )

                continue

            # Check whether Telegram delivery took too long.
            if time.time() >= entry_time:

                log(
                    f"{symbol}: entry time passed "
                    "before delivery completed. Cancelled."
                )

                send_telegram(
                    f"⚠️ {VERSION}\n\n"
                    f"{symbol}: signal cancelled because "
                    f"Telegram delivery was late."
                )

                continue

            pending_signal = {
                "symbol": symbol,
                "direction": signal["direction"],
                "entry_time": entry_time,
                "expiry_time": expiry_time,
                "entry_price": None
            }

            last_global_signal_time = time.time()
            last_pair_signal_time[symbol] = time.time()

            log(
                f"{symbol}: activated. "
                f"One global signal is now locked."
            )

    finally:
        response.close()


# =========================================================
# MAIN
# =========================================================

def main():

    log(f"{VERSION} starting...")
    log(
        "SETTINGS: S30 | EXPIRATION 60s | "
        "ENTRY 10s | MIN PAYOUT 92%"
    )

    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing.")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram credentials missing.")

    # 1. Account and S30 verification
    account = check_account()

    # 2. Eligible OTC pairs
    eligible = get_eligible_symbols()

    if not eligible:
        log("No eligible Forex OTC pairs.")
        return

    if len(eligible) > MAX_HISTORY_REQUESTS_PER_RUN:
        raise RuntimeError(
            f"Too many pairs: {len(eligible)}"
        )

    estimated_requests = len(eligible) + 4

    if account["remaining"] < estimated_requests:
        raise RuntimeError(
            "Not enough API requests to analyze pairs."
        )

    # 3. Historical candles
    histories = {}
    ranked = []

    log(
        f"Downloading S30 history for "
        f"{len(eligible)} eligible pairs."
    )

    for item in eligible:

        symbol = item["symbol"]

        try:

            candles = get_candles(symbol)

            if not candles:
                log(
                    f"{symbol}: insufficient S30 history."
                )
                continue

            histories[symbol] = candles

            ranked.append({
                **item,
                "history_count": len(candles)
            })

            log(
                f"History ready: {symbol} | "
                f"Payout={item['payout']}% | "
                f"Candles={len(candles)}"
            )

        except Exception as exc:

            log(
                f"{symbol}: history error: {exc}"
            )

    if not ranked:
        log("No pairs have sufficient S30 history.")
        return

    # 4. Live instruments
    live_limit = min(
        MAX_LIVE_SYMBOLS,
        account["instruments_limit"]
    )

    monitored = ranked[:live_limit]

    log(
        f"Eligible analyzed: {len(ranked)} | "
        f"Live monitored: {len(monitored)}"
    )

    log(
        "Watchlist: "
        + ", ".join(
            item["symbol"]
            for item in monitored
        )
    )

    # 5. Live stream
    reconnects = 0

    while reconnects <= MAX_RECONNECTS:

        try:

            run_live_stream(
                monitored,
                histories
            )

            break

        except requests.RequestException as exc:

            reconnects += 1

            log(
                f"Stream error "
                f"{reconnects}/{MAX_RECONNECTS}: {exc}"
            )

            if reconnects > MAX_RECONNECTS:
                break

            time.sleep(5)

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
