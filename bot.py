
import os
import requests

KEY = os.getenv("OTCHARTS_API_KEY")
BASE = "https://otcharts.com"

if not KEY:
    print("ERROR: OTCHARTS_API_KEY missing")
    raise SystemExit(1)

headers = {"Authorization": f"Bearer {KEY}"}

def test_endpoint(path, params=None):
    response = requests.get(
        BASE + path,
        headers=headers,
        params=params,
        timeout=20
    )
    print(f"\n{path} — HTTP {response.status_code}")
    try:
        data = response.json()
        if path == "/v1/symbols" and "symbols" in data:
            print("Nombre de pè:", data.get("count"))
            for item in data["symbols"][:10]:
                print(item)
        else:
            print(data)
    except ValueError:
        print(response.text[:500])

test_endpoint("/v1/venues")
test_endpoint("/v1/symbols", {"venue": "otc"})
test_endpoint("/v1/candles", {
    "venue": "otc",
    "symbol": "EURUSD_otc",
    "tf": 60,
    "limit": 5
})
