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

        # Kalkil Stochastic Oscillator (%K ak %D)
        stoch = ta.momentum.StochasticOscillator(
            high=df['High'], low=df['Low'], close=df['Close'], window=14, smooth_window=3
        )
        df['stoch_k'] = stoch.stoch()
        df['stoch_d'] = stoch.stoch_signal()

        bb = ta.volatility.BollingerBands(df['Close'], window=20, window_dev=2)
        df['bb_high'] = bb.bollinger_hband()
        df['bb_low'] = bb.bollinger_lband()

        latest = df.iloc[-1]
        
        # Kalkil volatilité pou rekòmande tan ekspirasyon an
        bb_width = (float(latest['bb_high']) - float(latest['bb_low'])) / float(latest['Close'])
        if bb_width > 0.002:
            recommended_expiry = "2M - 3M (Segondè Volatilité)"
        else:
            recommended_expiry = "1M - 2M (Nòmal)"

        return {
            "display_name": display_name,
            "price": float(latest['Close']),
            "stoch_k": float(latest['stoch_k']),
            "stoch_d": float(latest['stoch_d']),
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
            # Kondisyon Stochastic: %K anba oswa egal a 20 pou CALL, anwo oswa egal a 80 pou PUT
            if res['stoch_k'] <= 20:
                call_candidates.append(res)
            elif res['stoch_k'] >= 80:
                put_candidates.append(res)

    entry_time = (datetime.now() + timedelta(minutes=1)).strftime('%H:%M:00')

    # Voye pi bon CALL la si genyen (kote Stochastic %K pi ba nèt la)
    if call_candidates:
        best_call = min(call_candidates, key=lambda x: x['stoch_k'])
        msg = (
            f"🔥 PI BON SIYAL OTC (Stochastic): CALL 🚀\n"
            f"Asset: {best_call['display_name']}\n"
            f"⏰ LÈ POU RANTRE: {entry_time}\n"
            f"⏳ EKSPIRASYON REKÒMANDE: {best_call['expiry']}\n"
            f"Pri: {best_call['price']:.5f}\n"
            f"Stochastic %K: {best_call['stoch_k']:.1f} (Oversold)"
        )
        send_telegram_msg(msg)

    # Voye pi bon PUT la si genyen (kote Stochastic %K pi wo nèt la)
    if put_candidates:
        best_put = max(put_candidates, key=lambda x: x['stoch_k'])
        msg = (
            f"🔥 PI BON SIYAL OTC (Stochastic): PUT 🔻\n"
            f"Asset: {best_put['display_name']}\n"
            f"⏰ LÈ POU RANTRE: {entry_time}\n"
            f"⏳ EKSPIRASYON REKÒMANDE: {best_put['expiry']}\n"
            f"Pri: {best_put['price']:.5f}\n"
            f"Stochastic %K: {best_put['stoch_k']:.1f} (Overbought)"
        )
        send_telegram_msg(msg)
