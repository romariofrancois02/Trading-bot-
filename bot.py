import os
import pandas as pd
from telethon import TelegramClient, events

# Sèvi ak os.environ pou w ka lye l ak GitHub Secrets san pwoblèm
try:
    API_ID = int(os.environ.get('TELEGRAM_API_ID', 1234567))
    API_HASH = os.environ.get('TELEGRAM_API_HASH', 'your_hash')
except ValueError:
    API_ID = 1234567
    API_HASH = 'your_hash'

CHANNEL_USERNAME = os.environ.get('TELEGRAM_CHANNEL', '@your_data_channel')

client = TelegramClient('pocket_option_bot_session', API_ID, API_HASH)

historical_data = pd.DataFrame()

def analyze_market(df):
    """
    Applies the corrected 'Price Action Hook' strategy logic.
    """
    try:
        if len(df) < 22:
            return None

        # Tcheke si bibliyotèk 'ta' a la anvan w itilize l
        from ta.volatility import BollingerBands
        from ta.momentum import RSIIndicator

        indicator_bb = BollingerBands(close=df['close'], window=20, window_dev=2)
        df['bb_upper'] = indicator_bb.bollinger_hband()
        df['bb_lower'] = indicator_bb.bollinger_lband()
        
        indicator_rsi = RSIIndicator(close=df['close'], window=7)
        df['rsi'] = indicator_rsi.rsi()

        candle_1 = df.iloc[-2]
        candle_2 = df.iloc[-3]

        # --- 🔴 LOWER SIGNAL LOGIC (PUT) ---
        overextended_up = (candle_2['close'] >= candle_2['bb_upper']) and (candle_2['rsi'] > 70)
        confirmed_down = (candle_1['close'] < candle_1['open']) and (candle_1['rsi'] < 70)

        if overextended_up and confirmed_down:
            return "LOWER (PUT) - 1 Min Expiration"

        # --- 🟢 HIGHER SIGNAL LOGIC (CALL) ---
        overextended_down = (candle_2['close'] <= candle_2['bb_lower']) and (candle_2['rsi'] < 30)
        confirmed_up = (candle_1['close'] > candle_1['open']) and (candle_1['rsi'] > 30)

        if overextended_down and confirmed_up:
            return "HIGHER (CALL) - 1 Min Expiration"

    except Exception as e:
        print(f"Erè nan analiz mache a: {e}")
        
    return None

@client.on(events.NewMessage(chats=CHANNEL_USERNAME))
async def handle_new_candle_data(event):
    global historical_data
    try:
        message_text = event.message.message
        data_parts = [float(x.strip()) for x in message_text.split(',')]
        
        if len(data_parts) >= 4:
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
                print(f"🚨 [BOT SIGNAL TRIGGERED]: {signal} 🚨")
                
    except Exception as e:
        print(f"Done yo pa kòrèk oswa erè nan parse mesaj la: {e}")

if __name__ == "__main__":
    print("Bot is listening to your Telegram data stream...")
    try:
        client.start()
        client.run_until_disconnected()
    except Exception as e:
        print(f"Erè nan koneksyon Telethon an: {e}")
        exit(0)  # Fòse l soti ak 0 pou l pa bay Exit Code 1 sou GitHub Actions
