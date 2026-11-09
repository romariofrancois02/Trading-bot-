
import os
import time
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =====================================================
# ROMARIO TRADER PRO 8.3
# OTC FOREX | M1 | 60 SECONDS
# TELEGRAM ALERTS ONLY
# 3 STRATEGIES | 2/3 CONFIRMATIONS
# =====================================================

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 60
CANDLE_LIMIT = 300
OTC_OFFSET = 7200

MIN_PAYOUT = 92.0
MIN_VOTES = 2
MAX_PAIRS = 12

FLORIDA = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD",
    "NZD", "SGD", "HKD", "NOK", "SEK", "DKK", "PLN",
    "CZK", "HUF", "TRY", "ZAR", "MXN", "BRL", "INR",
    "THB", "PHP", "IDR", "MYR", "AED", "SAR", "PKR",
    "EGP", "NGN", "KES", "COP", "CLP", "PEN", "ARS",
    "KRW", "CNY"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

http = requests.Session()
http.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "RomarioTrader/8.3",
    "Accept": "application/json"
})


# ---------------- TIME ----------------

def florida_now():
    return datetime.now(FLORIDA)


def entry_time():
    now = florida_now()
    entry = now.replace(second=0, microsecond=0)
    entry += timedelta(minutes=1)
    return entry.strftime("%I:%M:%S %p")


# ---------------- TELEGRAM ----------------

def telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        logging.error("Telegram secrets missing")
        return False

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TG_CHAT,
                "text": message
            },
            timeout=20
        )

        data = response.json()

        if response.ok and data.get("ok"):
            logging.info("Telegram message sent")
            return True

        logging.error("Telegram response: %s", data)
        return False

    except Exception as exc:
        logging.error("Telegram error: %s", exc)
        return False


# ---------------- API ----------------

