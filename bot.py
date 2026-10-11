
import os
import time
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ASSETS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X",
}

def log(message):
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        log("ERÈ: Telegram Secrets yo manke.")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message},
            timeout=10
        )
        response.raise_for_status()

        if not response.json().get("ok"):
            log("Telegram pa konfime mesaj la.")
            return False

        log("Telegram konfime mesaj la.")
        return True

    except Exception as error:
        log(f"ERÈ TELEGRAM: {error}")
        return False

def get_candles(symbol):
    log(f"Ap chèche bouji pou {symbol}...")

    try:
        df = yf.download(
            tickers=symbol,
            period="1d",
            interval="1m",
            progress=False,
            auto_adjust=False,
            threads=False,
            timeout=12
        )

        if df is None or df.empty:
            log(f"PA GEN DONE: {symbol}")
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        needed = ["Open", "High", "Low", "Close"]
        if not all(col in df.columns for col in needed):
            log(f"Kolòn bouji yo manke: {symbol}")
            return None

        df = df[needed].dropna()

        # Dènye bouji a ka poko fèmen.
        if len(df) > 1:
            df = df.iloc[:-1].copy()

        if len(df) < 60:
            log(f"Pa gen ase bouji pou {symbol}: {len(df)}")
            return None

        log(f"Done resevwa pou {symbol}: {len(df)} bouji")
        return df

    except Exception as error:
        log(f"ERÈ DONE {symbol}: {error}")
        return None

def analyze(df):
    df = df.copy()

    df["EMA20"] = df["Close"].ewm(
        span=20, adjust=False
    ).mean()

    df["EMA50"] = df["Close"].ewm(
        span=50, adjust=False
    ).mean()

    change = df["Close"].diff()
    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1/14, adjust=False, min_periods=14
    ).mean()

    avg_loss = loss.ewm(
        alpha=1/14, adjust=False, min_periods=14
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"] = 100 - (100 / (1 + rs))
    df = df.dropna()

    if len(df) < 30:
        return None

    previous = df.iloc[-2]
    candle = df.iloc[-1]
    reference = df.iloc[-23:-3]

    resistance = float(reference["High"].max())
    support = float(reference["Low"].min())

    close = float(candle["Close"])
    open_price = float(candle["Open"])
    high = float(candle["High"])
    low = float(candle["Low"])
    ema20 = float(candle["EMA20"])
    ema50 = float(candle["EMA50"])
    rsi = float(candle["RSI"])
    previous_close = float(previous["Close"])

    tolerance = close * 0.0002

    # CALL: breakout anwo, retest, fèmti anlè zòn nan
    if (
        ema20 > ema50
        and close > ema20
        and 52 <= rsi <= 68
        and previous_close > resistance
        and low <= resistance + tolerance
        and close > resistance
        and close > open_price
    ):
        return {
            "side": "CALL",
            "price": close,
            "ema20": ema20,
            "ema50": ema50,
            "rsi": rsi,
            "zone": resistance,
            "time": str(df.index[-1])
        }

    # PUT: breakout anba, retest, fèmti anba zòn nan
    if (
        ema20 < ema50
        and close < ema20
        and 32 <= rsi <= 48
        and previous_close < support
        and high >= support - tolerance
        and close < support
        and close < open_price
    ):
        return {
            "side": "PUT",
            "price": close,
            "ema20": ema20,
            "ema50": ema50,
            "rsi": rsi,
            "zone": support,
            "time": str(df.index[-1])
        }

    return None

def main():
    log("BOT STARTING")
    log("GitHub Actions kòmanse travay la.")

    if not TOKEN or not CHAT_ID:
        log("STOP: TELEGRAM_TOKEN oswa TELEGRAM_CHAT_ID manke.")
        return

    log("Telegram Secrets disponib.")
    signals_found = 0
    assets_checked = 0

    for asset, symbol in ASSETS.items():
        log(f"Kòmanse analiz: {asset}")

        df = get_candles(symbol)
        if df is None:
            continue

        assets_checked += 1
        signal = analyze(df)

        if signal is None:
            log(f"{asset}: PA GEN SIYAL KI KONFIME.")
            continue

        message = (
            "ROMARIO TRADER - SIYAL EKSPERIMANTAL\n\n"
            f"Direksyon: {signal['side']}\n"
            f"Pè: {asset}\n"
            f"Pri nan done yo: {signal['price']:.5f}\n"
            f"EMA20: {signal['ema20']:.5f}\n"
            f"EMA50: {signal['ema50']:.5f}\n"
            f"RSI: {signal['rsi']:.1f}\n"
            f"Zòn retest: {signal['zone']:.5f}\n"
            f"Bouji: {signal['time']}\n\n"
            "Done sa yo soti nan Yahoo Finance, PA nan "
            "Pocket Option OTC. Siyal la pa garanti pwofi. "
            "Verifye pri ak lè bouji a anvan ou pran nenpòt desizyon."
        )

        if send_telegram(message):
            signals_found += 1

    log(f"ANALIZ FINI | Pè analize: {assets_checked}")
    log(f"Siyal voye avèk siksè: {signals_found}")
    log("BOT FINISHED")

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"ERÈ JENERAL: {error}")
        raise
