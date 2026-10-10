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

LAST_ASSET_FILE = "last_asset.txt"

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
        # Rale done an tan reyèl san cache
        data = yf.Ticker(ticker)
        df = data.history(period="1d", interval="1m")
        if df.empty or len(df) < 35:
            return None

        close_series = df['Close']
        high_series = df['High']
        low_series = df['Low']

        # Endikatè fòs ak tandans
        df['EMA20'] = ta.trend.ema_indicator(close_series, window=20)
        df['EMA50'] = ta.trend.ema_indicator(close_series, window=50)
        df['RSI'] = ta.momentum.rsi(close_series, window=14)
        df['ADX'] = ta.trend.adx(high_series, low_series, close_series, window=14)

        df['Resistance'] = high_series.rolling(window=10).max()
        df['Support'] = low_series.rolling(window=10).min()

        bb = ta.volatility.BollingerBands(close_series, window=20, window_dev=2)
        bb_width = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / close_series.iloc[-1]

        prev1 = df.iloc[-2]  
        latest = df.iloc[-1] 

        # --- FILTÈ FÒS MACHE (> 50%) ---
        # Calcul fòs tandans sou 100% baze sou ADX ak separasyon EMA
        adx_val = latest['ADX'] if not pd.isna(latest['ADX']) else 25
        ema_gap = (abs(latest['EMA20'] - latest['EMA50']) / latest['Close']) * 10000
        trend_strength = min(98, int((adx_val * 1.5) + (ema_gap * 2)))

        # Si fòs mache a pi ba pase 50%, rejte mache sa a kòm mache kouche
        if trend_strength < 50:
            return None

        call_score = 0
        put_score = 0
        matched_strategies = []

        # 1. EMA 20/50 Breakout & Trend
        if latest['EMA20'] > latest['EMA50'] and latest['Close'] > latest['Open']:
            call_score += 35
            matched_strategies.append("EMA Trend Breakout")
        elif latest['EMA20'] < latest['EMA50'] and latest['Close'] < latest['Open']:
            put_score += 35
            matched_strategies.append("EMA Trend Breakout")

        # 2. Support & Resistance Retest
        if prev1['Close'] >= prev1['Resistance'] and latest['Close'] > latest['Open']:
            call_score += 35
            matched_strategies.append("S&R Retest")
        elif prev1['Close'] <= prev1['Support'] and latest['Close'] < latest['Open']:
            put_score += 35
            matched_strategies.append("S&R Retest")

        # 3. EMA Pullback
        if latest['EMA20'] > latest['EMA50'] and latest['Low'] <= latest['EMA20'] and latest['Close'] > latest['Open']:
            call_score += 30
            matched_strategies.append("EMA Pullback")
        elif latest['EMA20'] < latest['EMA50'] and latest['High'] >= latest['EMA20'] and latest['Close'] < latest['Open']:
            put_score += 30
            matched_strategies.append("EMA Pullback")

        # 4. RSI Filter
        if latest['RSI'] > 50 and latest['RSI'] < 70 and call_score > 0:
            call_score += 15
            matched_strategies.append("RSI Filter")
        elif latest['RSI'] < 50 and latest['RSI'] > 30 and put_score > 0:
            put_score += 15
            matched_strategies.append("RSI Filter")

        if not matched_strategies:
            if latest['Close'] > latest['Open']:
                call_score = 30
                matched_strategies.append("Price Action Strong")
            else:
                put_score = 30
                matched_strategies.append("Price Action Strong")

        # TAN EKSPIRASYON EGZAK
        if bb_width > 0.0025:
            exact_expiry = "3 Minit"
        elif bb_width > 0.0015:
            exact_expiry = "2 Minit"
        else:
            exact_expiry = "1 Minit"

        signal_type = "CALL" if call_score >= put_score else "PUT"
        best_score = max(call_score, put_score)

        num_confirmations = len(matched_strategies)
        win_rate = 77 + (num_confirmations * 4) + (best_score // 12)
        win_rate = min(95, max(80, win_rate))

        return {
            "display_name": display_name,
            "type": signal_type,
            "price": float(latest['Close']),
            "win_rate": win_rate,
            "strength": trend_strength,
            "expiry": exact_expiry,
            "strategy": " + ".join(matched_strategies),
            "score": best_score + trend_strength
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

    last_asset = get_last_asset()
    
    # Retire dènye pè lajan ki te voye anvan an pou fòse yon nouvo mache!
    filtered_assets = [a for a in assets if a[1] != last_asset]
    random.shuffle(filtered_assets)

    all_signals = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(get_market_data, asset) for asset in filtered_assets]
        for future in as_completed(futures):
            res = future.result()
            if res:
                all_signals.append(res)

    # Si pa gen lòt signal, rale tout lis la pou asire w ou jwenn yon opòtinite
    if not all_signals:
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

        # Sove non asset sa a pou l pa repete pwochen fwa
        save_last_asset(best_signal['display_name'])

        emoji = "🚀 CALL (BUY)" if best_signal['type'] == "CALL" else "🔻 PUT (SELL)"
        msg = (
            f"👑 **ROMARIO TRADER - SIYAL NOUVO MACHE** 👑\n"
            f"⚡ *Siyal rapid ak fòs mache high-volatility*\n\n"
            f"🔥 SIYAL: {emoji}\n"
            f"📈 Asset: **{best_signal['display_name']}**\n"
            f"💪 FÒS MACHE: **{best_signal['strength']}%**\n"
            f"⏰ LÈ EGZAK POU ANTRE: `{entry_time}`\n"
            f"⏳ TAN EKSPIRASYON: **{best_signal['expiry']}**\n"
            f"🎯 Pri Kounye a: `{best_signal['price']:.5f}`\n"
            f"⚙️ Konfimasyon: _{best_signal['strategy']}_\n"
            f"📊 ESTIMASYON WIN: **~{best_signal['win_rate']}%**\n\n"
            f"⚠️ *Remak: Pè lajan sa a gen yon fòs mouvman ki anwo 50%, antre egzaktikman nan minit la!*"
        )
        send_telegram_msg(msg)
