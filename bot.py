
import os
import re
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# =========================================================
# ROMARIO TRADER PRO 9.7
# POCKET OPTION OTC | EMA50 + STOCHASTIC 14,3
# M1 | 60 SECONDS | TELEGRAM ALERTS ONLY
# =========================================================

BOT_NAME = "ROMARIO TRADER PRO 9.7"

BASE_URL = "https://otcharts.com"
SYMBOLS_ENDPOINT = "/v1/symbols"
CANDLES_ENDPOINT = "/v1/candles"

TIMEFRAME = 60
CANDLE_LIMIT = 300
MIN_PAYOUT = 92.0
EXPIRATION_SECONDS = 60
ENTRY_SECONDS_BEFORE = 15

OTC_OFFSET = 7200

RETRIES = 5
RETRY_BASE_DELAY = 5
RETRY_MAX_DELAY = 30
REQUEST_TIMEOUT = 30

FLORIDA_TZ = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RETRY_STATUSES = {429, 500, 502, 503, 504}

session = requests.Session()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================================================
# TIME
# =========================================================

def florida_now():
    return datetime.now(FLORIDA_TZ)


def florida_time_string(timestamp=None):
    if timestamp is None:
        dt = florida_now()
    else:
        dt = datetime.fromtimestamp(timestamp, FLORIDA_TZ)

    return dt.strftime("%Y-%m-%d %I:%M:%S %p")


# =========================================================
# TELEGRAM
# =========================================================

def telegram_send(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram secrets are missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            logging.error(
                "Telegram rejected message: %s",
                result
            )
            return False

        return True

    except requests.RequestException as error:
        logging.error("Telegram error: %s", error)
        return False


# =========================================================
# OTCHARTS API
# =========================================================

def api_get(endpoint, params=None):

    if not API_KEY:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing."
        )

    url = BASE_URL + endpoint

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json"
    }

    for attempt in range(RETRIES + 1):

        try:

            response = session.get(
                url,
                headers=headers,
                params=params or {},
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code in RETRY_STATUSES:

                raise requests.HTTPError(
                    f"HTTP {response.status_code}: "
                    f"{response.text[:250]}",
                    response=response
                )

            response.raise_for_status()

            return response.json()

        except requests.HTTPError as error:

            status = (
                error.response.status_code
                if error.response is not None
                else None
            )

            if status not in RETRY_STATUSES:
                raise

            if attempt >= RETRIES:
                raise

            delay = min(
                RETRY_BASE_DELAY * (2 ** attempt),
                RETRY_MAX_DELAY
            )

            logging.warning(
                "OTCharts HTTP %s. Retry %s/%s in %s seconds.",
                status,
                attempt + 1,
                RETRIES,
                delay
            )

            time.sleep(delay)

        except requests.RequestException as error:

            if attempt >= RETRIES:
                raise

            delay = min(
                RETRY_BASE_DELAY * (2 ** attempt),
                RETRY_MAX_DELAY
            )

            logging.warning(
                "OTCharts connection error. "
                "Retry %s/%s in %s seconds: %s",
                attempt + 1,
                RETRIES,
                delay,
                error
            )

            time.sleep(delay)

    raise RuntimeError(
        "OTCharts API failed after retries."
    )


# =========================================================
# DATA HELPERS
# =========================================================

def find_list(data, possible_keys):

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in possible_keys:

        value = data.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):

            found = find_list(
                value,
                possible_keys
            )

            if found:
                return found

    for value in data.values():

        if isinstance(value, (dict, list)):

            found = find_list(
                value,
                possible_keys
            )

            if found:
                return found

    return []


def get_field(item, names, default=None):

    if not isinstance(item, dict):
        return default

    for name in names:

        if item.get(name) is not None:
            return item[name]

    return default


def to_number(value, default=None):

    try:

        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace("%", "").strip()

        return float(value)

    except (ValueError, TypeError):
        return default


# =========================================================
# SYMBOL DISCOVERY
# =========================================================

