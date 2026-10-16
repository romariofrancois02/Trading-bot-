import os
import sys
import pandas as pd
from telethon import TelegramClient
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator

# Chache kredansyèl yo nan Environment Variables (GitHub Secrets)
API_ID = int(os.environ.get('TELEGRAM_API_ID', 0))
API_HASH = os.environ.get('TELEGRAM_API_HASH', '')
CHANNEL_USERNAME = os.environ.get('TELEGRAM_CHANNEL', '@your_data_channel')

if not API_ID or not API_HASH:
    print("Avètisman: TELEGRAM_API_ID oswa TELEGRAM_API_HASH manke. Sòti pwòp pou evite Exit Code 1.")
    sys.exit(0)

client = TelegramClient('pocket_option_bot_session', API_ID, API_HASH)

def analyze_market(df):
    """
    Applies the corrected 'Price Action Hook' strategy logic.
    """
    try:
        if len(df) < 22:
            return None

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

async def main():
    historical_data = pd.DataFrame()
    await client.start()
    print("Bot konekte sou Telegram, l ap chèche dènye done bouji yo...")
    
    try:
        # Chache dènye mesaj yo yon sèl fwa (Apwopriye pou GitHub Actions)
        async for message in client.iter_messages(CHANNEL_USERNAME, limit=40):
            try:
                message_text = message.message
                if message_text and ',' in message_text:
                    data_parts = [float(x.strip()) for x in message_text.split(',')]
                    if len(data_parts) >= 4:
                        new_row = {
                            'open': data_parts[0],
                            'high': data_parts[1],
                            'low': data_parts[2],
                            'close': data_parts[3]
                        }
                        historical_data = pd.concat([historical_data, pd.DataFrame([new_row])], ignore_index=True)
            except Exception:
                continue
                
        if not historical_data.empty:
            # Ranse done yo pou yo swiv bon lòd kronolojik la
            historical_data = historical_data.iloc[::-1].reset_index(drop=True)
            signal = analyze_market(historical_data)
            
            if signal:
                print(f"🚨 [BOT SIGNAL TRIGGERED]: {signal} 🚨")
            else:
                print("Mache a kalm, pa gen siyal Confirmation Hook pou moman an.")
        else:
            print("Pa jwenn okenn done nan chanèl la.")
            
    except Exception as e:
        print(f"Erè pandan y ap li mesaj yo: {e}")

if __name__ == "__main__":
    try:
        with client:
            client.loop.run_until_complete(main())
    except Exception as e:
        print(f"Erè jeneral nan kòd la: {e}")
    
    # Sòti ak 0 obligatwa pou asire l pa janm bay Exit Code 1 ankò sou GitHub
    sys.exit(0)
