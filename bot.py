
import os
import time
import logging
import requests
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================
# ROMARIO TRADER PRO - CLEAN BUILD 1.0
# M1 | EXPIRATION 60s | EMA PULLBACK | TELEGRAM
# ONE SIGNAL PER RUN | OTCHARTS DATA
# ============================================================

VERSION = "ROMARIO TRADER PRO CLEAN 1.0"

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
MAX_SCAN_MINUTES = 120

ENTRY_DELAY = 10
MAX_ENTRY_DELAY = 15

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 7
ATR_PERIOD = 14

MAX_CANDLE_ATR = 1.50
MIN_BODY_RATIO = 0.35

COOLDOWN_SECONDS = 60
MAX_API_REQUESTS = 5000

TZ = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ============================================================
# ENVIRONMENT
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
    "User-Agent": "ROMARIO-TRADER-CLEAN-1.0"
})

api_requests = 0
last_signal_time = 0
last_signal_symbol = None

# ============================================================
# API
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
        raise RuntimeError("OTCharts rate limit reached (429)")

    response.raise_for_status()
    return response.json()


def unwrap_data(data):
    if isinstance(data, dict):
        for key in ("data", "results", "result"):
            if key in data:
                return data[key]
    return data


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message
    }

    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram delivery failed")

    return True


# ============================================================
# DATA NORMALIZATION
# ============================================================

def get_value(item, keys, default=None):
    if not isinstance(item, dict):
        return default

    for key in keys:
        if key in item and item[key] is not None:
            return item[key]

    return default


def normalize_symbol(item):
    if isinstance(item, str):
        return item

    if not isinstance(item, dict):
        return None

    return get_value(
        item,
        ["symbol", "name", "ticker", "instrument"],
        None
    )


def normalize_payout(item):
    if not isinstance(item, dict):
        return None

    value = get_value(
        item,
        ["payout", "payout_percent", "profit", "return", "profit_percent"],
        None
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
):
        return True

    value = get_value(item, ["open", "is_open", "active"], True)

    if isinstance(value, str):
        return value.lower() not in ("false", "closed", "0", "no")

    return bool(value)


def get_symbols():
    raw = api_get("/v1/symbols", {"venue": VENUE})
    data = unwrap_data(raw)

    if isinstance(data, dict):
        data = data.get("symbols", data.get("instruments", []))

    if not isinstance(data, list):
        raise RuntimeError("Unexpected OTCharts symbols format")

    found = []

    for item in data:
        symbol = normalize_symbol(item)

        if not symbol:
            continue

        # Forex OTC symbols only
        if not symbol.lower().endswith("_otc"):
            continue

        payout = normalize_payout(item)

        if payout is None or payout < MIN_PAYOUT:
            continue

        if not symbol_is_open(item):
            continue

        found.append({
            "symbol": symbol,
            "payout": payout
        })

    found.sort(key=lambda x: x["payout"], reverse=True)

    return found[:MAX_SYMBOLS]


# ============================================================
# CANDLES
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
        data = data.get("candles", data.get("history", []))

    if not isinstance(data, list):
        return []

    candles = []

    for c in data:
        if not isinstance(c, dict):
            continue

        try:
            o = float(get_value(c, ["open", "o"]))
            h = float(get_value(c, ["high", "h"]))
            l = float(get_value(c, ["low", "l"]))
            cl = float(get_value(c, ["close", "c"]))

            ts = get_value(
                c,
                ["timestamp", "time", "ts", "datetime"],
                None
            )

            candles.append({
                "open": o,
                "high": h,
                "low": l,
                "close": cl,
                "timestamp": ts
            })

        except (ValueError, TypeError):
            continue

    candles.sort(key=lambda c: str(c["timestamp"] or ""))

    # Remove duplicate candles
    unique = []
    seen = set()

    for c in candles:
        key = str(c["timestamp"])
        if key not in seen:
            unique.append(c)
            seen.add(key)

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
        result = (value * multiplier) + (result * (1 - multiplier))

    return result


