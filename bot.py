
import os
import json
import time
import math
import logging
import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =========================================================
# ROMARIO TRADER PRO 11.9
# Bollinger Bands + RSI Reversal
# Pocket Option OTC | M1 | 60s expiration
# OTCharts Lite | Telegram alerts only
# =========================================================

VERSION = "ROMARIO TRADER PRO 11.9"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 60

CANDLE_LIMIT = 180
MIN_CANDLES = 80

MIN_PAYOUT = 92.0
MAX_LIVE_SYMBOLS = 5
MAX_HISTORY_REQUESTS_PER_RUN = 100

# Bollinger Bands
BB_PERIOD = 20
BB_STD = 1.9

# RSI
RSI_PERIOD = 7
CALL_RSI_MAX = 40
PUT_RSI_MIN = 60

# Timing
ENTRY_LEAD_SECONDS = 10
MAX_SCAN_MINUTES = 300
MAX_RECONNECTS = 3

API_TIMEOUT = 30

TIMEZONE = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "RomarioTraderPro/11.9",
    "Accept": "application/json"
})


# =========================================================
# GENERAL HELPERS
# =========================================================

def local_now():
    return datetime.now(TIMEZONE)


def log(message):
    logging.info(message)


# =========================================================
# OTCHARTS API WITH AUTOMATIC RETRIES
# =========================================================

def api_get(path, params=None, stream=False):

    url = BASE_URL + path
    attempts = 5 if not stream else 1

    for attempt in range(1, attempts + 1):

        response = None

        try:

            response = session.get(
                url,
                params=params,
                timeout=(
                    15,
                    None if stream else API_TIMEOUT
                ),
                stream=stream
            )

            if response.status_code == 429:
                raise RuntimeError(
                    "OTCharts API rate limit reached (429)."
                )

            if response.status_code in (401, 402, 403):
                raise RuntimeError(
                    f"OTCharts access error "
                    f"{response.status_code}: "
                    f"{response.text[:300]}"
                )

            if response.status_code in (
                500, 502, 503, 504
            ):

                if attempt < attempts:

                    wait = min(
                        3 * (2 ** (attempt - 1)),
                        30
                    )

                    log(
                        f"OTCharts server error "
                        f"{response.status_code}. "
                        f"Retry {attempt}/{attempts} "
                        f"in {wait}s."
                    )

                    response.close()
                    time.sleep(wait)
                    continue

                response.raise_for_status()

            response.raise_for_status()

            if stream:
                return response

            return response.json()

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError
        ) as exc:

            if attempt >= attempts:
                raise RuntimeError(
                    f"OTCharts connection failed after "
                    f"{attempts} attempts: {exc}"
                )

            wait = min(
                3 * (2 ** (attempt - 1)),
                30
            )

            log(
                f"Connection problem. "
                f"Retry {attempt}/{attempts} "
                f"in {wait}s: {exc}"
            )

            if response is not None:
                response.close()

            time.sleep(wait)

    raise RuntimeError("OTCharts request failed.")


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=15
        )

        response.raise_for_status()
        return True

    except Exception as exc:

        log(f"Telegram error: {exc}")
        return False


# =========================================================
# CHECK OTCHARTS PLAN AND OTC ACCESS
# =========================================================

def check_account():

    usage = api_get("/v1/usage")

    plan = str(
        usage.get(
            "planName",
            usage.get("plan", "Unknown")
        )
    )

    requests_data = usage.get("requests", {})
    streams_data = usage.get("streams", {})

    remaining = int(
        requests_data.get("remaining", 0)
    )

    quota = int(
        requests_data.get("quota", 0)
    )

    stream_limit = int(
        streams_data.get("limit", 0)
    )

    instruments_limit = int(
        streams_data.get("instrumentsPerStream") or 0
    )

    log(
        f"OTCharts plan={plan} | "
        f"requests remaining={remaining}/{quota} | "
        f"stream limit={stream_limit} | "
        f"instruments per stream={instruments_limit}"
    )

    if stream_limit < 1:
        raise RuntimeError(
            "No live stream available on this plan."
        )

    if instruments_limit < 1:
        raise RuntimeError(
            "No live instruments available on this plan."
        )

    venues_data = api_get("/v1/venues")
    venues = venues_data.get("venues", [])

    otc = next(
        (
            item for item in venues
            if item.get("id") == VENUE
        ),
        None
    )

    if not otc or otc.get("open") is not True:
        raise RuntimeError(
            "OTCharts does not confirm OTC access."
        )

    timeframes = otc.get("timeframes", [])

    if TIMEFRAME not in timeframes:
        raise RuntimeError(
            "M1 timeframe is not available for OTC."
        )

    if remaining < 20:
        raise RuntimeError(
            "Not enough API requests remaining."
        )

    return {
        "remaining": remaining,
        "stream_limit": stream_limit,
        "instruments_limit": instruments_limit
    }


