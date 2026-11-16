
import os
import json
import time
import logging
import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =========================================================
# ROMARIO TRADER PRO 11.8
# Pocket Option OTC | M1 | 60s expiration
# OTCharts Lite | Telegram alerts only
# =========================================================

VERSION = "ROMARIO TRADER PRO 11.8"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 60

CANDLE_LIMIT = 180
MIN_CANDLES = 80

MIN_PAYOUT = 92.0
MAX_LIVE_SYMBOLS = 5
MAX_HISTORY_REQUESTS_PER_RUN = 100

ADX_PERIOD = 14
ADX_MIN = 25
MIN_DI_GAP = 6

RSI_PERIOD = 7
STOCH_PERIOD = 14
MIN_SCORE = 4

ENTRY_LEAD_SECONDS = 10
MAX_SCAN_MINUTES = 300
MAX_RECONNECTS = 3

API_TIMEOUT = 30
STATE_FILE = "state.json"

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
    "User-Agent": "RomarioTraderPro/11.8",
    "Accept": "application/json"
})


# =========================================================
# GENERAL HELPERS
# =========================================================

def local_now():
    return datetime.now(TIMEZONE)


def log(message):
    logging.info(message)


# =========================================================
# API REQUEST WITH AUTOMATIC RETRIES
# =========================================================

def api_get(path, params=None, stream=False):

    url = BASE_URL + path

    attempts = 5 if not stream else 1

    for attempt in range(1, attempts + 1):

        response = None

        try:

            response = session.get(
                url,
                params=params,
                timeout=(15, None if stream else API_TIMEOUT),
                stream=stream
            )

            if response.status_code == 429:
                raise RuntimeError(
                    "OTCharts API rate limit reached (429)."
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
                        f"OTCharts temporary server error "
                        f"{response.status_code}. "
                        f"Retry {attempt}/{attempts} "
                        f"in {wait} seconds..."
                    )

                    response.close()
                    time.sleep(wait)
                    continue

                response.raise_for_status()

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
                    f"OTCharts connection failed after "
                    f"{attempts} attempts: {exc}"
                )

            wait = min(
                3 * (2 ** (attempt - 1)),
                30
            )

            log(
                f"Connection problem. "
                f"Retry {attempt}/{attempts} "
                f"in {wait} seconds..."
            )

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
            timeout=15
        )

        response.raise_for_status()

        return True

    except Exception as exc:

        log(f"Telegram error: {exc}")
        return False


# =========================================================
# ACCOUNT AND PLAN CHECK
# =========================================================

def check_account():

    usage = api_get("/v1/usage")

    plan = str(
        usage.get("planName", usage.get("plan", "Unknown"))
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
        f"requests remaining={remaining}/{quota} | "
        f"stream limit={stream_limit} | "
        f"instruments per stream={instruments_limit}"
    )

    if stream_limit < 1 or instruments_limit < 1:
        raise RuntimeError(
            "Kont OTCharts sa a pa gen live stream disponib."
        )

    venues = api_get("/v1/venues").get("venues", [])

    otc = next(
        (
            item for item in venues
            if item.get("id") == VENUE
        ),
        None
    )

    if not otc or otc.get("open") is not True:
        raise RuntimeError(
            "OTCharts pa konfime aksè OTC pou plan sa a."
        )

    timeframes = otc.get("timeframes", [])

    if TIMEFRAME not in timeframes:
        raise RuntimeError(
            "M1 timeframe pa disponib pou liv OTC sa a."
        )

    if remaining < 20:
        raise RuntimeError(
            "Pa gen ase demann API ki rete."
        )

    return {
        "remaining": remaining,
        "stream_limit": stream_limit,
        "instruments_limit": instruments_limit
    }


# =========================================================
# FOREX PAIR FILTER
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
# GET ALL ELIGIBLE OTC PAIRS
# =========================================================

