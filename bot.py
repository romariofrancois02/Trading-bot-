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
        print("Mesaj voye bay Telegram:", res.status_code)
    except Exception as e:
        print("Erè nan voye mesaj:", e)

def analyze_market(ticker, display_name):
    try:
        data = yf.Ticker(ticker)
        df = data.history(period="5d", interval="1m")
        if df.empty or len(df) < 200:
            print(f"[{display_name}] Pa gen ase done pou kalkile EMA 200.")
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

        print(f"[{display_name}] Pri: {price:.5f} | EMA200: {ema200:.5f} | RSI: {rsi:.1f}")

        if price > ema200 and price <= bb_low and rsi <= 35:
            msg = (
                f"🔥 SIYAL OTC ANRAJE: CALL 🚀\n"
                f"Asset: {display_name}\n"
                f"Pri: {price:.5f}\n"
                f"RSI: {rsi:.1f} (Oversold)\n"
                f"Expirations kòmande: 1M - 3M"
            )
            send_telegram_msg(msg)

        elif price < ema200 and price >= bb_high and rsi >= 65:
            msg = (
                f"🔥 SIYAL OTC ANRAJE: PUT 🔻\n"
                f"Asset: {display_name}\n"
                f"Pri: {price:.5f}\n"
                f"RSI: {rsi:.1f} (Overbought)\n"
                f"Expirations kòmande: 1M - 3M"
            )
            send_telegram_msg(msg)
    except Exception as e:
        print(f"Erè lè w ap analize {display_name}: {e}")

if __name__ == "__main__":
    send_telegram_msg("🤖 Kenm bot OTC elaji a limen, l ap tcheke tout mache yo!")
    
    # Lis tout gwo pè lajan ki koresponn ak mache OTC sou Pocket Option
    assets = [
        ("EURUSD=X", "EUR/USD OTC"),
        ("GBPUSD=X", "GBP/USD OTC"),
        ("USDJPY=X", "USD/JPY OTC"),
        ("AUDUSD=X", "AUD/USD OTC"),
        ("USDCAD=X", "USD/CAD OTC"),
        ("USDCHF=X", "USD/CHF OTC"),
        ("EURJPY=X", "EUR/JPY OTC"),
        ("GBPJPY=X", "GBP/JPY OTC"),
        ("CADJPY=X", "CAD/JPY OTC"),
        ("AUDCAD=X", "AUD/CAD OTC"),
        ("NZDUSD=X", "NZD/USD OTC"),
        ("CHFJPY=X", "CHF/JPY OTC")
    ]
    
    for ticker, display_name in assets:
        analyze_market(ticker, display_name)
