import os
import re
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ==========================================================
# ROMARIO TRADER PRO 10.2
# Pocket Option OTC | 30-second candles | 60-second expiry
# Telegram alerts only — no automatic trades
# ==========================================================

BOT_NAME = "ROMARIO TRADER PRO 10.2"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 30
EXPIRATION_SECONDS = 60
MIN_PAYOUT = 92.0
CANDLE_LIMIT = 150
MIN_CANDLES = 55

ENTRY_LEAD_SECONDS = 15
STOCH_TOLERANCE = 10

FLORIDA = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CALL_PAIRS = [
    (50, 63), (73, 63), (27, 43),
    (24, 45), (37, 45), (43, 36),
    (49, 35), (42, 18), (44, 16),
    (78, 76), (42, 21), (20, 31),
    (42, 63)
]

PUT_PAIRS = [
    (54, 70), (51, 56), (74, 81),
    (48, 71), (28, 41), (78, 53),
    (70, 53), (70, 57), (68, 37),
    (51, 44), (69, 54)
]

# Used to recognize common cryptocurrency tickers if they
# appear in an otherwise currency-looking symbol.
CRYPTO_CODES = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOT",
    "LTC", "DOG", "DOGE", "TRX", "TON", "AVAX",
    "SHIB", "BCH", "LINK", "XLM", "USDT", "USDC"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "RomarioTrader/10.2",
    "Accept": "application/json"
})


# ----------------------------------------------------------
# BASIC VALIDATION
# ----------------------------------------------------------

def validate_secrets():
    missing = []

    if not API_KEY:
        missing.append("OTCHARTS_API_KEY")
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise RuntimeError(
            "Missing GitHub Secrets: " + ", ".join(missing)
        )


def api_get(path, params=None):
    url = BASE_URL + path

    response = session.get(
        url,
        params=params,
        timeout=25
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"OTCharts HTTP {response.status_code}: "
            f"{response.text[:400]}"
        )

    try:
        return response.json()
    except ValueError:
        raise RuntimeError("OTCharts returned invalid JSON")


# ----------------------------------------------------------
# FOREX OTC SYMBOL FILTER
# ----------------------------------------------------------

def is_forex_otc(item):
    symbol = str(item.get("symbol", "")).strip()
    name = str(item.get("name", "")).strip().upper()

    if not symbol or symbol.startswith("#"):
        return False

    if not symbol.upper().endswith("_OTC"):
        return False

    # Prefer the provider's instrument name, e.g. EUR/USD OTC.
    match = re.match(
        r"^([A-Z]{3})/([A-Z]{3})(?:\s*\(?OTC\)?)?$",
        name
    )

    if not match:
        return False

    base, quote = match.groups()

    if base in CRYPTO_CODES or quote in CRYPTO_CODES:
        return False

    # isOtc must explicitly confirm this is an OTC instrument.
    if item.get("isOtc") is not True:
        return False

    # Only currently tradable instruments.
    if item.get("open") is not True:
        return False

    try:
        payout = float(item.get("payout"))
    except (TypeError, ValueError):
        return False

    if payout < MIN_PAYOUT:
        return False

    return True


def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    items = data.get("symbols", [])

    if not isinstance(items, list):
        raise RuntimeError("Unexpected symbols response format")

    eligible = []

    for item in items:
        if not isinstance(item, dict):
            continue

        if is_forex_otc(item):
            eligible.append({
                "symbol": item["symbol"],
                "name": item.get("name", item["symbol"]),
                "payout": float(item["payout"])
            })

    eligible.sort(key=lambda x: x["symbol"])

    logging.info(
        "Eligible OPEN Forex OTC pairs with payout >= %.1f%%: %s",
        MIN_PAYOUT,
        len(eligible)
    )

    return eligible


# ----------------------------------------------------------
# CANDLE DATA AND DATA QUALITY CHECKS
# ----------------------------------------------------------

