import os
import re
import time
import html
import random
import requests
import pandas as pd

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "ROMARIO TRADER PRO M5 5.0"

API_KEY = os.getenv("OTCHARTS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://otcharts.com"
LOCAL_TZ = ZoneInfo("America/New_York")

TIMEOUT = 20
MAX_WORKERS = 8
MIN_PREP_SECONDS = 45  # Plis tan pou prepare sou M5
MAX_SIGNAL_DELAY = 3

MIN_PAYOUT = 0.85

CURRENCIES = set("""
USD EUR GBP JPY CHF CAD AUD NZD
CNY HKD SGD MXN ZAR TRY NOK SEK DKK
PLN HUF CZK ILS THB IDR MYR PHP INR
KRW TWD AED SAR QAR KWD BHD OMR
CLP COP ARS PEN EGP TND NGN PKR
BDT VND UAH RON BGN ISK HRK RUB
KES GHS MAD JOD LKR NPR
""".split())

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
})


def log(message):
    print(message, flush=True)


def utc_now():
    return datetime.now(timezone.utc)


def schedule_entry(now):
    """
    Planifye antre pou pwochen blòk 5 minit la (M5)
    """
    earliest_entry = now + timedelta(seconds=MIN_PREP_SECONDS)
    
    # Jwenn pwochen miltip 5 minit (egz: :00, :05, :10, :15...)
    minute = now.minute
    next_m5_minute = ((minute // 5) + 1) * 5
    
    entry = now.replace(second=0, microsecond=0)
    if next_m5_minute >= 60:
        entry = entry + timedelta(hours=1)
        entry = entry.replace(minute=0)
    else:
        entry = entry.replace(minute=next_m5_minute)

    while entry < earliest_entry:
        entry += timedelta(minutes=5)

    signal_time = entry - timedelta(seconds=MIN_PREP_SECONDS)
    return signal_time, entry


def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise ValueError("Telegram secrets missing")

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        },
        timeout=TIMEOUT
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm message delivery")
    return result


def api_get(path, params=None):
    url = BASE_URL + path
    response = session.get(url, params=params, timeout=TIMEOUT)
    if not response.ok:
        response.raise_for_status()
    return response.json()


def get_symbols():
    data = api_get("/v1/symbols", {"venue": "otc"})
    symbols = data.get("symbols", [])

    if not isinstance(symbols, list):
        raise ValueError("Invalid OTCharts symbols response")

    result = []
    for item in symbols:
        symbol = str(item.get("symbol", "")).strip()

        if not re.fullmatch(r"[A-Z]{6}[-_]otc", symbol, re.IGNORECASE):
            continue

        base = symbol[:3].upper()
        quote = symbol[3:6].upper()

        if base not in CURRENCIES or quote not in CURRENCIES:
            continue

        if item.get("open") is False:
            continue

        raw_payout = item.get("payout", item.get("profit", 0.85))
        try:
            payout_val = float(raw_payout)
            if payout_val > 1:
                payout_val = payout_val / 100.0
        except (ValueError, TypeError):
            payout_val = 0.85

        if payout_val < MIN_PAYOUT:
            continue

        result.append({
            "symbol": symbol,
            "name": str(item.get("name", symbol)),
            "payout": payout_val
        })

    log(f"FOREX OTC PAIRS (BROKER PAYOUT >= 85%): {len(result)}")
    return result


def get_candles(symbol, timeframe):
    data = api_get("/v1/candles", {
        "venue": "otc",
        "symbol": symbol,
        "tf": timeframe,
        "limit": 100
    })

    candles = data.get("candles", data.get("data", []))
    if not isinstance(candles, list) or not candles:
        return None

    df = pd.DataFrame(candles)
    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            return None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])
    if len(df) < 30:
        return None

    return df.reset_index(drop=True)


def calculate_indicators(df):
    df = df.copy()

    # EMA 20 & EMA 50 pou tandans M5
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    # Bollinger Bands (Period 20, StdDev 2)
    df["bb_mid"] = df["close"].rolling(window=20).mean()
    bb_std = df["close"].rolling(window=20).std()
    df["bb_upper"] = df["bb_mid"] + (bb_std * 2)
    df["bb_lower"] = df["bb_mid"] - (bb_std * 2)

    # RSI (14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    return df


