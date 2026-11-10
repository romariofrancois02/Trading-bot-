
import os
import time
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# ==================================================
# ROMARIO TRADER PRO 9.9
# OTCharts | OTC ONLY | M1 | TELEGRAM ALERTS
# ==================================================

BASE_URL = "https://otcharts.com"
TIMEFRAME = 60
CANDLE_LIMIT = 120
MIN_PAYOUT = 92.0
TOLERANCE = 10.0
EXPIRATION = 60
SIGNAL_LEAD_SECONDS = 15

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

FLORIDA = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "ROMARIO-TRADER-PRO/9.9"
})

# --------------------------------------------------
# STOCHASTIC PAIRS PROVIDED BY YOU
# --------------------------------------------------

CALL_PAIRS = [
    (50, 63), (73, 63), (27, 43),
    (24, 45), (37, 45), (43, 36),
    (49, 35), (42, 18), (44, 16),
    (78, 76), (42, 21), (20, 31),
    (42, 63)
]

PUT_PAIRS = [
    (54, 70), (51, 56), (74, 81),
    (48, 71), (28, 41), (78, 53),
    (70, 53), (70, 57), (68, 37),
    (51, 44), (69, 54)
]


# --------------------------------------------------
# TIME
# --------------------------------------------------

def florida_now():
    return datetime.now(FLORIDA)


def format_time(timestamp):
    return datetime.fromtimestamp(
        timestamp, FLORIDA
    ).strftime("%Y-%m-%d %I:%M:%S %p")


# --------------------------------------------------
# HTTP
# --------------------------------------------------

def api_get(endpoint, params=None):
    url = BASE_URL + endpoint

    for attempt in range(3):
        try:
            response = SESSION.get(
                url,
                params=params,
                timeout=20
            )

            response.raise_for_status()
            data = response.json()

            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(str(data["error"]))

            return data

        except Exception as exc:
            logging.warning(
                "API attempt %s failed: %s",
                attempt + 1, exc
            )
            time.sleep(2)

    return None


# --------------------------------------------------
# SYMBOLS
# --------------------------------------------------

def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    if not data:
        return []

    symbols = data.get("symbols", [])

    if not isinstance(symbols, list):
        return []

    eligible = []

    for item in symbols:
        if not isinstance(item, dict):
            continue

        symbol = str(
            item.get("symbol", "")
        )

        # OTC ONLY. Never scan REAL pairs.
        if not symbol.lower().endswith("_otc"):
            continue

        payout = item.get(
            "payout",
            item.get("payout_percent", 0)
        )

        try:
            payout = float(
                str(payout).replace("%", "")
            )
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        is_open = item.get("open", True)

        if is_open is False:
            continue

        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    return eligible


# --------------------------------------------------
# CANDLES
# --------------------------------------------------

def get_candles(symbol):
    data = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    if not data:
        return []

    rows = data.get("candles")

    if not isinstance(rows, list):
        rows = data.get("data", [])

    if not isinstance(rows, list):
        return []

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            candle = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            }

            raw_time = row.get(
                "time",
                row.get("timestamp")
            )

            if raw_time is not None:
                ts = float(raw_time)

                if ts > 100000000000:
                    ts /= 1000

                candle["time"] = ts

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    if len(candles) < 60:
        return []

    # Exclude a candle that has not finished yet.
    if "time" in candles[-1]:
        now = time.time()
        last_close_time = candles[-1]["time"] + TIMEFRAME

        if last_close_time > now:
            candles = candles[:-1]
    else:
        # If timestamps are unavailable, avoid using
        # the newest potentially unfinished candle.
        candles = candles[:-1]

    return candles


# --------------------------------------------------
# INDICATORS
# --------------------------------------------------

def ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)
    result = [sum(values[:period]) / period]

    for value in values[period:]:
        result.append(
            (value * multiplier) +
            (result[-1] * (1 - multiplier))
        )

    return result


def stochastic(candles, period=14, smooth=3):
    if len(candles) < period + smooth:
        return None, None

    raw_k = []

    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1:i + 1]

        highest = max(x["high"] for x in window)
        lowest = min(x["low"] for x in window)
        close = candles[i]["close"]

        if highest == lowest:
            raw_k.append(50.0)
        else:
            raw_k.append(
                ((close - lowest) /
                 (highest - lowest)) * 100
            )

    if len(raw_k) < smooth:
        return None, None

    k_values = []

    for i in range(smooth - 1, len(raw_k)):
        k_values.append(
            sum(raw_k[i - smooth + 1:i + 1]) / smooth
        )

    d_values = []

    for i in range(smooth - 1, len(k_values)):
        d_values.append(
            sum(k_values[i - smooth + 1:i + 1]) / smooth
        )

    if not d_values:
        return None, None

    return k_values[-1], d_values[-1]


# --------------------------------------------------
# MATCH YOUR STOCHASTIC NUMBERS
# --------------------------------------------------

def match_pair(k, d, direction):
    pairs = CALL_PAIRS if direction == "CALL" else PUT_PAIRS

    best_distance = None
    best_pair = None

    for target_k, target_d in pairs:
        distance_k = abs(k - target_k)
        distance_d = abs(d - target_d)

        # BOTH numbers must be close.
        if distance_k <= TOLERANCE and distance_d <= TOLERANCE:
            total_distance = distance_k + distance_d

            if best_distance is None or total_distance < best_distance:
                best_distance = total_distance
                best_pair = (target_k, target_d)

    if best_pair is None:
        return False, None

    return True, best_pair


# --------------------------------------------------
# SIGNAL CONFIRMATION
# --------------------------------------------------

