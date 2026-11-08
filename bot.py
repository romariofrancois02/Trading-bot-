
import os
import re
import html
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ==========================================
# ROMARIO TRADER PRO 3.5
# PROFESSIONAL FOREX OTC SIGNAL
# ONE BEST SIGNAL PER MANUAL RUN
# ==========================================

API_KEY = os.getenv("OTCHARTS_API_KEY")
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
LOCAL_TZ = ZoneInfo("America/New_York")

M1_TF = 60
M5_TF = 300
CANDLE_LIMIT = 120

SWEEP_LOOKBACK = 10
REQUEST_DELAY = 0.10

# Mete payout reyèl Pocket Option la isit la.
# Egzanp: 85.0 vle di 85%.
# Kite None si ou poko verifye payout la.
PAYOUT_PERCENT = None

# Fichye sa a ka gen rezilta trade ou verifye.
# Kolòn obligatwa: result
# Egzanp: WIN, LOSS, WIN, LOSS
HISTORY_FILE = "trade_history.csv"

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "ROMARIO-TRADER-PRO-3.5"
})


# ==========================================
# TIME
# ==========================================

def local_time(value):
    return value.astimezone(LOCAL_TZ).strftime(
        "%I:%M:%S %p EST/EDT"
    )


def parse_timestamp(value):
    if isinstance(value, (int, float)):
        unit = "ms" if value > 100000000000 else "s"
        return pd.to_datetime(
            value, unit=unit, utc=True
        )

    return pd.to_datetime(value, utc=True)


# ==========================================
# API
# ==========================================

def api_get(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=30
    )

    response.raise_for_status()
    return response.json()


def get_symbols():
    print("Loading Pocket Option OTC catalog...")

    data = api_get(
        "/v1/symbols",
        {"venue": "otc"}
    )

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

    # Forex OTC only: six letters + _otc
    # Excludes stocks, crypto and indices.
    for item in items:

        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", ""))

        if not re.fullmatch(
            r"[A-Z]{6}_otc",
            symbol,
            flags=re.IGNORECASE
        ):
            continue

        opened = item.get("open")

        if opened is False:
            continue

        if isinstance(opened, str):
            if opened.lower() in (
                "false", "closed", "0"
            ):
                continue

        name = item.get("name") or symbol

        result[symbol] = {
            "symbol": symbol,
            "name": str(name)
        }

    symbols = list(result.values())

    if not symbols:
        raise RuntimeError(
            "No Forex OTC symbols found. "
            "Check the API catalog."
        )

    print("FOREX OTC PAIRS:", len(symbols))
    return symbols


# ==========================================
# CANDLE DATA
# ==========================================

def get_candles(symbol, timeframe):

    data = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": timeframe,
            "limit": CANDLE_LIMIT
        }
    )

    candles = data.get("candles", [])

    if not candles:
        raise RuntimeError("No candle data")

    df = pd.DataFrame(candles)

    required = [
        "time", "open", "high", "low", "close"
    ]

    for col in required:
        if col not in df.columns:
            raise RuntimeError(
                f"Missing field: {col}"
            )

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(
            df[col], errors="coerce"
        )

    df["time"] = df["time"].apply(parse_timestamp)

    df = df.dropna(subset=required)
    df = df.sort_values("time")
    df = df.drop_duplicates("time")

    return df.reset_index(drop=True)


# ==========================================
# INDICATORS
# ==========================================

def add_indicators(df):

    df = df.copy()

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

    return df


def completed_candles(df, timeframe, now):

    return df[
        df["time"] + pd.Timedelta(
            seconds=timeframe
        ) <= now
    ].reset_index(drop=True)


# ==========================================
# STRICT SIGNAL LOGIC
# ==========================================

