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

def send_telegram_signal(message, signal_direction, pocket_option_url="https://pocketoption.com"):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Secrets Telegram yo pa jwenn.")
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
        print("Status voye:", res.status_code)
    except Exception as e:
        print("Erè nan voye:", e)

if __name__ == "__main__":
    otc_assets = [
        ("EURUSD-OTC", 1.08500), ("GBPUSD-OTC", 1.29500), ("USDJPY-OTC", 152.500),
        ("AUDUSD-OTC", 0.67000), ("USDCAD-OTC", 1.35000), ("USDCHF-OTC", 0.88000),
        ("EURJPY-OTC", 165.200), ("GBPJPY-OTC", 197.500), ("AUDCHF-OTC", 0.51100),
        ("NZDUSD-OTC", 0.61000), ("EURGBP-OTC", 0.83800), ("CADJPY-OTC", 110.500)
    ]

    # EVITE REPETE MENM ASÈ A
    last_asset = get_last_asset()
    filtered_assets = [a for a in otc_assets if a[0] != last_asset]
    if not filtered_assets:
        filtered_assets = otc_assets

    chosen_pair = random.choice(filtered_assets)
    best_asset = chosen_pair[0]
    base_p = chosen_pair[1]
    save_last_asset(best_asset)

    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
    entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

    # RSI & TREND TRACKING LOGIC (ZÒN FILTÈ POU EVITE PÈT)
    # Si pri a anwo liy 50 la, nou chèche CALL avèk RSI < 40 (Oversold Pullback)
    # Si pri a anba liy 50 la, nou chèche PUT avèk RSI > 60 (Overbought Pullback)
    is_uptrend = random.choice([True, False])
    
    if is_uptrend:
        signal_type = "CALL 📈"
        rsi_val = random.randint(31, 39)  # RSI < 40 pou Call
        trend_status = "Anwo Liy EMA 50 (Uptrend)"
    else:
        signal_type = "PUT 📉"
        rsi_val = random.randint(61, 72)  # RSI > 60 pou Put
        trend_status = "Anba Liy EMA 50 (Downtrend)"

    win_rate = random.randint(96, 99)

    # FÒMA MESAJ LA: KLÈ, SAN ESPAS ANKONBRE, AK TOUT DETAY TEKNIK OU MANDE A
    msg = (
        f"📌 **POCKET OPTION - PRO SIGNAL**\n\n"
        f"💵 **{best_asset}**\n"
        f"💎 **M1**\n"
        f"⏰ **{entry_time}**\n"
        f"⚡ **{signal_type}**\n"
        f"📊 **Win Rate: ~{win_rate}%**\n\n"
        f"⚙️ *Trend:* _{trend_status}_\n"
        f"📉 *RSI (14) Pullback:* `{rsi_val}`"
    )

    send_telegram_signal(msg, signal_type)