def get_eligible_symbols():

    data = api_get(
        "/v1/symbols",
        params={"venue": VENUE}
    )

    symbols = data.get("symbols", [])

    eligible = []

    for item in symbols:

        symbol = item.get("symbol")
        payout = item.get("payout")

        if not is_forex_pair(symbol):
            continue

        if item.get("isOtc") is not True:
            continue

        if item.get("open") is not True:
            continue

        try:
            payout = float(payout)
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
        key=lambda item: (
            -item["payout"],
            item["symbol"]
        )
    )

    log(
        f"Forex OTC eligible pairs: {len(eligible)} "
        f"| Minimum payout: {MIN_PAYOUT}%"
    )

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

        log(
            f"{symbol}: rejected, "
            f"missing bars={gaps.get('missingBars')}"
        )

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

    parsed.sort(key=lambda item: item["time"])

    if len(parsed) < MIN_CANDLES:

        log(
            f"{symbol}: insufficient candles "
            f"({len(parsed)})"
        )

        return []

    for index in range(1, len(parsed)):

        difference = (
            parsed[index]["time"]
            - parsed[index - 1]["time"]
        )

        if difference != TIMEFRAME:

            log(
                f"{symbol}: candle time gap detected."
            )

            return []

    return parsed[-CANDLE_LIMIT:]


# =========================================================
# EMA
# =========================================================

def ema(values, period):

    if len(values) < period:
        return []

    multiplier = 2.0 / (period + 1)

    result = [
        sum(values[:period]) / period
    ]

    for value in values[period:]:

        result.append(
            (value - result[-1]) * multiplier
            + result[-1]
        )

    return [None] * (period - 1) + result


# =========================================================
# RSI
# =========================================================

def rsi(values, period=7):

    if len(values) <= period:
        return []

    gains = []
    losses = []

    for index in range(1, len(values)):

        change = values[index] - values[index - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = [None] * period

    for index in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1)
            + gains[index]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[index]
        ) / period

        if avg_loss == 0:
            value = 100.0
        else:
            rs = avg_gain / avg_loss
            value = 100 - (100 / (1 + rs))

        result.append(value)

    return result


# =========================================================
# STOCHASTIC
# =========================================================

def stochastic(candles, period=14):

    raw_k = []

    for index in range(len(candles)):

        if index < period - 1:
            raw_k.append(None)
            continue

        window = candles[
            index - period + 1:index + 1
        ]

        highest = max(
            item["high"] for item in window
        )

        lowest = min(
            item["low"] for item in window
        )

        close = candles[index]["close"]

        if highest == lowest:
            raw_k.append(50.0)
        else:
            raw_k.append(
                100 * (close - lowest)
                / (highest - lowest)
            )

    smooth_k = []

    for index in range(len(raw_k)):

        window = raw_k[
            max(0, index - 2):index + 1
        ]

        valid = [
            value for value in window
            if value is not None
        ]

        smooth_k.append(
            sum(valid) / len(valid)
            if len(valid) == 3
            else None
        )

    smooth_d = []

    for index in range(len(smooth_k)):

        window = smooth_k[
            max(0, index - 2):index + 1
        ]

        valid = [
            value for value in window
            if value is not None
        ]

        smooth_d.append(
            sum(valid) / len(valid)
            if len(valid) == 3
            else None
        )

    return smooth_k, smooth_d


# =========================================================
# ADX AND DIRECTIONAL INDICATORS
# =========================================================