def rsi(values, period=7):
    if len(values) < period + 2:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):
        c = candles[i]
        previous_close = candles[i - 1]["close"]

        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - previous_close),
            abs(c["low"] - previous_close)
        )

        trs.append(tr)

    return sum(trs[-period:]) / period


# ============================================================
# CANDLE QUALITY
# ============================================================

def candle_quality(current, previous, atr_value, direction):
    if atr_value is None or atr_value <= 0:
        return False

    candle_range = current["high"] - current["low"]
    body = abs(current["close"] - current["open"])

    if candle_range <= 0:
        return False

    body_ratio = body / candle_range

    # Avoid very weak candles
    if body_ratio < MIN_BODY_RATIO:
        return False

    # Avoid unusually large candles / chasing
    if candle_range > atr_value * MAX_CANDLE_ATR:
        return False

    # Direction must be confirmed by closed candle
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
# STRATEGY: EMA TREND + PULLBACK + MOMENTUM
# ============================================================

def analyze_candles(candles):
    if len(candles) < MIN_CANDLES:
        return None

    # Use CLOSED candles only.
    # Exclude the newest candle because it may still be forming.
    closed = candles[:-1]

    if len(closed) < MIN_CANDLES:
        return None

    closes = [c["close"] for c in closed]

    current = closed[-1]
    previous = closed[-2]

    e9 = ema(closes, EMA_FAST)
    e21 = ema(closes, EMA_MID)
    e50 = ema(closes, EMA_SLOW)

    rsi_value = rsi(closes, RSI_PERIOD)
    atr_value = atr(closed, ATR_PERIOD)

    if None in (e9, e21, e50, rsi_value, atr_value):
        return None

    if atr_value <= 0:
        return None

    # No flat EMA stack
    if abs(e9 - e21) < atr_value * 0.05:
        return None

    # No low-volatility or extreme-volatility market
    atr_ratio = atr_value / current["close"]

    if atr_ratio < 0.00005 or atr_ratio > 0.003:
        return None

    direction = None
    score = 0
    reasons = []

    # ---------------- CALL ----------------
    if e9 > e21 > e50:

        # Pullback must be close to EMA9 or EMA21
        near_ema = min(
            abs(current["low"] - e9),
            abs(current["low"] - e21),
            abs(current["close"] - e9),
            abs(current["close"] - e21)
        )

        pullback_ok = near_ema <= atr_value * 0.40

        # RSI must support upward momentum
        rsi_ok = 50 <= rsi_value <= 68

        candle_ok = candle_quality(
            current, previous, atr_value, "CALL"
        )

        # Avoid chasing price too far above EMA9
        chase_ok = current["close"] <= e9 + atr_value * 0.65

        if pullback_ok and rsi_ok and candle_ok and chase_ok:
            direction = "CALL"
            score = 25 + 20 + 20 + 15 + 10 + 10
            reasons = [
                "EMA bullish",
                "Pullback confirmed",
                "RSI momentum",
                "Candle confirmed",
                "Volatility valid",
                "Payout valid"
            ]

    # ---------------- PUT ----------------
    elif e9 < e21 < e50:

        near_ema = min(
            abs(current["high"] - e9),
            abs(current["high"] - e21),
            abs(current["close"] - e9),
            abs(current["close"] - e21)
        )

        pullback_ok = near_ema <= atr_value * 0.40

        rsi_ok = 32 <= rsi_value <= 50

        candle_ok = candle_quality(
            current, previous, atr_value, "PUT"
        )

        chase_ok = current["close"] >= e9 - atr_value * 0.65

        if pullback_ok and rsi_ok and candle_ok and chase_ok:
            direction = "PUT"
            score = 25 + 20 + 20 + 15 + 10 + 10
            reasons = [
                "EMA bearish",
                "Pullback confirmed",
                "RSI momentum",
                "Candle confirmed",
                "Volatility valid",
                "Payout valid"
            ]

    if direction is None:
        return None

    if score < MIN_SCORE:
        return None

    return {
        "direction": direction,
        "score": score,
        "rsi": round(rsi_value, 2),
        "ema9": e9,
        "ema21": e21,
        "ema50": e50,
        "atr": atr_value,
        "candle": current,
        "reasons": reasons
    }


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal(symbol, payout, analysis):
    now = datetime.now(TZ)

    entry_time = now + timedelta(seconds=ENTRY_DELAY)
    expiration_time = entry_time + timedelta(seconds=EXPIRATION)

    direction_emoji = (
        "🟢 CALL"
        if analysis["direction"] == "CALL"
        else "🔴 PUT"
    )

    quality = (
        "HIGH"
        if analysis["score"] >= 90
        else "GOOD"
    )

    message = (
        "🤖 ROMARIO TRADER\n\n"
        f"{direction_emoji} | {symbol}\n"
        f"⭐ QUALITY: {quality} ({analysis['score']}/100)\n"
        f"⏱ TF: 1M | EXP: 1M\n"
        f"💰 PAYOUT: {payout:.1f}%\n"
        f"🎯 ENTRY: {entry_time.strftime('%H:%M:%S')} ET\n"
        f"⌛ EXP: {expiration_time.strftime('%H:%M:%S')} ET\n\n"
        "EMA + PULLBACK + RSI CONFIRMED\n"
        "⚠️ Signal is not a guaranteed win."
    )

    return message, entry_time


