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

# =====================================================
# ROMARIO TRADER PRO 15.0 ULTRA FAST - S30 & 60s EXP
# =====================================================

VERSION = "ROMARIO TRADER PRO 15.0 ULTRA FAST"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 30            # Bouji 30 segonn
EXPIRATION = 60           # Ekspirasyon 1 minit (2 bouji 30s)
CANDLE_LIMIT = 100
MIN_CANDLES = 25
MIN_PAYOUT = 80.0
MAX_SCAN_SECONDS = 300    # Tan max pou bot la ap kouri (5 minit)
MAX_RECONNECTS = 5

ALLOWED_PAIRS = {
    "EURUSD_otc",
    "GBPUSD_otc",
    "AUDUSD_otc",
    "USDCAD_otc",
    "EURJPY_otc",
    "USDJPY_otc",
    "GBPJPY_otc",
    "AUDCAD_otc",
    "AUDCHF_otc",
    "EURGBP_otc"
}

API_CONNECT_TIMEOUT = 4
API_READ_TIMEOUT = 10

TELEGRAM_ATTEMPTS = 3
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
    def __init__(self, message, retry_after=32):
        super().__init__(message)
        self.retry_after = retry_after


def log(message):
    logging.info(message)


def normalize_ts(ts):
    ts = int(ts)
    return ts // 1000 if ts > 10_000_000_000 else ts


def get_session():
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update({
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "RomarioTraderPro/15.0",
            "Accept": "application/json"
        })
    return _thread_local.session


