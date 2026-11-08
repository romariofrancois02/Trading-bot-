
import os
import requests
from datetime import datetime, timezone

API_KEY = os.getenv("OTCHARTS_API_KEY")
BASE_URL = "https://otcharts.com"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
}

PAIR = "EURUSD_otc"
TIMEFRAMES = {
    60: "M1",
    300: "M5"
}

def get_data(path, params=None):
    response = requests.get(
        BASE_URL + path,
        headers=HEADERS,
        params=params,
        timeout=30
    )

    print(f"HTTP STATUS: {response.status_code}")
    response.raise_for_status()

    return response.json()

def check_candles(timeframe, label):
    print(f"\n===== {label} TEST =====")

    data = get_data(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": PAIR,
            "tf": timeframe,
            "limit": 10
        }
    )

    candles = data.get("candles", [])

    print("CANDLES RECEIVED:", len(candles))

    if not candles:
        print("ERROR: NO CANDLES")
        return

    now = datetime.now(timezone.utc)

    latest = datetime.fromtimestamp(
        candles[-1]["time"],
        timezone.utc
    )

    age = (now - latest).total_seconds() / 60

    print("CURRENT UTC:", now.isoformat())
    print("LATEST CANDLE UTC:", latest.isoformat())
    print("CANDLE AGE MINUTES:", round(age, 2))

    if age < 0:
        print("WARNING: CANDLE TIMESTAMP IS IN THE FUTURE")
    elif age > 10:
        print("WARNING: CANDLE DATA MAY BE STALE")
    else:
        print("CANDLE FRESHNESS CHECK: PASSED")

    print("\nLAST 3 CANDLES:")

    for candle in candles[-3:]:
        timestamp = datetime.fromtimestamp(
            candle["time"],
            timezone.utc
        )

        print(
            timestamp.isoformat(),
            "OPEN:", candle["open"],
            "HIGH:", candle["high"],
            "LOW:", candle["low"],
            "CLOSE:", candle["close"]
        )

def main():
    print("ROMARIO TRADER PRO 3.0")
    print("OTC DATA VALIDATION")
    print("====================")

    if not API_KEY:
        raise SystemExit("ERROR: API KEY MISSING")

    symbols_data = get_data(
        "/v1/symbols",
        {"venue": "otc"}
    )

    symbols = symbols_data.get("symbols", [])

    pair_data = next(
        (
            item for item in symbols
            if item.get("symbol") == PAIR
        ),
        None
    )

    if not pair_data:
        raise SystemExit("ERROR: EURUSD_otc NOT FOUND")

    print("PAIR:", pair_data.get("name"))
    print("SYMBOL:", PAIR)
    print("OPEN:", pair_data.get("open"))

    if pair_data.get("open") is False:
        raise SystemExit("ERROR: OTC PAIR CLOSED")

    for timeframe, label in TIMEFRAMES.items():
        check_candles(timeframe, label)

    print("\n====================")
    print("DATA VALIDATION FINISHED")
    print("NO TRADING SIGNALS SENT")

if __name__ == "__main__":
    main()
