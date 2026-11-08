
import os
import json
import time
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ==========================================
# ROMARIO TRADER PRO 4.9
# OTC | M1 + M5 | MANUAL ALERTS ONLY
# ==========================================

BASE_URL = "https://otcharts.com"
VENUE = "otc"

MIN_PAYOUT = 85.0
MAX_PAIRS = 5
MIN_SCORE = 6
COOLDOWN_SECONDS = 600

M1_LIMIT = 100
M5_LIMIT = 100

# Based on the timestamp offset observed in earlier logs.
# Verify this against fresh OTCharts candle timestamps.
CANDLE_OFFSET = int(os.getenv("CANDLE_TIME_OFFSET_SECONDS", "7200"))

STATE_FILE = "romario_state.json"
FLORIDA = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "ROMARIO-TRADER-PRO-4.9"
})


def log(message):
    now = datetime.now(FLORIDA).strftime("%I:%M:%S %p")
    print(f"[{now}] {message}", flush=True)


def api_get(path, params):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=20
    )
    response.raise_for_status()
    return response.json()


def get_list(data, key):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        value = data.get(key, [])
        return value if isinstance(value, list) else []
    return []


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    now = time.time()
    state = {
        pair: stamp for pair, stamp in state.items()
        if isinstance(stamp, (int, float)) and now - stamp < 86400
    }

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_pairs():
    data = api_get("/v1/symbols", {"venue": VENUE})
    rows = get_list(data, "symbols")

    pairs = []

    for item in rows:
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", ""))
        if not symbol.endswith("_otc"):
            continue

        name = symbol[:-4]
        if len(name) != 6 or not name.isalpha():
            continue

        try:
            payout = float(item.get("payout"))
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        if item.get("open") is not True:
            continue

        pairs.append({
            "symbol": symbol,
            "payout": payout
        })

    pairs.sort(key=lambda x: x["payout"], reverse=True)
    return pairs


def get_candles(symbol, tf, limit):
    data = api_get("/v1/candles", {
        "venue": VENUE,
        "symbol": symbol,
        "tf": tf,
        "limit": limit
    })

    rows = get_list(data, "candles")
    now = time.time()
    candles = {}

    for row in rows:
        try:
            stamp = float(row["time"]) - CANDLE_OFFSET

            candle = {
                "time": stamp,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            }

            # Use only fully closed candles.
            if stamp + tf <= now:
                candles[stamp] = candle

        except (KeyError, TypeError, ValueError):
            continue

    return sorted(candles.values(), key=lambda x: x["time"])


def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result += (value - result) * multiplier

    return result


def calculate_rsi(values, period=7):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
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


def calculate_atr(candles, period=14):
    if len(candles) <= period:
        return None

    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))

    return sum(ranges[-period:]) / period


def analyze(symbol):
    m1 = get_candles(symbol, 60, M1_LIMIT)
    m5 = get_candles(symbol, 300, M5_LIMIT)

    if len(m1) < 55 or len(m5) < 55:
        log(f"{symbol}: insufficient closed candles")
        return None

    c1 = [x["close"] for x in m1]
    c5 = [x["close"] for x in m5]

    fast1 = ema(c1, 9)
    slow1 = ema(c1, 21)
    fast5 = ema(c5, 20)
    slow5 = ema(c5, 50)

    rsi1 = calculate_rsi(c1, 7)
    rsi5 = calculate_rsi(c5, 7)
    atr1 = calculate_atr(m1, 14)

    if any(x is None for x in (
        fast1, slow1, fast5, slow5, rsi1, rsi5, atr1
    )):
        return None

    last = m1[-1]
    previous = m1[-2]
    last5 = m5[-1]

    candle_range = last["high"] - last["low"]
    body = abs(last["close"] - last["open"])

    if candle_range <= 0 or atr1 <= 0:
        return None

    # Reject weak candles and unusually large chasing candles.
    body_ratio = body / candle_range
    if body_ratio < 0.45:
        return None

    if candle_range > atr1 * 2.5:
        return None

    call_checks = [
        fast1 > slow1,
        last["close"] > fast1,
        rsi1 >= 53,
        last["close"] > last["open"],
        fast5 > slow5,
        last5["close"] > fast5,
        rsi5 > 50
    ]

    put_checks = [
        fast1 < slow1,
        last["close"] < fast1,
        rsi1 <= 47,
        last["close"] < last["open"],
        fast5 < slow5,
        last5["close"] < fast5,
        rsi5 < 50
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

    # Reject a signal if the latest candle is not fresh.
    age = time.time() - (last["time"] + 60)
    if age < 0 or age > 60:
        log(f"{symbol}: stale or timestamp mismatch ({age:.1f}s)")
        return None

    # Avoid entering after a strong candle has already exhausted.
    if body > atr1 * 1.5:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "rsi": rsi1,
        "atr": atr1,
        "candle_time": last["time"]
    }


def florida_time(timestamp):
    return datetime.fromtimestamp(
        timestamp, timezone.utc
    ).astimezone(FLORIDA).strftime("%I:%M:%S %p")