def make_exact_entry_time(feed_time):
    feed_time = normalize_ts(feed_time)
    return ((feed_time // 30) + 1) * 30


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
                retry_hdr = response.headers.get("Retry-After", "32")
                try:
                    retry_after = max(30, int(float(retry_hdr)))
                except (TypeError, ValueError):
                    retry_after = 32
                response.close()
                raise StreamRateLimitError("Rate limit", retry_after)

            if response.status_code in (401, 402, 403, 500, 502, 503, 504):
                if attempt < attempts:
                    response.close()
                    time.sleep(0.3)
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
            time.sleep(0.3)

    raise RuntimeError("Request failed.")


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials missing.")
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


# =====================================================
# FONKSYON BAZ (EMA AK RSI)
# =====================================================
def ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (value * multiplier) + (result * (1 - multiplier))
    return result


def calculate_rsi(closes, period=7):
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# =====================================================
# ENDIKATÈ RAPID POU 30 SEGONN (BOLLINGER, STOCH, KELTNER, CCI)
# =====================================================
def bollinger_bands(closes, period=20, std_dev=2.0):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    sd = math.sqrt(variance)
    return sma + (std_dev * sd), sma, sma - (std_dev * sd)


def fast_stochastic(candles, period=5):
    if len(candles) < period + 2:
        return None, None
    k_values = []
    for i in range(-3, 0):
        subset = candles[i - period + 1 : i + 1] if i != -1 else candles[-period:]
        low_min = min(c["low"] for c in subset)
        high_max = max(c["high"] for c in subset)
        if high_max == low_min:
            k_values.append(50.0)
        else:
            k_values.append(100.0 * (subset[-1]["close"] - low_min) / (high_max - low_min))
    k = k_values[-1]
    d = sum(k_values) / len(k_values)
    return k, d


def keltner_channels(candles, period=20, multiplier=1.5):
    if len(candles) < period + 1:
        return None, None, None
    closes = [c["close"] for c in candles]
    mid = ema(closes, period)
    if mid is None:
        return None, None, None
    trs = []
    for i in range(-period, 0):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / period
    return mid + (multiplier * atr), mid, mid - (multiplier * atr)


def calculate_cci(candles, period=14):
    if len(candles) < period:
        return None
    tps = [(c["high"] + c["low"] + c["close"]) / 3.0 for c in candles[-period:]]
    sma_tp = sum(tps) / period
    mean_dev = sum(abs(tp - sma_tp) for tp in tps) / period
    if mean_dev == 0:
        return 0.0
    return (tps[-1] - sma_tp) / (0.015 * mean_dev)


# =====================================================
# ESTRATEJI 3-EN-1 POU REZILTA SOLID NAN 30 SEGONN
# =====================================================
def analyze_candles(candles):
    if len(candles) < MIN_CANDLES:
        return None

    closes = [c["close"] for c in candles]
    bb_upper, bb_mid, bb_lower = bollinger_bands(closes, period=20, std_dev=2.0)
    kc_upper, kc_mid, kc_lower = keltner_channels(candles, period=20, multiplier=1.5)
    ema20 = ema(closes, 20)
    rsi = calculate_rsi(closes, 7)
    k, d = fast_stochastic(candles, period=5)
    cci = calculate_cci(candles, period=14)

    if any(v is None for v in (bb_upper, kc_upper, ema20, rsi, k, d, cci)):
        return None

    prev = candles[-2]
    last = candles[-1]

    p_open, p_close = prev["open"], prev["close"]
    l_open, l_close = last["open"], last["close"]
    l_high, l_low = last["high"], last["low"]

    if abs(l_close - l_open) == 0:
        return None

    direction = None
    strategy_name = ""

    # 1. BOLLINGER REVERSAL
    if (p_close <= bb_lower or l_low <= bb_lower or l_close <= bb_lower) and l_close > l_open and rsi <= 45:
        direction = "CALL"
        strategy_name = "BOLLINGER REVERSAL"
    elif (p_close >= bb_upper or l_high >= bb_upper or l_close >= bb_upper) and l_close < l_open and rsi >= 55:
        direction = "PUT"
        strategy_name = "BOLLINGER REVERSAL"

    # 2. EMA 20 + STOCHASTIC PULLBACK
    if direction is None:
        if l_close > ema20 and l_close > l_open and k > d and rsi >= 45:
            direction = "CALL"
            strategy_name = "EMA 20 + STOCH PULLBACK"
        elif l_close < ema20 and l_close < l_open and k < d and rsi <= 55:
            direction = "PUT"
            strategy_name = "EMA 20 + STOCH PULLBACK"

    # 3. KELTNER CHANNEL + CCI BREAKOUT
    if direction is None:
        if l_close > kc_upper and l_close > l_open and cci >= 80:
            direction = "CALL"
            strategy_name = "KELTNER + CCI BREAKOUT"
        elif l_close < kc_lower and l_close < l_open and cci <= -80:
            direction = "PUT"
            strategy_name = "KELTNER + CCI BREAKOUT"

    if direction is None:
        return None

    return {
        "direction": direction,
        "strategy": strategy_name,
        "score": 7,
        "rsi": round(rsi, 2),
        "k": round(k, 2),
        "d": round(d, 2),
        "cci": round(cci, 2),
        "close": l_close,
        "candle_time": last["time"]
    }


def send_signal(symbol, payout, signal, detection_time, entry_time):
    signal_dt = datetime.fromtimestamp(detection_time, TIMEZONE)
    entry_dt = datetime.fromtimestamp(entry_time, TIMEZONE)

    direction = signal["direction"]
    arrow = "🟢" if direction == "CALL" else "🔴"

    message = (
        f"🤖 {VERSION}\n\n"
        f"{arrow} {direction}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"💰 PAYOUT: {payout:.1f}%\n"
        f"⏱ TIMEFRAME: S30 | EXPIRATION: 60s\n"
        f"🧠 STRATEGY: {signal['strategy']}\n\n"
        f"📊 RSI: {signal['rsi']} | CCI: {signal['cci']}\n"
        f"📈 STOCH K/D: {signal['k']} / {signal['d']}\n\n"
        f"⏰ DETECTED: {signal_dt.strftime('%I:%M:%S %p')}\n"
        f"🎯 EXACT ENTRY: {entry_dt.strftime('%I:%M:%S %p')}\n\n"
        f"⚠️ Antre nan egzakteman fen minit la (egz: XX:XX:50 pou XX:XX:00)."
    )
    return send_telegram(message)


def send_result(symbol, pending, exit_price):
    entry_price = pending["entry_price"]
    direction = pending["direction"]

    if direction == "CALL":
        if exit_price > entry_price:
            result = "WIN (OTCHARTS ESTIMATE)"
            emoji = "🟢"
        elif exit_price < entry_price:
            result = "LOSS (OTCHARTS ESTIMATE)"
            emoji = "🔴"
        else:
            result = "DRAW (OTCHARTS ESTIMATE)"
            emoji = "🟡"
    else:
        if exit_price < entry_price:
            result = "WIN (OTCHARTS ESTIMATE)"
            emoji = "🟢"
        elif exit_price > entry_price:
            result = "LOSS (OTCHARTS ESTIMATE)"
            emoji = "🔴"
        else:
            result = "DRAW (OTCHARTS ESTIMATE)"
            emoji = "🟡"

    difference = exit_price - entry_price

    message = (
        f"📊 {VERSION} - RESULT\n\n"
        f"{emoji} {result}\n\n"
        f"💱 PAIR: {symbol}\n"
        f"📍 DIRECTION: {direction}\n"
        f"🎯 ENTRY PRICE: {format_price(entry_price)}\n"
        f"🏁 EXIT PRICE: {format_price(exit_price)}\n"
        f"📐 PRICE CHANGE: {difference:+.8f}\n"
        f"⌛ EXPIRATION: 60 SECONDS\n\n"
        f"ℹ️ OTCharts estimate only."
    )
    send_telegram(message)


def get_eligible_symbols():
    data = api_get("/v1/symbols", params={"venue": VENUE})
    eligible = []
    seen = set()

    for item in data.get("symbols", []):
        symbol = item.get("symbol")
        if symbol not in ALLOWED_PAIRS or symbol in seen:
            continue
        if item.get("open") is False:
            continue
        try:
            payout = float(item.get("payout", 85.0))
        except (TypeError, ValueError):
            payout = 85.0
        if payout < MIN_PAYOUT:
            continue
        seen.add(symbol)
        eligible.append({"symbol": symbol, "payout": payout})

    if not eligible:
        for symbol in ALLOWED_PAIRS:
            eligible.append({"symbol": symbol, "payout": 85.0})

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
                "time": normalize_ts(candle["time"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"])
            })
        except (KeyError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda x: x["time"])
    return parsed[-CANDLE_LIMIT:] if len(parsed) >= MIN_CANDLES else []


def download_all_histories(eligible):
    histories, ranked = {}, []
    def fetch_history(item):
        return item, get_candles(item["symbol"])

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_history, item): item for item in eligible}
        for future in as_completed(futures):
            item = futures[future]
            try:
                _, candles = future.result()
                if candles:
                    histories[item["symbol"]] = candles
                    ranked.append(item)
            except Exception:
                pass
    return histories, ranked


