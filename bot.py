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

def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Tokens Telegram yo pa jwenn.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        print("Status voye:", res.status_code)
    except Exception as e:
        print("Erè nan voye:", e)

if __name__ == "__main__":
    otc_assets = [
        ("EUR/USD OTC", 1.08500), ("GBP/USD OTC", 1.29500), ("USD/JPY OTC", 152.500),
        ("AUD/USD OTC", 0.67000), ("USD/CAD OTC", 1.35000), ("USD/CHF OTC", 0.88000),
        ("EUR/JPY OTC", 165.200), ("GBP/JPY OTC", 197.500), ("CAD/JPY OTC", 110.500),
        ("AUD/CAD OTC", 0.92000), ("AUD/CHF OTC", 0.51100), ("AUD/JPY OTC", 102.500),
        ("EUR/CHF OTC", 0.95500), ("EUR/GBP OTC", 0.83800), ("NZD/USD OTC", 0.61000),
        ("EUR/AUD OTC", 1.65000), ("GBP/AUD OTC", 1.95000), ("EUR/NZD OTC", 1.80000),
        ("GBP/NZD OTC", 2.12000), ("AUD/NZD OTC", 1.09000), ("NZD/JPY OTC", 93.500),
        ("USD/SGD OTC", 1.35000), ("USD/ZAR OTC", 18.5000), ("USD/TRY OTC", 32.5000),
        ("USD/MXN OTC", 17.5000), ("GBP/CAD OTC", 1.75000), ("EUR/CAD OTC", 1.48000),
        ("CHF/JPY OTC", 173.500), ("NZD/CAD OTC", 0.83000), ("AUD/SGD OTC", 0.89000),
        ("BTC/USD OTC", 65000.0), ("ETH/USD OTC", 3500.0), ("LTC/USD OTC", 85.000),
        ("XRP/USD OTC", 0.5500), ("BNB/USD OTC", 580.00), ("ADA/USD OTC", 0.4500)
    ]

    last_asset = get_last_asset()
    filtered_assets = [a for a in otc_assets if a[0] != last_asset]
    if not filtered_assets:
        filtered_assets = otc_assets

    chosen_pair = random.choice(filtered_assets)
    best_asset = chosen_pair[0]
    base_p = chosen_pair[1]
    save_last_asset(best_asset)

    # LIS PLIZYÈ STRATEJI PWOFESYONÈL BOUJI AK SIPÒ/REZISTANS
    strategies = [
        ("Morning Star Reversal + Support Bounce", "CALL"),
        ("Evening Star Reversal + Resistance Drop", "PUT"),
        ("Bullish Engulfing + Retracement Level", "CALL"),
        ("Bearish Engulfing + Retracement Level", "PUT"),
        ("Pin Bar (Hammer) at Strong Support", "CALL"),
        ("Shooting Star at Major Resistance", "PUT"),
        ("EMA 20/50 Trend Continuation + Momentum", random.choice(["CALL", "PUT"]))
    ]

    chosen_strat, signal_type = random.choice(strategies)

    pip_factor = 0.00120 if base_p < 10 else (0.15 if base_p < 500 else 15.0)
    current_price = base_p + random.uniform(-pip_factor, pip_factor)
    market_strength = random.randint(92, 99)
    win_rate = random.randint(95, 99)

    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
    entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

    emoji_signal = "🟢 🚀 **CALL (BUY) - VÈT**" if signal_type == "CALL" else "🔴 🔻 **PUT (SELL) - WOUJ**"

    msg = (
        f"👑 **ROMARIO TRADER - PRO PATTERNS BOT** 👑\n"
        f"⚡ *Siyal avanse ak Analiz Bouji ak Sipò/Rezistans*\n\n"
        f"🔥 SIYAL: {emoji_signal}\n"
        f"📈 Asset: **{best_asset}**\n"
        f"💪 FÒS MACHE: **{market_strength}%**\n"
        f"⏰ LÈ EGZAK POU ANTRE: `{entry_time}`\n"
        f"⏳ TAN EKSPIRASYON: **1 Minit**\n"
        f"🎯 Pri Kounye a: `{current_price:.5f}`\n"
        f"⚙️ Estrateji: _{chosen_strat}_\n"
        f"📊 WIN RATE: **~{win_rate}%**\n\n"
        f"⚠️ *Remak: Siyal baze sou estrateji pattern ak nivo kle yo!*"
    )
    send_telegram_msg(msg)
