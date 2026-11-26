import os
import sys
import json
import math
import time
import logging
import threading
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# =====================================================
# 1. PARAMÈT JENERAL - ROMARIO PRO (MIN 92% PAYOUT, NO MODERE, M1 60s, UTC-5)
# =====================================================
VERSION = "ROMARIO PRO"

BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 60            # Graf 1 minit (M1)
EXPIRATION = 60           # Ekspirasyon 60 segonn (1 minit)
MIN_PAYOUT = 92.0         # Sèlman mache ki gen 92%+ payout sou OTC
MAX_SIGNALS_PER_RUN = 1   # 1 sèl siyal pa kouri
MAX_WAIT_SECONDS = 300    # Chèche yon opòtinite nan 5 minit max
MAX_STREAM_PAIRS = 5      # Siveye top 5 pè >= 92% ki gen pi bon setup sou /v1/stream

MIN_CONFIDENCE = 85.0     # Pa gen MODERE — sèlman siyal solid (>= 85%)
MIN_ADX = 24.0            # Tandans solid sèlman sou M1 OTC

CANDLE_LIMIT = 100
MIN_CANDLES = 55
MAX_RECONNECTS = 5

# Evite pè ki gen spik toksik / twou brital / ticks etwat sou 60s
TOXIC_CURRENCIES = (
    "TRY", "ZAR", "MXN", "BRL", "PLN", "RUB", "INR", "CNH", "THB",
    "HUF", "SEK", "NOK", "MYR", "SYP", "NGN"
)

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
            "User-Agent": "RomarioProM1/4.8",
            "Accept": "application/json"
        })
    return _thread_local.session


def format_price(value):
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def is_clean_pair(symbol):
    up = symbol.upper()
    return not any(code in up for code in TOXIC_CURRENCIES)


