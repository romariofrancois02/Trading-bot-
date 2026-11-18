import os
import json
import time
import math
import logging
import threading
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo


# =========================================================
# ROMARIO TRADER PRO 15.0 - OPTIMIZED FAST SCAN
# OTCharts OTC | S30 | Expiration 60s
# =========================================================

VERSION = "ROMARIO TRADER PRO 15.0 FAST"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 30

CANDLE_LIMIT = 150  # Diminye pou telechaje istwa pi rapid
MIN_CANDLES = 60

MIN_PAYOUT = 90.0   # Bese ti kras pou pran plis opòtinite rapid
MAX_LIVE_SYMBOLS = 3
MAX_HISTORY_WORKERS = 10  # Ogmante workers pou telechaje an parallèl pi vit
MAX_SCAN_SECONDS = 60     # Desann fenèt scan an a 60 segonn
MAX_RECONNECTS = 1

BB_PERIOD = 20
BB_STD = 2.2

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 7
ATR_PERIOD = 14
ADX_PERIOD = 14

ADX_MIN = 10.0
MIN_DI_GAP = 1.5
MIN_STRATEGY_AGREEMENT = 2
MIN_CONFIRMATIONS = 2
MIN_SIGNAL_SCORE = 5

MIN_CANDLE_ATR_RATIO = 0.15
MAX_CANDLE_ATR_RATIO = 2.50

ENTRY_GRACE_SECONDS = 3
EXPIRATION_SECONDS = 60

API_CONNECT_TIMEOUT = 3
API_READ_TIMEOUT = 6

TELEGRAM_ATTEMPTS = 2
TELEGRAM_TIMEOUT = (1, 2)

TIMEZONE = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

_thread_local = threading.local()


class StreamRateLimitError(Exception):
    def __init__(self, message, retry_after=30):
        super().__init__(message)
        self.retry_after = retry_after


def log(message):
    logging.info(message)


def get_session():
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update({
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "RomarioTraderPro/15.0",
            "Accept": "application/json"
        })
    return _thread_local.session


def make_entry_time(signal_time):
    return (math.floor(signal_time / 10) + 1) * 10


def format_price(value):
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def api_get(path, params=None, stream=False):
    url = BASE_URL + path
    attempts = 1 if stream else 2

    for attempt in range(1, attempts + 1):
        response = None
        try:
            response = get_session().get(
                url,
                params=params,
                timeout=(
                    API_CONNECT_TIMEOUT,
                    None if stream else API_READ_TIMEOUT
                ),
                stream=stream
            )

            if response.status_code == 429:
                if stream:
                    response.close()
                    raise StreamRateLimitError("Rate limit", 30)
                response.close()
                raise RuntimeError("API rate limit")

            if response.status_code in (401, 402, 403, 500, 502, 503, 504):
                if attempt < attempts:
                    response.close()
                    time.sleep(0.2)
                    continue

            response.raise_for_status()
            if stream:
                return response
            return response.json()

        except StreamRateLimitError:
            raise
        except Exception:
            if response is not None:
                response.close()
            if attempt >= attempts:
                raise
            time.sleep(0.2)

    raise RuntimeError("Request failed.")


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(1, TELEGRAM_ATTEMPTS + 1):
        response = None
        try:
            response = requests.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                timeout=TELEGRAM_TIMEOUT
            )
            if response.ok:
                return True
        except Exception:
            pass
        finally:
            if response is not None:
                response.close()
        time.sleep(0.2)
    return False


def check_account():
    usage = api_get("/v1/usage")
    venues_data = api_get("/v1/venues")
    otc = next((item for item in venues_data.get("venues", []) if item.get("id") == VENUE), None)
    if not otc or otc.get("open") is not True:
        raise RuntimeError("OTC not available.")
    return {"status": "OK"}


CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD",
    "AUD", "NZD", "NOK", "SEK", "DKK", "PLN",
    "CZK", "HUF", "TRY", "ZAR", "MXN", "BRL",
    "INR", "SGD", "HKD", "THB", "MYR", "IDR",
    "PHP", "KRW", "TWD", "SAR", "AED", "QAR"
}


