
import os
import re
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# =====================================================
# ROMARIO TRADER PRO 7.0
# 8 STRATEGY VOTING SYSTEM
# =====================================================

VERSION = "ROMARIO TRADER PRO 7.0"

BASE_URL = "https://otcharts.com"
MIN_PAYOUT = 92.0
MIN_VOTES = 4
MAX_PAIRS = 25

M1 = 60
M5 = 300
EXPIRATION = 60

TZ = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {"Authorization": f"Bearer {API_KEY}"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =====================================================
# TELEGRAM
# =====================================================

def telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        logging.error("Telegram secrets missing")
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={
                "chat_id": TG_CHAT,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=20
        )
        r.raise_for_status()
    except Exception as e:
        logging.error("Telegram error: %s", e)


# =====================================================
# API
# =====================================================

def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY missing")

    r = requests.get(
        BASE_URL + endpoint,
        headers=HEADERS,
        params=params,
        timeout=25
    )
    r.raise_for_status()
    return r.json()


def get_pairs():
    data = api_get("/v1/symbols", {"venue": "otc"})
    pairs = data.get("symbols", [])

    valid = []

    for p in pairs:
        symbol = p.get("symbol", "")

        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        try:
            payout = float(p["payout"])
        except (KeyError, TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        valid.append({
            "symbol": symbol,
            "payout": payout
        })

    valid.sort(key=lambda x: x["payout"], reverse=True)
    return valid[:MAX_PAIRS]


def get_candles(symbol, tf):
    data = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": tf,
            "limit": 150
        }
    )

    rows = data.get("candles", [])
    result = []

    for c in rows:
        try:
            result.append({
                "time": int(c.get("time", c.get("timestamp"))),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"])
            })
        except (TypeError, ValueError, KeyError):
            continue

    result.sort(key=lambda x: x["time"])

    # Pwovizwa: retire dènye bouji a si li poko fini.
    # Verifye timestamp OTCharts yo anvan nou konte sou sa.
    if len(result) > 1:
        result = result[:-1]

    return result


# =====================================================
# INDICATORS
# =====================================================

def ema(values, period):
    if len(values) < period:
        return []

    result = [sum(values[:period]) / period]
    alpha = 2 / (period + 1)

    for value in values[period:]:
        result.append(
            alpha * value + (1 - alpha) * result[-1]
        )

    return [None] * (period - 1) + result


def rsi(values, period=14):
    if len(values) <= period:
        return []

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = [None] * period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100 - 100 / (1 + rs))

    return result


def bollinger(values, period=20, multiplier=2):
    upper, middle, lower = [], [], []

    for i in range(len(values)):
        if i < period - 1:
            upper.append(None)
            middle.append(None)
            lower.append(None)
            continue

        window = values[i - period + 1:i + 1]
        avg = sum(window) / period
        variance = sum((x - avg) ** 2 for x in window) / period
        sd = variance ** 0.5

        middle.append(avg)
        upper.append(avg + multiplier * sd)
        lower.append(avg - multiplier * sd)

    return upper, middle, lower


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    line = []

    for a, b in zip(fast, slow):
        if a is None or b is None:
            line.append(None)
        else:
            line.append(a - b)

    valid = [x for x in line if x is not None]
    signal_valid = ema(valid, 9)

    signal = [None] * (len(line) - len(signal_valid))
    signal.extend(signal_valid)

    return line, signal


def stochastic(candles, period=14, smooth=3):
    k = []

    for i in range(len(candles)):
        if i < period - 1:
            k.append(None)
            continue

        window = candles[i - period + 1:i + 1]
        highest = max(x["high"] for x in window)
        lowest = min(x["low"] for x in window)

        if highest == lowest:
            k.append(50.0)
        else:
            k.append(
                100 * (candles[i]["close"] - lowest)
                / (highest - lowest)
            )

    d = []

    for i in range(len(k)):
        values = k[max(0, i - smooth + 1):i + 1]
        values = [x for x in values if x is not None]

        if len(values) < smooth:
            d.append(None)
        else:
            d.append(sum(values) / len(values))

    return k, d


