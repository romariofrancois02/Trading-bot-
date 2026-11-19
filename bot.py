
import os
import time
import csv
import logging
import requests

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# ============================================================
# ROMARIO TRADER PRO CLEAN 1.2
# M1 | 60s EXPIRATION | CLOSED CANDLE CONFIRMATION
# ONE SIGNAL PER RUN | TELEGRAM ALERTS ONLY
# ============================================================

VERSION = "ROMARIO TRADER PRO CLEAN 1.2"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 60
EXPIRATION = 60

MIN_PAYOUT = 85.0
MIN_SCORE = 75

MAX_SYMBOLS = 5
CANDLE_LIMIT = 150
MIN_CANDLES = 80

SCAN_INTERVAL = 5
MAX_SCAN_SECONDS = 120

# Entry is 15 seconds after the confirmed candle closes.
ENTRY_DELAY = 15

# Require a few seconds remaining before planned entry.
MIN_ENTRY_LEAD = 3

# Assumption: OTCharts candle timestamp is candle OPEN time.
CANDLE_TIMESTAMP_IS_OPEN = True

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
    "User-Agent": "ROMARIO-TRADER-CLEAN-1.2"
})

api_requests = 0


# ============================================================
# TIME HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_et():
    return datetime.now(TZ)


def parse_timestamp(value):
    """
    Supports Unix seconds, Unix milliseconds,
    ISO timestamps and datetime strings.
    """

    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            stamp = float(value)

            if stamp > 1000000000000:
                stamp /= 1000

            return datetime.fromtimestamp(
                stamp,
                timezone.utc
            )

        text = str(value).strip()

        if not text:
            return None

        try:
            stamp = float(text)

            if stamp > 1000000000000:
                stamp /= 1000

            return datetime.fromtimestamp(
                stamp,
                timezone.utc
            )

        except ValueError:
            pass

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        parsed = datetime.fromisoformat(text)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, OverflowError):
        return None


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

    return result


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

        if payout is None or payout < MIN_PAYOUT:
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

            if h < l or h < max(o, c) or l > min(o, c):
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

    # Sort chronologically if timestamps are readable.
    if candles and all(
        parse_timestamp(c["timestamp"]) is not None
        for c in candles
    ):
        candles.sort(
            key=lambda c: parse_timestamp(c["timestamp"])
        )

    # Remove duplicate timestamps.
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


def get_closed_candles(candles):
    """
    Keep only candles whose full 60-second period
    has ended.

    This assumes OTCharts timestamps represent
    the candle opening time.
    """

    if not CANDLE_TIMESTAMP_IS_OPEN:
        return []

    current_time = now_utc()
    closed = []

    for candle in candles:
        candle_start = parse_timestamp(
            candle["timestamp"]
        )

        if candle_start is None:
            continue

        candle_close = candle_start + timedelta(
            seconds=TIMEFRAME
        )

        if candle_close <= current_time:
            item = dict(candle)
            item["close_time"] = candle_close
            closed.append(item)

    return closed


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
# QUALITY SCORE
# ============================================================

def calculate_quality_score(
    direction,
    ema9,
    ema21,
    ema50,
    rsi_value,
    atr_value,
    near_ema,
    body_ratio
):
    if atr_value <= 0:
        return 0

    trend_strength = (
        abs(ema9 - ema21) +
        abs(ema21 - ema50)
    ) / atr_value

    trend_score = min(
        25,
        trend_strength * 12.5
    )

    pullback_score = max(
        0,
        20 * (
            1 - near_ema / (atr_value * 0.40)
        )
    )

    target_rsi = 59 if direction == "CALL" else 41

    rsi_score = max(
        0,
        15 * (
            1 - abs(rsi_value - target_rsi) / 18
        )
    )

    candle_score = min(
        20,
        body_ratio * 20
    )

    volatility_score = 10

    score = (
        trend_score
        + pullback_score
        + rsi_score
        + candle_score
        + volatility_score
        + 10
    )

    return round(min(100, score))


# ============================================================
# EMA PULLBACK STRATEGY
# ============================================================

