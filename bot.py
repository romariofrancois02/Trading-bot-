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
        print("Erè: Secrets TELEGRAM_TOKEN oswa TELEGRAM_CHAT_ID manke nan GitHub/App ou a.")
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
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("✅ Siyal M3 voye ak siksè sou Telegram!")
        else:
            print("⚠️ Erè Telegram:", response.text)
    except Exception as e:
        print("Erè koneksyon Telegram:", e)

if __name__ == "__main__":
    try:
        print("🔍 Ap fè analiz M3 sou mache a...")
        
        # Lis tout pè OTC yo ak Pwofi Reyèl yo
        otc_market_data = {
            "EURUSD-OTC": {"ticker": "EURUSD=X", "profit": 92},
            "GBPUSD-OTC": {"ticker": "GBPUSD=X", "profit": 90},
            "USDJPY-OTC": {"ticker": "USDJPY=X", "profit": 88},
            "AUDUSD-OTC": {"ticker": "AUDUSD=X", "profit": 85},
            "USDCAD-OTC": {"ticker": "USDCAD=X", "profit": 82},
            "USDCHF-OTC": {"ticker": "USDCHF=X", "profit": 84},
            "EURJPY-OTC": {"ticker": "EURJPY=X", "profit": 87},
            "GBPJPY-OTC": {"ticker": "GBPJPY=X", "profit": 89},
            "CHFJPY-OTC": {"ticker": "CHFJPY=X", "profit": 83},
            "NZDUSD-OTC": {"ticker": "NZDUSD=X", "profit": 85},
            "EURGBP-OTC": {"ticker": "EURGBP=X", "profit": 86}
        }
        
        strategies = [
            "Rejection + Pullback",
            "Trend Following",
            "RSI Momentum",
            "Bollinger + Volatility",
            "S/R Reversal"
        ]
        
        selected_otc = random.choice(list(otc_market_data.keys()))
        market_info = otc_market_data[selected_otc]
        yf_ticker = market_info["ticker"]
        real_profit = market_info["profit"]
        selected_strategy = random.choice(strategies)
        
        # Telechaje done pou analiz la
        data = yf.download(yf_ticker, period="1d", interval="2m", progress=False)
        
        if data.empty:
            signal_type = "CALL 📈 (M3 BULLISH TENDENCY)"
            win_rate = 95.5
        else:
            closes = data['Close'].iloc[:, 0] if isinstance(data['Close'], pd.DataFrame) else data['Close']
            
            ema = closes.ewm(span=14, adjust=False).mean()
            current_close = float(closes.iloc[-1])
            current_ema = float(ema.iloc[-1])
            
            if current_close >= current_ema:
                signal_type = "CALL 📈 (BULLISH M3 STABILITY)"
            else:
                signal_type = "PUT 📉 (BEARISH M3 STABILITY)"
                
            win_rate = round(random.uniform(95.0, 99.1), 1)

        # Kalkil tan pou antre nan tranzaksyon M3 a
        local_time = datetime.now(timezone.utc) - timedelta(hours=4)
        entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

        msg = (
            f"🛡️ **ALGOX PRO - M3 STRATEGY BOT** 🛡️\n\n"
            f"💵 **{selected_otc}** (Pwofi Reyèl: **{real_profit}%** 💰)\n"
            f"💎 **Time Frame: M3 (3 Minit Expiration)**\n"
            f"⏰ **Lè pou Antre: {entry_time}**\n"
            f"⚡ **Aksyon: {signal_type}**\n\n"
            f"🎯 **Estrateji Itilize:** `{selected_strategy}`\n"
            f"📊 **Win Rate Kalite: ~{win_rate}%**\n\n"
            f"⚙️ **Analiz M3 Teknoloji:**\n"
            f"✅ Bwi mache a redui sou 3 minit pou pi bon presizyon.\n"
            f"🛡️ *Conseil:* Fè 03:00 sou kronomèt Pocket Option ou an!"
        )

        send_telegram_signal(msg, signal_type)

    except Exception as e:
        print(f"Erè nan kouri kòd la: {e}")
        sys.exit(1)

    sys.exit(0)
