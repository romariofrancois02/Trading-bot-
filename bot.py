import os
import sys
import json
import time
import logging
import threading
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

# =====================================================
# 1. PARAMÈT JENERAL (ALL ELIGIBLE OTC PAIRS -> TOP 4 SNAP 60S)
# =====================================================
VERSION = "ROMARIO PRO M1 - ALL OTC TOP 4 SNAP 60S"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 60            # Graf 1 minit (M1)
EXPIRATION = 60           # Ekspirasyon 60 segonn (1 minit)
MIN_PAYOUT = 85.0         # Minimòm 85% payout sou OTC
ENTRY_LEAD = 3            # Avèti w 3 segonn anvan ouvèti nouvo bouji a
MAX_SIGNALS_PER_RUN = 1   # Kanpe otomatikman aprè 1 sèl siyal
MAX_WAIT_SECONDS = 240    # Chèche yon bon opòtinite nan 4 minit max
MAX_STREAM_PAIRS = 4      # 4 pi bon pè yo pou /v1/stream

CANDLE_LIMIT = 90
MIN_CANDLES = 50
MAX_RECONNECTS = 4

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
            "User-Agent": "RomarioAllOtcSnapM1/1.0",
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
# 2. KALKIL EMA 9, EMA 20, EMA 40 ("MOMENTUM SNAP")
# =====================================================
def calc_ema_series(closes, period):
    if len(closes) < period:
        return []
    multiplier = 2.0 / (period + 1.0)
    ema_val = sum(closes[:period]) / period
    series = [ema_val]
    for price in closes[period:]:
        ema_val = (price - ema_val) * multiplier + ema_val
        series.append(ema_val)
    return series


def align_ema_series(closes, period):
    raw = calc_ema_series(closes, period)
    if not raw:
        return [None] * len(closes)
    pad_len = len(closes) - len(raw)
    return ([None] * pad_len) + raw


def calc_win_probability_snap(direction, ema9, ema20, ema40, curr, avg_range):
    prob = 56.0

    spread_now = abs(ema9[-1] - ema20[-1])
    spread_prev = abs(ema9[-2] - ema20[-2])
    if spread_now > spread_prev:
        prob += 12.0
    else:
        prob += 4.0

    if direction == "CALL":
        if ema9[-1] > ema9[-2] > ema9[-3] and ema20[-1] > ema20[-2] and ema40[-1] > ema40[-2]:
            prob += 14.0
        else:
            prob += 6.0
        if curr["close"] >= ema9[-1]:
            prob += 10.0
    else:
        if ema9[-1] < ema9[-2] < ema9[-3] and ema20[-1] < ema20[-2] and ema40[-1] < ema40[-2]:
            prob += 14.0
        else:
            prob += 6.0
        if curr["close"] <= ema9[-1]:
            prob += 10.0

    prob = max(50.0, min(98.0, prob))
    return int(round(prob))


def check_snap_structure(window):
    closes = [c["close"] for c in window]
    ema9 = align_ema_series(closes, 9)
    ema20 = align_ema_series(closes, 20)
    ema40 = align_ema_series(closes, 40)

    if any(x is None for x in (ema9[-1], ema9[-2], ema9[-3], ema20[-1], ema20[-2], ema40[-1], ema40[-2])):
        return None

    avg_range = sum(max(c["high"] - c["low"], 1e-12) for c in window[-10:]) / 10.0
    min_gap = max(avg_range * 0.25, 1e-12)

    gap_9_20_now = abs(ema9[-1] - ema20[-1])
    gap_20_40_now = abs(ema20[-1] - ema40[-1])
    gap_9_20_prev = abs(ema9[-2] - ema20[-2])

    expanding_9_20 = gap_9_20_now > gap_9_20_prev

    bull_snap_trend = (
        ema9[-1] > ema20[-1] > ema40[-1]
        and gap_9_20_now >= min_gap
        and gap_20_40_now >= min_gap
        and expanding_9_20
        and ema9[-1] > ema9[-2] > ema9[-3]
        and ema20[-1] > ema20[-2]
        and ema40[-1] > ema40[-2]
    )

    bear_snap_trend = (
        ema9[-1] < ema20[-1] < ema40[-1]
        and gap_9_20_now >= min_gap
        and gap_20_40_now >= min_gap
        and expanding_9_20
        and ema9[-1] < ema9[-2] < ema9[-3]
        and ema20[-1] < ema20[-2]
        and ema40[-1] < ema40[-2]
    )

    return {
        "ema9": ema9,
        "ema20": ema20,
        "ema40": ema40,
        "avg_range": avg_range,
        "bull_snap_trend": bull_snap_trend,
        "bear_snap_trend": bear_snap_trend
    }


