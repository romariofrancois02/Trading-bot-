
import os
import time
import logging
import requests
import statistics
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================================================
# ROMARIO TRADER PRO 7.2
# 8 STRATEGIES | M1 | 60 SECONDS
# =========================================================

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
MIN_PAYOUT = 92.0
MIN_VOTES = 2
TIMEFRAME = 60
EXPIRATION = 60
CANDLE_LIMIT = 150

FLORIDA = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "X-API-Key": API_KEY,
    "Accept": "application/json"
})

# Forex fiat OTC only. Excludes crypto, stocks and indices.
FIAT_CODES = {
    "AED", "AUD", "BDT", "BHD", "CAD", "CHF", "CNY",
    "CZK", "DKK", "EGP", "EUR", "GBP", "HKD", "HUF",
    "IDR", "ILS", "INR", "JPY", "KES", "KRW", "KWD",
    "LBP", "MAD", "MXN", "MYR", "NOK", "NZD", "OMR",
    "PKR", "PLN", "QAR", "RON", "SAR", "SEK", "SGD",
    "THB", "TRY", "TWD", "UAH", "USD", "VND", "ZAR"
}


def api_get(path, params=None, attempts=3):
    if not API_KEY:
        raise RuntimeError("Missing OTCHARTS_API_KEY")

    url = BASE_URL + path

    for attempt in range(1, attempts + 1):
        try:
            response = session.get(
                url,
                params=params,
                timeout=20
            )
            response.raise_for_status()
            return response.json()

        except Exception as error:
            logging.warning(
                "API attempt %s/%s failed: %s",
                attempt, attempts, error
            )
            if attempt < attempts:
                time.sleep(attempt * 2)

    raise RuntimeError("OTCharts request failed after retries")


def extract_list(data, possible_keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in possible_keys:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def get_payout(item):
    for key in (
        "payout", "payout_percent", "payoutPercent",
        "profit", "profit_percent", "profitPercent"
    ):
        value = item.get(key)

        if value is None:
            continue

        try:
            value = float(value)
            if 0 < value <= 1:
                value *= 100
            if 0 < value <= 100:
                return value
        except (ValueError, TypeError):
            pass

    return None


def is_forex_otc(symbol):
    if not isinstance(symbol, str):
        return False

    if not symbol.lower().endswith("_otc"):
        return False

    pair = symbol[:-4].upper()

    if len(pair) != 6:
        return False

    return (
        pair[:3] in FIAT_CODES
        and pair[3:] in FIAT_CODES
    )


def get_pairs():
    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    items = extract_list(
        data,
        ("symbols", "data", "results", "instruments")
    )

    pairs = []

    for item in items:
        if isinstance(item, str):
            symbol = item
            payout = None
        elif isinstance(item, dict):
            symbol = (
                item.get("symbol")
                or item.get("name")
                or item.get("ticker")
            )
            payout = get_payout(item)
        else:
            continue

        if not symbol or not is_forex_otc(symbol):
            continue

        if payout is None or payout < MIN_PAYOUT:
            continue

        pairs.append({
            "symbol": symbol,
            "payout": payout
        })

    return pairs


def normalize_timestamp(value):
    if value is None:
        return None

    try:
        if isinstance(value, str):
            value = value.strip()

            if value.isdigit():
                value = int(value)
            else:
                dt = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())

        value = float(value)

        if value > 100000000000:
            value /= 1000

        return int(value)

    except Exception:
        return None


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

    rows = extract_list(
        data,
        ("candles", "data", "results", "history")
    )

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        timestamp = normalize_timestamp(
            row.get("time", row.get("timestamp"))
        )

        try:
            candle = {
                "time": timestamp,
                "open": float(row.get("open")),
                "high": float(row.get("high")),
                "low": float(row.get("low")),
                "close": float(row.get("close"))
            }

            if timestamp is None:
                continue

            if candle["high"] < candle["low"]:
                continue

            candles.append(candle)

        except (ValueError, TypeError):
            continue

    candles.sort(key=lambda x: x["time"])

    # Remove duplicate timestamps.
    unique = {}
    for candle in candles:
        unique[candle["time"]] = candle

    candles = list(unique.values())

    # Keep only candles whose 60-second period has finished.
    now = int(time.time())
    candles = [
        c for c in candles
        if c["time"] + TIMEFRAME <= now
    ]

    if len(candles) < 60:
        raise RuntimeError(
            f"Not enough completed candles: {len(candles)}"
        )

    return candles


