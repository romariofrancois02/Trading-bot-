
import os
import re
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# =========================================================
# ROMARIO TRADER PRO 7.1
# 8 STRATEGY VOTING SYSTEM
# Pocket Option OTC | M1 | 60 Seconds
# =========================================================

VERSION = "ROMARIO TRADER PRO 7.1"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

MIN_PAYOUT = 92.0
MIN_VOTES = 4
MAX_PAIRS = 25

TIMEFRAME = 60
EXPIRATION = 60

FLORIDA = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "ROMARIO-TRADER-PRO/7.1"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# Fiat currency codes only. This excludes crypto pairs
# such as ETHUSD and DOTUSD.
FIAT_CODES = {
    "AED", "AUD", "BHD", "BDT", "CAD", "CHF",
    "CNY", "CNH", "EUR", "GBP", "HKD", "IDR",
    "ILS", "INR", "IQD", "JOD", "JPY", "KRW",
    "KWD", "LBP", "LKR", "MAD", "MXN", "MYR",
    "NGN", "NOK", "NZD", "OMR", "PKR", "PLN",
    "QAR", "SAR", "SEK", "SGD", "THB", "TRY",
    "TWD", "UAH", "USD", "VND", "ZAR"
}


# =========================================================
# TELEGRAM
# =========================================================

def telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        logging.error("Telegram secrets are missing.")
        return False

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TG_CHAT,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )

        response.raise_for_status()
        result = response.json()

        if result.get("ok") is True:
            logging.info("Telegram message sent.")
            return True

        logging.error("Telegram rejected message.")
        return False

    except requests.RequestException as error:
        logging.error("Telegram connection error: %s", error)
        return False


# =========================================================
# OTCHARTS API WITH RETRIES
# =========================================================

def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing.")

    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(
                BASE_URL + endpoint,
                headers=HEADERS,
                params=params,
                timeout=25
            )

            response.raise_for_status()
            return response.json()

        except requests.RequestException as error:
            last_error = error

            logging.warning(
                "API attempt %s/3 failed: %s",
                attempt + 1,
                error
            )

            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"OTCharts request failed: {last_error}")


# =========================================================
# SYMBOLS / PAYOUT
# =========================================================

def get_pairs():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    symbols = data.get("symbols", [])

    if not isinstance(symbols, list):
        raise RuntimeError("Unexpected symbols response format.")

    pairs = []

    for item in symbols:
        if not isinstance(item, dict):
            continue

        symbol = item.get("symbol", "")

        if not isinstance(symbol, str):
            continue

        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        base = symbol[:3]
        quote = symbol[3:6]

        if base not in FIAT_CODES or quote not in FIAT_CODES:
            continue

        raw_payout = item.get("payout")

        if raw_payout is None:
            continue

        try:
            payout = float(raw_payout)
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT or payout > 100:
            continue

        pairs.append({
            "symbol": symbol,
            "payout": payout
        })

    pairs.sort(
        key=lambda pair: pair["payout"],
        reverse=True
    )

    return pairs[:MAX_PAIRS]


# =========================================================
# CANDLE DATA
# =========================================================

def get_candles(symbol):
    data = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": 150
        }
    )

    rows = data.get("candles", [])

    if not isinstance(rows, list):
        return []

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            timestamp = row.get("time", row.get("timestamp"))

            candle = {
                "time": int(timestamp),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            }

            if candle["high"] < candle["low"]:
                continue

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda candle: candle["time"])

    # Remove duplicate timestamps.
    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = list(unique.values())
    candles.sort(key=lambda candle: candle["time"])

    # PROVISIONAL:
    # This assumes OTCharts returns the currently forming
    # candle last. Verify timestamps with your API data.
    if len(candles) > 1:
        candles = candles[:-1]

    return candles


# =========================================================
# EMA
# Always returns a list with the same length as input.
# =========================================================

def ema(values, period):
    n = len(values)

    if n < period:
        return [None] * n

    result = [None] * n

    average = sum(values[:period]) / period
    result[period - 1] = average

    alpha = 2 / (period + 1)

    for i in range(period, n):
        average = (
            values[i] * alpha
            + average * (1 - alpha)
        )

        result[i] = average

    return result


# =========================================================
# RSI
# =========================================================

def rsi(values, period=14):
    n = len(values)
    result = [None] * n

    if n <= period:
        return result

    gains = []
    losses = []

    for i in range(1, n):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_value(gain, loss):
        if loss == 0:
            return 100.0

        rs = gain / loss
        return 100 - (100 / (1 + rs))

    result[period] = rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, n):
        index = i - 1

        avg_gain = (
            avg_gain * (period - 1) + gains[index]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[index]
        ) / period

        result[i] = rsi_value(avg_gain, avg_loss)

    return result


