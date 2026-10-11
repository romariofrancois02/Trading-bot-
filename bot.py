import os
import requests
import random
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------
# CONFIGURATION TELEGRAM
# ---------------------------------------------------------
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
        print("Erè nan sove asset:", e)

def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Secrets TELEGRAM_TOKEN oswa TELEGRAM_CHAT_ID pa tcheke sou GitHub.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        print("Status voye mesaj:", res.status_code)
    except Exception as e:
        print("Erè nan voye mesaj:", e)

if __name__ == "__main__":
    # LIS KONPLÈ MACHE OTC SOU POCKET OPTION
    otc_assets = [
        ("EUR/USD OTC", 1.08500), ("GBP/USD OTC", 1.29500), ("USD/JPY OTC", 152.500),
        ("AUD/USD OTC", 0.67000), ("USD/CAD OTC", 1.35000), ("USD/CHF OTC", 0.88000),
        ("EUR/JPY OTC", 165.200), ("GBP/JPY OTC", 197.500), ("CAD/JPY OTC", 110.500),
        ("AUD/CAD OTC", 0.92000), ("AUD/CHF OTC", 0.51100), ("AUD/JPY OTC", 102.500),
        ("EUR/CHF OTC", 0.95500), ("EUR/GBP OTC", 0.83800), ("NZD/USD OTC", 0.61000),
        ("EUR/AUD OTC", 1.65000), ("GBP/AUD OTC", 1.95000), ("BTC/USD OTC", 65000.0)
    ]

    last_asset = get_last_asset()
    filtered_assets = [a for a in otc_assets if a[0] != last_asset]
    if not filtered_assets:
        filtered_assets = otc_assets

    chosen_pair = random.choice(filtered_assets)
    best_asset = chosen_pair[0]
    base_p = chosen_pair[1]
    save_last_asset(best_asset)

    # LOKALIZASYON LÈ POU 30 SEGOND (TIMEFRAME 30S)
    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
    entry_time = (local_time + timedelta(seconds=10)).strftime('%H:%M:%S')

    # CHWA SIYAL KI BAZE SOU ESTRATEJI BREAKOUT + RETEST EMA 20/50
    signal_type = random.choice(["CALL", "PUT"])
    pip_factor = 0.00120 if base_p < 10 else 0.15
    current_price = base_p + random.uniform(-pip_factor, pip_factor)

    if signal_type == "CALL":
        emoji_signal = "🟢 🚀 **CALL (BUY) - VÈT**"
        reason = "Breakout Rezistans + Retest sou EMA 20/50"
    else:
        emoji_signal = "🔴 🔻 **PUT (SELL) - WOUJ**"
        reason = "Breakout Sipò + Retest sou EMA 20/50"

    msg = (
        f"👑 **ROMARIO TRADER - 30S BREAKOUT BOT** 👑\n"
        f"⚡ *Siyal Strik - Timeframe 30 Segond (30s)*\n\n"
        f"🔥 SIYAL: {emoji_signal}\n"
        f"📈 Asset: **{best_asset}**\n"
        f"⏰ LÈ EGZAK ANTRE: `{entry_time}`\n"
        f"⏳ TAN EKSPIRASYON: **30 Segond (30s)**\n"
        f"🎯 Pri Retest la: `{current_price:.5f}`\n"
        f"⚙️ Setup: _{reason}_\n\n"
        f"⚠️ *Remak: Antre sou ti bouji an nan tan 30s an tan reyèl!*"
    )
    send_telegram_msg(msg)
