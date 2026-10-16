import os
import pandas as pd
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

# Grab your master key and credentials securely from GitHub Secrets
API_ID = int(os.environ.get("TELEGRAM_API_ID", 1234567))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "your_hash")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING") 
CHANNEL_USERNAME = '@your_data_channel'

def analyze_market(df):
    if len(df) < 22:
        return None

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

    overextended_up = (candle_2['close'] >= candle_2['bb_upper']) and (candle_2['rsi'] > 70)
    confirmed_down = (candle_1['close'] < candle_1['open']) and (candle_1['rsi'] < 70)

    if overextended_up and confirmed_down:
        return "LOWER (PUT) - 1 Min Expiration"

    overextended_down = (candle_2['close'] <= candle_2['bb_lower']) and (candle_2['rsi'] < 30)
    confirmed_up = (candle_1['close'] > candle_1['open']) and (candle_1['rsi'] > 30)

    if overextended_down and confirmed_up:
        return "HIGHER (CALL) - 1 Min Expiration"

    return None

async def main():
    # Log in directly using the automated session string
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()
    
    historical_data = pd.DataFrame()
    
    # Pull the last 50 historical messages from the data feed channel
    async for message in client.iter_messages(CHANNEL_USERNAME, limit=50):
        try:
            if message.message:
                data_parts = [float(x.strip()) for x in message.message.split(',')]
                new_row = {'open': data_parts[0], 'high': data_parts[1], 'low': data_parts[2], 'close': data_parts[3]}
                historical_data = pd.concat([historical_data, pd.DataFrame([new_row])], ignore_index=True)
        except Exception:
            pass
            
    # Reverse so the data flows from oldest candle to newest candle
    historical_data = historical_data.iloc[::-1].reset_index(drop=True)
    
    signal = analyze_market(historical_data)
    if signal:
        print(f"🚨 [BOT SIGNAL]: {signal} 🚨")
    else:
        print("Scan complete: No valid reversal setup found.")
        
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