# =========================================================
# BOLLINGER BANDS
# =========================================================

def bollinger(values, period=20, multiplier=2):
    n = len(values)

    upper = [None] * n
    middle = [None] * n
    lower = [None] * n

    for i in range(period - 1, n):
        window = values[i - period + 1:i + 1]

        average = sum(window) / period

        variance = sum(
            (value - average) ** 2
            for value in window
        ) / period

        deviation = variance ** 0.5

        middle[i] = average
        upper[i] = average + multiplier * deviation
        lower[i] = average - multiplier * deviation

    return upper, middle, lower


# =========================================================
# MACD
# =========================================================

def macd(values):
    n = len(values)

    fast = ema(values, 12)
    slow = ema(values, 26)

    line = [None] * n

    for i in range(n):
        if fast[i] is not None and slow[i] is not None:
            line[i] = fast[i] - slow[i]

    valid_indices = [
        i for i in range(n)
        if line[i] is not None
    ]

    valid_values = [
        line[i] for i in valid_indices
    ]

    signal_values = ema(valid_values, 9)
    signal = [None] * n

    # Put MACD signal values back at their original indices.
    for position, original_index in enumerate(valid_indices):
        signal[original_index] = signal_values[position]

    return line, signal


# =========================================================
# STOCHASTIC
# =========================================================

def stochastic(candles, period=14, smooth=3):
    n = len(candles)

    k = [None] * n
    d = [None] * n

    for i in range(period - 1, n):
        window = candles[i - period + 1:i + 1]

        highest = max(item["high"] for item in window)
        lowest = min(item["low"] for item in window)

        if highest == lowest:
            k[i] = 50.0
        else:
            k[i] = (
                100 * (candles[i]["close"] - lowest)
                / (highest - lowest)
            )

    for i in range(n):
        if i < period + smooth - 2:
            continue

        values = k[i - smooth + 1:i + 1]

        if any(value is None for value in values):
            continue

        d[i] = sum(values) / smooth

    return k, d


# =========================================================
# SUPERTREND
# =========================================================

def supertrend(candles, period=10, multiplier=3):
    n = len(candles)

    trend = [None] * n
    upper_band = [None] * n
    lower_band = [None] * n
    true_ranges = [0.0] * n
    atr = [None] * n

    for i, candle in enumerate(candles):
        if i == 0:
            true_ranges[i] = (
                candle["high"] - candle["low"]
            )
        else:
            previous_close = candles[i - 1]["close"]

            true_ranges[i] = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close)
            )

    for i in range(period - 1, n):
        atr[i] = (
            sum(true_ranges[i - period + 1:i + 1])
            / period
        )

    for i in range(n):
        if atr[i] is None:
            continue

        candle = candles[i]
        midpoint = (candle["high"] + candle["low"]) / 2

        basic_upper = midpoint + multiplier * atr[i]
        basic_lower = midpoint - multiplier * atr[i]

        if i == 0 or trend[i - 1] is None:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower

            trend[i] = (
                1 if candle["close"] >= midpoint else -1
            )

            continue

        previous_upper = upper_band[i - 1]
        previous_lower = lower_band[i - 1]
        previous_close = candles[i - 1]["close"]

        if previous_upper is None or previous_lower is None:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
            trend[i] = trend[i - 1]
            continue

        if previous_close <= previous_upper:
            upper_band[i] = min(
                basic_upper,
                previous_upper
            )
        else:
            upper_band[i] = basic_upper

        if previous_close >= previous_lower:
            lower_band[i] = max(
                basic_lower,
                previous_lower
            )
        else:
            lower_band[i] = basic_lower

        if (
            trend[i - 1] == -1
            and candle["close"] > upper_band[i]
        ):
            trend[i] = 1

        elif (
            trend[i - 1] == 1
            and candle["close"] < lower_band[i]
        ):
            trend[i] = -1

        else:
            trend[i] = trend[i - 1]

    return trend


# =========================================================
# 8 STRATEGY VOTING
# =========================================================

