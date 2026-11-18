
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


# ============================================================
# ROMARIO TRADER PRO 15.1 ULTRA FAST
# OTCharts OTC | S30 | EXPIRATION 60 SECONDS
# Fast multi-pair scanner | Telegram alerts
# ============================================================

VERSION = "ROMARIO TRADER PRO 15.1"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 30

CANDLE_LIMIT = 150
MIN_CANDLES = 60

MIN_PAYOUT = 85.0
MAX_LIVE_SYMBOLS = 5
MAX_HISTORY_WORKERS = 5

MAX_SCAN_SECONDS = 120
MAX_RECONNECTS = 3

# Indicators
BB_PERIOD = 20
BB_STD = 2.0

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 7
ATR_PERIOD = 14
ADX_PERIOD = 14

# Signal filters
MIN_SIGNAL_SCORE = 5
ADX_MIN = 8.0
MIN_DI_GAP = 0.5

MIN_CANDLE_ATR_RATIO = 0.08
MAX_CANDLE_ATR_RATIO = 2.50

# Execution
EXPIRATION_SECONDS = 60

API_CONNECT_TIMEOUT = 3
API_READ_TIMEOUT = 15

TELEGRAM_ATTEMPTS = 3
TELEGRAM_TIMEOUT = (2, 4)

TIMEZONE = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

_thread_local = threading.local()


# ============================================================
# LOGGING AND SESSION
# ============================================================

def log(message):
    logging.info(message)


def get_session():
    if not hasattr(_thread_local, "session"):
        session = requests.Session()

        session.headers.update({
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "RomarioTraderPro/15.1",
            "Accept": "application/json"
        })

        _thread_local.session = session

    return _thread_local.session


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
                raise RuntimeError("OTCharts rate limit reached.")

            response.raise_for_status()

            if stream:
                return response

            return response.json()

        except Exception:

            if response is not None:
                response.close()

            if attempt >= attempts:
                raise

            time.sleep(0.3)

    raise RuntimeError("API request failed.")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials missing.")
        return False

    url = (
        f"https://api.telegram.org/"
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

            if response.ok:
                return True

            log(f"Telegram HTTP error: {response.status_code}")

        except Exception as exc:
            log(f"Telegram attempt {attempt}: {exc}")

        finally:
            if response is not None:
                response.close()

        if attempt < TELEGRAM_ATTEMPTS:
            time.sleep(0.3)

    return False


# ============================================================
# ACCOUNT AND OTC CHECK
# ============================================================

def check_account():

    usage = api_get("/v1/usage")
    venues_data = api_get("/v1/venues")

    otc = next(
        (
            item for item in venues_data.get("venues", [])
            if item.get("id") == VENUE
        ),
        None
    )

    if not otc or otc.get("open") is not True:
        raise RuntimeError("OTCharts OTC venue is not available.")

    log("OTCharts account connected.")
    log(f"OTC venue: {otc.get('id')}")

    if usage:
        log("API usage information received.")

    return True


# ============================================================
# SYMBOL FILTER
# ============================================================

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD",
    "AUD", "NZD", "NOK", "SEK", "DKK", "PLN",
    "CZK", "HUF", "TRY", "ZAR", "MXN", "BRL",
    "INR", "SGD", "HKD", "THB", "MYR", "IDR",
    "PHP", "KRW", "TWD", "SAR", "AED", "QAR"
}


