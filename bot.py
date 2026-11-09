
import os
import re
import time
import requests
import pandas as pd

from datetime import timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
# ROMARIO TRADER PRO 5.1
# THREE STRATEGIES - OTC ONLY
# ==========================================

VERSION = "ROMARIO TRADER PRO 5.1"

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


# ==========================================
# TIME
# ==========================================

def florida_now():
    from datetime import datetime
    return datetime.now(FLORIDA)


# ==========================================
# TELEGRAM
# ==========================================

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Telegram credentials missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            print("Telegram rejected message:", result)
            return False

        return True

    except Exception as exc:
        print("Telegram error:", exc)
        return False


# ==========================================
# OTCHARTS API
# ==========================================

def api_get(path, params=None):

    url = BASE_URL + path

    response = session.get(
        url,
        params=params,
        timeout=25
    )

    if response.status_code != 200:
        print("API ERROR:", response.status_code)
        print("URL:", response.url)
        print("BODY:", response.text[:500])

    response.raise_for_status()

    result = response.json()

    if isinstance(result, dict):

        if result.get("success") is False:
            raise RuntimeError(str(result))

        if "data" in result:
            return result["data"]

        if "results" in result:
            return result["results"]

    return result


# ==========================================
# PAYOUT
# ==========================================

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


# ==========================================
# GET OTC FOREX PAIRS
# ==========================================

def get_symbols():

    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    if isinstance(data, dict):
        data = data.get(
            "symbols",
            data.get("items", [])
        )

    if not isinstance(data, list):
        raise RuntimeError(
            "Unexpected symbols response format."
        )

    pairs = []

    for item in data:

        if not isinstance(item, dict):
            continue

        # IMPORTANT:
        # Preserve original symbol case.
        # Example: EURUSD_otc

        raw_symbol = str(
            item.get(
                "symbol",
                item.get("name", "")
            )
        ).strip()

        if not re.fullmatch(
            r"[A-Z]{6}_otc",
            raw_symbol,
            re.IGNORECASE
        ):
            continue

        base = raw_symbol[:3].upper()
        quote = raw_symbol[3:6].upper()

        if base not in CURRENCIES:
            continue

        if quote not in CURRENCIES:
            continue

        if item.get("open") is False:
            continue

        payout = normalize_payout(
            item.get("payout")
        )

        if payout is None:
            continue

        if payout < MIN_PAYOUT:
            continue

        pairs.append({
            "symbol": raw_symbol,
            "payout": payout
        })

    return pairs


# ==========================================
# GET CANDLES
# ==========================================

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
        data = data.get(
            "candles",
            data.get("items", [])
        )

    if not isinstance(data, list):
        return None

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

    required = [
        "open",
        "high",
        "low",
        "close"
    ]

    if not all(
        column in df.columns
        for column in required
    ):
        print(
            symbol,
            "Missing OHLC columns:",
            list(df.columns)
        )
        return None

    for column in required:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna(
        subset=required
    ).reset_index(drop=True)

    if len(df) < 55:
        return None

    return df


# ==========================================
# INDICATORS
# ==========================================

def indicators(df):

    df = df.copy()

    df["ema20"] = (
        df["close"].ewm(
            span=20,
            adjust=False
        ).mean()
    )

    df["ema50"] = (
        df["close"].ewm(
            span=50,
            adjust=False
        ).mean()
    )

    delta = df["close"].diff()

    gain = delta.clip(
        lower=0
    ).rolling(14).mean()

    loss = (
        -delta.clip(upper=0)
    ).rolling(14).mean()

    rs = gain / loss.replace(
        0,
        float("nan")
    )

    df["rsi"] = 100 - (
        100 / (1 + rs)
    )

    df["range"] = (
        df["high"] - df["low"]
    )

    df["body"] = (
        df["close"] - df["open"]
    ).abs()

    return df.dropna().reset_index(drop=True)


def candle_direction(row):

    if row["close"] > row["open"]:
        return "CALL"

    if row["close"] < row["open"]:
        return "PUT"

    return None


# ==========================================
# STRATEGY 1: TREND PULLBACK
# ==========================================

def strategy_trend_pullback(m1, m5):

    current = m1.iloc[-1]
    previous = m1.iloc[-2]
    trend = m5.iloc[-1]

    distance = (
        abs(current["close"] - current["ema20"])
        / current["close"]
    )

    if distance > 0.0015:
        return None

    if trend["close"] > trend["ema50"]:

        if (
            candle_direction(current) == "CALL"
            and previous["low"] <= previous["ema20"] * 1.001
            and current["close"] > current["ema20"]
        ):

            return {
                "strategy": "TREND PULLBACK",
                "direction": "CALL",
                "score": 3
            }

    if trend["close"] < trend["ema50"]:

        if (
            candle_direction(current) == "PUT"
            and previous["high"] >= previous["ema20"] * 0.999
            and current["close"] < current["ema20"]
        ):

            return {
                "strategy": "TREND PULLBACK",
                "direction": "PUT",
                "score": 3
            }

    return None


# ==========================================
# STRATEGY 2: BREAKOUT + RETEST
# ==========================================

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


# ==========================================
# STRATEGY 3: SUPPORT / RESISTANCE
# ==========================================

def strategy_reversal(m1, m5):

    current = m1.iloc[-1]

    history = m1.iloc[-22:-2]

    support = history["low"].min()
    resistance = history["high"].max()

    if current["range"] <= 0:
        return None

    lower_wick = (
        min(
            current["open"],
            current["close"]
        ) - current["low"]
    )

    upper_wick = (
        current["high"] - max(
            current["open"],
            current["close"]
        )
    )

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