def analyze(candles):
    if len(candles) < 60:
        return None

    close = [candle["close"] for candle in candles]

    e9 = ema(close, 9)
    e21 = ema(close, 21)
    e50 = ema(close, 50)

    rv = rsi(close, 14)

    bu, bm, bl = bollinger(close)

    macd_line, macd_signal = macd(close)

    stoch_k, stoch_d = stochastic(candles)

    st = supertrend(candles)

    i = len(candles) - 1
    p = i - 1

    required = [
        e9[i], e9[p],
        e21[i], e21[p],
        e50[i], e50[p],
        rv[i], rv[p],
        bu[i], bl[i],
        bu[p], bl[p],
        macd_line[i], macd_line[p],
        macd_signal[i], macd_signal[p],
        stoch_k[i], stoch_k[p],
        stoch_d[i], stoch_d[p],
        st[i], st[p]
    ]

    if any(value is None for value in required):
        return None

    current = candles[i]
    previous = candles[p]

    call_votes = []
    put_votes = []

    def vote(condition, direction, name):
        if not condition:
            return

        if direction == "CALL":
            call_votes.append(name)
        else:
            put_votes.append(name)

    # 1. RSI REVERSAL
    vote(
        rv[p] < 30 and rv[i] >= 30,
        "CALL",
        "RSI Reversal"
    )

    vote(
        rv[p] > 70 and rv[i] <= 70,
        "PUT",
        "RSI Reversal"
    )

    # 2. EMA CROSS
    vote(
        e9[p] <= e21[p] and e9[i] > e21[i],
        "CALL",
        "EMA Cross"
    )

    vote(
        e9[p] >= e21[p] and e9[i] < e21[i],
        "PUT",
        "EMA Cross"
    )

    # 3. BOLLINGER BOUNCE
    vote(
        previous["low"] <= bl[p]
        and current["close"] > bl[i],
        "CALL",
        "Bollinger Bounce"
    )

    vote(
        previous["high"] >= bu[p]
        and current["close"] < bu[i],
        "PUT",
        "Bollinger Bounce"
    )

    # 4. MACD CROSS
    vote(
        macd_line[p] <= macd_signal[p]
        and macd_line[i] > macd_signal[i],
        "CALL",
        "MACD Cross"
    )

    vote(
        macd_line[p] >= macd_signal[p]
        and macd_line[i] < macd_signal[i],
        "PUT",
        "MACD Cross"
    )

    # 5. STOCHASTIC CROSS
    vote(
        stoch_k[p] <= stoch_d[p]
        and stoch_k[i] > stoch_d[i]
        and stoch_k[i] < 25,
        "CALL",
        "Stochastic Cross"
    )

    vote(
        stoch_k[p] >= stoch_d[p]
        and stoch_k[i] < stoch_d[i]
        and stoch_k[i] > 75,
        "PUT",
        "Stochastic Cross"
    )

    # 6. SUPERTREND FLIP
    vote(
        st[p] == -1 and st[i] == 1,
        "CALL",
        "SuperTrend Flip"
    )

    vote(
        st[p] == 1 and st[i] == -1,
        "PUT",
        "SuperTrend Flip"
    )

    # 7. COMBO
    combo_call = sum([
        rv[i] > rv[p] and rv[i] < 50,
        current["close"] > bl[i],
        e9[i] > e21[i]
    ])

    combo_put = sum([
        rv[i] < rv[p] and rv[i] > 50,
        current["close"] < bu[i],
        e9[i] < e21[i]
    ])

    vote(
        combo_call >= 2,
        "CALL",
        "Combo"
    )

    vote(
        combo_put >= 2,
        "PUT",
        "Combo"
    )

    # 8. TRIPLE EMA
    bullish_alignment = (
        e9[i] > e21[i] > e50[i]
    )

    previous_bullish = (
        e9[p] > e21[p] > e50[p]
    )

    bearish_alignment = (
        e9[i] < e21[i] < e50[i]
    )

    previous_bearish = (
        e9[p] < e21[p] < e50[p]
    )

    vote(
        bullish_alignment and not previous_bullish,
        "CALL",
        "Triple EMA"
    )

    vote(
        bearish_alignment and not previous_bearish,
        "PUT",
        "Triple EMA"
    )

    logging.info(
        "%s votes: CALL=%s PUT=%s",
        candles[i]["time"],
        len(call_votes),
        len(put_votes)
    )

    if (
        len(call_votes) >= MIN_VOTES
        and len(call_votes) > len(put_votes)
    ):
        direction = "CALL"
        selected_votes = call_votes

    elif (
        len(put_votes) >= MIN_VOTES
        and len(put_votes) > len(call_votes)
    ):
        direction = "PUT"
        selected_votes = put_votes

    else:
        return None

    return {
        "direction": direction,
        "votes": len(selected_votes),
        "strategies": selected_votes,
        "call_count": len(call_votes),
        "put_count": len(put_votes),
        "rsi": rv[i],
        "price": current["close"],
        "candle_time": current["time"]
    }


