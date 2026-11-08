
import os
import re
import time
import math
import requests
import pandas as pd
import numpy as np

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

# ==========================================
# ROMARIO TRADER PRO 4.6
# OTC SCANNER - MANUAL TELEGRAM SIGNALS
# ==========================================

VERSION = "ROMARIO TRADER PRO 4.6"

BASE_URL = "https://otcharts.com"

MIN_PAYOUT = 79
MIN_SCORE = 6
CANDLE_LIMIT = 100

M1 = 60
M5 = 300
EXPIRATION = 60

SIGNAL_LEAD = 30
VALIDATION_BEFORE_SIGNAL = 10

LOCAL_TZ = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "ROMARIO-TRADER-PRO/4.6"
})


# ==========================================
# BASIC FUNCTIONS
# ==========================================

def log(message):
    print(message, flush=True)


def utc_now():
    return datetime.now(timezone.utc)


def florida_time(timestamp):
    return datetime.fromtimestamp(
        timestamp, timezone.utc
    ).astimezone(LOCAL_TZ)


def format_time(timestamp):
    return florida_time(timestamp).strftime("%I:%M:%S %p")


def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("TELEGRAM SECRETS MISSING")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=20
        )

        log(f"TELEGRAM STATUS: {response.status_code}")

        if response.status_code == 200:
            log("TELEGRAM MESSAGE SENT")
            return True

        log(f"TELEGRAM ERROR: {response.text[:500]}")
        return False

    except requests.RequestException as error:
        log(f"TELEGRAM CONNECTION ERROR: {error}")
        return False


# ==========================================
# API REQUESTS WITH RETRIES
# ==========================================

def api_request(endpoint, params=None):
    url = BASE_URL + endpoint

    last_error = None

    for attempt in range(1, 4):
        try:
            response = session.get(
                url,
                params=params,
                timeout=30
            )

            log(
                f"API {endpoint}: HTTP {response.status_code} "
                f"(attempt {attempt}/3)"
            )

            if response.status_code in (429, 500, 502, 503, 504):
                last_error = RuntimeError(
                    f"HTTP {response.status_code}"
                )

                if attempt < 3:
                    time.sleep(attempt * 2)
                    continue

            response.raise_for_status()

            return response

        except requests.RequestException as error:
            last_error = error

            if attempt < 3:
                time.sleep(attempt * 2)

    raise RuntimeError(
        f"API request failed after 3 attempts: {last_error}"
    )


def extract_list(payload, keys):
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)

            if isinstance(value, list):
                return value

        for value in payload.values():
            if isinstance(value, (dict, list)):
                result = extract_list(value, keys)

                if result:
                    return result

    return []


def get_server_timestamp(response):
    """
    Use the HTTP Date header when available.
    Fall back to the GitHub runner's UTC clock.
    """

    date_header = response.headers.get("Date")

    if date_header:
        try:
            server_date = parsedate_to_datetime(date_header)

            if server_date.tzinfo is None:
                server_date = server_date.replace(
                    tzinfo=timezone.utc
                )

            return server_date.timestamp()

        except (TypeError, ValueError, OverflowError):
            pass

    return time.time()


# ==========================================
# SYMBOLS AND PAYOUT
# ==========================================

def get_otc_symbols():
    response = api_request(
        "/v1/symbols",
        {"venue": "otc"}
    )

    payload = response.json()

    items = extract_list(
        payload,
        ["data", "symbols", "instruments", "results"]
    )

    if not items:
        raise RuntimeError(
            "Could not find OTC symbols in API response."
        )

    result = []

    for item in items:
        if not isinstance(item, dict):
            continue

        symbol = str(
            item.get("symbol")
            or item.get("ticker")
            or item.get("name")
            or ""
        ).strip()

        if not re.fullmatch(r"[A-Z]{6}_otc", symbol):
            continue

        if item.get("open", True) is False:
            continue

        try:
            payout = float(item.get("payout"))
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        result.append({
            "symbol": symbol,
            "name": symbol.replace("_otc", ""),
            "payout": payout
        })

    return result


# ==========================================
# CANDLE TIMESTAMP CORRECTION
# ==========================================

