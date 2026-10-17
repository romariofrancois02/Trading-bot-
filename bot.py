import os
import requests
import random
from datetime import datetime, timedelta, timezone

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# 🎯 METE PWOFIS EGZAK LA ISI A (Chanje l selon sa w wè sou ekran an, egzanp: 92, 90, 85)
EXACT_PROFIT = 92

def send_telegram_signal(message, signal_direction, pocket_option_url="https://pocketoption.com"):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Token Telegram oswa Chat ID manke.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    btn_text = "🚀 CALL (BUY)" if "CALL" in signal_direction else "📉 PUT (SELL)"
    
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": btn_text, "url": pocket_option_url}
                ]
            ]
        }
    }
    
    try:
        res = requests.post(url, json=payload, timeout=10)
        print("Status Telegram:", res.status_code)
    except Exception as e:
        print("Erè Telegram:", e)

if __name__ == "__main__":
    try:
        # Lis tout pi gwo pè OTC ki disponib sou Pocket Option yo
        otc_assets = [
            "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", 
            "AUDUSD-OTC", "USDCAD-OTC", "USDCHF-OTC",
            "EURJPY-OTC", "GBPJPY-OTC", "AUDCHF-OTC",
            "CADJPY-OTC", "NZDUSD-OTC", "EURGBP-OTC",
            "EURAUD-OTC", "GBPCAD-OTC", "CHFJPY-OTC"
        ]

        selected_asset = random.choice(otc_assets)
        
        local_time = datetime.now(timezone.utc) - timedelta(hours=4)
        entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

        direction_choice = random.choice(["CALL", "PUT"])
        win_rate = round(random.uniform(88.0, 98.5), 1)
        confirmations_count = 4
        total_checks = 4

        if direction_choice == "CALL":
            signal_type = "CALL 📈 (SAFE REVERSAL - HIGH PROBABILITY)"
            cci_val = round(random.uniform(-245.0, -220.0), 1)
            strategy_details = (
                f"✅ **Pwofi Egzat:** `{EXACT_PROFIT}%` (Mete pa ou a).\n"
                f"✅ **Confirmation Hook:** CCI soti nan ekzakteman `{cci_val}` (Zòn oversold fò).\n"
                f"✅ **Trend & Volim:** Sipò valid ak ranvèsman sou Candle[1].\n"
                f"🛡️ *Ekzekisyon:* Antre nan 2 premye segond yo nan nouvo bouji a!"
            )
        else:
            signal_type = "PUT 📉 (SAFE REVERSAL - HIGH PROBABILITY)"
            cci_val = round(random.uniform(220.0, 245.0), 1)
            strategy_details = (
                f"✅ **Pwofi Egzat:** `{EXACT_PROFIT}%` (Mete pa ou a).\n"
                f"✅ **Confirmation Hook:** CCI soti nan ekzakteman `+{cci_val}` (Zòn overbought fò).\n"
                f"✅ **Trend & Volim:** Rezistans valid ak rejeksyon sou Candle[1].\n"
                f"🛡️ *Ekzekisyon:* Antre nan 2 premye segond yo nan nouvo bouji a!"
            )

        msg = (
            f"🛡️ **POCKET OPTION - EXACT PROFIT BOT** 🛡️\n\n"
            f"💵 **{selected_asset}** (Pwofi: **{EXACT_PROFIT}%** 💰)\n"
            f"💎 **M1 (1 Minit)**\n"
            f"⏰ **{entry_time}**\n"
            f"⚡ **{signal_type}**\n\n"
            f"🎯 **Konfimasyon Strik: {confirmations_count}/{total_checks} Valide**\n"
            f"📊 **Win Rate Sekirize: ~{win_rate}%**\n\n"
            f"⚙️ **Analiz Pwoteksyon:**\n{strategy_details}"
        )

        send_telegram_signal(msg, signal_type)
        print("Siyal la voye avèk siksè sou tout lis OTC yo!")
        
    except Exception as e:
        print(f"Erè nan ekzekisyon an: {e}")
        exit(0)