# =========================================================
# ENTRY TIME - FLORIDA
# =========================================================

def next_entry():
    now = datetime.now(FLORIDA)

    entry = (
        now.replace(second=0, microsecond=0)
        + timedelta(minutes=1)
    )

    signal = entry - timedelta(seconds=30)

    if now >= signal:
        entry += timedelta(minutes=1)
        signal = entry - timedelta(seconds=30)

    return signal, entry


def wait_until(target):
    while True:
        remaining = (
            target - datetime.now(FLORIDA)
        ).total_seconds()

        if remaining <= 0:
            return

        time.sleep(min(remaining, 2))


# =========================================================
# SIGNAL MESSAGE
# =========================================================

def make_message(pair, payout, result, entry):
    if result["direction"] == "CALL":
        emoji = "🟢"
        action = "BUY / CALL"
    else:
        emoji = "🔴"
        action = "SELL / PUT"

    signal_time = entry - timedelta(seconds=30)
    expiration_time = entry + timedelta(seconds=EXPIRATION)

    strategy_list = "\n".join(
        f"✓ {name}"
        for name in result["strategies"]
    )

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"{emoji} <b>{action}</b>\n\n"
        f"💱 PAIR: <b>{pair}</b>\n"
        f"💰 PAYOUT: <b>{payout:.1f}%</b>\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🕒 SIGNAL TIME: "
        f"{signal_time.strftime('%I:%M:%S %p')}\n"
        f"🎯 ENTRY TIME: "
        f"{entry.strftime('%I:%M:%S %p')}\n"
        f"🏁 EXPIRATION: "
        f"{expiration_time.strftime('%I:%M:%S %p')}\n\n"
        f"⭐ STRATEGY VOTES: "
        f"{result['votes']}/8\n"
        f"{strategy_list}\n\n"
        f"📊 RSI: {result['rsi']:.2f}\n"
        f"💵 PRICE: {result['price']}\n"
        f"🕐 FLORIDA TIME\n"
        f"⚠️ No guaranteed win rate. Demo test recommended."
    )


# =========================================================
# MAIN
# =========================================================

def main():
    print("=" * 48)
    print(VERSION)
    print("8 STRATEGY VOTING SYSTEM")
    print("=" * 48)

    pairs = get_pairs()

    print(
        f"Forex OTC pairs with payout >= "
        f"{MIN_PAYOUT}%: {len(pairs)}"
    )

    if not pairs:
        telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            "⚪ No eligible Forex OTC pairs found.\n"
            "No signal generated."
        )
        return

    candidates = []

    for number, pair in enumerate(pairs, start=1):
        symbol = pair["symbol"]
        payout = pair["payout"]

        print(
            f"[{number}/{len(pairs)}] "
            f"Scanning {symbol} | {payout}%"
        )

        try:
            candles = get_candles(symbol)

            print(
                f"   Candles received: {len(candles)}"
            )

            result = analyze(candles)

            if result:
                result["symbol"] = symbol
                result["payout"] = payout

                candidates.append(result)

                print(
                    f"   VALID: {result['direction']} "
                    f"| Votes: {result['votes']}/8"
                )

            else:
                print("   No qualifying setup.")

        except Exception as error:
            logging.warning(
                "%s skipped: %s",
                symbol,
                error
            )

    print(
        f"Total valid candidates: {len(candidates)}"
    )

    if not candidates:
        telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            "⚪ No pair reached the required "
            f"{MIN_VOTES}/8 votes.\n"
            "No signal was forced."
        )
        return

    candidates.sort(
        key=lambda item: (
            item["votes"],
            item["payout"]
        ),
        reverse=True
    )

    best = candidates[0]

    signal_time, entry_time = next_entry()

    print(
        f"BEST: {best['symbol']} "
        f"{best['direction']} "
        f"{best['votes']}/8"
    )

    print(
        "Signal time:",
        signal_time.strftime("%I:%M:%S %p")
    )

    print(
        "Entry time:",
        entry_time.strftime("%I:%M:%S %p")
    )

    wait_until(signal_time)

    if datetime.now(FLORIDA) >= entry_time:
        print("Entry time missed. Signal cancelled.")
        return

    message = make_message(
        best["symbol"],
        best["payout"],
        best,
        entry_time
    )

    telegram(message)

    print("Signal process completed.")


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        logging.exception("BOT FAILED")

        telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            f"🔴 BOT ERROR\n"
            f"<code>{str(error)[:400]}</code>"
        )