def get_candles(symbol, timeframe):
    response = api_request(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": timeframe,
            "limit": CANDLE_LIMIT
        }
    )

    server_now = get_server_timestamp(response)

    payload = response.json()

    candles = extract_list(
        payload,
        ["data", "candles", "results", "bars"]
    )

    if not candles:
        raise RuntimeError(
            f"No candle data returned for {symbol}."
        )

    df = pd.DataFrame(candles)

    required = [
        "open", "high", "low", "close", "time"
    ]

    if not all(column in df.columns for column in required):
        raise RuntimeError(
            f"Missing candle fields: {list(df.columns)}"
        )

    for column in required:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna(subset=required).copy()

    if df.empty:
        raise RuntimeError("No valid numeric candles.")

    # Convert milliseconds to seconds if necessary.
    if df["time"].median() > 100000000000:
        df["time"] = df["time"] / 1000

    df["time"] = df["time"].astype("int64")

    df = (
        df.sort_values("time")
        .drop_duplicates(subset=["time"])
        .reset_index(drop=True)
    )

    # Estimate provider timestamp offset from the API's
    # HTTP Date header. Use 15-minute increments to support
    # time zones with non-whole-hour offsets.
    latest_raw_timestamp = int(df["time"].iloc[-1])

    difference = latest_raw_timestamp - server_now

    estimated_offset = round(
        difference / 900
    ) * 900

    # Refuse unusual offsets instead of guessing.
    if abs(estimated_offset) > 14 * 3600:
        raise RuntimeError(
            f"Unusual timestamp offset: {estimated_offset} seconds."
        )

    log(
        f"{symbol} TF={timeframe} "
        f"RAW TIME: {latest_raw_timestamp}"
    )

    log(
        f"{symbol} ESTIMATED TIME OFFSET: "
        f"{estimated_offset} seconds"
    )

    # Convert provider timestamps to UTC.
    df["time"] = df["time"] - estimated_offset

    latest_corrected = int(df["time"].iloc[-1])
    latest_age = server_now - latest_corrected

    log(
        f"{symbol} CORRECTED TIME: {latest_corrected} "
        f"| AGE: {latest_age:.1f}s"
    )

    # Reject data that is excessively old or in the future.
    max_age = max(timeframe * 5, 900)

    if latest_age < -10 or latest_age > max_age:
        raise RuntimeError(
            f"Stale or future candle data. Age={latest_age:.1f}s"
        )

    # Only use candles whose full timeframe has elapsed.
    df = df[
        (df["time"] + timeframe) <= server_now
    ].copy()

    df = df.reset_index(drop=True)

    if len(df) < 55:
        raise RuntimeError(
            f"Only {len(df)} closed candles available."
        )

    return df


# ==========================================
# INDICATORS
# ==========================================

def calculate_rsi(close, period=14):
    delta = close.diff()

    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = losses.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    rsi = 100 - (100 / (1 + rs))

    rsi = rsi.mask(
        (avg_loss == 0) & (avg_gain > 0),
        100
    )

    rsi = rsi.mask(
        (avg_gain == 0) & (avg_loss > 0),
        0
    )

    return rsi


def calculate_atr(df, period=14):
    previous_close = df["close"].shift(1)

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def calculate_adx(df, period=14):
    high_diff = df["high"].diff()
    low_diff = -df["low"].diff()

    plus_dm = pd.Series(
        np.where(
            (high_diff > low_diff) & (high_diff > 0),
            high_diff,
            0.0
        ),
        index=df.index
    )

    minus_dm = pd.Series(
        np.where(
            (low_diff > high_diff) & (low_diff > 0),
            low_diff,
            0.0
        ),
        index=df.index
    )

    previous_close = df["close"].shift(1)

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    plus_di = 100 * (
        plus_dm.ewm(alpha=1 / period, adjust=False).mean()
        / atr.replace(0, np.nan)
    )

    minus_di = 100 * (
        minus_dm.ewm(alpha=1 / period, adjust=False).mean()
        / atr.replace(0, np.nan)
    )

    denominator = (plus_di + minus_di).replace(
        0, np.nan
    )

    dx = (
        100 * (plus_di - minus_di).abs()
        / denominator
    )

    return dx.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def prepare_indicators(df):
    df = df.copy()

    df["ema9"] = df["close"].ewm(
        span=9, adjust=False
    ).mean()

    df["ema20"] = df["close"].ewm(
        span=20, adjust=False
    ).mean()

    df["ema50"] = df["close"].ewm(
        span=50, adjust=False
    ).mean()

    df["rsi"] = calculate_rsi(df["close"])
    df["atr"] = calculate_atr(df)
    df["adx"] = calculate_adx(df)

    return df.dropna().reset_index(drop=True)


# ==========================================
# SIGNAL ANALYSIS
# ==========================================

