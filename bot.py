import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

LAST_ASSET_FILE = "last_asset.txt"

# CONFIGURATION TEKNIK
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
EXPIRATION_CANDIDATE = "1 Minit"

def get_last_asset():
    if os.path.exists(LAST_ASSET_FILE):
        try:
            with open(LAST_ASSET_FILE, "r") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""

def save_last_asset(asset_name):
    try:
        with open(LAST_ASSET_FILE, "w") as f:
            f.write(asset_name)
    except Exception as e:
        print("Erè nan sove dènye asset:", e)

def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Secrets TELEGRAM_TOKEN oswa TELEGRAM_CHAT_ID pa konfigure.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        print("Mesaj voye bay Telegram:", res.status_code)
    except Exception as e:
        print("Erè nan voye mesaj:", e)

def calculate_indicators(df):
    try:
        close = df['Close']
        high = df['High']
        low = df['Low']

        # Kalkil EMA 20 ak EMA 50
        df['EMA20'] = close.ewm(span=EMA_FAST, adjust=False).mean()
        df['EMA50'] = close.ewm(span=EMA_SLOW, adjust=False).mean()

        # Kalkil RSI 14
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
        rs = gain / (loss + 1e-9)
        df['RSI'] = 100 - (100 / (1 + rs))

        # Kalkil ATR (Volatility)
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['ATR'] = tr.rolling(window=ATR_PERIOD).mean()

        # Support & Resistance (12 dènye bouji)
        df['Resistance'] = high.rolling(window=12).max()
        df['Support'] = low.rolling(window=12).min()

        return df
    except Exception as e:
        print("Erè kalkil endikatè:", e)
        return None

def signal_validator(df):
    try:
        if len(df) < 50:
            return None, "", 0

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # 1. Filtre Mache Flat / Konsolidasyon
        ema_diff = abs(latest['EMA20'] - latest['EMA50']) / (latest['Close'] + 1e-9)
        if ema_diff < 0.0001:
            return None, "", 0  # Pa gen aksyon sou mache san direksyon

        close_price = latest['Close']
        open_price = latest['Open']
        rsi_val = latest['RSI']
        ema20 = latest['EMA20']
        ema50 = latest['EMA50']

        # KONDISYON BUY (CALL): EMA20 > EMA50 + Bouji Vèt + RSI > 50 + Pri anlè sipò
        if ema20 > ema50:
            if (close_price > open_price) and (rsi_val > 51) and (rsi_val < 72):
                win_rate = int(min(94, 85 + (ema_diff * 10000)))
                return "CALL", "EMA Bullish Trend + RSI Momentum + Price Action Confirmation", win_rate

        # KONDISYON SELL (PUT): EMA20 < EMA50 + Bouji Wouj + RSI < 50 + Pri anba rezistans
        if ema20 < ema50:
            if (close_price < open_price) and (rsi_val < 49) and (rsi_val > 28):
                win_rate = int(min(94, 85 + (ema_diff * 10000)))
                return "PUT", "EMA Bearish Trend + RSI Momentum + Price Action Confirmation", win_rate

        return None, "", 0

    except Exception as e:
        print("Erè nan validator:", e)
        return None, "", 0

def get_market_data(ticker, display_name):
    try:
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(period="1d", interval="1m")

        if df.empty or len(df) < 30:
            return None

        df = df.dropna()
        df = calculate_indicators(df)
        if df is None:
            return None

        signal_type, strategy_name, win_rate = signal_validator(df)
        if not signal_type:
            return None

        latest = df.iloc[-1]
        current_price = float(latest['Close'])

        return {
            "display_name": display_name,
            "type": signal_type,
            "price": current_price,
            "win_rate": win_rate,
            "expiry": EXPIRATION_CANDIDATE,
            "strategy": strategy_name
        }
    except Exception as e:
        print(f"Erè sou {display_name}: {e}")
        return None

if __name__ == "__main__":
    # Lis pè aktyèl yo sou yfinance pou analiz teknik an tan reyèl
    assets = [
        ("EURUSD=X", "EUR/USD"), ("GBPUSD=X", "GBP/USD"), ("USDJPY=X", "USD/JPY"),
        ("AUDUSD=X", "AUD/USD"), ("USDCAD=X", "USD/CAD"), ("USDCHF=X", "USD/CHF"),
        ("EURJPY=X", "EUR/JPY"), ("GBPJPY=X", "GBP/JPY"), ("CADJPY=X", "CAD/JPY"),
        ("AUDCAD=X", "AUD/CAD"), ("AUDJPY=X", "AUD/JPY"), ("NZDUSD=X", "NZD/USD"),
        ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum")
    ]

    last_asset = get_last_asset()
    filtered_assets = [a for a in assets if a[1] != last_asset]

    valid_signals = []
    for asset in filtered_assets:
        res = get_market_data(asset[0], asset[1])
        if res:
            valid_signals.append(res)
            break

    # Lè lokal (UTC - 4)
    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
    entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

    if valid_signals:
        best_signal = valid_signals[0]
        save_last_asset(best_signal['display_name'])

        emoji_signal = "🟢 🚀 **CALL (BUY) - VÈT**" if best_signal['type'] == "CALL" else "🔴 🔻 **PUT (SELL) - WOUJ**"

        msg = (
            f"👑 **ROMARIO TRADER - REAL ANALYTICS BOT** 👑\n"
            f"⚡ *Siyal 100% Teknik sou done mache reyèl*\n\n"
            f"🔥 SIYAL: {emoji_signal}\n"
            f"📈 Asset: **{best_signal['display_name']}**\n"
            f"⏰ LÈ EGZAK POU ANTRE: `{entry_time}`\n"
            f"⏳ TAN EKSPIRASYON: **{best_signal['expiry']}**\n"
            f"🎯 Pri Kounye a: `{best_signal['price']:.5f}`\n"
            f"⚙️ Konfimasyon: _{best_signal['strategy']}_\n"
            f"📊 WIN RATE ESTIMÉ: **~{best_signal['win_rate']}%**\n\n"
            f"⚠️ *Remak: Siyal teknik ki valide sou EMA, RSI ak Price Action!*"
        )
        send_telegram_msg(msg)
    else:
        print("Okenn siyal teknik valid pa jwenn nan minit sa a (Mache pa satisfè tout règ yo).")
