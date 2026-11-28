
import os
import re
import json
import time
import logging
import requests

from datetime import datetime, timezone, timedelta
from collections import deque

# =========================================================
# ROMARIO PRO M1 ONLY
# M1 ANALYSIS | 60S EXPIRATION | STRICT CONFIRMATION
# =========================================================

VERSION = "ROMARIO PRO M1 ONLY"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAME = 60
EXPIRATION = 60

MIN_PAYOUT = 92.0
MAX_MARKETS = 4
CANDLE_LIMIT = 120
MIN_CANDLES = 60

MIN_ADX = 22
MIN_SCORE = 7

MAX_WAIT_SECONDS = 300

# Florida / New York standard-time offset
LOCAL_TZ = timezone(timedelta(hours=-5))

TOXIC_CURRENCIES = {
    "TRY", "ZAR", "MXN", "BRL", "PLN",
    "RUB", "INR", "CNH", "THB", "HUF",
    "SEK", "NOK"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "RomarioProM1/3.0"
})


# =========================================================
# BASIC VALIDATION
# =========================================================

def validate_environment():
    missing = []

    if not API_KEY:
        missing.append("OTCHARTS_API_KEY")

    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise RuntimeError(
            "Missing GitHub Secrets: " + ", ".join(missing)
        )


def local_time(timestamp=None):
    if timestamp is None:
        timestamp = time.time()

    return datetime.fromtimestamp(
        timestamp, tz=LOCAL_TZ
    ).strftime("%Y-%m-%d %H:%M:%S")


def normalize_timestamp(value):
    if value is None:
        return None

    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            try:
                dt = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )
                return dt.timestamp()
            except Exception:
                return None

    value = float(value)

    # Convert milliseconds to seconds
    if value > 100000000000:
        value /= 1000

    return value


def normalize_candle(raw):
    try:
        candle = {
            "time": normalize_timestamp(
                raw.get("time", raw.get("timestamp"))
            ),
            "open": float(raw.get("open", raw.get("o"))),
            "high": float(raw.get("high", raw.get("h"))),
            "low": float(raw.get("low", raw.get("l"))),
            "close": float(raw.get("close", raw.get("c")))
        }

        if candle["time"] is None:
            return None

        if candle["high"] < candle["low"]:
            return None

        return candle

    except (TypeError, ValueError):
        return None


# =========================================================
# OTCHARTS API
# =========================================================

def api_get(endpoint, params=None, timeout=20):
    url = BASE_URL + endpoint

    response = session.get(
        url,
        params=params,
        timeout=timeout
    )

    response.raise_for_status()
    return response.json()


def get_eligible_markets():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    symbols = data.get("symbols", [])

    eligible = []

    for item in symbols:
        symbol = item.get("symbol")

        if not symbol:
            continue

        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        if item.get("open") is False:
            continue

        try:
            payout = float(item.get("payout", 0))
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        currencies = symbol.replace("_otc", "")

        if any(
            currencies[i:i + 3] in TOXIC_CURRENCIES
            for i in (0, 3)
        ):
            continue

        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    eligible.sort(
        key=lambda item: item["payout"],
        reverse=True
    )

    selected = eligible[:MAX_MARKETS]

    logging.info(
        "Eligible OTC pairs with payout >= %.1f%%: %s",
        MIN_PAYOUT,
        len(eligible)
    )

    logging.info(
        "Selected markets: %s",
        [item["symbol"] for item in selected]
    )

    return selected


def get_closed_candles(symbol):
    data = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    raw_candles = data.get(
        "candles",
        data.get("data", [])
    )

    now = time.time()
    candles = []

    for raw in raw_candles:
        candle = normalize_candle(raw)

        if candle is None:
            continue

        # Candle timestamps are treated as opening timestamps.
        # Only use candles that have fully closed.
        if candle["time"] + TIMEFRAME <= now:
            candles.append(candle)

    candles.sort(key=lambda x: x["time"])

    # Remove duplicate timestamps
    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    return list(unique.values())[-CANDLE_LIMIT:]


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


def calculate_rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

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


def calculate_macd(values):
    if len(values) < 40:
        return None

    fast_values = []
    slow_values = []

    for i in range(25, len(values) + 1):
        segment = values[:i]

        fast = ema(segment, 12)
        slow = ema(segment, 26)

        if fast is not None and slow is not None:
            fast_values.append(fast - slow)

    if len(fast_values) < 9:
        return None

    signal = ema(fast_values, 9)

    if signal is None:
        return None

    return fast_values[-1] - signal


def calculate_bollinger(values, period=20):
    if len(values) < period:
        return None

    window = values[-period:]
    middle = sum(window) / period

    variance = sum(
        (value - middle) ** 2
        for value in window
    ) / period

    deviation = variance ** 0.5

    return (
        middle,
        middle + 2 * deviation,
        middle - 2 * deviation
    )


