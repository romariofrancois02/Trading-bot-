
import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

# ==========================================
# ROMARIO TRADER PRO 3.4
# MORE SIGNALS - ALL OTC
# MANUAL SCAN ONLY
# ==========================================

API_KEY = os.getenv("OTCHARTS_API_KEY")
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
OFFSET_HOURS = 2

M1_TF = 60
M5_TF = 300
LIMIT = 100

DELAY = 0.12
MAX_SYMBOLS = 150

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "ROMARIO-TRADER-PRO-3.4"
})


# ==========================================
# API
# ==========================================

def api_get(path, params=None):
    r = session.get(
        BASE_URL + path,
        params=params,
        timeout=30
    )
    r.raise_for_status()
    return r.json()


def convert_time(value):
    if isinstance(value, (int, float)):
        unit = "ms" if value > 100000000000 else "s"
        dt = pd.to_datetime(value, unit=unit, utc=True)
    else:
        dt = pd.to_datetime(value, utc=True)

    return dt - timedelta(hours=OFFSET_HOURS)


# ==========================================
# SYMBOL LIST
# ==========================================

def get_symbols():
    data = api_get("/v1/symbols", {"venue": "otc"})

    if isinstance(data, list):
        items = data
    else:
        items = (
            data.get("symbols")
            or data.get("instruments")
            or data.get("data")
            or []
        )

    if isinstance(items, dict):
        items = items.get("symbols", [])

    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue

        symbol = item.get("symbol")
        if not symbol:
            continue

        opened = item.get("open")
        if opened is False:
            continue
        if isinstance(opened, str) and opened.lower() in (
            "false", "closed", "0"
        ):
            continue

        name = item.get("name") or symbol
        result[str(symbol)] = {
            "symbol": str(symbol),
            "name": str(name)
        }

    symbols = list(result.values())[:MAX_SYMBOLS]

    if not symbols:
        raise RuntimeError("No OTC symbols returned by API.")

    return symbols


# ==========================================
# CANDLES
# ==========================================

def get_candles(symbol, timeframe):
    data = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": timeframe,
            "limit": LIMIT
        }
    )

    candles = data.get("candles", [])
    if not candles:
        raise RuntimeError("No candles")

    df = pd.DataFrame(candles)

    required = ["time", "open", "high", "low", "close"]
    for col in required:
        if col not in df.columns:
            raise RuntimeError("Missing field: " + col)

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = df["time"].apply(convert_time)
    df = df.dropna(subset=required)
    df = df.sort_values("time")
    df = df.drop_duplicates("time")

    return df.reset_index(drop=True)


# ==========================================
# INDICATORS
# ==========================================

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

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1/14, adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1/14, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))

    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)

    return df


def completed(df, seconds, now):
    return df[
        df["time"] + timedelta(seconds=seconds) <= now
    ].reset_index(drop=True)


# ==========================================
# SIGNAL LOGIC
# ==========================================

def analyze(m1, m5, now):

    if len(m1) < 30 or len(m5) < 25:
        return None

    a = m1.iloc[-1]
    previous = m1.iloc[-2]
    trend = m5.iloc[-1]

    age1 = (
        now - (a["time"] + timedelta(seconds=M1_TF))
    ).total_seconds()

    age5 = (
        now - (trend["time"] + timedelta(seconds=M5_TF))
    ).total_seconds()

    if age1 < 0 or age1 > 180:
        return None

    if age5 < 0 or age5 > 600:
        return None

    # CALL CONDITIONS
    call_score = 0

    if a["ema9"] > a["ema20"]:
        call_score += 1

    if a["close"] > a["ema9"]:
        call_score += 1

    if 52 <= a["rsi"] <= 75:
        call_score += 1

    if a["close"] > a["open"] and a["close"] > previous["close"]:
        call_score += 1

    if trend["ema20"] > trend["ema50"]:
        call_score += 1

    # PUT CONDITIONS
    put_score = 0

    if a["ema9"] < a["ema20"]:
        put_score += 1

    if a["close"] < a["ema9"]:
        put_score += 1

    if 25 <= a["rsi"] <= 48:
        put_score += 1

    if a["close"] < a["open"] and a["close"] < previous["close"]:
        put_score += 1

    if trend["ema20"] < trend["ema50"]:
        put_score += 1

    if call_score >= 3 and call_score > put_score:
        direction = "CALL"
        score = call_score

    elif put_score >= 3 and put_score > call_score:
        direction = "PUT"
        score = put_score

    elif call_score >= 2 and call_score > put_score:
        direction = "WEAK CALL"
        score = call_score

    elif put_score >= 2 and put_score > call_score:
        direction = "WEAK PUT"
        score = put_score

    else:
        return None

    if score >= 4:
        strength = "STRONGER TECHNICAL SETUP"
    elif score == 3:
        strength = "MODERATE TECHNICAL SETUP"
    else:
        strength = "WEAK - EXTRA CAUTION"

    return {
        "direction": direction,
        "score": score,
        "strength": strength,
        "price": float(a["close"]),
        "rsi": round(float(a["rsi"]), 2),
        "time": a["time"]
    }


