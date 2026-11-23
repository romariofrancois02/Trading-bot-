import os
import json
import time
import logging
import threading
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

# =====================================================
# 1. PARAMÈT JENERAL (M1, EXP 1M, PAYOUT >= 85%, 1 SIGNAL/RUN)
# =====================================================
VERSION = "ROMARIO PRO M1"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 60            # Bouji 1 minit
EXPIRATION = 60           # Ekspirasyon 1 minit
MIN_PAYOUT = 85.0         # Mache 85% ou plis
ENTRY_LEAD = 12           # Avèti w 12 segonn anvan bouji a louvri (10-15s)
MAX_SIGNALS_PER_RUN = 1   # Kanpe otomatikman aprè 1 sèl siyal
MAX_WAIT_SECONDS = 180    # Chèche yon bon opòtinite nan 1 a 3 minit
MAX_STREAM_PAIRS = 4      # Limit ki pi stab pou /v1/stream

CANDLE_LIMIT = 80
MIN_CANDLES = 35
MAX_RECONNECTS = 4

# Pè klasik ki pi estab (evite pè ekzotik ki manipile)
VALID_PAIRS = {
    "EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDUSD_otc",
    "USDCAD_otc", "USDCHF_otc", "NZDUSD_otc", "EURGBP_otc",
    "EURJPY_otc", "GBPJPY_otc", "AUDCHF_otc", "EURCHF_otc",
    "AUDCAD_otc", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"
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
    def __init__(self, message, retry_after=30):
        super().__init__(message)
        self.retry_after = retry_after


class StreamBadRequestError(Exception):
    pass


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
            "User-Agent": "RomarioProM1/1.0",
            "Accept": "application/json"
        })
    return _thread_local.session


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

            if response.status_code == 400 and stream:
                body = ""
                try:
                    body = response.text[:160]
                except Exception:
                    pass
                response.close()
                raise StreamBadRequestError(f"400 Bad Request: {body}")

            if response.status_code == 429:
                retry_hdr = response.headers.get("Retry-After", "30")
                try:
                    retry_after = max(25, int(float(retry_hdr)))
                except (TypeError, ValueError):
                    retry_after = 30
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

        except (StreamRateLimitError, StreamBadRequestError):
            raise
        except Exception:
            if response is not None:
                response.close()
            if attempt >= attempts:
                raise
            time.sleep(0.3)

    raise RuntimeError("Request failed.")


def send_telegram_fn(message):
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
# 2. KALKIL ENDIKATÈ REYÈL (PA GEN DEVINÈT)
# =====================================================
def calc_ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = (v * k) + (ema_val * (1 - k))
    return ema_val


