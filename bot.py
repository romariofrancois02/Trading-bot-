
import os
import re
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
# ROMARIO TRADER PRO 5.0
# THREE STRATEGIES - ALERT ONLY
# ==========================================

BASE_URL = "https://otcharts.com"
API_KEY = os.getenv("OTCHARTS_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_PAYOUT = 75
EXPIRATION_SECONDS = 60
MAX_WORKERS = 5
FLORIDA = ZoneInfo("America/New_York")

CURRENCIES = set("""
USD EUR GBP JPY CHF CAD AUD NZD CNY HKD SGD MXN ZAR TRY
NOK SEK DKK PLN HUF CZK ILS THB IDR MYR PHP INR KRW TWD
AED SAR QAR KWD BHD OMR CLP COP ARS PEN EGP TND NGN PKR
BDT VND UAH RON BGN ISK HRK RUB KES GHS MAD JOD LKR NPR
""".split())

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
})


def florida_now():
    return datetime.now(FLORIDA)


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=15
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print("Telegram error:", exc)
        return False


def api_get(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict):
        if data.get("success") is False:
            raise RuntimeError(str(data))
        return data.get("data", data.get("results", data))

    return data


def normalize_payout(value):
    try:
        value = float(value)
        if 0 < value <= 1:
            value *= 100
        if 0 < value <= 100:
            return value
    except (TypeError, ValueError):
        pass
    return None


def get_symbols():
    data = api_get("/v1/symbols", {"venue": "otc"})

    if isinstance(data, dict):
        data = data.get("symbols", data.get("items", []))

    symbols = []

    for item in data:
        if not isinstance(item, dict):
            continue

        symbol = str(
            item.get("symbol", item.get("name", ""))
        ).upper()

        if not re.fullmatch(r"[A-Z]{6}_OTC", symbol):
            continue

        base, quote = symbol[:3], symbol[3:6]

        if base not in CURRENCIES or quote not in CURRENCIES:
            continue

        if item.get("open") is False:
            continue

        # Payout must be supplied by the API.
        payout = normalize_payout(item.get("payout"))

        if payout is None or payout < MIN_PAYOUT:
            continue

        symbols.append({
            "symbol": symbol,
            "payout": payout
        })

    return symbols


def get_candles(symbol, timeframe):
    data = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": timeframe,
            "limit": 120
        }
    )

    if isinstance(data, dict):
        data = data.get("candles", data.get("items", []))

    df = pd.DataFrame(data)

    if df.empty:
        return None

    aliases = {
        "open": ["open", "o"],
        "high": ["high", "h"],
        "low": ["low", "l"],
        "close": ["close", "c"]
    }

    rename = {}

    for standard, choices in aliases.items():
        for choice in choices:
            if choice in df.columns:
                rename[choice] = standard
                break

    df = df.rename(columns=rename)

    required = ["open", "high", "low", "close"]

    if not all(column in df.columns for column in required):
        return None

    for column in required:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=required).reset_index(drop=True)

    if len(df) < 55:
        return None

    return df


def indicators(df):
    df = df.copy()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()

    rs = gain / loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    df["range"] = df["high"] - df["low"]
    df["body"] = (df["close"] - df["open"]).abs()

    return df.dropna().reset_index(drop=True)


def candle_direction(row):
    if row["close"] > row["open"]:
        return "CALL"
    if row["close"] < row["open"]:
        return "PUT"
    return None


def strategy_trend_pullback(m1, m5):
    a = m1.iloc[-1]
    b = m1.iloc[-2]
    trend = m5.iloc[-1]

    distance = abs(a["close"] - a["ema20"]) / a["close"]

    if distance > 0.0015:
        return None

    direction = None

    if trend["close"] > trend["ema50"]:
        if (
            candle_direction(a) == "CALL"
            and b["low"] <= b["ema20"] * 1.001
            and a["close"] > a["ema20"]
        ):
            direction = "CALL"

    elif trend["close"] < trend["ema50"]:
        if (
            candle_direction(a) == "PUT"
            and b["high"] >= b["ema20"] * 0.999
            and a["close"] < a["ema20"]
        ):
            direction = "PUT"

    if direction:
        return {
            "strategy": "TREND PULLBACK",
            "direction": direction,
            "score": 3
        }

    return None


def strategy_breakout_retest(m1, m5):
    if len(m1) < 25:
        return None

    previous = m1.iloc[-2]
    current = m1.iloc[-1]
    history = m1.iloc[-22:-2]

    resistance = history["high"].max()
    support = history["low"].min()

    if (
        previous["close"] > resistance
        and current["low"] <= resistance * 1.001
        and current["close"] > resistance
        and candle_direction(current) == "CALL"
    ):
        return {
            "strategy": "BREAKOUT + RETEST",
            "direction": "CALL",
            "score": 3
        }

    if (
        previous["close"] < support
        and current["high"] >= support * 0.999
        and current["close"] < support
        and candle_direction(current) == "PUT"
    ):
        return {
            "strategy": "BREAKOUT + RETEST",
            "direction": "PUT",
            "score": 3
        }

    return None


