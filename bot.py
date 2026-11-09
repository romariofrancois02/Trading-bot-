# ============================================================
# ROMARIO TRADER PRO 7.5
# Pocket Option OTC | M1 | 60-second expiration
# Telegram alerts only — no automatic trading
# ============================================================

import os
import time
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---------------- SETTINGS ----------------

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 60
CANDLE_LIMIT = 150
OTC_CLOCK_OFFSET = 7200

MIN_PAYOUT = 92.0
MIN_VOTES = 2
EXPIRATION = 60
MAX_PAIRS = 10

FLORIDA = ZoneInfo("America/New_York")

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
    "User-Agent": "RomarioTrader/7.5",
    "Accept": "application/json"
})


# ---------------- TIME ----------------

def florida_now():
    return datetime.now(FLORIDA)


def get_entry_time():
    now = florida_now()
    entry = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return entry.strftime("%I:%M:%S %p")


# ---------------- TELEGRAM ----------------

def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram secrets are missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=20
        )

        result = response.json()

        if response.ok and result.get("ok"):
            logging.info("Telegram message sent.")
            return True

        logging.error("Telegram error: %s", result)
        return False

    except Exception as error:
        logging.error("Telegram connection error: %s", error)
        return False


# ---------------- API ----------------

def api_get(endpoint, params=None):
    url = BASE_URL + endpoint

    for attempt in range(3):
        try:
            response = session.get(
                url,
                params=params,
                timeout=25
            )

            if response.status_code == 200:
                return response.json()

            logging.warning(
                "API %s: HTTP %s | %s",
                endpoint,
                response.status_code,
                response.text[:250]
            )

            if response.status_code in (401, 402, 403, 429):
                raise RuntimeError(
                    f"OTCharts refused request: HTTP {response.status_code}"
                )

        except requests.RequestException as error:
            logging.warning("API attempt %s failed: %s", attempt + 1, error)

        if attempt < 2:
            time.sleep(2)

    return None