def calc_rsi(closes, period=7):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_adx(candles, period=14):
    if len(candles) < period * 2:
        return 20.0
    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        ph, pl = candles[i - 1]["high"], candles[i - 1]["low"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up_move = h - ph
        down_move = pl - l
        plus_dms.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dms.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
    atr = sum(trs[-period:]) / period
    if atr == 0:
        return 0.0
    p_di = 100 * (sum(plus_dms[-period:]) / period) / atr
    m_di = 100 * (sum(minus_dms[-period:]) / period) / atr
    if (p_di + m_di) == 0:
        return 0.0
    return 100 * abs(p_di - m_di) / (p_di + m_di)


# =====================================================
# 3. ESTRATEJI "TREND PULLBACK + REJECTION"
# =====================================================
def evaluate_real_market(candles):
    if len(candles) < 35:
        return None

    closes = [c["close"] for c in candles]
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    rsi = calc_rsi(closes, 7)
    adx = calc_adx(candles, 14)

    if any(v is None for v in (ema9, ema21, rsi)):
        return None

    prev = candles[-2]
    curr = candles[-1]

    if adx < 13:
        return None

    direction = None

    # ---------------- CALL (TANDANS MONTE + REBOND) ----------------
    if (ema9 > ema21 and prev["low"] <= ema9
            and curr["close"] > curr["open"] and curr["close"] > ema9
            and 42 <= rsi <= 72):
        direction = "CALL"

    # ---------------- PUT (TANDANS DESANN + REBOND) ----------------
    elif (ema9 < ema21 and prev["high"] >= ema9
            and curr["close"] < curr["open"] and curr["close"] < ema9
            and 28 <= rsi <= 58):
        direction = "PUT"

    if direction is None:
        return None

    if adx >= 20 and ((direction == "CALL" and 48 <= rsi <= 66) or (direction == "PUT" and 34 <= rsi <= 52)):
        confidence = "🟢 TRÈ SOLID (Bon anpil)"
        score = 6
    else:
        confidence = "🟡 RISKE (Fè pridan)"
        score = 5

    return {
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "close": curr["close"],
        "candle_time": curr["time"]
    }


# =====================================================
# 4. FORMAT MESAJ TELEGRAM SENP & RÈG WIN SÈLMAN
# =====================================================
def format_signal_message(pair, payout, entry_time_str, signal_data):
    emoji = "🟢" if signal_data["direction"] == "CALL" else "🔴"
    return (
        f"🤖 ROMARIO PRO M1\n\n"
        f"{emoji} {signal_data['direction']} | {pair} ({payout:.0f}%)\n"
        f"🎯 Antre: {entry_time_str} (Exp: 1 min)\n"
        f"🛡️ Konfyans: {signal_data['confidence']}"
    )


def handle_trade_result(pair, payout, is_win, send_fn):
    if is_win:
        msg = f"✅ REZILTA: WIN | {pair} (+{payout:.0f}%)"
        send_fn(msg)


def get_eligible_symbols():
    data = api_get("/v1/symbols", params={"venue": VENUE})
    eligible = []
    seen = set()

    for item in data.get("symbols", []):
        symbol = item.get("symbol")
        if symbol not in VALID_PAIRS or symbol in seen:
            continue
        if item.get("open") is False:
            continue
        try:
            payout = float(item.get("payout", 0.0))
        except (TypeError, ValueError):
            payout = 0.0
        if payout < MIN_PAYOUT:
            continue
        seen.add(symbol)
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
    for c in candles:
        try:
            parsed.append({
                "time": normalize_ts(c["time"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"])
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

    ranked.sort(key=lambda x: (-x["payout"], x["symbol"]))
    return histories, ranked


def run_live_stream(monitored, histories, current_bars, deadline):
    symbols = [item["symbol"] for item in monitored]
    params = {"venue": VENUE, "symbol": ",".join(symbols)}

    log(f"Opening M1 stream for {len(symbols)} pairs (>= {MIN_PAYOUT:.0f}%).")
    response = api_get("/v1/stream", params=params, stream=True)

    connected_symbols = set()
    signals_sent = 0
    pending = None
    event_name = ""

    try:
        for raw_line in response.iter_lines(decode_unicode=True, chunk_size=1):
            now = time.time()

            if pending is None and now >= deadline:
                log("Scan window ended with no signal.")
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

            if pending is not None:
                if symbol == pending["symbol"]:
                    if pending["entry_price"] is None and now >= pending["entry_time"]:
                        pending["entry_price"] = price
                        log(f"{symbol}: Entry set at {format_price(price)}")

                    if pending["entry_price"] is not None and now >= pending["expiry_time"]:
                        is_win = (
                            (pending["direction"] == "CALL" and price > pending["entry_price"]) or
                            (pending["direction"] == "PUT" and price < pending["entry_price"])
                        )
                        handle_trade_result(
                            symbol,
                            pending["payout"],
                            is_win,
                            send_telegram_fn
                        )
                        log(f"{symbol}: Trade finished ({'WIN' if is_win else 'LOSS'}). Stopping bot.")
                        return "done"
                continue

            if signals_sent >= MAX_SIGNALS_PER_RUN:
                return "done"

            bucket = (feed_time // TIMEFRAME) * TIMEFRAME

            if symbol not in current_bars:
                histories[symbol] = [c for c in histories.get(symbol, []) if c["time"] < bucket]
                current_bars[symbol] = {
                    "time": bucket, "open": price, "high": price, "low": price, "close": price
                }
                continue

            current = current_bars[symbol]

            seconds_left_in_bar = (current["time"] + TIMEFRAME) - feed_time
            if 0 < seconds_left_in_bar <= ENTRY_LEAD and not current.get("evaluated_preclose"):
                current["evaluated_preclose"] = True
                hist = histories.get(symbol, [])
                if len(hist) >= 1 and len(hist) + 1 >= MIN_CANDLES:
                    window = hist[-(MIN_CANDLES - 1):] + [current]
                    signal_data = evaluate_real_market(window)
                    if signal_data:
                        pair_info = next((x for x in monitored if x["symbol"] == symbol), {"payout": MIN_PAYOUT})
                        payout = pair_info["payout"]
                        entry_ts = current["time"] + TIMEFRAME
                        entry_time_str = datetime.fromtimestamp(entry_ts, TIMEZONE).strftime("%I:%M:%S %p")

                        msg = format_signal_message(symbol, payout, entry_time_str, signal_data)
                        if send_telegram_fn(msg):
                            signals_sent += 1
                            pending = {
                                "symbol": symbol,
                                "payout": payout,
                                "direction": signal_data["direction"],
                                "entry_time": entry_ts,
                                "expiry_time": entry_ts + EXPIRATION,
                                "entry_price": None
                            }
                            log(f"{symbol}: Signal sent ({signal_data['direction']}) -> Entry {entry_time_str}")

            if bucket > current["time"]:
                clean_bar = {
                    "time": current["time"],
                    "open": current["open"],
                    "high": current["high"],
                    "low": current["low"],
                    "close": current["close"]
                }
                histories.setdefault(symbol, []).append(clean_bar)
                histories[symbol] = histories[symbol][-CANDLE_LIMIT:]

                current_bars[symbol] = {
                    "time": bucket, "open": price, "high": price, "low": price, "close": price
                }
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
        log("No pairs >= 85% open right now.")
        return

    histories, ranked = download_all_histories(eligible)
    if not ranked:
        log("No candle history available.")
        return

    active_limit = min(MAX_STREAM_PAIRS, len(ranked))
    current_bars = {}
    deadline = time.time() + MAX_WAIT_SECONDS

    reconnects = 0
    while time.time() < deadline:
        stream_subset = ranked[:active_limit]
        try:
            status = run_live_stream(stream_subset, histories, current_bars, deadline)
            if status == "done":
                break
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                break
            wait_sec = min(4.0, max(1.0, deadline - time.time()))
            log(f"Reconnecting in {wait_sec:.1f}s...")
            time.sleep(wait_sec)
        except StreamBadRequestError as e:
            log(f"Stream error: {e}")
            if active_limit > 1:
                active_limit -= 1
                log(f"Reducing stream batch to {active_limit} pairs and retrying...")
                time.sleep(1.0)
                continue
            break
        except StreamRateLimitError as e:
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                break
            wait_sec = min(e.retry_after, max(0.0, deadline - time.time()))
            if wait_sec <= 2:
                break
            log(f"Rate limit. Waiting {wait_sec:.1f}s...")
            time.sleep(wait_sec)
        except Exception as e:
            log(f"Stream error: {e}")
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                break
            wait_sec = min(4.0, max(1.0, deadline - time.time()))
            if wait_sec <= 1:
                break
            time.sleep(wait_sec)

    log(f"{VERSION} finished.")


if __name__ == "__main__":
    main()