# =========================================================
# FOREX PAIR FILTER
# =========================================================

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD",
    "AUD", "NZD", "NOK", "SEK", "DKK", "PLN",
    "CZK", "HUF", "TRY", "ZAR", "MXN", "BRL",
    "INR", "SGD", "HKD", "THB", "MYR", "IDR",
    "PHP", "KRW", "TWD", "SAR", "AED", "QAR",
    "KWD", "EGP", "NGN", "ILS", "CLP", "COP",
    "PEN", "ARS", "RUB", "UAH"
}


def is_forex_pair(symbol):

    if not isinstance(symbol, str):
        return False

    if not symbol.endswith("_otc"):
        return False

    pair = symbol[:-4]

    if len(pair) != 6 or not pair.isalpha():
        return False

    base = pair[:3].upper()
    quote = pair[3:].upper()

    return (
        base in CURRENCIES
        and quote in CURRENCIES
        and base != quote
    )


# =========================================================
# GET ALL QUALIFIED FOREX OTC PAIRS
# =========================================================

def get_eligible_symbols():

    data = api_get(
        "/v1/symbols",
        params={"venue": VENUE}
    )

    symbols = data.get("symbols", [])
    eligible = []

    for item in symbols:

        symbol = item.get("symbol")
        payout = item.get("payout")

        if not is_forex_pair(symbol):
            continue

        if item.get("isOtc") is not True:
            continue

        if item.get("open") is not True:
            continue

        try:
            payout = float(payout)
        except (TypeError, ValueError):
            continue

        if payout < MIN_PAYOUT:
            continue

        eligible.append({
            "symbol": symbol,
            "payout": payout,
            "name": item.get("name", symbol)
        })

    eligible.sort(
        key=lambda item: (
            -item["payout"],
            item["symbol"]
        )
    )

    log(
        f"Eligible Forex OTC pairs: {len(eligible)} "
        f"| Minimum payout: {MIN_PAYOUT}%"
    )

    return eligible


# =========================================================
# GET HISTORICAL CANDLES
# =========================================================