def is_forex_pair(symbol):

    if not isinstance(symbol, str):
        return False

    if not symbol.endswith("_otc"):
        return False

    pair = symbol[:-4]

    if len(pair) != 6 or not pair.isalpha():
        return False

    return (
        pair[:3].upper() in CURRENCIES
        and pair[3:].upper() in CURRENCIES
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

    log(f"Eligible OTC Forex pairs: {len(eligible)}")

    return eligible


# ============================================================
# HISTORY
# ============================================================

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

    parsed = []

    for candle in data.get("candles", []):

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

    if len(parsed) < MIN_CANDLES:
        return []

    return parsed[-CANDLE_LIMIT:]


def download_histories(monitored):

    histories = {}

    def fetch(item):
        return item["symbol"], get_candles(item["symbol"])

    with ThreadPoolExecutor(
        max_workers=MAX_HISTORY_WORKERS
    ) as executor:

        futures = [
            executor.submit(fetch, item)
            for item in monitored
        ]

        for future in as_completed(futures):

            try:
                symbol, candles = future.result()

                if candles:
                    histories[symbol] = candles
                    log(f"{symbol}: {len(candles)} candles loaded.")

            except Exception as exc:
                log(f"History error: {exc}")

    return histories


# ============================================================
# INDICATORS
# ============================================================

def ema_series(values, period):

    if len(values) < period:
        return []

    multiplier = 2.0 / (period + 1)

    current = sum(values[:period]) / period

    result = [None] * (period - 1)
    result.append(current)

    for value in values[period:]:

        current = (
            value * multiplier
            + current * (1 - multiplier)
        )

        result.append(current)

    return result


def bollinger_bands(closes, period=20, std_dev=2.0):

    if len(closes) < period:
        return None, None, None

    window = closes[-period:]

    middle = sum(window) / period

    variance = sum(
        (x - middle) ** 2 for x in window
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

    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]

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
        return 100.0 if avg_gain > 0 else 50.0

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

    trs = []
    plus_dms = []
    minus_dms = []

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

        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    tr_smooth = sum(trs[:period])
    plus_smooth = sum(plus_dms[:period])
    minus_smooth = sum(minus_dms[:period])

    dx_values = []

    plus_di = 0.0
    minus_di = 0.0

    for i in range(period, len(trs)):

        if i > period:
            tr_smooth = (
                tr_smooth - tr_smooth / period + trs[i]
            )

            plus_smooth = (
                plus_smooth - plus_smooth / period
                + plus_dms[i]
            )

            minus_smooth = (
                minus_smooth - minus_smooth / period
                + minus_dms[i]
            )

        if tr_smooth <= 0:
            continue

        plus_di = 100 * plus_smooth / tr_smooth
        minus_di = 100 * minus_smooth / tr_smooth

        denominator = plus_di + minus_di

        if denominator > 0:
            dx = (
                100 * abs(plus_di - minus_di)
                / denominator
            )
            dx_values.append(dx)

    if len(dx_values) < period:
        return None, None, None

    adx = sum(dx_values[-period:]) / period

    return adx, plus_di, minus_di


# ============================================================
# SIGNAL ENGINE
# ============================================================

def analyze_candles(candles):

    if len(candles) < MIN_CANDLES:
        return None

    closes = [c["close"] for c in candles]

    last = candles[-1]
    previous = candles[-2]

    upper, middle, lower = bollinger_bands(
        closes, BB_PERIOD, BB_STD
    )

    ema9_values = ema_series(closes, EMA_FAST)
    ema21_values = ema_series(closes, EMA_MID)
    ema50_values = ema_series(closes, EMA_SLOW)

    rsi = calculate_rsi(closes, RSI_PERIOD)
    previous_rsi = calculate_rsi(
        closes[:-1], RSI_PERIOD
    )

    atr = calculate_atr(candles, ATR_PERIOD)

    adx, plus_di, minus_di = calculate_adx(
        candles, ADX_PERIOD
    )

    required = [
        upper, middle, lower, rsi, previous_rsi,
        atr, adx, plus_di, minus_di
    ]

    if any(v is None for v in required):
        return None

    ema9 = ema9_values[-1]
    ema21 = ema21_values[-1]
    ema50 = ema50_values[-1]

    if any(v is None for v in [ema9, ema21, ema50]):
        return None

    candle_range = last["high"] - last["low"]

    if atr <= 0 or candle_range <= 0:
        return None

    candle_ratio = candle_range / atr

    if not (
        MIN_CANDLE_ATR_RATIO
        <= candle_ratio
        <= MAX_CANDLE_ATR_RATIO
    ):
        return None

    bullish = last["close"] > last["open"]
    bearish = last["close"] < last["open"]

    rsi_rising = rsi > previous_rsi
    rsi_falling = rsi < previous_rsi

    # Bollinger rejection
    lower_rejection = (
        last["low"] <= lower
        and last["close"] > lower
    )

    upper_rejection = (
        last["high"] >= upper
        and last["close"] < upper
    )

    # Trend conditions
    bullish_trend = (
        ema9 > ema21
        and ema21 > ema50
    )

    bearish_trend = (
        ema9 < ema21
        and ema21 < ema50
    )

    bullish_di = (
        plus_di > minus_di + MIN_DI_GAP
    )

    bearish_di = (
        minus_di > plus_di + MIN_DI_GAP
    )

    strong_trend = adx >= ADX_MIN

    # ========================================================
    # CALL SCORING
    # ========================================================

    call_score = 0
    call_confirmations = []

    if ema9 > ema21:
        call_score += 1
        call_confirmations.append("EMA 9 > EMA 21")

    if ema21 > ema50:
        call_score += 1
        call_confirmations.append("EMA 21 > EMA 50")

    if last["close"] > ema9:
        call_score += 1
        call_confirmations.append("PRICE ABOVE EMA 9")

    if 45 <= rsi <= 68 and rsi_rising:
        call_score += 1
        call_confirmations.append("RSI BULLISH")

    if bullish_di:
        call_score += 1
        call_confirmations.append("DI+ CONFIRMED")

    if strong_trend:
        call_score += 1
        call_confirmations.append("ADX ACTIVE")

    if lower_rejection:
        call_score += 1
        call_confirmations.append("BOLLINGER LOWER REJECTION")

    if bullish:
        call_score += 1
        call_confirmations.append("BULLISH CANDLE")

    call_momentum = (
        bullish_trend
        and last["close"] > ema9
        and 48 <= rsi <= 68
        and bullish_di
        and strong_trend
    )

    call_reversal = (
        lower_rejection
        and bullish
        and rsi < 48
        and rsi_rising
        and not bearish_di
    )

    # ========================================================
    # PUT SCORING
    # ========================================================

    put_score = 0
    put_confirmations = []

    if ema9 < ema21:
        put_score += 1
        put_confirmations.append("EMA 9 < EMA 21")

    if ema21 < ema50:
        put_score += 1
        put_confirmations.append("EMA 21 < EMA 50")

    if last["close"] < ema9:
        put_score += 1
        put_confirmations.append("PRICE BELOW EMA 9")

    if 32 <= rsi <= 55 and rsi_falling:
        put_score += 1
        put_confirmations.append("RSI BEARISH")

    if bearish_di:
        put_score += 1
        put_confirmations.append("DI- CONFIRMED")

    if strong_trend:
        put_score += 1
        put_confirmations.append("ADX ACTIVE")

    if upper_rejection:
        put_score += 1
        put_confirmations.append("BOLLINGER UPPER REJECTION")

    if bearish:
        put_score += 1
        put_confirmations.append("BEARISH CANDLE")

    put_momentum = (
        bearish_trend
        and last["close"] < ema9
        and 32 <= rsi <= 52
        and bearish_di
        and strong_trend
    )

    put_reversal = (
        upper_rejection
        and bearish
        and rsi > 52
        and rsi_falling
        and not bullish_di
    )

    # ========================================================
    # FINAL DECISION
    # ========================================================

    call_valid = (
        call_score >= MIN_SIGNAL_SCORE
        and (call_momentum or call_reversal)
    )

    put_valid = (
        put_score >= MIN_SIGNAL_SCORE
        and (put_momentum or put_reversal)
    )

    if call_valid and put_valid:
        return None

    if call_valid:

        return {
            "direction": "CALL",
            "score": call_score,
            "rsi": round(rsi, 2),
            "adx": round(adx, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "confirmations": call_confirmations
        }

    if put_valid:

        return {
            "direction": "PUT",
            "score": put_score,
            "rsi": round(rsi, 2),
            "adx": round(adx, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "confirmations": put_confirmations
        }

    return None


# ============================================================
# ENTRY TIME
# ============================================================

def make_exact_entry_time(feed_time):

    return (feed_time // TIMEFRAME + 1) * TIMEFRAME


def format_price(value):

    return f"{float(value):.8f}".rstrip("0").rstrip(".")


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def send_signal(
    symbol,
    payout,
    signal,
    detection_time,
    entry_time
):

    signal_dt = datetime.fromtimestamp(
        detection_time, TIMEZONE
    )

    entry_dt = datetime.fromtimestamp(
        entry_time, TIMEZONE
    )

    direction = signal["direction"]

    arrow = (
        "🟢" if direction == "CALL" else "🔴"
    )

    confirmations = "\n".join(
        f"✓ {item}"
        for item in signal["confirmations"]
    )

    message = (
        f"🤖 {VERSION}\n\n"
        f"{arrow} {direction}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"💰 PAYOUT: {payout:.1f}%\n\n"
        f"⏱ TIMEFRAME: S30\n"
        f"⌛ EXPIRATION: 60s\n\n"
        f"⭐ QUALITY: {signal['score']}/8\n"
        f"📊 RSI 7: {signal['rsi']}\n"
        f"📊 ADX: {signal['adx']}\n"
        f"DI+: {signal['plus_di']}\n"
        f"DI-: {signal['minus_di']}\n\n"
        f"📋 CONFIRMATIONS:\n"
        f"{confirmations}\n\n"
        f"⏰ DETECTED: "
        f"{signal_dt.strftime('%I:%M:%S %p')}\n"
        f"🎯 EXACT ENTRY: "
        f"{entry_dt.strftime('%I:%M:%S %p')}\n\n"
        f"⚠️ Entry next S30 boundary. "
        f"Verify the actual platform price."
    )

    return send_telegram(message)


# ============================================================
# RESULT TRACKING
# ============================================================

def send_result(symbol, pending, exit_price):

    entry_price = pending["entry_price"]
    direction = pending["direction"]

    if direction == "CALL":

        if exit_price > entry_price:
            result = "WIN"
            emoji = "🟢"

        elif exit_price < entry_price:
            result = "LOSS"
            emoji = "🔴"

        else:
            result = "DRAW"
            emoji = "🟡"

    else:

        if exit_price < entry_price:
            result = "WIN"
            emoji = "🟢"

        elif exit_price > entry_price:
            result = "LOSS"
            emoji = "🔴"

        else:
            result = "DRAW"
            emoji = "🟡"

    difference = exit_price - entry_price

    message = (
        f"📊 {VERSION} - RESULT\n\n"
        f"{emoji} {result}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"📍 DIRECTION: {direction}\n"
        f"🎯 ENTRY PRICE: {format_price(entry_price)}\n"
        f"🏁 EXIT PRICE: {format_price(exit_price)}\n"
        f"📐 PRICE CHANGE: {difference:+.8f}\n\n"
        f"⌛ EXPIRATION: 60 SECONDS\n"
        f"ℹ️ OTCharts tick estimate only. "
        f"Not an official Pocket Option result."
    )

    send_telegram(message)


# ============================================================
# LIVE STREAM
# ============================================================

def run_live_stream(monitored, histories, deadline):

    symbols = [
        item["symbol"] for item in monitored
    ]

    params = {
        "venue": VENUE,
        "symbol": ",".join(symbols)
    }

    log(
        f"Opening S30 stream for {len(symbols)} pairs."
    )

    response = api_get(
        "/v1/stream",
        params=params,
        stream=True
    )

    current_bars = {}
    connected_symbols = set()

    pending_signal = None
    event_name = ""

    try:

        for raw_line in response.iter_lines(
            decode_unicode=True,
            chunk_size=1
        ):

            if time.monotonic() >= deadline:
                log("120-second scan window completed.")
                return

            if not raw_line:
                event_name = ""
                continue

            line = (
                raw_line.decode(
                    "utf-8", errors="replace"
                ).strip()
                if isinstance(raw_line, bytes)
                else raw_line.strip()
            )

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
                event_name = ""
                continue

            if event_name == "connected":

                connected_symbols = set(
                    data.get("symbols", [])
                )

                log(
                    "Connected symbols: "
                    + ", ".join(sorted(connected_symbols))
                )

                event_name = ""
                continue

            if event_name != "tick":
                event_name = ""
                continue

            event_name = ""

            symbol = data.get("symbol")

            if symbol not in connected_symbols:
                continue

            try:
                price = float(data["price"])
                feed_time = int(data["time"])
            except (KeyError, TypeError, ValueError):
                continue

            # ------------------------------------------------
            # PENDING SIGNAL: CAPTURE ENTRY AND EXIT
            # ------------------------------------------------

            if pending_signal is not None:

                if symbol != pending_signal["symbol"]:
                    continue

                pending = pending_signal

                if pending["entry_price"] is None:

                    if feed_time >= pending["entry_time"]:

                        # Do not invent an entry if the first
                        # available tick is already past expiry.
                        if feed_time >= pending["expiry_time"]:
                            log(
                                "Entry tick unavailable before expiry."
                            )
                            return

                        pending["entry_price"] = price

                        log(
                            f"Entry captured: {symbol} "
                            f"at {format_price(price)}"
                        )

                elif feed_time >= pending["expiry_time"]:

                    send_result(
                        symbol,
                        pending,
                        price
                    )

                    log("Signal result completed.")
                    return

                continue

            # ------------------------------------------------
            # BUILD S30 CANDLES
            # ------------------------------------------------

            bucket = (
                feed_time // TIMEFRAME
            ) * TIMEFRAME

            if symbol not in current_bars:

                history = [
                    c for c in histories.get(symbol, [])
                    if c["time"] < bucket
                ]

                histories[symbol] = history

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

                continue

            current = current_bars[symbol]

            if bucket > current["time"]:

                # Previous candle is now closed.
                histories[symbol].append(current)

                histories[symbol] = (
                    histories[symbol][-CANDLE_LIMIT:]
                )

                closed_candles = histories[symbol]

                signal = analyze_candles(
                    closed_candles
                )

                if signal:

                    pair_info = next(
                        (
                            item for item in monitored
                            if item["symbol"] == symbol
                        ),
                        None
                    )

                    if pair_info:

                        detection_time = time.time()

                        entry_time = make_exact_entry_time(
                            feed_time
                        )

                        sent = send_signal(
                            symbol,
                            pair_info["payout"],
                            signal,
                            detection_time,
                            entry_time
                        )

                        if sent:

                            pending_signal = {
                                "symbol": symbol,
                                "direction": signal["direction"],
                                "entry_time": entry_time,
                                "expiry_time": (
                                    entry_time
                                    + EXPIRATION_SECONDS
                                ),
                                "entry_price": None
                            }

                            log(
                                f"{symbol}: "
                                f"{signal['direction']} signal sent."
                            )

                        else:
                            log(
                                "Signal not activated: "
                                "Telegram delivery failed."
                            )

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

            elif bucket == current["time"]:

                current["high"] = max(
                    current["high"], price
                )

                current["low"] = min(
                    current["low"], price
                )

                current["close"] = price

    finally:
        response.close()


# ============================================================
# MAIN
# ============================================================

def main():

    log("=" * 55)
    log(f"{VERSION} STARTING")
    log("=" * 55)

    if not API_KEY:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing."
        )

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Telegram credentials are missing."
        )

    deadline = time.monotonic() + MAX_SCAN_SECONDS

    check_account()

    eligible = get_eligible_symbols()

    if not eligible:
        log("No eligible OTC Forex pairs.")
        return

    # Rotate the starting group so the same top five
    # are not always selected at every scheduled run.
    rotation = (
        int(time.time() // MAX_SCAN_SECONDS)
        % len(eligible)
    )

    rotated = (
        eligible[rotation:] + eligible[:rotation]
    )

    monitored = rotated[:MAX_LIVE_SYMBOLS]

    log(
        "Selected pairs: "
        + ", ".join(
            item["symbol"] for item in monitored
        )
    )

    histories = download_histories(monitored)

    monitored = [
        item for item in monitored
        if item["symbol"] in histories
    ]

    if not monitored:
        log("No pairs have enough historical candles.")
        return

    reconnects = 0

    while time.monotonic() < deadline:

        try:

            run_live_stream(
                monitored,
                histories,
                deadline
            )

            break

        except Exception as exc:

            reconnects += 1

            log(
                f"Stream error {reconnects}/"
                f"{MAX_RECONNECTS}: {exc}"
            )

            if reconnects > MAX_RECONNECTS:
                log("Maximum reconnects reached.")
                break

            if time.monotonic() + 2 >= deadline:
                break

            time.sleep(2)

    log("=" * 55)
    log(f"{VERSION} FINISHED")
    log("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("BOT FAILED")
        raise
