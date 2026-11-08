
import os
import time
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ==========================================
# ROMARIO TRADER PRO 4.8
# Pocket Option OTC - Manual alerts only
# ==========================================

BASE_URL = "https://otcharts.com"
VENUE = "otc"

MIN_PAYOUT = 78
MAX_PAIRS = 5
MIN_SCORE = 6

M1_LIMIT = 100
M5_LIMIT = 100

# OTCharts documents Pocket Option OTC candle timestamps
# as UTC+2. Convert them back to UTC for timing.
OTC_TIME_OFFSET = 7200

FLORIDA = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "ROMARIO-TRADER-PRO-4.8"
})


def log(message):
    print(f"[{datetime.now(FLORIDA).strftime('%I:%M:%S %p')}] {message}",
          flush=True)


def api_get(endpoint, params=None):
    url = BASE_URL + endpoint

    response = session.get(
        url,
        params=params,
        timeout=20
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"OTCharts HTTP {response.status_code}: "
            f"{response.text[:250]}"
        )

    return response.json()


def get_pairs():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    symbols = data.get("symbols", [])
    valid = []

    for item in symbols:
        symbol = item.get("symbol", "")
        payout = item.get("payout")
        is_open = item.get("open")

        if not symbol.endswith("_otc"):
            continue

        # Keep currency and metal pairs such as EURUSD and XAGUSD.
        pair_name = symbol.replace("_otc", "")
        if len(pair_name) != 6 or not pair_name.isalpha():
            continue

        if is_open is not True:
            continue

        if payout is None:
            continue

        try:
            payout = float(payout)
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        valid.append({
            "symbol": symbol,
            "name": item.get("name", symbol),
            "payout": payout
        })

    # Highest payout first. Only five pairs are analyzed.
    valid.sort(key=lambda x: x["payout"], reverse=True)

    return valid[:MAX_PAIRS]


def get_candles(symbol, timeframe, limit):
    data = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": timeframe,
            "limit": limit
        }
    )

    candles = data.get("candles", [])

    if not isinstance(candles, list):
        return []

    now = time.time()
    clean = []

    for candle in candles:
        try:
            item = {
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "time": float(candle["time"]) - OTC_TIME_OFFSET
            }

            # Only use candles that have fully closed.
            if item["time"] + timeframe <= now:
                clean.append(item)

        except (KeyError, TypeError, ValueError):
            continue

    clean.sort(key=lambda x: x["time"])

    # Remove duplicate candle timestamps.
    unique = {}
    for candle in clean:
        unique[candle["time"]] = candle

    return list(unique.values())


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (value - result) * multiplier + result

    return result


def rsi(values, period=7):
    if len(values) <= period:
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


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        ranges.append(tr)

    return sum(ranges[-period:]) / period


def analyze(symbol):
    m1 = get_candles(symbol, 60, M1_LIMIT)
    m5 = get_candles(symbol, 300, M5_LIMIT)

    if len(m1) < 55 or len(m5) < 55:
        log(f"{symbol}: not enough closed candles")
        return None

    m1_close = [x["close"] for x in m1]
    m5_close = [x["close"] for x in m5]

    m1_fast = ema(m1_close, 9)
    m1_mid = ema(m1_close, 21)

    m5_fast = ema(m5_close, 20)
    m5_slow = ema(m5_close, 50)

    m1_rsi = rsi(m1_close, 7)
    m5_rsi = rsi(m5_close, 7)

    volatility = atr(m1, 14)

    if None in (
        m1_fast, m1_mid, m5_fast,
        m5_slow, m1_rsi, m5_rsi, volatility
    ):
        return None

    last = m1[-1]
    previous = m1[-2]
    last_m5 = m5[-1]

    # Seven technical checks for each direction.
    call_checks = [
        m1_fast > m1_mid,
        last["close"] > m1_fast,
        m1_rsi > 50,
        last["close"] > last["open"],
        m5_fast > m5_slow,
        last_m5["close"] > m5_fast,
        m5_rsi > 50
    ]

    put_checks = [
        m1_fast < m1_mid,
        last["close"] < m1_fast,
        m1_rsi < 50,
        last["close"] < last["open"],
        m5_fast < m5_slow,
        last_m5["close"] < m5_fast,
        m5_rsi < 50
    ]

    call_score = sum(call_checks)
    put_score = sum(put_checks)

    if call_score >= MIN_SCORE and call_score > put_score:
        direction = "CALL"
        score = call_score

    elif put_score >= MIN_SCORE and put_score > call_score:
        direction = "PUT"
        score = put_score

    else:
        return None

    # Avoid extremely small or flat candles.
    candle_range = last["high"] - last["low"]

    if candle_range <= 0 or volatility <= 0:
        return None

    if candle_range < volatility * 0.15:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "rsi": m1_rsi,
        "atr": volatility,
        "last_candle_time": last["time"]
    }


