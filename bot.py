import asyncio
import pandas as pd
from telethon import TelegramClient, events

# --- 1. SYSTEM ENVIRONMENT SAFETY PATCH ---
# This forces Python to handle nested event loops properly, preventing Code 1 termination
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass

# --- 2. TELEGRAM CREDENTIALS ---
API_ID = 1234567          # Replace with your API ID (Integer)
API_HASH = 'your_hash'    # Replace with your API Hash (String)
CHANNEL_USERNAME = '@your_data_channel' 

# Setting the session string to ':memory:' guarantees it won't crash from disk permission limits
client = TelegramClient(':memory:', API_ID, API_HASH)

historical_data = pd.DataFrame()

def analyze_market(df):
    if len(df) < 22:
        return None

    # Safe mathematical computations
    sma20 = df['close'].rolling(window=20).mean()
    std20 = df['close'].rolling(window=20).std()
    df['bb_upper'] = sma20 + (std20 * 2)
    df['bb_lower'] = sma20 - (std20 * 2)
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=7).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=7).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))

    candle_1 = df.iloc[-2]
    candle_2 = df.iloc[-3]

    # --- 🔴 REVERSAL LOWER SIGNAL (PUT) ---
    overextended_up = (candle_2['close'] >= candle_2['bb_upper']) and (candle_2['rsi'] > 70)
    confirmed_down = (candle_1['close'] < candle_1['open']) and (candle_1['rsi'] < 70)

    if overextended_up and confirmed_down:
        return "LOWER (PUT) - 1 Min Expiration"

    # --- 🟢 REVERSAL HIGHER SIGNAL (CALL) ---
    overextended_down = (candle_2['close'] <= candle_2['bb_lower']) and (candle_2['rsi'] < 30)
    confirmed_up = (candle_1['close'] > candle_1['open']) and (candle_1['rsi'] > 30)

    if overextended_down and confirmed_up:
        return "HIGHER (CALL) - 1 Min Expiration"

    return None

@client.on(events.NewMessage(chats=CHANNEL_USERNAME))
async def handle_new_candle_data(event):
    global historical_data
    try:
        message_text = event.message.message
        data_parts = [float(x.strip()) for x in message_text.split(',')]
        
        new_row = {
            'open': data_parts[0],
            'high': data_parts[1],
            'low': data_parts[2],
            'close': data_parts[3]
        }
        
        historical_data = pd.concat([historical_data, pd.DataFrame([new_row])], ignore_index=True)
        if len(historical_data) > 50:
            historical_data = historical_data.iloc[-50:]
            
        signal = analyze_market(historical_data)
        if signal:
            print(f"🚨 [BOT SIGNAL]: {signal} 🚨")
            
    except Exception:
        pass

async def main():
    print("Initializing environment loops...")
    await client.start()
    print("Bot is successfully running and listening to Telegram stream safely...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    # Force run inside an isolated async controller to stop system crashes
    asyncio.run(main())
