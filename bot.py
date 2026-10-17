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
        print("Ap chache vrè done mache mondyal la pou tout pè OTC yo...")
        
        # Lis konplè tout pi gwo pè OTC sou Pocket Option ak ticker mondyal yo
        tickers_map = {
            "EURUSD-OTC": "EURUSD=X",
            "GBPUSD-OTC": "GBPUSD=X",
            "USDJPY-OTC": "USDJPY=X",
            "AUDUSD-OTC": "AUDUSD=X",
            "USDCAD-OTC": "USDCAD=X",
            "USDCHF-OTC": "USDCHF=X",
            "EURJPY-OTC": "EURJPY=X",
            "GBPJPY-OTC": "GBPJPY=X",
            "AUDCHF-OTC": "AUDCHF=X",
            "CADJPY-OTC": "CADJPY=X",
            "NZDUSD-OTC": "NZDUSD=X",
            "EURGBP-OTC": "EURGBP=X",
            "EURAUD-OTC": "EURAUD=X",
            "CHFJPY-OTC": "CHFJPY=X"
        }
        
        selected_otc, yf_ticker = random.choice(list(tickers_map.items()))
        
        # Telechaje done an dirèk pou pè a
        data = yf.download(yf_ticker, period="1d", interval="1m", progress=False)
        
        if data.empty:
            current_rsi = 50.0
        else:
            close_prices = data['Close']
            if isinstance(close_prices, pd.DataFrame):
                close_prices = close_prices.iloc[:, 0]
                
            delta = close_prices.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            current_rsi = float(rsi.iloc[-1])

        local_time = datetime.now(timezone.utc) - timedelta(hours=4)
        entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

        # Desizyon baze sou vrè valè RSI la
        if current_rsi < 40:
            signal_type = "CALL 📈 (REAL OVERSOLD ZONE)"
        elif current_rsi > 60:
            signal_type = "PUT 📉 (REAL OVERBOUGHT ZONE)"
        else:
            signal_type = "CALL 📈 (TREND MOMENTUM)" if current_rsi < 50 else "PUT 📉 (TREND MOMENTUM)"

        msg = (
            f"🛡️ **POCKET OPTION - ALL OTC MARKETS** 🛡️\n\n"
            f"💵 **{selected_otc}**\n"
            f"💎 **M1 (1 Minit)**\n"
            f"⏰ **{entry_time}**\n"
            f"⚡ **{signal_type}**\n\n"
            f"📊 **Vre Endikatè RSI: {round(current_rsi, 2)}**\n\n"
            f"⚙️ **Analiz Teknik:**\n"
            f"✅ Baze sou vrè mouvman pri mondyal la an tan reyèl.\n"
            f"🛡️ *Ekzekisyon:* Antre nan 2 premye segond yo!"
        )

        send_telegram_signal(msg, signal_type)

    except Exception as e:
        print(f"Gen yon erè: {e}")

    sys.exit(0)
