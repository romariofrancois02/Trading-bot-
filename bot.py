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
# 1. PARAMÈT JENERAL (S30, EXP 30S, PAYOUT >= 85%, 1 SIGNAL/RUN)
# =====================================================
VERSION = "ROMARIO PRO S30 - TRENDLINE ADV"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 30            # Bouji 30 segonn
EXPIRATION = 30           # Ekspirasyon 30 segonn
MIN_PAYOUT = 85.0         # Mache 85% ou plis
ENTRY_LEAD = 6            # Avèti w 6 segonn anvan bouji 30s lan fèmen
MAX_SIGNALS_PER_RUN = 1   # Kanpe otomatikman aprè 1 sèl siyal
MAX_WAIT_SECONDS = 180    # Chèche yon bon opòtinite nan 1 a 3 minit
MAX_STREAM_PAIRS = 4      # 4 pi bon pè yo pou /v1/stream

CANDLE_LIMIT = 80
MIN_CANDLES = 30
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
            "User-Agent": "RomarioTrendLineAdvS30/1.0",
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
# 2. KALKIL RSI AK TREND LINE ADVANCED (AJISTE 30S)
# =====================================================
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


def fit_trendline(points):
    n = len(points)
    if n < 2:
        return None

    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)

    denom = (n * sum_xx) - (sum_x * sum_x)
    if denom == 0:
        return None

    slope = ((n * sum_xy) - (sum_x * sum_y)) / denom
    intercept = (sum_y - (slope * sum_x)) / n
    return slope, intercept


def build_trendlines(window):
    base = window[:-1]
    if len(base) < 12:
        return None, None

    low_pivots = []
    high_pivots = []

    for i in range(1, len(base) - 1):
        if base[i]["low"] <= base[i - 1]["low"] and base[i]["low"] <= base[i + 1]["low"]:
            low_pivots.append((i, base[i]["low"]))
        if base[i]["high"] >= base[i - 1]["high"] and base[i]["high"] >= base[i + 1]["high"]:
            high_pivots.append((i, base[i]["high"]))

    if len(low_pivots) < 2:
        sorted_lows = sorted(
            [(i, base[i]["low"]) for i in range(max(0, len(base) - 16), len(base))],
            key=lambda t: t[1]
        )[:2]
        low_pivots = sorted(sorted_lows, key=lambda t: t[0])
    else:
        low_pivots = low_pivots[-3:]

    if len(high_pivots) < 2:
        sorted_highs = sorted(
            [(i, base[i]["high"]) for i in range(max(0, len(base) - 16), len(base))],
            key=lambda t: t[1],
            reverse=True
        )[:2]
        high_pivots = sorted(sorted_highs, key=lambda t: t[0])
    else:
        high_pivots = high_pivots[-3:]

    last_idx = len(window) - 1
    support_line = None
    resistance_line = None

    fit_sup = fit_trendline(low_pivots)
    if fit_sup is not None:
        sup_slope, sup_intercept = fit_sup
        sup_val = (sup_slope * last_idx) + sup_intercept
        support_line = {
            "slope": sup_slope,
            "value": sup_val,
            "pivots": len(low_pivots)
        }

    fit_res = fit_trendline(high_pivots)
    if fit_res is not None:
        res_slope, res_intercept = fit_res
        res_val = (res_slope * last_idx) + res_intercept
        resistance_line = {
            "slope": res_slope,
            "value": res_val,
            "pivots": len(high_pivots)
        }

    return support_line, resistance_line


def calc_win_probability(direction, rsi, line_info, curr, line_val):
    prob = 50.0

    if line_info is not None:
        if direction == "CALL" and line_info["slope"] > 0:
            prob += 12.0
        elif direction == "PUT" and line_info["slope"] < 0:
            prob += 12.0
        else:
            prob += 4.0

        if line_info.get("pivots", 0) >= 2:
            prob += 10.0

    if direction == "CALL" and curr["close"] > line_val:
        prob += 12.0
    elif direction == "PUT" and curr["close"] < line_val:
        prob += 12.0

    if direction == "CALL":
        if 42 <= rsi <= 65:
            prob += 14.0
        else:
            prob += 6.0
    else:
        if 35 <= rsi <= 58:
            prob += 14.0
        else:
            prob += 6.0

    prob = max(50.0, min(98.0, prob))
    return int(round(prob))


def score_trendline_structure(candles):
    if len(candles) < MIN_CANDLES:
        return 0.0

    window = candles[-40:] if len(candles) >= 40 else candles[-25:]
    closes = [c["close"] for c in window]
    rsi = calc_rsi(closes, 7)
    support_line, resistance_line = build_trendlines(window)
    curr = window[-1]
    prev = window[-2]

    avg_range = sum(max(c["high"] - c["low"], 1e-12) for c in window[-12:]) / 12.0
    tol = avg_range * 0.50

    score = 0.0
    if support_line is not None:
        score += 15.0 + (support_line.get("pivots", 0) * 5.0)
        if support_line["slope"] > 0:
            score += 12.0
        if abs(curr["close"] - support_line["value"]) <= tol * 1.5:
            score += 10.0

    if resistance_line is not None:
        score += 15.0 + (resistance_line.get("pivots", 0) * 5.0)
        if resistance_line["slope"] < 0:
            score += 12.0
        if abs(curr["close"] - resistance_line["value"]) <= tol * 1.5:
            score += 10.0

    if rsi is not None and 25 <= rsi <= 75:
        score += 8.0

    if prev["close"] != prev["open"]:
        score += 3.0

    return round(score, 2)


