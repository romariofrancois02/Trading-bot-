import os
import sys
import json
import math
import time
import logging
import threading
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# =========================================================
# ROMARIO TRADER PRO 3.2 - SMART PAIR SELECTION
# OTCharts OTC | M1 | Expiration 60s | UTC-5
# =========================================================

VERSION = "ROMARIO TRADER PRO 3.2"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 60
EXPIRATION = 60
MIN_PAYOUT = 85.0

# Final confirmation 2-3 seconds before candle close
CONFIRM_WINDOW_MAX = 3
CONFIRM_WINDOW_MIN = 2

# One confirmed trade per GitHub Actions run
MAX_SIGNALS_PER_RUN = 1
MAX_WAIT_SECONDS = 300

# Analyze up to four pairs simultaneously
MAX_STREAM_PAIRS = 4

MIN_CONFIDENCE = 75.0
MIN_ADX = 20.0

CANDLE_LIMIT = 100
MIN_CANDLES = 55
MAX_RECONNECTS = 5

TOXIC_CURRENCIES = (
    "TRY", "ZAR", "MXN", "BRL", "PLN", "RUB",
    "INR", "CNH", "THB", "HUF", "SEK", "NOK"
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


class StreamRateLimitError(Exception):
    def __init__(self, message, retry_after=30):
        super().__init__(message)
        self.retry_after = retry_after


class StreamBadRequestError(Exception):
    pass


def log(message):
    logging.info(message)


def normalize_ts(ts):
    ts = int(ts)
    return ts // 1000 if ts > 10_000_000_000 else ts


def get_session():
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "RomarioProM1/3.2",
            "Accept": "application/json"
        })
        _thread_local.session = session
    return _thread_local.session


def format_price(value):
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def is_clean_pair(symbol):
    upper = symbol.upper()
    return not any(code in upper for code in TOXIC_CURRENCIES)


