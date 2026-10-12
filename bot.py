import asyncio
import json
import websockets
import numpy as np
from datetime import datetime, timezone, timedelta
import requests
import random

# ==========================================
# KONFIGIRASYON KONT OU (CHANJE YO AK PA W YO)
# ==========================================
SSID_KONT = "METE_SSID_OU_LA"  # Long kòd SSID ou a nan navigatè a
UID_KONT = "12345678"          # Nimewo ID kont ou sou Pocket Option
CHAT_ID = "CHATI_ID_TELEGRAM_OU" # Chat ID Telegram pou resevwa notifikasyon tou
TELEGRAM_TOKEN = "TOKEN_BOT_TELEGRAM_OU"

# Lis plizyè mache OTC pou bot la analize
OTC_ASSETS = [
    "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", 
    "AUDUSD-OTC", "USDCAD-OTC", "USDCHF-OTC",
    "EURJPY-OTC", "GBPJPY-OTC", "AUDCHF-OTC"
]

TIMEFRAME = 60  # 1 Minit an segond
ACCOUNT_TYPE = 0  # 0 pou Demo, 1 pou Real
MAX_CONSECUTIVE_LOSSES = 3

consecutive_losses = 0

def calculate_ema(prices, period=50):
    if len(prices) < period:
        return None
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    a = np.convolve(prices, weights, mode='full')[:len(prices)]
    return a[period-1]

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0:
        return 100.0
    rs = up / down
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi

async def send_telegram_notification(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[ERÈ TELEGRAM]: {e}")

async def pocket_option_bot():
    global consecutive_losses
    uri = "wss://demo-api-eu.pocketoption.com/socket.io/?EIO=3&transport=websocket"
    
    # Chak asè pral gen pwòp list pri pa l
    market_histories = {asset: [] for asset in OTC_ASSETS}

    print("[INFO] Koneksyon ap fèt ak Pocket Option sou plizyè mache...")
    async with websockets.connect(uri) as websocket:
        # Otantifikasyon sou Sèvè a
        auth_payload = f'42["auth", {{"session": "{SSID_KONT}", "isDemo": {ACCOUNT_TYPE}, "uid": {UID_KONT}, "platform": 1}}]'
        await websocket.send(auth_payload)

        while True:
            response = await websocket.recv()
            
            if response.startswith("42"):
                try:
                    data = json.loads(response[2:])
                    event_name = data[0]
                    event_data = data[1]

                    if event_name == "candles" or event_name == "update-price":
                        # Chwazi yon asè o aza nan lis la pou tcheke l an tan reyèl
                        current_asset = random.choice(OTC_ASSETS)
                        current_close_price = float(event_data.get("price", 0))
                        
                        if current_close_price > 0:
                            market_histories[current_asset].append(current_close_price)

                        prices_history = market_histories[current_asset]

                        # Nou bezwen omwen 50 bouji pou chak asè
                        if len(prices_history) >= 50:
                            ema_50 = calculate_ema(prices_history, 50)
                            rsi_14 = calculate_rsi(prices_history, 14)

                            if ema_50 and rsi_14:
                                latest_price = prices_history[-1]
                                
                                if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                                    print("[RISK] Sispann bot la: 3 pèt afile anrejistre.")
                                    await send_telegram_notification("⚠️ *BOT STOPPED*: 3 pèt afile anrejistre pou pwoteje kapital.")
                                    break

                                signal = None
                                if latest_price > ema_50 and rsi_14 < 40:
                                    signal = "CALL"
                                    trend_desc = "Anwo EMA 50 (Uptrend) + RSI Oversold Pullback"
                                elif latest_price < ema_50 and rsi_14 > 60:
                                    signal = "PUT"
                                    trend_desc = "Anba EMA 50 (Downtrend) + RSI Overbought Pullback"

                                if signal:
                                    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
                                    entry_time = local_time.strftime('%H:%M:00')
                                    
                                    msg = (
                                        f"📌 *POCKET OPTION - LIVE BOT*\n\n"
                                        f"💵 *{current_asset}*\n"
                                        f"💎 *M1*\n"
                                        f"⏰ *{entry_time}*\n"
                                        f"⚡ *{signal}*\n"
                                        f"⚙️ *Setup:* _{trend_desc}_\n"
                                        f"📉 *RSI:* `{rsi_14:.2f}`"
                                    )
                                    print(f"[SIYAL VOYE] {signal} sou {current_asset}")
                                    await send_telegram_notification(msg)

                                    order_payload = f'42["open-order", {{"asset": "{current_asset}", "amount": 1, "action": "{signal.lower()}", "time": 60, "isDemo": {ACCOUNT_TYPE}}}]'
                                    await websocket.send(order_payload)
                                    
                                    if len(prices_history) > 200:
                                          prices_history.pop(0)

                except Exception as e:
                    print(f"[ERÈ PARSE DATA]: {e}")

if __name__ == "__main__":
    asyncio.run(pocket_option_bot())
