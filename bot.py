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
# ROMARIO TRADER PRO - TREND LINE ADVANCED ONLY
# M1 | EXP 1M | 85%+ PAYOUT | 1 SIGNAL / RUN
# =====================================================

VERSION = "ROMARIO PRO M1 TREND LINE ADV 2.0"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 60
EXPIRATION = 60
MIN_PAYOUT = 85.0

ENTRY_LEAD = 12
MAX_SIGNALS_PER_RUN = 1
MAX_WAIT_SECONDS = 180
MAX_STREAM_PAIRS = 4

CANDLE_LIMIT = 80
MIN_CANDLES = 30
MAX_RECONNECTS = 4

VALID_PAIRS = {
    "EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDUSD_otc",
    "USDCAD_otc", "USDCHF_otc", "NZDUSD_otc", "EURGBP_otc",
    "EURJPY_otc", "GBPJPY_otc", "AUDCHF_otc", "EURCHF_otc",
    "AUDCAD_otc", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"
}

API_CONNECT_TIMEOUT = 4
API_READ_TIMEOUT = 10

TELEGRAM_ATTEMPTS = 3
TELEGRAM_TIMEOUT = (3, 5)

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
            "User-Agent": "RomarioTrendLineAdv/2.0",
            "Accept": "application/json"
        })
    return _thread_local.session


def format_price(value):
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


# =====================================================
# API
# =====================================================

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
                body = response.text[:160]
                response.close()
                raise StreamBadRequestError(body)

            if response.status_code == 429:
                retry_hdr = response.headers.get("Retry-After", "30")
                try:
                    retry_after = max(25, int(float(retry_hdr)))
                except (TypeError, ValueError):
                    retry_after = 30

                response.close()
                raise StreamRateLimitError(
                    "Rate limit", retry_after
                )

            if response.status_code in (401, 402, 403, 500, 502, 503, 504):
                if attempt < attempts:
                    response.close()
                    time.sleep(0.5)
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

            time.sleep(0.5)

    raise RuntimeError("API request failed")


# =====================================================
# TELEGRAM
# =====================================================

def send_telegram_fn(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    for attempt in range(1, TELEGRAM_ATTEMPTS + 1):
        response = None

        try:
            response = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message
                },
                timeout=TELEGRAM_TIMEOUT
            )

            if response.ok:
                return True

            log(f"Telegram HTTP error: {response.status_code}")

        except Exception as e:
            log(f"Telegram attempt {attempt}: {e}")

        finally:
            if response is not None:
                response.close()

        time.sleep(0.5)

    return False


# =====================================================
# RSI 7
# =====================================================

def calc_rsi(closes, period=7):
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1)) + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + losses[i]
        ) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (100.0 / (1.0 + rs))


# =====================================================
# TRENDLINE MATHEMATICS
# =====================================================

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

    slope = (
        (n * sum_xy) - (sum_x * sum_y)
    ) / denom

    intercept = (sum_y - (slope * sum_x)) / n

    return slope, intercept


def build_trendlines(window):
    # Dènye bouji a se bouji k ap fòme.
    # Nou pa sèvi avè l pou jwenn pivot.
    base = window[:-1]

    if len(base) < 12:
        return None, None

    low_pivots = []
    high_pivots = []

    for i in range(1, len(base) - 1):

        if (
            base[i]["low"] <= base[i - 1]["low"]
            and base[i]["low"] <= base[i + 1]["low"]
        ):
            low_pivots.append(
                (i, base[i]["low"])
            )

        if (
            base[i]["high"] >= base[i - 1]["high"]
            and base[i]["high"] >= base[i + 1]["high"]
        ):
            high_pivots.append(
                (i, base[i]["high"])
            )

    # Fallback pou jwenn 2 pivot low
    if len(low_pivots) < 2:

        start = max(0, len(base) - 16)

        sorted_lows = sorted(
            [
                (i, base[i]["low"])
                for i in range(start, len(base))
            ],
            key=lambda t: t[1]
        )[:2]

        low_pivots = sorted(
            sorted_lows,
            key=lambda t: t[0]
        )

    else:
        low_pivots = low_pivots[-3:]

    # Fallback pou jwenn 2 pivot high
    if len(high_pivots) < 2:

        start = max(0, len(base) - 16)

        sorted_highs = sorted(
            [
                (i, base[i]["high"])
                for i in range(start, len(base))
            ],
            key=lambda t: t[1],
            reverse=True
        )[:2]

        high_pivots = sorted(
            sorted_highs,
            key=lambda t: t[0]
        )

    else:
        high_pivots = high_pivots[-3:]

    last_idx = len(window) - 1

    support_line = None
    resistance_line = None

    # SUPPORT
    fit_sup = fit_trendline(low_pivots)

    if fit_sup is not None:
        sup_slope, sup_intercept = fit_sup
        sup_val = (
            sup_slope * last_idx
        ) + sup_intercept

        support_line = {
            "slope": sup_slope,
            "value": sup_val,
            "pivots": len(low_pivots)
        }

    # RESISTANCE
    fit_res = fit_trendline(high_pivots)

    if fit_res is not None:
        res_slope, res_intercept = fit_res
        res_val = (
            res_slope * last_idx
        ) + res_intercept

        resistance_line = {
            "slope": res_slope,
            "value": res_val,
            "pivots": len(high_pivots)
        }

    return support_line, resistance_line


