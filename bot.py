import pandas as pd
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from telethon import TelegramClient, events

# 1. TELEGRAM API CREDENTIALS (Get these from my.telegram.org)
API_ID = 1234567          # Replace with your API ID (Integer)
API_HASH = 'your_hash'    # Replace with your API Hash (String)
CHANNEL_USERNAME = '@your_data_channel' # Your GitHub/Telegram chart data feed

client = TelegramClient('pocket_option_bot_session', API_ID, API_HASH)

# Global DataFrame to hold incoming candle data history
# Expected columns from your source: ['timestamp', 'open', 'high', 'low', 'close']
historical_data = pd.DataFrame()

def analyze_market(df):
    """
    Applies the corrected 'Price Action Hook' strategy logic.
    Evaluates historical candle array data.
    """
    if len(df) < 22: # Ensure we have enough candles to compute 20-period metrics
        return None

    # Calculate Technical Indicators using 'ta' library
    indicator_bb = BollingerBands(close=df['close'], window=20, window_dev=2)
    df['bb_upper'] = indicator_bb.bollinger_hband()
    df['bb_lower'] = indicator_bb.bollinger_lband()
    
    indicator_rsi = RSIIndicator(close=df['close'], window=7)
    df['rsi'] = indicator_rsi.rsi()

    # Indexing:
    # df.iloc[-1] = Current moving live candle (Candle 0) - DO NOT TRADE
    # df.iloc[-2] = Last fully closed candle (Candle 1) -> The Confirmation Hook
    # df.iloc[-3] = The candle before last (Candle 2)    -> The Overextension
    
    candle_1 = df.iloc[-2]
    candle_2 = df.iloc[-3]

    # --- 🔴 CORRECTED LOWER SIGNAL LOGIC (PUT) ---
    # Step 1: Candle 2 closed outside or touching the Upper BB, and RSI was overbought
    overextended_up = (candle_2['close'] >= candle_2['bb_upper']) and (candle_2['rsi'] > 70)
    
    # Step 2 & 3: Candle 1 closed Bearish (Red) and the RSI hooked back down below 70
    confirmed_down = (candle_1['close'] < candle_1['open']) and (candle_1['rsi'] < 70)

    if overextended_up and confirmed_down:
        return "LOWER (PUT) - 1 Min Expiration"

    # --- 🟢 CORRECTED HIGHER SIGNAL LOGIC (CALL) ---
    # Step 1: Candle 2 closed outside or touching the Lower BB, and RSI was oversold
    overextended_down = (candle_2['close'] <= candle_2['bb_lower']) and (candle_2['rsi'] < 30)
    
    # Step 2 & 3: Candle 1 closed Bullish (Green) and the RSI hooked back up above 30
    confirmed_up = (candle_1['close'] > candle_1['open']) and (candle_1['rsi'] > 30)

    if overextended_down and confirmed_up:
        return "HIGHER (CALL) - 1 Min Expiration"

    return None

@client.on(events.NewMessage(chats=CHANNEL_USERNAME))
async def handle_new_candle_data(event):
    global historical_data
    
    # ASSUMPTION: Your Telegram data source pushes structured text 
    # every time a candle closes (e.g., "178.995, 179.010, 178.950, 178.980")
    try:
        message_text = event.message.message
        
        # Parse incoming text message into numbers (Customize this parsing logic to match your specific feed style)
        data_parts = [float(x.strip()) for x in message_text.split(',')]
        
        new_row = {
            'open': data_parts[0],
            'high': data_parts[1],
            'low': data_parts[2],
            'close': data_parts[3]
        }
        
        # Append new data and keep dataframe slim
        historical_data = pd.concat([historical_data, pd.DataFrame([new_row])], ignore_index=True)
        if len(historical_data) > 50:
            historical_data = historical_data.iloc[-50:]
            
        # Run calculation
        signal = analyze_market(historical_data)
        
        if signal:
            print(f"🚨 [BOT SIGNAL TRIGGERED]: {signal} 🚨")
            # Here you can trigger your automated browser clicker function to execute on Pocket Option
            
    except Exception as e:
        print(f"Data parsing error or incomplete candle data: {e}")

print("Bot is listening to your Telegram data stream...")
client.start()
client.run_until_disconnected()
