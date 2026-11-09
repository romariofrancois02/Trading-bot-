
import os
import re
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# =========================================================
# ROMARIO TRADER PRO 4.1
# Pocket Option OTC | M1 Entry | 60 Seconds Expiration
# =========================================================

VERSION = "ROMARIO TRADER PRO 4.1"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

MIN_PAYOUT = 92.0
MIN_SCORE = 6
MAX_PAIRS = 25

TIMEFRAME_M1 = 60
TIMEFRAME_M5 = 300
EXPIRATION_SECONDS = 60

FLORIDA = ZoneInfo("America/New_York")

OTCHARTS_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "Authorization": f"Bearer {OTCHARTS_KEY}",
    "User-Agent": "ROMARIO-TRADER-PRO/4.1",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram secrets are missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )

        if response.status_code == 200:
            logging.info("Telegram message sent.")
            return True

        logging.error("Telegram error: %s", response.text[:300])
        return False

    except requests.RequestException as error:
        logging.error("Telegram connection error: %s", error)
        return False


# =========================================================
# OTCHARTS API
# =========================================================

def api_get(endpoint, params=None):
    if not OTCHARTS_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing.")

    response = requests.get(
        f"{BASE_URL}{endpoint}",
        headers=HEADERS,
        params=params,
        timeout=25,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"OTCharts HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    return response.json()


def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE},
    )

    symbols = data.get("symbols", [])

    # Forex currency pairs only. Exclude stocks and other assets.
    forex_pairs = []

    for item in symbols:
        symbol = item.get("symbol", "")

        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        if item.get("open") is not True:
            continue

        payout = item.get("payout")

        # Never invent a payout when the API returns null.
        if payout is None:
            continue

        try:
            payout = float(payout)
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        if item.get("isOtc") is not True:
            continue

        item["_payout"] = payout
        forex_pairs.append(item)

    forex_pairs.sort(
        key=lambda item: item["_payout"],
        reverse=True,
    )

    return forex_pairs[:MAX_PAIRS]


def get_candles(symbol, timeframe):
    data = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": timeframe,
            "limit": 100,
        },
    )

    candles = data.get("candles", [])

    if not isinstance(candles, list):
        return []

    if data.get("gaps", {}).get("runs", 0) > 0:
        logging.warning("Missing candle gaps for %s", symbol)
        return []

    clean = []

    for candle in candles:
        try:
            clean.append({
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "time": int(candle["time"]),
            })
        except (KeyError, TypeError, ValueError):
            continue

    clean.sort(key=lambda candle: candle["time"])

    # Use completed candles only; the newest returned bar may
    # still be forming. This assumption must be checked against
    # the timestamps in your OTCharts feed.
    if len(clean) > 1:
        clean = clean[:-1]

    return clean


# =========================================================
# INDICATORS
# =========================================================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (value * multiplier) + (
            result * (1 - multiplier)
        )

    return result


def calculate_rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    for index in range(period, len(gains)):
        average_gain = (
            average_gain * (period - 1) + gains[index]
        ) / period

        average_loss = (
            average_loss * (period - 1) + losses[index]
        ) / period

    if average_loss == 0:
        return 100.0

    relative_strength = average_gain / average_loss

    return 100 - (100 / (1 + relative_strength))


# =========================================================
# 4.1 STRATEGY
# =========================================================

def analyze_pair(symbol, payout):
    m1 = get_candles(symbol, TIMEFRAME_M1)
    m5 = get_candles(symbol, TIMEFRAME_M5)

    if len(m1) < 60 or len(m5) < 60:
        return None

    closes_m1 = [c["close"] for c in m1]
    closes_m5 = [c["close"] for c in m5]

    ema9 = ema(closes_m1, 9)
    ema20 = ema(closes_m1, 20)
    ema50 = ema(closes_m1, 50)

    ema20_m5 = ema(closes_m5, 20)
    ema50_m5 = ema(closes_m5, 50)

    rsi = calculate_rsi(closes_m1, 14)

    if None in (
        ema9, ema20, ema50,
        ema20_m5, ema50_m5, rsi
    ):
        return None

    last = m1[-1]
    previous = m1[-2]
    older = m1[-3]

    score_call = 0
    score_put = 0

    # 1. M1 EMA 9 / 20 / 50 direction
    if ema9 > ema20 > ema50:
        score_call += 1

    if ema9 < ema20 < ema50:
        score_put += 1

    # 2. M5 trend confirmation
    if ema20_m5 > ema50_m5:
        score_call += 1

    if ema20_m5 < ema50_m5:
        score_put += 1

    # 3. First candle momentum
    if last["close"] > last["open"]:
        score_call += 1

    if last["close"] < last["open"]:
        score_put += 1

    # 4. Second candle momentum
    if previous["close"] > previous["open"]:
        score_call += 1

    if previous["close"] < previous["open"]:
        score_put += 1

    # 5. RSI confirmation
    if 50 <= rsi <= 68:
        score_call += 1

    if 32 <= rsi <= 50:
        score_put += 1

    # Reject extreme RSI conditions
    if rsi > 75 or rsi < 25:
        return None

    # 6. Candle body strength
    candle_range = last["high"] - last["low"]

    if candle_range <= 0:
        return None

    body_ratio = abs(
        last["close"] - last["open"]
    ) / candle_range

    if body_ratio >= 0.25:
        if last["close"] > last["open"]:
            score_call += 1
        elif last["close"] < last["open"]:
            score_put += 1

    # 7. Price position relative to EMA20
    if last["close"] > ema20:
        score_call += 1

    if last["close"] < ema20:
        score_put += 1

    if score_call >= MIN_SCORE and score_call > score_put:
        direction = "CALL"
        score = score_call

    elif score_put >= MIN_SCORE and score_put > score_call:
        direction = "PUT"
        score = score_put

    else:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "rsi": rsi,
        "price": last["close"],
        "payout": payout,
        "m1_time": last["time"],
    }


