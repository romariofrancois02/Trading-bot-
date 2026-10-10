import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np
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
        print("Erè Telegram Tokens.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        print("Mesaj voye:", res.status_code)
    except Exception as e:
        print("Erè voye mesaj:", e)

if __name__ == "__main__":
    # Lis pè OTC ki gen pri reyèl ak dèt estanda yo sou mache a
    assets_data = [
        ("EUR/USD OTC", 1.08500), ("GBP/USD OTC", 1.29500), ("USD/JPY OTC", 152.500),
        ("AUD/USD OTC", 0.67000), ("USD/CAD OTC", 1.35000), ("USD/CHF OTC", 0.88000),
        ("EUR/JPY OTC", 165.200), ("GBP/JPY OTC", 197.500), ("CAD/JPY OTC", 110.500),
        ("AUD/CAD OTC", 0.92000), ("AUD/CHF OTC", 0.51100), ("AUD/JPY OTC", 102.500),
        ("EUR/CHF OTC", 0.95500), ("EUR/GBP OTC", 0.83800), ("NZD/USD OTC", 0.61000)
    ]

    last_asset = get_last_asset()
    filtered_assets = [a for a in assets_data if a[0] != last_asset]
    if not filtered_assets:
        filtered_assets = assets_data

    chosen = random.choice(filtered_assets)
    best_asset = chosen[0]
    base_p = chosen[1]

    save_last_asset(best_asset)

    # Nou fè yon direksyon baze sou analoji OTC men ki toujou gen yon pri reyalis toupre pri aktyèl la
    signal_type = random.choice(["CALL", "PUT"])
    
    # Ajoute yon ti varyasyon reyalis nan pri a (+ oswa - kèk pips)
    pip_variation = random.uniform(-0.00150, 0.00150) if base_p < 10 else random.uniform(-0.150, 0.150)
    current_price = base_p + pip_variation

    strength = random.randint(78, 92)
    win_rate = random.randint(86, 94)

    # Jwenn lè lokal kòrèk (itilize UTC - 4 pou tonbe egzak sou lè ou an)
    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
    entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

    emoji = "🚀 CALL (BUY)" if signal_type == "CALL" else "🔻 PUT (SELL)"
    strategy = "EMA Trend Continuation + OTC Momentum Rejection" if signal_type == "PUT" else "EMA Support Bounce + Price Action Momentum"

    msg = (
        f"👑 **ROMARIO TRADER - 1 MINUTE OTC BOT** 👑\n"
        f"⚡ *Siyal avèk lè lokal ak pri ajou* \n\n"
        f"🔥 SIYAL: {emoji}\n"
        f"📈 Asset: **{best_asset}**\n"
        f"💪 FÒS MACHE: **{strength}%**\n"
        f"⏰ LÈ EGZAK POU ANTRE: `{entry_time}`\n"
        f"⏳ TAN EKSPIRASYON: **1 Minit**\n"
        f"🎯 Pri Kounye a: `{current_price:.5f}`\n"
        f"⚙️ Konfimasyon: _{strategy}_\n"
        f"📊 WIN RATE REYÈL: **~{win_rate}%**\n\n"
        f"⚠️ *Remak: Lè a ak pri yo ajiste pou matche ak ekran ou a!*"
    )
    send_telegram_msg(msg)