# ==========================================
# TELEGRAM
# ==========================================

def telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    r = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )

    r.raise_for_status()

    if not r.json().get("ok"):
        raise RuntimeError("Telegram rejected message.")


# ==========================================
# SCAN
# ==========================================

def main():

    print("ROMARIO TRADER PRO 3.4")
    print("ALL OTC - MORE SIGNALS")
    print("MANUAL SCAN")
    print("==============================")

    if not API_KEY or not TOKEN or not CHAT_ID:
        raise RuntimeError("Missing GitHub Secrets.")

    now = datetime.now(timezone.utc)

    symbols = get_symbols()

    print("OTC SYMBOLS:", len(symbols))

    signals = []
    scanned = 0
    skipped = 0

    for number, item in enumerate(symbols, 1):

        symbol = item["symbol"]
        name = item["name"]

        print(f"\n{number}/{len(symbols)} SCANNING {name}")

        try:
            m1 = get_candles(symbol, M1_TF)
            time.sleep(DELAY)

            m5 = get_candles(symbol, M5_TF)
            time.sleep(DELAY)

            m1 = indicators(m1)
            m5 = indicators(m5)

            m1 = completed(m1, M1_TF, now)
            m5 = completed(m5, M5_TF, now)

            result = analyze(m1, m5, now)

            scanned += 1

            if result:
                result["name"] = name
                result["symbol"] = symbol
                signals.append(result)

                print(
                    "SIGNAL:",
                    result["direction"],
                    "SCORE:",
                    result["score"],
                    "/5"
                )
            else:
                print("WAIT")

        except Exception as error:
            skipped += 1
            print("SKIPPED:", symbol, str(error))

        time.sleep(DELAY)

    # ======================================
    # REPORT
    # ======================================

    calls = [
        s for s in signals
        if "CALL" in s["direction"]
    ]

    puts = [
        s for s in signals
        if "PUT" in s["direction"]
    ]

    lines = [
        "ROMARIO TRADER PRO 3.4",
        "ALL OTC SCANNER",
        "-------------------------",
        f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"OTC FOUND: {len(symbols)}",
        f"SCANNED: {scanned}",
        f"SKIPPED: {skipped}",
        f"TOTAL SIGNALS: {len(signals)}",
        f"CALL: {len(calls)}",
        f"PUT: {len(puts)}",
        "TIMEFRAME: M1",
        "TREND: M5",
        "EXPIRATION REFERENCE: 60 SECONDS",
        "-------------------------"
    ]

    if not signals:
        lines.extend([
            "STATUS: WAIT",
            "No setup met the signal threshold."
        ])

    for s in signals:
        lines.extend([
            "",
            f"PAIR: {s['name']}",
            f"SYMBOL: {s['symbol']}",
            f"SIGNAL: {s['direction']}",
            f"TECHNICAL SCORE: {s['score']}/5",
            f"SETUP: {s['strength']}",
            f"OTCHARTS CLOSE: {s['price']}",
            f"RSI: {s['rsi']}",
            f"CANDLE TIME: {s['time']}",
            "-------------------------"
        ])

    lines.extend([
        "",
        "IMPORTANT:",
        "Score is NOT a win probability.",
        "Weak signals require extra caution.",
        "OTCharts prices are not verified against",
        "Pocket Option execution prices.",
        "MANUAL ALERTS ONLY - NO AUTO TRADES."
    ])

    message = "\n".join(lines)

    # Telegram messages must stay below limit.
    chunks = []
    current = ""

    for line in message.split("\n"):
        if len(current) + len(line) + 1 > 3500:
            chunks.append(current)
            current = ""
        current += line + "\n"

    if current:
        chunks.append(current)

    for chunk in chunks:
        telegram(chunk)

    print("\nSCAN COMPLETE")
    print("SIGNALS:", len(signals))
    print("SKIPPED:", skipped)


if __name__ == "__main__":
    main()