def score_snap_structure(candles):
    if len(candles) < MIN_CANDLES:
        return 0.0

    window = candles[-60:] if len(candles) >= 60 else candles
    info = check_snap_structure(window)
    if info is None:
        return 0.0

    curr = window[-1]
    e9 = info["ema9"][-1]
    avg_r = info["avg_range"]

    score = 20.0
    if info["bull_snap_trend"] or info["bear_snap_trend"]:
        score += 40.0
    elif (info["ema9"][-1] > info["ema20"][-1] > info["ema40"][-1]) or (
        info["ema9"][-1] < info["ema20"][-1] < info["ema40"][-1]
    ):
        score += 15.0

    if abs(curr["close"] - e9) <= avg_r * 1.6:
        score += 15.0

    return round(score, 2)


# =====================================================
# 3. ESTRATEJI "MOMENTUM SNAP" 60S (CALL & PUT)
# =====================================================
def evaluate_real_market(candles):
    if len(candles) < MIN_CANDLES:
        return None, "not enough candles"

    window = candles[-60:] if len(candles) >= 60 else candles
    info = check_snap_structure(window)
    if info is None:
        return None, "ema unavailable"

    ema9 = info["ema9"]
    ema20 = info["ema20"]
    ema40 = info["ema40"]
    avg_r = info["avg_range"]

    curr = window[-1]
    e9_now = ema9[-1]
    e20_now = ema20[-1]
    e40_now = ema40[-1]

    range_curr = max(curr["high"] - curr["low"], 1e-12)
    body_curr = abs(curr["close"] - curr["open"])
    body_ratio = body_curr / range_curr

    diag = (
        f"e9={format_price(e9_now)} e20={format_price(e20_now)} "
        f"body_r={body_ratio:.2f} bull={info['bull_snap_trend']} bear={info['bear_snap_trend']}"
    )

    if range_curr < avg_r * 0.55 or body_ratio < 0.25:
        return None, f"{diag} doji_or_tiny_candle"

    if abs(curr["close"] - e9_now) > avg_r * 1.8:
        return None, f"{diag} overextended"

    if info["bull_snap_trend"] and curr["close"] > e9_now > e20_now > e40_now:
        no_deep_pullback = curr["low"] > e20_now and curr["close"] > e20_now
        shallow_touch_e9 = curr["low"] <= e9_now + (avg_r * 0.25)
        lower_wick = max(0.0, min(curr["open"], curr["close"]) - curr["low"])
        has_bottom_rejection = lower_wick >= (range_curr * 0.22) and (lower_wick >= body_curr * 0.45)

        if no_deep_pullback and shallow_touch_e9 and has_bottom_rejection:
            win_prob = calc_win_probability_snap("CALL", ema9, ema20, ema40, curr, avg_r)
            confidence = "🟢 TRÈ SOLID (Bon anpil)" if win_prob >= 75 else "🟡 RISKE (Fè pridan)"
            return {
                "direction": "CALL",
                "confidence": confidence,
                "win_prob": win_prob,
                "score": 6 if win_prob >= 75 else 5,
                "close": curr["close"],
                "candle_time": curr["time"]
            }, f"{diag} CALL_SNAP_60S"

    if info["bear_snap_trend"] and curr["close"] < e9_now < e20_now < e40_now:
        no_deep_pullback = curr["high"] < e20_now and curr["close"] < e20_now
        shallow_touch_e9 = curr["high"] >= e9_now - (avg_r * 0.25)
        upper_wick = max(0.0, curr["high"] - max(curr["open"], curr["close"]))
        has_top_rejection = upper_wick >= (range_curr * 0.22) and (upper_wick >= body_curr * 0.45)

        if no_deep_pullback and shallow_touch_e9 and has_top_rejection:
            win_prob = calc_win_probability_snap("PUT", ema9, ema20, ema40, curr, avg_r)
            confidence = "🟢 TRÈ SOLID (Bon anpil)" if win_prob >= 75 else "🟡 RISKE (Fè pridan)"
            return {
                "direction": "PUT",
                "confidence": confidence,
                "win_prob": win_prob,
                "score": 6 if win_prob >= 75 else 5,
                "close": curr["close"],
                "candle_time": curr["time"]
            }, f"{diag} PUT_SNAP_60S"

    return None, f"{diag} no_momentum_snap"


# =====================================================
# 4. FORMAT MESAJ TELEGRAM SENP & RÈG WIN SÈLMAN
# =====================================================
def format_signal_message(pair, payout, entry_time_str, signal_data):
    emoji = "🟢" if signal_data["direction"] == "CALL" else "🔴"
    return (
        f"🤖 ROMARIO PRO M1 (SNAP 60S)\n\n"
        f"{emoji} {signal_data['direction']} | {pair} ({payout:.0f}%)\n"
        f"📊 Nivo: {signal_data['win_prob']}%\n"
        f"🎯 Antre: {entry_time_str} (Exp: 60s)\n"
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
        symbol = (item.get("symbol") or "").strip()
        if not symbol or symbol in seen:
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
    log(f"Scanning {len(eligible)} eligible OTC pairs (>= {MIN_PAYOUT:.0f}%)...")

    def fetch_and_score(item):
        candles = get_candles(item["symbol"])
        setup_score = score_snap_structure(candles) if candles else 0.0
        enriched = dict(item)
        enriched["setup_score"] = setup_score
        return enriched, candles

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_and_score, item): item for item in eligible}
        for future in as_completed(futures):
            item = futures[future]
            try:
                enriched, candles = future.result()
                if candles:
                    histories[enriched["symbol"]] = candles
                    ranked.append(enriched)
            except Exception:
                pass

    ranked.sort(key=lambda x: (-x["setup_score"], -x["payout"], x["symbol"]))
    return histories, ranked


