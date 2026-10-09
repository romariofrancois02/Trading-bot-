import os
import requests
import yfinance as yf
import pandas as pd
import ta

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print("Erè nan voye mesaj:", e)

def analyze_market(ticker="EURUSD=X"):
    # Pran done sou 1 minit
    df = yf.download(tickers=ticker, period="1d", interval="1m")
    if df.empty or len(df) < 200:
        print("Pa gen ase done pou kalkile EMA 200.")
        return

    # 1. Trend Filter: EMA 200
    df['EMA200'] = ta.trend.ema_indicator(df['Close'], window=200)

    # 2. Reversal Indicator: Bollinger Bands (20, 2)
    bb = ta.volatility.BollingerBands(df['Close'], window=20, window_dev=2)
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low'] = bb.bollinger_lband()

    # 3. Momentum: RSI 14
    df['RSI'] = ta.momentum.rsi(df['Close'], window=14)

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(latest['Close'])
    ema200 = float(latest['EMA200'])
    rsi = float(latest['RSI'])
    bb_low = float(latest['bb_low'])
    bb_high = float(latest['bb_high'])

    print(f"[{ticker}] Pri: {price:.5f} | EMA200: {ema200:.5f} | RSI: {rsi:.1f}")

    # --- ESTRATEJI ANRAJE (3 Konfimasyon) ---
    
    # CONDITION CALL:
    # 1. Pri anwo EMA 200 (Strong Uptrend)
    # 2. Pri touche oswa travèse anba Bollinger Lower Band
    # 3. RSI anba 35 (Oversold)
    if price > ema200 and price <= bb_low and rsi <= 35:
        msg = (
            f"🔥 SIYAL ANRAJE: CALL 🚀\n"
            f"Asset: {ticker}\n"
            f"Pri: {price:.5f}\n"
            f"RSI: {rsi:.1f} (Oversold)\n"
            f"Konfimasyon: Rebound sou BB Low + Trend Anwo EMA 200\n"
            f"Expirations kòmande: 1M - 3M"
        )
        send_telegram_msg(msg)

    # CONDITION PUT:
    # 1. Pri anba EMA 200 (Strong Downtrend)
    # 2. Pri touche oswa travèse anwo Bollinger Upper Band
    # 3. RSI anwo 65 (Overbought)
    elif price < ema200 and price >= bb_high and rsi >= 65:
        msg = (
            f"🔥 SIYAL ANRAJE: PUT 🔻\n"
            f"Asset: {ticker}\n"
            f"Pri: {price:.5f}\n"
            f"RSI: {rsi:.1f} (Overbought)\n"
            f"Konfimasyon: Rejection sou BB High + Trend Anba EMA 200\n"
            f"Expirations kòmande: 1M - 3M"
        )
        send_telegram_msg(msg)

if __name__ == "__main__":
    # Ou ka ajoute lòt pè lajan si w vle konsa:
    assets = ["EURUSD=X", "GBPUSD=X", "USDJPY=X"]
    for asset in assets:
        analyze_market(asset)