def supertrend(candles, period=10, multiplier=3):
    atr = [None] * len(candles)
    tr = []

    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c["high"] - c["low"])
        else:
            prev = candles[i - 1]["close"]
            tr.append(max(
                c["high"] - c["low"],
                abs(c["high"] - prev),
                abs(c["low"] - prev)
            ))

    for i in range(period - 1, len(candles)):
        atr[i] = sum(tr[i - period + 1:i + 1]) / period

    trend = [None] * len(candles)
    upper_band = [None] * len(candles)
    lower_band = [None] * len(candles)

    for i, c in enumerate(candles):
        if atr[i] is None:
            continue

        mid = (c["high"] + c["low"]) / 2
        basic_upper = mid + multiplier * atr[i]
        basic_lower = mid - multiplier * atr[i]

        if i == 0 or trend[i - 1] is None:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
            trend[i] = 1 if c["close"] >= mid else -1
            continue

        prev_upper = upper_band[i - 1]
        prev_lower = lower_band[i - 1]
        prev_close = candles[i - 1]["close"]

        upper_band[i] = (
            min(basic_upper, prev_upper)
            if prev_close <= prev_upper else basic_upper
        )

        lower_band[i] = (
            max(basic_lower, prev_lower)
            if prev_close >= prev_lower else basic_lower
        )

        if trend[i - 1] == -1 and c["close"] > upper_band[i]:
            trend[i] = 1
        elif trend[i - 1] == 1 and c["close"] < lower_band[i]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]

    return trend


# =====================================================
# STRATEGY VOTING
# =====================================================

def analyze(candles):
    if len(candles) < 60:
        return None

    close = [c["close"] for c in candles]

    e9 = ema(close, 9)
    e21 = ema(close, 21)
    e50 = ema(close, 50)

    rv = rsi(close, 14)
    bu, bm, bl = bollinger(close)

    ml, ms = macd(close)
    sk, sd = stochastic(candles)
    st = supertrend(candles)

    i = len(candles) - 1
    p = i - 1

    needed = [
        e9[i], e9[p], e21[i], e21[p],
        e50[i], e50[p], rv[i], rv[p],
        bu[i], bm[i], bl[i], bu[p], bl[p],
        ml[i], ml[p], ms[i], ms[p],
        sk[i], sk[p], sd[i], sd[p],
        st[i], st[p]
    ]

    if any(x is None for x in needed):
        return None

    c = candles[i]
    prev = candles[p]

    call = []
    put = []

    def vote(condition, direction, name):
        if condition:
            if direction == "CALL":
                call.append(name)
            else:
                put.append(name)

    # 1. RSI REVERSAL
    vote(
        rv[p] < 30 and rv[i] >= 30,
        "CALL", "RSI Reversal"
    )
    vote(
        rv[p] > 70 and rv[i] <= 70,
        "PUT", "RSI Reversal"
    )

    # 2. EMA CROSS
    vote(
        e9[p] <= e21[p] and e9[i] > e21[i],
        "CALL", "EMA Cross"
    )
    vote(
        e9[p] >= e21[p] and e9[i] < e21[i],
        "PUT", "EMA Cross"
    )

    # 3. BOLLINGER BOUNCE
    vote(
        prev["low"] <= bl[p] and c["close"] > bl[i],
        "CALL", "Bollinger Bounce"
    )
    vote(
        prev["high"] >= bu[p] and c["close"] < bu[i],
        "PUT", "Bollinger Bounce"
    )

    # 4. MACD CROSS
    vote(
        ml[p] <= ms[p] and ml[i] > ms[i],
        "CALL", "MACD Cross"
    )
    vote(
        ml[p] >= ms[p] and ml[i] < ms[i],
        "PUT", "MACD Cross"
    )

    # 5. STOCHASTIC CROSS
    vote(
        sk[p] <= sd[p] and sk[i] > sd[i]
        and sk[i] < 25,
        "CALL", "Stochastic Cross"
    )
    vote(
        sk[p] >= sd[p] and sk[i] < sd[i]
        and sk[i] > 75,
        "PUT", "Stochastic Cross"
    )

    # 6. SUPERTREND FLIP
    vote(
        st[p] == -1 and st[i] == 1,
        "CALL", "SuperTrend Flip"
    )
    vote(
        st[p] == 1 and st[i] == -1,
        "PUT", "SuperTrend Flip"
    )

    # 7. COMBO: RSI + BOLLINGER + EMA TREND
    combo_call = sum([
        rv[i] > rv[p] and rv[i] < 50,
        c["close"] > bl[i],
        e9[i] > e21[i]
    ])

    combo_put = sum([
        rv[i] < rv[p] and rv[i] > 50,
        c["close"] < bu[i],
        e9[i] < e21[i]
    ])

    vote(combo_call >= 2, "CALL", "Combo")
    vote(combo_put >= 2, "PUT", "Combo")

    # 8. TRIPLE EMA: FIRST ALIGNMENT
    vote(
        e9[i] > e21[i] > e50[i]
        and not (e9[p] > e21[p] > e50[p]),
        "CALL", "Triple EMA"
    )
    vote(
        e9[i] < e21[i] < e50[i]
        and not (e9[p] < e21[p] < e50[p]),
        "PUT", "Triple EMA"
    )

    # Require a clear majority, not a tie.
    if len(call) >= MIN_VOTES and len(call) > len(put):
        direction = "CALL"
        votes = call

    elif len(put) >= MIN_VOTES and len(put) > len(call):
        direction = "PUT"
        votes = put

    else:
        return None

    return {
        "direction": direction,
        "votes": len(votes),
        "strategies": votes,
        "rsi": rv[i],
        "price": c["close"],
        "candle_time": c["time"]
    }


