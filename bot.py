
import os
import re
import time
import requests
import pandas as pd
import numpy as np

VERSION = "ROMARIO TRADER PRO 4.5 DIAGNOSTIC"
BASE_URL = "https://otcharts.com"

MIN_PAYOUT = 79
MIN_SCORE = 6
CANDLE_LIMIT = 100

LOCAL_TZ = "America/New_York"

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "ROMARIO-TRADER-PRO/4.5"
})


def log(message):
    print(message, flush=True)


def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram secrets missing.")
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
        log(response.text[:500])

        return response.status_code == 200

    except requests.RequestException as error:
        log(f"TELEGRAM ERROR: {error}")
        return False


def api_get(endpoint, params=None):
    response = session.get(
        BASE_URL + endpoint,
        params=params,
        timeout=25
    )

    log(f"API {endpoint}: HTTP {response.status_code}")
    response.raise_for_status()

    return response.json()


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


def get_symbols():
    payload = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

    items = extract_list(
        payload,
        ["data", "symbols", "instruments", "results"]
    )

    if not items:
        raise RuntimeError("OTCharts returned no symbols list.")

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

        if payout >= MIN_PAYOUT:
            result.append({
                "symbol": symbol,
                "name": symbol.replace("_otc", ""),
                "payout": payout
            })

    return result


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
        raise RuntimeError("No candle data.")

    df = pd.DataFrame(candles)

    required = ["open", "high", "low", "close", "time"]

    if not all(column in df.columns for column in required):
        raise RuntimeError(
            f"Missing fields: {list(df.columns)}"
        )

    for column in required:
        df[column] = pd.to_numeric(
            df[column], errors="coerce"
        )

    df = df.dropna(subset=required)
    df["time"] = df["time"].astype("int64")

    df = (
        df.sort_values("time")
        .drop_duplicates("time")
        .reset_index(drop=True)
    )

    # Keep only candles whose full duration has passed.
    now = int(time.time())

    df = df[
        (df["time"] + timeframe) <= now
    ].reset_index(drop=True)

    if len(df) < 55:
        raise RuntimeError(
            f"Only {len(df)} closed candles available."
        )

    return df