def analyze_candles(candles, payout):
    if len(candles) < MIN_CANDLES:
        return None

    # All candles passed here must already be closed.
    closes = [
        candle["close"]
        for candle in candles
    ]

    current = candles[-1]
    previous = candles[-2]

    ema9 = ema(closes, EMA_FAST)
    ema21 = ema(closes, EMA_MID)
    ema50 = ema(closes, EMA_SLOW)

    rsi_value = rsi(closes, RSI_PERIOD)
    atr_value = atr(candles, ATR_PERIOD)

    if any(value is None for value in (
        ema9, ema21, ema50, rsi_value, atr_value
    )):
        return None

    if atr_value <= 0 or current["close"] <= 0:
        return None

    atr_ratio = atr_value / current["close"]

    if atr_ratio < 0.00005 or atr_ratio > 0.003:
        return None

    if payout < MIN_PAYOUT:
        return None

    candle_range = current["high"] - current["low"]

    if candle_range <= 0:
        return None

    body_ratio = (
        abs(current["close"] - current["open"])
        / candle_range
    )

    direction = None
    near_ema = None

    # ---------------- CALL ----------------

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

    # ---------------- PUT ----------------

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

    score = calculate_quality_score(
        direction,
        ema9,
        ema21,
        ema50,
        rsi_value,
        atr_value,
        near_ema,
        body_ratio
    )

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
        "close_time": current["close_time"],
        "reasons": [
            "EMA trend aligned",
            "Pullback confirmed",
            "RSI momentum confirmed",
            "Closed candle confirmed",
            "ATR volatility valid",
            "Payout passed"
        ]
    }


# ============================================================
# SIGNAL LOG
# ============================================================

def save_signal(symbol, payout, analysis, sent_time, entry_time):
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
                "signal_sent",
                "symbol",
                "direction",
                "payout",
                "score",
                "rsi",
                "entry_time",
                "status"
            ])

        writer.writerow([
            sent_time.isoformat(),
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
    sent_time = now_et()

    candle_close = analysis["close_time"].astimezone(TZ)

    # Entry is 15 seconds after the confirmed candle closes.
    entry_time = candle_close + timedelta(
        seconds=ENTRY_DELAY
    )

    if entry_time <= sent_time + timedelta(
        seconds=MIN_ENTRY_LEAD
    ):
        logging.warning(
            "%s: entry time is too close or already passed.",
            symbol
        )
        return None, None, None

    if analysis["direction"] == "CALL":
        direction_text = "🟢 CALL"
    else:
        direction_text = "🔴 PUT"

    quality = (
        "HIGH"
        if analysis["score"] >= 85
        else "GOOD"
    )

    message = (
        "🤖 ROMARIO TRADER\n\n"
        f"{direction_text} | #{symbol}\n"
        f"⭐ QUALITY: {quality}\n"
        "⏱ TIMEFRAME: 1M\n"
        "⌛ EXPIRATION: 1M\n"
        f"💰 PAYOUT: {payout:.1f}%\n"
        f"📩 SIGNAL SENT: {sent_time.strftime('%H:%M:%S')} ET\n"
        f"🎯 ENTRY: {entry_time.strftime('%H:%M:%S')} ET\n\n"
        "EMA + PULLBACK + RSI CONFIRMED\n"
        "⚠️ Quality is a technical score, not win probability."
    )

    return message, sent_time, entry_time


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
    logging.info("ENTRY DELAY: %s seconds after candle close", ENTRY_DELAY)
    logging.info("MAX SIGNALS PER RUN: 1")
    logging.info("=" * 55)

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

    logging.info("Markets selected: %s", len(markets))

    for market in markets:
        logging.info(
            "WATCH: %s | PAYOUT: %.1f%%",
            market["symbol"],
            market["payout"]
        )

    start_time = time.time()
    scanned_candles = {}

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

                closed = get_closed_candles(candles)

                if len(closed) < MIN_CANDLES:
                    logging.info(
                        "%s: not enough confirmed closed candles (%s)",
                        symbol,
                        len(closed)
                    )
                    continue

                current_closed = closed[-1]
                candle_key = str(
                    current_closed["timestamp"]
                )

                if scanned_candles.get(symbol) == candle_key:
                    continue

                scanned_candles[symbol] = candle_key

                analysis = analyze_candles(
                    closed,
                    payout
                )

                if analysis is None:
                    logging.info(
                        "%s: no valid setup",
                        symbol
                    )
                    continue

                message, sent_time, entry_time = build_signal(
                    symbol,
                    payout,
                    analysis
                )

                if message is None:
                    logging.info(
                        "%s: signal too late for planned entry",
                        symbol
                    )
                    continue

                logging.info(
                    "VALID SIGNAL: %s %s SCORE=%s",
                    symbol,
                    analysis["direction"],
                    analysis["score"]
                )

                telegram_send(message)

                save_signal(
                    symbol,
                    payout,
                    analysis,
                    sent_time,
                    entry_time
                )

                logging.info(
                    "Telegram signal sent successfully. Stopping run."
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
        "Scan ended: no valid setup found."
    )

    # No fabricated WIN/LOSS message.
    return


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        logging.info("Bot stopped manually.")

    except Exception:
        logging.exception("Fatal bot error")