def analyze_pair(m1, m5, now):

    if len(m1) < 40 or len(m5) < 30:
        return None

    last = m1.iloc[-1]
    bos = m1.iloc[-2]
    sweep = m1.iloc[-3]

    signal_close_time = (
        last["time"] + pd.Timedelta(seconds=M1_TF)
    )

    # Latest M5 candle closed before signal time
    valid_m5 = m5[
        m5["time"] + pd.Timedelta(
            seconds=M5_TF
        ) <= signal_close_time
    ]

    if valid_m5.empty:
        return None

    trend = valid_m5.iloc[-1]

    # Freshness checks
    age_m1 = (
        now - (
            last["time"] + pd.Timedelta(seconds=M1_TF)
        )
    ).total_seconds()

    age_m5 = (
        now - (
            trend["time"] + pd.Timedelta(seconds=M5_TF)
        )
    ).total_seconds()

    if age_m1 < 0 or age_m1 > 180:
        return None

    if age_m5 < 0 or age_m5 > 600:
        return None

    history = m1.iloc[
        -3-SWEEP_LOOKBACK:-3
    ]

    if len(history) < SWEEP_LOOKBACK:
        return None

    old_low = history["low"].min()
    old_high = history["high"].max()

    # Liquidity sweep
    bullish_sweep = (
        sweep["low"] < old_low
        and sweep["close"] > old_low
    )

    bearish_sweep = (
        sweep["high"] > old_high
        and sweep["close"] < old_high
    )

    # Break of structure
    bullish_bos = (
        bullish_sweep
        and bos["close"] > sweep["high"]
        and bos["close"] > bos["open"]
    )

    bearish_bos = (
        bearish_sweep
        and bos["close"] < sweep["low"]
        and bos["close"] < bos["open"]
    )

    # Retest
    atr_range = (
        m1["high"] - m1["low"]
    ).rolling(14).mean().iloc[-3]

    if pd.isna(atr_range) or atr_range <= 0:
        return None

    tolerance = atr_range * 0.20

    bullish_retest = (
        bullish_bos
        and last["low"] <= sweep["high"] + tolerance
        and last["close"] > sweep["high"]
        and last["close"] > last["open"]
    )

    bearish_retest = (
        bearish_bos
        and last["high"] >= sweep["low"] - tolerance
        and last["close"] < sweep["low"]
        and last["close"] < last["open"]
    )

    # M5 trend confirmation
    m5_call = (
        trend["ema20"] > trend["ema50"]
        and trend["close"] > trend["ema20"]
    )

    m5_put = (
        trend["ema20"] < trend["ema50"]
        and trend["close"] < trend["ema20"]
    )

    # RSI confirmation
    rsi = float(last["rsi"])

    if bullish_retest and m5_call and 50 <= rsi <= 70:

        return {
            "direction": "CALL",
            "price": float(last["close"]),
            "rsi": round(rsi, 2),
            "time": signal_close_time,
            "score": 5
        }

    if bearish_retest and m5_put and 30 <= rsi <= 50:

        return {
            "direction": "PUT",
            "price": float(last["close"]),
            "rsi": round(rsi, 2),
            "time": signal_close_time,
            "score": 5
        }

    return None


# ==========================================
# WIN RATE HISTORY
# ==========================================

def get_win_rate():

    if not os.path.exists(HISTORY_FILE):
        return None

    try:
        df = pd.read_csv(HISTORY_FILE)

        if "result" not in df.columns:
            return None

        results = (
            df["result"]
            .astype(str)
            .str.upper()
        )

        wins = (results == "WIN").sum()
        losses = (results == "LOSS").sum()

        total = wins + losses

        if total == 0:
            return None

        return {
            "wins": int(wins),
            "losses": int(losses),
            "total": int(total),
            "rate": round(wins / total * 100, 2)
        }

    except Exception:
        return None


# ==========================================
# TELEGRAM MESSAGE
# ==========================================

def send_telegram(message):

    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        },
        timeout=20
    )

    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("Telegram delivery failed")


