
import os
import sys
import json
import math
import time
import logging
import threading
import requests

from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# =====================================================
# ROMARIO PRO MULTI-TIMEFRAME 2.0
# M30 TREND + M10 DIRECTION + M1 ENTRY
# EXPIRATION 60S | UTC-5
# =====================================================

VERSION = "ROMARIO PRO MTF 2.0"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

# TIMEFRAMES (SECONDS)
TF_ENTRY = 60
TF_DIRECTION = 600
TF_TREND = 1800

EXPIRATION = 60
MIN_PAYOUT = 92.0

MAX_WAIT_SECONDS = 300
MAX_STREAM_PAIRS = 4
MAX_SIGNALS_PER_RUN = 1
MAX_RECONNECTS = 5

CANDLE_LIMIT = 100
MIN_CANDLES = 55

MIN_ADX_M1 = 22.0
MIN_ADX_HTF = 18.0
MIN_M1_SCORE = 8

# Refresh M10/M30 direction every 60 seconds
HTF_REFRESH_SECONDS = 60

TOXIC_CURRENCIES = (
    "TRY", "ZAR", "MXN", "BRL", "PLN", "RUB", "INR",
    "CNH", "THB", "HUF", "SEK", "NOK", "MYR",
    "SYP", "NGN", "IDR", "OMR"
)

API_CONNECT_TIMEOUT = 4
API_READ_TIMEOUT = 10

TELEGRAM_ATTEMPTS = 3
TELEGRAM_TIMEOUT = (2, 4)

USER_TZ = timezone(timedelta(hours=-5))

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

_thread_local = threading.local()


# =====================================================
# 1. BASIC FUNCTIONS
# =====================================================

def log(message):
    logging.info(message)


def now_ts():
    return time.time()


def normalize_ts(ts):
    ts = int(ts)
    return ts // 1000 if ts > 10_000_000_000 else ts


def format_price(value):
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def local_time(ts=None):
    if ts is None:
        ts = now_ts()
    return datetime.fromtimestamp(
        ts, tz=USER_TZ
    ).strftime("%I:%M:%S %p")


def get_session():
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update({
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "RomarioProMTF/2.0",
            "Accept": "application/json"
        })
    return _thread_local.session


def api_get(path, params=None, stream=False):
    url = BASE_URL + path
    attempts = 1 if stream else 4

    for attempt in range(1, attempts + 1):
        response = None
        try:
            response = get_session().get(
                url,
                params=params,
                stream=stream,
                timeout=(
                    API_CONNECT_TIMEOUT,
                    None if stream else API_READ_TIMEOUT
                )
            )

            if response.status_code == 429:
                retry = response.headers.get("Retry-After", "30")
                response.close()
                raise RuntimeError(
                    f"OTCharts rate limit. Retry after {retry}s"
                )

            if response.status_code in (502, 503, 504):
                code = response.status_code
                response.close()
                if attempt < attempts:
                    time.sleep(attempt * 1.5)
                    continue
                raise RuntimeError(f"OTCharts HTTP {code}")

            response.raise_for_status()

            if stream:
                return response

            return response.json()

        except Exception:
            if response is not None:
                response.close()
            if attempt >= attempts:
                raise
            time.sleep(attempt * 1.2)

    raise RuntimeError("API request failed")


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials missing")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    for attempt in range(TELEGRAM_ATTEMPTS):
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message
                },
                timeout=TELEGRAM_TIMEOUT
            )

            ok = response.ok
            response.close()

            if ok:
                return True

        except Exception as exc:
            log(f"Telegram error: {exc}")

        time.sleep(0.5)

    return False


def is_clean_pair(symbol):
    upper = symbol.upper()
    return not any(x in upper for x in TOXIC_CURRENCIES)


# =====================================================
# 2. CANDLE DATA
# =====================================================

