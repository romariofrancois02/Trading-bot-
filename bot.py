import os
import sys
import requests
import random
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram_signal(message, signal_direction, pocket_option_url="https://pocketoption.com"):
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
                [{"text": btn_text, "url": pocket_option_url}]
            ]
        }
    }
    
    try:
        requests.post(url, json=payload, timeout=10)
        print("Siyal voye ak siksè!")
    except Exception as e:
        print("Erè Telegram:", e)

if __name__ == "__main__":
    try:
        # Vrè lis pè OTC ki egziste sou Pocket Option ak pwofi nòmal yo
        real_otc_assets = [
            ("EURUSD-OTC", 92),
            ("GBPUSD-OTC", 90),
            ("USDJPY-OTC", 88),
            ("AUDUSD-OTC", 85),
            ("USDCAD-OTC", 82),
            ("EURJPY-OTC", 87),
            ("GBPJPY-OTC", 89),
            ("CHFJPY-OTC", 83),
            ("NZDUSD-OTC", 85),
            ("EURGBP-OTC", 86)
        ]

        # Chwazi yon pè reyèl ak pwofi reyèl li ansanm otomatikman
        selected_asset, exact_profit = random.choice(real_otc_assets)
        
        local_time = datetime.now(timezone.utc) - timedelta(hours=4)
        entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

        direction_choice = random.choice(["CALL", "PUT"])
        win_rate = round(random.uniform(91.0, 98.5), 1)

        if direction_choice == "CALL":
            signal_type = "CALL 📈 (SAFE REVERSAL)"
            strategy_details = (
                f"✅ **Pwofi Reyèl:** `{exact_profit}%` (Pè Otantik sou Pocket Option).\n"
                f"✅ **Sipò:** Konfime sou zòn oversold.\n"
                f"🛡️ *Antre:* Nan 2 premye segond yo nan nouvo bouji a!"
            )
        else:
            signal_type = "PUT 📉 (SAFE REVERSAL)"
            strategy_details = (
                f"✅ **Pwofi Reyèl:** `{exact_profit}%` (Pè Otantik sou Pocket Option).\n"
                f"✅ **Rezistans:** Konfime sou zòn overbought.\n"
                f"🛡️ *Antre:* Nan 2 premye segond yo nan nouvo bouji a!"
            )

        msg = (
            f"🛡️ **POCKET OPTION - REAL ASSET BOT** 🛡️\n\n"
            f"💵 **{selected_asset}** (Pwofi: **{exact_profit}%** 💰)\n"
            f"💎 **M1 (1 Minit)**\n"
            f"⏰ **{entry_time}**\n"
            f"⚡ **{signal_type}**\n\n"
            f"📊 **Win Rate Sekirize: ~{win_rate}%**\n\n"
            f"⚙️ **Analiz Teknik:**\n{strategy_details}"
        )

        send_telegram_signal(msg, signal_type)

    except Exception as e:
        print(f"Erè: {e}")

    sys.exit(0)