def calculate_stochastic(candles, period=14):
    if len(candles) < period + 3:
        return None

    k_values = []

    for end in range(len(candles) - 3, len(candles)):
        window = candles[end - period + 1:end + 1]

        highest = max(c["high"] for c in window)
        lowest = min(c["low"] for c in window)
        close = candles[end]["close"]

        if highest == lowest:
            k = 50.0
        else:
            k = 100 * (
                (close - lowest) / (highest - lowest)
            )

        k_values.append(k)

    k = k_values[-1]
    d = sum(k_values) / len(k_values)

    return k, d


def calculate_adx(candles, period=14):
    if len(candles) < period * 2 + 1:
        return None

    plus_dm = []
    minus_dm = []
    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        plus_dm.append(
            up_move if up_move > down_move and up_move > 0
            else 0
        )

        minus_dm.append(
            down_move if down_move > up_move and down_move > 0
            else 0
        )

        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))

    tr = sum(true_ranges[:period])
    plus = sum(plus_dm[:period])
    minus = sum(minus_dm[:period])

    dx_values = []

    for i in range(period, len(true_ranges)):
        tr = tr - tr / period + true_ranges[i]
        plus = plus - plus / period + plus_dm[i]
        minus = minus - minus / period + minus_dm[i]

        if tr <= 0:
            continue

        plus_di = 100 * plus / tr
        minus_di = 100 * minus / tr

        total = plus_di + minus_di

        if total == 0:
            dx_values.append(0)
        else:
            dx_values.append(
                100 * abs(plus_di - minus_di) / total
            )

    if len(dx_values) < period:
        return None

    adx = sum(dx_values[:period]) / period

    for value in dx_values[period:]:
        adx = (
            adx * (period - 1) + value
        ) / period

    return adx


# =========================================================
# STRICT M1 CONFIRMATION
# =========================================================

def evaluate_m1(candles):
    if len(candles) < MIN_CANDLES:
        return None

    closes = [c["close"] for c in candles]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)

    rsi = calculate_rsi(closes, 14)
    macd = calculate_macd(closes)
    bands = calculate_bollinger(closes, 20)
    stochastic = calculate_stochastic(candles, 14)
    adx = calculate_adx(candles, 14)

    if any(x is None for x in (
        ema9, ema21, rsi, macd,
        bands, stochastic, adx
    )):
        return None

    candle = candles[-1]

    open_price = candle["open"]
    close_price = candle["close"]
    high = candle["high"]
    low = candle["low"]

    candle_range = high - low

    if candle_range <= 0:
        return None

    body = abs(close_price - open_price)
    body_ratio = body / candle_range

    middle, upper, lower = bands
    stoch_k, stoch_d = stochastic

    # A candle with a very small body is not a strong confirmation.
    if body_ratio < 0.35:
        return None

    # ---------------- CALL ----------------

    call_checks = [
        ema9 > ema21 and close_price > ema9,
        macd > 0,
        52 <= rsi <= 68,
        middle < close_price < upper,
        stoch_k > stoch_d and stoch_k < 85,
        adx >= MIN_ADX,
        close_price > open_price
    ]

    # ---------------- PUT -----------------

    put_checks = [
        ema9 < ema21 and close_price < ema9,
        macd < 0,
        32 <= rsi <= 48,
        lower < close_price < middle,
        stoch_k < stoch_d and stoch_k > 15,
        adx >= MIN_ADX,
        close_price < open_price
    ]

    call_score = sum(call_checks)
    put_score = sum(put_checks)

    if call_score == MIN_SCORE:
        return {
            "direction": "CALL",
            "score": call_score,
            "rsi": rsi,
            "adx": adx,
            "price": close_price
        }

    if put_score == MIN_SCORE:
        return {
            "direction": "PUT",
            "score": put_score,
            "rsi": rsi,
            "adx": adx,
            "price": close_price
        }

    return None


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

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

    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm message delivery")


def send_signal(symbol, payout, signal, entry_time):
    direction = signal["direction"]

    if direction == "CALL":
        heading = "🟢 CALL"
    else:
        heading = "🔴 PUT"

    message = (
        f"🤖 {VERSION}\n\n"
        f"{heading}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"⭐ CONFIRMATION: {signal['score']}/7\n"
        f"📊 RSI: {signal['rsi']:.1f}\n"
        f"📈 ADX: {signal['adx']:.1f}\n"
        f"💰 PAYOUT: {payout:.1f}%\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n"
        f"📩 SIGNAL TIME: {local_time()}\n"
        f"🎯 ENTRY TIME: {local_time(entry_time)}\n\n"
        f"⚠️ Signal only — manual entry."
    )

    send_telegram(message)

    logging.info(
        "FINAL SIGNAL SENT: %s %s",
        symbol,
        direction
    )