def get_candles(symbol):
    data = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    candles = data.get("candles", [])

    if not isinstance(candles, list):
        logging.warning("%s: invalid candle list", symbol)
        return None

    interval = str(data.get("interval", "")).lower()

    if interval != "30s":
        logging.warning(
            "%s: expected 30s candles, API returned %s",
            symbol,
            interval or "unknown interval"
        )
        return None

    gaps = data.get("gaps") or {}
    missing_bars = int(gaps.get("missingBars", 0) or 0)

    if missing_bars > 0:
        logging.warning(
            "%s: skipped because API reports %s missing candles",
            symbol,
            missing_bars
        )
        return None

    if data.get("open") is False:
        logging.info("%s: broker reports pair closed", symbol)
        return None

    now = time.time()
    clean = []

    for candle in candles:
        try:
            timestamp = int(candle["time"])
            op = float(candle["open"])
            high = float(candle["high"])
            low = float(candle["low"])
            close = float(candle["close"])

            if not all(map(
                lambda x: x == x and abs(x) != float("inf"),
                [op, high, low, close]
            )):
                continue

            if high < low:
                continue

            # Use only completed 30-second candles.
            if timestamp + TIMEFRAME <= now - 1:
                clean.append({
                    "time": timestamp,
                    "open": op,
                    "high": high,
                    "low": low,
                    "close": close
                })

        except (KeyError, TypeError, ValueError):
            continue

    clean.sort(key=lambda x: x["time"])

    # Remove duplicate timestamps.
    unique = {}
    for candle in clean:
        unique[candle["time"]] = candle

    clean = list(unique.values())

    if len(clean) < MIN_CANDLES:
        logging.warning(
            "%s: only %s completed usable candles",
            symbol,
            len(clean)
        )
        return None

    # Verify the recent bars are actually spaced 30 seconds apart.
    recent = clean[-10:]

    for previous, current in zip(recent, recent[1:]):
        difference = current["time"] - previous["time"]

        if difference != TIMEFRAME:
            logging.warning(
                "%s: irregular candle spacing (%ss); skipped",
                symbol,
                difference
            )
            return None

    logging.info(
        "%s: candles=%s | interval=%s | gaps=%s",
        symbol,
        len(clean),
        interval,
        missing_bars
    )

    return clean


# ----------------------------------------------------------
# INDICATORS
# ----------------------------------------------------------

