import os
import sys
import json
import time
import logging
import threading
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# =====================================================
# 1. PARAMÈT JENERAL - ROMARIO PRO (M1, EXP 60S, UTC-5)
# =====================================================
VERSION = "ROMARIO PRO"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 60            # Graf 1 minit (M1)
EXPIRATION = 60           # Ekspirasyon 60 segonn (1 minit)
MIN_PAYOUT = 85.0         # Minimòm 85% payout sou OTC
ENTRY_LEAD = 10           # Avèti w 10 segonn anvan nouvo bouji a ouvè
MAX_SIGNALS_PER_RUN = 1   # Kanpe otomatikman aprè 1 sèl siyal
MAX_WAIT_SECONDS = 300    # Chèche yon opòtinite nan 5 minit max
MAX_STREAM_PAIRS = 4      # Top 4 pi bon pè yo pou /v1/stream

CANDLE_LIMIT = 90
MIN_CANDLES = 50
MAX_RECONNECTS = 5

# Evite vye mache ekzotik ki gen gwo twou / spik brital sou 60s
TOXIC_CURRENCIES = ("TRY", "ZAR", "MXN", "BRL", "PLN", "RUB", "INR", "CNH", "THB", "HUF", "SEK", "NOK")

API_CONNECT_TIMEOUT = 4
API_READ_TIMEOUT = 10

TELEGRAM_ATTEMPTS = 3
TELEGRAM_TIMEOUT = (1, 2)

# Lè egzak itilizatè a: UTC-5
USER_TZ = timezone(timedelta(hours=-5))

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
            "User-Agent": "RomarioProM1/2.5",
            "Accept": "application/json"
        })
    return _thread_local.session


def format_price(value):
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def is_clean_pair(symbol):
    up = symbol.upper()
    return not any(code in up for code in TOXIC_CURRENCIES)


