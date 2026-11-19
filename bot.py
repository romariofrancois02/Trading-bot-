
import os
import time
import csv
import logging
import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ============================================================
# ROMARIO TRADER PRO - CLEAN 1.1
# M1 | EXPIRATION 60s | EMA PULLBACK
# ONE SIGNAL PER RUN | TELEGRAM ALERTS ONLY
# ============================================================

VERSION = "ROMARIO TRADER PRO CLEAN 1.1"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 60
EXPIRATION = 60

MIN_PAYOUT = 85.0
MIN_SCORE = 85

MAX_SYMBOLS = 5
CANDLE_LIMIT = 150
MIN_CANDLES = 80

SCAN_INTERVAL = 5
MAX_SCAN_SECONDS = 120

ENTRY_DELAY = 10

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 7
ATR_PERIOD = 14

MAX_CANDLE_ATR = 1.50
MIN_BODY_RATIO = 0.35

MAX_API_REQUESTS = 5000

TZ = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ============================================================
# SECRETS
# ============================================================

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not API_KEY or not TG_TOKEN or not TG_CHAT_ID:
    raise RuntimeError(
        "Missing GitHub Secrets: "
        "OTCHARTS_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID"
    )

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "ROMARIO-TRADER-CLEAN-1.1"
})

api_requests = 0

# ============================================================
# OTCHARTS API
# ============================================================

def api_get(path, params=None, timeout=25):
    global api_requests

    if api_requests >= MAX_API_REQUESTS:
        raise RuntimeError("API request limit reached")

    api_requests += 1

    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=timeout
    )

    if response.status_code == 429:
        raise RuntimeError("OTCharts rate limit reached")

    response.raise_for_status()

    return response.json()


def unwrap_data(data):
    if isinstance(data, dict):
        for key in ("data", "results", "result"):
            if key in data:
                return data[key]

    return data


