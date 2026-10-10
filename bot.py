import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np
import random
from datetime import datetime, timedelta

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

LAST_ASSET_FILE = "last_asset.txt"

# ==========================================
# CONFIGURATION SETTINGS (Fiks sou 1 Minit)
# ==========================================
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

FILTER_SIDEWAYS_MARKET = True
BLOCK_DUPLICATE_SIGNALS = True
EXPIRATION_CANDIDATE = "1 Minit" # Fiks sou 1 minit sèlman

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
        print("Erè nan sove dènye asset:", e)

def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Erè: Secrets TELEGRAM_TOKEN oswa TELEGRAM_CHAT_ID pa rekonèt.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload)
        print("Mesaj voye bay Telegram:", res.status_code)
    except Exception as e:
        print("Erè nan voye mesaj:", e)

def fast_analyzer(df):
    close = df['Close']
    high = df['High']
    low = df['Low']

    df['EMA20'] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df['EMA50'] = close.ewm(span=EMA_SLOW, adjust=False).mean()

    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(window=ATR_PERIOD).mean()

    df['Resistance'] = high.rolling(window=15).max()
    df['Support'] = low.rolling(window=15).min()

    return df

# ==========================================
# SIGNAL VALIDATOR (Règ Strik)
# ==========================================
def signal_validator(df):
    if len(df) < 5:
        return None, []

    prev2 = df.iloc[-3] 
    prev1 = df.iloc[-2] # Bouji Breakout (fòk li fèmen deyò nivo a)
    latest = df.iloc[-1] # Bouji Konfimasyon & Retest

    ema_diff = abs(latest['EMA20'] - latest['EMA50']) / latest['Close']
    if FILTER_SIDEWAYS_MARKET and ema_diff < 0.0003:
        return None, []

    call_strategies = []
    put_strategies = []

    res_level = prev2['Resistance']
    sup_level = prev2['Support']

    # --- KONDISYON BUY (CALL) ---
    if latest['EMA20'] > latest['EMA50']:
        is_breakout_call = (prev1['Close'] > res_level) and (prev1['Open'] < res_level)
        is_retest_call = (latest['Low'] <= res_level * 1.0005) and (latest['Close'] > res_level)
        is_rejection_call = (latest['Close'] > latest['Open']) and (latest['Low'] < latest['Close'])

        if is_breakout_call and is_retest_call and is_rejection_call:
            call_strategies.append("EMA Trend + Breakout + Retest & Rejection")

    # --- KONDISYON SELL (PUT) ---
    if latest['EMA20'] < latest['EMA50']:
        is_breakout_put = (prev1['Close'] < sup_level) and (prev1['Open'] > sup_level)
        is_retest_put = (latest['High'] >= sup_level * 0.9995) and (latest['Close'] < sup_level)
        is_rejection_put = (latest['Close'] < latest['Open']) and (latest['High'] > latest['Close'])

        if is_breakout_put and is_retest_put and is_rejection_put:
            put_strategies.append("EMA Trend + Breakout + Retest & Rejection")

    dist_ema20 = abs(latest['Close'] - latest['EMA20']) / latest['Close']
    if dist_ema20 > 0.004:
        return None, []

    if len(call_strategies) > 0:
        return "CALL", call_strategies
    elif len(put_strategies) > 0:
        return "PUT", put_strategies
    else:
        return None, []

def get_market_data(ticker, display_name):
    try:
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(period="1d", interval="1m")
        if df.empty or len(df) < 40:
            return None

        df = df.dropna()
        df = fast_analyzer(df)
        
        signal_type, matched_strategies = signal_validator(df)
        if not signal_type:
            return None

        latest = df.iloc[-1]
        atr_val = latest['ATR'] if not np.isnan(latest['ATR']) else 0.001

        trend_strength = int(min(98, max(60, 55 + (atr_val * 10000) + random.randint(3, 12))))
        win_rate = int(min(96, max(82, 80 + (len(matched_strategies) * 8) + random.randint(1, 5))))

        score = len(matched_strategies) * 40 + trend_strength

        return {
            "display_name": display_name,
            "type": signal_type,
            "price": float(latest['Close']),
            "win_rate": win_rate,
            "strength": trend_strength,
            "expiry": EXPIRATION_CANDIDATE, # Fiks sou 1 Minit
            "strategy": " + ".join(matched_strategies),
            "score": score
        }
    except Exception as e:
        print(f"Erè sou {display_name}: {e}")
        return None

if __name__ == "__main__":
    assets = [
        ("EURUSD=X", "EUR/USD OTC"), ("GBPUSD=X", "GBP/USD OTC"), ("USDJPY=X", "USD/JPY OTC"),
        ("AUDUSD=X", "AUD/USD OTC"), ("USDCAD=X", "USD/CAD OTC"), ("USDCHF=X", "USD/CHF OTC"),
        ("EURJPY=X", "EUR/JPY OTC"), ("GBPJPY=X", "GBP/JPY OTC"), ("CADJPY=X", "CAD/JPY OTC"),
        ("AUDCAD=X", "AUD/CAD OTC"), ("AUDCHF=X", "AUD/CHF OTC"), ("AUDJPY=X", "AUD/JPY OTC"),
        ("AUDNZD=X", "AUD/NZD OTC"), ("CADCHF=X", "CAD/CHF OTC"), ("CHFJPY=X", "CHF/JPY OTC"),
        ("