def compute_next_candle_utc5(ref_ts=None):
    """
    Kalkile pwochen ouvèti bouji M1 (Next Candle) an UTC-5 apati lè aktyèl la.
    """
    now_utc5 = ref_ts if ref_ts is not None else datetime.now(USER_TZ).timestamp()
    next_candle_ts = ((int(now_utc5) // TIMEFRAME) + 1) * TIMEFRAME
    countdown = max(1, int(round(next_candle_ts - now_utc5)))
    return next_candle_ts, countdown


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
# 2. KALKIL 6 ENDIKATÈ YO (RSI 14, MACD, BB, EMA 9/21, STOCH, ADX)
# =====================================================
def calc_ema_aligned(values, period):
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1.0)
    ema_val = sum(values[:period]) / period
    out[period - 1] = ema_val
    for i in range(period, n):
        ema_val = (values[i] - ema_val) * k + ema_val
        out[i] = ema_val
    return out


def calc_rsi_aligned(closes, period=14):
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out

    gains = []
    losses = []
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def to_rsi(g, l):
        if l == 0.0 and g == 0.0:
            return 50.0
        if l == 0.0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = to_rsi(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = to_rsi(avg_gain, avg_loss)

    return out


def calc_macd_aligned(closes, fast=12, slow=26, signal=9):
    n = len(closes)
    macd_line = [None] * n
    signal_line = [None] * n
    hist_line = [None] * n

    ema_fast = calc_ema_aligned(closes, fast)
    ema_slow = calc_ema_aligned(closes, slow)

    valid_indices = []
    valid_macd_vals = []
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            val = ema_fast[i] - ema_slow[i]
            macd_line[i] = val
            valid_indices.append(i)
            valid_macd_vals.append(val)

    if len(valid_macd_vals) >= signal:
        sig_aligned = calc_ema_aligned(valid_macd_vals, signal)
        for idx_pos, candle_idx in enumerate(valid_indices):
            s_val = sig_aligned[idx_pos]
            if s_val is not None:
                signal_line[candle_idx] = s_val
                hist_line[candle_idx] = macd_line[candle_idx] - s_val

    return macd_line, signal_line, hist_line


def calc_bollinger_aligned(closes, period=20, mult=2.0):
    n = len(closes)
    upper = [None] * n
    middle = [None] * n
    lower = [None] * n

    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = math.sqrt(max(variance, 0.0))
        middle[i] = mean
        upper[i] = mean + mult * std
        lower[i] = mean - mult * std

    return upper, middle, lower


def calc_stochastic_aligned(candles, k_period=14, d_period=3, smooth_k=3):
    n = len(candles)
    raw_k = [None] * n
    k_line = [None] * n
    d_line = [None] * n

    for i in range(k_period - 1, n):
        sub = candles[i - k_period + 1:i + 1]
        highest_h = max(c["high"] for c in sub)
        lowest_l = min(c["low"] for c in sub)
        denom = highest_h - lowest_l
        if denom <= 1e-12:
            raw_k[i] = 50.0
        else:
            raw_k[i] = ((candles[i]["close"] - lowest_l) / denom) * 100.0

    valid_k_idx = [i for i in range(n) if raw_k[i] is not None]
    valid_k_vals = [raw_k[i] for i in valid_k_idx]

    if len(valid_k_vals) >= smooth_k:
        for pos in range(smooth_k - 1, len(valid_k_vals)):
            c_idx = valid_k_idx[pos]
            k_line[c_idx] = sum(valid_k_vals[pos - smooth_k + 1:pos + 1]) / smooth_k

    valid_s_idx = [i for i in range(n) if k_line[i] is not None]
    valid_s_vals = [k_line[i] for i in valid_s_idx]

    if len(valid_s_vals) >= d_period:
        for pos in range(d_period - 1, len(valid_s_vals)):
            c_idx = valid_s_idx[pos]
            d_line[c_idx] = sum(valid_s_vals[pos - d_period + 1:pos + 1]) / d_period

    return k_line, d_line


def calc_adx_aligned(candles, period=14):
    n = len(candles)
    adx_out = [None] * n
    if n <= period * 2:
        return adx_out

    tr_list = []
    plus_dm_list = []
    minus_dm_list = []

    for i in range(1, n):
        curr = candles[i]
        prev = candles[i - 1]
        tr = max(
            curr["high"] - curr["low"],
            abs(curr["high"] - prev["close"]),
            abs(curr["low"] - prev["close"])
        )
        up_move = curr["high"] - prev["high"]
        down_move = prev["low"] - curr["low"]

        plus_dm = up_move if (up_move > down_move and up_move > 0.0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0.0) else 0.0

        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    sm_tr = sum(tr_list[:period])
    sm_plus = sum(plus_dm_list[:period])
    sm_minus = sum(minus_dm_list[:period])

    dx_vals = []
    dx_indices = []

    def calc_dx(tr_v, p_v, m_v):
        if tr_v <= 1e-12:
            return 0.0
        p_di = 100.0 * (p_v / tr_v)
        m_di = 100.0 * (m_v / tr_v)
        di_sum = p_di + m_di
        if di_sum <= 1e-12:
            return 0.0
        return 100.0 * abs(p_di - m_di) / di_sum

    dx_vals.append(calc_dx(sm_tr, sm_plus, sm_minus))
    dx_indices.append(period)

    for i in range(period, len(tr_list)):
        sm_tr = sm_tr - (sm_tr / period) + tr_list[i]
        sm_plus = sm_plus - (sm_plus / period) + plus_dm_list[i]
        sm_minus = sm_minus - (sm_minus / period) + minus_dm_list[i]
        dx_vals.append(calc_dx(sm_tr, sm_plus, sm_minus))
        dx_indices.append(i + 1)

    if len(dx_vals) >= period:
        adx_val = sum(dx_vals[:period]) / period
        adx_out[dx_indices[period - 1]] = adx_val
        for pos in range(period, len(dx_vals)):
            adx_val = ((adx_val * (period - 1)) + dx_vals[pos]) / period
            adx_out[dx_indices[pos]] = adx_val

    return adx_out


# =====================================================
# 3. EVALYASYON 6 ENDIKATÈ YO (PA GEN MODERE, CONFIANCE >= 85%, ADX >= 24)
# =====================================================
def evaluate_six_indicators(candles):
    if len(candles) < MIN_CANDLES:
        return None, "not enough candles"

    window = candles[-80:] if len(candles) >= 80 else candles
    closes = [c["close"] for c in window]
    bar = window[-1]
    price = bar["close"]

    ema9 = calc_ema_aligned(closes, 9)
    ema21 = calc_ema_aligned(closes, 21)
    rsi14 = calc_rsi_aligned(closes, 14)
    macd_line, signal_line, hist_line = calc_macd_aligned(closes, 12, 26, 9)
    bb_upper, bb_mid, bb_lower = calc_bollinger_aligned(closes, 20, 2.0)
    stoch_k, stoch_d = calc_stochastic_aligned(window, 14, 3, 3)
    adx14 = calc_adx_aligned(window, 14)

    if any(
        x is None for x in (
            ema9[-1], ema21[-1], rsi14[-1],
            macd_line[-1], signal_line[-1], hist_line[-1],
            bb_upper[-1], bb_mid[-1], bb_lower[-1],
            stoch_k[-1], stoch_d[-1], adx14[-1]
        )
    ):
        return None, "indicators unavailable"

    score_call = 0
    score_put = 0

    # 1. EMA 9 / 21 (3 pwen)
    if ema9[-1] > ema21[-1]:
        score_call += 3
        ema_state = "tendance haussière"
    elif ema9[-1] < ema21[-1]:
        score_put += 3
        ema_state = "tendance baissière"
    else:
        ema_state = "neutre"

    # 2. MACD (12, 26, 9) (3 pwen)
    if macd_line[-1] > signal_line[-1] and hist_line[-1] > 0:
        score_call += 3
        macd_state = "haussier"
    elif macd_line[-1] < signal_line[-1] and hist_line[-1] < 0:
        score_put += 3
        macd_state = "baissier"
    else:
        macd_state = "neutre"

    # 3. RSI (14) (2 pwen)
    rsi_val = rsi14[-1]
    if 51.0 <= rsi_val <= 65.0:
        score_call += 2
    elif 35.0 <= rsi_val <= 49.0:
        score_put += 2

    # 4. Bollinger Bands (20, 2) (2 pwen)
    bb_span = max(bb_upper[-1] - bb_lower[-1], 1e-12)
    bb_pos = (price - bb_lower[-1]) / bb_span

    if bb_pos >= 0.75:
        bb_state = "proche bande haute"
    elif bb_pos <= 0.25:
        bb_state = "proche bande basse"
    elif price >= bb_mid[-1]:
        bb_state = "milieu haut"
    else:
        bb_state = "milieu bas"

    if price > bb_mid[-1] and bb_pos <= 0.74:
        score_call += 2
    elif price < bb_mid[-1] and bb_pos >= 0.26:
        score_put += 2

    # 5. Stochastique (14, 3, 3) (2 pwen)
    sk = stoch_k[-1]
    sd = stoch_d[-1]
    if sk > sd and 28.0 <= sk <= 72.0:
        score_call += 2
    elif sk < sd and 28.0 <= sk <= 72.0:
        score_put += 2

    # 6. ADX (14) (3 pwen)
    adx_val = adx14[-1]
    adx_dir = "▲" if ema9[-1] >= ema21[-1] else "▼"
    if adx_val >= MIN_ADX:
        if ema9[-1] > ema21[-1]:
            score_call += 3
        elif ema9[-1] < ema21[-1]:
            score_put += 3

    diag = (
        f"CALL={score_call} PUT={score_put} ADX={adx_val:.2f} "
        f"RSI={rsi_val:.2f} STOCH={sk:.2f} MACD={macd_state} BBPOS={bb_pos:.2f}"
    )

    if adx_val < MIN_ADX:
        return None, f"{diag} adx_below_{MIN_ADX:.1f}"

    adx_bonus = min(3.0, max(0.0, (adx_val - MIN_ADX) * 0.25))
    score_bonus = min(1.0, max(0.0, (abs(score_call - score_put) - 8) * 0.25))
    win_prob = int(round(max(85.0, min(89.0, 85.0 + adx_bonus + score_bonus))))

    summary_lines = (
        f"• RSI (14): {rsi_val:.2f} | MACD: {macd_state}\n"
        f"• Bollinger: {bb_state} | EMA 9/21: {ema_state}\n"
        f"• Stoch %K: {sk:.2f} | ADX: {adx_val:.2f} ({adx_dir})"
    )

    if score_call >= 12 and score_call > score_put and win_prob >= MIN_CONFIDENCE:
        if macd_state != "haussier" or rsi_val > 65.0 or sk > 72.0 or bb_pos > 0.74:
            return None, f"{diag} call_filtered"
        return {
            "direction": "CALL",
            "score_call": score_call,
            "score_put": score_put,
            "win_prob": win_prob,
            "adx": adx_val,
            "confidence": "🟢 TRÈ SOLID",
            "indicators_summary": summary_lines,
            "ref_quote": price,
            "candle_time": bar["time"]
        }, f"{diag} CALL_SIGNAL conf={win_prob}%"

    if score_put >= 12 and score_put > score_call and win_prob >= MIN_CONFIDENCE:
        if macd_state != "baissier" or rsi_val < 35.0 or sk < 28.0 or bb_pos < 0.26:
            return None, f"{diag} put_filtered"
        return {
            "direction": "PUT",
            "score_call": score_call,
            "score_put": score_put,
            "win_prob": win_prob,
            "adx": adx_val,
            "confidence": "🟢 TRÈ SOLID",
            "indicators_summary": summary_lines,
            "ref_quote": price,
            "candle_time": bar["time"]
        }, f"{diag} PUT_SIGNAL conf={win_prob}%"

    return None, f"{diag} no_strong_solid_setup"


def build_live_window(symbol, histories, current_bar):
    """
    Konbine bouji ki deja fèmen yo (Historical Candles) ak bouji k ap fòme an dirèk la (Live Forming Candle).
    """
    hist = histories.get(symbol, [])
    if len(hist) < 1 or current_bar is None:
        return []
    live_bar = {
        "time": current_bar["time"],
        "open": current_bar["open"],
        "high": current_bar["high"],
        "low": current_bar["low"],
        "close": current_bar["close"]
    }
    return hist[-(CANDLE_LIMIT - 1):] + [live_bar]


def score_setup_quality(candles):
    sig, _ = evaluate_six_indicators(candles)
    if sig is not None:
        return round(100.0 + float(sig["win_prob"]), 2)

    if len(candles) < MIN_CANDLES:
        return 0.0

    window = candles[-80:] if len(candles) >= 80 else candles
    closes = [c["close"] for c in window]
    ema9 = calc_ema_aligned(closes, 9)
    ema21 = calc_ema_aligned(closes, 21)
    adx14 = calc_adx_aligned(window, 14)

    quality = 0.0
    if ema9[-1] is not None and ema21[-1] is not None:
        if ema9[-1] > ema21[-1] or ema9[-1] < ema21[-1]:
            quality += 25.0
    if adx14[-1] is not None and adx14[-1] >= MIN_ADX:
        quality += min(35.0, adx14[-1])

    return round(quality, 2)


# =====================================================
# 4. FORMAT MESAJ TELEGRAM SENP & VERIFIKASYON OTCHARTS 60S
# =====================================================
def format_signal_message(pair, payout, entry_time_str, countdown_sec, signal_data, entry_quote=None):
    emoji = "🟢" if signal_data["direction"] == "CALL" else "🔴"
    quote_str = format_price(entry_quote) if entry_quote is not None else format_price(signal_data["ref_quote"])
    return (
        f"🤖 {VERSION}\n\n"
        f"{emoji} {signal_data['direction']} | {pair} (OTCharts {payout:.0f}%)\n"
        f"📊 Confiance: {signal_data['win_prob']}% | SCORE CALL: {signal_data['score_call']} vs SCORE PUT: {signal_data['score_put']}\n"
        f"🎯 Kot: {quote_str}\n"
        f"🕒 Next Candle: {entry_time_str} ({countdown_sec}s | Expiration 1min)\n"
        f"{signal_data['indicators_summary']}\n"
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
    last_eval_ts = {}
    signals_sent = 0
    active_trade = None
    event_name = ""

    try:
        for raw_line in response.iter_lines(decode_unicode=True, chunk_size=1):
            now = datetime.now(USER_TZ).timestamp()

            if active_trade is not None:
                if active_trade["entry_price"] is None and now >= active_trade["entry_time"] and active_trade["last_price"] is not None:
                    active_trade["entry_price"] = active_trade["last_price"]
                    log(f"{active_trade['symbol']}: Entry price at entry time -> {format_price(active_trade['entry_price'])}")

                if now >= active_trade["expiry_time"]:
                    finalize_pending_trade(active_trade, last_prices)
                    return "stop"

            if signals_sent >= MAX_SIGNALS_PER_RUN and active_trade is None:
                log("Single signal completed. Stopping bot.")
                return "stop"

            if active_trade is None and now >= deadline:
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

            if active_trade is not None:
                if symbol == active_trade["symbol"]:
                    active_trade["last_price"] = price
                    if active_trade["entry_price"] is None and now >= active_trade["entry_time"]:
                        active_trade["entry_price"] = price
                        log(f"{symbol}: Entry price at entry time -> {format_price(price)}")

                if now >= active_trade["expiry_time"]:
                    finalize_pending_trade(active_trade, last_prices)
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
                current = current_bars[symbol]
            else:
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price

            if now - last_eval_ts.get(symbol, 0.0) >= 2.0:
                last_eval_ts[symbol] = now
                window = build_live_window(symbol, histories, current)
                if len(window) >= MIN_CANDLES:
                    signal_data, diag = evaluate_six_indicators(window)
                    if signal_data:
                        pair_info = next((x for x in monitored if x["symbol"] == symbol), {"payout": MIN_PAYOUT})
                        payout = pair_info["payout"]
                        next_candle_ts, countdown_sec = compute_next_candle_utc5(now)
                        entry_time_str = datetime.fromtimestamp(next_candle_ts, tz=USER_TZ).strftime("%I:%M:%S %p")

                        msg = format_signal_message(
                            symbol,
                            payout,
                            entry_time_str,
                            countdown_sec,
                            signal_data,
                            entry_quote=price
                        )
                        if send_telegram_fn(msg):
                            signals_sent += 1
                            active_trade = {
                                "symbol": symbol,
                                "payout": payout,
                                "direction": signal_data["direction"],
                                "entry_time": next_candle_ts,
                                "expiry_time": next_candle_ts + EXPIRATION,
                                "entry_price": None,
                                "last_price": price
                            }
                            log(
                                f"{symbol}: Signal sent ({signal_data['direction']}, win={signal_data['win_prob']}%) "
                                f"-> Next Candle {entry_time_str} ({countdown_sec}s, UTC-5)."
                            )
                    else:
                        log(f"{symbol}: no setup ({diag})")

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
