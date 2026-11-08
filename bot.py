
import os
import re
import time
import requests
import pandas as pd
import numpy as np

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ==========================================
# ROMARIO TRADER PRO 4.4
# POCKET OPTION OTC - MANUAL ALERTS ONLY
# ==========================================

VERSION = "ROMARIO TRADER PRO 4.4"

BASE_URL = "https://otcharts.com"
MIN_PAYOUT = 79
CANDLE_LIMIT = 100
MIN_SCORE = 6

TIMEFRAME_M1 = 60
TIMEFRAME_M5 = 300
EXPIRATION_SECONDS = 60

# Signal notification is sent 30 seconds before entry.
SIGNAL_LEAD_SECONDS = 30

LOCAL_TZ = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "ROMARIO-TRADER-PRO/4.4"
})


# ==========================================
# GENERAL FUNCTIONS
# ==========================================

def log(message):
    print(message, flush=True)


def now_utc():
    return datetime.now(timezone.utc)


def local_time(timestamp):
    return datetime.fromtimestamp(
        timestamp, timezone.utc
    ).astimezone(LOCAL_TZ)


def format_time(timestamp):
    return local_time(timestamp).strftime("%I:%M:%S %p")


def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("TELEGRAM ERROR: Missing Telegram secrets.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=20
        )

        log(f"TELEGRAM HTTP STATUS: {response.status_code}")

        if response.status_code == 200:
            log("TELEGRAM MESSAGE SENT")
            return True

        log(f"TELEGRAM ERROR: {response.text[:500]}")
        return False

    except requests.RequestException as error:
        log(f"TELEGRAM CONNECTION ERROR: {error}")
        return False


def api_get(endpoint, params=None):
    url = BASE_URL + endpoint

    response = session.get(
        url,
        params=params,
        timeout=25
    )

    log(f"API {endpoint} STATUS: {response.status_code}")
    response.raise_for_status()

    return response.json()


def extract_list(payload, possible_keys):
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in possible_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value

        for value in payload.values():
            if isinstance(value, dict):
                result = extract_list(value, possible_keys)
                if result:
                    return result

    return []


# ==========================================
# SYMBOLS AND PAYOUT
# ==========================================

def get_otc_symbols():
    payload = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    symbols = extract_list(
        payload,
        ["data", "symbols", "instruments", "results"]
    )

    if not symbols:
        raise RuntimeError(
            "Could not find symbols list in OTCharts response."
        )

    forex_symbols = []

    for item in symbols:
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

        is_open = item.get("open", True)

        if is_open is False:
            continue

        try:
            payout = float(item.get("payout"))
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        forex_symbols.append({
            "symbol": symbol,
            "name": symbol.replace("_otc", ""),
            "payout": payout
        })

    return forex_symbols


# ==========================================
# CANDLE DATA
# ==========================================

def get_candles(symbol, timeframe):
    payload = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": timeframe,
            "limit": CANDLE_LIMIT
        }
    )

    candles = extract_list(
        payload,
        ["data", "candles", "results", "bars"]
    )

    if not candles:
        raise RuntimeError(
            f"No candles returned for {symbol}, TF={timeframe}"
        )

    df = pd.DataFrame(candles)

    required = ["open", "high", "low", "close", "time"]

    if not all(column in df.columns for column in required):
        raise RuntimeError(
            f"Missing candle fields for {symbol}: {list(df.columns)}"
        )

    for column in ["open", "high", "low", "close", "time"]:
        df[column] = pd.to_numeric(
            df[column], errors="coerce"
        )

    df = df.dropna(
        subset=["open", "high", "low", "close", "time"]
    )

    df["time"] = df["time"].astype("int64")

    df = df.sort_values("time")
    df = df.drop_duplicates(subset=["time"])
    df = df.reset_index(drop=True)

    # A candle is closed only after its full duration has passed.
    current_timestamp = int(time.time())

    df = df[
        (df["time"] + timeframe) <= current_timestamp
    ].copy()

    df = df.reset_index(drop=True)

    if len(df) < 55:
        raise RuntimeError(
            f"Not enough CLOSED candles for {symbol}: {len(df)}"
        )

    return df