def finalize_pending_trade(pending, last_prices):
    sym = pending["symbol"]
    entry_price = pending["entry_price"] if pending["entry_price"] is not None else pending["last_price"]
    exit_price = pending["last_price"] if pending["last_price"] is not None else last_prices.get(sym)

    if entry_price is not None and exit_price is not None:
        is_win = (
            (pending["direction"] == "CALL" and exit_price > entry_price) or
            (pending["direction"] == "PUT" and exit_price < entry_price)
        )
        handle_trade_result(
            sym,
            pending["payout"],
            is_win,
            send_telegram_fn
        )
        log(f"{sym}: 60s trade finished ({'WIN' if is_win else 'LOSS'}). Stopping bot.")
    else:
        log(f"{sym}: Trade window ended. Stopping bot.")


def run_live_stream(monitored, histories, current_bars, deadline):
    symbols = [item["symbol"] for item in monitored]
    params = {"venue": VENUE, "symbol": ",".join(symbols)}

    log(f"Opening M1 Momentum Snap stream for top {len(symbols)} pairs: {', '.join(symbols)}")
    response = api_get("/v1/stream", params=params, stream=True)

    connected_symbols = set()
    last_prices = {}
    signals_sent = 0
    pending = None
    event_name = ""

    try:
        for raw_line in response.iter_lines(decode_unicode=True, chunk_size=1):
            now = time.time()

            if pending is not None:
                if pending["entry_price"] is None and now >= pending["entry_time"] and pending["last_price"] is not None:
                    pending["entry_price"] = pending["last_price"]
                    log(f"{pending['symbol']}: Entry set at {format_price(pending['entry_price'])}")

                if now >= pending["expiry_time"]:
                    finalize_pending_trade(pending, last_prices)
                    return "stop"

            if signals_sent >= MAX_SIGNALS_PER_RUN and pending is None:
                log("Single signal completed. Stopping bot.")
                return "stop"

            if pending is None and now >= deadline:
                log("Scan window ended with no signal.")
                return "stop"

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

            last_prices[symbol] = price
            now = time.time()

            if pending is not None:
                if symbol == pending["symbol"]:
                    pending["last_price"] = price
                    if pending["entry_price"] is None and now >= pending["entry_time"]:
                        pending["entry_price"] = price
                        log(f"{symbol}: Entry set at {format_price(price)}")

                if now >= pending["expiry_time"]:
                    finalize_pending_trade(pending, last_prices)
                    return "stop"
                continue

            if signals_sent >= MAX_SIGNALS_PER_RUN:
                log("Single signal limit reached. Stopping bot.")
                return "stop"

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
                    signal_data, diag = evaluate_real_market(window)
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
                                "entry_price": None,
                                "last_price": price
                            }
                            log(f"{symbol}: Single 60s snap signal sent ({signal_data['direction']}, {signal_data['win_prob']}%) -> Entry {entry_time_str}. Waiting {EXPIRATION}s then stopping.")
                    else:
                        log(f"{symbol}: no setup ({diag})")

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

    return "stop" if signals_sent >= MAX_SIGNALS_PER_RUN else "reconnect"


def main():
    log(f"{VERSION} starting...")
    if not API_KEY or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Credentials missing!")
        sys.exit(1)

    eligible = get_eligible_symbols()
    if not eligible:
        log("No pairs >= 85% open right now.")
        sys.exit(0)

    histories, ranked = download_all_histories(eligible)
    if not ranked:
        log("No candle history available.")
        sys.exit(0)

    active_limit = min(MAX_STREAM_PAIRS, len(ranked))
    top_pairs = ranked[:active_limit]
    log("Top selected snap pairs: " + ", ".join(f"{x['symbol']}(score={x['setup_score']})" for x in top_pairs))

    current_bars = {}
    deadline = time.time() + MAX_WAIT_SECONDS

    reconnects = 0
    while time.time() < deadline:
        stream_subset = ranked[:active_limit]
        try:
            status = run_live_stream(stream_subset, histories, current_bars, deadline)
            if status in ("done", "stop"):
                log(f"{VERSION} finished.")
                sys.stdout.flush()
                os._exit(0)
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
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
