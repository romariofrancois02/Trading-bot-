import os
import requests
import yfinance as yf
import pandas as pd
import ta

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Secrets TELEGRAM_TOKEN oswa TELEGRAM_CHAT_ID pa rekonèt.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        res = requests.post(url, json=payload)
        print("Mesaage voye bay Telegram:", res.status_code)
    except Exception as e:
        print("Erè nan voye mesaj:", e)

def analyze_market(ticker="EURUSD=X"):
    try:
        data = yf.Ticker(ticker)
        df = data.history(period="5d", interval="1m")
        if df.empty or len(df) < 200:
            print(f"[{ticker}] Pa gen ase done pou kalkile EMA 200.")
            return

        df['EMA200'] = ta.trend.ema_indicator(df['Close'], window=200)

        bb = ta.volatility.BollingerBands(df['Close'], window=20, window_dev=2)
        df['bb_high'] = bb.bollinger_hband()
        df['bb_low'] = bb.bollinger_lband()

        df['RSI'] = ta.momentum.rsi(df['Close'], window=14)

        latest = df.iloc[-1]
        price = float(latest['Close'])
        ema200 = float(latest['EMA200'])
        rsi = float(latest['RSI'])
        bb_low = float(latest['bb_low'])
        bb_high = float(latest['bb_high'])

        print(f"[{ticker}] Pri: {price:.5f} | EMA200: {ema200:.5f} | RSI: {rsi:.1f}")

        if price > ema200 and price <= bb_low and rsi <= 35:
            msg = (
                f"🔥 SIYAL ANRAJE: CALL 🚀\n"
                f"Asset: {ticker}\n"
                f"Pri: {price:.5f}\n"
                f"RSI: {rsi:.1f} (Oversold)\n"
                f"Expirations kòmande: 1M - 3M"
            )
            send_telegram_msg(msg)

        elif price < ema200 and price >= bb_high and rsi >= 65:
            msg = (
                f"🔥 SIYAL ANRAJE: PUT 🔻\n"
                f"Asset: {ticker}\n"
                f"Pri: {price:.5f}\n"
                f"RSI: {rsi:.1f} (Overbought)\n"
                f"Expirations kòmande: 1M - 3M"
            )
            send_telegram_msg(msg)
    except Exception as e:
        print(f"Erè lè w ap analize {ticker}: {e}")

if __name__ == "__main__":
    # Tèst mesaj pou konfime bot la ap kominike ak Telegram
    send_telegram_msg("🤖 Kenm bot limen epi l ap siveye mache a!")
    
    assets = ["EURUSD=X", "GBPUSD=X", "USDJPY=X"]
    for asset in assets:
        analyze_market(asset)