def api_get(endpoint, params=None):
    for attempt in range(3):
        try:
            response = http.get(
                BASE_URL + endpoint,
                params=params,
                timeout=25
            )

            if response.status_code == 200:
                return response.json()

            logging.warning(
                "%s HTTP %s: %s",
                endpoint,
                response.status_code,
                response.text[:250]
            )

            if response.status_code in (401, 402, 403, 429):
                raise RuntimeError(
                    f"OTCharts HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )

        except requests.RequestException as exc:
            logging.warning(
                "API attempt %s: %s",
                attempt + 1,
                exc
            )

        if attempt < 2:
            time.sleep(2)

    return None


def extract_list(data, keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]

    return []


# ---------------- PAIRS ----------------

def valid_forex_pair(symbol):
    if not isinstance(symbol, str):
        return False

    if not symbol.endswith("_otc"):
        return False

    pair = symbol[:-4]

    if len(pair) != 6 or not pair.isalpha():
        return False

    base = pair[:3]
    quote = pair[3:]

    return (
        base in CURRENCIES
        and quote in CURRENCIES
        and base != quote
    )


def get_pairs():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    if not data:
        raise RuntimeError("Could not retrieve OTC symbols")

    symbols = extract_list(data, ("symbols", "data"))
    pairs = []

    for item in symbols:
        if not isinstance(item, dict):
            continue

        symbol = item.get("symbol")

        if not valid_forex_pair(symbol):
            continue

        if item.get("isOtc") is not True:
            continue

        if item.get("open") is not True:
            continue

        try:
            payout = float(item["payout"])
        except (TypeError, ValueError, KeyError):
            continue

        if payout < MIN_PAYOUT:
            continue

        pairs.append({
            "symbol": symbol,
            "payout": payout
        })

    pairs.sort(
        key=lambda item: item["payout"],
        reverse=True
    )

    if pairs:
        offset = (int(time.time()) // 300) % len(pairs)
        pairs = pairs[offset:] + pairs[:offset]

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
        return [], None

    rows = extract_list(data, ("candles", "data"))
    gaps = data.get("gaps") if isinstance(data, dict) else None

    unique = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        try:
            candle = {
                "time": int(float(row["time"])),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            }

            if min(
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"]
            ) <= 0:
                continue

            if candle["high"] < max(
                candle["open"], candle["close"]
            ):
                continue

            if candle["low"] > min(
                candle["open"], candle["close"]
            ):
                continue

            unique[candle["time"]] = candle

        except (KeyError, TypeError, ValueError):
            continue

    candles = sorted(
        unique.values(),
        key=lambda item: item["time"]
    )

    now = time.time()

    completed = [
        candle for candle in candles
        if candle["time"] - OTC_OFFSET + TIMEFRAME <= now
    ]

    return completed, gaps


# ---------------- INDICATORS ----------------

def ema(values, period):
    if len(values) < period:
        return []

    result = [None] * (period - 1)
    current = sum(values[:period]) / period
    result.append(current)

    alpha = 2 / (period + 1)

    for value in values[period:]:
        current = (
            alpha * value
            + (1 - alpha) * current
        )
        result.append(current)

    return result


def rsi_series(values, period=14):
    if len(values) < period + 1:
        return []

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(value, 0) for value in changes]
    losses = [max(-value, 0) for value in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = []

    def calculate(gain, loss):
        if loss == 0:
            return 100.0
        if gain == 0:
            return 0.0
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    result.append(calculate(avg_gain, avg_loss))

    for index in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[index]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[index]
        ) / period

        result.append(calculate(avg_gain, avg_loss))

    return result


def bollinger(values, period=20):
    if len(values) < period:
        return None

    sample = values[-period:]
    middle = sum(sample) / period

    variance = sum(
        (value - middle) ** 2
        for value in sample
    ) / period

    deviation = variance ** 0.5

    return (
        middle - 2 * deviation,
        middle,
        middle + 2 * deviation
    )


# ---------------- STRATEGIES ----------------

def strategy_votes(candles):
    result = {
        "CALL": 0,
        "PUT": 0,
        "call_reasons": [],
        "put_reasons": []
    }

    if len(candles) < 60:
        return result

    closes = [c["close"] for c in candles]
    current = candles[-1]
    previous = candles[-2]

    bullish = current["close"] > current["open"]
    bearish = current["close"] < current["open"]

    # 1. EMA TREND
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)

    if e9 and e21 and e50:

        if (
            e9[-1] > e21[-1] > e50[-1]
            and current["close"] > e9[-1]
            and e9[-1] > e9[-2]
            and bullish
        ):
            result["CALL"] += 1
            result["call_reasons"].append("EMA bullish trend")

        elif (
            e9[-1] < e21[-1] < e50[-1]
            and current["close"] < e9[-1]
            and e9[-1] < e9[-2]
            and bearish
        ):
            result["PUT"] += 1
            result["put_reasons"].append("EMA bearish trend")

    # 2. RSI MOMENTUM
    rsi_values = rsi_series(closes)

    if len(rsi_values) >= 2:
        current_rsi = rsi_values[-1]
        previous_rsi = rsi_values[-2]

        if (
            50 < current_rsi < 68
            and current_rsi > previous_rsi
            and bullish
        ):
            result["CALL"] += 1
            result["call_reasons"].append("RSI bullish momentum")

        elif (
            32 < current_rsi < 50
            and current_rsi < previous_rsi
            and bearish
        ):
            result["PUT"] += 1
            result["put_reasons"].append("RSI bearish momentum")

    # 3. BOLLINGER REVERSAL
    old_bands = bollinger(closes[:-1])
    new_bands = bollinger(closes)

    if old_bands and new_bands:

        if (
            previous["low"] <= old_bands[0]
            and current["close"] > new_bands[0]
            and bullish
        ):
            result["CALL"] += 1
            result["call_reasons"].append("Bollinger lower bounce")

        elif (
            previous["high"] >= old_bands[2]
            and current["close"] < new_bands[2]
            and bearish
        ):
            result["PUT"] += 1
            result["put_reasons"].append("Bollinger upper rejection")

    return result


def get_direction(votes):
    call = votes["CALL"]
    put = votes["PUT"]

    if call >= MIN_VOTES and call > put:
        return "CALL"

    if put >= MIN_VOTES and put > call:
        return "PUT"

    return None


# ---------------- REPORT ----------------

def no_signal(pairs, reasons):
    now_text = florida_now().strftime(
        "%Y-%m-%d %I:%M:%S %p"
    )

    detail = "\n".join(
        f"• {key}: {value}"
        for key, value in reasons.items()
    )

    message = (
        "🤖 ROMARIO TRADER PRO 8.3\n\n"
        "⚪ NO VALID SIGNAL\n"
        "No setup reached 2/3 confirmations.\n\n"
        f"Pairs scanned: {len(pairs)}\n"
        f"Minimum payout: {MIN_PAYOUT}%\n"
        "Strategies: EMA + RSI + Bollinger\n"
        "Required confirmations: 2/3\n\n"
        "📋 SCAN REPORT:\n"
        f"{detail}\n\n"
        f"🕒 FLORIDA TIME: {now_text}"
    )

    print(message)
    telegram(message)


# ---------------- MAIN ----------------

