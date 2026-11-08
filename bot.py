
import os
import requests
from datetime import datetime, timezone, timedelta

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

    print("HTTP STATUS:", response.status_code)
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

    latest = candles[-1]["time"]

    # Interpretasyon 1: timestamp kòm UTC
    raw_time = datetime.fromtimestamp(
        latest,
        timezone.utc
    )

    # Interpretasyon 2: esè pou verifye diferans UTC+2
    adjusted_time = raw_time - timedelta(hours=2)

    raw_age = (now - raw_time).total_seconds() / 60
    adjusted_age = (now - adjusted_time).total_seconds() / 60

    print("CURRENT UTC:", now.isoformat())
    print("RAW TIMESTAMP:", latest)
    print("RAW TIME UTC:", raw_time.isoformat())
    print("RAW AGE MINUTES:", round(raw_age, 2))

    print("\nUTC+2 HYPOTHESIS TEST:")
    print("ADJUSTED TIME:", adjusted_time.isoformat())
    print("ADJUSTED AGE MINUTES:", round(adjusted_age, 2))

    if 0 <= raw_age <= 10:
        print("RAW TIME CHECK: PLAUSIBLE")
    else:
        print("RAW TIME CHECK: NOT VERIFIED")

    if 0 <= adjusted_age <= 10:
        print("UTC+2 CHECK: PLAUSIBLE")
    else:
        print("UTC+2 CHECK: NOT VERIFIED")

    print("\nLAST 3 CANDLES:")

    for candle in candles[-3:]:
        candle_time = datetime.fromtimestamp(
            candle["time"],
            timezone.utc
        )

        print(
            "TIME:", candle_time.isoformat(),
            "OPEN:", candle["open"],
            "HIGH:", candle["high"],
            "LOW:", candle["low"],
            "CLOSE:", candle["close"]
        )


def main():
    print("ROMARIO TRADER PRO 3.0")
    print("OTC TIMESTAMP VALIDATION")
    print("========================")

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

    print("\n========================")
    print("TIMESTAMP TEST FINISHED")
    print("NO SIGNALS SENT")
    print("BROKER ALIGNMENT NOT YET VERIFIED")


if __name__ == "__main__":
    main()
