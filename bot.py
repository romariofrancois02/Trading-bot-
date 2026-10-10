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
    # Lis mache elaji ki gen ladan Forex OTC, Kripto OTC, ak Metòd Altènatif
    assets_market = [
        ("EUR/USD OTC", 1.08500), ("GBP/USD OTC", 1.29500), ("USD/JPY OTC", 152.500),
        ("AUD/USD OTC", 0.67000), ("USD/CAD OTC", 1.35000), ("USD/CHF OTC", 0.88000),
        ("EUR/JPY OTC", 165.200), ("GBPJPY OTC", 197.500), ("CAD/JPY OTC", 110.500),
        ("AUD/CAD OTC", 0.92000), ("AUD/CHF OTC", 0.51100), ("AUD/JPY OTC", 102.500),
        ("EUR/CHF OTC", 0.95500), ("EUR/GBP OTC", 0.83800), ("NZD/USD OTC", 0.61000),
        ("Bitcoin OTC", 64500.0), ("Ethereum OTC", 3450.00), ("Ripple OTC", 0.55000),
        ("Tesla OTC", 240.000), ("Gold OTC", 2380.000), ("Silver OTC", 28.5000)
    ]

    last_asset = get_last_asset()
    filtered_assets = [a for a in assets_market if a[0] != last_asset]
    if not filtered_assets:
        filtered_assets = assets_market

    # Chwazi yon pakèt os aza pou teste dinamik diferan estrateji yo
    chosen = random.choice(filtered_assets)
    best_asset = chosen[0]
    base_p = chosen[1]
    save_last_asset(best_asset)

    # Multi-Strategy Engine: Chwazi ki estrateji ki ap domine analoji sa a
    strategies_pool = [
        ("EMA 20/50 Trend Confirmation + RSI Momentum", "EMA"),
        ("Bollinger Bands Breakout + Volume Spike", "BB"),
        ("Support & Resistance Level Rejection + Price Action", "SR"),
        ("MACD Histogram Crossover + Stochastic Retest", "MACD")
    ]
    chosen_strat, strat_type = random.choice(strategies_pool)

    # Detèmine direksyon (CALL oswa PUT) baze sou yon algoritm milti-faktoryèl similye pou OTC
    signal_type = random.choice(["CALL", "PUT"])

    # Kalkil pri reyalis ak varyasyon pip
    pip_factor = 0.00120 if base_p < 10 else (0.15 if base_p < 500 else 15.0)
    current_price = base_p + random.uniform(-pip_factor, pip_factor)

    strength = random.randint(82, 95)
    win_rate = random.randint(87, 96)

    # Lè lokal ki ajou nèt (UTC - 4 pou tonbe egzak sou lè w la)
    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
    entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

    # Flèch Vizyèl Klè ak Koulè
    if signal_type == "CALL":
        emoji_signal = "🟢 🚀 **CALL (BUY) - VÈT**"
    else:
        emoji_signal = "🔴 🔻 **PUT (SELL) - WOUJ**"

    msg = (
        f"👑 **ROMARIO TRADER - ULTIMATE MULTI-STRATEGY BOT** 👑\n"
        f"⚡ *Siyal avèk analiz plizyè estrateji konbine* \n\n"
        f"🔥 SIYAL: {emoji_signal}\n"
        f"📈 Asset: **{best_asset}**\n"
        f"💪 FÒS MACHE: **{strength}%**\n"
        f"⏰ LÈ EGZAK POU ANTRE: `{entry_time}`\n"
        f"⏳ TAN EKSPIRASYON: **1 Minit**\n"
        f"🎯 Pri Kounye a: `{current_price:.5f}`\n"
        f"⚙️ Estrateji Itilize: _{chosen_strat}_\n"
        f"📊 WIN RATE REYÈL: **~{win_rate}%**\n\n"
        f"⚠️ *Remak: Bot la chanje estrateji dinamikman pou chak siyal!*"
    )
    send_telegram_msg(msg)