# =====================================================
# 3. ESTRATEJI "TREND LINE ADVANCED" SÈLMAN (30S)
# =====================================================
def evaluate_real_market(candles):
    if len(candles) < MIN_CANDLES:
        return None, "not enough candles"

    window = candles[-40:] if len(candles) >= 40 else candles[-25:]
    closes = [c["close"] for c in window]
    rsi = calc_rsi(closes, 7)
    if rsi is None:
        return None, "rsi unavailable"

    support_line, resistance_line = build_trendlines(window)
    prev = window[-2]
    curr = window[-1]

    avg_range = sum(max(c["high"] - c["low"], 1e-12) for c in window[-12:]) / 12.0
    tol = avg_range * 0.50

    sup_slope = support_line["slope"] if support_line else 0.0
    res_slope = resistance_line["slope"] if resistance_line else 0.0
    diag = f"RSI={rsi:.1f} sup_m={sup_slope:.2e} res_m={res_slope:.2e}"

    direction = None
    win_prob = 50

    # ---------------- CALL: TREND LINE SIPÒ (30S) ----------------
    if support_line is not None and support_line["slope"] >= 0:
        sup_val = support_line["value"]
        near_sup = (
            prev["low"] <= sup_val + tol
            or prev["close"] <= sup_val + tol
            or curr["low"] <= sup_val + tol
        )
        bullish_move = (curr["close"] > curr["open"]) and (curr["close"] >= sup_val - (tol * 0.4))

        if near_sup and bullish_move and 20 <= rsi <= 72:
            direction = "CALL"
            win_prob = calc_win_probability("CALL", rsi, support_line, curr, sup_val)

    # ---------------- PUT: TREND LINE REZISTANS (30S) ----------------
    if direction is None and resistance_line is not None and resistance_line["slope"] <= 0:
        res_val = resistance_line["value"]
        near_res = (
            prev["high"] >= res_val - tol
            or prev["close"] >= res_val - tol
            or curr["high"] >= res_val - tol
        )
        bearish_move = (curr["close"] < curr["open"]) and (curr["close"] <= res_val + (tol * 0.4))

        if near_res and bearish_move and 28 <= rsi <= 80:
            direction = "PUT"
            win_prob = calc_win_probability("PUT", rsi, resistance_line, curr, res_val)

    if direction is None:
        return None, diag

    if win_prob >= 75:
        confidence = "🟢 TRÈ SOLID (Bon anpil)"
        score = 6
    else:
        confidence = "🟡 RISKE (Fè pridan)"
        score = 5

    return {
        "direction": direction,
        "confidence": confidence,
        "win_prob": win_prob,
        "score": score,
        "rsi": round(rsi, 2),
        "close": curr["close"],
        "candle_time": curr["time"]
    }, diag


# =====================================================
# 4. FORMAT MESAJ TELEGRAM SENP & RÈG WIN SÈLMAN
# =====================================================
def format_signal_message(pair, payout, entry_time_str, signal_data):
    emoji = "🟢" if signal_data["direction"] == "CALL" else "🔴"
    return (
        f"🤖 ROMARIO PRO S30\n\n"
        f"{emoji} {signal_data['direction']} | {pair} ({payout:.0f}%)\n"
        f"📊 Nivo: {signal_data['win_prob']}%\n"
        f"🎯 Antre: {entry_time_str} (Exp: 30s)\n"
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
    log(f"Scanning {len(eligible)} eligible OTC pairs (>= {MIN_PAYOUT:.0f}%)...")

    def fetch_and_score(item):
        candles = get_candles(item["symbol"])
        setup_score = score_trendline_structure(candles) if candles else 0.0
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
        log(f"{sym}: Trade finished ({'WIN' if is_win else 'LOSS'}). Stopping bot.")
    else:
        log(f"{sym}: Trade window ended. Stopping bot.")


def run_live_stream(monitored, histories, current_bars, deadline):
    symbols = [item["symbol"] for item in monitored]
    params = {"venue": VENUE, "symbol": ",".join(symbols)}

    log(f"Opening S30 stream for top {len(symbols)} pairs: {', '.join(symbols)}")
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
                            log(f"{symbol}: Single signal sent ({signal_data['direction']}, {signal_data['win_prob']}%) -> Entry {entry_time_str}. Waiting {EXPIRATION}s then stopping.")
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
    log("Top selected pairs: " + ", ".join(f"{x['symbol']}(score={x['setup_score']})" for x in top_pairs))

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
