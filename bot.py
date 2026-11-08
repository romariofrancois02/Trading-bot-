
import os
import requests
import json

API_KEY = os.getenv("OTCHARTS_API_KEY")
BASE_URL = "https://otcharts.com"

if not API_KEY:
    print("ERROR: OTCHARTS_API_KEY is missing")
    raise SystemExit(1)

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
}

def test_api():
    print("ROMARIO TRADER PRO 3.0")
    print("Testing OTCharts connection...")

    response = requests.get(
        f"{BASE_URL}/v1/venues",
        headers=headers,
        timeout=30
    )

    print("HTTP STATUS:", response.status_code)

    try:
        data = response.json()
        print(json.dumps(data, indent=2)[:5000])
    except ValueError:
        print(response.text[:1000])

    if response.status_code == 200:
        print("API CONNECTION SUCCESS")
    else:
        print("API CONNECTION NEEDS CHECKING")

if __name__ == "__main__":
    test_api()
