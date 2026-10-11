
import os
import time
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime

# ==============================
# ROMARIO TRADER - EMA 20/50
# 1-minute breakout + retest
# ==============================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_EVERY_SECONDS = 15

# Forex nòmal, PA OTC Pocket Option
ASSETS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CAD": "CAD=X",
    "NZD/USD": "NZDUSD=X",
    "USD/CHF": "CHF=X",
    "EUR/JPY": "EURJPY=X",
    "GBP/JPY": "GBPJPY=X",
}

last_signals = {}


def log(message):
    now = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log("ERÈ: Ajiste TELEGRAM_TOKEN ak TELEGRAM_CHAT_ID.")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": message},
            timeout=10
        )

        data = response.json()

        if response.ok and data.get("ok"):
            log("Mesaj Telegram voye.")
            return True

        log(f"Telegram refize mesaj la: {data}")
        return False

    except Exception as e:
        log(f"Erè Telegram: {e}")
        return False


def get_candles(symbol):
    """Jwenn bouji 1-minit ki disponib sou Yahoo Finance."""
    try:
        df = yf.download(
            symbol,
            period="1d",
            interval="1m",
            progress=False,
            auto_adjust=False,
            threads=False
        )

        if df is None or df.empty:
            log(f"{symbol}: pa gen done.")
            return None

        # Sipò pou kolòn MultiIndex kèk vèsyon yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        required = ["Open", "High", "Low", "Close"]
        if not all(col in df.columns for col in required):
            log(f"{symbol}: kolòn bouji yo manke.")
            return None

        df = df[required].dropna()

        # Retire dènye bouji a, paske li ka poko fini
        if len(df) > 1:
            df = df.iloc[:-1].copy()

        if len(df) < 60:
            log(f"{symbol}: pa gen ase bouji pou analiz.")
            return None

        return df

    except Exception as e:
        log(f"Erè done {symbol}: {e}")
        return None


def add_indicators(df):
    df = df.copy()

    df["EMA20"] = df["Close"].ewm(
        span=20, adjust=False
    ).mean()

    df["EMA50"] = df["Close"].ewm(
        span=50, adjust=False
    ).mean()

    delta = df["Close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.ewm(
        alpha=1/14, adjust=False, min_periods=14
    ).mean()

    avg_loss = losses.ewm(
        alpha=1/14, adjust=False, min_periods=14
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"] = 100 - (100 / (1 + rs))

    return df.dropna()


def analyze(df):
    """
    Breakout sou bouji -2, retest/konfimasyon
    sou dènye bouji fèmen -1.
    """

    df = add_indicators(df)

    if len(df) < 60:
        return None

    previous = df.iloc[-2]
    candle = df.iloc[-1]

    # Zòn rezistans/sipò ki soti nan bouji ki
    # vini anvan bouji breakout ak retest yo.
    reference = df.iloc[-23:-3]

    resistance = reference["High"].max()
    support = reference["Low"].min()

    close = float(candle["Close"])
    open_price = float(candle["Open"])
    high = float(candle["High"])
    low = float(candle["Low"])

    ema20 = float(candle["EMA20"])
    ema50 = float(candle["EMA50"])
    rsi = float(candle["RSI"])

    prev_close = float(previous["Close"])

    # Tolerans retest: 10% lajè zòn referans lan
    zone_width = resistance - support
    tolerance = max(zone_width * 0.10, close * 0.0001)

    bullish = close > open_price
    bearish = close < open_price

    # CALL: tandans monte + breakout + retest
    call_breakout = prev_close > resistance
    call_retest = (
        low <= resistance + tolerance
        and close > resistance
    )

    if (
        ema20 > ema50
        and close > ema20
        and 52 <= rsi <= 68
        and bullish
        and call_breakout
        and call_retest
    ):
        return {
            "direction": "CALL 🟢",
            "price": close,
            "rsi": rsi,
            "ema20": ema20,
            "ema50": ema50,
            "zone": resistance
        }

    # PUT: tandans desann + kase sipò + retest
    put_breakout = prev_close < support
    put_retest = (
        high >= support - tolerance
        and close < support
    )

    if (
        ema20 < ema50
        and close < ema20
        and 32 <= rsi <= 48
        and bearish
        and put_breakout
        and put_retest
    ):
        return {
            "direction": "PUT 🔴",
            "price": close,
            "rsi": rsi,
            "ema20": ema20,
            "ema50": ema50,
            "zone": support
        }

    return None


def run_bot():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log("Mete TELEGRAM_TOKEN ak TELEGRAM_CHAT_ID anvan.")
        return

    log("ROMARIO TRADER DEMARE.")
    log("Mòd: Forex nòmal; EMA20/50 + RSI + retest.")
    log("Ap tann bouji ki konfime yon konfigirasyon...")

    while True:
        try:
            for asset, symbol in ASSETS.items():
                log(f"Analiz {asset}...")

                candles = get_candles(symbol)
                if candles is None:
                    continue

                signal = analyze(candles)

                if signal is None:
                    log(f"{asset}: pa gen siyal konfime.")
                    continue

                # Anpeche menm siyal la repete
                # sou menm bouji a.
                candle_time = str(candles.index[-1])
                signal_key = (asset, candle_time, signal["direction"])

                if last_signals.get(asset) == signal_key:
                    continue

                price = signal["price"]
                rsi = signal["rsi"]

                message = (
                    "👑 ROMARIO TRADER 👑\n\n"
                    f"📈 Pè: {asset}\n"
                    f"🔥 Siyal: {signal['direction']}\n"
                    f"💰 Pri referans: {price:.5f}\n"
                    f"📊 EMA20: {signal['ema20']:.5f}\n"
                    f"📊 EMA50: {signal['ema50']:.5f}\n"
                    f"📉 RSI(14): {rsi:.1f}\n"
                    f"🎯 Zòn breakout/retest: "
                    f"{signal['zone']:.5f}\n"
                    f"🕒 Bouji fèmen: {candle_time}\n\n"
                    "⚠️ Siyal eksperimantal, pa garanti pwofi.\n"
                    "Done yo soti nan Yahoo Finance, pa nan "
                    "Pocket Option OTC.\n"
                    "Verifye pri aktyèl la anvan nenpòt desizyon."
                )

                if send_telegram_msg(message):
                    last_signals[asset] = signal_key

            log("Analiz fini; pwochen sik la ap kòmanse.")
            time.sleep(CHECK_EVERY_SECONDS)

        except KeyboardInterrupt:
            log("Bot la kanpe.")
            break

        except Exception as e:
            log(f"Erè jeneral: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run_bot()
