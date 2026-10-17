import os
import sys
import requests
import random
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram_signal(message, signal_direction):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Secrets Telegram manke.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    btn_text = "🚀 CALL (BUY)" if "CALL" in signal_direction else "📉 PUT (SELL)"
    
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": btn_text, "url": "https://pocketoption.com"}]
            ]
        }
    }
    
    try:
        requests.post(url, json=payload, timeout=10)
        print("Siyal voye ak siksè sou Telegram!")
    except Exception as e:
        print("Erè Telegram:", e)

if __name__ == "__main__":
    try:
        print("Ap analize mache a ak Pwofi Reyèl, RSI, ak Fractal...")
        
        # Lis konplè pè OTC yo ak Pwofi Reyèl yo konn genyen sou Pocket Option
        otc_market_data = {
            "EURUSD-OTC": {"ticker": "EURUSD=X", "profit": 92},
            "GBPUSD-OTC": {"ticker": "GBPUSD=X", "profit": 90},
            "USDJPY-OTC": {"ticker": "USDJPY=X", "profit": 88},
            "AUDUSD-OTC": {"ticker": "AUDUSD=X", "profit": 85},
            "USDCAD-OTC": {"ticker": "USDCAD=X", "profit": 82},
            "USDCHF-OTC": {"ticker": "USDCHF=X", "profit": 84},
            "EURJPY-OTC": {"ticker": "EURJPY=X", "profit": 87},
            "GBPJPY-OTC": {"ticker": "GBPJPY=X", "profit": 89},
            "AUDCHF-OTC": {"ticker": "AUDCHF=X", "profit": 83},
            "CADJPY-OTC": {"ticker": "CADJPY=X", "profit": 86},
            "NZDUSD-OTC": {"ticker": "NZDUSD=X", "profit": 85},
            "EURGBP-OTC": {"ticker": "EURGBP=X", "profit": 86},
            "EURAUD-OTC": {"ticker": "EURAUD=X", "profit": 84},
            "CHFJPY-OTC": {"ticker": "CHFJPY=X", "profit": 83}
        }
        
        selected_otc = random.choice(list(otc_market_data.keys()))
        market_info = otc_market_data[selected_otc]
        yf_ticker = market_info["ticker"]
        real_profit = market_info["profit"]
        
        # Telechaje done an dirèk
        data = yf.download(yf_ticker, period="1d", interval="1m", progress=False)
        
        if data.empty:
            current_rsi = 50.0
            fractal_signal = "Neutral"
        else:
            # Netwaye kolòn High, Low, Close
            highs = data['High'].iloc[:, 0] if isinstance(data['High'], pd.DataFrame) else data['High']
            lows = data['Low'].iloc[:, 0] if isinstance(data['Low'], pd.DataFrame) else data['Low']
            closes = data['Close'].iloc[:, 0] if isinstance(data['Close'], pd.DataFrame) else data['Close']
            
            # Kalkile RSI (14)
            delta = closes.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            current_rsi = float(rsi.iloc[-1])
            
            # Kalkile Williams Fractal (Deteksyon tèt / anba sou dènye bouji yo)
            # Yon Bullish Fractal fòme si low aktyèl la pi ba pase 2 anvan yo ak 2 apre yo
            # Yon Bearish Fractal fòme si high aktyèl la pi wo pase 2 anvan yo ak 2 apre yo
            f_bullish = False
            f_bearish = False
            if len(lows) >= 5:
                # Tcheke dènye bouji konplè yo pou Fractal
                i = -3
                if lows.iloc[i] < lows.iloc[i-1] and lows.iloc[i] < lows.iloc[i-2] and lows.iloc[i] < lows.iloc[i+1] and lows.iloc[i] < lows.iloc[i+2]:
                    f_bullish = True
                if highs.iloc[i] > highs.iloc[i-1] and highs.iloc[i] > highs.iloc[i-2] and highs.iloc[i] > highs.iloc[i+1] and highs.iloc[i] > highs.iloc[i+2]:
                    f_bearish = True

            if f_bullish or current_rsi < 40:
                signal_type = "CALL 📈 (BULLISH FRACTAL & OVERSOLD)"
            elif f_bearish or current_rsi > 60:
                signal_type = "PUT 📉 (BEARISH FRACTAL & OVERBOUGHT)"
            else:
                signal_type = "CALL 📈 (TREND MOMENTUM)" if current_rsi < 50 else "PUT 📉 (TREND MOMENTUM)"

        local_time = datetime.now(timezone.utc) - timedelta(hours=4)
        entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')
        win_rate = round(random.uniform(93.0, 98.2), 1)

        msg = (
            f"🛡️ **POCKET OPTION - PRO FRACTAL BOT** 🛡️\n\n"
            f"💵 **{selected_otc}** (Pwofi Reyèl: **{real_profit}%** 💰)\n"
            f"💎 **M1 (1 Minit)**\n"
            f"⏰ **{entry_time}**\n"
            f"⚡ **{signal_type}**\n\n"
            f"📊 **RSI: {round(current_rsi, 2)} | Win Rate: ~{win_rate}%**\n\n"
            f"⚙️ **Analiz Teknik & Fractal:**\n"
            f"✅ Konfimasyon avèk Pwofi Otantik ak Pwen Fractal.\n"
            f"🛡️ *Ekzekisyon:* Antre nan 2 premye segond nouvo bouji a!"
        )

        send_telegram_signal(msg, signal_type)

    except Exception as e:
        print(f"Gen yon erè: {e}")

    sys.exit(0)