def calculate_rsi(close, period=14):
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - (100 / (1 + rs))


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
            0
        ),
        index=df.index
    )

    minus_dm = pd.Series(
        np.where(
            (low_diff > high_diff) & (low_diff > 0),
            low_diff,
            0
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

    denominator = (plus_di + minus_di).replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / denominator

    return dx.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def indicators(df):
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


def analyze(item):
    symbol = item["symbol"]

    m1_raw = get_candles(symbol, 60)
    m5_raw = get_candles(symbol, 300)

    m1 = indicators(m1_raw)
    m5 = indicators(m5_raw)

    a = m1.iloc[-1]
    b = m5.iloc[-1]

    close = float(a["close"])
    atr = float(a["atr"])
    rsi = float(a["rsi"])
    adx = float(b["adx"])

    candle_range = float(a["high"] - a["low"])
    body = abs(float(a["close"] - a["open"]))

    body_ratio = (
        body / candle_range if candle_range > 0 else 0
    )

    atr_percent = (
        atr / close * 100 if close > 0 else 0
    )

    # Calculate directional scores even if a filter fails.
    call = 0
    put = 0

    if a["ema9"] > a["ema20"] > a["ema50"]:
        call += 1

    if a["ema9"] < a["ema20"] < a["ema50"]:
        put += 1

    if b["ema20"] > b["ema50"]:
        call += 1

    if b["ema20"] < b["ema50"]:
        put += 1

    if a["close"] > a["open"]:
        call += 1

    if a["close"] < a["open"]:
        put += 1

    if a["close"] > a["ema9"]:
        call += 1

    if a["close"] < a["ema9"]:
        put += 1

    if 50 <= rsi <= 70:
        call += 1

    if 30 <= rsi <= 50:
        put += 1

    if adx >= 20:
        if call > put:
            call += 1
        elif put > call:
            put += 1

    if body_ratio >= 0.50:
        if a["close"] > a["open"]:
            call += 1
        elif a["close"] < a["open"]:
            put += 1

    score = max(call, put)

    if call > put:
        direction = "CALL"
    elif put > call:
        direction = "PUT"
    else:
        direction = "TIE"

    # Count every failed filter independently.
    failures = []

    if adx < 18:
        failures.append("ADX")

    if not 25 <= rsi <= 75:
        failures.append("RSI")

    if not 0.001 <= atr_percent <= 1.0:
        failures.append("ATR")

    if body_ratio < 0.35:
        failures.append("BODY")

    if score < MIN_SCORE:
        failures.append("SCORE")

    if direction == "TIE":
        failures.append("DIRECTION")

    return {
        "symbol": symbol,
        "payout": item["payout"],
        "direction": direction,
        "score": score,
        "call_score": call,
        "put_score": put,
        "rsi": rsi,
        "adx": adx,
        "atr_percent": atr_percent,
        "body_ratio": body_ratio,
        "failures": failures
    }


def make_report(scanned, results, errors):
    counts = {
        "ADX": 0,
        "RSI": 0,
        "ATR": 0,
        "BODY": 0,
        "SCORE": 0,
        "DIRECTION": 0
    }

    valid = []

    for result in results:
        for reason in result["failures"]:
            counts[reason] += 1

        if not result["failures"]:
            valid.append(result)

    near_misses = sorted(
        results,
        key=lambda x: (
            x["score"],
            -len(x["failures"]),
            x["adx"]
        ),
        reverse=True
    )[:5]

    lines = [
        f"🤖 {VERSION}",
        "",
        "⚪ DIAGNOSTIC REPORT",
        "",
        f"PAIRS SCANNED: {scanned}",
        f"PAIRS ANALYZED: {len(results)}",
        f"DATA ERRORS: {errors}",
        f"MINIMUM PAYOUT: {MIN_PAYOUT}%",
        "",
        "========== FILTER RESULTS ==========",
        f"ADX FAILED: {counts['ADX']}",
        f"RSI FAILED: {counts['RSI']}",
        f"ATR FAILED: {counts['ATR']}",
        f"BODY FAILED: {counts['BODY']}",
        f"SCORE FAILED: {counts['SCORE']}",
        f"DIRECTION TIE: {counts['DIRECTION']}",
        "",
        f"VALID SETUPS: {len(valid)}",
        "",
        "========== TOP 5 NEAR MISSES =========="
    ]

    for item in near_misses:
        lines.append(
            f"{item['symbol']} | {item['direction']} "
            f"| SCORE {item['score']}/7 "
            f"| RSI {item['rsi']:.1f} "
            f"| ADX {item['adx']:.1f}"
        )

        lines.append(
            f"FAILED: {', '.join(item['failures']) or 'NONE'}"
        )

    if valid:
        lines.extend([
            "",
            "VALID SETUPS FOUND.",
            "This diagnostic does not send trade signals."
        ])
    else:
        lines.extend([
            "",
            "NO VALID SETUP.",
            "No trade recommended."
        ])

    lines.extend([
        "",
        "WIN RATE: NOT ENOUGH DATA",
        "DIAGNOSTIC ONLY - NO TRADE EXECUTED"
    ])

    return "\n".join(lines)


def main():
    log("=" * 45)
    log(VERSION)
    log("=" * 45)

    if not API_KEY:
        raise RuntimeError("OTCHARTS_API_KEY missing.")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram secrets missing.")

    symbols = get_symbols()

    log(f"ELIGIBLE SYMBOLS: {len(symbols)}")

    results = []
    errors = 0

    for index, item in enumerate(symbols, start=1):
        log(
            f"[{index}/{len(symbols)}] "
            f"Analyzing {item['symbol']} "
            f"| payout {item['payout']}%"
        )

        try:
            result = analyze(item)
            results.append(result)

            log(
                f"RESULT {item['symbol']} "
                f"| SCORE {result['score']}/7 "
                f"| FAILED {result['failures']}"
            )

        except Exception as error:
            errors += 1
            log(f"ERROR {item['symbol']}: {error}")

    report = make_report(
        len(symbols),
        results,
        errors
    )

    log("")
    log(report)

    telegram_send(report)

    log("DIAGNOSTIC COMPLETE.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"FATAL ERROR: {error}")

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            telegram_send(
                f"🤖 {VERSION}\n🔴 BOT ERROR\n{str(error)[:700]}"
            )

        raise
