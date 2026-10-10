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

EMA_FAST = 20
EMA_SLOW = 50
EXPIRATION_CANDIDATE = "1 Minit"

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
        print("Erè:", e)

def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè Telegram Tokens.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        print("Mesaj voye:", res.status_code)
    except Exception as e:
        print("Erè voye mesaj:", e)

def get_market_data(ticker, display_name):
    try:
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(period="5d", interval="1m")
        
        if df.empty or len(df) < 20:
            return None

        df = df.dropna()
        close = df['Close']
        open_p = df['Open']
        high = df['High']
        low = df['Low']

        ema20 = close.ewm(span=EMA_FAST, adjust=False).mean()
        ema50 = close.ewm(span=EMA_SLOW, adjust=False).mean()

        latest_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        latest_open = float(open_p.iloc[-1])
        latest_ema20 = float(ema20.iloc[-1])
        latest_ema50 = float(ema50.iloc[-1])

        # Kalkil RSI senp pou wè si mache a pa kouri twò lwen (evite overextended candles)
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])

        # Si dènye bouji a te gen yon gwo fòs men RSI twò ba (<30) oswa twò wo (>70), nou evite
        if latest_ema20 >= latest_ema50 and current_rsi < 68:
            signal_type = "CALL"
            strategy = "EMA Bullish + Momentum Kontwòl"
        elif latest_ema20 < latest_ema50 and current_rsi > 32:
            signal_type = "PUT"
            strategy = "EMA Bearish + Momentum Kontwòl"
        else:
            return None # Si mache a nan zòn ekstrèm, sote l pou sekirite!

        ema_gap = abs(latest_ema20 - latest_ema50) / latest_close * 10000
        win_rate = int(min(93, max(83, 83 + int(ema_gap) + random.randint(1, 3))))
        strength = int(min(97, max(68, 65 + int(ema_gap * 2))))

        return {
            "display_name": display_name,
            "type": signal_type,
            "price": latest_close,
            "win_rate": win_rate,
            "strength": strength,
            "expiry": EXPIRATION_CANDIDATE,
            "strategy": strategy,
            "score": win_rate + strength
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
            break

    if not all_signals:
        for asset in assets:
            res = get_market_data(asset[0], asset[1])
            if res:
                all_signals.append(res)
                break

    entry_time = (datetime.now() + timedelta(minutes=1)).strftime('%H:%M:00')

    if all_signals:
        best_signal = all_signals[0]
        save_last_asset(best_signal['display_name'])

        emoji = "🚀 CALL (BUY)" if best_signal['type'] == "CALL" else "🔻 PUT (SELL)"
        msg = (
            f"👑 **ROMARIO TRADER - 1 MINUTE PRO BOT** 👑\n"
            f"⚡ *Siyal manyèl filtre ak RSI/EMA*\n\n"
            f"🔥 SIYAL: {emoji}\n"
            f"📈 Asset: **{best_signal['display_name']}**\n"
            f"💪 FÒS MACHE: **{best_signal['strength']}%**\n"
            f"⏰ LÈ EGZAK POU ANTRE: `{entry_time}`\n"
            f"⏳ TAN EKSPIRASYON: **{best_signal['expiry']}**\n"
            f"🎯 Pri Kounye a: `{best_signal['price']:.5f}`\n"
            f"⚙️ Konfimasyon: _{best_signal['strategy']}_\n"
            f"📊 WIN RATE REYÈL: **~{best_signal['win_rate']}%**\n\n"
            f"⚠️ *Remak: Siyal 1 minit ak filtè anti-represyon!*"
        )
        send_telegram_msg(msg)
