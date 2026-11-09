
import os
import re
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ==========================================
# ROMARIO TRADER PRO 5.2
# OTC TREND PULLBACK SCANNER
# ==========================================

BASE_URL = "https://otcharts.com"
API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_PAYOUT = 92
TIMEZONE = ZoneInfo("America/New_York")
HEADERS = {"Authorization": f"Bearer {API_KEY}"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

EMA_PERIOD = 20
TREND_EMA = 50
RSI_PERIOD = 14


def florida_now():
    return datetime.now(TIMEZONE)


def log(message):
    print(f"[{florida_now():%Y-%m-%d %I:%M:%S %p}] {message}")


def telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        log("Telegram secrets missing.")
        return False

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            data={"chat_id": TG_CHAT_ID, "text": message},
            timeout=15
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        log(f"Telegram error: {exc}")
        return False


def get_symbols():
    url = f"{BASE_URL}/v1/symbols"
    response = SESSION.get(url, params={"venue": "otc"}, timeout=20)
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("data", payload.get("symbols", []))
    else:
        items = []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", item.get("name", ""))).strip()
        payout = item.get("payout")

        if not re.fullmatch(r"[A-Z]{6}_otc", symbol, re.IGNORECASE):
            continue

        try:
            payout = float(str(payout).replace("%", ""))
        except (TypeError, ValueError):
            continue

        if payout >= MIN_PAYOUT:
            results.append({"symbol": symbol, "payout": payout})

    return results


def get_candles(symbol, timeframe, limit=120):
    url = f"{BASE_URL}/v1/candles"
    params = {
        "venue": "otc",
        "symbol": symbol,
        "tf": timeframe,
        "limit": limit
    }

    response = SESSION.get(url, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data", payload.get("candles", []))
    else:
        rows = []

    if not rows:
        return None

    df = pd.DataFrame(rows)

    required = {"open", "high", "low", "close"}
    if not required.issubset(df.columns):
        log(f"{symbol}: candle fields missing: {list(df.columns)}")
        return None

    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])

    if len(df) < 60:
        return None

    return df.reset_index(drop=True)


def calculate_rsi(series, period=RSI_PERIOD):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean()
    avg_loss = loss.ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def analyze(symbol, payout):
    m1 = get_candles(symbol, 60)
    m5 = get_candles(symbol, 300)

    if m1 is None or m5 is None:
        return None

    # Do not assume the final row is complete without timestamps.
    # The API timestamp schema must be verified before exact entry timing.
    m1 = m1.copy()
    m5 = m5.copy()

    m1["ema20"] = m1["close"].ewm(span=EMA_PERIOD, adjust=False).mean()
    m1["rsi"] = calculate_rsi(m1["close"])
    m5["ema50"] = m5["close"].ewm(span=TREND_EMA, adjust=False).mean()

    last = m1.iloc[-1]
    prev = m1.iloc[-2]
    trend = m5.iloc[-1]

    if pd.isna(last["rsi"]) or pd.isna(trend["ema50"]):
        return None

    checks = []

    # M5 direction
    bullish = trend["close"] > trend["ema50"]
    bearish = trend["close"] < trend["ema50"]

    # M1 trend alignment
    checks.append(last["close"] > last["ema20"] if bullish
                  else last["close"] < last["ema20"] if bearish else False)

    # Pullback near EMA20
    distance = abs(last["close"] - last["ema20"]) / last["close"] * 100
    checks.append(distance <= 0.15)

    # RSI momentum
    checks.append(50 < last["rsi"] < 70 if bullish
                  else 30 < last["rsi"] < 50 if bearish else False)

    # Candle direction
    checks.append(last["close"] > last["open"] if bullish
                  else last["close"] < last["open"] if bearish else False)

    # Previous candle pulled back toward EMA
    checks.append(
        prev["close"] < prev["ema20"] if bullish
        else prev["close"] > prev["ema20"] if bearish else False
    )

    # Avoid extremely small candle bodies
    body = abs(last["close"] - last["open"])
    candle_range = max(last["high"] - last["low"], 1e-10)
    checks.append(body / candle_range >= 0.35)

    score = sum(bool(x) for x in checks)

    if not bullish and not bearish:
        direction = None
    else:
        direction = "CALL" if bullish else "PUT"

    if direction is None:
        return None

    return {
        "symbol": symbol,
        "payout": payout,
        "direction": direction,
        "score": score,
        "checks": len(checks),
        "rsi": float(last["rsi"]),
        "distance": float(distance)
    }


def build_message(signal):
    emoji = "🟢" if signal["direction"] == "CALL" else "🔴"
    quality = "6/6" if signal["score"] == 6 else f'{signal["score"]}/6'

    now = florida_now()
    entry = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    signal_time = entry - timedelta(seconds=30)
    expiry = entry + timedelta(minutes=1)

    return (
        "🤖 ROMARIO TRADER PRO 5.2\n\n"
        f"{emoji} {signal['direction']} / "
        f"{'BUY' if signal['direction'] == 'CALL' else 'SELL'}\n\n"
        f"💱 PAIR: {signal['symbol']}\n"
        f"💰 PAYOUT: {signal['payout']:.1f}%\n"
        "📊 STRATEGY: TREND PULLBACK\n"
        "⏱ TIMEFRAME: M1\n"
        "⌛ EXPIRATION: 60 SECONDS\n"
        f"🎯 SETUP SCORE: {quality}\n"
        f"📈 RSI: {signal['rsi']:.1f}\n\n"
        f"🕒 SIGNAL TIME: {signal_time:%I:%M:%S %p}\n"
        f"🚀 ENTRY TIME: {entry:%I:%M:%S %p}\n"
        f"🏁 EXPIRY TIME: {expiry:%I:%M:%S %p}\n"
        "🌎 TIMEZONE: FLORIDA"
    )


def main():
    log("ROMARIO TRADER PRO 5.2 STARTING")

    if not API_KEY:
        raise RuntimeError("Missing OTCHARTS_API_KEY")
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Missing Telegram secrets")

    symbols = get_symbols()
    log(f"Forex OTC pairs with payout >= {MIN_PAYOUT}%: {len(symbols)}")

    if not symbols:
        log("No pairs meet payout requirement.")
        return

    candidates = []

    for index, item in enumerate(symbols, 1):
        symbol = item["symbol"]
        log(f"[{index}/{len(symbols)}] Scanning {symbol}")

        try:
            result = analyze(symbol, item["payout"])
            if result:
                candidates.append(result)
                log(
                    f"Candidate {symbol}: {result['direction']} "
                    f"{result['score']}/6"
                )
        except requests.RequestException as exc:
            log(f"{symbol}: API error: {exc}")
        except Exception as exc:
            log(f"{symbol}: analysis error: {exc}")

    if not candidates:
        log("No usable market data. No signal sent.")
        return

    # Always choose the highest-ranked valid candidate.
    candidates.sort(
        key=lambda x: (x["score"], x["payout"]),
        reverse=True
    )
    best = candidates[0]

    log(
        f"BEST: {best['symbol']} {best['direction']} "
        f"{best['score']}/6"
    )

    message = build_message(best)
    if telegram(message):
        log("Signal sent to Telegram.")
    else:
        log("Signal delivery failed.")


if __name__ == "__main__":
    main()
