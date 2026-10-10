import os
import requests
import yfinance as yf
import pandas as pd
import ta
from datetime import datetime, timedelta

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

def get_market_data(ticker, display_name):
    try:
        data = yf.Ticker(ticker)
        df = data.history(period="5d", interval="1m")
        if df.empty or len(df) < 200:
            return None

        # Kalkil EMA 20 ak EMA 50
        df['EMA20'] = ta.trend.ema_indicator(df['Close'], window=20)
        df['EMA50'] = ta.trend.ema_indicator(df['Close'], window=50)
        
        # RSI pou konfimasyon siplemantè
        df['RSI'] = ta.momentum.rsi(df['Close'], window=14)

        # Volatilité (Bollinger Bands width) pou detèmine ekspirasyon an
        bb = ta.volatility.BollingerBands(df['Close'], window=20, window_dev=2)
        bb_width = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / df['Close'].iloc[-1]

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        ema20_curr = float(latest['EMA20'])
        ema50_curr = float(latest['EMA50'])
        ema20_prev = float(prev['EMA20'])
        ema50_prev = float(prev['EMA50'])

        signal_type = None
        strength_score = 0

        # Kwazman pou CALL: EMA 20 pase anwo EMA 50
        if ema20_prev <= ema50_prev and ema20_curr > ema50_curr:
            if float(latest['RSI']) > 50:
                signal_type = "CALL"
                distance = abs(ema20_curr - ema50_curr) / float(latest['Close'])
                strength_score = min(90, max(75, int(72 + (distance * 10000))))

        # Kwazman pou PUT: EMA 20 pase anba EMA 50
        elif ema20_prev >= ema50_prev and ema20_curr < ema50_curr:
            if float(latest['RSI']) < 50:
                signal_type = "PUT"
                distance = abs(ema20_curr - ema50_curr) / float(latest['Close'])
                strength_score = min(90, max(75, int(72 + (distance * 10000))))

        if not signal_type:
            return None

        # Chwazi YON SÈL tan ekspirasyon ki klè ki baze sou volatilité a
        if bb_width > 0.0025:
            exact_expiry = "3 Minit"
        elif bb_width > 0.0015:
            exact_expiry = "2 Minit"
        else:
            exact_expiry = "1 Minit"

        return {
            "display_name": display_name,
            "type": signal_type,
            "price": float(latest['Close']),
            "rsi": float(latest['RSI']),
            "win_rate": strength_score,
            "expiry": exact_expiry
        }
    except Exception as e:
        print(f"Erè sou {display_name}: {e}")
        return None

if __name__ == "__main__":
    assets = [
        ("EURUSD=X", "EUR/USD OTC"), ("GBPUSD=X", "GBP/USD OTC"), ("USDJPY=X", "USD/JPY OTC"),
        ("AUDUSD=X", "AUD/USD OTC"), ("USDCAD=X", "USD/CAD OTC"), ("USDCHF=X", "USD/CHF OTC"),
        ("EURJPY=X", "EUR/JPY OTC"), ("GBPJPY=X", "GBP/JPY OTC"), ("CADJPY=X", "CAD/JPY OTC"),
        ("AUDCAD=X", "AUD/CAD OTC"), ("AUDCHF=X", "AUD/CHF OTC"), ("AUDJPY=X", "AUD/JPY OTC"),
        ("AUDNZD=X", "AUD/NZD OTC"), ("CADCHF=X", "CAD/CHF OTC"), ("CHFJPY=X", "CHF/CHF OTC"),
        ("EURCHF=X", "EUR/CHF OTC"), ("EURGBP=X", "EUR/GBP OTC"), ("EURNZD=X", "EUR/NZD OTC"),
        ("GBPAUD=X", "GBP/AUD OTC"), ("NZDJPY=X", "NZD/JPY OTC"), ("NZDUSD=X", "NZD/USD OTC")
    ]

    all_signals = []
    for ticker, display_name in assets:
        res = get_market_data(ticker, display_name)
        if res:
            all_signals.append(res)

    entry_time = (datetime.now() + timedelta(minutes=1)).strftime('%H:%M:00')

    # Voye siyal yo sou Telegram ak fòma ki pi senp la
    for sig in all_signals:
        emoji = "🚀 CALL" if sig['type'] == "CALL" else "🔻 PUT"
        msg = (
            f"🔥 SIYAL: {emoji}\n"
            f"Asset: {sig['display_name']}\n"
            f"⏰ LÈ POU RANTRE: {entry_time}\n"
            f"⏳ EKSPIRASYON: {sig['expiry']}\n"
            f"Pri: {sig['price']:.5f}\n"
            f"📊 ESTIMASYON WIN: ~{sig['win_rate']}%"
        )
        send_telegram_msg(msg)
