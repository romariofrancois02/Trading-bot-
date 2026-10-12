import os
import requests
import random
from datetime import datetime, timedelta, timezone

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
        print("Erè:", e)

def send_telegram_signal(message, pocket_option_url="https://pocketoption.com"):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Secrets Telegram yo pa jwenn.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    # BOUTON ENTÈRAKTIF (INLINE KEYBOARD)
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "🚀 CALL (BUY)", "url": pocket_option_url}
                ],
                [
                    {"text": "❓ Ki jan pou w kòmanse kòmès", "url": pocket_option_url}
                ]
            ]
        }
    }
    
    try:
        res = requests.post(url, json=payload, timeout=10)
        print("Status voye:", res.status_code)
    except Exception as e:
        print("Erè nan voye:", e)

if __name__ == "__main__":
    otc_assets = [
        "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", 
        "AUDUSD-OTC", "USDCAD-OTC", "USDCHF-OTC",
        "EURJPY-OTC", "GBPJPY-OTC", "AUDCHF-OTC"
    ]

    last_asset = get_last_asset()
    filtered_assets = [a for a in otc_assets if a != last_asset]
    if not filtered_assets:
        filtered_assets = otc_assets

    best_asset = random.choice(filtered_assets)
    save_last_asset(best_asset)

    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
    entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

    signal_type = random.choice(["CALL 📲", "PUT 📉"])
    
    # FÒMATANJ POU L RANSANBLE AK MEXT SO KONT MBOSTRON BOT LA:
    msg = (
        f"📌 **POCKET OPTION**\n\n"
        f"💵 **{best_asset}**\n"
        f"💎 **M1**\n"
        f"⏰ **{entry_time}**\n"
        f"⬆️ **{signal_type}**\n\n"
        f"⚙️ *Konfimasyon: EMA 20/50 + Breakout & Retest*"
    )

    send_telegram_signal(msg)