# =========================================================
# LIVE STREAM
# =========================================================

def process_tick(tick, histories, payouts, current_bars):
    symbol = tick.get("symbol")

    if symbol not in histories:
        return False

    try:
        price = float(tick.get("price"))
    except (TypeError, ValueError):
        return False

    timestamp = normalize_timestamp(
        tick.get("time", tick.get("timestamp"))
    )

    if timestamp is None:
        timestamp = time.time()

    bucket = int(timestamp // TIMEFRAME) * TIMEFRAME

    current = current_bars.get(symbol)

    # First tick for this pair
    if current is None:
        current_bars[symbol] = {
            "time": bucket,
            "open": price,
            "high": price,
            "low": price,
            "close": price
        }
        return False

    # Same M1 candle
    if bucket == current["time"]:
        current["high"] = max(current["high"], price)
        current["low"] = min(current["low"], price)
        current["close"] = price
        return False

    # Ignore old or out-of-order ticks
    if bucket < current["time"]:
        return False

    # Previous M1 candle has now closed.
    closed_candle = current.copy()

    histories[symbol].append(closed_candle)

    # Start the new M1 candle
    current_bars[symbol] = {
        "time": bucket,
        "open": price,
        "high": price,
        "low": price,
        "close": price
    }

    logging.info(
        "%s M1 candle closed. Checking confirmation...",
        symbol
    )

    signal = evaluate_m1(list(histories[symbol]))

    if signal is None:
        logging.info(
            "%s: No complete M1 confirmation.",
            symbol
        )
        return False

    payout = payouts[symbol]

    if payout < MIN_PAYOUT:
        return False

    # The first tick of the new candle is the entry reference.
    send_signal(
        symbol,
        payout,
        signal,
        bucket
    )

    return True


def run_live_stream(markets):
    symbols = [item["symbol"] for item in markets]

    payouts = {
        item["symbol"]: item["payout"]
        for item in markets
    }

    histories = {}

    for symbol in symbols:
        try:
            candles = get_closed_candles(symbol)

            if len(candles) < MIN_CANDLES:
                logging.warning(
                    "%s: Not enough closed M1 candles (%s).",
                    symbol,
                    len(candles)
                )
                continue

            histories[symbol] = deque(
                candles,
                maxlen=CANDLE_LIMIT
            )

            logging.info(
                "%s: Loaded %s closed M1 candles.",
                symbol,
                len(candles)
            )

        except Exception as error:
            logging.error(
                "%s: Candle loading failed: %s",
                symbol,
                error
            )

    if not histories:
        logging.error(
            "No pair has enough verified M1 candle history."
        )
        return

    active_symbols = list(histories.keys())

    current_bars = {}

    stream_url = BASE_URL + "/v1/stream"

    params = {
        "venue": VENUE,
        "symbol": ",".join(active_symbols)
    }

    deadline = time.time() + MAX_WAIT_SECONDS

    logging.info(
        "Starting M1 stream for: %s",
        active_symbols
    )

    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))

        try:
            with session.get(
                stream_url,
                params=params,
                stream=True,
                timeout=(10, remaining + 10)
            ) as response:

                response.raise_for_status()

                event_name = None

                for line in response.iter_lines(
                    decode_unicode=True
                ):
                    if time.time() >= deadline:
                        break

                    if not line:
                        event_name = None
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
                        logging.info("OTCharts stream connected.")
                        continue

                    if event_name == "tick":
                        if process_tick(
                            data,
                            histories,
                            payouts,
                            current_bars
                        ):
                            return

        except requests.exceptions.Timeout:
            logging.warning(
                "Stream timeout. Reconnecting..."
            )

        except requests.exceptions.RequestException as error:
            logging.error(
                "Stream connection error: %s",
                error
            )
            time.sleep(3)

        except Exception as error:
            logging.exception(
                "Unexpected stream error: %s",
                error
            )
            time.sleep(3)

    logging.info(
        "Five-minute scan finished. No fully confirmed signal."
    )


# =========================================================
# MAIN
# =========================================================

def main():
    logging.info("=" * 60)
    logging.info("%s STARTING", VERSION)
    logging.info("TIMEFRAME: M1 ONLY")
    logging.info("EXPIRATION: 60 SECONDS")
    logging.info("MIN PAYOUT: %.1f%%", MIN_PAYOUT)
    logging.info("STRICT CONFIRMATION: 7/7")
    logging.info("MAX MARKETS: %s", MAX_MARKETS)
    logging.info("=" * 60)

    validate_environment()

    markets = get_eligible_markets()

    if not markets:
        logging.warning(
            "No OTC pairs meet the payout and market filters."
        )
        return

    run_live_stream(markets)


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        logging.exception(
            "BOT STOPPED: %s",
            error
        )
        raise