# ==========================================
# INDICATORS
# ==========================================

def calculate_rsi(series, period=14):
    delta = series.diff()

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

    return 100 - (100 / (1 + rs))


def calculate_atr(df, period=14):
    previous_close = df["close"].shift(1)

    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    )

    true_range = ranges.max(axis=1)

    return true_range.ewm(
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

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    atr = true_range.ewm(
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

    denominator = (plus_di + minus_di).replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / denominator

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

def analyze_pair(symbol_info):
    symbol = symbol_info["symbol"]

    m1_raw = get_candles(symbol, TIMEFRAME_M1)
    m5_raw = get_candles(symbol, TIMEFRAME_M5)

    m1 = prepare_indicators(m1_raw)
    m5 = prepare_indicators(m5_raw)

    if len(m1) < 10 or len(m5) < 10:
        return None

    a = m1.iloc[-1]
    b = m5.iloc[-1]

    atr = float(a["atr"])
    adx_m5 = float(b["adx"])
    rsi = float(a["rsi"])

    if not np.isfinite(atr) or atr <= 0:
        return None

    if not np.isfinite(adx_m5) or adx_m5 < 18:
        return None

    body = abs(float(a["close"]) - float(a["open"]))
    candle_range = float(a["high"]) - float(a["low"])

    if candle_range <= 0:
        return None

    body_ratio = body / candle_range

    if body_ratio < 0.35:
        return None

    if not 25 <= rsi <= 75:
        return None

    # ATR must be valid. It is a volatility check, not a win probability.
    atr_percent = (atr / float(a["close"])) * 100

    if not 0.001 <= atr_percent <= 1.0:
        return None

    call_score = 0
    put_score = 0

    # M1 trend
    if a["ema9"] > a["ema20"] > a["ema50"]:
        call_score += 1

    if a["ema9"] < a["ema20"] < a["ema50"]:
        put_score += 1

    # M5 trend confirmation
    if b["ema20"] > b["ema50"]:
        call_score += 1

    if b["ema20"] < b["ema50"]:
        put_score += 1

    # Last closed M1 candle
    if a["close"] > a["open"]:
        call_score += 1

    if a["close"] < a["open"]:
        put_score += 1

    # Price position
    if a["close"] > a["ema9"]:
        call_score += 1

    if a["close"] < a["ema9"]:
        put_score += 1

    # RSI direction
    if 50 <= rsi <= 70:
        call_score += 1

    if 30 <= rsi <= 50:
        put_score += 1

    # ADX trend strength
    if adx_m5 >= 20:
        if call_score > put_score:
            call_score += 1
        elif put_score > call_score:
            put_score += 1

    # Candle body confirmation
    if body_ratio >= 0.50:
        if a["close"] > a["open"]:
            call_score += 1
        elif a["close"] < a["open"]:
            put_score += 1

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
        "name": symbol_info["name"],
        "direction": direction,
        "emoji": emoji,
        "score": score,
        "payout": symbol_info["payout"],
        "rsi": round(rsi, 2),
        "adx_m5": round(adx_m5, 2),
        "atr": round(atr, 5),
        "atr_percent": round(atr_percent, 5),
        "m1_timestamp": int(m1_raw.iloc[-1]["time"]),
        "m5_timestamp": int(m5_raw.iloc[-1]["time"])
    }


# ==========================================
# ENTRY TIME
# ==========================================

def calculate_entry_time():
    now = time.time()

    # Select a future minute boundary with enough time
    # to send the alert exactly 30 seconds beforehand.
    entry_timestamp = (
        int((now + SIGNAL_LEAD_SECONDS + 1) // 60) + 1
    ) * 60

    signal_timestamp = entry_timestamp - SIGNAL_LEAD_SECONDS

    if signal_timestamp <= now:
        entry_timestamp += 60
        signal_timestamp += 60

    return int(signal_timestamp), int(entry_timestamp)


def wait_until(timestamp):
    while True:
        remaining = timestamp - time.time()

        if remaining <= 0:
            return

        time.sleep(min(remaining, 1))


# ==========================================
# FINAL VALIDATION AND TELEGRAM
# ==========================================

def final_validation(candidate):
    symbol_info = {
        "symbol": candidate["symbol"],
        "name": candidate["name"],
        "payout": candidate["payout"]
    }

    fresh = analyze_pair(symbol_info)

    if not fresh:
        return None

    if fresh["direction"] != candidate["direction"]:
        return None

    if fresh["payout"] < MIN_PAYOUT:
        return None

    return fresh


def make_signal_message(candidate, entry_timestamp, signal_timestamp):
    return f"""
🤖 {VERSION}

{candidate['emoji']} {candidate['direction']}

💱 PAIR: {candidate['name']} OTC
📊 SYMBOL: {candidate['symbol']}

⏱ TIMEFRAME: M1
⌛ EXPIRATION: 60 SECONDS

🕒 SIGNAL SENT: {format_time(signal_timestamp)}
🎯 ENTRY TIME: {format_time(entry_timestamp)}

💰 PAYOUT: {candidate['payout']:.0f}%
📈 WIN RATE: NOT ENOUGH DATA

📊 M1/M5 CONFIRMATION
TREND: {candidate['direction']}
RSI: {candidate['rsi']}
M5 ADX: {candidate['adx_m5']}
ATR: {candidate['atr']}

⭐ TECHNICAL SCORE: {candidate['score']}/8

⚠️ Manual entry only.
⚠️ No guaranteed result.
""".strip()


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

    log(f"UTC NOW: {now_utc().isoformat()}")
    log(f"FLORIDA NOW: {datetime.now(LOCAL_TZ).isoformat()}")

    symbols = get_otc_symbols()

    log(f"PAIRS WITH PAYOUT >= {MIN_PAYOUT}%: {len(symbols)}")

    if not symbols:
        telegram_send(
            make_wait_message("No eligible OTC Forex pairs.", 0)
        )
        return

    candidates = []
    scanned = 0

    for item in symbols:
        scanned += 1

        log(
            f"Scanning {item['symbol']} "
            f"| Payout: {item['payout']}%"
        )

        try:
            result = analyze_pair(item)

            if result:
                candidates.append(result)

                log(
                    f"VALID: {result['direction']} "
                    f"{result['symbol']} "
                    f"SCORE {result['score']}"
                )
            else:
                log(f"NO VALID SETUP: {item['symbol']}")

        except Exception as error:
            log(f"SKIPPED {item['symbol']}: {error}")

    log(f"SCANNED: {scanned}")
    log(f"VALID CANDIDATES: {len(candidates)}")

    if not candidates:
        telegram_send(
            make_wait_message("No setup passed all filters.", scanned)
        )
        return

    candidates.sort(
        key=lambda x: (x["score"], x["payout"], x["adx_m5"]),
        reverse=True
    )

    best = candidates[0]

    log(f"BEST CANDIDATE: {best}")

    signal_timestamp, entry_timestamp = calculate_entry_time()

    log(f"SIGNAL TIME: {format_time(signal_timestamp)}")
    log(f"ENTRY TIME: {format_time(entry_timestamp)}")

    log("WAITING FOR SIGNAL TIME...")
    wait_until(signal_timestamp)

    # Do not send a late signal.
    if time.time() > signal_timestamp + 3:
        log("MISSED SIGNAL TIME. NO SIGNAL SENT.")
        telegram_send(
            make_wait_message("Signal timing was missed.", scanned)
        )
        return

    log("REFRESHING DATA BEFORE SIGNAL...")

    try:
        verified = final_validation(best)
    except Exception as error:
        log(f"FINAL VALIDATION ERROR: {error}")
        verified = None

    if not verified:
        log("FINAL VALIDATION FAILED.")
        telegram_send(
            make_wait_message(
                "Setup changed or failed final validation.",
                scanned
            )
        )
        return

    # Make sure there is still enough time to enter.
    if time.time() >= entry_timestamp:
        log("ENTRY TIME PASSED. NO SIGNAL SENT.")
        return

    message = make_signal_message(
        verified,
        entry_timestamp,
        signal_timestamp
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
                f"🤖 {VERSION}\n🔴 BOT ERROR\n{str(error)[:800]}"
            )

        raise