# =====================================================
# ENTRY AND MESSAGE
# =====================================================

def next_entry():
    now = datetime.now(TZ)

    entry = now.replace(
        second=0, microsecond=0
    ) + timedelta(minutes=1)

    signal = entry - timedelta(seconds=30)

    if now >= signal:
        entry += timedelta(minutes=1)
        signal = entry - timedelta(seconds=30)

    return signal, entry


def wait_until(target):
    while True:
        remaining = (
            target - datetime.now(TZ)
        ).total_seconds()

        if remaining <= 0:
            return

        time.sleep(min(remaining, 2))


def make_message(pair, payout, result, entry):
    if result["direction"] == "CALL":
        emoji = "🟢"
        action = "BUY / CALL"
    else:
        emoji = "🔴"
        action = "SELL / PUT"

    signal = entry - timedelta(seconds=30)
    expiration = entry + timedelta(seconds=EXPIRATION)

    strategy_text = "\n".join(
        f"✓ {name}" for name in result["strategies"]
    )

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"{emoji} <b>{action}</b>\n\n"
        f"💱 PAIR: <b>{pair}</b>\n"
        f"💰 PAYOUT: <b>{payout:.1f}%</b>\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🕒 SIGNAL TIME: {signal.strftime('%I:%M:%S %p')}\n"
        f"🎯 ENTRY TIME: {entry.strftime('%I:%M:%S %p')}\n"
        f"🏁 EXPIRATION: {expiration.strftime('%I:%M:%S %p')}\n\n"
        f"⭐ STRATEGY VOTES: {result['votes']}/8\n"
        f"{strategy_text}\n\n"
        f"📊 RSI: {result['rsi']:.2f}\n"
        f"💵 PRICE: {result['price']}\n"
        f"🕐 FLORIDA TIME\n"
        f"⚠️ DEMO TEST RECOMMENDED"
    )


# =====================================================
# MAIN
# =====================================================

def main():
    print("=" * 45)
    print(VERSION)
    print("8 STRATEGY VOTING SYSTEM")
    print("=" * 45)

    pairs = get_pairs()
    print(f"Eligible OTC Forex pairs: {len(pairs)}")

    candidates = []

    for number, pair in enumerate(pairs, 1):
        symbol = pair["symbol"]
        payout = pair["payout"]

        print(
            f"[{number}/{len(pairs)}] "
            f"Scanning {symbol} | {payout}%"
        )

        try:
            candles = get_candles(symbol, M1)
            result = analyze(candles)

            if result:
                result["symbol"] = symbol
                result["payout"] = payout
                candidates.append(result)

                print(
                    f"VALID {result['direction']} "
                    f"{symbol} "
                    f"{result['votes']}/8"
                )

        except Exception as error:
            logging.warning("%s: %s", symbol, error)

    if not candidates:
        telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            "⚪ No pair reached the required 4/8 votes.\n"
            "No signal generated."
        )
        return

    candidates.sort(
        key=lambda x: (x["votes"], x["payout"]),
        reverse=True
    )

    best = candidates[0]
    signal_time, entry_time = next_entry()

    print("BEST:", best["symbol"], best["direction"])
    print("VOTES:", best["votes"], "/8")
    print("SIGNAL:", signal_time)
    print("ENTRY:", entry_time)

    wait_until(signal_time)

    if datetime.now(TZ) >= entry_time:
        print("Entry time missed. Cancelled.")
        return

    message = make_message(
        best["symbol"],
        best["payout"],
        best,
        entry_time
    )

    telegram(message)
    print("Signal sent.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.exception("BOT FAILED")
        telegram(
            f"🤖 <b>{VERSION}</b>\n"
            f"🔴 BOT ERROR\n"
            f"<code>{str(error)[:400]}</code>"
        )