def get_symbols():

    data = api_get(
        SYMBOLS_ENDPOINT,
        {"venue": "otc"}
    )

    symbols = find_list(
        data,
        ("symbols", "data", "result", "items")
    )

    if not symbols:
        raise RuntimeError(
            "OTCharts returned no readable symbols."
        )

    eligible = []

    for item in symbols:

        if isinstance(item, str):

            symbol = item.strip()
            payout = None
            is_open = True
            is_otc = True

        elif isinstance(item, dict):

            symbol = str(
                get_field(
                    item,
                    ("symbol", "name", "ticker"),
                    ""
                )
            ).strip()

            payout = to_number(
                get_field(
                    item,
                    (
                        "payout",
                        "payout_percent",
                        "profit",
                        "profit_percent"
                    )
                )
            )

            is_open = get_field(
                item,
                ("open", "is_open", "active"),
                False
            )

            is_otc = get_field(
                item,
                ("isOtc", "is_otc", "otc"),
                False
            )

        else:
            continue

        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        if is_open is not True:
            continue

        if is_otc is not True:
            continue

        if payout is None:
            continue

        if 0 < payout <= 1:
            payout *= 100

        if payout < MIN_PAYOUT:
            continue

        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    unique = {}

    for item in eligible:
        unique[item["symbol"]] = item

    return list(unique.values())


# =========================================================
# CANDLE PARSING
# =========================================================

def parse_timestamp(value):

    try:

        if isinstance(value, str):

            try:
                value = float(value)

            except ValueError:

                dt = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )

                if dt.tzinfo is None:
                    dt = dt.replace(
                        tzinfo=timezone.utc
                    )

                return dt.timestamp()

        number = float(value)

        if number > 10_000_000_000:
            number /= 1000

        return number

    except (
        ValueError,
        TypeError,
        OverflowError
    ):
        return None


def parse_candle(item):

    if isinstance(item, dict):

        timestamp = get_field(
            item,
            ("time", "timestamp", "t", "datetime", "date")
        )

        open_price = get_field(item, ("open", "o"))
        high_price = get_field(item, ("high", "h"))
        low_price = get_field(item, ("low", "l"))
        close_price = get_field(item, ("close", "c"))

    elif isinstance(item, (list, tuple)) and len(item) >= 5:

        timestamp = item[0]
        open_price = item[1]
        high_price = item[2]
        low_price = item[3]
        close_price = item[4]

    else:
        return None

    timestamp = parse_timestamp(timestamp)
    open_price = to_number(open_price)
    high_price = to_number(high_price)
    low_price = to_number(low_price)
    close_price = to_number(close_price)

    if None in (
        timestamp,
        open_price,
        high_price,
        low_price,
        close_price
    ):
        return None

    return {
        "time": timestamp,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price
    }


# =========================================================
# GET CANDLES
# =========================================================