def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)
    seed = sum(values[:period]) / period
    result[period - 1] = seed

    multiplier = 2 / (period + 1)
    previous = seed

    for i in range(period, len(values)):
        previous = (
            values[i] * multiplier
            + previous * (1 - multiplier)
        )
        result[i] = previous

    return result


def rsi(values, period=14):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def calculate(gain, loss):
        if loss == 0:
            return 100.0
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    result[period] = calculate(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (
            (avg_gain * (period - 1) + gain) / period
        )
        avg_loss = (
            (avg_loss * (period - 1) + loss) / period
        )

        result[i] = calculate(avg_gain, avg_loss)

    return result


def bollinger(values, period=20, deviations=2):
    middle = [None] * len(values)
    upper = [None] * len(values)
    lower = [None] * len(values)

    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        avg = statistics.mean(window)
        std = statistics.pstdev(window)

        middle[i] = avg
        upper[i] = avg + deviations * std
        lower[i] = avg - deviations * std

    return upper, middle, lower


def stochastic(candles, period=14, smooth=3):
    k = [None] * len(candles)
    d = [None] * len(candles)

    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1:i + 1]
        highest = max(x["high"] for x in window)
        lowest = min(x["low"] for x in window)

        if highest == lowest:
            k[i] = 50.0
        else:
            k[i] = (
                (candles[i]["close"] - lowest)
                / (highest - lowest)
            ) * 100

    for i in range(period + smooth - 2, len(candles)):
        values = k[i - smooth + 1:i + 1]
        if all(x is not None for x in values):
            d[i] = statistics.mean(values)

    return k, d


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    line = [None] * len(values)

    for i in range(len(values)):
        if fast[i] is not None and slow[i] is not None:
            line[i] = fast[i] - slow[i]

    valid = [x for x in line if x is not None]
    signal_values = ema(valid, 9)

    signal = [None] * len(values)
    j = 0

    for i in range(len(values)):
        if line[i] is not None:
            signal[i] = signal_values[j]
            j += 1

    return line, signal


def supertrend(candles, period=10, multiplier=3):
    n = len(candles)
    trend = [None] * n
    direction = [None] * n

    atr = [None] * n
    tr_values = []

    for i in range(n):
        if i == 0:
            tr = candles[i]["high"] - candles[i]["low"]
        else:
            previous_close = candles[i - 1]["close"]
            tr = max(
                candles[i]["high"] - candles[i]["low"],
                abs(candles[i]["high"] - previous_close),
                abs(candles[i]["low"] - previous_close)
            )

        tr_values.append(tr)

        if i >= period - 1:
            atr[i] = statistics.mean(
                tr_values[i - period + 1:i + 1]
            )

    upper = [None] * n
    lower = [None] * n

    for i in range(period - 1, n):
        mid = (
            candles[i]["high"] + candles[i]["low"]
        ) / 2

        basic_upper = mid + multiplier * atr[i]
        basic_lower = mid - multiplier * atr[i]

        if i == period - 1:
            upper[i] = basic_upper
            lower[i] = basic_lower
            direction[i] = -1
            trend[i] = upper[i]
            continue

        upper[i] = (
            min(basic_upper, upper[i - 1])
            if candles[i - 1]["close"] <= upper[i - 1]
            else basic_upper
        )

        lower[i] = (
            max(basic_lower, lower[i - 1])
            if candles[i - 1]["close"] >= lower[i - 1]
            else basic_lower
        )

        if direction[i - 1] == -1:
            direction[i] = (
                1 if candles[i]["close"] > upper[i]
                else -1
            )
        else:
            direction[i] = (
                -1 if candles[i]["close"] < lower[i]
                else 1
            )

        trend[i] = (
            lower[i] if direction[i] == 1 else upper[i]
        )

    return direction