def analyze_symbol(item):
    symbol = item["symbol"]
    payout = item["payout"]

    candles = get_candles(symbol)

    if len(candles) < 60:
        logging.info("%s: insufficient candles", symbol)
        return None

    closes = [c["close"] for c in candles]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)

    if not ema9 or not ema21 or not ema50:
        return None

    k, d = stochastic(candles)

    if k is None or d is None:
        return None

    last = candles[-1]
    previous = candles[-2]

    # CALL confirmation
    call_trend = (
        ema9[-1] > ema21[-1] > ema50[-1]
        and last["close"] > ema9[-1]
        and last["close"] > last["open"]
    )

    # PUT confirmation
    put_trend = (
        ema9[-1] < ema21[-1] < ema50[-1]
        and last["close"] < ema9[-1]
        and last["close"] < last["open"]
    )

    call_match, call_reference = match_pair(
        k, d, "CALL"
    )

    put_match, put_reference = match_pair(
        k, d, "PUT"
    )

    # Direction is NEVER inverted.
    if call_match and call_trend:
        direction = "CALL"
        reference = call_reference

    elif put_match and put_trend:
        direction = "PUT"
        reference = put_reference

    else:
        logging.info(
            "%s: no confirmation | K=%.2f D=%.2f",
            symbol, k, d
        )
        return None

    # Smaller distance to the reference pair is better.
    distance = (
        abs(k - reference[0]) +
        abs(d - reference[1])
    )

    return {
        "symbol": symbol,
        "direction": direction,
        "payout": payout,
        "k": k,
        "d": d,
        "reference": reference,
        "distance": distance
    }


# --------------------------------------------------
# ENTRY TIMING
# Send alert 15 seconds before candle opens.
# --------------------------------------------------

def wait_for_entry():
    now = time.time()

    entry_open = (int(now // 60) + 1) * 60
    send_at = entry_open - SIGNAL_LEAD_SECONDS

    # If scanning finished too late, use the next
    # available minute instead of sending a stale alert.
    if send_at <= now:
        entry_open += 60
        send_at += 60

    delay = send_at - time.time()

    if delay > 0:
        logging.info(
            "Waiting %.1f seconds for alert time",
            delay
        )
        time.sleep(delay)

    # Never send an alert after its entry candle opened.
    if time.time() >= entry_open:
        logging.warning("Entry time passed. Signal skipped.")
        return None

    return entry_open


# --------------------------------------------------
# TELEGRAM
# --------------------------------------------------

def send_telegram(message):
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
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=20
        )

        response.raise_for_status()
        result = response.json()

        return result.get("ok", False)

    except Exception as exc:
        logging.error("Telegram error: %s", exc)
        return False


def build_message(signal, entry_open):
    direction = signal["direction"]

    emoji = "🟢" if direction == "CALL" else "🔴"

    signal_time = format_time(time.time())
    entry_time = format_time(entry_open)

    return f"""
🤖 ROMARIO TRADER PRO 9.9

{emoji} {direction}

💱 PAIR: {signal['symbol']}
💰 PAYOUT: {signal['payout']:.1f}%

📊 STOCH K: {signal['k']:.2f}
📊 STOCH D: {signal['d']:.2f}

🎯 MATCHED PAIR: {signal['reference'][0]} / {signal['reference'][1]}
📏 DISTANCE: {signal['distance']:.2f}

⏱ TIMEFRAME: M1
⌛ EXPIRATION: 60 SECONDS

📨 SIGNAL TIME: {signal_time}
🎯 ENTRY TIME: {entry_time}

⚠️ Demo-test the signal before live trading.
""".strip()


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    logging.info("ROMARIO TRADER PRO 9.9 starting...")

    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram secrets are missing")

    symbols = get_symbols()

    logging.info(
        "Eligible OTC pairs with payout >= %.1f%%: %s",
        MIN_PAYOUT, len(symbols)
    )

    if not symbols:
        logging.warning("No eligible OTC symbols found.")
        send_telegram(
            "🤖 ROMARIO TRADER PRO 9.9\n\n"
            "⚪ NO VALID SIGNAL\n"
            "No eligible OTC pairs were returned."
        )
        return

    candidates = []

    for index, item in enumerate(symbols, start=1):
        logging.info(
            "Scanning %s (%s/%s)",
            item["symbol"], index, len(symbols)
        )

        try:
            signal = analyze_symbol(item)

            if signal:
                candidates.append(signal)
                logging.info(
                    "Candidate: %s %s | K %.2f D %.2f",
                    signal["direction"],
                    signal["symbol"],
                    signal["k"],
                    signal["d"]
                )

        except Exception:
            logging.exception(
                "Error analyzing %s",
                item["symbol"]
            )

    if not candidates:
        logging.info("No valid signal passed all filters.")

        send_telegram(
            "🤖 ROMARIO TRADER PRO 9.9\n\n"
            "⚪ NO VALID SIGNAL\n"
            "No pair passed your STOCHASTIC, EMA "
            "and candle confirmation rules.\n\n"
            "No signal was forced."
        )
        return

    # One best confirmed candidate.
    candidates.sort(
        key=lambda x: (
            x["distance"],
            -x["payout"]
        )
    )

    best = candidates[0]

    logging.info(
        "Best candidate: %s %s",
        best["direction"],
        best["symbol"]
    )

    entry_open = wait_for_entry()

    if entry_open is None:
        logging.warning("Signal skipped because entry time passed.")
        return

    message = build_message(best, entry_open)

    if send_telegram(message):
        logging.info("Telegram signal sent successfully.")
    else:
        logging.error("Telegram signal could not be sent.")


if __name__ == "__main__":
    main()
