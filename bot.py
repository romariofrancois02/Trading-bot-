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

        df['EMA200'] = ta.trend.ema_indicator(df['Close'], window=200)
        bb = ta.volatility.BollingerBands(df['Close'], window=20, window_dev=2)
        df['bb_high'] = bb.bollinger_hband()
        df['bb_low'] = bb.bollinger_lband()
        df['RSI'] = ta.momentum.rsi(df['Close'], window=14)

        latest = df.iloc[-1]
        
        # Kalkile volatilité pou deside sou tan ekspirasyon an (1M, 2M, oubyen 3M)
        bb_width = (float(latest['bb_high']) - float(latest['bb_low'])) / float(latest['Close'])
        if bb_width > 0.002:
            recommended_expiry = "2M - 3M (Segondè Volatilité)"
        else:
            recommended_expiry = "1M - 2M (Nòmal)"

        return {
            "display_name": display_name,
            "price": float(latest['Close']),
            "rsi": float(latest['RSI']),
            "ema200": float(latest['EMA200']),
            "bb_low": float(latest['bb_low']),
            "bb_high": float(latest['bb_high']),
            "expiry": recommended_expiry
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
            # Règleman: RSI ≤ 30 AK Pri a anba oswa toupre Bollinger Band anba a pou CALL
            if res['rsi'] <= 30 and res['price'] <= (res['bb_low'] * 1.001):
                call_candidates.append(res)
            # Règleman: RSI ≥ 60 AK Pri a anwo oswa toupre Bollinger Band anwo a pou PUT
            elif res['rsi'] >= 60 and res['price'] >= (res['bb_high'] * 0.999):
                put_candidates.append(res)

    entry_time = (datetime.now() + timedelta(minutes=1)).strftime('%H:%M:00')

    # Voye pi bon CALL la si genyen
    if call_candidates:
        best_call = min(call_candidates, key=lambda x: x['rsi'])
        msg = (
            f"🔥 PI BON SIYAL OTC: CALL 🚀\n"
            f"Asset: {best_call['display_name']}\n"
            f"⏰ LÈ POU RANTRE: {entry_time}\n"
            f"⏳ EKSPIRASYON REKÒMANDE: {best_call['expiry']}\n"
            f"Pri: {best_call['price']:.5f}\n"
            f"RSI: {best_call['rsi']:.1f} (Oversold)"
        )
        send_telegram_msg(msg)

    # Voye pi bon PUT la si genyen
    if put_candidates:
        best_put = max(put_candidates, key=lambda x: x['rsi'])
        msg = (
            f"🔥 PI BON SIYAL OTC: PUT 🔻\n"
            f"Asset: {best_put['display_name']}\n"
            f"⏰ LÈ POU RANTRE: {entry_time}\n"
            f"⏳ EKSPIRASYON REKÒMANDE: {best_put['expiry']}\n"
            f"Pri: {best_put['price']:.5f}\n"
            f"RSI: {best_put['rsi']:.1f} (Overbought)"
        )
        send_telegram_msg(msg)