# ==========================================
# ANALYZE ONE PAIR
# ==========================================

def analyze(pair):

    symbol = pair["symbol"]

    try:

        m1 = get_candles(symbol, 60)
        m5 = get_candles(symbol, 300)

        if m1 is None or m5 is None:
            return []

        # Assumes final returned candle is incomplete.
        # Verify this assumption against API timestamps.

        m1 = indicators(
            m1.iloc[:-1].reset_index(drop=True)
        )

        m5 = indicators(
            m5.iloc[:-1].reset_index(drop=True)
        )

        if len(m1) < 25 or len(m5) < 25:
            return []

        current = m1.iloc[-1]

        if current["range"] <= 0:
            return []

        if (
            current["range"] / current["close"]
            > 0.002
        ):
            return []

        body_ratio = (
            current["body"] / current["range"]
        )

        if body_ratio < 0.30:
            return []

        results = [
            strategy_trend_pullback(m1, m5),
            strategy_breakout_retest(m1, m5),
            strategy_reversal(m1, m5)
        ]

        valid = [
            result
            for result in results
            if result is not None
        ]

        if not valid:
            return []

        directions = {
            result["direction"]
            for result in valid
        }

        # Reject conflicting directions.
        if len(directions) != 1:
            return []

        direction = valid[0]["direction"]

        # M5 trend confirmation.
        trend = m5.iloc[-1]

        if (
            direction == "CALL"
            and trend["close"] <= trend["ema50"]
        ):
            return []

        if (
            direction == "PUT"
            and trend["close"] >= trend["ema50"]
        ):
            return []

        return [{
            "symbol": symbol,
            "payout": pair["payout"],
            "direction": direction,
            "strategies": [
                result["strategy"]
                for result in valid
            ],
            "score": sum(
                result["score"]
                for result in valid
            )
        }]

    except Exception as exc:

        print(
            f"{symbol} analysis error: {exc}"
        )

        return []


# ==========================================
# SIGNAL MESSAGE
# ==========================================

def build_message(signal, entry):

    direction = signal["direction"]

    if direction == "CALL":
        emoji = "🟢"
        label = "BUY / CALL"
    else:
        emoji = "🔴"
        label = "SELL / PUT"

    signal_time = (
        entry - timedelta(seconds=30)
    )

    expiry = (
        entry + timedelta(
            seconds=EXPIRATION_SECONDS
        )
    )

    strategies = " + ".join(
        signal["strategies"]
    )

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"{emoji} <b>{label}</b>\n\n"
        f"💱 PAIR: {signal['symbol']}\n"
        f"💰 PAYOUT: {signal['payout']:.1f}%\n"
        f"⏱ TIMEFRAME: M1\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"🧠 STRATEGY: {strategies}\n"
        f"📊 SETUP SCORE: {signal['score']}/9\n\n"
        f"🕒 SIGNAL TIME: "
        f"{signal_time.strftime('%I:%M:%S %p')}\n"
        f"🎯 ENTRY TIME: "
        f"{entry.strftime('%I:%M:%S %p')}\n"
        f"🏁 EXPIRY: "
        f"{expiry.strftime('%I:%M:%S %p')}\n"
        f"📍 TIMEZONE: Florida\n\n"
        f"⚠️ Technical signal only.\n"
        f"🧪 DEMO TEST ONLY."
    )


# ==========================================
# MAIN
# ==========================================

def main():

    print("=" * 45)
    print(VERSION)
    print("Starting OTC Forex scanner")
    print("=" * 45)

    print(
        "Florida time:",
        florida_now().strftime(
            "%Y-%m-%d %I:%M:%S %p"
        )
    )

    if not API_KEY:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing."
        )

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Telegram secrets are missing."
        )

    telegram(
        "🟡 <b>ROMARIO TRADER PRO 5.1</b>\n"
        "Analyzing OTC Forex markets..."
    )

    pairs = get_symbols()

    print(
        "Pairs with payout >= 75%:",
        len(pairs)
    )

    if not pairs:

        print(
            "No pairs passed payout filter."
        )

        return

    candidates = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(analyze, pair): pair
            for pair in pairs
        }

        for future in as_completed(futures):

            try:
                candidates.extend(
                    future.result()
                )

            except Exception as exc:
                print("Worker error:", exc)

    print(
        "Qualified setups:",
        len(candidates)
    )

    if not candidates:

        print(
            "NO QUALIFIED SETUP. "
            "NO SIGNAL SENT."
        )

        return

    candidates.sort(
        key=lambda item: (
            item["score"],
            item["payout"]
        ),
        reverse=True
    )

    best = candidates[0]

    # Schedule signal 30 seconds before next minute.
    now = florida_now()

    entry = (
        now.replace(
            second=0,
            microsecond=0
        ) + timedelta(minutes=1)
    )

    signal_time = (
        entry - timedelta(seconds=30)
    )

    wait_seconds = (
        signal_time - now
    ).total_seconds()

    if wait_seconds > 0:

        print(
            f"Waiting {wait_seconds:.1f} seconds."
        )

        time.sleep(wait_seconds)

    current = florida_now()

    if current >= entry - timedelta(seconds=2):

        print(
            "Signal is too late. Skipping."
        )

        return

    if current < entry - timedelta(seconds=32):

        print(
            "Signal timing not reached. Skipping."
        )

        return

    message = build_message(
        best,
        entry
    )

    sent = telegram(message)

    print(
        "Telegram signal sent:",
        sent
    )

    print(
        "Selected pair:",
        best["symbol"]
    )

    print(
        "Selected strategies:",
        best["strategies"]
    )


if __name__ == "__main__":
    main()
