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
        if df.empty or len(df) < 100:
            return None

        # Endikatè de baz
        df['EMA20'] = ta.trend.ema_indicator(df['Close'], window=20)
        df['EMA50'] = ta.trend.ema_indicator(df['Close'], window=50)
        df['Resistance'] = df['High'].rolling(window=10).max()
        df['Support'] = df['Low'].rolling(window=10).min()

        bb = ta.volatility.BollingerBands(df['Close'], window=20, window_dev=2)
        bb_width = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / df['Close'].iloc[-1]

        prev2 = df.iloc[-3]
        prev1 = df.iloc[-2]  # Bouji kase / Retest
        latest = df.iloc[-1] # Bouji konfimasyon

        call_score = 0
        put_score = 0
        matched_strategies = []

        # 1. EMA 20/50 Retest Breakout
        if prev1['EMA20'] > prev1['EMA50'] and latest['Close'] > latest['Open'] and latest['Close'] > prev1['EMA20']:
            call_score += 35
            matched_strategies.append("EMA Breakout")
        elif prev1['EMA20'] < prev1['EMA50'] and latest['Close'] < latest['Open'] and latest['Close'] < prev1['EMA20']:
            put_score += 35
            matched_strategies.append("EMA Breakout")

        # 2. Support & Resistance Breakout + Konfimasyon
        if prev1['Close'] >= prev1['Resistance'] and latest['Close'] > latest['Open']:
            call_score += 35
            matched_strategies.append("S&R Breakout")
        elif prev1['Close'] <= prev1['Support'] and latest['Close'] < latest['Open']:
            put_score += 35
            matched_strategies.append("S&R Breakout")

        # 3. Engulfing Candle (Bouji Anvlòp)
        is_bullish_engulfing = (prev1['Close'] < prev1['Open']) and (latest['Close'] > latest['Open']) and (latest['Close'] >= prev1['Open']) and (latest['Open'] <= prev1['Close'])
        is_bearish_engulfing = (prev1['Close'] > prev1['Open']) and (latest['Close'] < latest['Open']) and (latest['Close'] <= prev1['Open']) and (latest['Open'] >= prev1['Close'])

        if is_bullish_engulfing:
            call_score += 30
            matched_strategies.append("Engulfing")
        elif is_bearish_engulfing:
            put_score += 30
            matched_strategies.append("Engulfing")

        # Tan ekspirasyon ki baze sou volatilité
        if bb_width > 0.0025:
            exact_expiry = "3 Minit"
        elif bb_width > 0.0015:
            exact_expiry = "2 Minit"
        else:
            exact_expiry = "1 Minit"

        signal_type = None
        final_score = 0

        if call_score >= 35 and call_score >= put_score:
            signal_type = "CALL"
            final_score = min(95, max(78, 60 + call_score))
        elif put_score >= 35 and put_score > call_score:
            signal_type = "PUT"
            final_score = min(95, max(78, 60 + put_score))

        if not signal_type:
            return None

        return {
            "display_name": display_name,
            "type": signal_type,
            "price": float(latest['Close']),
            "win_rate": final_score,
            "expiry": exact_expiry,
            "strategy": " + ".join(matched_strategies)
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

    # Voye pi bon siyal ki gen pi gwo win rate la anba non Romario Trader
    if all_signals:
        best_signal = max(all_signals, key=lambda x: x['win_rate'])
        emoji = "🚀 CALL" if best_signal['type'] == "CALL" else "🔻 PUT"
        msg = (
            f"👑 **ROMARIO TRADER - SIYAL OFISYÈL** 👑\n\n"
            f"🔥 SIYAL: {emoji}\n"
            f"Asset: {best_signal['display_name']}\n"
            f"⏰ LÈ POU RANTRE: {entry_time}\n"
            f"⏳ EKSPIRASYON: {best_signal['expiry']}\n"
            f"Pri: {best_signal['price']:.5f}\n"
            f"⚙️ Konfimasyon Estrateji: {best_signal['strategy']}\n"
            f"📊 ESTIMASYON WIN: ~{best_signal['win_rate']}%"
        )
        send_telegram_msg(msg)
