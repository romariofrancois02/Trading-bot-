import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram_signal(message, signal_direction, pocket_option_url="https://pocketoption.com"):
    if not TELEGRAM_TOKEN or not CHAT_ID:
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
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Erè Telegram:", e)

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

if __name__ == "__main__":
    symbols = {
        "EURUSD-OTC": "EURUSD=X",
        "GBPUSD-OTC": "GBPUSD=X",
        "USDJPY-OTC": "USDJPY=X",
        "AUDUSD-OTC": "AUDUSD=X",
        "USDCAD-OTC": "USDCAD=X"
    }
    
    selected_asset, ticker = list(symbols.items()[0]) if isinstance(symbols, dict) else list(symbols.items())[0]

    data = yf.download(ticker, period="1d", interval="1m", progress=False)
    
    if data.empty or len(data) < 60:
        print("Erè: Pa jwenn ase done sou mache a.")
        exit(1)

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    closes = data['Close']
    opens = data['Open']

    ema_50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
    rsi_series = calculate_rsi(closes, 14)
    rsi_14 = rsi_series.iloc[-1]
    latest_price = closes.iloc[-1]

    # Tcheke momantòm dènye bouji yo pou nou pa janm ale kont gwo mouvman
    last_3_candles_red = (closes.iloc[-1] < opens.iloc[-1]) and (closes.iloc[-2] < opens.iloc[-2]) and (closes.iloc[-3] < opens.iloc[-3])
    last_3_candles_green = (closes.iloc[-1] > opens.iloc[-1]) and (closes.iloc[-2] > opens.iloc[-2]) and (closes.iloc[-3] > opens.iloc[-3])

    local_time = datetime.now(timezone.utc) - timedelta(hours=4)
    entry_time = (local_time + timedelta(minutes=1)).strftime('%H:%M:00')

    signal_type = None
    trend_desc = ""

    # Lojik ki pi strik: Si mache a ap desann ak gwo bouji wouj, li sèlman bay PUT (pa janm CALL)
    if latest_price < ema_50 and last_3_candles_red:
        signal_type = "PUT 📉"
        trend_desc = "Gwo Tonbe (Strong Downtrend) + Konfimasyon Bouji Wouj"
    elif latest_price > ema_50 and last_3_candles_green:
        signal_type = "CALL 📈"
        trend_desc = "Gwo Moute (Strong Uptrend) + Konfimasyon Bouji Vèt"
    else:
        print(f"Mache a pa klè oswa li nan yon zòn risk. Pa gen siyal voye.")
        exit(0)

    # Mesaj la vin pi pwòp, san fo pousantaj, ak tout detay teknik yo
    msg = (
        f"📌 **POCKET OPTION - CLEAN SIGNAL**\n\n"
        f"💵 **{selected_asset}**\n"
        f"💎 **M1**\n"
        f"⏰ **{entry_time}**\n"
        f"⚡ **{signal_type}**\n\n"
        f"⚙️ *Analiz:* _{trend_desc}_\n"
        f"📉 *RSI (14):* `{rsi_14:.2f}`\n"
        f"📈 *EMA (50):* `{ema_50:.5f}`"
    )

    send_telegram_signal(msg, signal_type)