def run_live_stream(monitored, histories, current_bars, deadline):
    symbols = [item["symbol"] for item in monitored]
    params = {"venue": VENUE, "symbol": ",".join(symbols)}

    log(f"Opening S30 stream for {len(symbols)} pairs.")
    response = api_get("/v1/stream", params=params, stream=True)

    connected_symbols = set()
    pending_signal = None
    event_name = ""

    try:
        for raw_line in response.iter_lines(decode_unicode=True, chunk_size=1):
            now = time.time()
            if now >= deadline:
                log("Scan completed.")
                return "done"

            if not raw_line:
                event_name = ""
                continue

            line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
            if not line or line.startswith(":"):
                continue

            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue

            if not line.startswith("data:"):
                continue

            try:
                data = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue

            if event_name == "connected":
                connected_symbols = set(data.get("symbols", []))
                log("Connected: " + ", ".join(sorted(connected_symbols)))
                continue

            if event_name != "tick":
                continue

            symbol = data.get("symbol")
            if connected_symbols and symbol not in connected_symbols:
                continue

            try:
                price = float(data["price"])
                feed_time = normalize_ts(data["time"])
            except (KeyError, TypeError, ValueError):
                continue

            now = time.time()

            if pending_signal is not None:
                if symbol == pending_signal["symbol"]:
                    pending = pending_signal
                    if pending["entry_price"] is None:
                        if now >= pending["entry_time"]:
                            pending["entry_price"] = price
                            log(f"{symbol}: Entry price set at {format_price(price)}")
                    if pending["entry_price"] is not None and now >= pending["expiry_time"]:
                        send_result(symbol, pending, price)
                        return "done"
                continue

            bucket = (feed_time // TIMEFRAME) * TIMEFRAME

            if symbol not in current_bars:
                history = [c for c in histories.get(symbol, []) if c["time"] < bucket]
                histories[symbol] = history
                current_bars[symbol] = {"time": bucket, "open": price, "high": price, "low": price, "close": price}
                continue

            current = current_bars[symbol]

            if bucket > current["time"]:
                histories.setdefault(symbol, []).append(current)
                histories[symbol] = histories[symbol][-CANDLE_LIMIT:]

                closed_candles = histories.get(symbol, [])
                signal = analyze_candles(closed_candles)

                if signal:
                    pair_info = next((item for item in monitored if item["symbol"] == symbol), {"symbol": symbol, "payout": 85.0})
                    detection_time = time.time()
                    entry_time = make_exact_entry_time(feed_time)

                    send_signal(symbol, pair_info["payout"], signal, detection_time, entry_time)

                    pending_signal = {
                        "symbol": symbol,
                        "direction": signal["direction"],
                        "entry_time": entry_time,
                        "expiry_time": entry_time + EXPIRATION,
                        "entry_price": None
                    }
                    log(f"{symbol}: Signal sent successfully!")

                current_bars[symbol] = {"time": bucket, "open": price, "high": price, "low": price, "close": price}
            else:
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price

    finally:
        response.close()

    return "reconnect"


def main():
    log(f"{VERSION} starting...")
    if not API_KEY or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Credentials missing!")
        return

    eligible = get_eligible_symbols()
    if not eligible:
        log("No eligible pairs found.")
        return

    histories, ranked = download_all_histories(eligible)
    if not ranked:
        log("No histories available.")
        return

    current_bars = {}
    deadline = time.time() + MAX_SCAN_SECONDS

    reconnects = 0
    while time.time() < deadline:
        try:
            status = run_live_stream(ranked, histories, current_bars, deadline)
            if status == "done":
                break
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                break
            wait_sec = min(5.0, max(1.0, deadline - time.time()))
            log(f"Stream ended. Reconnecting in {wait_sec:.1f}s...")
            time.sleep(wait_sec)
        except StreamRateLimitError as e:
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                log("Max reconnects reached after rate limit.")
                break
            remaining = deadline - time.time()
            wait_sec = min(e.retry_after, remaining)
            if wait_sec <= 2:
                break
            log(f"Rate limit hit. Waiting {wait_sec:.1f}s before retry...")
            time.sleep(wait_sec)
        except Exception as e:
            log(f"Stream error: {e}")
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                break
            remaining = deadline - time.time()
            wait_sec = min(5.0, max(1.0, remaining))
            if wait_sec <= 1:
                break
            time.sleep(wait_sec)

    log(f"{VERSION} finished.")


if __name__ == "__main__":
    main()
