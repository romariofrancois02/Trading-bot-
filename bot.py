import pandas as pd
from telethon import TelegramClient, events

# 1. TELEGRAM API CREDENTIALS (Get these from my.telegram.org)
API_ID = 1234567          # Replace with your API ID (Integer)
API_HASH = 'your_hash'    # Replace with your API Hash (String)
CHANNEL_USERNAME = '@your_data_channel' # Your GitHub/Telegram chart data feed

client = TelegramClient('pocket_option_bot_session', API_ID, API_HASH)

# Global DataFrame to hold incoming candle data history
historical_data = pd.DataFrame()

def analyze_market(df):
    """
    Pure Python calculation of Bollinger Bands and RSI to avoid library errors.
    """
    if len(df) < 22: # Ensure we have enough data for 20-period metrics
        return None

    # Calculate Bollinger Bands manually
    sma20 = df['close'].rolling(window=20).mean()
    std20 = df['close'].rolling(window=20).std()
    df['bb_upper'] = sma20 + (std20 * 2)
    df['bb_lower'] = sma20 - (std20 * 2)
    
    # Calculate RSI (7) manually
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=7).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=7).mean()
    rs = gain / (loss + 1e-10) # 1e-10 prevents division by zero error
    df['rsi'] = 100 - (100 / (1 + rs))

    # --- POCKET OPTION PRICE ACTION HOOK INDEXING ---
    # df.iloc[-1] = Live candle (Candle 0) - DO NOT TOUCH
    # df.iloc[-2] = Last fully closed candle (Candle 1) -> The Confirmation Hook
    # df.iloc[-3] = The candle before last (Candle 2)    -> The Overextension
    
    candle_1 = df.iloc[-2]
    candle_2 = df.iloc[-3]

    # --- 🔴 CORRECTED LOWER SIGNAL LOGIC (PUT) ---
    # Step 1: Candle 2 closed outside Upper BB, and RSI was overbought (>70)
    overextended_up = (candle_2['close'] >= candle_2['bb_upper']) and (candle_2['rsi'] > 70)
    # Step 2: Candle 1 confirmed the reversal by closing Bearish (Red) and RSI hooked down
    confirmed_down = (candle_1['close'] < candle_1['open']) and (candle_1['rsi'] < 70)

    if overextended_up and confirmed_down:
        return "LOWER (PUT) - 1 Min Expiration"

    # --- 🟢 CORRECTED HIGHER SIGNAL LOGIC (CALL) ---
    # Step 1: Candle 2 closed outside Lower BB, and RSI was oversold (<30)
    overextended_down = (candle_2['close'] <= candle_2['bb_lower']) and (candle_2['rsi'] < 30)
    # Step 2: Candle 1 confirmed the reversal by closing Bullish (Green) and RSI hooked up
    confirmed_up = (candle_1['close'] > candle_1['open']) and (candle_1['rsi'] > 30)

    if overextended_down and confirmed_up:
        return "HIGHER (CALL) - 1 Min Expiration"

    return None

@client.on(events.NewMessage(chats=CHANNEL_USERNAME))
async def handle_new_candle_data(event):
    global historical_data
    try:
        message_text = event.message.message
        
        # Simple parser assuming CSV string format: "Open, High, Low, Close"
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
            
    except Exception as e:
        # Prevents message format variance from crashing the bot session
        pass

print("Bot is safely running and listening to Telegram...")
client.start()
client.run_until_disconnected()