def ema_series(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    first = sum(values[:period]) / period
    result = [None] * (period - 1)
    result.append(first)

    current = first

    for value in values[period:]:
        current = (
            value * multiplier
            + current * (1 - multiplier)
        )
        result.append(current)

    return result


def stochastic_series(candles, period=14, smooth_k=3, smooth_d=3):
    raw_k = []

    for i in range(len(candles)):
        if i < period - 1:
            raw_k.append(None)
            continue

        window = candles[i - period + 1:i + 1]

        highest = max(c["high"] for c in window)
        lowest = min(c["low"] for c in window)
        close = candles[i]["close"]

        if highest == lowest:
            raw_k.append(50.0)
        else:
            raw_k.append(
                ((close - lowest) / (highest - lowest)) * 100
            )

    smooth_values = []

    for i in range(len(raw_k)):
        if i < period - 1 + smooth_k - 1:
            smooth_values.append(None)
            continue

        window = raw_k[i - smooth_k + 1:i + 1]

        if any(v is None for v in window):
            smooth_values.append(None)
        else:
            smooth_values.append(sum(window) / smooth_k)

    d_values = []

    for i in range(len(smooth_values)):
        if i < period - 1 + smooth_k + smooth_d - 2:
            d_values.append(None)
            continue

        window = smooth_values[i - smooth_d + 1:i + 1]

        if any(v is None for v in window):
            d_values.append(None)
        else:
            d_values.append(sum(window) / smooth_d)

    return smooth_values, d_values


# ----------------------------------------------------------
# STOCHASTIC REFERENCE PAIR MATCH
# ----------------------------------------------------------

def reference_distance(k, d, direction):
    pairs = CALL_PAIRS if direction == "CALL" else PUT_PAIRS

    distances = []

    for ref_k, ref_d in pairs:
        dk = abs(k - ref_k)
        dd = abs(d - ref_d)

        if dk <= STOCH_TOLERANCE and dd <= STOCH_TOLERANCE:
            distances.append(dk + dd)

    if distances:
        return min(distances)

    return None


# ----------------------------------------------------------
# SIGNAL ANALYSIS
# ----------------------------------------------------------

def analyze_pair(pair):
    symbol = pair["symbol"]
    payout = pair["payout"]

    candles = get_candles(symbol)

    if candles is None:
        return None

    closes = [c["close"] for c in candles]

    ema9 = ema_series(closes, 9)
    ema21 = ema_series(closes, 21)
    ema50 = ema_series(closes, 50)

    k_values, d_values = stochastic_series(candles)

    if not ema50 or len(k_values) < 2 or len(d_values) < 2:
        logging.info("%s: indicators not ready", symbol)
        return None

    i = len(candles) - 1

    current = candles[i]
    previous = candles[i - 1]

    e9 = ema9[i]
    e9_prev = ema9[i - 1]
    e21 = ema21[i]
    e50 = ema50[i]

    k = k_values[i]
    k_prev = k_values[i - 1]
    d = d_values[i]
    d_prev = d_values[i - 1]

    if None in (e9, e9_prev, e21, e50, k, k_prev, d, d_prev):
        logging.info("%s: indicator values incomplete", symbol)
        return None

    close = current["close"]
    candle_bullish = current["close"] > current["open"]
    candle_bearish = current["close"] < current["open"]

    # ---------------- CALL ----------------

    call_trend = (
        e9 > e21
        and e9 > e9_prev
        and e21 >= e50
    )

    call_stochastic = (
        k > d
        and k > k_prev
    )

    if call_trend and call_stochastic:
        price_confirmation = close > e21
        candle_confirmation = candle_bullish

        ref_distance = reference_distance(k, d, "CALL")
        reference_bonus = ref_distance is not None

        score = (
            2
            + int(price_confirmation)
            + int(candle_confirmation)
            + int(reference_bonus)
        )

        # Trend and Stochastic are required.
        # At least one extra confirmation is also required.
        if price_confirmation or candle_confirmation:
            logging.info(
                "%s CALL candidate | K=%.1f D=%.1f | score=%s/5 | ref=%s",
                symbol,
                k,
                d,
                score,
                "YES" if reference_bonus else "NO"
            )

            return {
                "symbol": symbol,
                "direction": "CALL",
                "payout": payout,
                "k": k,
                "d": d,
                "score": score,
                "reference": reference_bonus,
                "reference_distance": (
                    ref_distance if ref_distance is not None else 999
                )
            }

    # ---------------- PUT ----------------

    put_trend = (
        e9 < e21
        and e9 < e9_prev
        and e21 <= e50
    )

    put_stochastic = (
        k < d
        and k < k_prev
    )

    if put_trend and put_stochastic:
        price_confirmation = close < e21
        candle_confirmation = candle_bearish

        ref_distance = reference_distance(k, d, "PUT")
        reference_bonus = ref_distance is not None

        score = (
            2
            + int(price_confirmation)
            + int(candle_confirmation)
            + int(reference_bonus)
        )

        if price_confirmation or candle_confirmation:
            logging.info(
                "%s PUT candidate | K=%.1f D=%.1f | score=%s/5 | ref=%s",
                symbol,
                k,
                d,
                score,
                "YES" if reference_bonus else "NO"
            )

            return {
                "symbol": symbol,
                "direction": "PUT",
                "payout": payout,
                "k": k,
                "d": d,
                "score": score,
                "reference": reference_bonus,
                "reference_distance": (
                    ref_distance if ref_distance is not None else 999
                )
            }

    logging.info(
        "%s: no setup | K=%.1f D=%.1f | EMA9=%.6f EMA21=%.6f",
        symbol,
        k,
        d,
        e9,
        e21
    )

    return None


# ----------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------

def send_telegram(message):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        },
        timeout=20
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram error {response.status_code}: "
            f"{response.text[:300]}"
        )

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm message delivery")


# ----------------------------------------------------------
# ENTRY TIMING
# ----------------------------------------------------------

def florida_time(epoch):
    return datetime.fromtimestamp(
        epoch,
        tz=timezone.utc
    ).astimezone(FLORIDA)