def analyze(candles):
    close = [c["close"] for c in candles]
    n = len(candles)
    i = n - 1
    p = i - 1

    if p < 1:
        return None

    fast = ema(close, 9)
    mid = ema(close, 21)
    slow = ema(close, 50)
    rsi_values = rsi(close, 14)
    upper, middle, lower = bollinger(close)
    k, d = stochastic(candles)
    macd_line, macd_signal = macd(close)
    st_direction = supertrend(candles)

    required = [
        fast[i], mid[i], slow[i],
        rsi_values[i], upper[i], lower[i],
        k[i], d[i], macd_line[i],
        macd_signal[i], st_direction[i]
    ]

    if any(x is None for x in required):
        return None

    previous_required = [
        rsi_values[p], k[p], d[p],
        macd_line[p], macd_signal[p],
        st_direction[p]
    ]

    if any(x is None for x in previous_required):
        return None

    current = candles[i]
    previous = candles[p]

    votes_call = 0
    votes_put = 0
    reasons_call = []
    reasons_put = []

    # 1. RSI Reversal
    if rsi_values[p] <= 30 and rsi_values[i] > 30:
        votes_call += 1
        reasons_call.append("RSI Reversal")

    if rsi_values[p] >= 70 and rsi_values[i] < 70:
        votes_put += 1
        reasons_put.append("RSI Reversal")

    # 2. EMA Cross
    if fast[p] <= mid[p] and fast[i] > mid[i]:
        votes_call += 1
        reasons_call.append("EMA Cross")

    if fast[p] >= mid[p] and fast[i] < mid[i]:
        votes_put += 1
        reasons_put.append("EMA Cross")

    # 3. Bollinger Bounce
    if (
        previous["close"] <= lower[p]
        and current["close"] > lower[i]
    ):
        votes_call += 1
        reasons_call.append("Bollinger Bounce")

    if (
        previous["close"] >= upper[p]
        and current["close"] < upper[i]
    ):
        votes_put += 1
        reasons_put.append("Bollinger Bounce")

    # 4. MACD Cross
    if (
        macd_line[p] <= macd_signal[p]
        and macd_line[i] > macd_signal[i]
    ):
        votes_call += 1
        reasons_call.append("MACD Cross")

    if (
        macd_line[p] >= macd_signal[p]
        and macd_line[i] < macd_signal[i]
    ):
        votes_put += 1
        reasons_put.append("MACD Cross")

    # 5. Stochastic Cross
    if (
        k[p] <= d[p] and k[i] > d[i]
        and k[i] <= 35
    ):
        votes_call += 1
        reasons_call.append("Stochastic Cross")

    if (
        k[p] >= d[p] and k[i] < d[i]
        and k[i] >= 65
    ):
        votes_put += 1
        reasons_put.append("Stochastic Cross")

    # 6. SuperTrend Flip
    if st_direction[p] == -1 and st_direction[i] == 1:
        votes_call += 1
        reasons_call.append("SuperTrend Flip")

    if st_direction[p] == 1 and st_direction[i] == -1:
        votes_put += 1
        reasons_put.append("SuperTrend Flip")

    # 7. Combo: RSI + Bollinger + EMA trend
    if (
        rsi_values[i] < 45
        and current["close"] > lower[i]
        and fast[i] > mid[i]
    ):
        votes_call += 1
        reasons_call.append("Combo")

    if (
        rsi_values[i] > 55
        and current["close"] < upper[i]
        and fast[i] < mid[i]
    ):
        votes_put += 1
        reasons_put.append("Combo")

    # 8. Triple EMA Alignment
    if (
        fast[p] <= mid[p]
        and mid[p] <= slow[p]
        and fast[i] > mid[i] > slow[i]
    ):
        votes_call += 1
        reasons_call.append("Triple EMA")

    if (
        fast[p] >= mid[p]
        and mid[p] >= slow[p]
        and fast[i] < mid[i] < slow[i]
    ):
        votes_put += 1
        reasons_put.append("Triple EMA")

    # Direction confirmation: EMA 9/21/50 + candle direction.
    bullish_confirmation = (
        fast[i] > mid[i] > slow[i]
        and current["close"] > current["open"]
    )

    bearish_confirmation = (
        fast[i] < mid[i] < slow[i]
        and current["close"] < current["open"]
    )

    logging.info(
        "%s votes: CALL=%s PUT=%s",
        current["time"], votes_call, votes_put
    )

    if (
        votes_call >= MIN_VOTES
        and votes_call > votes_put
        and bullish_confirmation
    ):
        return {
            "direction": "CALL",
            "votes": votes_call,
            "reasons": reasons_call,
            "candle_time": current["time"]
        }

    if (
        votes_put >= MIN_VOTES
        and votes_put > votes_call
        and bearish_confirmation
    ):
        return {
            "direction": "PUT",
            "votes": votes_put,
            "reasons": reasons_put,
            "candle_time": current["time"]
        }

    return None


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing Telegram secrets")

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
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
    logging.info("Telegram message sent.")


