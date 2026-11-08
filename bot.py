def get_symbols():
    url = "https://api.otcharts.com/v1/symbols"

    response = session.get(
        url,
        params={"venue": "otc"},
        timeout=20
    )

    log(f"SYMBOL API STATUS: {response.status_code}")
    log(f"SYMBOL API RESPONSE: {response.text[:1000]}")

    response.raise_for_status()

    data = response.json()
    symbols = data.get("symbols", [])

    result = []

    for item in symbols:
        symbol = str(item.get("symbol", "")).strip()
        name = str(item.get("name", "")).strip()

        if re.fullmatch(r"[A-Z]{6}_otc", symbol):
            if item.get("open") is not False:
                result.append({
                    "symbol": symbol,
                    "name": name or symbol
                })

    return result