def wait_until(timestamp):
    while time.time() < timestamp:
        time.sleep(min(3, max(0.2, timestamp - time.time())))


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={"chat_id": TG_CHAT, "text": message},
        timeout=20
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm message delivery")


def main():
    print("=" * 48)
    print("ROMARIO TRADER PRO 4.9")
    print("OTC MANUAL SIGNAL SCANNER")
    print("=" * 48)

    if not all([API_KEY, TG_TOKEN, TG_CHAT]):
        raise RuntimeError("Missing required GitHub Secrets")

    state = load_state()

    log(f"Loading OTC pairs with payout >= {MIN_PAYOUT}%")
    pairs = get_pairs()

    log(f"Eligible pairs: {len(pairs)}")

    if not pairs:
        log("No eligible open pairs.")
        return

    # Exclude recently alerted pairs BEFORE candle requests.
    now = time.time()
    available = []

    for pair in pairs:
        last_sent = float(state.get(pair["symbol"], 0))
        remaining = COOLDOWN_SECONDS - (now - last_sent)

        if remaining > 0:
            log(
                f"COOLDOWN {pair['symbol']}: "
                f"{int(remaining)} seconds left"
            )
            continue

        available.append(pair)

    selected_pairs = available[:MAX_PAIRS]

    if not selected_pairs:
        log("All eligible pairs are in cooldown. No Telegram WAIT.")
        return

    candidates = []

    for index, pair in enumerate(selected_pairs, 1):
        symbol = pair["symbol"]
        log(
            f"[{index}/{len(selected_pairs)}] "
            f"Checking {symbol}, payout {pair['payout']}%"
        )

        try:
            result = analyze(symbol)

            if result:
                result["payout"] = pair["payout"]
                candidates.append(result)
                log(
                    f"Candidate: {result['direction']} "
                    f"{symbol}, score {result['score']}/7"
                )
            else:
                log(f"No qualifying setup: {symbol}")

        except Exception as error:
            log(f"Error on {symbol}: {error}")

    if not candidates:
        log("No qualifying signal. No Telegram WAIT sent.")
        return

    candidates.sort(
        key=lambda x: (x["score"], x["payout"]),
        reverse=True
    )

    chosen = candidates[0]

    # Schedule an entry at least 30 seconds after the alert.
    entry_time = (int(time.time() // 60) * 60) + 120
    signal_time = entry_time - 30

    log(f"Selected pair: {chosen['symbol']}")
    log(f"Signal scheduled: {florida_time(signal_time)}")
    log(f"Entry scheduled: {florida_time(entry_time)}")

    wait_until(signal_time)

    # Recheck the selected pair once before sending.
    log("Final validation...")

    try:
        final = analyze(chosen["symbol"])
    except Exception as error:
        log(f"Final validation error: {error}")
        return

    if not final:
        log("Final validation failed. Signal cancelled.")
        return

    if final["direction"] != chosen["direction"]:
        log("Direction changed. Signal cancelled.")
        return

    if final["score"] < MIN_SCORE:
        log("Score dropped. Signal cancelled.")
        return

    if time.time() >= entry_time:
        log("Entry time passed. Signal cancelled.")
        return

    # Recheck cooldown in case another run sent this pair.
    state = load_state()
    last_sent = float(state.get(chosen["symbol"], 0))

    if time.time() - last_sent < COOLDOWN_SECONDS:
        log("Pair was recently alerted by another run. Cancelled.")
        return

    emoji = "🟢" if final["direction"] == "CALL" else "🔴"

    message = (
        "🤖 ROMARIO TRADER PRO 4.9\n\n"
        f"{emoji} {final['direction']}\n\n"
        f"💱 PAIR: {chosen['symbol'].replace('_otc', '')} OTC\n"
        f"📊 SYMBOL: {chosen['symbol']}\n\n"
        "⏱ TIMEFRAME: M1\n"
        "⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🕒 SIGNAL SENT: {florida_time(time.time())}\n"
        f"🎯 ENTRY TIME: {florida_time(entry_time)}\n\n"
        f"💰 PAYOUT: {chosen['payout']:.1f}%\n"
        "📈 WIN RATE: NOT ENOUGH DATA\n\n"
        "📊 M1 / M5 CONFIRMATION\n"
        f"RSI: {final['rsi']:.2f}\n"
        f"ATR: {final['atr']:.6f}\n\n"
        f"⭐ TECHNICAL SCORE: {final['score']}/7\n\n"
        "⚠️ MANUAL ENTRY ONLY\n"
        "⚠️ NO GUARANTEED RESULT"
    )

    # Save cooldown only after Telegram confirms delivery.
    send_telegram(message)

    state[chosen["symbol"]] = time.time()
    save_state(state)

    log("TELEGRAM SIGNAL SENT SUCCESSFULLY")
    log("Manual alert only. No automatic trade.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"BOT ERROR: {error}")
        raise
