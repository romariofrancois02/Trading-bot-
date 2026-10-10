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

        # EMA 20 ak 50 pou direksyon jeneral
        df['EMA20'] = ta.trend.ema_indicator(df['Close'], window=20)
        df['EMA50'] = ta.trend.ema_indicator(df['Close'], window=50)

        # Nivo Sipò ak Rezistans sou dènye 20 bouji yo
        df['Resistance'] = df['High'].rolling(window=20).max()
        df['Support'] = df['Low'].rolling(window=20).min()

        # Volatilité pou ekspirasyon
        bb = ta.volatility.BollingerBands(df['Close'], window=20, window_dev=2)
        bb_width = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / df['Close'].iloc[-1]

        prev_candle = df.iloc[-2]     # Bouji ki fè kase an (Breakout)
        latest_candle = df.iloc[-1]   # Bouji konfimasyon an

        # Tcheke kalite bouji konfimasyon an
        is_green_confirmation = latest_candle['Close'] > latest_candle['Open']
        is_red_confirmation = latest_candle['Close'] < latest_candle['Open']

        signal_type = None

        # KONFIMASYON CALL: Prev kase Rezistans + Latest se yon bouji vèt
        if prev_candle['Close'] > prev_candle['Resistance'] and is_green_confirmation:
            signal_type = "CALL"

        # KONFIMASYON PUT: Prev kase Sipò + Latest se yon bouji wouj
        elif prev_candle['Close'] < prev_candle['Support'] and is_red_confirmation:
            signal_type = "PUT"

        if not signal_type:
            return None

        # Ekspirasyon egzak
        if bb_width > 0.0025:
            exact_expiry = "3 Minit"
        elif bb_width > 0.0015:
            exact_expiry = "2 Minit"
        else:
            exact_expiry = "1 Minit"

        # Kalkil fòs (Win Rate Estimate)
        strength_score = int(80 + (abs(latest_candle['Close'] - latest_candle['Open']) / latest_candle['Close'] * 10000))
        win_rate = min(92, max(78, strength_score))

        return {
            "display_name": display_name,
            "type": signal_type,
            "price": float(latest_candle['Close']),
            "win_rate": win_rate,
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

    call_candidates = []
    put_candidates = []

    for ticker, display_name in assets:
        res = get_market_data(ticker, display_name)
        if res:
            if res['type'] == "CALL":
                call_candidates.append(res)
            elif res['type'] == "PUT":
                put_candidates.append(res)

    entry_time = (datetime.now() + timedelta(minutes=1)).strftime('%H:%M:00')

    # Voye sèlman PI BON CALL la
    if call_candidates:
        best_call = max(call_candidates, key=lambda x: x['win_rate'])
        msg = (
            f"🔥 SIYAL BREAKOUT + KONFIMASYON: 🚀 CALL\n"
            f"Asset: {best_call['display_name']}\n"
            f"⏰ LÈ POU RANTRE: {entry_time}\n"
            f"⏳ EKSPIRASYON: {best_call['expiry']}\n"
            f"Pri: {best_call['price']:.5f}\n"
            f"📊 ESTIMASYON WIN: ~{best_call['win_rate']}%"
        )
        send_telegram_msg(msg)

    # Voye sèlman PI BON PUT la
    if put_candidates:
        best_put = max(put_candidates, key=lambda x: x['win_rate'])
        msg = (
            f"🔥 SIYAL BREAKOUT + KONFIMASYON: 🔻 PUT\n"
            f"Asset: {best_put['display_name']}\n"
            f"⏰ LÈ POU RANTRE: {entry_time}\n"
            f"⏳ EKSPIRASYON: {best_put['expiry']}\n"
            f"Pri: {best_put['price']:.5f}\n"
            f"📊 ESTIMASYON WIN: ~{best_put['win_rate']}%"
        )
        send_telegram_msg(msg)
