
import os
import time
import requests
from datetime import datetime, timezone, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_KEY = os.getenv("OTCHARTS_API_KEY")
BASE_URL = "https://otcharts.com"
PAIR = "EURUSD_otc"

TIMEFRAMES = {
    60: "M1",
    300: "M5"
}

session = requests.Session()

retry = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[502, 503, 504],
    allowed_methods=["GET"]
)

adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)

session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
})


def get_data(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=30
    )

    print("HTTP STATUS:", response.status_code)
    response.raise_for_status()

    return response.json()


def test_candles(timeframe, label):
    print(f"\n========== {label} ==========")

    data = get_data(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": PAIR,
            "tf": timeframe,
            "limit": 100
        }
    )

    candles = data.get("candles", [])

    print("CANDLES RECEIVED:", len(candles))

    if len(candles) < 50:
        print("NOT ENOUGH DATA FOR INDICATOR TEST")
        return False

    now = datetime.now(timezone.utc)

    latest = datetime.fromtimestamp(
        candles[-1]["time"],
        timezone.utc
    ) - timedelta(hours=2)

    age = (now - latest).total_seconds() / 60

    print("CURRENT UTC:", now.isoformat())
    print("LATEST ADJUSTED UTC:", latest.isoformat())
    print("CANDLE AGE MINUTES:", round(age, 2))

    if age < 0 or age > 10:
        print("FRESHNESS: NOT VERIFIED")
        return False

    for candle in candles:
        for field in ["open", "high", "low", "close"]:
            if field not in candle:
                print("MISSING FIELD:", field)
                return False

            try:
                float(candle[field])
            except (TypeError, ValueError):
                print("INVALID PRICE:", field)
                return False

    print("PRICE DATA: VALID")

    print("\nLAST 5 CANDLES:")

    for candle in candles[-5:]:
        candle_time = datetime.fromtimestamp(
            candle["time"],
            timezone.utc
        ) - timedelta(hours=2)

        print(
            candle_time.isoformat(),
            "O:", candle["open"],
            "H:", candle["high"],
            "L:", candle["low"],
            "C:", candle["close"]
        )

    print("DATA TEST: PASSED")
    return True


def main():
    print("ROMARIO TRADER PRO 3.0")
    print("INDICATOR DATA PREPARATION")
    print("==========================")

    if not API_KEY:
        raise SystemExit("ERROR: API KEY MISSING")

    results = []

    for timeframe, label in TIMEFRAMES.items():
        results.append(
            test_candles(timeframe, label)
        )

    print("\n==========================")

    if all(results):
        print("M1 AND M5 DATA TEST PASSED")
        print("READY FOR INDICATOR CALCULATION TEST")
    else:
        print("DATA VALIDATION FAILED")
        print("NO SIGNALS SENT")

    print("NO TRADES EXECUTED")


if __name__ == "__main__":
    main()