def telegram_send(message):
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

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram error: {response.text[:250]}"
        )

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm the message")


def florida_time(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        timezone.utc
    ).astimezone(FLORIDA).strftime("%I:%M:%S %p")


def wait_until(timestamp):
    while True:
        remaining = timestamp - time.time()

        if remaining <= 0:
            return

        time.sleep(min(remaining, 5))


def main():
    print("=" * 45)
    print("ROMARIO TRADER PRO 4.8")
    print("POCKET OPTION OTC SCANNER")
    print("=" * 45)

    if not API_KEY or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing GitHub Secrets")

    log("Loading OTC pairs...")

    pairs = get_pairs()

    log(f"Pairs with payout >= {MIN_PAYOUT}%: {len(pairs)}")
    log(f"Analyzing maximum {MAX_PAIRS} pairs")

    if not pairs:
        log("No open pairs with qualifying payout.")
        return

    candidates = []

    for number, pair in enumerate(pairs, start=1):
        symbol = pair["symbol"]

        log(
            f"[{number}/{len(pairs)}] "
            f"{symbol} | PAYOUT {pair['payout']}%"
        )

        try:
            result = analyze(symbol)

            if result:
                result["payout"] = pair["payout"]
                candidates.append(result)

                log(
                    f"VALID: {result['direction']} "
                    f"{symbol} | SCORE {result['score']}/7"
                )
            else:
                log(f"No valid setup: {symbol}")

        except Exception as error:
            log(f"Pair error {symbol}: {error}")

    if not candidates:
        log("No qualifying signal. No Telegram WAIT message sent.")
        return

    # Highest score first, then payout.
    candidates.sort(
        key=lambda x: (x["score"], x["payout"]),
        reverse=True
    )

    selected = candidates[0]

    log(
        f"BEST: {selected['direction']} "
        f"{selected['symbol']} "
        f"{selected['score']}/7"
    )

    # Give the signal time to prepare.
    # Entry is set two minutes ahead, with a 30-second alert lead.
    entry_time = (int(time.time() // 60) * 60) + 120
    signal_time = entry_time - 30

    log(f"Scheduled entry: {florida_time(entry_time)}")
    log(f"Waiting until signal time: {florida_time(signal_time)}")

    wait_until(signal_time)

    # One final M1/M5 refresh for the selected pair.
    log("Final candle validation...")

    try:
        final_result = analyze(selected["symbol"])
    except Exception as error:
        log(f"Final validation failed: {error}")
        return

    if not final_result:
        log("Setup no longer valid. Signal cancelled.")
        return

    if final_result["direction"] != selected["direction"]:
        log("Direction changed. Signal cancelled.")
        return

    if final_result["score"] < MIN_SCORE:
        log("Technical score dropped. Signal cancelled.")
        return

    # Ensure the entry time has not already passed.
    if time.time() >= entry_time:
        log("Entry time passed. Late signal cancelled.")
        return

    emoji = "🟢" if final_result["direction"] == "CALL" else "🔴"

    message = (
        "🤖 ROMARIO TRADER PRO 4.8\n\n"
        f"{emoji} {final_result['direction']}\n\n"
        f"💱 PAIR: {selected['symbol'].replace('_otc', '')} OTC\n"
        f"📊 SYMBOL: {selected['symbol']}\n\n"
        "⏱ TIMEFRAME: M1\n"
        "⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🕒 SIGNAL SENT: {florida_time(time.time())}\n"
        f"🎯 ENTRY TIME: {florida_time(entry_time)}\n\n"
        f"💰 PAYOUT: {selected['payout']}%\n"
        "📈 WIN RATE: NOT ENOUGH DATA\n\n"
        "📊 M1 / M5 CONFIRMATION\n"
        f"RSI: {final_result['rsi']:.2f}\n"
        f"ATR: {final_result['atr']:.6f}\n\n"
        f"⭐ TECHNICAL SCORE: {final_result['score']}/7\n\n"
        "⚠️ MANUAL ENTRY ONLY\n"
        "⚠️ NO GUARANTEED RESULT"
    )

    telegram_send(message)

    log("TELEGRAM SIGNAL SENT SUCCESSFULLY")
    log("No automatic trade was placed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"BOT ERROR: {error}")
        raise