def schedule_entry():
    now = time.time()

    # Choose the next 30-second boundary that is at least
    # 15 seconds in the future.
    entry_epoch = (
        int((now + ENTRY_LEAD_SECONDS) // TIMEFRAME) + 1
    ) * TIMEFRAME

    send_epoch = entry_epoch - ENTRY_LEAD_SECONDS

    delay = send_epoch - time.time()

    if delay > 0:
        logging.info(
            "Waiting %.1f seconds for alert time",
            delay
        )
        time.sleep(delay)

    if time.time() >= entry_epoch:
        raise RuntimeError(
            "Entry time passed before alert could be sent; "
            "signal cancelled"
        )

    return entry_epoch


# ----------------------------------------------------------
# MAIN
# ----------------------------------------------------------

def main():
    logging.info("%s STARTING", BOT_NAME)

    validate_secrets()

    pairs = get_symbols()

    if not pairs:
        send_telegram(
            f"🤖 <b>{BOT_NAME}</b>\n\n"
            "⚪ <b>NO VALID SIGNAL</b>\n"
            "No open Forex OTC pair passed the payout filter.\n\n"
            f"Minimum payout: {MIN_PAYOUT:.1f}%"
        )
        return

    candidates = []
    scanned = 0
    errors = 0

    for index, pair in enumerate(pairs, start=1):
        symbol = pair["symbol"]

        logging.info(
            "Scanning %s (%s/%s)",
            symbol,
            index,
            len(pairs)
        )

        scanned += 1

        try:
            candidate = analyze_pair(pair)

            if candidate:
                candidates.append(candidate)

        except Exception as exc:
            errors += 1
            logging.warning(
                "%s: scan error: %s",
                symbol,
                str(exc)[:250]
            )

    logging.info(
        "SCAN COMPLETE | scanned=%s | candidates=%s | errors=%s",
        scanned,
        len(candidates),
        errors
    )

    if not candidates:
        send_telegram(
            f"🤖 <b>{BOT_NAME}</b>\n\n"
            "⚪ <b>NO VALID SIGNAL</b>\n"
            "No pair passed the trend, Stochastic and "
            "confirmation rules.\n\n"
            f"Pairs checked: {scanned}\n"
            f"Minimum payout: {MIN_PAYOUT:.1f}%\n"
            f"Timeframe: {TIMEFRAME} seconds\n"
            f"Errors: {errors}\n"
            "No signal was forced."
        )
        return

    # Best score first; then closest reference pair;
    # then higher payout.
    candidates.sort(
        key=lambda x: (
            -x["score"],
            x["reference_distance"],
            -x["payout"]
        )
    )

    best = candidates[0]

    entry_epoch = schedule_entry()

    signal_epoch = time.time()

    signal_time = florida_time(signal_epoch)
    entry_time = florida_time(entry_epoch)

    direction_icon = (
        "🟢" if best["direction"] == "CALL" else "🔴"
    )

    reference_text = (
        "Matched within tolerance"
        if best["reference"]
        else "No exact reference match; other confirmations passed"
    )

    message = (
        f"🤖 <b>{BOT_NAME}</b>\n\n"
        f"{direction_icon} <b>{best['direction']}</b>\n\n"
        f"💱 PAIR: <b>{best['symbol']}</b>\n"
        f"💰 PAYOUT: <b>{best['payout']:.1f}%</b>\n"
        f"⏱ TIMEFRAME: <b>30 seconds</b>\n"
        f"⌛ EXPIRATION: <b>60 seconds</b>\n"
        f"📊 STOCH K: <b>{best['k']:.1f}</b>\n"
        f"📊 STOCH D: <b>{best['d']:.1f}</b>\n"
        f"⭐ SETUP SCORE: <b>{best['score']}/5</b>\n"
        f"🎯 STOCH REFERENCE: {reference_text}\n\n"
        f"📨 SIGNAL TIME: <b>{signal_time.strftime('%I:%M:%S %p')}</b>\n"
        f"🚀 ENTRY TIME: <b>{entry_time.strftime('%I:%M:%S %p')}</b>\n"
        f"🌴 FLORIDA TIME\n\n"
        "⚠️ Alert only — verify the live chart before trading."
    )

    send_telegram(message)

    logging.info(
        "SIGNAL SENT | %s | %s | entry=%s",
        best["direction"],
        best["symbol"],
        entry_time.strftime("%Y-%m-%d %H:%M:%S")
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.exception("BOT FAILED: %s", error)
        raise