def build_message(signal, win_data):

    direction = signal["direction"]

    arrow = "⬆️" if direction == "CALL" else "⬇️"

    if PAYOUT_PERCENT is None:
        payout_text = "NOT VERIFIED"
    else:
        payout_text = f"{PAYOUT_PERCENT:.1f}%"

    if win_data is None:
        win_text = "NOT VERIFIED"
        history_text = "No verified trade history"
    else:
        win_text = f"{win_data['rate']:.2f}%"
        history_text = (
            f"{win_data['wins']} wins / "
            f"{win_data['losses']} losses "
            f"({win_data['total']} trades)"
        )

    pair = html.escape(signal["name"])
    symbol = html.escape(signal["symbol"])

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "   <b>ROMARIO TRADER PRO 3.5</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>{arrow} {direction}</b>\n"
        f"<b>{pair}</b>\n"
        f"<code>{symbol}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>TRADE DETAILS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Timeframe: <b>M1</b>\n"
        "Expiration: <b>60 Seconds</b>\n"
        f"Entry reference: <b>{signal['price']}</b>\n"
        f"Florida time: <b>{local_time(signal['time'].to_pydatetime())}</b>\n"
        f"RSI: <b>{signal['rsi']}</b>\n"
        "Setup score: <b>5/5</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>PERFORMANCE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Payout / profit rate: <b>{payout_text}</b>\n"
        f"Verified win rate: <b>{win_text}</b>\n"
        f"History: {history_text}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Technical candidate only.</i>\n"
        "<i>Price must be checked on Pocket Option.</i>\n"
        "<i>No guaranteed profit. Manual trading only.</i>"
    )


# ==========================================
# MAIN
# ==========================================

def main():

    print("ROMARIO TRADER PRO 3.5")
    print("ONE BEST FOREX OTC SIGNAL")
    print("MANUAL MODE")

    if not API_KEY or not TOKEN or not CHAT_ID:
        raise RuntimeError("Missing GitHub Secrets")

    now = pd.Timestamp.now(tz="UTC")

    symbols = get_symbols()

    candidates = []
    scanned = 0
    skipped = 0

    for index, item in enumerate(symbols, 1):

        symbol = item["symbol"]
        name = item["name"]

        print(
            f"SCANNING {index}/{len(symbols)}: {name}"
        )

        try:

            m1 = get_candles(symbol, M1_TF)
            m5 = get_candles(symbol, M5_TF)

            m1 = add_indicators(m1)
            m5 = add_indicators(m5)

            m1 = completed_candles(
                m1, M1_TF, now
            )

            m5 = completed_candles(
                m5, M5_TF, now
            )

            result = analyze_pair(
                m1, m5, now
            )

            scanned += 1

            if result:
                result["symbol"] = symbol
                result["name"] = name
                candidates.append(result)

                print(
                    "VALID CANDIDATE:",
                    name,
                    result["direction"]
                )

        except Exception as error:

            skipped += 1
            print("SKIPPED:", symbol, str(error))

    print("\nSCAN FINISHED")
    print("PAIRS:", len(symbols))
    print("SCANNED:", scanned)
    print("SKIPPED:", skipped)
    print("CANDIDATES:", len(candidates))

    if not candidates:

        message = (
            "ROMARIO TRADER PRO 3.5\n\n"
            "STATUS: WAIT\n"
            "No valid Forex OTC setup found.\n\n"
            f"Pairs found: {len(symbols)}\n"
            f"Scanned: {scanned}\n"
            f"Skipped: {skipped}\n\n"
            "No trade recommended."
        )

        send_telegram(message)
        print("WAIT REPORT SENT")
        return

    # Select ONE best candidate.
    # If tied, choose the most recent completed signal.
    best = sorted(
        candidates,
        key=lambda x: x["time"],
        reverse=True
    )[0]

    win_data = get_win_rate()

    message = build_message(
        best,
        win_data
    )

    send_telegram(message)

    print("ONE SIGNAL SENT:", best["symbol"])
    print("DIRECTION:", best["direction"])


if __name__ == "__main__":
    main()