def extract_list(data, possible_keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in possible_keys:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


# ---------------- SYMBOLS ----------------

def is_forex_otc(symbol):
    if not isinstance(symbol, str):
        return False

    if not symbol.endswith("_otc"):
        return False

    pair = symbol[:-4]

    if pair.startswith("#"):
        return False

    if len(pair) != 6 or not pair.isalpha():
        return False

    return True


def get_pairs():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    if not data:
        raise RuntimeError("Could not retrieve OTC symbols.")

    symbols = extract_list(data, ("symbols", "data"))

    pairs = []

    for item in symbols:
        if not isinstance(item, dict):
            continue

        symbol = item.get("symbol")

        if not is_forex_otc(symbol):
            continue

        if item.get("isOtc") is not True:
            continue

        if item.get("open") is not True:
            continue

        payout = item.get("payout")

        try:
            payout = float(payout)
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        pairs.append({
            "symbol": symbol,
            "payout": payout
        })

    pairs.sort(key=lambda x: x["payout"], reverse=True)

    return pairs[:MAX_PAIRS]


# ---------------- CANDLES ----------------

def get_candles(symbol):
    data = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    if not data:
        return []

    rows = extract_list(data, ("candles", "data"))

    candles = []

    for candle in rows:
        if not isinstance(candle, dict):
            continue

        try:
            candle_time = int(float(candle["time"]))

            open_price = float(candle["open"])
            high_price = float(candle["high"])
            low_price = float(candle["low"])
            close_price = float(candle["close"])

            if min(open_price, high_price, low_price, close_price) <= 0:
                continue

            candles.append({
                "time": candle_time,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price
            })

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    # Remove duplicate candle timestamps.
    unique = {}
    for candle in candles:
        unique[candle["time"]] = candle

    candles = list(unique.values())
    candles.sort(key=lambda x: x["time"])

    # Pocket Option OTC timestamps are UTC+2.
    # Remove the offset before checking candle completion.
    now_utc = time.time()

    completed = [
        candle for candle in candles
        if candle["time"] - OTC_CLOCK_OFFSET + TIMEFRAME <= now_utc
    ]

    return completed


# ---------------- INDICATORS ----------------

def ema(values, period):
    if len(values) < period:
        return []

    result = [None] * (period - 1)

    current = sum(values[:period]) / period
    result.append(current)

    multiplier = 2 / (period + 1)

    for value in values[period:]:
        current = (
            value * multiplier
            + current * (1 - multiplier)
        )
        result.append(current)

    return result


def rsi(values, period=7):
    if len(values) < period + 2:
        return None, None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    previous_rsi = None
    current_rsi = None

    for i in range(period, len(gains)):
        if avg_loss == 0:
            value = 100.0
        else:
            rs = avg_gain / avg_loss
            value = 100 - (100 / (1 + rs))

        previous_rsi = current_rsi
        current_rsi = value

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    return previous_rsi, current_rsi


def bollinger(values, period=20, multiplier=2):
    if len(values) < period:
        return None

    recent = values[-period:]
    middle = sum(recent) / period

    variance = sum(
        (value - middle) ** 2 for value in recent
    ) / period

    deviation = variance ** 0.5

    return (
        middle - multiplier * deviation,
        middle,
        middle + multiplier * deviation
    )


def stochastic(candles, period=14, smooth=3):
    if len(candles) < period + smooth + 2:
        return None, None

    k_values = []

    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1:i + 1]

        highest = max(c["high"] for c in window)
        lowest = min(c["low"] for c in window)
        close = candles[i]["close"]

        if highest == lowest:
            k = 50.0
        else:
            k = 100 * (close - lowest) / (highest - lowest)

        k_values.append(k)

    d_values = []

    for i in range(smooth - 1, len(k_values)):
        d_values.append(
            sum(k_values[i - smooth + 1:i + 1]) / smooth
        )

    if len(k_values) < 2 or len(d_values) < 2:
        return None, None

    # Align the K and D values to the same candle.
    return k_values[-2:], d_values[-2:]


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    if not fast or not slow:
        return None, None

    line = []

    for i in range(len(values)):
        if fast[i] is None or slow[i] is None:
            line.append(None)
        else:
            line.append(fast[i] - slow[i])

    valid_line = [x for x in line if x is not None]

    signal_values = ema(valid_line, 9)

    if len(signal_values) < 2:
        return None, None

    line_previous = valid_line[-2]
    line_current = valid_line[-1]
    signal_previous = signal_values[-2]
    signal_current = signal_values[-1]

    return (
        (line_previous, line_current),
        (signal_previous, signal_current)
    )


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        ranges.append(true_range)

    return sum(ranges[-period:]) / period


# ---------------- STRATEGY VOTES ----------------