def get_candles(symbol):

    data = api_get(
        "/v1/candles",
        params={
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    candles = data.get("candles", [])
    gaps = data.get("gaps", {})

    if gaps.get("missingBars", 0) > 0:

        log(
            f"{symbol}: missing candle bars: "
            f"{gaps.get('missingBars')}"
        )

        return []

    parsed = []

    for candle in candles:

        try:

            parsed.append({
                "time": int(candle["time"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"])
            })

        except (KeyError, TypeError, ValueError):
            continue

    parsed.sort(
        key=lambda item: item["time"]
    )

    # Remove duplicate candle timestamps.
    unique = {}

    for candle in parsed:
        unique[candle["time"]] = candle

    parsed = [
        unique[key]
        for key in sorted(unique)
    ]

    if len(parsed) < MIN_CANDLES:

        log(
            f"{symbol}: insufficient candles "
            f"({len(parsed)})"
        )

        return []

    # Reject histories with missing M1 bars.
    for index in range(1, len(parsed)):

        difference = (
            parsed[index]["time"]
            - parsed[index - 1]["time"]
        )

        if difference != TIMEFRAME:

            log(
                f"{symbol}: candle time gap detected."
            )

            return []

    return parsed[-CANDLE_LIMIT:]


# =========================================================
# BOLLINGER BANDS
# =========================================================

def bollinger_bands(
    closes,
    period=20,
    std_dev=1.9
):

    if len(closes) < period:
        return None, None, None

    window = closes[-period:]

    sma = sum(window) / period

    variance = sum(
        (value - sma) ** 2
        for value in window
    ) / period

    sd = math.sqrt(variance)

    upper = sma + (std_dev * sd)
    lower = sma - (std_dev * sd)

    return upper, sma, lower


# =========================================================
# RSI
# =========================================================

def calculate_rsi(closes, period=7):

    if len(closes) <= period:
        return None

    changes = [
        closes[index] - closes[index - 1]
        for index in range(1, len(closes))
    ]

    gains = [
        max(change, 0)
        for change in changes
    ]

    losses = [
        max(-change, 0)
        for change in changes
    ]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for index in range(period, len(changes)):

        avg_gain = (
            avg_gain * (period - 1)
            + gains[index]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[index]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# =========================================================
# BOLLINGER + RSI REVERSAL STRATEGY
# =========================================================

def analyze_signal(candles):

    if len(candles) < MIN_CANDLES:
        return None

    # IMPORTANT:
    # This function receives CLOSED candles only.

    closes = [
        candle["close"]
        for candle in candles
    ]

    if len(closes) < BB_PERIOD + 1:
        return None

    upper, mid, lower = bollinger_bands(
        closes,
        BB_PERIOD,
        BB_STD
    )

    rsi_value = calculate_rsi(
        closes,
        RSI_PERIOD
    )

    if (
        upper is None
        or mid is None
        or lower is None
        or rsi_value is None
    ):
        return None

    prev = candles[-2]
    last = candles[-1]

    # Bands calculated before the latest candle.
    prev_upper, prev_mid, prev_lower = (
        bollinger_bands(
            closes[:-1],
            BB_PERIOD,
            BB_STD
        )
    )

    if prev_lower is None:
        return None

    prev_close = prev["close"]

    last_open = last["open"]
    last_close = last["close"]
    last_high = last["high"]
    last_low = last["low"]

    # -----------------------------------------------------
    # CALL: LOWER BAND REVERSAL
    # -----------------------------------------------------

    touched_lower = (
        prev_close <= prev_lower
        or last_low <= lower
    )

    bullish_candle = (
        last_close > last_open
    )

    if (
        touched_lower
        and bullish_candle
        and rsi_value <= CALL_RSI_MAX
        and last_close < mid
    ):

        return {
            "direction": "CALL",
            "score": 6,
            "rsi": round(rsi_value, 2),
            "k": 0.0,
            "d": 0.0,
            "adx": 0.0,
            "plus_di": 0.0,
            "minus_di": 0.0,
            "ema9": mid,
            "ema21": lower,
            "ema50": upper,
            "close": last_close,
            "candle_time": last["time"]
        }

    # -----------------------------------------------------
    # PUT: UPPER BAND REVERSAL
    # -----------------------------------------------------

    touched_upper = (
        prev_close >= prev_upper
        or last_high >= upper
    )

    bearish_candle = (
        last_close < last_open
    )

    if (
        touched_upper
        and bearish_candle
        and rsi_value >= PUT_RSI_MIN
        and last_close > mid
    ):

        return {
            "direction": "PUT",
            "score": 6,
            "rsi": round(rsi_value, 2),
            "k": 0.0,
            "d": 0.0,
            "adx": 0.0,
            "plus_di": 0.0,
            "minus_di": 0.0,
            "ema9": mid,
            "ema21": upper,
            "ema50": lower,
            "close": last_close,
            "candle_time": last["time"]
        }

    return None


# =========================================================
# SEND SIGNAL TO TELEGRAM
# =========================================================

def send_signal(symbol, payout, signal):

    now = local_now()

    entry = (
        now.replace(second=0, microsecond=0)
        + timedelta(minutes=1)
    )

    direction = signal["direction"]

    arrow = (
        "🟢" if direction == "CALL"
        else "🔴"
    )

    message = (
        f"🤖 {VERSION}\n\n"
        f"{arrow} {direction}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"💰 PAYOUT: {payout:.1f}%\n\n"
        f"⏰ SIGNAL: {now.strftime('%I:%M:%S %p')}\n"
        f"🎯 ENTRY: {entry.strftime('%I:%M:%S %p')}"
    )

    return send_telegram(message)


# =========================================================
# LIVE STREAM
# =========================================================

def run_live_stream(monitored, histories):

    symbols = [
        item["symbol"]
        for item in monitored
    ]

    params = {
        "venue": VENUE,
        "symbol": ",".join(symbols)
    }

    log(
        f"Opening live stream for "
        f"{len(symbols)} pairs."
    )

    response = api_get(
        "/v1/stream",
        params=params,
        stream=True
    )

    current_bars = {}
    signal_minute = {}

    connected_symbols = set()
    pending = None

    started_at = time.time()
    event_name = ""

    try:

        for raw_line in response.iter_lines(
            decode_unicode=True,
            chunk_size=1
        ):

            if (
                time.time() - started_at
                > MAX_SCAN_MINUTES * 60
            ):

                log("Maximum scan duration reached.")
                return

            if not raw_line:

                event_name = ""
                continue

            line = raw_line.strip()

            if line.startswith(":"):
                continue

            if line.startswith("event:"):

                event_name = line[6:].strip()
                continue

            if not line.startswith("data:"):
                continue

            raw_data = line[5:].strip()

            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                continue

            # ---------------------------------------------
            # STREAM CONNECTED
            # ---------------------------------------------

            if event_name == "connected":

                connected_symbols = set(
                    data.get("symbols", [])
                )

                log(
                    "Stream connected: "
                    + ", ".join(
                        sorted(connected_symbols)
                    )
                )

                if not connected_symbols:

                    log(
                        "OTCharts did not confirm "
                        "any subscribed symbols."
                    )

                    return

                continue

            # ---------------------------------------------
            # STREAM EVENTS
            # ---------------------------------------------

            if event_name == "closed":

                log(
                    f"OTCharts closed event: {data}"
                )

                continue

            if event_name == "replaced":

                log(
                    "Stream replaced by another connection."
                )

                return

            if event_name != "tick":
                continue

            symbol = data.get("symbol")

            if symbol not in connected_symbols:
                continue

            try:

                price = float(data["price"])
                feed_time = int(data["time"])

            except (KeyError, TypeError, ValueError):
                continue

            receive_time = time.time()

            # M1 candle bucket.
            bucket = (
                feed_time // 60
            ) * 60

            # ---------------------------------------------
            # INITIAL LIVE CANDLE
            # ---------------------------------------------

            if symbol not in current_bars:

                history = histories.get(
                    symbol,
                    []
                )

                # Do not reuse the currently forming candle
                # from historical data.
                history = [
                    candle
                    for candle in history
                    if candle["time"] < bucket
                ]

                histories[symbol] = history

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

            current = current_bars[symbol]

            # ---------------------------------------------
            # NEW M1 CANDLE
            # ---------------------------------------------

            if bucket > current["time"]:

                gap = (
                    bucket - current["time"]
                )

                if gap != TIMEFRAME:

                    log(
                        f"{symbol}: live feed gap detected. "
                        "Skipping pair."
                    )

                    connected_symbols.discard(symbol)
                    continue

                # Previous live candle is now closed.
                histories[symbol].append(current)

                histories[symbol] = (
                    histories[symbol][-CANDLE_LIMIT:]
                )

                # Start new forming candle.
                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

            elif bucket == current["time"]:

                current["high"] = max(
                    current["high"],
                    price
                )

                current["low"] = min(
                    current["low"],
                    price
                )

                current["close"] = price

            else:
                continue

            # ---------------------------------------------
            # RESULT ESTIMATE
            # ---------------------------------------------

            if (
                pending
                and pending["symbol"] == symbol
            ):

                if (
                    pending["entry_price"] is None
                    and receive_time >= pending["entry_time"]
                ):

                    pending["entry_price"] = price

                    log(
                        f"Entry tick captured: "
                        f"{symbol} {price}"
                    )

                if (
                    pending["entry_price"] is not None
                    and receive_time >= pending["expiry_time"]
                ):

                    entry_price = pending["entry_price"]

                    if price == entry_price:

                        result = "TIE"

                    elif pending["direction"] == "CALL":

                        result = (
                            "WIN"
                            if price > entry_price
                            else "LOSS"
                        )

                    else:

                        result = (
                            "WIN"
                            if price < entry_price
                            else "LOSS"
                        )

                    message = (
                        f"🤖 {VERSION}\n\n"
                        f"📊 OTCHARTS RESULT ESTIMATE\n"
                        f"💱 PAIR: {symbol}\n"
                        f"📍 SIGNAL: {pending['direction']}\n"
                        f"📈 RESULT: {result}\n\n"
                        f"Entry: {entry_price}\n"
                        f"Exit: {price}\n\n"
                        f"⚠️ OTCharts estimate only. "
                        f"Not a verified Pocket Option result."
                    )

                    send_telegram(message)

                    log(
                        f"{symbol}: estimated result={result}"
                    )

                    return

            # ---------------------------------------------
            # SIGNAL CHECK
            # ---------------------------------------------

            if pending is not None:
                continue

            now = local_now()

            # Send signal around 10 seconds before
            # the next local minute.
            if now.second < 50:
                continue

            minute_key = int(
                receive_time // 60
            )

            if (
                signal_minute.get(symbol)
                == minute_key
            ):
                continue

            signal_minute[symbol] = minute_key

            # IMPORTANT:
            # Analyze CLOSED candles only.
            # Do not include current_bars[symbol].
            closed_candles = histories.get(
                symbol,
                []
            )

            if len(closed_candles) < MIN_CANDLES:
                continue

            signal = analyze_signal(
                closed_candles
            )

            if not signal:
                continue

            pair_info = next(
                (
                    item
                    for item in monitored
                    if item["symbol"] == symbol
                ),
                None
            )

            if not pair_info:
                continue

            log(
                f"SIGNAL {signal['direction']} "
                f"{symbol} | "
                f"RSI={signal['rsi']} | "
                f"score={signal['score']}"
            )

            delivered = send_signal(
                symbol,
                pair_info["payout"],
                signal
            )

            if not delivered:

                log(
                    "Telegram signal delivery failed."
                )

                return

            # Next minute is the planned entry.
            entry_time = (
                int(receive_time // 60) * 60 + 60
            )

            pending = {
                "symbol": symbol,
                "direction": signal["direction"],
                "entry_time": entry_time,
                "expiry_time": entry_time + 60,
                "entry_price": None
            }

            log(
                f"Signal sent for {symbol}. "
                f"Waiting for entry and expiry estimate."
            )

    finally:

        response.close()


# =========================================================
# MAIN
# =========================================================

def main():

    log(f"{VERSION} starting...")

    if not API_KEY:
        raise RuntimeError(
            "OTCHARTS_API_KEY is missing."
        )

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Telegram secrets are missing."
        )

    # 1. Check plan and API limits.
    account = check_account()

    # 2. Find every eligible Forex OTC pair.
    eligible = get_eligible_symbols()

    if not eligible:

        log(
            "No Forex OTC pairs meet "
            "the payout requirement."
        )

        return

    if (
        len(eligible)
        > MAX_HISTORY_REQUESTS_PER_RUN
    ):

        raise RuntimeError(
            f"{len(eligible)} eligible pairs exceed "
            f"the safety limit "
            f"({MAX_HISTORY_REQUESTS_PER_RUN})."
        )

    estimated_requests = len(eligible) + 4

    if account["remaining"] < estimated_requests:

        raise RuntimeError(
            "Not enough OTCharts requests remaining "
            "to analyze all eligible pairs."
        )

    # 3. Download history once for each eligible pair.
    histories = {}
    ranked = []

    log(
        f"Analyzing history for all "
        f"{len(eligible)} eligible Forex OTC pairs."
    )

    for item in eligible:

        symbol = item["symbol"]

        try:

            candles = get_candles(symbol)

            if not candles:
                continue

            histories[symbol] = candles

            ranked.append({
                **item,
                "history_count": len(candles)
            })

            log(
                f"History ready: {symbol} | "
                f"Payout={item['payout']}% | "
                f"Candles={len(candles)}"
            )

        except Exception as exc:

            log(
                f"{symbol}: history error: {exc}"
            )

    if not ranked:

        log(
            "No pairs have sufficient clean "
            "candle history."
        )

        return

    # 4. Lite supports up to five live instruments.
    live_limit = min(
        MAX_LIVE_SYMBOLS,
        account["instruments_limit"]
    )

    monitored = ranked[:live_limit]

    log(
        f"All eligible pairs analyzed: {len(ranked)} "
        f"| Live monitored: {len(monitored)}"
    )

    log(
        "Live watchlist: "
        + ", ".join(
            item["symbol"]
            for item in monitored
        )
    )

    # 5. Open one live stream.
    reconnects = 0

    while reconnects <= MAX_RECONNECTS:

        try:

            run_live_stream(
                monitored,
                histories
            )

            break

        except requests.RequestException as exc:

            reconnects += 1

            log(
                f"Stream connection error "
                f"({reconnects}/{MAX_RECONNECTS}): "
                f"{exc}"
            )

            if reconnects > MAX_RECONNECTS:
                break

            time.sleep(5)

        except Exception as exc:

            log(
                f"Bot stopped: {exc}"
            )

            break

    log(f"{VERSION} finished.")


# =========================================================
# START BOT
# =========================================================

if __name__ == "__main__":
    main()