def main():
    print("=" * 55)
    print("ROMARIO TRADER PRO 8.3")
    print("3 STRATEGY CONFIRMATION SYSTEM")
    print("=" * 55)

    if not API_KEY:
        raise RuntimeError("Missing OTCHARTS_API_KEY")

    if not TG_TOKEN or not TG_CHAT:
        raise RuntimeError("Missing Telegram secrets")

    pairs = get_pairs()

    print("Pairs selected:", len(pairs))

    reasons = {
        "Insufficient candles": 0,
        "Recent candle gaps": 0,
        "No current setup": 0,
        "API/data errors": 0
    }

    if not pairs:
        no_signal(pairs, reasons)
        return

    candidates = []

    for number, pair in enumerate(pairs, 1):
        symbol = pair["symbol"]

        print(
            f"\n[{number}/{len(pairs)}] "
            f"{symbol} | payout {pair['payout']}%"
        )

        try:
            candles, gaps = get_candles(symbol)

            print("Completed candles:", len(candles))

            if len(candles) < 100:
                reasons["Insufficient candles"] += 1
                continue

            # Require the latest candles to be consecutive.
            if (
                candles[-1]["time"] - candles[-2]["time"]
                != TIMEFRAME
                or candles[-2]["time"] - candles[-3]["time"]
                != TIMEFRAME
            ):
                reasons["Recent candle gaps"] += 1
                continue

            votes = strategy_votes(candles)
            direction = get_direction(votes)

            print(
                f"CALL: {votes['CALL']}/3 | "
                f"PUT: {votes['PUT']}/3"
            )

            if not direction:
                reasons["No current setup"] += 1
                continue

            reasons_list = (
                votes["call_reasons"]
                if direction == "CALL"
                else votes["put_reasons"]
            )

            candidates.append({
                "symbol": symbol,
                "payout": pair["payout"],
                "direction": direction,
                "score": votes[direction],
                "reasons": reasons_list
            })

            print(
                "VALID SETUP:",
                direction,
                votes[direction],
                "/3"
            )

        except Exception:
            logging.exception("Pair scan error: %s", symbol)
            reasons["API/data errors"] += 1

    if not candidates:
        no_signal(pairs, reasons)
        return

    # Best setup: highest confirmations, then payout.
    candidates.sort(
        key=lambda item: (
            item["score"],
            item["payout"]
        ),
        reverse=True
    )

    best = candidates[0]

    # Final verification
    print("\nFinal verification:", best["symbol"])

    fresh_candles, fresh_gaps = get_candles(best["symbol"])

    if len(fresh_candles) < 100:
        reasons["API/data errors"] += 1
        no_signal(pairs, reasons)
        return

    if (
        fresh_candles[-1]["time"] - fresh_candles[-2]["time"]
        != TIMEFRAME
        or fresh_candles[-2]["time"] - fresh_candles[-3]["time"]
        != TIMEFRAME
    ):
        reasons["Recent candle gaps"] += 1
        no_signal(pairs, reasons)
        return

    fresh_votes = strategy_votes(fresh_candles)
    fresh_direction = get_direction(fresh_votes)

    if fresh_direction != best["direction"]:
        reasons["No current setup"] += 1
        no_signal(pairs, reasons)
        return

    newest = fresh_candles[-1]["time"] - OTC_OFFSET
    age = time.time() - (newest + TIMEFRAME)

    if age < 0 or age > 90:
        reasons["API/data errors"] += 1
        no_signal(pairs, reasons)
        return

    # Calculate entry after final verification.
    entry = entry_time()
    now_text = florida_now().strftime(
        "%Y-%m-%d %I:%M:%S %p"
    )

    icon = "🟢" if fresh_direction == "CALL" else "🔴"

    confirmations = "\n".join(
        f"• {reason}"
        for reason in (
            fresh_votes["call_reasons"]
            if fresh_direction == "CALL"
            else fresh_votes["put_reasons"]
        )
    )

    message = (
        "🤖 ROMARIO TRADER PRO 8.3\n\n"
        f"{icon} {fresh_direction}\n\n"
        f"💱 PAIR: {best['symbol']}\n"
        f"💰 PAYOUT: {best['payout']:.1f}%\n"
        f"📊 STRATEGY SCORE: "
        f"{fresh_votes[fresh_direction]}/3\n"
        "⏱ TIMEFRAME: M1\n"
        "⌛ EXPIRATION: 60 SECONDS\n"
        f"🎯 ENTRY TIME: {entry} (Florida)\n\n"
        "📌 CONFIRMATIONS:\n"
        f"{confirmations}\n\n"
        f"🕒 FLORIDA TIME: {now_text}\n\n"
        "⚠️ Signals are not guaranteed wins."
    )

    print(message)
    telegram(message)


# ---------------- RUN ----------------

if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        logging.exception("BOT FAILED")

        error_message = (
            "🤖 ROMARIO TRADER PRO 8.3\n\n"
            "⚠️ BOT ERROR\n"
            f"{str(exc)[:400]}\n"
            "🕒 FLORIDA TIME: "
            f"{florida_now().strftime('%Y-%m-%d %I:%M:%S %p')}"
        )

        telegram(error_message)
        raise