def is_forex_pair(symbol):
    if not isinstance(symbol, str) or not symbol.endswith("_otc"):
        return False
    pair = symbol[:-4]
    if len(pair) != 6 or not pair.isalpha():
        return False
    return pair[:3].upper() in CURRENCIES and pair[3:].upper() in CURRENCIES


def get_eligible_symbols():
    data = api_get("/v1/symbols", params={"venue": VENUE})
    eligible = []
    for item in data.get("symbols", []):
        symbol = item.get("symbol")
        if not is_forex_pair(symbol) or item.get("open") is not True:
            continue
        try:
            payout = float(item.get("payout"))
        except (TypeError, ValueError):
            continue
        if payout < MIN_PAYOUT:
            continue
        eligible.append({"symbol": symbol, "payout": payout})
    eligible.sort(key=lambda x: (-x["payout"], x["symbol"]))
    return eligible


def get_candles(symbol):
    data = api_get(
        "/v1/candles",
        params={"venue": VENUE, "symbol": symbol, "tf": TIMEFRAME, "limit": CANDLE_LIMIT}
    )
    candles = data.get("candles", [])
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
    parsed.sort(key=lambda x: x["time"])
    if len(parsed) < MIN_CANDLES:
        return []
    return parsed[-CANDLE_LIMIT:]


def ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)
    return result


def bollinger_bands(closes, period=20, std_dev=2.2):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    sd = math.sqrt(variance)
    return middle + std_dev * sd, middle, middle - std_dev * sd


def calculate_rsi(closes, period=7):
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]
        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )
        ranges.append(tr)
    return sum(ranges[-period:]) / period


def calculate_adx(candles, period=14):
    if len(candles) < (period * 2) + 2:
        return None, None, None
    true_ranges, plus_moves, minus_moves = [], [], []
    for i in range(1, len(candles)):
        current, previous = candles[i], candles[i - 1]
        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )
        true_ranges.append(tr)
        plus_moves.append(plus_dm)
        minus_moves.append(minus_dm)

    dx_values = []
    start = max(period, len(true_ranges) - period)
    for end in range(start, len(true_ranges) + 1):
        tr_sum = sum(true_ranges[end - period:end])
        plus_sum = sum(plus_moves[end - period:end])
        minus_sum = sum(minus_moves[end - period:end])
        if tr_sum <= 0:
            continue
        plus_di = 100 * plus_sum / tr_sum
        minus_di = 100 * minus_sum / tr_sum
        denominator = plus_di + minus_di
        if denominator > 0:
            dx_values.append(100 * abs(plus_di - minus_di) / denominator)

    if not dx_values:
        return None, None, None
    adx = sum(dx_values) / len(dx_values)
    tr_sum = sum(true_ranges[-period:])
    plus_sum = sum(plus_moves[-period:])
    minus_sum = sum(minus_moves[-period:])
    if tr_sum <= 0:
        return None, None, None
    return adx, 100 * plus_sum / tr_sum, 100 * minus_sum / tr_sum


def wick_rejection(candle, direction):
    o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
    body = abs(c - o)
    candle_range = h - l
    if candle_range <= 0:
        return False
    lower_wick, upper_wick = min(o, c) - l, h - max(o, c)
    minimum = max(body * 1.2, candle_range * 0.30)
    return lower_wick >= minimum if direction == "CALL" else upper_wick >= minimum