def compute_entry_timestamp(seconds_left):
    now = time.time()
    remaining = max(1, min(TIMEFRAME, int(round(seconds_left))))
    next_bar = ((int(now) // TIMEFRAME) + 1) * TIMEFRAME

    if abs(next_bar - (now + remaining)) <= 3:
        return int(next_bar)

    return int(round(now + remaining))


def api_get(path, params=None, stream=False):
    url = BASE_URL + path
    attempts = 1 if stream else 4
    backoffs = (0.8, 1.5, 2.5)

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

            if response.status_code == 400 and stream:
                body = response.text[:160]
                response.close()
                raise StreamBadRequestError(
                    f"400 Bad Request: {body}"
                )

            if response.status_code == 429:
                retry_header = response.headers.get("Retry-After", "30")
                try:
                    retry_after = max(25, int(float(retry_header)))
                except (TypeError, ValueError):
                    retry_after = 30
                response.close()
                raise StreamRateLimitError(
                    "OTCharts rate limit",
                    retry_after
                )

            if response.status_code in (502, 503, 504):
                status = response.status_code
                response.close()
                if attempt < attempts:
                    delay = backoffs[min(attempt - 1, 2)]
                    log(
                        f"HTTP {status} on {path}; retrying "
                        f"in {delay:.1f}s."
                    )
                    time.sleep(delay)
                    continue
                raise requests.HTTPError(f"HTTP {status}")

            response.raise_for_status()

            if stream:
                return response

            return response.json()

        except (StreamRateLimitError, StreamBadRequestError):
            raise
        except Exception:
            if response is not None:
                response.close()
            if attempt >= attempts:
                raise
            time.sleep(backoffs[min(attempt - 1, 2)])

    raise RuntimeError("API request failed.")


def send_telegram_fn(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

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
            log(
                f"Telegram HTTP {response.status_code} "
                f"(attempt {attempt})."
            )
        except Exception as exc:
            log(f"Telegram error (attempt {attempt}): {exc}")
        finally:
            if response is not None:
                response.close()

        time.sleep(0.4)

    return False


# =========================================================
# INDICATORS
# =========================================================

def calc_ema_aligned(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out

    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    out[period - 1] = ema

    for i in range(period, len(values)):
        ema = (values[i] - ema) * k + ema
        out[i] = ema

    return out


def calc_rsi_aligned(closes, period=14):
    n = len(closes)
    out = [None] * n

    if n <= period:
        return out

    gains = []
    losses = []

    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def to_rsi(gain, loss):
        if gain == 0 and loss == 0:
            return 50.0
        if loss == 0:
            return 100.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = to_rsi(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period
        out[i + 1] = to_rsi(avg_gain, avg_loss)

    return out


def calc_macd_aligned(closes, fast=12, slow=26, signal=9):
    n = len(closes)
    macd = [None] * n
    signal_line = [None] * n
    histogram = [None] * n

    ema_fast = calc_ema_aligned(closes, fast)
    ema_slow = calc_ema_aligned(closes, slow)

    indices = []
    values = []

    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd[i] = ema_fast[i] - ema_slow[i]
            indices.append(i)
            values.append(macd[i])

    if len(values) >= signal:
        signal_values = calc_ema_aligned(values, signal)
        for pos, candle_index in enumerate(indices):
            if signal_values[pos] is not None:
                signal_line[candle_index] = signal_values[pos]
                histogram[candle_index] = (
                    macd[candle_index] - signal_values[pos]
                )

    return macd, signal_line, histogram


def calc_bollinger_aligned(closes, period=20, multiplier=2.0):
    n = len(closes)
    upper = [None] * n
    middle = [None] * n
    lower = [None] * n

    for i in range(period - 1, n):
        values = closes[i - period + 1:i + 1]
        mean = sum(values) / period
        variance = sum((x - mean) ** 2 for x in values) / period
        std = math.sqrt(max(variance, 0.0))

        middle[i] = mean
        upper[i] = mean + multiplier * std
        lower[i] = mean - multiplier * std

    return upper, middle, lower


def calc_stochastic_aligned(candles, k_period=14, d_period=3, smooth_k=3):
    n = len(candles)
    raw_k = [None] * n
    k_line = [None] * n
    d_line = [None] * n

    for i in range(k_period - 1, n):
        subset = candles[i - k_period + 1:i + 1]
        highest = max(c["high"] for c in subset)
        lowest = min(c["low"] for c in subset)
        spread = highest - lowest

        raw_k[i] = (
            50.0 if spread <= 1e-12
            else (candles[i]["close"] - lowest) / spread * 100.0
        )

    valid_indices = [i for i, value in enumerate(raw_k) if value is not None]
    valid_values = [raw_k[i] for i in valid_indices]

    for pos in range(smooth_k - 1, len(valid_values)):
        index = valid_indices[pos]
        k_line[index] = sum(
            valid_values[pos - smooth_k + 1:pos + 1]
        ) / smooth_k

    valid_d_indices = [i for i, value in enumerate(k_line) if value is not None]
    valid_d_values = [k_line[i] for i in valid_d_indices]

    for pos in range(d_period - 1, len(valid_d_values)):
        index = valid_d_indices[pos]
        d_line[index] = sum(
            valid_d_values[pos - d_period + 1:pos + 1]
        ) / d_period

    return k_line, d_line


def calc_adx_aligned(candles, period=14):
    n = len(candles)
    output = [None] * n

    if n <= period * 2:
        return output

    tr_values = []
    plus_dm_values = []
    minus_dm_values = []

    for i in range(1, n):
        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

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

        tr_values.append(tr)
        plus_dm_values.append(plus_dm)
        minus_dm_values.append(minus_dm)

    sm_tr = sum(tr_values[:period])
    sm_plus = sum(plus_dm_values[:period])
    sm_minus = sum(minus_dm_values[:period])

    def dx_value(tr, plus, minus):
        if tr <= 1e-12:
            return 0.0
        plus_di = 100.0 * plus / tr
        minus_di = 100.0 * minus / tr
        total = plus_di + minus_di
        if total <= 1e-12:
            return 0.0
        return 100.0 * abs(plus_di - minus_di) / total

    dx_values = [dx_value(sm_tr, sm_plus, sm_minus)]
    dx_indices = [period]

    for i in range(period, len(tr_values)):
        sm_tr = sm_tr - sm_tr / period + tr_values[i]
        sm_plus = sm_plus - sm_plus / period + plus_dm_values[i]
        sm_minus = sm_minus - sm_minus / period + minus_dm_values[i]

        dx_values.append(dx_value(sm_tr, sm_plus, sm_minus))
        dx_indices.append(i + 1)

    if len(dx_values) >= period:
        adx = sum(dx_values[:period]) / period
        output[dx_indices[period - 1]] = adx

        for pos in range(period, len(dx_values)):
            adx = (
                adx * (period - 1) + dx_values[pos]
            ) / period
            output[dx_indices[pos]] = adx

    return output


# =========================================================
# SIGNAL EVALUATION
# =========================================================

def evaluate_six_indicators(candles):
    if len(candles) < MIN_CANDLES:
        return None, "not enough candles"

    window = candles[-80:]
    closes = [c["close"] for c in window]
    price = window[-1]["close"]

    ema9 = calc_ema_aligned(closes, 9)
    ema21 = calc_ema_aligned(closes, 21)
    rsi = calc_rsi_aligned(closes, 14)
    macd, macd_signal, macd_hist = calc_macd_aligned(closes)
    bb_upper, bb_middle, bb_lower = calc_bollinger_aligned(closes)
    stoch_k, stoch_d = calc_stochastic_aligned(window)
    adx = calc_adx_aligned(window, 14)

    latest = (
        ema9[-1], ema21[-1], rsi[-1],
        macd[-1], macd_signal[-1], macd_hist[-1],
        bb_upper[-1], bb_middle[-1], bb_lower[-1],
        stoch_k[-1], stoch_d[-1], adx[-1]
    )

    if any(value is None for value in latest):
        return None, "indicators unavailable"

    call_score = 0
    put_score = 0

    # EMA 9 / 21
    if ema9[-1] > ema21[-1]:
        call_score += 2
    elif ema9[-1] < ema21[-1]:
        put_score += 2

    # MACD
    if macd[-1] > macd_signal[-1] and macd_hist[-1] >= 0:
        call_score += 2
    elif macd[-1] < macd_signal[-1] and macd_hist[-1] <= 0:
        put_score += 2

    # RSI
    rsi_value = rsi[-1]
    if 50 < rsi_value < 75:
        call_score += 2
    elif 25 < rsi_value < 50:
        put_score += 2

    # Bollinger Bands
    if price >= bb_middle[-1] and price < bb_upper[-1]:
        call_score += 2
    elif price <= bb_middle[-1] and price > bb_lower[-1]:
        put_score += 2

    # Stochastic
    k_value = stoch_k[-1]
    d_value = stoch_d[-1]

    if k_value > d_value and 20 <= k_value <= 85:
        call_score += 2
    elif k_value < d_value and 15 <= k_value <= 80:
        put_score += 2

    # ADX trend-strength vote
    adx_value = adx[-1]
    if adx_value > MIN_ADX:
        if ema9[-1] >= ema21[-1]:
            call_score += 2
        else:
            put_score += 2

    total_votes = call_score + put_score

    diagnostics = (
        f"CALL={call_score} PUT={put_score} "
        f"ADX={adx_value:.2f} RSI={rsi_value:.2f} "
        f"STOCH={k_value:.2f}"
    )

    if adx_value <= MIN_ADX:
        return None, f"{diagnostics} ADX_TOO_LOW"

    if total_votes == 0:
        return None, f"{diagnostics} NO_VOTES"

    call_agreement = call_score / total_votes * 100.0
    put_agreement = put_score / total_votes * 100.0

    if call_agreement >= MIN_CONFIDENCE and call_agreement >= put_agreement:
        direction = "CALL"
        agreement = int(round(call_agreement))
    elif put_agreement >= MIN_CONFIDENCE and put_agreement > call_agreement:
        direction = "PUT"
        agreement = int(round(put_agreement))
    else:
        return None, f"{diagnostics} AGREEMENT_TOO_LOW"

    label = "STRONG AGREEMENT" if agreement >= 80 else "MODERATE AGREEMENT"

    signal = {
        "direction": direction,
        "score_call": call_score,
        "score_put": put_score,
        "agreement": agreement,
        "adx": adx_value,
        "confidence": label,
        "ref_quote": price,
        "candle_time": window[-1]["time"]
    }

    return signal, f"{diagnostics} {direction}_CANDIDATE={agreement}%"


def score_setup_quality(candles):
    signal, _ = evaluate_six_indicators(candles)

    if signal:
        return 100.0 + float(signal["agreement"])

    if len(candles) < MIN_CANDLES:
        return 0.0

    window = candles[-80:]
    closes = [c["close"] for c in window]

    ema9 = calc_ema_aligned(closes, 9)
    ema21 = calc_ema_aligned(closes, 21)
    adx = calc_adx_aligned(window, 14)

    quality = 0.0

    if ema9[-1] is not None and ema21[-1] is not None:
        if ema9[-1] != ema21[-1]:
            quality += 25.0

    if adx[-1] is not None and adx[-1] > MIN_ADX:
        quality += min(35.0, adx[-1])

    return round(quality, 2)


# =========================================================
# TELEGRAM MESSAGE FORMATS
# =========================================================

def format_prealert_message(pair, direction, entry_time, agreement):
    emoji = "🟢" if direction == "CALL" else "🔴"

    return (
        f"👀 ROMARIO PRO 3.2 - PRE-ALERT\n\n"
        f"💱 SELECTED PAIR: {pair}\n"
        f"{emoji} POSSIBLE DIRECTION: {direction}\n"
        f"📊 INDICATOR AGREEMENT: {agreement}%\n"
        f"🕒 EXPECTED ENTRY: {entry_time}\n\n"
        f"⏳ WAITING FOR FINAL CONFIRMATION\n"
        f"⚠️ NOT A SIGNAL. DO NOT ENTER YET."
    )


def format_signal_message(pair, payout, entry_time, signal, entry_quote):
    emoji = "🟢" if signal["direction"] == "CALL" else "🔴"

    return (
        f"🤖 {VERSION}\n\n"
        f"{emoji} {signal['direction']} | {pair} ({payout:.0f}%)\n"
        f"📊 INDICATOR AGREEMENT: {signal['agreement']}%\n"
        f"📈 ADX: {signal['adx']:.1f}\n"
        f"🎯 REFERENCE PRICE: {format_price(entry_quote)}\n"
        f"🕒 ENTRY: {entry_time} (UTC-5)\n"
        f"⌛ EXPIRATION: 60 SECONDS\n"
        f"🛡️ {signal['confidence']}\n\n"
        f"⚠️ Indicator agreement is not a guaranteed win probability."
    )


def send_cancel_message(pair, direction, reason):
    send_telegram_fn(
        f"❌ PRE-ALERT CANCELLED\n\n"
        f"PAIR: {pair}\n"
        f"DIRECTION: {direction}\n"
        f"REASON: {reason}\n\n"
        f"Bot will continue scanning the other pairs."
    )


# =========================================================
# SYMBOLS AND HISTORICAL CANDLES
# =========================================================

def get_eligible_symbols():
    data = api_get("/v1/symbols", params={"venue": VENUE})

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
            payout = float(item.get("payout", 0.0))
        except (TypeError, ValueError):
            payout = 0.0

        if payout < MIN_PAYOUT:
            continue

        seen.add(symbol)
        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    eligible.sort(key=lambda item: (-item["payout"], item["symbol"]))
    return eligible


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
                "time": normalize_ts(candle["time"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"])
            })
        except (KeyError, TypeError, ValueError):
            continue

    parsed.sort(key=lambda candle: candle["time"])

    if len(parsed) < MIN_CANDLES:
        return []

    return parsed[-CANDLE_LIMIT:]


def download_all_histories(eligible):
    histories = {}
    ranked = []

    log(
        f"Scanning {len(eligible)} eligible OTC pairs "
        f"with payout >= {MIN_PAYOUT:.0f}%."
    )

    def fetch_and_score(item):
        candles = get_candles(item["symbol"])
        score = score_setup_quality(candles) if candles else 0.0

        enriched = dict(item)
        enriched["setup_score"] = score

        return enriched, candles

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_and_score, item): item
            for item in eligible
        }

        for future in as_completed(futures):
            try:
                item, candles = future.result()

                if candles:
                    histories[item["symbol"]] = candles
                    ranked.append(item)

            except Exception as exc:
                log(f"History fetch failed: {exc}")

    ranked.sort(
        key=lambda item: (
            -item["setup_score"],
            -item["payout"],
            item["symbol"]
        )
    )

    return histories, ranked


def build_live_window(symbol, histories, current_bar):
    history = histories.get(symbol, [])

    if not history or current_bar is None:
        return []

    live_bar = {
        "time": current_bar["time"],
        "open": current_bar["open"],
        "high": current_bar["high"],
        "low": current_bar["low"],
        "close": current_bar["close"]
    }

    return history[-(CANDLE_LIMIT - 1):] + [live_bar]


# =========================================================
# TRADE RESULT CHECK
# =========================================================

def finalize_trade(trade):
    entry = trade.get("entry_price")
    exit_price = trade.get("last_price")

    if entry is None or exit_price is None:
        log(
            f"{trade['symbol']}: Cannot calculate result; "
            "entry or exit tick missing."
        )
        send_telegram_fn(
            f"⚠️ TRADE RESULT UNAVAILABLE\n"
            f"PAIR: {trade['symbol']}\n"
            f"Reason: Missing entry or exit tick."
        )
        return

    if trade["direction"] == "CALL":
        is_win = exit_price > entry
    else:
        is_win = exit_price < entry

    result = "WIN" if is_win else "LOSS"

    message = (
        f"{'✅' if is_win else '❌'} TRADE RESULT: {result}\n\n"
        f"PAIR: {trade['symbol']}\n"
        f"DIRECTION: {trade['direction']}\n"
        f"ENTRY: {format_price(entry)}\n"
        f"EXIT: {format_price(exit_price)}\n"
        f"EXPIRATION: 60s"
    )

    send_telegram_fn(message)
    log(
        f"{trade['symbol']}: Result {result}; "
        f"{format_price(entry)} -> {format_price(exit_price)}."
    )


# =========================================================
# LIVE STREAM
# =========================================================

def run_live_stream(monitored, histories, current_bars, deadline):
    symbols = [item["symbol"] for item in monitored]

    response = api_get(
        "/v1/stream",
        params={
            "venue": VENUE,
            "symbol": ",".join(symbols)
        },
        stream=True
    )

    log("Stream opened for: " + ", ".join(symbols))

    connected_symbols = set()
    last_prices = {}

    signals_sent = 0
    pending_order = None
    active_trade = None
    last_analysis_time = {}

    event_name = ""

    try:
        for raw_line in response.iter_lines(
            decode_unicode=True,
            chunk_size=1
        ):
            now = time.time()

            # Manage active trade before processing new ticks
            if active_trade is not None:
                if (
                    active_trade["entry_price"] is None
                    and now >= active_trade["entry_time"]
                ):
                    current_price = last_prices.get(active_trade["symbol"])
                    if current_price is not None:
                        active_trade["entry_price"] = current_price
                        log(
                            f"{active_trade['symbol']}: Entry price "
                            f"{format_price(current_price)}"
                        )

                if now >= active_trade["expiry_time"]:
                    final_price = last_prices.get(active_trade["symbol"])
                    if final_price is not None:
                        active_trade["last_price"] = final_price

                    finalize_trade(active_trade)
                    return "stop"

            if signals_sent >= MAX_SIGNALS_PER_RUN and active_trade is None:
                return "stop"

            if now >= deadline and active_trade is None:
                log("Maximum scan time reached.")
                return "stop"

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
                log("Connected symbols: " + ", ".join(sorted(connected_symbols)))
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
            now = time.time()

            # If a trade is active, only track its pair for result calculation
            if active_trade is not None:
                if symbol == active_trade["symbol"]:
                    active_trade["last_price"] = price

                    if (
                        active_trade["entry_price"] is None
                        and now >= active_trade["entry_time"]
                    ):
                        active_trade["entry_price"] = price
                        log(
                            f"{symbol}: Entry tick captured at "
                            f"{format_price(price)}."
                        )
                continue

            bucket = (feed_time // TIMEFRAME) * TIMEFRAME

            # Initialize live candle
            if symbol not in current_bars:
                histories[symbol] = [
                    candle for candle in histories.get(symbol, [])
                    if candle["time"] < bucket
                ]

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }
                continue

            current = current_bars[symbol]

            # Candle has changed
            if bucket > current["time"]:

                # Cancel a pending candidate if its candle closed
                if (
                    pending_order is not None
                    and pending_order["symbol"] == symbol
                    and pending_order["bar_time"] == current["time"]
                ):
                    old = pending_order
                    pending_order = None

                    log(
                        f"{symbol}: PRE-ALERT expired at candle close."
                    )

                    send_cancel_message(
                        symbol,
                        old["direction"],
                        "Candle closed before final confirmation."
                    )

                closed_candle = {
                    "time": current["time"],
                    "open": current["open"],
                    "high": current["high"],
                    "low": current["low"],
                    "close": current["close"]
                }

                histories.setdefault(symbol, []).append(closed_candle)
                histories[symbol] = histories[symbol][-CANDLE_LIMIT:]

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

                current = current_bars[symbol]

            else:
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price

            seconds_left = (current["time"] + TIMEFRAME) - feed_time

            # -------------------------------------------------
            # STEP 1: PRE-ALERT
            # Only one pending pair at a time.
            # If it fails, the other pairs remain eligible.
            # -------------------------------------------------

            if (
                pending_order is None
                and seconds_left > CONFIRM_WINDOW_MAX
            ):
                last_eval = last_analysis_time.get(symbol, 0.0)

                if now - last_eval >= 3.0:
                    last_analysis_time[symbol] = now

                    window = build_live_window(
                        symbol,
                        histories,
                        current
                    )

                    if len(window) >= MIN_CANDLES:
                        signal, diagnostics = evaluate_six_indicators(window)

                        if signal is not None:
                            entry_ts = compute_entry_timestamp(seconds_left)
                            entry_time_str = datetime.fromtimestamp(
                                entry_ts,
                                tz=USER_TZ
                            ).strftime("%I:%M:%S %p")

                            prealert = format_prealert_message(
                                symbol,
                                signal["direction"],
                                entry_time_str,
                                signal["agreement"]
                            )

                            # Lock candidate only if Telegram accepted PRE-ALERT
                            if send_telegram_fn(prealert):
                                pending_order = {
                                    "symbol": symbol,
                                    "bar_time": current["time"],
                                    "direction": signal["direction"],
                                    "agreement": signal["agreement"],
                                    "entry_ts": entry_ts,
                                    "entry_time_str": entry_time_str,
                                    "prealert_sent": True
                                }

                                log(
                                    f"{symbol}: PRE-ALERT sent; "
                                    f"{signal['direction']} "
                                    f"agreement={signal['agreement']}%. "
                                    f"Waiting for final confirmation."
                                )
                            else:
                                log(
                                    f"{symbol}: PRE-ALERT delivery failed; "
                                    "candidate not locked."
                                )

            # -------------------------------------------------
            # STEP 2: FINAL CONFIRMATION 2-3 SECONDS BEFORE CLOSE
            # -------------------------------------------------

            if (
                CONFIRM_WINDOW_MIN <= seconds_left <= CONFIRM_WINDOW_MAX
                and not current.get("final_verified", False)
            ):
                current["final_verified"] = True

                # Only verify the currently selected pending pair
                if (
                    pending_order is not None
                    and pending_order["symbol"] == symbol
                    and pending_order["bar_time"] == current["time"]
                ):
                    window = build_live_window(
                        symbol,
                        histories,
                        current
                    )

                    signal, diagnostics = (
                        evaluate_six_indicators(window)
                        if len(window) >= MIN_CANDLES
                        else (None, "not enough candles")
                    )

                    same_direction = (
                        signal is not None
                        and signal["direction"] == pending_order["direction"]
                    )

                    if same_direction and pending_order["prealert_sent"]:
                        pair_info = next(
                            (
                                item for item in monitored
                                if item["symbol"] == symbol
                            ),
                            {"payout": MIN_PAYOUT}
                        )

                        payout = pair_info["payout"]

                        # Recalculate the next candle entry time
                        entry_ts = compute_entry_timestamp(seconds_left)
                        entry_time_str = datetime.fromtimestamp(
                            entry_ts,
                            tz=USER_TZ
                        ).strftime("%I:%M:%S %p")

                        final_message = format_signal_message(
                            symbol,
                            payout,
                            entry_time_str,
                            signal,
                            entry_quote=price
                        )

                        if send_telegram_fn(final_message):
                            signals_sent += 1

                            active_trade = {
                                "symbol": symbol,
                                "payout": payout,
                                "direction": signal["direction"],
                                "entry_time": entry_ts,
                                "expiry_time": entry_ts + EXPIRATION,
                                "entry_price": None,
                                "last_price": price
                            }

                            pending_order = None

                            log(
                                f"{symbol}: FINAL CONFIRMATION PASSED. "
                                f"{signal['direction']} "
                                f"agreement={signal['agreement']}%. "
                                f"Entry={entry_time_str}."
                            )
                        else:
                            log(
                                f"{symbol}: Final signal Telegram "
                                "delivery failed. No trade registered."
                            )

                            old = pending_order
                            pending_order = None
                            send_cancel_message(
                                symbol,
                                old["direction"],
                                "Final signal could not be delivered."
                            )

                    else:
                        old = pending_order
                        pending_order = None

                        log(
                            f"{symbol}: FINAL CONFIRMATION FAILED. "
                            f"{diagnostics}"
                        )

                        send_cancel_message(
                            symbol,
                            old["direction"],
                            "Final indicators did not confirm the same direction."
                        )

    finally:
        response.close()

    return "stop" if signals_sent >= MAX_SIGNALS_PER_RUN else "reconnect"


# =========================================================
# STARTUP / RETRIES
# =========================================================

def fetch_eligible_with_retry(deadline):
    while True:
        try:
            return get_eligible_symbols()
        except Exception as exc:
            remaining = deadline - time.time()

            if remaining <= 3:
                raise

            wait_seconds = min(4.0, max(1.0, remaining - 1.0))
            log(
                f"Symbols request failed: {exc}. "
                f"Retrying in {wait_seconds:.1f}s."
            )
            time.sleep(wait_seconds)


def main():
    log(f"{VERSION} starting.")

    if not API_KEY or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log(
            "Missing credentials. Set OTCHARTS_API_KEY, "
            "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID."
        )
        sys.exit(1)

    deadline = time.time() + MAX_WAIT_SECONDS

    try:
        eligible = fetch_eligible_with_retry(deadline)
    except Exception as exc:
        log(f"Unable to load eligible symbols: {exc}")
        sys.exit(1)

    if not eligible:
        log(f"No open clean OTC pairs with payout >= {MIN_PAYOUT:.0f}%.")
        sys.exit(0)

    histories, ranked = download_all_histories(eligible)

    if not ranked:
        log("No sufficient candle history available.")
        sys.exit(0)

    active_limit = min(MAX_STREAM_PAIRS, len(ranked))
    monitored = ranked[:active_limit]

    log(
        "Selected stream pairs: "
        + ", ".join(
            f"{item['symbol']}(setup={item['setup_score']}, "
            f"payout={item['payout']:.0f}%)"
            for item in monitored
        )
    )

    log(
        "Bot will monitor all selected pairs. "
        "Only one PRE-ALERT can be active at a time."
    )

    current_bars = {}
    reconnects = 0

    while time.time() < deadline:
        try:
            status = run_live_stream(
                monitored,
                histories,
                current_bars,
                deadline
            )

            if status == "stop":
                log(f"{VERSION} finished.")
                return

            reconnects += 1

            if reconnects > MAX_RECONNECTS:
                log("Maximum reconnect attempts reached.")
                break

            remaining = deadline - time.time()
            wait_seconds = min(7.0, max(2.0, remaining))

            log(f"Reconnecting in {wait_seconds:.1f}s.")
            time.sleep(wait_seconds)

        except StreamBadRequestError as exc:
            log(f"Stream bad request: {exc}")

            if active_limit > 1:
                active_limit -= 1
                monitored = ranked[:active_limit]
                log(
                    f"Reducing stream pairs to {active_limit} "
                    "and retrying."
                )
                time.sleep(1.5)
                continue

            break

        except StreamRateLimitError as exc:
            reconnects += 1

            if reconnects > MAX_RECONNECTS:
                break

            remaining = deadline - time.time()
            wait_seconds = min(exc.retry_after, max(0.0, remaining))

            if wait_seconds <= 2:
                break

            log(f"Rate limited. Waiting {wait_seconds:.1f}s.")
            time.sleep(wait_seconds)

        except Exception as exc:
            reconnects += 1
            log(f"Stream error: {exc}")

            if reconnects > MAX_RECONNECTS:
                break

            remaining = deadline - time.time()
            if remaining <= 1:
                break

            wait_seconds = min(7.0, max(2.0, remaining))
            time.sleep(wait_seconds)

    log(f"{VERSION} finished: scan window ended.")


if __name__ == "__main__":
    main()