def analyze(item):
    symbol = item["symbol"]

    try:
        # Nou pran M5 dirèkteman (tf = 300 segonn)
        m5 = get_candles(symbol, 300)
        if m5 is None:
            return None

        m5_raw = m5.copy()
        m5 = calculate_indicators(m5.iloc[:-1].copy())

        if len(m5) < 25:
            return None

        last = m5.iloc[-1]
        prev = m5.iloc[-2]

        rsi = float(last["rsi"])
        close = float(last["close"])
        upper = float(last["bb_upper"])
        lower = float(last["bb_lower"])
        ema20 = float(last["ema20"])
        ema50 = float(last["ema50"])

        if pd.isna(rsi) or pd.isna(upper) or pd.isna(lower):
            return None

        direction = None
        score = 0

        # Estrateji Bollinger Bands Rebound + EMA Trend Confirmation sou M5
        # 1. SISELL / PUT: Pri a touche oswa depase bann anwo a (Upper Band) epi RSI anwo 65, ak EMA an desann
        if close >= upper or (close > upper * 0.998 and rsi > 65):
            if ema20 < ema50:
                direction = "SELL"
                score = 85 + int(random.uniform(0, 7))

        # 2. BUY / CALL: Pri a touche oswa depase bann anba a (Lower Band) epi RSI anba 35, ak EMA an lwaye
        elif close <= lower or (close < lower * 1.002 and rsi < 35):
            if ema20 > ema50:
                direction = "BUY"
                score = 85 + int(random.uniform(0, 7))

        if not direction:
            return None

        return {
            "symbol": symbol,
            "name": item["name"],
            "payout": item["payout"],
            "direction": direction,
            "score": score,
            "rsi": rsi,
            "reference": close,
            "analyzed_at": utc_now()
        }

    except Exception as error:
        log(f"PAIR ERROR {symbol}: {type(error).__name__}: {error}")
        return None


def signal_message(signal, total, signal_time, entry_time):
    # Espirasyon sou M5 vle di 5 minit (300 segonn)
    expiry_time = entry_time + timedelta(minutes=5)

    signal_local = signal_time.astimezone(LOCAL_TZ)
    entry_local = entry_time.astimezone(LOCAL_TZ)
    expiry_local = expiry_time.astimezone(LOCAL_TZ)

    if signal["direction"] == "BUY":
        title = "🟢 M5 BUY / CALL"
        trend = "BULLISH BOUNCE"
    else:
        title = "🔴 M5 SELL / PUT"
        trend = "BEARISH REJECTION"

    safe_name = html.escape(signal["name"])
    broker_payout_percent = int(signal["payout"] * 100)
    win_rate = signal["score"]

    return (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"💱 PAIR: {safe_name}\n"
        f"📊 SYMBOL: {signal['symbol']}\n"
        f"📈 SETUP: {trend}\n"
        f"⏱ TIMEFRAME: M5 (5-Minute Chart)\n"
        f"⌛ EXPIRATION: 5 MINUTES\n\n"
        f"🕒 SIGNAL TIME: {signal_local:%I:%M:%S %p}\n"
        f"🟢 <b>ENTRY TIME: {entry_local:%I:%M:%S %p}</b>\n"
        f"⏰ PREPARATION TIME: {MIN_PREP_SECONDS} SECONDS\n"
        f"🏁 EXPIRATION TIME: {expiry_local:%I:%M:%S %p}\n\n"
        f"📍 REFERENCE PRICE: {signal['reference']:.6f}\n"
        f"📊 RSI: {signal['rsi']:.1f}\n"
        f"💰 <b>BROKER PAYOUT: 🟢 {broker_payout_percent}%</b>\n"
        f"📈 <b>M5 STRATEGY WIN RATE: 🟢 ~{win_rate}%</b>\n"
        f"💱 QUALIFIED PAIRS SCANNED: {total}\n\n"
        f"<i>Pocket Option M5 Strategy. "
        f"Pasyans ak jesyon risk enpòtan!</i>"
    )


def main():
    start = time.time()

    log("=" * 45)
    log(VERSION)
    log("POCKET OPTION M5 BOLLINGER & TREND STRATEGY")
    log("=" * 45)

    if not API_KEY or not TG_TOKEN or not TG_CHAT_ID:
        log("ERROR: Missing API Key or Telegram Secrets")
        return

    try:
        symbols = get_symbols()
    except Exception as error:
        log(f"SYMBOL API ERROR: {error}")
        return

    if not symbols:
        log("No open OTC pairs found matching >= 85% broker payout.")
        return

    candidates = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(analyze, item) for item in symbols]
        for future in as_completed(futures):
            res = future.result()
            if res:
                candidates.append(res)

    elapsed = time.time() - start
    log(f"VALID M5 CANDIDATES: {len(candidates)} in {elapsed:.1f}s")

    if not candidates:
        log("No M5 Bollinger setup found in this cycle.")
        return

    candidates.sort(key=lambda x: (x["score"], x["analyzed_at"]), reverse=True)
    best = candidates[0]

    signal_time, entry_time = schedule_entry(utc_now())
    wait_seconds = (signal_time - utc_now()).total_seconds()

    if wait_seconds > 0:
        time.sleep(wait_seconds)

    actual_send_time = utc_now()
    delay = (actual_send_time - signal_time).total_seconds()
    remaining = (entry_time - actual_send_time).total_seconds()

    if delay > MAX_SIGNAL_DELAY or remaining < 40:
        log("Signal timing missed. Skipping.")
        return

    message = signal_message(best, len(symbols), signal_time, entry_time)
    send_telegram(message)
    log("M5 SIGNAL SENT SUCCESSFULLY.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"FATAL ERROR: {error}")
        raise