# =====================================================
# TREND LINE ADVANCED — SÈL STRATEJI
# =====================================================

def evaluate_real_market(candles):

    if len(candles) < MIN_CANDLES:
        return None, "not enough candles"

    window = candles[-25:]

    closes = [
        c["close"] for c in window
    ]

    rsi = calc_rsi(closes, 7)

    if rsi is None:
        return None, "RSI unavailable"

    support_line, resistance_line = build_trendlines(window)

    prev = window[-2]
    curr = window[-1]

    avg_range = sum(
        max(c["high"] - c["low"], 1e-12)
        for c in window[-10:]
    ) / 10.0

    tolerance = avg_range * 0.65

    sup_slope = (
        support_line["slope"]
        if support_line else 0.0
    )

    res_slope = (
        resistance_line["slope"]
        if resistance_line else 0.0
    )

    diag = (
        f"RSI={rsi:.1f} "
        f"SUP={sup_slope:.3e} "
        f"RES={res_slope:.3e}"
    )

    direction = None
    solid_hit = False

    # -------------------------------------------------
    # CALL — SUPPORT TRENDLINE
    # -------------------------------------------------

    if (
        support_line is not None
        and support_line["slope"] >= 0
    ):

        sup_val = support_line["value"]

        near_support = (
            prev["low"] <= sup_val + tolerance
            or prev["close"] <= sup_val + tolerance
            or curr["low"] <= sup_val + tolerance
        )

        bullish_move = (
            curr["close"] > curr["open"]
            and curr["close"] >= (
                sup_val - (tolerance * 0.5)
            )
        )

        if (
            near_support
            and bullish_move
            and 15 <= rsi <= 75
        ):

            direction = "CALL"

            solid_hit = (
                support_line["slope"] > 0
                and support_line["pivots"] >= 2
                and curr["close"] > sup_val
                and 35 <= rsi <= 68
            )

    # -------------------------------------------------
    # PUT — RESISTANCE TRENDLINE
    # -------------------------------------------------

    if (
        direction is None
        and resistance_line is not None
        and resistance_line["slope"] <= 0
    ):

        res_val = resistance_line["value"]

        near_resistance = (
            prev["high"] >= res_val - tolerance
            or prev["close"] >= res_val - tolerance
            or curr["high"] >= res_val - tolerance
        )

        bearish_move = (
            curr["close"] < curr["open"]
            and curr["close"] <= (
                res_val + (tolerance * 0.5)
            )
        )

        if (
            near_resistance
            and bearish_move
            and 25 <= rsi <= 85
        ):

            direction = "PUT"

            solid_hit = (
                resistance_line["slope"] < 0
                and resistance_line["pivots"] >= 2
                and curr["close"] < res_val
                and 32 <= rsi <= 65
            )

    # -------------------------------------------------
    # NO SIGNAL
    # -------------------------------------------------

    if direction is None:
        return None, diag

    # -------------------------------------------------
    # SIGNAL CLASSIFICATION
    # -------------------------------------------------

    if solid_hit:
        confidence = "🟢 TRÈ SOLID"
        score = 6
    else:
        confidence = "🟡 RISKE"
        score = 5

    return {
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "rsi": round(rsi, 2),
        "close": curr["close"],
        "candle_time": curr["time"]
    }, diag


