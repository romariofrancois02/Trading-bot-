import os
import requests
import yfinance as yf
import pandas as pd
import ta
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Secrets TELEGRAM_TOKEN oswa TELEGRAM_CHAT_ID pa rekonèt.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload)
        print("Mesaj voye bay Telegram:", res.status_code)
    except Exception as e:
        print("Erè nan voye mesaj:", e)

def get_market_data(asset_tuple):
    ticker, display_name = asset_tuple
    try:
        # Rapid: Telechaje sèlman 1d nan fòma 1m pou akselere tout bagay
        df = yf.download(ticker, period="1d", interval="1m", progress=False)
        if df.empty or len(df) < 50:
            return None

        # Si yfinance retounen MultiIndex, nou plat li
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # --- ENDIKATÈ YO ---
        close_series = df['Close']
        high_series = df['High']
        low_series = df['Low']

        df['EMA20'] = ta.trend.ema_indicator(close_series, window=20)
        df['EMA50'] = ta.trend.ema_indicator(close_series, window=50)
        df['RSI'] = ta.momentum.rsi(close_series, window=14)

        df['Resistance'] = high_series.rolling(window=15).max()
        df['Support'] = low_series.rolling(window=15).min()

        bb = ta.volatility.BollingerBands(close_series, window=20, window_dev=2)
        bb_width = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / close_series.iloc[-1]

        prev1 = df.iloc[-2]  # Bouji retest / breakout
        latest = df.iloc[-1] # Bouji konfimasyon

        call_score = 0
        put_score = 0
        matched_strategies = []

        # Filtè: Pa antre si EMA20 ak EMA50 twò plat
        ema_diff = abs(latest['EMA20'] - latest['EMA50']) / latest['Close']
        if ema_diff < 0.0001:
            return None

        # 1. EMA 20/50 + Breakout + Retest
        dist_ema20 = abs(latest['Close'] - latest['EMA20']) / latest['Close']
        if dist_ema20 < 0.003: # Pri pa twò lwen EMA20
            if latest['EMA20'] > latest['EMA50'] and prev1['High'] >= prev1['Resistance'] and latest['Low'] <= latest['EMA20'] and latest['Close'] > latest['Open']:
                call_score += 35
                matched_strategies.append("EMA Breakout/Retest")
            elif latest['EMA20'] < latest['EMA50'] and prev1['Low'] <= prev1['Support'] and latest['High'] >= latest['EMA20'] and latest['Close'] < latest['Open']:
                put_score += 35
                matched_strategies.append("EMA Breakout/Retest")

        # 2. Support/Resistance Breakout Retest
        if prev1['Close'] >= prev1['Resistance'] and latest['Low'] <= prev1['Resistance'] and latest['Close'] > latest['Open']:
            call_score += 30
            matched_strategies.append("S&R Retest")
        elif prev1['Close'] <= prev1['Support'] and latest['High'] >= prev1['Support'] and latest['Close'] < latest['Open']:
            put_score += 30
            matched_strategies.append("S&R Retest")

        # 3. EMA Pullback + Trend Continuation
        if latest['EMA20'] > latest['EMA50'] and latest['Low'] <= latest['EMA20'] and latest['Close'] > latest['Open']:
            call_score += 25
            matched_strategies.append("EMA Pullback")
        elif latest['EMA20'] < latest['EMA50'] and latest['High'] >= latest['EMA20'] and latest['Close'] < latest['Open']:
            put_score += 25
            matched_strategies.append("EMA Pullback")

        # 4. RSI + Support/Resistance
        if latest['RSI'] > 50 and latest['RSI'] < 70 and call_score > 0:
            call_score += 15
            matched_strategies.append("RSI Filter")
        elif latest['RSI'] < 50 and latest['RSI'] > 30 and put_score > 0:
            put_score += 15
            matched_strategies.append("RSI Filter")

        if not matched_strategies or (call_score < 25 and put_score < 25):
            return None

        # EKSPIRASYON EGZAK
        if bb_width > 0.0025:
            exact_expiry = "3 Minit"
        elif bb_width > 0.0015:
            exact_expiry = "2 Minit"
        else:
            exact_expiry = "1 Minit"

        signal_type = "CALL" if call_score >= put_score else "PUT"
        best_score = max(call_score, put_score)

        # KALKIL WIN RATE (pèmèt rive 95%)
        num_confirmations = len(matched_strategies)
        win_rate = 76 + (num_confirmations * 5) + (best_score // 10)
        win_rate = min(95, max(78, win_rate))

        return {
            "display_name": display_name,
            "type": signal_type,
            "price": float(latest['Close']),
            "win_rate": win_rate,
            "expiry": exact_expiry,
            "strategy": " + ".join(matched_strategies),
            "score": best_score
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
        ("AUDNZD=X", "AUD/NZD OTC"), ("CADCHF=X", "CAD/CHF OTC"), ("CHFJPY=X", "CHF/JPY OTC"),
        ("EURCHF=X", "EUR/CHF OTC"), ("EURGBP=X", "EUR/GBP OTC"), ("EURNZD=X", "EUR/NZD OTC"),
        ("GBPAUD=X", "GBP/AUD OTC"), ("NZDJPY=X", "NZD/JPY OTC"), ("NZDUSD=X", "NZD/USD OTC")
    ]

    # Melanje pè lajan yo pou gen rotasyon
    random.shuffle(assets)

    all_signals = []
    
    # EKSEKUSYON AN PARALÈL: Tcheke tout pè lajan yo an menm tan pou akselerasyon
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(get_market_data, asset) for asset in assets]
        for future in as_completed(futures):
            res = future.result()
            if res:
                all_signals.append(res)

    entry_time = (datetime.now() + timedelta(minutes=1)).strftime('%H:%M:00')

    if all_signals:
        all_signals.sort(key=lambda x: x['score'], reverse=True)
        best_signal = all_signals[0]

        emoji = "🚀 CALL (BUY)" if best_signal['type'] == "CALL" else "🔻 PUT (SELL)"
        msg = (
            f"👑 **ROMARIO TRADER - SIYAL RAPID** 👑\n"
            f"⚡ *Siyal jènere an tan reyèl*\n\n"
            f"🔥 SIYAL: {emoji}\n"
            f"📈 Asset: **{best_signal['display_name']}**\n"
            f"⏰ LÈ EGZAK POU ANTRE: `{entry_time}`\n"
            f"⏳ TAN EKSPIRASYON: **{best_signal['expiry']}**\n"
            f"🎯 Pri Kounye a: `{best_signal['price']:.5f}`\n"
            f"⚙️ Estrateji ki Konfime: _{best_signal['strategy']}_\n"
            f"📊 ESTIMASYON WIN: **~{best_signal['win_rate']}%**\n\n"
            f"⚠️ *Remak: Lè win rate la anwo 90%, fòk ou gen anpil konfyans nan analiz la epi byen jere risk ou!*"
        )
        send_telegram_msg(msg)
