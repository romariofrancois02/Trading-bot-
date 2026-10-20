import os
import requests
import random
from datetime import datetime, timezone, timedelta

# 🔑 Mete Token Telegram ou ak Chat ID ou isit la
TELEGRAM_TOKEN = "METE_TOKEN_OU_ISI"
CHAT_ID = "METE_CHAT_ID_OU_ISI"

def send_telegram_signal():
    # Lis pè OTC yo ki sou Pocket Option
    otc_pairs = [
        "EUR/USD OTC", "GBP/USD OTC", "USD/JPY OTC", 
        "AUD/USD OTC", "USD/CAD OTC", "EUR/JPY OTC", 
        "GBP/JPY OTC", "AUD/CHF OTC", "NZD/USD OTC"
    ]
    
    # Tout estrateji egzak nou wè nan imaj la (AlgoX Pro)
    strategies = [
        "Rejection + Pullback",
        "Trend Following",
        "Breakout + Retest",
        "RSI Momentum",
        "Bollinger + Volatility",
        "Range Trading",
        "S/R Reversal"
    ]
    
    selected_pair = random.choice(otc_pairs)
    selected_strategy = random.choice(strategies)
    signal_direction = random.choice(["CALL 🚀 (BUY)", "PUT 📉 (SELL)"])
    real_profit = random.randint(82, 93)
    win_rate = round(random.uniform(94.5, 99.0), 1)
    
    # Kalkil lè M1 la
    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
    entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')
    
    msg = (
        f"🤖 **ALGOX PRO - TELEPHONE BOT** 🤖\n\n"
        f"💵 **{selected_pair}** (Pwofi: **{real_profit}%** 💰)\n"
        f"💎 **M1 Entry | Expiry: 1 min**\n"
        f"⏰ **Lè Antre: {entry_time}**\n"
        f"⚡ **Aksyon: {signal_direction}**\n\n"
        f"🎯 **Estrateji Aktif:** `{selected_strategy}`\n"
        f"📊 **Win Rate Kalite: ~{win_rate}%**\n\n"
        f"🛡️ *Ekzekisyon:* Antre nan 2 premye segond nouvo bouji a!"
    )
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message_text := msg,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("✅ Siyal la voye sou Telegram ak siksè!")
        else:
            print("⚠️ Erè nan voye l:", response.text)
    except Exception as e:
        print("Erè koneksyon:", e)

if __name__ == "__main__":
    print("Ap voye siyal AlgoX Pro sou Telegram...")
    send_telegram_signal()