# =====================================================
# TELEGRAM SIGNAL FORMAT
# =====================================================

def format_signal_message(
    pair,
    payout,
    entry_time_str,
    signal_data
):

    emoji = (
        "🟢"
        if signal_data["direction"] == "CALL"
        else "🔴"
    )

    return (
        f"🤖 ROMARIO TRADER PRO TREND LINE ADV\n\n"
        f"{emoji} {signal_data['direction']}\n"
        f"💱 PAIR: {pair}\n"
        f"⭐ SCORE: {signal_data['score']}/6\n"
        f"🛡️ STATUS: {signal_data['confidence']}\n"
        f"📊 RSI 7: {signal_data['rsi']}\n"
        f"💰 PAYOUT: {payout:.1f}%\n"
        f"⏱ TIMEFRAME: 1M\n"
        f"⌛ EXPIRATION: 1M\n"
        f"🎯 ENTRY: {entry_time_str}\n\n"
        f"📈 TRENDLINE SUPPORT/RESISTANCE\n"
        f"⚠️ Score is not win probability.\n"
        f"Manual entry only. Demo recommended."
    )


# =====================================================
# MARKET LIST
# =====================================================

def get_eligible_symbols():

    data = api_get(
        "/v1/symbols",
        params={"venue": VENUE}
    )

    eligible = []
    seen = set()

    for item in data.get("symbols", []):

        symbol = item.get("symbol")

        if symbol not in VALID_PAIRS:
            continue

        if symbol in seen:
            continue

        if item.get("open") is False:
            continue

        try:
            payout = float(
                item.get("payout", 0.0)
            )
        except (TypeError, ValueError):
            payout = 0.0

        if payout < MIN_PAYOUT:
            continue

        seen.add(symbol)

        eligible.append({
            "symbol": symbol,
            "payout": payout
        })

    eligible.sort(
        key=lambda x: (-x["payout"], x["symbol"])
    )

    return eligible


# =====================================================
# CANDLE HISTORY
# =====================================================

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

    parsed.sort(
        key=lambda x: x["time"]
    )

    if len(parsed) >= MIN_CANDLES:
        return parsed[-CANDLE_LIMIT:]

    return []


def download_all_histories(eligible):

    histories = {}
    ranked = []

    def fetch_history(item):
        return item, get_candles(item["symbol"])

    with ThreadPoolExecutor(max_workers=5) as executor:

        futures = {
            executor.submit(fetch_history, item): item
            for item in eligible
        }

        for future in as_completed(futures):

            item = futures[future]

            try:
                _, candles = future.result()

                if candles:
                    histories[item["symbol"]] = candles
                    ranked.append(item)

            except Exception as e:
                log(
                    f"History failed {item['symbol']}: {e}"
                )

    ranked.sort(
        key=lambda x: (-x["payout"], x["symbol"])
    )

    return histories, ranked


# =====================================================
# TRADE RESULT
# =====================================================

def handle_trade_result(
    pair,
    payout,
    is_win,
    send_fn
):

    if is_win:
        message = (
            f"✅ REZILTA: WIN | {pair} "
            f"(Payout {payout:.0f}%)"
        )
    else:
        message = (
            f"❌ REZILTA: LOSS | {pair}"
        )

    send_fn(message)


def finalize_pending_trade(pending, last_prices):

    symbol = pending["symbol"]

    entry_price = pending.get("entry_price")

    exit_price = pending.get("last_price")

    if exit_price is None:
        exit_price = last_prices.get(symbol)

    if entry_price is not None and exit_price is not None:

        if pending["direction"] == "CALL":
            is_win = exit_price > entry_price
        else:
            is_win = exit_price < entry_price

        handle_trade_result(
            symbol,
            pending["payout"],
            is_win,
            send_telegram_fn
        )

        log(
            f"{symbol}: Trade ended "
            f"{'WIN' if is_win else 'LOSS'}"
        )

    else:
        log(
            f"{symbol}: Unable to determine result "
            f"from available ticks."
        )


# =====================================================
# LIVE STREAM
# =====================================================