def make_message(candidate):
    direction = candidate["direction"]

    if direction == "CALL":
        emoji = "🟢"
        label = "BUY / CALL"
    else:
        emoji = "🔴"
        label = "SELL / PUT"

    florida_now = datetime.now(FLORIDA)

    # Next full minute after scanning.
    entry = florida_now.replace(
        second=0,
        microsecond=0
    ) + timedelta(minutes=1)

    entry_text = entry.strftime("%I:%M:%S %p")

    reasons = ", ".join(candidate["reasons"])

    return (
        "🤖 ROMARIO TRADER PRO 7.2\n\n"
        f"{emoji} {label}\n\n"
        f"💱 PAIR: {candidate['symbol']}\n"
        f"💰 PAYOUT: {candidate['payout']:.1f}%\n"
        "⏱ TIMEFRAME: M1\n"
        "⌛ EXPIRATION: 60 SECONDS\n"
        f"📊 SETUP SCORE: {candidate['votes']}/8\n"
        f"🕒 ENTRY TIME: {entry_text} Florida\n"
        f"📌 CONFIRMATION: {reasons}\n\n"
        "⚠️ Demo test recommended. No guaranteed win rate."
    )


def main():
    print("=" * 50)
    print("ROMARIO TRADER PRO 7.2")
    print("8 STRATEGY VOTING SYSTEM")
    print("=" * 50)

    pairs = get_pairs()

    print(
        f"Forex OTC pairs with payout >= "
        f"{MIN_PAYOUT}%: {len(pairs)}"
    )

    if not pairs:
        send_telegram(
            "🤖 ROMARIO TRADER PRO 7.2\n"
            "⚠️ No Forex OTC pairs with payout >= 92%."
        )
        return

    candidates = []

    for number, pair in enumerate(pairs, 1):
        symbol = pair["symbol"]
        payout = pair["payout"]

        print(
            f"[{number}/{len(pairs)}] "
            f"Scanning {symbol} | {payout:.1f}%"
        )

        try:
            candles = get_candles(symbol)
            print(f"   Candles received: {len(candles)}")

            setup = analyze(candles)

            if setup:
                setup["symbol"] = symbol
                setup["payout"] = payout
                candidates.append(setup)
                print(
                    f"   VALID: {setup['direction']} "
                    f"{setup['votes']}/8"
                )
            else:
                print("   No qualifying setup.")

        except Exception as error:
            logging.warning("%s skipped: %s", symbol, error)

    print(f"Total valid candidates: {len(candidates)}")

    if not candidates:
        send_telegram(
            "🤖 ROMARIO TRADER PRO 7.2\n\n"
            "⚪ NO VALID SIGNAL\n"
            "No pair passed the 2/8 vote and direction "
            "confirmation rules.\n"
            "No signal was forced."
        )
        return

    candidates.sort(
        key=lambda x: (
            x["votes"],
            x["payout"]
        ),
        reverse=True
    )

    best = candidates[0]
    message = make_message(best)
    send_telegram(message)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.exception("BOT ERROR")
        try:
            send_telegram(
                "🤖 ROMARIO TRADER PRO 7.2\n"
                f"🔴 BOT ERROR: {str(error)[:500]}"
            )
        except Exception:
            pass
        raise