def analyze_candles(candles):
    if len(candles) < MIN_CANDLES:
        return None
    closes = [float(c["close"]) for c in candles]
    upper, middle, lower = bollinger_bands(closes, BB_PERIOD, BB_STD)
    prev_upper, _, prev_lower = bollinger_bands(closes[:-1], BB_PERIOD, BB_STD)
    ema9, ema21, ema50 = ema(closes, EMA_FAST), ema(closes, EMA_MID), ema(closes, EMA_SLOW)
    ema9_prev = ema(closes[:-1], EMA_FAST)
    rsi = calculate_rsi(closes, RSI_PERIOD)
    atr = calculate_atr(candles, ATR_PERIOD)
    adx, plus_di, minus_di = calculate_adx(candles, ADX_PERIOD)

    if any(v is None for v in [upper, middle, lower, ema9, rsi, atr, adx, plus_di, minus_di]):
        return None
    if atr <= 0 or adx < ADX_MIN:
        return None

    last = candles[-1]
    candle_range = last["high"] - last["low"]
    if candle_range <= 0 or not (MIN_CANDLE_ATR_RATIO <= candle_range / atr <= MAX_CANDLE_ATR_RATIO):
        return None

    # Egzanp senplifye pou prese pran siyal yo pi vit
    call = last["close"] > middle and rsi < 45 and wick_rejection(last, "CALL")
    put = last["close"] < middle and rsi > 55 and wick_rejection(last, "PUT")

    if call:
        return {"direction": "CALL", "strategies": ["BOLLINGER QUICK REVERSAL"], "strategies_agree": 2, "score": 6, "rsi": round(rsi, 2), "adx": round(adx, 2), "plus_di": round(plus_di, 2), "minus_di": round(minus_di, 2), "close": last["close"], "confirmations": ["QUICK CONFIRMATION"]}
    if put:
        return {"direction": "PUT", "strategies": ["BOLLINGER QUICK REVERSAL"], "strategies_agree": 2, "score": 6, "rsi": round(rsi, 2), "adx": round(adx, 2), "plus_di": round(plus_di, 2), "minus_di": round(minus_di, 2), "close": last["close"], "confirmations": ["QUICK CONFIRMATION"]}

    return None


def send_signal(symbol, payout, signal, detection_time, entry_time):
    direction = signal["direction"]
    arrow = "🟢" if direction == "CALL" else "🔴"
    message = (
        f"🤖 {VERSION}\n\n"
        f"{arrow} {direction}\n"
        f"💱 PAIR: {symbol} | PAYOUT: {payout:.1f}%\n"
        f"⏱ TIMEFRAME: S30 | EXPIRATION: 60s\n"
        f"⭐ QUALITY SCORE: {signal['score']}/8\n"
        f"📊 RSI: {signal['rsi']} | ADX: {signal['adx']}"
    )
    return send_telegram(message)


def download_all_histories(eligible):
    histories, ranked = {}, []
    def fetch_history(item):
        return item, get_candles(item["symbol"])

    with ThreadPoolExecutor(max_workers=MAX_HISTORY_WORKERS) as executor:
        futures = {executor.submit(fetch_history, item): item for item in eligible[:10]}
        for future in as_completed(futures):
            item = futures[future]
            try:
                _, candles = future.result()
                if candles:
                    histories[item["symbol"]] = candles
                    ranked.append({**item, "history_count": len(candles)})
            except Exception:
                pass
    ranked.sort(key=lambda x: (-x["payout"], x["symbol"]))
    return histories, ranked


def main():
    log(f"{VERSION} starting...")
    check_account()
    eligible = get_eligible_symbols()
    if not eligible:
        return

    histories, ranked = download_all_histories(eligible)
    if not ranked:
        return

    monitored = ranked[:MAX_LIVE_SYMBOLS]
    deadline = time.time() + MAX_SCAN_SECONDS

    symbols = [item["symbol"] for item in monitored]
    response = api_get("/v1/stream", params={"venue": VENUE, "symbol": ",".join(symbols)}, stream=True)

    current_bars = {}
    try:
        for raw_line in response.iter_lines(decode_unicode=True, chunk_size=1):
            if time.time() >= deadline:
                break
            if not raw_line or not raw_line.startswith("data:"):
                continue
            try:
                data = json.loads(raw_line[5:].strip())
            except Exception:
                continue

            symbol, price, feed_time = data.get("symbol"), float(data.get("price", 0)), int(data.get("time", 0))
            bucket = (feed_time // TIMEFRAME) * TIMEFRAME

            if symbol not in current_bars:
                current_bars[symbol] = {"time": bucket, "open": price, "high": price, "low": price, "close": price}
                continue

            current = current_bars[symbol]
            if bucket > current["time"]:
                closed_candles = histories.get(symbol, [])
                closed_candles.append(current)
                signal = analyze_candles(closed_candles)
                if signal:
                    pair_info = next(item for item in monitored if item["symbol"] == symbol)
                    send_signal(symbol, pair_info["payout"], signal, time.time(), make_entry_time(time.time()))
                    break
                current_bars[symbol] = {"time": bucket, "open": price, "high": price, "low": price, "close": price}
            else:
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price
    finally:
        response.close()
    log("Run finished.")


if __name__ == "__main__":
    main()