def run_live_stream(
    monitored,
    histories,
    current_bars,
    deadline
):

    symbols = [
        item["symbol"] for item in monitored
    ]

    params = {
        "venue": VENUE,
        "symbol": ",".join(symbols)
    }

    log(
        f"Opening stream for {len(symbols)} pairs."
    )

    response = api_get(
        "/v1/stream",
        params=params,
        stream=True
    )

    connected_symbols = set()
    last_prices = {}

    signals_sent = 0
    pending = None
    event_name = ""

    try:

        for raw_line in response.iter_lines(
            decode_unicode=True,
            chunk_size=1
        ):

            now = time.time()

            # -----------------------------
            # MONITOR PENDING TRADE
            # -----------------------------

            if pending is not None:

                if (
                    pending["entry_price"] is None
                    and now >= pending["entry_time"]
                    and pending["last_price"] is not None
                ):

                    pending["entry_price"] = (
                        pending["last_price"]
                    )

                    log(
                        f"{pending['symbol']} entry price: "
                        f"{format_price(pending['entry_price'])}"
                    )

                if now >= pending["expiry_time"]:
                    finalize_pending_trade(
                        pending,
                        last_prices
                    )
                    return "stop"

            # -----------------------------
            # ONE SIGNAL MAX
            # -----------------------------

            if (
                signals_sent >= MAX_SIGNALS_PER_RUN
                and pending is None
            ):
                log("Signal limit reached.")
                return "stop"

            if (
                pending is None
                and now >= deadline
            ):
                log("Scan window ended. No signal.")
                return "stop"

            if not raw_line:
                event_name = ""
                continue

            line = (
                raw_line.decode(
                    "utf-8",
                    errors="replace"
                ).strip()
                if isinstance(raw_line, bytes)
                else raw_line.strip()
            )

            if not line or line.startswith(":"):
                continue

            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue

            if not line.startswith("data:"):
                continue

            try:
                data = json.loads(
                    line[5:].strip()
                )
            except json.JSONDecodeError:
                continue

            # -----------------------------
            # CONNECTION EVENT
            # -----------------------------

            if event_name == "connected":

                connected_symbols = set(
                    data.get("symbols", [])
                )

                log(
                    "Connected: "
                    + ", ".join(
                        sorted(connected_symbols)
                    )
                )

                continue

            if event_name != "tick":
                continue

            symbol = data.get("symbol")

            if (
                connected_symbols
                and symbol not in connected_symbols
            ):
                continue

            try:
                price = float(data["price"])
                feed_time = normalize_ts(data["time"])

            except (KeyError, TypeError, ValueError):
                continue

            last_prices[symbol] = price

            now = time.time()

            # -----------------------------
            # UPDATE ACTIVE TRADE
            # -----------------------------

            if pending is not None:

                if symbol == pending["symbol"]:

                    pending["last_price"] = price

                    if (
                        pending["entry_price"] is None
                        and now >= pending["entry_time"]
                    ):

                        pending["entry_price"] = price

                        log(
                            f"{symbol}: Entry captured "
                            f"{format_price(price)}"
                        )

                if now >= pending["expiry_time"]:

                    finalize_pending_trade(
                        pending,
                        last_prices
                    )

                    return "stop"

                continue

            if (
                signals_sent >= MAX_SIGNALS_PER_RUN
            ):
                return "stop"

            # -----------------------------
            # BUILD CURRENT M1 BAR
            # -----------------------------

            bucket = (
                feed_time // TIMEFRAME
            ) * TIMEFRAME

            if symbol not in current_bars:

                histories[symbol] = [
                    candle
                    for candle in histories.get(
                        symbol, []
                    )
                    if candle["time"] < bucket
                ]

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

                continue

            current = current_bars[symbol]

            seconds_left = (
                current["time"] + TIMEFRAME
            ) - feed_time

            # -----------------------------
            # EVALUATE BEFORE CLOSE
            # -----------------------------

            if (
                0 < seconds_left <= ENTRY_LEAD
                and not current.get(
                    "evaluated_preclose", False
                )
            ):

                current["evaluated_preclose"] = True

                history = histories.get(
                    symbol, []
                )

                if (
                    len(history) >= 1
                    and len(history) + 1 >= MIN_CANDLES
                ):

                    window = (
                        history[-(MIN_CANDLES - 1):]
                        + [current]
                    )

                    signal_data, diag = (
                        evaluate_real_market(window)
                    )

                    if signal_data:

                        pair_info = next(
                            (
                                item
                                for item in monitored
                                if item["symbol"] == symbol
                            ),
                            {"payout": MIN_PAYOUT}
                        )

                        payout = pair_info["payout"]

                        entry_ts = (
                            current["time"] + TIMEFRAME
                        )

                        entry_time_str = (
                            datetime.fromtimestamp(
                                entry_ts,
                                TIMEZONE
                            ).strftime("%I:%M:%S %p")
                        )

                        message = format_signal_message(
                            symbol,
                            payout,
                            entry_time_str,
                            signal_data
                        )

                        if send_telegram_fn(message):

                            signals_sent += 1

                            pending = {
                                "symbol": symbol,
                                "payout": payout,
                                "direction": signal_data["direction"],
                                "entry_time": entry_ts,
                                "expiry_time": (
                                    entry_ts + EXPIRATION
                                ),
                                "entry_price": None,
                                "last_price": price
                            }

                            log(
                                f"{symbol}: Signal sent "
                                f"{signal_data['direction']} "
                                f"| {signal_data['confidence']} "
                                f"| ENTRY {entry_time_str}"
                            )

                    else:
                        log(
                            f"{symbol}: No setup | {diag}"
                        )

            # -----------------------------
            # UPDATE / CLOSE BAR
            # -----------------------------

            if bucket > current["time"]:

                clean_bar = {
                    "time": current["time"],
                    "open": current["open"],
                    "high": current["high"],
                    "low": current["low"],
                    "close": current["close"]
                }

                histories.setdefault(
                    symbol, []
                ).append(clean_bar)

                histories[symbol] = (
                    histories[symbol][-CANDLE_LIMIT:]
                )

                current_bars[symbol] = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price
                }

            else:

                current["high"] = max(
                    current["high"], price
                )

                current["low"] = min(
                    current["low"], price
                )

                current["close"] = price

    finally:
        response.close()

    return (
        "stop"
        if signals_sent >= MAX_SIGNALS_PER_RUN
        else "reconnect"
    )