def analyze(candles):
    if len(candles) < 60:
        return None

    closes = [c["close"] for c in candles]

    previous = candles[-2]
    latest = candles[-1]

    votes = {
        "CALL": 0,
        "PUT": 0
    }

    reasons = []

    # 1. RSI reversal
    rsi_previous, rsi_current = rsi(closes, 7)

    if rsi_previous is not None and rsi_current is not None:
        if rsi_previous <= 30 and rsi_current > 30:
            votes["CALL"] += 1
            reasons.append("RSI reversal CALL")

        elif rsi_previous >= 70 and rsi_current < 70:
            votes["PUT"] += 1
            reasons.append("RSI reversal PUT")

    # 2. EMA 9 / EMA 21 crossover
    fast = ema(closes, 9)
    middle = ema(closes, 21)

    if fast and middle:
        if (
            fast[-2] is not None and middle[-2] is not None
            and fast[-1] is not None and middle[-1] is not None
        ):
            if fast[-2] <= middle[-2] and fast[-1] > middle[-1]:
                votes["CALL"] += 1
                reasons.append("EMA cross CALL")

            elif fast[-2] >= middle[-2] and fast[-1] < middle[-1]:
                votes["PUT"] += 1
                reasons.append("EMA cross PUT")

    # 3. Bollinger Band bounce
    bands_previous = bollinger(closes[:-1], 20)
    bands_current = bollinger(closes, 20)

    if bands_previous and bands_current:
        lower_previous = bands_previous[0]
        upper_previous = bands_previous[2]

        lower_current = bands_current[0]
        upper_current = bands_current[2]

        if (
            previous["close"] <= lower_previous
            and latest["close"] > lower_current
        ):
            votes["CALL"] += 1
            reasons.append("Bollinger bounce CALL")

        elif (
            previous["close"] >= upper_previous
            and latest["close"] < upper_current
        ):
            votes["PUT"] += 1
            reasons.append("Bollinger bounce PUT")

    # 4. MACD crossover
    macd_result = macd(closes)

    if macd_result:
        line, signal = macd_result

        if line[0] <= signal[0] and line[1] > signal[1]:
            votes["CALL"] += 1
            reasons.append("MACD cross CALL")

        elif line[0] >= signal[0] and line[1] < signal[1]:
            votes["PUT"] += 1
            reasons.append("MACD cross PUT")

    # 5. Stochastic crossover
    stoch_result = stochastic(candles)

    if stoch_result:
        k_values, d_values = stoch_result

        if (
            k_values[0] <= d_values[0]
            and k_values[1] > d_values[1]
            and k_values[1] < 35
        ):
            votes["CALL"] += 1
            reasons.append("Stochastic CALL")

        elif (
            k_values[0] >= d_values[0]
            and k_values[1] < d_values[1]
            and k_values[1] > 65
        ):
            votes["PUT"] += 1
            reasons.append("Stochastic PUT")

    # 6. ATR trend direction proxy
    current_atr = atr(candles)

    if current_atr:
        midpoint = (
            latest["high"] + latest["low"]
        ) / 2

        if (
            latest["close"] > midpoint
            and latest["close"] > previous["close"]
            and latest["close"] > closes[-2]
        ):
            votes["CALL"] += 1
            reasons.append("ATR trend CALL")

        elif (
            latest["close"] < midpoint
            and latest["close"] < previous["close"]
            and latest["close"] < closes[-2]
        ):
            votes["PUT"] += 1
            reasons.append("ATR trend PUT")

    # 7. RSI + EMA combination
    if (
        rsi_current is not None
        and fast and middle
        and fast[-1] is not None
        and middle[-1] is not None
    ):
        if rsi_current > 50 and fast[-1] > middle[-1]:
            votes["CALL"] += 1
            reasons.append("RSI/EMA combo CALL")

        elif rsi_current < 50 and fast[-1] < middle[-1]:
            votes["PUT"] += 1
            reasons.append("RSI/EMA combo PUT")

    # 8. Triple EMA alignment
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)

    if ema9 and ema21 and ema50:
        if (
            ema9[-1] is not None
            and ema21[-1] is not None
            and ema50[-1] is not None
        ):
            if (
                ema9[-1] > ema21[-1] > ema50[-1]
            ):
                votes["CALL"] += 1
                reasons.append("Triple EMA CALL")

            elif (
                ema9[-1] < ema21[-1] < ema50[-1]
            ):
                votes["PUT"] += 1
                reasons.append("Triple EMA PUT")

    # Direction confirmation: last completed candle.
    candle_direction = None

    if latest["close"] > latest["open"]:
        candle_direction = "CALL"
    elif latest["close"] < latest["open"]:
        candle_direction = "PUT"

    if candle_direction == "CALL":
        votes["PUT"] = 0
    elif candle_direction == "PUT":
        votes["CALL"] = 0
    else:
        return None

    if votes["CALL"] >= MIN_VOTES and votes["CALL"] > votes["PUT"]:
        direction = "CALL"
    elif votes["PUT"] >= MIN_VOTES and votes["PUT"] > votes["CALL"]:
        direction = "PUT"
    else:
        return None

    return {
        "direction": direction,
        "score": votes[direction],
        "reasons": reasons
    }


