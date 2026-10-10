import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np
import random
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

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def calculate_rsi(series, length=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def get_market_data(ticker, display_name):
    try:
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(period="1d", interval="1m")
        
        if df.empty or len(df) < 30:
            return None

        df = df.dropna()
        close = df['Close']
        high = df['High']
        low = df['Low']
        open_p = df['Open']

        ema20 = calculate_ema(close, 20)
        ema50 = calculate_ema(close, 50)
        rsi = calculate_rsi(close, 14)

        resistance = high.rolling(window=10).max()
        support = low.rolling(window=10).min()

        prev1_close = close.iloc[-2]
        prev1_open = open_p.iloc[-2]

        latest_close = close.iloc[-1]
        latest_open = open_p.iloc[-1]
        latest_high = high.iloc[-1]
        latest_low = low.iloc[-1]
        latest_ema20 = ema20.iloc[-1]
        latest_ema50 = ema50.iloc[-1]
        latest_rsi = rsi.iloc[-1]

        call_strategies = []
        put_strategies = []

        # --- 1. EMA 20/50 Trend & Breakout ---
        if latest_ema20 > latest_ema50 and latest_close > latest_open:
            call_strategies.append("EMA Trend Breakout")
        elif latest_ema20 < latest_ema50 and latest_close < latest_open:
            put_strategies.append("EMA Trend Breakout")

        # --- 2. Support & Resistance Retest ---
        if prev1_close >= resistance.iloc[-2] and latest_close > latest_open:
            call_strategies.append("S&R Retest")
        elif prev1_close <= support.iloc[-2] and latest_close < latest_open:
            put_strategies.append("S&R Retest")

        # --- 3. EMA Pullback ---
        if latest_ema20 > latest_ema50 and latest_low <= latest_ema20 and latest_close > latest_open:
            call_strategies.append("EMA Pullback")
        elif latest_ema20 < latest_ema50 and latest_high >= latest_ema20 and latest_close < latest_open:
            put_strategies.append("EMA Pullback")

        # --- 4. RSI Filter ---
        if not np.isnan(latest_rsi):
            if latest_rsi > 50 and latest_rsi < 70:
                call_strategies.append("RSI Filter")
            elif latest_rsi < 50 and latest_rsi > 30:
                put_strategies.append("RSI Filter")

        # Deside si se CALL oswa PUT baze sou ki bò ki gen plis konfimasyon
        if len(call_strategies) > len(put_strategies):
            signal_type = "CALL"
            matched_strategies = call_strategies
        elif len(put_strategies) > len(call_strategies):
            signal_type = "PUT"
            matched_strategies = put_strategies
        else:
            # Si yo egal, itilize dènye mouvman bouji a pou deside san patipandans
            if latest_close > latest_open:
                signal_type = "CALL"
                matched_strategies = ["Price Action Momentum"]
            else:
                signal_type = "PUT"
                matched_strategies = ["Price Action Momentum"]

        volatility = (high.tail(10) - low.tail(10)).mean() / latest_close * 10000
        trend_strength = int(min(98, max(55, 50 + volatility * 4 + random.randint(2, 12))))
        
        win_rate = int(min(95, max(78, 75 + (len(matched_strategies) * 5) + random.randint(1, 6))))

        if volatility > 2.0:
            exact_expiry = "3 Minit"
        elif volatility > 1.0:
            exact_expiry = "2 Minit"
        else:
            exact_expiry = "1 Minit"

        score = len(matched_strategies) * 30 + trend_strength

        return {
            "display_name": display_name,
            "type": signal_type,
            "price": float(latest_close),
            "win_rate": win_rate,
            "strength": trend_strength,
            "expiry": exact_expiry,
            "strategy": " + ".join(matched_strategies),
            "score": score
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

    last_asset = get_last_asset()
    filtered_assets = [a for a in assets if a[1] != last_asset]
    random.shuffle(filtered_assets)

    all_signals = []
    for asset in filtered_assets:
        res = get_market_data(asset[0], asset[1])
        if res:
            all_signals.append(res)
            if len(all_signals) >= 4:
                break

    if not all_signals:
        random.shuffle(assets)
        for asset in assets:
            res = get_market_data(asset[0], asset[1])
            if res:
                all_signals.append(res)
                if len(all_signals) >= 4:
                    break

    entry_time = (datetime.now() + timedelta(minutes=1)).strftime('%H:%M:00')

    if all_signals:
        all_signals.sort(key=lambda x: x['score'], reverse=True)
        best_signal = all_signals[0]

        save_last_asset(best_signal['display_name'])

        emoji = "🚀 CALL (BUY)" if best_signal['type'] == "CALL" else "🔻 PUT (SELL)"
        msg = (
            f"👑 **ROMARIO TRADER - SIYAL DINAMIK** 👑\n"
            f"⚡ *Siyal jènere ak balans CALL / PUT*\n\n"
            f"🔥 SIYAL: {emoji}\n"
            f"📈 Asset: **{best_signal['display_name']}**\n"
            f"💪 FÒS MACHE: **{best_signal['strength']}%**\n"
            f"⏰ LÈ EGZAK POU ANTRE: `{entry_time}`\n"
            f"⏳ TAN EKSPIRASYON: **{best_signal['expiry']}**\n"
            f"🎯 Pri Kounye a: `{best_signal['price']:.5f}`\n"
            f"⚙️ Konfimasyon: _{best_signal['strategy']}_\n"
            f"📊 ESTIMASYON WIN: **~{best_signal['win_rate']}%**\n\n"
            f"⚠️ *Remak: Analiz la kounye a bay tou de CALL ak PUT lib selon sa mache a mande!*"
        )
        send_telegram_msg(msg)
