
import os
import requests
from datetime import datetime, timezone

API_KEY = os.getenv("OTCHARTS_API_KEY")
BASE = "https://otcharts.com"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def get_data(path, params=None):
    response = requests.get(
        BASE + path,
        headers=HEADERS,
        params=params,
        timeout=30
    )
    print(f"\nHTTP STATUS: {response.status_code}")
    response.raise_for_status()
    return response.json()

def main():
    if not API_KEY:
        raise SystemExit("ERROR: API KEY MISSING")

    print("ROMARIO TRADER PRO 3.0")
    print("Testing Pocket Option OTC candles...")

    symbols_data = get_data(
        "/v1/symbols",
        {"venue": "otc"}
    )

    symbols = symbols_data.get("symbols", [])
    pair = next(
        (s for s in symbols if s.get("symbol") == "EURUSD_otc"),
        None
    )

    if not pair:
        raise SystemExit("EURUSD_otc not found in OTC catalogue")

    print("PAIR:", pair.get("name"))
    print("SYMBOL:", pair["symbol"])
    print("OPEN:", pair.get("open"))

    if pair.get("open") is False:
        raise SystemExit("Pair is currently closed")

    for timeframe, label in [(60, "M1"), (300, "M5")]:
        print(f"\nTesting {label} candles...")

        data = get_data(
            "/v1/candles",
            {
                "venue": "otc",
                "symbol": "EURUSD_otc",
                "tf": timeframe,
                "limit": 10
            }
        )

        candles = data.get("candles", [])
        print("CANDLES RECEIVED:", len(candles))

        if not candles:
            print("NO CANDLES RECEIVED")
            continue

        for candle in candles[-3:]:
            timestamp = datetime.fromtimestamp(
                candle["time"], timezone.utc
            )
            print(
                timestamp.isoformat(),
                "OPEN:", candle["open"],
                "HIGH:", candle["high"],
                "LOW:", candle["low"],
                "CLOSE:", candle["close"]
            )

    print("\nCANDLE DATA TEST FINISHED")

if __name__ == "__main__":
    main()