# ============================================================
# SCAN
# ============================================================

def main():
    global last_signal_time, last_signal_symbol

    logging.info("=" * 50)
    logging.info("%s starting", VERSION)
    logging.info("TIMEFRAME=M1 | EXP=60s | PAYOUT>=85")
    logging.info("EMA9/21/50 | RSI7 | ATR14")
    logging.info("ONE SIGNAL PER RUN")
    logging.info("=" * 50)

    try:
        markets = get_symbols()
    except Exception as e:
        logging.exception("Could not load symbols: %s", e)
        telegram_send("ROMARIO TRADER: OTCharts pa kapab chaje mache yo.")
        return

    if not markets:
        logging.warning("No OTC symbols passed payout/open filters.")
        telegram_send(
            "ROMARIO TRADER\n"
            "Pa gen mache OTC ki pase filtè payout 85% la."
        )
        return

    logging.info("Markets selected: %s", len(markets))

    for item in markets:
        logging.info(
            "WATCH: %s | payout %.1f%%",
            item["symbol"],
            item["payout"]
        )

    start_time = time.time()
    scanned_candles = {}

    while (time.time() - start_time) < MAX_SCAN_MINUTES * 60:

        for market in markets:

            symbol = market["symbol"]
            payout = market["payout"]

            try:
                candles = get_candles(symbol)

                if len(candles) < MIN_CANDLES:
                    logging.info("%s: insufficient candles", symbol)
                    continue

                # Only analyze when the latest CLOSED candle changes.
                closed_key = str(candles[-2]["timestamp"])

                if scanned_candles.get(symbol) == closed_key:
                    continue

                scanned_candles[symbol] = closed_key

                analysis = analyze_candles(candles)

                if not analysis:
                    continue

                # Cooldown / single-signal protection
                if time.time() - last_signal_time < COOLDOWN_SECONDS:
                    continue

                message, entry_time = build_signal(
                    symbol, payout, analysis
                )

                logging.info(
                    "SIGNAL: %s %s SCORE=%s",
                    symbol,
                    analysis["direction"],
                    analysis["score"]
                )

                # Send only ONE signal and stop this run.
                telegram_send(message)

                last_signal_time = time.time()
                last_signal_symbol = symbol

                logging.info("Signal sent. Stopping run.")
                return

            except requests.RequestException as e:
                logging.warning("%s API request error: %s", symbol, e)
                continue

            except Exception as e:
                logging.exception("%s analysis failed: %s", symbol, e)
                continue

        logging.info("No valid setup. Waiting %s seconds.", SCAN_INTERVAL)
        time.sleep(SCAN_INTERVAL)

    logging.info("Scan timeout: no valid signal found.")

    # No signal message sent when nothing passes.
    return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Bot stopped manually.")
    except Exception:
        logging.exception("Fatal error")