def strategy_reversal(m1, m5):
    current = m1.iloc[-1]
    previous = m1.iloc[-2]
    history = m1.iloc[-22:-2]

    support = history["low"].min()
    resistance = history["high"].max()

    if current["range"] <= 0:
        return None

    lower_wick = min(current["open"], current["close"]) - current["low"]
    upper_wick = current["high"] - max(current["open"], current["close"])

    if (
        current["low"] <= support * 1.001
        and current["close"] > current["open"]
        and lower_wick / current["range"] >= 0.40
        and current["rsi"] < 40
    ):
        return {
            "strategy": "SUPPORT REVERSAL",
            "direction": "CALL",
            "score": 3
        }

    if (
        current["high"] >= resistance * 0.999
        and current["close"] < current["open"]
        and upper_wick / current["range"] >= 0.40
        and current["rsi"] > 60
    ):
        return {
            "strategy": "RESISTANCE REVERSAL",
            "direction": "PUT",
            "score": 3
        }

    return None


def analyze(pair):
    symbol = pair["symbol"]

    try:
        m1 = get_candles(symbol, 60)
        m5 = get_candles(symbol, 300)

        if m1 is None or m5 is None:
            return []

        # Exclude the most recent candle, assumed incomplete.
        m1 = indicators(m1.iloc[:-1].reset_index(drop=True))
        m5 = indicators(m5.iloc[:-1].reset_index(drop=True))

        if len(m1) < 25 or len(m5) < 25:
            return []

        last = m1.iloc[-1]

        if last["range"] <= 0:
            return []

        if last["range"] / last["close"] > 0.002:
            return []

        body_ratio = last["body"] / last["range"]

        if body_ratio < 0.30:
            return []

        strategies = [
            strategy_trend_pullback(m1, m5),
            strategy_breakout_retest(m1, m5),
            strategy_reversal(m1, m5)
        ]

        valid = [s for s in strategies if s]

        if not valid:
            return []

        # Require agreement when more than one strategy triggers.
        directions = {s["direction"] for s in valid}

        if len(directions) > 1:
            return []

        direction = valid[0]["direction"]

        # M5 trend confirmation.
        trend = m5.iloc[-1]

        if direction == "CALL" and trend["close"] <= trend["ema50"]:
            return []

        if direction == "PUT" and trend["close"] >= trend["ema50"]:
            return []

        return [{
            "symbol": symbol,
            "payout": pair["payout"],
            "direction": direction,
            "strategies": [s["strategy"] for s in valid],
            "score": sum(s["score"] for s in valid),
            "analysis_time": florida_now()
        }]

    except Exception as exc:
        print(f"{symbol} analysis error: {exc}")
        return []


def build_message(signal):
    direction = signal["direction"]

    if direction == "CALL":
        emoji = "🟢"
        label = "BUY / CALL"
    else:
        emoji = "🔴"
        label = "SELL / PUT"

    now = florida_now()
    entry = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    signal_time = entry - timedelta(seconds=30)
    expiry = entry + timedelta(seconds=EXPIRATION_SECONDS)

    strategy_text = " + ".join(signal["strategies"])

    return (
        "🤖 <b>ROMARIO TRADER PRO 5.0</b>\n\n"
        f"{emoji} <b>{label}</b>\n"
        f"💱 PAIR: {signal['symbol']}\n"
        f"💰 PAYOUT: {signal['payout']:.1f}%\n"
        "⏱ TIMEFRAME: M1\n"
        "⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🧠 STRATEGY: {strategy_text}\n"
        f"📊 SETUP SCORE: {signal['score']}/9\n"
        f"🕒 SIGNAL TIME: {signal_time.strftime('%I:%M:%S %p')}\n"
        f"🎯 ENTRY TIME: {entry.strftime('%I:%M:%S %p')}\n"
        f"🏁 EXPIRY: {expiry.strftime('%I:%M:%S %p')}\n"
        "📍 TIMEZONE: Florida\n\n"
        "⚠️ Technical signal only. No guaranteed win rate.\n"
        "🧪 TEST ON DEMO FIRST."
    )


def main():
    print("ROMARIO TRADER PRO 5.0 STARTING")
    print("Florida time:", florida_now().strftime("%Y-%m-%d %I:%M:%S %p"))

    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing.")

    telegram("🟡 <b>ROMARIO TRADER PRO 5.0</b>\nAnalyzing OTC Forex markets...")

    pairs = get_symbols()

    print("Pairs with verified payout >= 75%:", len(pairs))

    if not pairs:
        print("No pairs passed the payout filter.")
        return

    candidates = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(analyze, pair): pair
            for pair in pairs
        }

        for future in as_completed(futures):
            try:
                candidates.extend(future.result())
            except Exception as exc:
                print("Worker error:", exc)

    print("Qualified setups:", len(candidates))

    if not candidates:
        print("NO QUALIFIED SETUP. NO SIGNAL SENT.")
        return

    candidates.sort(
        key=lambda x: (x["score"], x["payout"]),
        reverse=True
    )

    best = candidates[0]

    # Avoid sending a stale signal.
    now = florida_now()
    next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    send_at = next_minute - timedelta(seconds=30)

    wait_seconds = (send_at - now).total_seconds()

    if wait_seconds > 0:
        print(f"Waiting {wait_seconds:.1f} seconds for signal time.")
        time.sleep(wait_seconds)

    current = florida_now()

    if current >= next_minute - timedelta(seconds=2):
        print("Signal too late. Skipping.")
        return

    if current < next_minute - timedelta(seconds=32):
        print("Signal timing not reached. Skipping.")
        return

    message = build_message(best)
    sent = telegram(message)

    print("Telegram signal sent:", sent)


if __name__ == "__main__":
    main()