def get_candles(symbol, timeframe, limit=CANDLE_LIMIT):
    data = api_get(
        "/v1/candles",
        params={
            "venue": VENUE,
            "symbol": symbol,
            "tf": timeframe,
            "limit": limit
        }
    )

    parsed = []

    for candle in data.get("candles", []):
        try:
            parsed.append({
                "time": normalize_ts(candle["time"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"])
            })
        except (KeyError, TypeError, ValueError):
            continue

    parsed.sort(key=lambda x: x["time"])

    return parsed[-limit:]


def get_closed_candles(symbol, timeframe):
    """
    Retire bouji ki poko fèmen.
    Timestamp bouji a konsidere kòm lè ouvèti li.
    """
    candles = get_candles(symbol, timeframe)

    current_time = int(now_ts())

    closed = [
        c for c in candles
        if c["time"] + timeframe <= current_time
    ]

    return closed[-CANDLE_LIMIT:]


# =====================================================
# 3. INDICATORS
# =====================================================

def ema(values, period):
    if len(values) < period:
        return None

    result = [None] * len(values)
    value = sum(values[:period]) / period
    result[period - 1] = value

    multiplier = 2.0 / (period + 1)

    for i in range(period, len(values)):
        value = (
            values[i] - value
        ) * multiplier + value

        result[i] = value

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calculate(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    value = calculate(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

        value = calculate(avg_gain, avg_loss)

    return value


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return None, None

    macd_values = []

    for i in range(len(values)):
        if fast[i] is not None and slow[i] is not None:
            macd_values.append(fast[i] - slow[i])

    signal = ema(macd_values, 9)

    if signal is None or signal[-1] is None:
        return None, None

    return macd_values[-1], macd_values[-1] - signal[-1]


def bollinger(values, period=20, multiplier=2):
    if len(values) < period:
        return None

    window = values[-period:]
    middle = sum(window) / period

    variance = sum(
        (x - middle) ** 2 for x in window
    ) / period

    deviation = math.sqrt(variance)

    return (
        middle + multiplier * deviation,
        middle,
        middle - multiplier * deviation
    )


def stochastic(candles, period=14):
    if len(candles) < period + 2:
        return None, None

    raw_values = []

    for i in range(len(candles) - period + 1, len(candles)):
        window = candles[i - period + 1:i + 1]

        highest = max(x["high"] for x in window)
        lowest = min(x["low"] for x in window)

        if highest == lowest:
            value = 50.0
        else:
            value = (
                (candles[i]["close"] - lowest)
                / (highest - lowest)
            ) * 100

        raw_values.append(value)

    if len(raw_values) < 3:
        return None, None

    k = sum(raw_values[-3:]) / 3

    # Smooth %D
    k_values = []

    for end in range(3, len(raw_values) + 1):
        k_values.append(
            sum(raw_values[end - 3:end]) / 3
        )

    if len(k_values) < 3:
        return k, k

    d = sum(k_values[-3:]) / 3

    return k, d


def adx(candles, period=14):
    if len(candles) < period * 2 + 1:
        return None

    tr_values = []
    plus_values = []
    minus_values = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        up = current["high"] - previous["high"]
        down = previous["low"] - current["low"]

        plus = up if up > down and up > 0 else 0
        minus = down if down > up and down > 0 else 0

        tr_values.append(tr)
        plus_values.append(plus)
        minus_values.append(minus)

    sm_tr = sum(tr_values[:period])
    sm_plus = sum(plus_values[:period])
    sm_minus = sum(minus_values[:period])

    dx_values = []

    def get_dx(tr, plus, minus):
        if tr <= 0:
            return 0

        pdi = 100 * plus / tr
        mdi = 100 * minus / tr

        if pdi + mdi == 0:
            return 0

        return 100 * abs(pdi - mdi) / (pdi + mdi)

    dx_values.append(get_dx(sm_tr, sm_plus, sm_minus))

    for i in range(period, len(tr_values)):
        sm_tr = sm_tr - sm_tr / period + tr_values[i]
        sm_plus = sm_plus - sm_plus / period + plus_values[i]
        sm_minus = sm_minus - sm_minus / period + minus_values[i]

        dx_values.append(
            get_dx(sm_tr, sm_plus, sm_minus)
        )

    if len(dx_values) < period:
        return None

    value = sum(dx_values[:period]) / period

    for item in dx_values[period:]:
        value = (
            value * (period - 1) + item
        ) / period

    return value


def heavy_lower_wick(candle):
    total = max(candle["high"] - candle["low"], 1e-12)
    body_bottom = min(candle["open"], candle["close"])
    wick = body_bottom - candle["low"]
    return wick / total >= 0.45


def heavy_upper_wick(candle):
    total = max(candle["high"] - candle["low"], 1e-12)
    body_top = max(candle["open"], candle["close"])
    wick = candle["high"] - body_top
    return wick / total >= 0.45


# =====================================================
# 4. M30 TREND FILTER + M10 DIRECTION FILTER
# =====================================================

def get_timeframe_direction(candles, timeframe_name):
    if len(candles) < 55:
        return None, f"{timeframe_name}: insufficient candles"

    closes = [c["close"] for c in candles]

    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    macd_value, macd_hist = macd(closes)
    adx_value = adx(candles)
    rsi_value = rsi(closes, 14)

    if (
        e9 is None
        or e21 is None
        or macd_value is None
        or adx_value is None
        or rsi_value is None
    ):
        return None, f"{timeframe_name}: indicators unavailable"

    last_price = closes[-1]

    if adx_value < MIN_ADX_HTF:
        return None, (
            f"{timeframe_name}: weak trend "
            f"ADX={adx_value:.1f}"
        )

    # CALL direction
    if (
        e9[-1] > e21[-1]
        and macd_hist > 0
        and last_price > e21[-1]
        and rsi_value > 50
    ):
        return "CALL", (
            f"{timeframe_name}=CALL "
            f"ADX={adx_value:.1f}"
        )

    # PUT direction
    if (
        e9[-1] < e21[-1]
        and macd_hist < 0
        and last_price < e21[-1]
        and rsi_value < 50
    ):
        return "PUT", (
            f"{timeframe_name}=PUT "
            f"ADX={adx_value:.1f}"
        )

    return None, (
        f"{timeframe_name}: mixed direction "
        f"RSI={rsi_value:.1f}"
    )


# =====================================================
# 5. M1 ENTRY CONFIRMATION
# =====================================================

def evaluate_m1(candles):
    if len(candles) < MIN_CANDLES:
        return None, "M1 insufficient candles"

    candles = candles[-80:]
    closes = [c["close"] for c in candles]

    e9 = ema(closes, 9)
    e21 = ema(closes, 21)

    rsi_value = rsi(closes, 14)
    macd_value, macd_hist = macd(closes)

    bb = bollinger(closes)
    sk, sd = stochastic(candles)
    adx_value = adx(candles)

    if any(x is None for x in (
        e9, e21, rsi_value, macd_value,
        macd_hist, bb, sk, sd, adx_value
    )):
        return None, "M1 indicators unavailable"

    last = candles[-1]
    previous = candles[-2]

    price = last["close"]
    upper, middle, lower = bb

    bb_span = max(upper - lower, 1e-12)
    bb_position = (price - lower) / bb_span

    call_score = 0
    put_score = 0

    # EMA
    if e9[-1] > e21[-1]:
        call_score += 2
    elif e9[-1] < e21[-1]:
        put_score += 2

    # MACD
    if macd_value > 0 and macd_hist > 0:
        call_score += 2
    elif macd_value < 0 and macd_hist < 0:
        put_score += 2

    # RSI
    if 50 < rsi_value <= 68:
        call_score += 2
    elif 32 <= rsi_value < 50:
        put_score += 2

    # Bollinger
    if price > middle and bb_position <= 0.78:
        call_score += 2
    elif price < middle and bb_position >= 0.22:
        put_score += 2

    # Stochastic
    if sk > sd and 25 <= sk <= 76:
        call_score += 2
    elif sk < sd and 24 <= sk <= 75:
        put_score += 2

    # ADX trend confirmation
    if adx_value >= MIN_ADX_M1:
        if e9[-1] > e21[-1]:
            call_score += 2
        elif e9[-1] < e21[-1]:
            put_score += 2

    diag = (
        f"M1 CALL={call_score} PUT={put_score} "
        f"RSI={rsi_value:.1f} ADX={adx_value:.1f} "
        f"STOCH={sk:.1f}/{sd:.1f}"
    )

    if adx_value < MIN_ADX_M1:
        return None, f"{diag} | ADX too weak"

    # Bouji final la dwe dakò ak direksyon an
    bullish = last["close"] > last["open"]
    bearish = last["close"] < last["open"]

    previous_bullish = previous["close"] > previous["open"]
    previous_bearish = previous["close"] < previous["open"]

    if (
        call_score >= MIN_M1_SCORE
        and call_score > put_score
    ):
        if (
            e9[-1] <= e21[-1]
            or macd_hist <= 0
            or not bullish
            or rsi_value > 68
            or sk > 78
            or bb_position > 0.78
            or heavy_upper_wick(last)
            or (previous_bearish and bearish)
        ):
            return None, f"{diag} | CALL candle guard"

        return {
            "direction": "CALL",
            "call_score": call_score,
            "put_score": put_score,
            "rsi": rsi_value,
            "adx": adx_value,
            "stoch": sk,
            "macd": macd_hist,
            "bb_position": bb_position,
            "price": price,
            "candle_time": last["time"]
        }, f"{diag} | CALL confirmed"

    if (
        put_score >= MIN_M1_SCORE
        and put_score > call_score
    ):
        if (
            e9[-1] >= e21[-1]
            or macd_hist >= 0
            or not bearish
            or rsi_value < 32
            or sk < 22
            or bb_position < 0.22
            or heavy_lower_wick(last)
            or (previous_bullish and bullish)
        ):
            return None, f"{diag} | PUT candle guard"

        return {
            "direction": "PUT",
            "call_score": call_score,
            "put_score": put_score,
            "rsi": rsi_value,
            "adx": adx_value,
            "stoch": sk,
            "macd": macd_hist,
            "bb_position": bb_position,
            "price": price,
            "candle_time": last["time"]
        }, f"{diag} | PUT confirmed"

    return None, f"{diag} | no strong setup"


# =====================================================
# 6. THREE-TIMEFRAME VALIDATION
# =====================================================

def validate_three_timeframes(symbol, m1_candles, htf_cache):
    """
    M30 ak M10 dwe dakò.
    M1 dwe bay menm direksyon an.
    """

    m30 = htf_cache[symbol].get("M30")
    m10 = htf_cache[symbol].get("M10")

    if not m30 or not m10:
        return None, "Higher timeframe data missing"

    m30_direction, m30_diag = get_timeframe_direction(
        m30, "M30"
    )

    m10_direction, m10_diag = get_timeframe_direction(
        m10, "M10"
    )

    if m30_direction is None:
        return None, m30_diag

    if m10_direction is None:
        return None, m10_diag

    if m30_direction != m10_direction:
        return None, (
            f"TIMEFRAME DISAGREEMENT: "
            f"M30={m30_direction}, M10={m10_direction}"
        )

    m1_signal, m1_diag = evaluate_m1(m1_candles)

    if m1_signal is None:
        return None, m1_diag

    if m1_signal["direction"] != m30_direction:
        return None, (
            f"M1 disagreement: M1={m1_signal['direction']} "
            f"M30/M10={m30_direction}"
        )

    m1_signal["m30"] = m30_direction
    m1_signal["m10"] = m10_direction

    return m1_signal, (
        f"{m30_diag} | {m10_diag} | {m1_diag}"
    )


# =====================================================
# 7. TELEGRAM MESSAGES
# =====================================================

def send_pre_alert(symbol, payout, direction):
    message = (
        f"🤖 {VERSION}\n\n"
        f"⚠️ PRE-ALERT — PREPARE\n\n"
        f"💱 PAIR: {symbol}\n"
        f"👁️ Direksyon preliminè: {direction}\n"
        f"📈 M30 + M10: dakò\n"
        f"💰 PAYOUT: {payout:.0f}%\n\n"
        f"⏳ Robo a poko valide bouji M1 la.\n"
        f"Pa antre anvan siyal final la."
    )
    send_telegram(message)


def format_final_signal(symbol, payout, signal, entry_price):
    direction = signal["direction"]
    emoji = "🟢" if direction == "CALL" else "🔴"

    return (
        f"🤖 {VERSION}\n\n"
        f"{emoji} {direction}\n"
        f"💱 PAIR: {symbol}\n"
        f"💰 PAYOUT: {payout:.0f}%\n\n"
        f"⏱️ TIMEFRAME: M1\n"
        f"📊 TREND: M30 = {signal['m30']}\n"
        f"📊 DIRECTION: M10 = {signal['m10']}\n"
        f"🎯 ENTRY: M1 = {direction}\n\n"
        f"⭐ M1 SCORE: "
        f"CALL {signal['call_score']} / "
        f"PUT {signal['put_score']}\n"
        f"📈 RSI: {signal['rsi']:.1f}\n"
        f"📉 ADX: {signal['adx']:.1f}\n"
        f"📊 STOCH: {signal['stoch']:.1f}\n"
        f"💹 MACD HIST: {signal['macd']:.6f}\n\n"
        f"🎯 ENTRY PRICE: {format_price(entry_price)}\n"
        f"🕒 SIGNAL SENT: {local_time()} UTC-5\n"
        f"⌛ EXPIRATION: 60 SECONDS\n"
        f"🛡️ 3 TIMEFRAMES CONFIRMED"
    )


# =====================================================
# 8. SYMBOLS AND INITIAL RANKING
# =====================================================

def get_eligible_symbols():
    data = api_get(
        "/v1/symbols",
        params={"venue": VENUE}
    )

    eligible = []
    seen = set()

    for item in data.get("symbols", []):
        symbol = (item.get("symbol") or "").strip()

        if not symbol or symbol in seen:
            continue

        if not is_clean_pair(symbol):
            continue

        if item.get("open") is False:
            continue

        try:
            payout = float(item.get("payout", 0))
        except (ValueError, TypeError):
            continue

        if payout < MIN_PAYOUT:
            continue

        seen.add(symbol)

        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    eligible.sort(
        key=lambda x: (-x["payout"], x["symbol"])
    )

    return eligible


def load_market_data(eligible):
    histories = {}
    htf_cache = {}
    ranked = []

    log(f"Loading {len(eligible)} eligible OTC pairs...")

    def fetch_market(item):
        symbol = item["symbol"]

        m1 = get_closed_candles(symbol, TF_ENTRY)
        m10 = get_closed_candles(symbol, TF_DIRECTION)
        m30 = get_closed_candles(symbol, TF_TREND)

        if (
            len(m1) < MIN_CANDLES
            or len(m10) < MIN_CANDLES
            or len(m30) < MIN_CANDLES
        ):
            return None

        direction30, _ = get_timeframe_direction(m30, "M30")
        direction10, _ = get_timeframe_direction(m10, "M10")

        if (
            direction30 is None
            or direction10 is None
            or direction30 != direction10
        ):
            return None

        return {
            "item": item,
            "m1": m1,
            "m10": m10,
            "m30": m30
        }

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(fetch_market, item)
            for item in eligible
        ]

        for future in as_completed(futures):
            try:
                result = future.result()

                if not result:
                    continue

                item = result["item"]
                symbol = item["symbol"]

                histories[symbol] = result["m1"]

                htf_cache[symbol] = {
                    "M10": result["m10"],
                    "M30": result["m30"],
                    "last_refresh": now_ts()
                }

                ranked.append(item)

            except Exception as exc:
                log(f"Market loading error: {exc}")

    ranked.sort(
        key=lambda x: (-x["payout"], x["symbol"])
    )

    return histories, htf_cache, ranked


# =====================================================
# 9. REFRESH HIGHER TIMEFRAMES
# =====================================================

def refresh_htf_if_needed(symbol, htf_cache):
    cached = htf_cache.get(symbol)

    if cached is None:
        return False

    if now_ts() - cached["last_refresh"] < HTF_REFRESH_SECONDS:
        return True

    try:
        m10 = get_closed_candles(symbol, TF_DIRECTION)
        m30 = get_closed_candles(symbol, TF_TREND)

        if len(m10) < MIN_CANDLES or len(m30) < MIN_CANDLES:
            return False

        cached["M10"] = m10
        cached["M30"] = m30
        cached["last_refresh"] = now_ts()

        return True

    except Exception as exc:
        log(f"{symbol}: HTF refresh error: {exc}")
        return False


# =====================================================
# 10. LIVE STREAM AND M1 CANDLE MANAGEMENT
# =====================================================

def run_live_stream(monitored, histories, htf_cache, deadline):

    symbols = [x["symbol"] for x in monitored]

    payout_map = {
        x["symbol"]: x["payout"]
        for x in monitored
    }

    response = api_get(
        "/v1/stream",
        params={
            "venue": VENUE,
            "symbol": ",".join(symbols)
        },
        stream=True
    )

    current_bars = {}
    pre_alerts = {}
    last_precheck = {}

    connected_symbols = set()
    event_name = ""

    active_trade = None
    signals_sent = 0
    last_prices = {}

    log("Live stream: " + ", ".join(symbols))

    try:
        for raw_line in response.iter_lines(
            decode_unicode=True,
            chunk_size=1
        ):

            current_time = now_ts()

            # -----------------------------------------
            # CHECK TRADE EXPIRATION
            # -----------------------------------------

            if active_trade is not None:
                if current_time >= active_trade["expiry_time"]:

                    symbol = active_trade["symbol"]
                    exit_price = last_prices.get(symbol)

                    if exit_price is not None:
                        entry_price = active_trade["entry_price"]

                        if active_trade["direction"] == "CALL":
                            is_win = exit_price > entry_price
                        else:
                            is_win = exit_price < entry_price

                        result = "WIN" if is_win else "LOSS"

                        send_telegram(
                            f"📊 ROMARIO PRO RESULT\n\n"
                            f"PAIR: {symbol}\n"
                            f"DIRECTION: {active_trade['direction']}\n"
                            f"RESULT: {result}\n"
                            f"ENTRY: {format_price(entry_price)}\n"
                            f"EXIT: {format_price(exit_price)}"
                        )

                        log(f"{symbol}: {result}")

                    else:
                        log(f"{symbol}: No exit tick; result unavailable.")

                    return "stop"

            if signals_sent >= MAX_SIGNALS_PER_RUN:
                return "stop"

            if active_trade is None and current_time >= deadline:
                log("Five-minute scan window ended.")
                return "stop"

            # -----------------------------------------
            # SSE PARSING
            # -----------------------------------------

            if not raw_line:
                event_name = ""
                continue

            line = (
                raw_line.decode("utf-8", errors="replace").strip()
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
                continue

            if event_name == "connected":
                connected_symbols = set(data.get("symbols", []))
                log("Connected: " + ", ".join(connected_symbols))
                continue

            if event_name != "tick":
                continue

            symbol = data.get("symbol")

            if symbol not in symbols:
                continue

            if connected_symbols and symbol not in connected_symbols:
                continue

            try:
                price = float(data["price"])
                feed_time = normalize_ts(data["time"])
            except (KeyError, TypeError, ValueError):
                continue

            last_prices[symbol] = price

            # -----------------------------------------
            # MANAGE ACTIVE TRADE
            # -----------------------------------------

            if active_trade is not None:
                continue

            bucket = (feed_time // TF_ENTRY) * TF_ENTRY

            # First tick for this symbol
            if symbol not in current_bars:
                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }
                continue

            current = current_bars[symbol]

            # -----------------------------------------
            # DETECT M1 CANDLE CLOSE
            # -----------------------------------------

            if bucket > current["time"]:

                closed_bar = {
                    "time": current["time"],
                    "open": current["open"],
                    "high": current["high"],
                    "low": current["low"],
                    "close": current["close"]
                }

                histories[symbol].append(closed_bar)
                histories[symbol] = histories[symbol][-CANDLE_LIMIT:]

                closed_time = closed_bar["time"]

                # Nouvelle bouji M1 lan kòmanse.
                # Nou verifye bouji ki fèk fèmen an.
                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

                log(
                    f"{symbol}: M1 candle closed. "
                    f"Checking M30 + M10 + M1..."
                )

                # Verifye si pre-alert la te fèt sou bouji sa a.
                staged = pre_alerts.get(symbol)

                # Refresh M10/M30 si sa nesesè
                if not refresh_htf_if_needed(symbol, htf_cache):
                    pre_alerts.pop(symbol, None)
                    log(f"{symbol}: HTF unavailable. No signal.")
                    continue

                # M1 se bouji ki fèk fèmen an
                signal, diag = validate_three_timeframes(
                    symbol,
                    histories[symbol],
                    htf_cache
                )

                # Evite voye menm bouji a de fwa
                pre_alerts.pop(symbol, None)

                if signal is None:
                    log(f"{symbol}: FINAL CONFIRMATION FAILED: {diag}")
                    continue

                if signal["candle_time"] != closed_time:
                    log(
                        f"{symbol}: Candle timestamp mismatch. "
                        f"Signal rejected."
                    )
                    continue

                log(f"{symbol}: ALL 3 TIMEFRAMES CONFIRMED: {diag}")

                # Entry price: premye tick pwochen bouji a
                entry_price = price

                message = format_final_signal(
                    symbol,
                    payout_map[symbol],
                    signal,
                    entry_price
                )

                if send_telegram(message):
                    signals_sent += 1

                    active_trade = {
                        "symbol": symbol,
                        "direction": signal["direction"],
                        "entry_price": entry_price,
                        "entry_time": now_ts(),
                        "expiry_time": now_ts() + EXPIRATION
                    }

                    log(
                        f"{symbol}: FINAL {signal['direction']} "
                        f"sent. Entry={format_price(entry_price)}"
                    )

                    return_trade = False

                continue

            # -----------------------------------------
            # UPDATE FORMING CANDLE
            # -----------------------------------------

            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price

            # -----------------------------------------
            # PRE-ALERT CHECK
            # -----------------------------------------

            seconds_left = (
                current["time"] + TF_ENTRY - feed_time
            )

            # Pa voye PRE-ALERT nan dènye 3 segonn yo
            if seconds_left <= 3 or seconds_left > 55:
                continue

            last_check = last_precheck.get(symbol, 0)

            if current_time - last_check < 3:
                continue

            last_precheck[symbol] = current_time

            if symbol in pre_alerts:
                if pre_alerts[symbol] == current["time"]:
                    continue

            if not refresh_htf_if_needed(symbol, htf_cache):
                continue

            # Istwa + bouji k ap fòme a pou PRE-ALERT sèlman
            live_window = (
                histories[symbol][- (CANDLE_LIMIT - 1):]
                + [{
                    "time": current["time"],
                    "open": current["open"],
                    "high": current["high"],
                    "low": current["low"],
                    "close": current["close"]
                }]
            )

            preliminary, diag = validate_three_timeframes(
                symbol,
                live_window,
                htf_cache
            )

            if preliminary is None:
                continue

            direction = preliminary["direction"]

            pre_alerts[symbol] = current["time"]

            send_pre_alert(
                symbol,
                payout_map[symbol],
                direction
            )

            log(
                f"{symbol}: PRE-ALERT {direction}. "
                f"Waiting for M1 candle close."
            )

    finally:
        response.close()

    return "reconnect"


# =====================================================
# 11. MAIN
# =====================================================

def main():

    log("=" * 60)
    log(f"{VERSION} STARTING")
    log("M30 TREND | M10 DIRECTION | M1 ENTRY")
    log("EXPIRATION 60 SECONDS | PAYOUT >= 92%")
    log("=" * 60)

    if not API_KEY or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Missing credentials.")
        sys.exit(1)

    deadline = now_ts() + MAX_WAIT_SECONDS

    try:
        eligible = get_eligible_symbols()
    except Exception as exc:
        log(f"Unable to get OTC symbols: {exc}")
        sys.exit(0)

    if not eligible:
        log("No open OTC pairs with payout >= 92%.")
        sys.exit(0)

    log(f"Eligible OTC pairs: {len(eligible)}")

    histories, htf_cache, ranked = load_market_data(eligible)

    if not ranked:
        log(
            "No pairs passed the M30 + M10 initial filters "
            "or candle history was insufficient."
        )
        sys.exit(0)

    # Chwazi 4 pi bon pè ki pase filtè M30/M10 yo
    monitored = ranked[:MAX_STREAM_PAIRS]

    log(
        "MONITORED PAIRS: "
        + ", ".join(x["symbol"] for x in monitored)
    )

    send_telegram(
        f"🤖 {VERSION} STARTED\n\n"
        f"📊 M30: TREND\n"
        f"📊 M10: DIRECTION\n"
        f"🎯 M1: ENTRY\n"
        f"💰 MIN PAYOUT: {MIN_PAYOUT:.0f}%\n"
        f"⌛ EXPIRATION: 60s\n"
        f"👁️ PAIRS: {len(monitored)}\n"
        f"🕒 TIME: {local_time()} UTC-5"
    )

    reconnects = 0

    while now_ts() < deadline:

        try:
            status = run_live_stream(
                monitored,
                histories,
                htf_cache,
                deadline
            )

            if status == "stop":
                log("One-run process completed.")
                return

            reconnects += 1

            if reconnects > MAX_RECONNECTS:
                break

            wait = min(5, max(1, deadline - now_ts()))

            log(f"Reconnecting in {wait:.1f}s...")
            time.sleep(wait)

        except Exception as exc:

            reconnects += 1

            log(f"Stream error: {exc}")

            if reconnects > MAX_RECONNECTS:
                break

            wait = min(5, max(1, deadline - now_ts()))
            time.sleep(wait)

    log(f"{VERSION} FINISHED")


if __name__ == "__main__":
    main()
