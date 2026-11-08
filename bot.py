
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("ERROR: Telegram secrets missing.")
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )

    print("Telegram status:", response.status_code)
    print("Telegram response:", response.text)
    response.raise_for_status()


def main():
    print("ROMARIO TRADER PRO 3.7")
    print("TELEGRAM SIGNAL TEST")

    florida_time = datetime.now(
        ZoneInfo("America/New_York")
    ).strftime("%I:%M:%S %p")

    message = (
        "🤖 ROMARIO TRADER PRO 3.7\n\n"
        "🟢 CALL — TEST\n"
        "🔴 PUT — TEST\n"
        "⚪ WAIT — TEST\n\n"
        "💱 PAIR: EUR/USD OTC\n"
        "⏱ TIMEFRAME: M1\n"
        "⌛ EXPIRATION: 60 SECONDS\n"
        f"🕒 FLORIDA TIME: {florida_time}\n\n"
        "⚪ DEMO TEST ONLY\n"
        "Not a live trading signal."
    )

    send_telegram(message)
    print("TEST COMPLETED SUCCESSFULLY")


if __name__ == "__main__":
    main()