def analyze_pair(item):
    symbol = item["symbol"]

    m1_raw = get_candles(symbol, M1)
    m5_raw = get_candles(symbol, M5)

    m1 = prepare_indicators(m1_raw)
    m5 = prepare_indicators(m5_raw)

    if len(m1) < 10 or len(m5) < 10:
        raise RuntimeError("Not enough indicator history.")

    a = m1.iloc[-1]
    b = m5.iloc[-1]

    rsi = float(a["rsi"])
    atr = float(a["atr"])
    adx = float(b["adx"])
    close = float(a["close"])

    if not all(np.isfinite(x) for x in [rsi, atr, adx, close]):
        return None

    if atr <= 0 or close <= 0:
        return None

    candle_range = float(a["high"] - a["low"])
    body = abs(float(a["close"] - a["open"]))

    if candle_range <= 0:
        return None

    body_ratio = body / candle_range
    atr_percent = (atr / close) * 100

    call_score = 0
    put_score = 0

    # M1 EMA trend
    if a["ema9"] > a["ema20"] > a["ema50"]:
        call_score += 1

    if a["ema9"] < a["ema20"] < a["ema50"]:
        put_score += 1

    # M5 trend confirmation
    if b["ema20"] > b["ema50"]:
        call_score += 1

    if b["ema20"] < b["ema50"]:
        put_score += 1

    # M1 candle direction
    if a["close"] > a["open"]:
        call_score += 1

    if a["close"] < a["open"]:
        put_score += 1

    # Price relative to EMA9
    if a["close"] > a["ema9"]:
        call_score += 1

    if a["close"] < a["ema9"]:
        put_score += 1

    # RSI direction
    if 50 <= rsi <= 70:
        call_score += 1

    if 30 <= rsi <= 50:
        put_score += 1

    # Strong M5 trend bonus
    if adx >= 20:
        if call_score > put_score:
            call_score += 1
        elif put_score > call_score:
            put_score += 1

    # Strong candle body bonus
    if body_ratio >= 0.50:
        if a["close"] > a["open"]:
            call_score += 1
        elif a["close"] < a["open"]:
            put_score += 1

    # Hard filters
    if adx < 18:
        return None

    if not 25 <= rsi <= 75:
        return None

    if not 0.001 <= atr_percent <= 1.0:
        return None

    if body_ratio < 0.35:
        return None

    if call_score >= MIN_SCORE and call_score > put_score:
        direction = "CALL"
        emoji = "🟢"
        score = call_score

    elif put_score >= MIN_SCORE and put_score > call_score:
        direction = "PUT"
        emoji = "🔴"
        score = put_score

    else:
        return None

    return {
        "symbol": symbol,
        "name": item["name"],
        "direction": direction,
        "emoji": emoji,
        "score": score,
        "payout": item["payout"],
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "atr": round(atr, 5),
        "atr_percent": round(atr_percent, 5),
        "body_ratio": round(body_ratio, 3)
    }


# ==========================================
# ENTRY TIME
# ==========================================

def calculate_entry_time():
    now = time.time()

    # Ensure enough time for final validation before the alert.
    entry_timestamp = math.ceil(
        (now + 75) / 60
    ) * 60

    signal_timestamp = entry_timestamp - SIGNAL_LEAD

    return int(signal_timestamp), int(entry_timestamp)


def wait_until(timestamp):
    while True:
        remaining = timestamp - time.time()

        if remaining <= 0:
            return

        time.sleep(min(remaining, 1))


# ==========================================
# TELEGRAM MESSAGES
# ==========================================

def make_wait_message(reason, scanned):
    return f"""
🤖 {VERSION}

⚪ WAIT

No valid OTC signal.

PAIRS SCANNED: {scanned}
MINIMUM PAYOUT: {MIN_PAYOUT}%

REASON: {reason}

No trade recommended.
""".strip()


def make_signal_message(candidate, entry_timestamp):
    actual_signal_time = time.time()

    return f"""
🤖 {VERSION}

{candidate['emoji']} {candidate['direction']}

💱 PAIR: {candidate['name']} OTC
📊 SYMBOL: {candidate['symbol']}

⏱ TIMEFRAME: M1
⌛ EXPIRATION: {EXPIRATION} SECONDS

🕒 SIGNAL SENT: {format_time(actual_signal_time)}
🎯 ENTRY TIME: {format_time(entry_timestamp)}

💰 PAYOUT: {candidate['payout']:.0f}%
📈 WIN RATE: NOT ENOUGH DATA

📊 M1 / M5 CONFIRMATION
RSI: {candidate['rsi']}
M5 ADX: {candidate['adx']}
ATR: {candidate['atr']}

⭐ TECHNICAL SCORE: {candidate['score']}/7

⚠️ MANUAL ENTRY ONLY
⚠️ NO GUARANTEED RESULT
""".strip()