def get_candles(symbol):

    logging.info(
        "Requesting candles: %s",
        symbol
    )

    data = api_get(
        CANDLES_ENDPOINT,
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    raw_candles = find_list(
        data,
        ("candles", "data", "result", "items")
    )

    candles = []

    for item in raw_candles:

        candle = parse_candle(item)

        if candle:
            candles.append(candle)

    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = sorted(
        unique.values(),
        key=lambda candle: candle["time"]
    )

    now = time.time()

    completed = [
        candle for candle in candles
        if candle["time"] - OTC_OFFSET + TIMEFRAME <= now
    ]

    logging.info(
        "%s candles received for %s; %s completed.",
        len(candles),
        symbol,
        len(completed)
    )

    if len(completed) < 60:

        raise ValueError(
            f"Not enough completed candles for {symbol}. "
            f"Received {len(completed)}."
        )

    return completed


# =========================================================
# EMA
# =========================================================

def calculate_ema(values, period):

    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    ema_values = [
        sum(values[:period]) / period
    ]

    for value in values[period:]:

        ema = (
            value * multiplier
            + ema_values[-1] * (1 - multiplier)
        )

        ema_values.append(ema)

    return [None] * (period - 1) + ema_values


# =========================================================
# STOCHASTIC 14,3
# =========================================================

def calculate_stochastic(candles, period=14):

    k_values = []

    for index in range(len(candles)):

        if index < period - 1:
            k_values.append(None)
            continue

        window = candles[
            index - period + 1:index + 1
        ]

        highest = max(
            candle["high"] for candle in window
        )

        lowest = min(
            candle["low"] for candle in window
        )

        close = candles[index]["close"]

        if highest == lowest:
            k_values.append(50.0)

        else:
            k_values.append(
                ((close - lowest) / (highest - lowest)) * 100
            )

    d_values = []

    for index in range(len(k_values)):

        if index < 2:
            d_values.append(None)
            continue

        window = k_values[index - 2:index + 1]

        if any(value is None for value in window):
            d_values.append(None)

        else:
            d_values.append(
                sum(window) / 3
            )

    return k_values, d_values


# =========================================================
# STRATEGY 9.7
# SEPARATE CALL / PUT SCORING
# =========================================================

def analyze_pair(pair):

    symbol = pair["symbol"]
    payout = pair["payout"]

    candles = get_candles(symbol)

    closes = [
        candle["close"] for candle in candles
    ]

    ema_values = calculate_ema(
        closes,
        50
    )

    k_values, d_values = calculate_stochastic(
        candles
    )

    if len(candles) < 55:
        return None

    index = len(candles) - 1

    ema_now = ema_values[index]
    ema_previous = ema_values[index - 1]

    k_now = k_values[index]
    k_previous = k_values[index - 1]
    k_two_back = k_values[index - 2]

    d_now = d_values[index]
    d_previous = d_values[index - 1]

    required = (
        ema_now,
        ema_previous,
        k_now,
        k_previous,
        k_two_back,
        d_now,
        d_previous
    )

    if any(value is None for value in required):
        return None

    candle = candles[index]

    bullish = candle["close"] > candle["open"]
    bearish = candle["close"] < candle["open"]

    call_conditions = [
        candle["close"] > ema_now,
        ema_now > ema_previous,
        k_now > d_now,
        k_now > k_previous,
        d_now >= d_previous,
        k_now > k_two_back,
        bullish,
        20 < k_now < 80
    ]

    put_conditions = [
        candle["close"] < ema_now,
        ema_now < ema_previous,
        k_now < d_now,
        k_now < k_previous,
        d_now <= d_previous,
        k_now < k_two_back,
        bearish,
        20 < k_now < 80
    ]

    call_score = sum(call_conditions)
    put_score = sum(put_conditions)

    logging.info(
        "%s | CALL %s/8 | PUT %s/8 | K=%.2f | D=%.2f",
        symbol,
        call_score,
        put_score,
        k_now,
        d_now
    )

    # Both directions must meet all 8 conditions.
    if call_score == 8 and put_score == 8:
        logging.warning(
            "%s: conflicting signal. Skipped.",
            symbol
        )
        return None

    if call_score == 8:

        direction = "CALL"
        score = call_score

    elif put_score == 8:

        direction = "PUT"
        score = put_score

    else:
        return None

    ema_distance = (
        abs(
            (candle["close"] - ema_now) / ema_now
        )
        if ema_now else 0
    )

    return {
        "symbol": symbol,
        "direction": direction,
        "payout": payout,
        "score": score,
        "ema_distance": ema_distance,
        "stochastic_k": k_now,
        "stochastic_d": d_now
    }


# =========================================================
# ENTRY TIMING
# =========================================================

def wait_for_entry():

    now = time.time()

    next_minute = (
        int(now // 60) + 1
    ) * 60

    target = next_minute - ENTRY_SECONDS_BEFORE

    if target <= now:
        target += 60

    delay = max(
        0,
        target - time.time()
    )

    logging.info(
        "Waiting %.1f seconds for entry.",
        delay
    )

    time.sleep(delay)

    return target


# =========================================================
# TELEGRAM SIGNAL
# =========================================================

def send_signal(signal, entry_timestamp):

    entry_time = florida_time_string(
        entry_timestamp
    )

    current_time = florida_time_string()

    message = (
        f"🤖 <b>{BOT_NAME}</b>\n\n"
        f"🟢 <b>{signal['direction']}</b>\n\n"
        f"💱 PAIR: <b>{signal['symbol']}</b>\n"
        f"💰 PAYOUT: <b>{signal['payout']:.1f}%</b>\n"
        f"📊 SETUP: <b>{signal['score']}/8</b>\n\n"
        f"⏱ TIMEFRAME: <b>M1</b>\n"
        f"⌛ EXPIRATION: <b>{EXPIRATION_SECONDS} SECONDS</b>\n"
        f"🎯 ENTRY TIME: <b>{entry_time}</b>\n\n"
        f"📈 STOCH K: {signal['stochastic_k']:.2f}\n"
        f"📉 STOCH D: {signal['stochastic_d']:.2f}\n\n"
        f"🕒 FLORIDA TIME: {current_time}\n"
        f"⚠️ Alert only — verify before entering."
    )

    telegram_send(message)


# =========================================================
# NO SIGNAL
# =========================================================

def send_no_signal(pair_count):

    message = (
        f"🤖 <b>{BOT_NAME}</b>\n\n"
        f"⚪ <b>NO VALID SIGNAL</b>\n\n"
        f"Pairs checked: {pair_count}\n"
        f"Minimum payout: {MIN_PAYOUT:.1f}%\n"
        f"Timeframe: M1\n\n"
        f"No pair passed all strategy conditions.\n"
        f"No signal was forced.\n\n"
        f"🕒 FLORIDA TIME: {florida_time_string()}"
    )

    telegram_send(message)


# =========================================================
# API ERROR
# =========================================================

def send_api_unavailable(error):

    message = (
        f"⚠️ <b>{BOT_NAME} API ERROR</b>\n\n"
        f"OTCharts could not provide required data.\n"
        f"No signal generated.\n\n"
        f"Error: <code>{str(error)[:300]}</code>\n\n"
        f"🕒 FLORIDA TIME: {florida_time_string()}"
    )

    telegram_send(message)


# =========================================================
# MAIN
# =========================================================

def main():

    logging.info(
        "%s starting...",
        BOT_NAME
    )

    if not API_KEY:

        error = "OTCHARTS_API_KEY is missing."

        logging.error(error)

        send_api_unavailable(error)

        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        logging.error(
            "Telegram secrets are missing."
        )

        return

    try:

        pairs = get_symbols()

    except Exception as error:

        logging.error(
            "Symbol discovery failed: %s",
            error
        )

        send_api_unavailable(error)

        return

    logging.info(
        "Eligible OTC pairs found: %s",
        len(pairs)
    )

    if not pairs:

        send_no_signal(0)

        return

    signals = []
    candle_errors = 0

    # Scan every eligible pair
    for number, pair in enumerate(
        pairs,
        start=1
    ):

        symbol = pair["symbol"]

        logging.info(
            "Scanning %s (%s/%s)...",
            symbol,
            number,
            len(pairs)
        )

        try:

            signal = analyze_pair(pair)

            if signal:

                signals.append(signal)

                logging.info(
                    "VALID %s %s | %s/8 | payout %.1f%%",
                    signal["direction"],
                    symbol,
                    signal["score"],
                    signal["payout"]
                )

            else:

                logging.info(
                    "No valid setup: %s",
                    symbol
                )

        except Exception as error:

            candle_errors += 1

            logging.warning(
                "Could not scan %s: %s",
                symbol,
                error
            )

    if candle_errors == len(pairs):

        send_api_unavailable(
            "Candle data failed for every eligible pair. "
            "Check GitHub logs."
        )

        return

    if not signals:

        send_no_signal(len(pairs))

        return

    # Best signal first
    signals.sort(
        key=lambda item: (
            item["score"],
            item["ema_distance"],
            item["payout"]
        ),
        reverse=True
    )

    best_signal = signals[0]

    logging.info(
        "BEST SIGNAL: %s %s",
        best_signal["direction"],
        best_signal["symbol"]
    )

    entry_timestamp = wait_for_entry()

    send_signal(
        best_signal,
        entry_timestamp
    )

    logging.info(
        "Bot finished."
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as error:

        logging.exception(
            "Unexpected bot error."
        )

        send_api_unavailable(error)