def adx_values(candles, period=14):

    plus_dm = [0.0]
    minus_dm = [0.0]

    true_range = [
        candles[0]["high"] - candles[0]["low"]
    ]

    for index in range(1, len(candles)):

        current = candles[index]
        previous = candles[index - 1]

        up_move = (
            current["high"] - previous["high"]
        )

        down_move = (
            previous["low"] - current["low"]
        )

        plus_dm.append(
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus_dm.append(
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

        tr = max(
            current["high"] - current["low"],
            abs(
                current["high"] - previous["close"]
            ),
            abs(
                current["low"] - previous["close"]
            )
        )

        true_range.append(tr)

    dx_values = []
    plus_di_values = []
    minus_di_values = []

    for index in range(len(candles)):

        if index < period:

            dx_values.append(None)
            plus_di_values.append(None)
            minus_di_values.append(None)

            continue

        start = index - period + 1

        tr_sum = sum(
            true_range[start:index + 1]
        )

        plus_sum = sum(
            plus_dm[start:index + 1]
        )

        minus_sum = sum(
            minus_dm[start:index + 1]
        )

        if tr_sum == 0:

            plus_di = 0.0
            minus_di = 0.0

        else:

            plus_di = 100 * plus_sum / tr_sum
            minus_di = 100 * minus_sum / tr_sum

        denominator = plus_di + minus_di

        dx = (
            100 * abs(plus_di - minus_di)
            / denominator
            if denominator
            else 0.0
        )

        plus_di_values.append(plus_di)
        minus_di_values.append(minus_di)
        dx_values.append(dx)

    adx = []

    for index in range(len(dx_values)):

        if index < period * 2:

            adx.append(None)
            continue

        window = [
            value
            for value in dx_values[
                index - period + 1:index + 1
            ]
            if value is not None
        ]

        adx.append(
            sum(window) / len(window)
            if len(window) == period
            else None
        )

    return adx, plus_di_values, minus_di_values


# =========================================================
# SIGNAL STRATEGY
# =========================================================

def analyze_signal(candles):

    if len(candles) < MIN_CANDLES:
        return None

    closes = [
        item["close"] for item in candles
    ]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)

    rsi_data = rsi(closes, RSI_PERIOD)

    k_values, d_values = stochastic(
        candles,
        STOCH_PERIOD
    )

    adx_data, plus_di, minus_di = adx_values(
        candles,
        ADX_PERIOD
    )

    try:

        k = k_values[-1]
        d = d_values[-1]
        old_k = k_values[-2]

        current_rsi = rsi_data[-1]

        current_adx = adx_data[-1]
        current_plus = plus_di[-1]
        current_minus = minus_di[-1]

        fast = ema9[-1]
        medium = ema21[-1]
        slow = ema50[-1]

    except (IndexError, TypeError):
        return None

    values = [
        k, d, old_k, current_rsi,
        current_adx, current_plus,
        current_minus, fast, medium, slow
    ]

    if any(value is None for value in values):
        return None

    if current_adx < ADX_MIN:
        return None

    di_gap = abs(
        current_plus - current_minus
    )

    if di_gap < MIN_DI_GAP:
        return None

    last = candles[-1]

    candle_range = (
        last["high"] - last["low"]
    )

    if candle_range <= 0:
        return None

    body_ratio = (
        abs(last["close"] - last["open"])
        / candle_range
    )

    bullish = (
        k > d
        and k > old_k
        and 25 <= k <= 75
    )

    bearish = (
        k < d
        and k < old_k
        and 30 <= k <= 75
    )

    # STOCHASTIC IS MANDATORY.
    # RSI CANNOT OVERRIDE STOCHASTIC.

    if bullish:

        direction = "CALL"

        if current_plus <= current_minus:
            return None

        if fast <= medium:
            return None

    elif bearish:

        direction = "PUT"

        if current_minus <= current_plus:
            return None

        if fast >= medium:
            return None

    else:
        return None

    score = 1

    if direction == "CALL":

        if fast > medium > slow:
            score += 1

        if current_plus > current_minus:
            score += 1

        if current_rsi > 50:
            score += 1

        if last["close"] > last["open"]:
            score += 1

    else:

        if fast < medium < slow:
            score += 1

        if current_minus > current_plus:
            score += 1

        if current_rsi < 50:
            score += 1

        if last["close"] < last["open"]:
            score += 1

    if body_ratio >= 0.50:
        score += 1

    if score < MIN_SCORE:
        return None

    return {
        "direction": direction,
        "score": score,
        "k": round(k, 2),
        "d": round(d, 2),
        "rsi": round(current_rsi, 2),
        "adx": round(current_adx, 2)
    }


# =========================================================
# SEND SIGNAL
# =========================================================

def send_signal(symbol, payout, signal):

    now = local_now()

    entry = (
        now.replace(second=0, microsecond=0)
        + timedelta(minutes=1)
    )

    direction = signal["direction"]

    arrow = (
        "🟢" if direction == "CALL"
        else "🔴"
    )

    message = (
        f"🤖 {VERSION}\n\n"
        f"{arrow} {direction}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"💰 PAYOUT: {payout:.1f}%\n\n"
        f"⏰ SIGNAL: {now.strftime('%I:%M:%S %p')}\n"
        f"🎯 ENTRY: {entry.strftime('%I:%M:%S %p')}"
    )

    return send_telegram(message)


# =========================================================
# LIVE STREAM
# =========================================================

def run_live_stream(monitored, histories):

    symbols = [
        item["symbol"] for item in monitored
    ]

    params = {
        "venue": VENUE,
        "symbol": ",".join(symbols)
    }

    log(
        f"Opening live stream for "
        f"{len(symbols)} pairs."
    )

    response = api_get(
        "/v1/stream",
        params=params,
        stream=True
    )

    current_bars = {}
    signal_minute = {}

    pending = None
    connected_symbols = set()

    started_at = time.time()
    event_name = ""

    try:

        for raw_line in response.iter_lines(
            decode_unicode=True,
            chunk_size=1
        ):

            if (
                time.time() - started_at
                > MAX_SCAN_MINUTES * 60
            ):

                log("Maximum scan duration reached.")
                return

            if not raw_line:

                event_name = ""
                continue

            line = raw_line.strip()

            if line.startswith(":"):
                continue

            if line.startswith("event:"):

                event_name = line[6:].strip()
                continue

            if not line.startswith("data:"):
                continue

            raw_data = line[5:].strip()

            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                continue

            if event_name == "connected":

                connected_symbols = set(
                    data.get("symbols", [])
                )

                log(
                    "Stream connected: "
                    + ", ".join(
                        sorted(connected_symbols)
                    )
                )

                if not connected_symbols:

                    log(
                        "OTCharts did not confirm "
                        "any subscribed symbols."
                    )

                    return

                continue

            if event_name == "closed":

                log(
                    f"OTCharts closed event: {data}"
                )

                continue

            if event_name == "replaced":

                log(
                    "Stream replaced by another connection."
                )

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
                feed_time // 60
            ) * 60

            # INITIAL LIVE CANDLE

            if symbol not in current_bars:

                history = histories.get(symbol, [])

                history = [
                    candle
                    for candle in history
                    if candle["time"] < bucket
                ]

                histories[symbol] = history

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

            current = current_bars[symbol]

            # NEW MINUTE

            if bucket > current["time"]:

                gap = (
                    bucket - current["time"]
                )

                if gap != 60:

                    log(
                        f"{symbol}: live feed gap detected. "
                        "Skipping pair."
                    )

                    connected_symbols.discard(symbol)
                    continue

                histories[symbol].append(current)

                histories[symbol] = (
                    histories[symbol][-CANDLE_LIMIT:]
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

            # RESULT ESTIMATE

            if (
                pending
                and pending["symbol"] == symbol
            ):

                if (
                    pending["entry_price"] is None
                    and receive_time >= pending["entry_time"]
                ):

                    pending["entry_price"] = price

                    log(
                        f"Entry tick captured: "
                        f"{symbol} {price}"
                    )

                if (
                    pending["entry_price"] is not None
                    and receive_time >= pending["expiry_time"]
                ):

                    entry_price = pending["entry_price"]

                    if price == entry_price:

                        result = "TIE"

                    elif pending["direction"] == "CALL":

                        result = (
                            "WIN"
                            if price > entry_price
                            else "LOSS"
                        )

                    else:

                        result = (
                            "WIN"
                            if price < entry_price
                            else "LOSS"
                        )

                    message = (
                        f"🤖 {VERSION}\n\n"
                        f"📊 OTCHARTS RESULT ESTIMATE\n"
                        f"💱 PAIR: {symbol}\n"
                        f"📍 SIGNAL: {pending['direction']}\n"
                        f"📈 RESULT: {result}\n\n"
                        f"Entry: {entry_price}\n"
                        f"Exit: {price}\n\n"
                        f"⚠️ OTCharts estimate only. "
                        f"Not a verified Pocket Option result."
                    )

                    send_telegram(message)

                    log(
                        f"{symbol}: estimated result={result}"
                    )

                    return

            # SIGNAL CHECK

            if pending is not None:
                continue

            now = local_now()

            if now.second < 50:
                continue

            minute_key = int(
                receive_time // 60
            )

            if (
                signal_minute.get(symbol)
                == minute_key
            ):
                continue

            signal_minute[symbol] = minute_key

            candles_for_analysis = (
                histories[symbol]
                + [current_bars[symbol]]
            )

            if len(candles_for_analysis) < MIN_CANDLES:
                continue

            signal = analyze_signal(
                candles_for_analysis
            )

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

            log(
                f"SIGNAL {signal['direction']} "
                f"{symbol} | score={signal['score']}"
            )

            delivered = send_signal(
                symbol,
                pair_info["payout"],
                signal
            )

            if not delivered:

                log(
                    "Telegram signal delivery failed."
                )

                return

            entry_time = (
                int(receive_time // 60) * 60 + 60
            )

            pending = {
                "symbol": symbol,
                "direction": signal["direction"],
                "entry_time": entry_time,
                "expiry_time": entry_time + 60,
                "entry_price": None
            }

            log(
                "Signal sent. Waiting for entry "
                "and 60-second result estimate."
            )

    finally:

        response.close()


# =========================================================
# MAIN
# =========================================================

def main():

    log(f"{VERSION} starting...")

    if not API_KEY:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing."
        )

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Telegram secrets are missing."
        )

    account = check_account()

    eligible = get_eligible_symbols()

    if not eligible:

        log(
            "No Forex OTC pairs meet "
            "the payout requirement."
        )

        return

    if (
        len(eligible)
        > MAX_HISTORY_REQUESTS_PER_RUN
    ):

        raise RuntimeError(
            f"{len(eligible)} eligible pairs exceed "
            f"the safety limit "
            f"({MAX_HISTORY_REQUESTS_PER_RUN})."
        )

    estimated_requests = len(eligible) + 4

    if account["remaining"] < estimated_requests:

        raise RuntimeError(
            "Not enough OTCharts requests remaining "
            "to analyze all eligible pairs."
        )

    # ANALYZE ALL ELIGIBLE PAIRS

    histories = {}
    ranked = []

    log(
        f"Analyzing history for all "
        f"{len(eligible)} eligible Forex OTC pairs."
    )

    for item in eligible:

        symbol = item["symbol"]

        try:

            candles = get_candles(symbol)

            if not candles:
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

        log(
            "No pairs have sufficient clean "
            "candle history."
        )

        return

    # LITE PLAN: MAXIMUM FIVE LIVE PAIRS

    live_limit = min(
        MAX_LIVE_SYMBOLS,
        account["instruments_limit"]
    )

    monitored = ranked[:live_limit]

    log(
        f"All eligible pairs analyzed: {len(ranked)} "
        f"| Live monitored: {len(monitored)}"
    )

    log(
        "Live watchlist: "
        + ", ".join(
            item["symbol"] for item in monitored
        )
    )

    # ONE LIVE STREAM

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
                f"Stream connection error "
                f"({reconnects}/{MAX_RECONNECTS}): "
                f"{exc}"
            )

            if reconnects > MAX_RECONNECTS:
                break

            time.sleep(5)

        except Exception as exc:

            log(
                f"Bot stopped: {exc}"
            )

            break

    log(f"{VERSION} finished.")


if __name__ == "__main__":

    main()