# ==========================================
# FINAL VALIDATION
# ==========================================

def final_validation(candidate):
    latest_symbols = get_otc_symbols()

    latest_item = next(
        (
            item for item in latest_symbols
            if item["symbol"] == candidate["symbol"]
        ),
        None
    )

    if not latest_item:
        log("PAIR NO LONGER MEETS PAYOUT REQUIREMENT.")
        return None

    fresh = analyze_pair(latest_item)

    if not fresh:
        log("FINAL VALIDATION: NO VALID SETUP.")
        return None

    if fresh["direction"] != candidate["direction"]:
        log("FINAL VALIDATION: DIRECTION CHANGED.")
        return None

    if fresh["score"] < MIN_SCORE:
        log("FINAL VALIDATION: SCORE TOO LOW.")
        return None

    return fresh


# ==========================================
# MAIN
# ==========================================

def main():
    log("=" * 45)
    log(VERSION)
    log("POCKET OPTION OTC SCANNER")
    log("=" * 45)

    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing.")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram secrets are missing.")

    log(f"UTC NOW: {utc_now().isoformat()}")
    log(
        "FLORIDA NOW: "
        + datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %I:%M:%S %p")
    )

    symbols = get_otc_symbols()

    log(f"PAIRS WITH PAYOUT >= {MIN_PAYOUT}%: {len(symbols)}")

    if not symbols:
        telegram_send(
            make_wait_message("No eligible OTC pairs found.", 0)
        )
        return

    candidates = []
    scanned = 0
    errors = 0

    for item in symbols:
        scanned += 1

        log(
            f"[{scanned}/{len(symbols)}] "
            f"Scanning {item['symbol']} "
            f"| PAYOUT {item['payout']}%"
        )

        try:
            result = analyze_pair(item)

            if result:
                candidates.append(result)

                log(
                    f"VALID: {result['direction']} "
                    f"{result['symbol']} "
                    f"SCORE {result['score']}/7"
                )
            else:
                log(f"NO VALID SETUP: {item['symbol']}")

        except Exception as error:
            errors += 1
            log(f"ERROR {item['symbol']}: {error}")

    log(f"PAIRS SCANNED: {scanned}")
    log(f"VALID CANDIDATES: {len(candidates)}")
    log(f"DATA ERRORS: {errors}")

    if not candidates:
        telegram_send(
            make_wait_message(
                f"No setup passed filters. Data errors: {errors}.",
                scanned
            )
        )
        return

    candidates.sort(
        key=lambda item: (
            item["score"],
            item["payout"],
            item["adx"]
        ),
        reverse=True
    )

    best = candidates[0]

    log(f"BEST CANDIDATE: {best}")

    signal_timestamp, entry_timestamp = calculate_entry_time()

    validation_timestamp = (
        signal_timestamp - VALIDATION_BEFORE_SIGNAL
    )

    log(
        f"VALIDATION TIME: {format_time(validation_timestamp)}"
    )
    log(f"SIGNAL TIME: {format_time(signal_timestamp)}")
    log(f"ENTRY TIME: {format_time(entry_timestamp)}")

    log("WAITING FOR FINAL VALIDATION...")
    wait_until(validation_timestamp)

    try:
        verified = final_validation(best)

    except Exception as error:
        log(f"FINAL VALIDATION ERROR: {error}")
        verified = None

    if not verified:
        telegram_send(
            make_wait_message(
                "Candidate failed final validation.",
                scanned
            )
        )
        return

    log("FINAL VALIDATION PASSED.")
    log("WAITING FOR SIGNAL TIME...")

    wait_until(signal_timestamp)

    # Do not send late alerts.
    if time.time() > signal_timestamp + 3:
        log("SIGNAL TIME MISSED. NO SIGNAL SENT.")
        telegram_send(
            make_wait_message(
                "Signal timing was missed.",
                scanned
            )
        )
        return

    # Confirm there is still enough time for manual entry.
    if time.time() >= entry_timestamp:
        log("ENTRY TIME PASSED. NO SIGNAL SENT.")
        return

    message = make_signal_message(
        verified,
        entry_timestamp
    )

    telegram_send(message)

    log("PROCESS COMPLETE.")


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        log(f"FATAL ERROR: {error}")

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            telegram_send(
                f"🤖 {VERSION}\n"
                f"🔴 BOT ERROR\n"
                f"{str(error)[:700]}"
            )

        raise