# =====================================================
# MAIN
# =====================================================

def main():

    log(f"{VERSION} starting...")

    if (
        not API_KEY
        or not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        log("Credentials missing!")
        sys.exit(1)

    try:

        eligible = get_eligible_symbols()

    except Exception as e:

        log(f"Failed loading symbols: {e}")
        sys.exit(1)

    if not eligible:

        log(
            f"No OTC pairs with payout >= "
            f"{MIN_PAYOUT:.0f}% available."
        )

        sys.exit(0)

    log(
        f"Eligible pairs: {len(eligible)}"
    )

    histories, ranked = (
        download_all_histories(eligible)
    )

    if not ranked:

        log("No candle histories available.")
        sys.exit(0)

    active_limit = min(
        MAX_STREAM_PAIRS,
        len(ranked)
    )

    current_bars = {}
    deadline = (
        time.time() + MAX_WAIT_SECONDS
    )

    reconnects = 0

    while time.time() < deadline:

        stream_subset = ranked[:active_limit]

        try:

            status = run_live_stream(
                stream_subset,
                histories,
                current_bars,
                deadline
            )

            if status in ("done", "stop"):

                log(f"{VERSION} finished.")
                sys.stdout.flush()
                os._exit(0)

            reconnects += 1

            if reconnects > MAX_RECONNECTS:
                break

            wait_sec = min(
                4.0,
                max(1.0, deadline - time.time())
            )

            log(
                f"Reconnecting in {wait_sec:.1f}s..."
            )

            time.sleep(wait_sec)

        except StreamBadRequestError as e:

            log(f"Stream bad request: {e}")

            if active_limit > 1:

                active_limit -= 1

                log(
                    f"Reducing stream to "
                    f"{active_limit} pairs."
                )

                time.sleep(1)
                continue

            break

        except StreamRateLimitError as e:

            reconnects += 1

            if reconnects > MAX_RECONNECTS:
                break

            wait_sec = min(
                e.retry_after,
                max(0.0, deadline - time.time())
            )

            if wait_sec <= 2:
                break

            log(
                f"Rate limited. Waiting "
                f"{wait_sec:.1f}s..."
            )

            time.sleep(wait_sec)

        except Exception as e:

            log(f"Stream error: {e}")

            reconnects += 1

            if reconnects > MAX_RECONNECTS:
                break

            wait_sec = min(
                4.0,
                max(1.0, deadline - time.time())
            )

            if wait_sec <= 1:
                break

            time.sleep(wait_sec)

    log(f"{VERSION} finished.")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