# =========================================================
# ENTRY TIME
# =========================================================

def next_entry_time():
    now = datetime.now(FLORIDA)

    next_minute = now.replace(
        second=0,
        microsecond=0,
    ) + timedelta(minutes=1)

    signal_time = next_minute - timedelta(seconds=30)

    # If scanning took too long, skip the missed entry.
    if now >= signal_time:
        next_minute += timedelta(minutes=1)
        signal_time = next_minute - timedelta(seconds=30)

    return signal_time, next_minute


def wait_until(target):
    while True:
        now = datetime.now(FLORIDA)
        remaining = (target - now).total_seconds()

        if remaining <= 0:
            return

        time.sleep(min(remaining, 2))


# =========================================================
# MESSAGE
# =========================================================

def build_signal(candidate, entry_time):
    direction = candidate["direction"]

    if direction == "CALL":
        emoji = "🟢"
        action = "BUY / CALL"
    else:
        emoji = "🔴"
        action = "SELL / PUT"

    signal_time = entry_time - timedelta(seconds=30)
    expiration = entry_time + timedelta(seconds=60)

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"{emoji} <b>{action}</b>\n\n"
        f"💱 PAIR: <b>{candidate['symbol']}</b>\n"
        f"💰 PAYOUT: <b>{candidate['payout']:.1f}%</b>\n"
        f"📊 TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🕒 SIGNAL TIME: "
        f"{signal_time.strftime('%I:%M:%S %p')}\n"
        f"🎯 ENTRY TIME: "
        f"{entry_time.strftime('%I:%M:%S %p')}\n"
        f"🏁 EXPIRATION TIME: "
        f"{expiration.strftime('%I:%M:%S %p')}\n"
        f"💵 PRICE: {candidate['price']}\n"
        f"📈 RSI: {candidate['rsi']:.2f}\n"
        f"⭐ TECHNICAL SCORE: "
        f"{candidate['score']}/7\n\n"
        f"🕐 TIMEZONE: Florida\n"
        f"📌 PAYOUT SOURCE: OTCharts\n"
        f"⚪ VERIFIED WIN RATE: NOT AVAILABLE\n\n"
        f"⚠️ Demo test recommended."
    )


# =========================================================
# MAIN
# =========================================================

def main():
    print("=" * 45)
    print(VERSION)
    print("POCKET OPTION OTC SCANNER")
    print("=" * 45)

    try:
        pairs = get_symbols()

        print(f"Pairs with payout >= {MIN_PAYOUT}%: {len(pairs)}")

        if not pairs:
            send_telegram(
                f"🤖 <b>{VERSION}</b>\n\n"
                "⚪ No eligible Forex OTC pair found.\n"
                "Payout must be verified at 92% or higher.\n"
                "No signal was forced."
            )
            return

        candidates = []

        for index, item in enumerate(pairs, start=1):
            symbol = item["symbol"]
            payout = item["_payout"]

            print(
                f"[{index}/{len(pairs)}] "
                f"Scanning {symbol} | payout {payout}%"
            )

            try:
                result = analyze_pair(symbol, payout)

                if result:
                    candidates.append(result)
                    print(
                        f"VALID: {result['direction']} "
                        f"{symbol} "
                        f"score={result['score']}/7"
                    )

            except Exception as error:
                logging.warning(
                    "Could not analyze %s: %s",
                    symbol,
                    error,
                )

        if not candidates:
            send_telegram(
                f"🤖 <b>{VERSION}</b>\n\n"
                "⚪ No setup passed the 6/7 filter.\n"
                "No trade signal was forced."
            )
            return

        # Best technical score first, then highest payout.
        candidates.sort(
            key=lambda item: (
                item["score"],
                item["payout"],
            ),
            reverse=True,
        )

        best = candidates[0]

        signal_time, entry_time = next_entry_time()

        print(
            f"BEST: {best['symbol']} "
            f"{best['direction']} "
            f"{best['score']}/7"
        )

        print(
            "Signal scheduled:",
            signal_time.strftime("%I:%M:%S %p"),
        )

        print(
            "Entry scheduled:",
            entry_time.strftime("%I:%M:%S %p"),
        )

        wait_until(signal_time)

        # Do not send a late signal.
        if datetime.now(FLORIDA) >= entry_time:
            print("Entry time missed. Signal cancelled.")
            return

        message = build_signal(best, entry_time)
        send_telegram(message)

        print("Signal process completed.")

    except Exception as error:
        logging.exception("Bot error")

        send_telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            f"🔴 BOT ERROR\n"
            f"<code>{str(error)[:500]}</code>"
        )


if __name__ == "__main__":
    main()
