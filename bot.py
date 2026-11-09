
def get_candles(symbol):
    data = api_get(
        "/v1/candles",
        {
            "venue": "otc",
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    rows = extract_list(
        data,
        ("candles", "data", "results", "history")
    )

    logging.info(
        "%s raw candles received: %s",
        symbol, len(rows)
    )

    candles = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        timestamp = normalize_timestamp(
            row.get("time", row.get("timestamp"))
        )

        try:
            candle = {
                "time": timestamp,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            }

            if timestamp is None:
                continue

            if candle["high"] < candle["low"]:
                continue

            if not (
                candle["low"] <= candle["open"]
                and candle["low"] <= candle["close"]
                and candle["high"] >= candle["open"]
                and candle["high"] >= candle["close"]
            ):
                continue

            candles.append(candle)

        except (ValueError, TypeError, KeyError):
            continue

    candles.sort(key=lambda x: x["time"])

    # Remove duplicate candle timestamps.
    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = list(unique.values())

    # Pocket Option OTC timestamps use a clock
    # that runs two hours ahead of UTC.
    api_clock_now = int(time.time()) + OTC_CLOCK_OFFSET

    # Keep completed candles using the OTC clock.
    completed = [
        candle for candle in candles
        if candle["time"] + TIMEFRAME <= api_clock_now
    ]

    logging.info(
        "%s candles: raw=%s valid=%s completed=%s",
        symbol,
        len(rows),
        len(candles),
        len(completed)
    )

    if completed:
        logging.info(
            "%s latest completed candle: %s",
            symbol,
            completed[-1]["time"]
        )

    if len(completed) < 60:
        raise RuntimeError(
            f"Not enough completed candles: {len(completed)}"
        )

    return completed