def compute_utc5_entry_timestamp(seconds_left_in_bar):
    """
    Kalkile timestamp pwochen ouvèti bouji 60s la apati lè UTC-5 aktyèl la.
    """
    rem = max(1, min(TIMEFRAME, int(round(seconds_left_in_bar))))
    now_utc5_epoch = datetime.now(USER_TZ).timestamp()
    next_minute_utc5 = ((int(now_utc5_epoch) // TIMEFRAME) + 1) * TIMEFRAME
    if abs(next_minute_utc5 - (now_utc5_epoch + rem)) <= 3:
        return next_minute_utc5
    return int(round(now_utc5_epoch + rem))


def api_get(path, params=None, stream=False):
    url = BASE_URL + path
    attempts = 1 if stream else 4
    backoffs = (0.8, 1.5, 2.5)

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

            if response.status_code in (502, 503, 504):
                status_code = response.status_code
                response.close()
                if attempt < attempts:
                    delay = backoffs[min(attempt - 1, len(backoffs) - 1)]
                    log(f"HTTP {status_code} on {path}. Retrying in {delay:.1f}s ({attempt}/{attempts})...")
                    time.sleep(delay)
                    continue
                raise requests.HTTPError(f"{status_code} Server Error for url: {url}")

            if response.status_code in (401, 402, 403, 500):
                status_code = response.status_code
                response.close()
                if attempt < 2:
                    time.sleep(0.4)
                    continue
                raise requests.HTTPError(f"{status_code} Server Error for url: {url}")

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
            delay = backoffs[min(attempt - 1, len(backoffs) - 1)]
            time.sleep(delay)

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
# 2. KALKIL EMA 9, EMA 20, EMA 40 ("MOMENTUM SNAP" 60S)
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


def check_snap_structure(window):
    closes = [c["close"] for c in window]
    ema9 = align_ema_series(closes, 9)
    ema20 = align_ema_series(closes, 20)
    ema40 = align_ema_series(closes, 40)

    if any(x is None for x in (ema9[-1], ema9[-2], ema9[-3], ema20[-1], ema20[-2], ema40[-1], ema40[-2])):
        return None

    avg_range = sum(max(c["high"] - c["low"], 1e-12) for c in window[-10:]) / 10.0
    min_gap = max(avg_range * 0.22, 1e-12)

    gap_9_20_now = abs(ema9[-1] - ema20[-1])
    gap_20_40_now = abs(ema20[-1] - ema40[-1])
    gap_9_20_prev = abs(ema9[-2] - ema20[-2])

    expanding_9_20 = gap_9_20_now >= gap_9_20_prev * 0.98

    bull_snap_trend = (
        ema9[-1] > ema20[-1] > ema40[-1]
        and gap_9_20_now >= min_gap
        and gap_20_40_now >= min_gap
        and expanding_9_20
        and ema9[-1] > ema9[-2]
        and ema20[-1] > ema20[-2]
        and ema40[-1] > ema40[-2]
    )

    bear_snap_trend = (
        ema9[-1] < ema20[-1] < ema40[-1]
        and gap_9_20_now >= min_gap
        and gap_20_40_now >= min_gap
        and expanding_9_20
        and ema9[-1] < ema9[-2]
        and ema20[-1] < ema20[-2]
        and ema40[-1] < ema40[-2]
    )

    return {
        "ema9": ema9,
        "ema20": ema20,
        "ema40": ema40,
        "avg_range": avg_range,
        "gap_9_20_now": gap_9_20_now,
        "gap_9_20_prev": gap_9_20_prev,
        "expanding_9_20": expanding_9_20,
        "bull_snap_trend": bull_snap_trend,
        "bear_snap_trend": bear_snap_trend
    }


def calc_setup_and_win_rate(direction, bar, e9, e20, avg_r, gap_now, gap_prev, wick_ratio, body_ratio):
    score = 0.0
    win_pct = 55.0

    if gap_now > gap_prev:
        score += 25.0
        win_pct += 11.0
    if gap_now >= avg_r * 0.30:
        score += 10.0
        win_pct += 5.0

    if wick_ratio >= 0.32:
        score += 25.0
        win_pct += 10.0
    elif wick_ratio >= 0.18:
        score += 15.0
        win_pct += 6.0

    if body_ratio >= 0.42:
        score += 20.0
        win_pct += 6.0
    elif body_ratio >= 0.22:
        score += 10.0
        win_pct += 3.0

    dist_e9 = abs(bar["close"] - e9)
    if dist_e9 <= avg_r * 0.9:
        score += 20.0
        win_pct += 5.0
    elif dist_e9 <= avg_r * 1.6:
        score += 10.0
        win_pct += 2.0

    if direction == "CALL" and bar["close"] > e20:
        win_pct += 2.0
    elif direction == "PUT" and bar["close"] < e20:
        win_pct += 2.0

    setup_score = round(min(100.0, score), 1)
    win_prob = int(round(max(50.0, min(92.0, win_pct))))
    return setup_score, win_prob


# =====================================================
# 3. EVALYASYON 10S ANVAN FÈMTI BOUJI A (MOMENTUM SNAP 60S)
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

    bar = window[-1]
    e9_now = ema9[-1]
    e20_now = ema20[-1]
    e40_now = ema40[-1]

    range_bar = max(bar["high"] - bar["low"], 1e-12)
    body_bar = abs(bar["close"] - bar["open"])
    body_ratio = body_bar / range_bar

    diag = (
        f"e9={format_price(e9_now)} e20={format_price(e20_now)} "
        f"body_r={body_ratio:.2f} bull={info['bull_snap_trend']} bear={info['bear_snap_trend']}"
    )

    if range_bar < avg_r * 0.45 or body_ratio < 0.20:
        return None, f"{diag} doji_or_tiny_bar"

    if abs(bar["close"] - e9_now) > avg_r * 1.9:
        return None, f"{diag} overextended"

    if info["bull_snap_trend"] and bar["close"] > e9_now > e20_now > e40_now:
        no_deep_pullback = bar["low"] > e20_now and bar["close"] > e20_now
        shallow_touch_e9 = bar["low"] <= e9_now + (avg_r * 0.30)
        lower_wick = max(0.0, min(bar["open"], bar["close"]) - bar["low"])
        wick_ratio = lower_wick / range_bar
        has_bottom_rejection = lower_wick >= (range_bar * 0.18) and (lower_wick >= body_bar * 0.35)

        if no_deep_pullback and shallow_touch_e9 and has_bottom_rejection:
            setup_score, win_prob = calc_setup_and_win_rate(
                "CALL", bar, e9_now, e20_now, avg_r,
                info["gap_9_20_now"], info["gap_9_20_prev"], wick_ratio, body_ratio
            )
            confidence = "🟢 TRÈ SOLID" if setup_score >= 70 else "🟡 MODERE"
            return {
                "direction": "CALL",
                "setup_score": setup_score,
                "win_prob": win_prob,
                "confidence": confidence,
                "ref_quote": bar["close"],
                "candle_time": bar["time"]
            }, f"{diag} CALL_SNAP_60S score={setup_score} win={win_prob}%"

    if info["bear_snap_trend"] and bar["close"] < e9_now < e20_now < e40_now:
        no_deep_pullback = bar["high"] < e20_now and bar["close"] < e20_now
        shallow_touch_e9 = bar["high"] >= e9_now - (avg_r * 0.30)
        upper_wick = max(0.0, bar["high"] - max(bar["open"], bar["close"]))
        wick_ratio = upper_wick / range_bar
        has_top_rejection = upper_wick >= (range_bar * 0.18) and (upper_wick >= body_bar * 0.35)

        if no_deep_pullback and shallow_touch_e9 and has_top_rejection:
            setup_score, win_prob = calc_setup_and_win_rate(
                "PUT", bar, e9_now, e20_now, avg_r,
                info["gap_9_20_now"], info["gap_9_20_prev"], wick_ratio, body_ratio
            )
            confidence = "🟢 TRÈ SOLID" if setup_score >= 70 else "🟡 MODERE"
            return {
                "direction": "PUT",
                "setup_score": setup_score,
                "win_prob": win_prob,
                "confidence": confidence,
                "ref_quote": bar["close"],
                "candle_time": bar["time"]
            }, f"{diag} PUT_SNAP_60S score={setup_score} win={win_prob}%"

    return None, f"{diag} no_momentum_snap"


def score_setup_quality(candles):
    sig, _ = evaluate_real_market(candles)
    if sig is not None:
        return round(100.0 + sig["setup_score"], 2)

    if len(candles) < MIN_CANDLES:
        return 0.0

    window = candles[-60:] if len(candles) >= 60 else candles
    info = check_snap_structure(window)
    if info is None:
        return 0.0

    bar = window[-1]
    e9 = info["ema9"][-1]
    avg_r = info["avg_range"]

    quality = 0.0
    if info["bull_snap_trend"] or info["bear_snap_trend"]:
        quality += 35.0
    if info["expanding_9_20"]:
        quality += 15.0
    if abs(bar["close"] - e9) <= avg_r * 1.3:
        quality += 15.0

    return round(quality, 2)


# =====================================================
# 4. FORMAT MESAJ TELEGRAM SENP & VERIFIKASYON OTCHARTS 60S
# =====================================================
def format_signal_message(pair, payout, entry_time_str, signal_data, entry_quote=None):
    emoji = "🟢" if signal_data["direction"] == "CALL" else "🔴"
    quote_str = format_price(entry_quote) if entry_quote is not None else format_price(signal_data["ref_quote"])
    return (
        f"🤖 {VERSION}\n\n"
        f"{emoji} {signal_data['direction']} | {pair} ({payout:.0f}%)\n"
        f"📊 Win: {signal_data['win_prob']}%\n"
        f"🎯 Kot: {quote_str}\n"
        f"🕒 Antre: {entry_time_str} (Exp: 60s)\n"
        f"🛡️ Konfyans: {signal_data['confidence']}"
    )


def handle_otcharts_60s_result(pair, payout, is_win, entry_quote, exit_quote, send_fn):
    if is_win:
        msg = (
            f"✅ REZILTA (OTCharts 60s): WIN | {pair} (+{payout:.0f}%)\n"
            f"Kot: {format_price(entry_quote)} -> {format_price(exit_quote)}"
        )
        send_fn(msg)


def get_eligible_symbols():
    data = api_get("/v1/symbols", params={"venue": VENUE})
    eligible = []
    seen = set()

    for item in data.get("symbols", []):
        symbol = (item.get("symbol") or "").strip()
        if not symbol or symbol in seen:
            continue
        if not is_clean_pair(symbol):
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
    log(f"Scanning {len(eligible)} clean OTC pairs (>= {MIN_PAYOUT:.0f}%)...")

    def fetch_and_score(item):
        candles = get_candles(item["symbol"])
        setup_score = score_setup_quality(candles) if candles else 0.0
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
    entry_price = pending["entry_price"]
    exit_price = pending["last_price"] if pending["last_price"] is not None else last_prices.get(sym)

    if entry_price is not None and exit_price is not None:
        is_win = (
            (pending["direction"] == "CALL" and exit_price > entry_price) or
            (pending["direction"] == "PUT" and exit_price < entry_price)
        )
        handle_otcharts_60s_result(
            sym,
            pending["payout"],
            is_win,
            entry_price,
            exit_price,
            send_telegram_fn
        )
        log(f"{sym}: OTCharts 60s check -> {'WIN' if is_win else 'LOSS'}. Stopping bot.")
    else:
        log(f"{sym}: Missing entry/exit tick. Stopping bot.")


def run_live_stream(monitored, histories, current_bars, deadline):
    symbols = [item["symbol"] for item in monitored]
    params = {"venue": VENUE, "symbol": ",".join(symbols)}

    log(f"Opening M1 stream for top {len(symbols)} pairs: {', '.join(symbols)}")
    response = api_get("/v1/stream", params=params, stream=True)

    connected_symbols = set()
    last_prices = {}
    signals_sent = 0
    pending = None
    event_name = ""

    try:
        for raw_line in response.iter_lines(decode_unicode=True, chunk_size=1):
            now = datetime.now(USER_TZ).timestamp()

            if pending is not None:
                if pending["entry_price"] is None and now >= pending["entry_time"] and pending["last_price"] is not None:
                    pending["entry_price"] = pending["last_price"]
                    log(f"{pending['symbol']}: Entry price at entry time -> {format_price(pending['entry_price'])}")

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
            now = datetime.now(USER_TZ).timestamp()

            if pending is not None:
                if symbol == pending["symbol"]:
                    pending["last_price"] = price
                    if pending["entry_price"] is None and now >= pending["entry_time"]:
                        pending["entry_price"] = price
                        log(f"{symbol}: Entry price at entry time -> {format_price(price)}")

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
                        entry_ts = compute_utc5_entry_timestamp(seconds_left_in_bar)
                        entry_time_str = datetime.fromtimestamp(entry_ts, tz=USER_TZ).strftime("%I:%M:%S %p")

                        msg = format_signal_message(symbol, payout, entry_time_str, signal_data, entry_quote=price)
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
                            log(f"{symbol}: Signal sent ({signal_data['direction']}, win={signal_data['win_prob']}%) -> Entry {entry_time_str} (UTC-5).")
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


def fetch_eligible_with_retry(deadline):
    while True:
        try:
            return get_eligible_symbols()
        except Exception as e:
            remaining = deadline - datetime.now(USER_TZ).timestamp()
            if remaining <= 3.0:
                raise
            wait_sec = min(4.0, max(1.0, remaining - 1.0))
            log(f"Symbols fetch failed ({e}). Retrying in {wait_sec:.1f}s...")
            time.sleep(wait_sec)


def main():
    log(f"{VERSION} starting...")
    if not API_KEY or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Credentials missing!")
        sys.exit(1)

    deadline = datetime.now(USER_TZ).timestamp() + MAX_WAIT_SECONDS

    try:
        eligible = fetch_eligible_with_retry(deadline)
    except Exception as e:
        log(f"Unable to load eligible symbols: {e}")
        sys.exit(0)

    if not eligible:
        log(f"No clean pairs >= {MIN_PAYOUT:.0f}% open right now.")
        sys.exit(0)

    histories, ranked = download_all_histories(eligible)
    if not ranked:
        log("No candle history available.")
        sys.exit(0)

    active_limit = min(MAX_STREAM_PAIRS, len(ranked))
    top_pairs = ranked[:active_limit]
    log("Top selected pairs: " + ", ".join(f"{x['symbol']}(score={x['setup_score']})" for x in top_pairs))

    current_bars = {}

    reconnects = 0
    while datetime.now(USER_TZ).timestamp() < deadline:
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
            rem = deadline - datetime.now(USER_TZ).timestamp()
            wait_sec = min(7.0, max(2.0, rem))
            log(f"Reconnecting in {wait_sec:.1f}s...")
            time.sleep(wait_sec)
        except StreamBadRequestError as e:
            log(f"Stream error: {e}")
            if active_limit > 1:
                active_limit -= 1
                log(f"Reducing stream batch to {active_limit} pairs and retrying...")
                time.sleep(1.5)
                continue
            break
        except StreamRateLimitError as e:
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                break
            rem = deadline - datetime.now(USER_TZ).timestamp()
            wait_sec = min(e.retry_after, max(0.0, rem))
            if wait_sec <= 2:
                break
            log(f"Rate limit. Waiting {wait_sec:.1f}s...")
            time.sleep(wait_sec)
        except Exception as e:
            log(f"Stream error: {e}")
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                break
            rem = deadline - datetime.now(USER_TZ).timestamp()
            wait_sec = min(7.0, max(2.0, rem))
            if wait_sec <= 1:
                break
            time.sleep(wait_sec)

    log(f"{VERSION} finished.")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
