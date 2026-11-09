
import os
import time
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =====================================================
# ROMARIO TRADER PRO 8.0
# OTC M1 | 60 SECONDS | TELEGRAM ALERTS ONLY
# =====================================================

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 60
CANDLE_LIMIT = 300
OTC_CLOCK_OFFSET = 7200

MIN_PAYOUT = 92.0
MIN_VOTES = 2
MIN_BACKTEST_TRADES = 12
MIN_BACKTEST_WINRATE = 56.0

MAX_PAIRS = 12
FLORIDA = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

http = requests.Session()
http.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "RomarioTrader/8.0",
    "Accept": "application/json"
})


# ---------------- TIME ----------------

def florida_now():
    return datetime.now(FLORIDA)


def entry_time():
    now = florida_now()
    future = now.replace(second=0, microsecond=0)
    future += timedelta(minutes=1)
    return future.strftime("%I:%M:%S %p")


# ---------------- TELEGRAM ----------------

def send_telegram(message):
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
            logging.info("Telegram sent")
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
                response.text[:200]
            )

            if response.status_code in (401, 402, 403, 429):
                raise RuntimeError(
                    f"OTCharts HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )

        except requests.RequestException as exc:
            logging.warning("API attempt %s: %s", attempt + 1, exc)

        if attempt < 2:
            time.sleep(2)

    return None


def get_list(data, keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


# ---------------- PAIRS ----------------

def valid_forex_otc(symbol):
    if not isinstance(symbol, str):
        return False

    if not symbol.endswith("_otc"):
        return False

    pair = symbol[:-4]

    return len(pair) == 6 and pair.isalpha()


def get_pairs():
    data = api_get("/v1/symbols", {"venue": VENUE})

    if not data:
        raise RuntimeError("Cannot retrieve OTC symbols")

    symbols = get_list(data, ("symbols", "data"))
    pairs = []

    for item in symbols:
        if not isinstance(item, dict):
            continue

        symbol = item.get("symbol")

        if not valid_forex_otc(symbol):
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

    pairs.sort(key=lambda x: x["payout"], reverse=True)

    # Rotate the scan order every five minutes.
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
        return []

    rows = get_list(data, ("candles", "data"))
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

            unique[candle["time"]] = candle

        except (KeyError, TypeError, ValueError):
            continue

    candles = sorted(unique.values(), key=lambda x: x["time"])

    # Exclude any candle that has not completed.
    now = time.time()

    return [
        c for c in candles
        if c["time"] - OTC_CLOCK_OFFSET + TIMEFRAME <= now
    ]


# ---------------- INDICATORS ----------------

def ema(values, period):
    if len(values) < period:
        return []

    result = [None] * (period - 1)
    current = sum(values[:period]) / period
    result.append(current)

    alpha = 2 / (period + 1)

    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current
        result.append(current)

    return result


def rsi(values, period=14):
    if len(values) < period + 2:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def bollinger(values, period=20):
    if len(values) < period:
        return None

    sample = values[-period:]
    middle = sum(sample) / period

    variance = sum(
        (x - middle) ** 2 for x in sample
    ) / period

    deviation = variance ** 0.5

    return (
        middle - 2 * deviation,
        middle,
        middle + 2 * deviation
    )


def stochastic(candles, period=14):
    if len(candles) < period + 4:
        return None

    def k_at(end):
        window = candles[end - period + 1:end + 1]
        highest = max(c["high"] for c in window)
        lowest = min(c["low"] for c in window)

        if highest == lowest:
            return 50.0

        return 100 * (
            candles[end]["close"] - lowest
        ) / (highest - lowest)

    k_previous = k_at(len(candles) - 2)
    k_current = k_at(len(candles) - 1)

    # D is a three-period average of K.
    previous_d = sum(
        k_at(i)
        for i in range(len(candles) - 4, len(candles) - 1)
    ) / 3

    current_d = sum(
        k_at(i)
        for i in range(len(candles) - 3, len(candles))
    ) / 3

    return (
        k_previous,
        k_current,
        previous_d,
        current_d
    )


# ---------------- SIX STRATEGIES ----------------

def strategy_votes(candles):
    if len(candles) < 60:
        return {"CALL": 0, "PUT": 0}

    closes = [c["close"] for c in candles]
    previous = candles[-2]
    current = candles[-1]

    call = 0
    put = 0

    # 1. EMA trend pullback
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)

    if e9 and e21 and e50:
        if (
            e9[-1] > e21[-1] > e50[-1]
            and previous["close"] <= e9[-2]
            and current["close"] > e9[-1]
        ):
            call += 1

        elif (
            e9[-1] < e21[-1] < e50[-1]
            and previous["close"] >= e9[-2]
            and current["close"] < e9[-1]
        ):
            put += 1

    # 2. RSI + Bollinger reversal
    bands_previous = bollinger(closes[:-1])
    bands_current = bollinger(closes)
    current_rsi = rsi(closes)

    if bands_previous and bands_current and current_rsi is not None:
        if (
            previous["close"] <= bands_previous[0]
            and current["close"] > bands_current[0]
            and current_rsi < 45
        ):
            call += 1

        elif (
            previous["close"] >= bands_previous[2]
            and current["close"] < bands_current[2]
            and current_rsi > 55
        ):
            put += 1

    # 3. MACD crossover
    fast = ema(closes, 12)
    slow = ema(closes, 26)

    if fast and slow:
        macd_line = [
            fast[i] - slow[i]
            for i in range(len(closes))
            if fast[i] is not None and slow[i] is not None
        ]

        signal = ema(macd_line, 9)

        if len(signal) >= 2:
            if (
                macd_line[-2] <= signal[-2]
                and macd_line[-1] > signal[-1]
            ):
                call += 1

            elif (
                macd_line[-2] >= signal[-2]
                and macd_line[-1] < signal[-1]
            ):
                put += 1

    # 4. Stochastic crossover
    stoch = stochastic(candles)

    if stoch:
        kp, kc, dp, dc = stoch

        if kp <= dp and kc > dc and kc < 30:
            call += 1

        elif kp >= dp and kc < dc and kc > 70:
            put += 1

    # 5. Ten-candle breakout
    if len(candles) >= 12:
        prior = candles[-11:-1]
        highest = max(c["high"] for c in prior)
        lowest = min(c["low"] for c in prior)

        if (
            current["close"] > highest
            and current["close"] > current["open"]
            and e21[-1] > e21[-2]
        ):
            call += 1

        elif (
            current["close"] < lowest
            and current["close"] < current["open"]
            and e21[-1] < e21[-2]
        ):
            put += 1

    # 6. Engulfing candle
    bullish_engulfing = (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )

    bearish_engulfing = (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )

    if bullish_engulfing:
        call += 1

    elif bearish_engulfing:
        put += 1

    return {"CALL": call, "PUT": put}


# ---------------- HISTORICAL TEST ----------------

def backtest(candles):
    wins = 0
    losses = 0

    # Each historical signal is evaluated against the
    # following candle's open and close (one-minute expiry).
    for index in range(60, len(candles) - 1):
        history = candles[:index + 1]
        votes = strategy_votes(history)

        if votes["CALL"] >= MIN_VOTES and votes["CALL"] > votes["PUT"]:
            direction = "CALL"

        elif votes["PUT"] >= MIN_VOTES and votes["PUT"] > votes["CALL"]:
            direction = "PUT"

        else:
            continue

        next_candle = candles[index + 1]

        if next_candle["close"] == next_candle["open"]:
            continue

        if (
            direction == "CALL"
            and next_candle["close"] > next_candle["open"]
        ):
            wins += 1

        elif (
            direction == "PUT"
            and next_candle["close"] < next_candle["open"]
        ):
            wins += 1

        else:
            losses += 1

    total = wins + losses

    if total == 0:
        return 0, 0.0

    return total, (wins / total) * 100


# ---------------- SCANNER ----------------

def main():
    print("=" * 50)
    print("ROMARIO TRADER PRO 8.0")
    print("=" * 50)

    if not API_KEY:
        raise RuntimeError("Missing OTCHARTS_API_KEY")

    if not TG_TOKEN or not TG_CHAT:
        raise RuntimeError("Missing Telegram secrets")

    pairs = get_pairs()

    print("Pairs selected:", len(pairs))
    print("Minimum payout:", MIN_PAYOUT)

    candidates = []

    for number, pair in enumerate(pairs, 1):
        symbol = pair["symbol"]
        payout = pair["payout"]

        print(f"[{number}/{len(pairs)}] {symbol} | {payout}%")

        try:
            candles = get_candles(symbol)

            print("Completed candles:", len(candles))

            if len(candles) < 100:
                print("Skipped: insufficient candles")
                continue

            total, winrate = backtest(candles)

            print(
                f"Historical signals: {total} "
                f"| Historical win rate: {winrate:.1f}%"
            )

            if total < MIN_BACKTEST_TRADES:
                print("Skipped: insufficient backtest sample")
                continue

            if winrate < MIN_BACKTEST_WINRATE:
                print("Skipped: historical win rate below threshold")
                continue

            votes = strategy_votes(candles)

            if (
                votes["CALL"] >= MIN_VOTES
                and votes["CALL"] > votes["PUT"]
            ):
                direction = "CALL"
                score = votes["CALL"]

            elif (
                votes["PUT"] >= MIN_VOTES
                and votes["PUT"] > votes["CALL"]
            ):
                direction = "PUT"
                score = votes["PUT"]

            else:
                print("No current setup")
                continue

            candidates.append({
                "symbol": symbol,
                "payout": payout,
                "direction": direction,
                "score": score,
                "total": total,
                "winrate": winrate
            })

        except Exception as exc:
            logging.exception("%s scan error: %s", symbol, exc)

    # Best historical rate, then more tested signals,
    # then stronger current vote count and payout.
    candidates.sort(
        key=lambda x: (
            x["winrate"],
            x["total"],
            x["score"],
            x["payout"]
        ),
        reverse=True
    )

    now_text = florida_now().strftime("%Y-%m-%d %I:%M:%S %p")

    if not candidates:
        message = (
            "🤖 ROMARIO TRADER PRO 8.0\n\n"
            "⚪ NO VALID SIGNAL\n"
            "No pair passed all filters.\n\n"
            f"Pairs scanned: {len(pairs)}\n"
            f"Minimum payout: {MIN_PAYOUT}%\n"
            f"Minimum historical sample: {MIN_BACKTEST_TRADES}\n"
            f"Minimum historical win rate: {MIN_BACKTEST_WINRATE}%\n"
            f"🕒 FLORIDA TIME: {now_text}"
        )

        print(message)
        send_telegram(message)
        return

    best = candidates[0]

    # Calculate the entry time after scanning is complete.
    entry = entry_time()
    icon = "🟢" if best["direction"] == "CALL" else "🔴"

    message = (
        "🤖 ROMARIO TRADER PRO 8.0\n\n"
        f"{icon} {best['direction']}\n\n"
        f"💱 PAIR: {best['symbol']}\n"
        f"💰 PAYOUT: {best['payout']:.1f}%\n"
        f"📊 STRATEGY VOTES: {best['score']}/6\n"
        f"📈 HISTORICAL WIN RATE: {best['winrate']:.1f}%\n"
        f"🧪 HISTORICAL SIGNALS: {best['total']}\n"
        "⏱ TIMEFRAME: M1\n"
        "⌛ EXPIRATION: 60 SECONDS\n"
        f"🎯 ENTRY TIME: {entry} (Florida)\n\n"
        f"🕒 FLORIDA TIME: {now_text}\n\n"
        "⚠️ Historical results do not guarantee future wins."
    )

    print(message)
    send_telegram(message)


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        logging.exception("BOT FAILED")

        error_message = (
            "🤖 ROMARIO TRADER PRO 8.0\n\n"
            "⚠️ BOT ERROR\n"
            f"{str(exc)[:400]}\n"
            f"🕒 {florida_now().strftime('%Y-%m-%d %I:%M:%S %p')}"
        )

        send_telegram(error_message)
        raise