def get_value(item, keys, default=None):
    if not isinstance(item, dict):
        return default

    for key in keys:
        if key in item and item[key] is not None:
            return item[key]

    return default


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message
    }

    response = requests.post(
        url,
        json=payload,
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram message was not accepted")

    return True


# ============================================================
# SYMBOLS AND PAYOUT
# ============================================================

def normalize_symbol(item):
    if isinstance(item, str):
        return item

    if not isinstance(item, dict):
        return None

    return get_value(
        item,
        ["symbol", "name", "ticker", "instrument"]
    )


def normalize_payout(item):
    if not isinstance(item, dict):
        return None

    value = get_value(
        item,
        [
            "payout",
            "payout_percent",
            "profit",
            "return",
            "profit_percent"
        ]
    )

    if value is None:
        return None

    try:
        value = float(value)

        if 0 < value <= 1:
            value *= 100

        return value

    except (ValueError, TypeError):
        return None


def symbol_is_open(item):
    if not isinstance(item, dict):
        return True

    value = get_value(
        item,
        ["open", "is_open", "active"],
        True
    )

    if isinstance(value, str):
        return value.lower() not in (
            "false", "closed", "0", "no"
        )

    return bool(value)


def get_symbols():
    raw = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    data = unwrap_data(raw)

    if isinstance(data, dict):
        data = data.get(
            "symbols",
            data.get("instruments", [])
        )

    if not isinstance(data, list):
        raise RuntimeError(
            "Unexpected OTCharts symbols response"
        )

    markets = []

    for item in data:
        symbol = normalize_symbol(item)

        if not symbol:
            continue

        if not symbol.lower().endswith("_otc"):
            continue

        payout = normalize_payout(item)

        if payout is None:
            continue

        if payout < MIN_PAYOUT:
            continue

        if not symbol_is_open(item):
            continue

        markets.append({
            "symbol": symbol,
            "payout": payout
        })

    markets.sort(
        key=lambda x: x["payout"],
        reverse=True
    )

    return markets[:MAX_SYMBOLS]


# ============================================================
# CANDLE DATA
# ============================================================

def get_candles(symbol):
    raw = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    data = unwrap_data(raw)

    if isinstance(data, dict):
        data = data.get(
            "candles",
            data.get("history", [])
        )

    if not isinstance(data, list):
        return []

    candles = []

    for item in data:
        if not isinstance(item, dict):
            continue

        try:
            o = float(get_value(item, ["open", "o"]))
            h = float(get_value(item, ["high", "h"]))
            l = float(get_value(item, ["low", "l"]))
            c = float(get_value(item, ["close", "c"]))

            ts = get_value(
                item,
                ["timestamp", "time", "ts", "datetime"]
            )

            if h < l:
                continue

            candles.append({
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "timestamp": ts
            })

        except (ValueError, TypeError):
            continue

    # Keep API chronological order.
    # Do not sort timestamps as strings.

    # Remove duplicates only when timestamp exists.
    unique = []
    seen = set()

    for candle in candles:
        ts = candle["timestamp"]

        if ts is not None:
            key = str(ts)

            if key in seen:
                continue

            seen.add(key)

        unique.append(candle)

    return unique


# ============================================================
# INDICATORS
# ============================================================

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


def rsi(values, period=7):
    if len(values) < period + 2:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        difference = values[i] - values[i - 1]

        gains.append(max(difference, 0))
        losses.append(max(-difference, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close)
        )

        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


# ============================================================
# CANDLE CONFIRMATION
# ============================================================

def candle_quality(current, previous, atr_value, direction):
    if atr_value is None or atr_value <= 0:
        return False

    candle_range = current["high"] - current["low"]

    if candle_range <= 0:
        return False

    body = abs(
        current["close"] - current["open"]
    )

    body_ratio = body / candle_range

    if body_ratio < MIN_BODY_RATIO:
        return False

    if candle_range > atr_value * MAX_CANDLE_ATR:
        return False

    if direction == "CALL":
        if current["close"] <= current["open"]:
            return False

        if current["close"] <= previous["close"]:
            return False

    elif direction == "PUT":
        if current["close"] >= current["open"]:
            return False

        if current["close"] >= previous["close"]:
            return False

    return True


# ============================================================
# EMA PULLBACK STRATEGY
# ============================================================

def analyze_candles(candles, payout):
    if len(candles) < MIN_CANDLES:
        return None

    # The newest candle may still be forming.
    # Analyze the previous, closed candle.
    closed = candles[:-1]

    if len(closed) < MIN_CANDLES:
        return None

    closes = [
        candle["close"]
        for candle in closed
    ]

    current = closed[-1]
    previous = closed[-2]

    ema9 = ema(closes, EMA_FAST)
    ema21 = ema(closes, EMA_MID)
    ema50 = ema(closes, EMA_SLOW)

    rsi_value = rsi(closes, RSI_PERIOD)
    atr_value = atr(closed, ATR_PERIOD)

    if any(value is None for value in (
        ema9, ema21, ema50, rsi_value, atr_value
    )):
        return None

    if atr_value <= 0 or current["close"] <= 0:
        return None

    # Volatility filter
    atr_ratio = atr_value / current["close"]

    if atr_ratio < 0.00005 or atr_ratio > 0.003:
        return None

    if payout < MIN_PAYOUT:
        return None

    direction = None
    score = 0
    reasons = []

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    if ema9 > ema21 > ema50:

        near_ema = min(
            abs(current["low"] - ema9),
            abs(current["low"] - ema21),
            abs(current["close"] - ema9),
            abs(current["close"] - ema21)
        )

        pullback_ok = near_ema <= atr_value * 0.40

        rsi_ok = 50 <= rsi_value <= 68

        candle_ok = candle_quality(
            current,
            previous,
            atr_value,
            "CALL"
        )

        chase_ok = (
            current["close"]
            <= ema9 + atr_value * 0.65
        )

        if (
            pullback_ok
            and rsi_ok
            and candle_ok
            and chase_ok
        ):
            direction = "CALL"

    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    elif ema9 < ema21 < ema50:

        near_ema = min(
            abs(current["high"] - ema9),
            abs(current["high"] - ema21),
            abs(current["close"] - ema9),
            abs(current["close"] - ema21)
        )

        pullback_ok = near_ema <= atr_value * 0.40

        rsi_ok = 32 <= rsi_value <= 50

        candle_ok = candle_quality(
            current,
            previous,
            atr_value,
            "PUT"
        )

        chase_ok = (
            current["close"]
            >= ema9 - atr_value * 0.65
        )

        if (
            pullback_ok
            and rsi_ok
            and candle_ok
            and chase_ok
        ):
            direction = "PUT"

    if direction is None:
        return None

    # Score is a technical filter score, not win probability.
    score = 100

    reasons = [
        "EMA trend aligned",
        "Pullback confirmed",
        "RSI momentum confirmed",
        "Closed candle confirmed",
        "ATR volatility valid",
        "Payout passed"
    ]

    if score < MIN_SCORE:
        return None

    return {
        "direction": direction,
        "score": score,
        "rsi": round(rsi_value, 2),
        "ema9": ema9,
        "ema21": ema21,
        "ema50": ema50,
        "atr": atr_value,
        "candle": current,
        "reasons": reasons
    }


# ============================================================
# SIGNAL LOG
# ============================================================

def save_signal(symbol, payout, analysis, entry_time):
    filename = "signals_log.csv"

    file_exists = os.path.isfile(filename)

    with open(
        filename,
        "a",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.writer(file)

        if not file_exists:
            writer.writerow([
                "time",
                "symbol",
                "direction",
                "payout",
                "score",
                "rsi",
                "entry_time",
                "status"
            ])

        writer.writerow([
            datetime.now(TZ).isoformat(),
            symbol,
            analysis["direction"],
            payout,
            analysis["score"],
            analysis["rsi"],
            entry_time.isoformat(),
            "SIGNAL_SENT"
        ])


# ============================================================
# TELEGRAM SIGNAL
# ============================================================

def build_signal(symbol, payout, analysis):
    now = datetime.now(TZ)

    entry_time = now + timedelta(
        seconds=ENTRY_DELAY
    )

    expiry_time = entry_time + timedelta(
        seconds=EXPIRATION
    )

    if analysis["direction"] == "CALL":
        direction_text = "🟢 CALL"
    else:
        direction_text = "🔴 PUT"

    if analysis["score"] >= 90:
        quality = "HIGH"
    else:
        quality = "GOOD"

    message = (
        "🤖 ROMARIO TRADER\n\n"
        f"{direction_text} | {symbol}\n"
        f"⭐ QUALITY: {quality} ({analysis['score']}/100)\n"
        "⏱ TIMEFRAME: 1M\n"
        "⌛ EXPIRATION: 1M\n"
        f"💰 PAYOUT: {payout:.1f}%\n"
        f"🎯 ENTRY: {entry_time.strftime('%H:%M:%S')} ET\n"
        f"🏁 EXPIRY: {expiry_time.strftime('%H:%M:%S')} ET\n\n"
        "EMA + PULLBACK + RSI CONFIRMED\n"
        "⚠️ Technical score is not a win probability."
    )

    return message, entry_time


# ============================================================
# MAIN SCANNER
# ============================================================

def main():
    logging.info("=" * 55)
    logging.info("%s starting", VERSION)
    logging.info("TIMEFRAME: %s seconds", TIMEFRAME)
    logging.info("EXPIRATION: %s seconds", EXPIRATION)
    logging.info("MIN PAYOUT: %s%%", MIN_PAYOUT)
    logging.info("MIN SCORE: %s", MIN_SCORE)
    logging.info("MAX SIGNALS PER RUN: 1")
    logging.info("=" * 55)

    # Load markets
    try:
        markets = get_symbols()

    except Exception as error:
        logging.exception(
            "Could not load OTCharts symbols: %s",
            error
        )

        telegram_send(
            "ROMARIO TRADER\n"
            "OTCharts pa kapab chaje mache yo."
        )

        return

    if not markets:
        logging.warning(
            "No OTC market passed payout/open filters."
        )

        telegram_send(
            "ROMARIO TRADER\n"
            "Pa gen mache OTC ki pase filtè 85% la."
        )

        return

    logging.info(
        "Markets selected: %s",
        len(markets)
    )

    for market in markets:
        logging.info(
            "WATCH: %s | PAYOUT: %.1f%%",
            market["symbol"],
            market["payout"]
        )

    start_time = time.time()
    scanned_candles = {}

    # Scan for up to two minutes.
    while time.time() - start_time < MAX_SCAN_SECONDS:

        for market in markets:

            symbol = market["symbol"]
            payout = market["payout"]

            try:
                candles = get_candles(symbol)

                if len(candles) < MIN_CANDLES:
                    logging.info(
                        "%s: not enough candles (%s)",
                        symbol,
                        len(candles)
                    )
                    continue

                # Use timestamp to avoid analyzing the same
                # closed candle repeatedly.
                closed_candle = candles[-2]
                candle_key = closed_candle["timestamp"]

                if candle_key is None:
                    candle_key = (
                        str(closed_candle["open"])
                        + str(closed_candle["close"])
                    )

                candle_key = str(candle_key)

                if scanned_candles.get(symbol) == candle_key:
                    continue

                scanned_candles[symbol] = candle_key

                analysis = analyze_candles(
                    candles,
                    payout
                )

                if analysis is None:
                    logging.info(
                        "%s: no valid setup",
                        symbol
                    )
                    continue

                message, entry_time = build_signal(
                    symbol,
                    payout,
                    analysis
                )

                logging.info(
                    "VALID SIGNAL: %s %s SCORE=%s",
                    symbol,
                    analysis["direction"],
                    analysis["score"]
                )

                # Send one signal only.
                telegram_send(message)

                save_signal(
                    symbol,
                    payout,
                    analysis,
                    entry_time
                )

                logging.info(
                    "Telegram signal sent. Stopping this run."
                )

                return

            except requests.RequestException as error:
                logging.warning(
                    "%s API error: %s",
                    symbol,
                    error
                )
                continue

            except Exception as error:
                logging.exception(
                    "%s analysis error: %s",
                    symbol,
                    error
                )
                continue

        remaining = MAX_SCAN_SECONDS - (
            time.time() - start_time
        )

        if remaining <= 0:
            break

        logging.info(
            "No valid signal. Waiting %s seconds.",
            SCAN_INTERVAL
        )

        time.sleep(
            min(SCAN_INTERVAL, remaining)
        )

    logging.info(
        "Scan ended: no setup passed the filter."
    )

    # No signal and no false WIN/LOSS message.
    return


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        logging.info("Bot stopped manually.")

    except Exception:
        logging.exception("Fatal bot error")