# ---------------- MAIN SCANNER ----------------

def main():
    print("=" * 55)
    print("ROMARIO TRADER PRO 7.5")
    print("POCKET OPTION OTC SCANNER")
    print("=" * 55)

    if not API_KEY:
        raise RuntimeError("Missing OTCHARTS_API_KEY secret.")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing Telegram secrets.")

    print("Florida time:", florida_now().strftime("%Y-%m-%d %I:%M:%S %p"))
    print("Timeframe: M1")
    print("Expiration:", EXPIRATION, "seconds")
    print("Minimum payout:", MIN_PAYOUT)

    pairs = get_pairs()

    print("Pairs with payout >= minimum:", len(pairs))

    candidates = []

    for index, pair in enumerate(pairs, start=1):
        symbol = pair["symbol"]
        payout = pair["payout"]

        print(
            f"[{index}/{len(pairs)}] Scanning {symbol} "
            f"| Payout: {payout}%"
        )

        try:
            candles = get_candles(symbol)

            print("Completed candles:", len(candles))

            if len(candles) < 60:
                print("Skipped: not enough completed candles.")
                continue

            result = analyze(candles)

            if not result:
                print("No valid setup.")
                continue

            print(
                "VALID:",
                result["direction"],
                symbol,
                result["score"],
                "/8"
            )

            candidates.append({
                "symbol": symbol,
                "payout": payout,
                "direction": result["direction"],
                "score": result["score"],
                "reasons": result["reasons"]
            })

        except Exception as error:
            print("Pair error:", symbol, error)

    # Highest score first, then highest payout.
    candidates.sort(
        key=lambda x: (x["score"], x["payout"]),
        reverse=True
    )

    florida_time = florida_now().strftime("%Y-%m-%d %I:%M:%S %p")

    if not candidates:
        message = (
            "🤖 ROMARIO TRADER PRO 7.5\n\n"
            "⚪ NO VALID SIGNAL\n"
            "No pair passed the setup rules.\n\n"
            f"Pairs checked: {len(pairs)}\n"
            f"Minimum payout: {MIN_PAYOUT}%\n"
            "Timeframe: M1\n"
            f"🕒 FLORIDA TIME: {florida_time}"
        )

        print(message)
        telegram_send(message)
        return

    best = candidates[0]

    # Calculate entry time after scanning, immediately before sending.
    entry_time = get_entry_time()

    if best["direction"] == "CALL":
        signal_icon = "🟢"
    else:
        signal_icon = "🔴"

    reason_text = "\n".join(
        f"• {reason}" for reason in best["reasons"]
        if reason.endswith(best["direction"])
    )

    message = (
        "🤖 ROMARIO TRADER PRO 7.5\n\n"
        f"{signal_icon} {best['direction']}\n\n"
        f"💱 PAIR: {best['symbol']}\n"
        f"💰 PAYOUT: {best['payout']:.1f}%\n"
        f"📊 SETUP SCORE: {best['score']}/8\n"
        "⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: {EXPIRATION} SECONDS\n"
        f"🎯 ENTRY TIME: {entry_time} (Florida)\n\n"
        "📌 CONFIRMATIONS:\n"
        f"{reason_text if reason_text else '• Setup confirmed'}\n\n"
        f"🕒 FLORIDA TIME: {florida_time}\n\n"
        "⚠️ Signal only — not a guaranteed result."
    )

    print(message)
    telegram_send(message)


# ---------------- RUN ----------------

if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        logging.exception("BOT FAILED")

        error_message = (
            "🤖 ROMARIO TRADER PRO 7.5\n\n"
            "⚠️ BOT ERROR\n"
            f"{str(error)[:500]}\n\n"
            f"🕒 FLORIDA TIME: "
            f"{florida_now().strftime('%Y-%m-%d %I:%M:%S %p')}"
        )

        telegram_send(error_message)
        raise
