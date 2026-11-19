
import os
import time
import math
import logging
import requests

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# =========================================================
# ROMARIO TRADER PRO CLEAN 1.6
# OTCharts OTC | M1 | 60s expiration
# Telegram manual alerts only
# =========================================================

VERSION = "ROMARIO TRADER PRO CLEAN 1.6"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 60
EXPIRATION = 60

MIN_PAYOUT = 85.0
MIN_CANDLES = 25
CANDLE_LIMIT = 50

MAX_MARKETS = 15

# Increased from 100 to 180 requests per run.
# Watch your OTCharts daily plan usage.
MAX_API_REQUESTS = 180

SCAN_INTERVAL = 10
MAX_SCAN_SECONDS = 120

ENTRY_DELAY = 15
MIN_ENTRY_LEAD = 3
MAX_ENTRY_LEAD = 80

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 20

RSI_PERIOD = 7
ATR_PERIOD = 14

# Pocket Option OTC timestamp correction.
OTC_TIMESTAMP_OFFSET_SECONDS = 7200

ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("romario")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SESSION = requests.Session()

SESSION.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "User-Agent": "RomarioTrader/1.6"
})

request_count = 0


# =========================================================
# TIME
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)


def fmt_et(dt):
    return dt.astimezone(ET).strftime("%I:%M:%S %p ET")


def parse_time(raw):
    if raw is None:
        return None

    try:
        if isinstance(raw, (int, float)):
            value = float(raw)

            if value > 1e12:
                value /= 1000.0

            dt = datetime.fromtimestamp(
                value,
                tz=timezone.utc
            )

        else:
            text = str(raw).strip()

            if text.isdigit():
                value = float(text)

                if value > 1e12:
                    value /= 1000.0

                dt = datetime.fromtimestamp(
                    value,
                    tz=timezone.utc
                )

            else:
                dt = datetime.fromisoformat(
                    text.replace("Z", "+00:00")
                )

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                dt = dt.astimezone(timezone.utc)

        if VENUE == "otc":
            dt -= timedelta(
                seconds=OTC_TIMESTAMP_OFFSET_SECONDS
            )

        return dt

    except (
        ValueError,
        TypeError,
        OverflowError,
        OSError
    ):
        return None


# =========================================================
# API
# =========================================================

def api_get(path, params=None, retries=2):
    global request_count

    for attempt in range(retries + 1):

        if request_count >= MAX_API_REQUESTS:
            log.warning(
                "API request limit reached: %d/%d",
                request_count,
                MAX_API_REQUESTS
            )
            return None

        request_count += 1

        try:
            response = SESSION.get(
                BASE_URL + path,
                params=params,
                timeout=(8, 15)
            )

            if response.status_code == 503:
                log.warning(
                    "API 503: endpoint or market unavailable: %s",
                    params.get("symbol") if params else path
                )
                return None

            if response.status_code in (
                429, 500, 502, 504
            ):
                log.warning(
                    "Temporary API HTTP %s: %s",
                    response.status_code,
                    path
                )

                if attempt < retries:
                    time.sleep(attempt + 1)
                    continue

                return None

            if response.status_code != 200:
                log.warning(
                    "API HTTP %s: %s",
                    response.status_code,
                    response.text[:250]
                )
                return None

            try:
                return response.json()

            except ValueError:
                log.warning(
                    "Invalid JSON from API: %s",
                    path
                )
                return None

        except requests.RequestException as exc:
            log.warning(
                "API connection error: %s",
                exc
            )

            if attempt < retries:
                time.sleep(attempt + 1)
            else:
                return None

    return None


# =========================================================
# DATA HELPERS
# =========================================================

def unwrap(obj):
    if isinstance(obj, dict):
        for key in (
            "data",
            "result",
            "results",
            "symbols",
            "candles",
            "items"
        ):
            value = obj.get(key)

            if isinstance(value, (list, dict)):
                return value

    return obj


def as_list(obj):
    obj = unwrap(obj)

    if isinstance(obj, list):
        return obj

    if isinstance(obj, dict):
        result = []

        for key, value in obj.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("symbol", key)
                result.append(row)

        return result

    return []


def getv(data, keys, default=None):
    if not isinstance(data, dict):
        return default

    for key in keys:
        if key in data and data[key] is not None:
            return data[key]

    return default


# =========================================================
# MARKETS
# =========================================================

def symbol_name(row):
    if isinstance(row, str):
        return row.strip()

    return str(
        getv(
            row,
            [
                "symbol",
                "name",
                "ticker",
                "instrument",
                "pair",
                "asset"
            ],
            ""
        )
    ).strip()


def payout_value(row):
    raw = getv(
        row,
        [
            "payout",
            "payout_percent",
            "profit",
            "profit_percent",
            "rate",
            "return",
            "percentage"
        ]
    )

    try:
        value = float(
            str(raw).replace("%", "").strip()
        )

        return value * 100 if 0 < value <= 1 else value

    except (TypeError, ValueError):
        return 0.0


def is_open(row):
    status = getv(
        row,
        [
            "open",
            "is_open",
            "active",
            "enabled",
            "status",
            "tradeable",
            "tradable"
        ],
        True
    )

    if isinstance(status, bool):
        return status

    return str(status).strip().lower() not in {
        "false",
        "0",
        "closed",
        "inactive",
        "disabled",
        "off",
        "unavailable"
    }


def fetch_symbols():
    raw = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    rows = as_list(raw)

    markets = []
    seen = set()

    for row in rows:
        name = symbol_name(row)

        if not name or not name.lower().endswith("_otc"):
            continue

        payout = payout_value(row)

        if payout < MIN_PAYOUT:
            continue

        if not is_open(row):
            continue

        if name in seen:
            continue

        seen.add(name)

        markets.append({
            "symbol": name,
            "payout": payout
        })

    markets.sort(
        key=lambda item: (
            -item["payout"],
            item["symbol"]
        )
    )

    log.info(
        "OTC symbols received=%d | eligible payout/open=%d",
        len(rows),
        len(markets)
    )

    return markets


def rotate_markets(markets):
    if not markets:
        return []

    bucket = int(now_utc().timestamp() // 300)
    start = bucket % len(markets)

    rotated = markets[start:] + markets[:start]

    return rotated[:MAX_MARKETS]


# =========================================================
# CANDLES
# =========================================================

def candle_fields(candle):

    if isinstance(candle, (list, tuple)) and len(candle) >= 5:
        timestamp, open_price, high, low, close = candle[:5]

    elif isinstance(candle, dict):
        timestamp = getv(
            candle,
            ["timestamp", "time", "date", "datetime", "t"]
        )

        open_price = getv(candle, ["open", "o"])
        high = getv(candle, ["high", "h"])
        low = getv(candle, ["low", "l"])
        close = getv(candle, ["close", "c"])

    else:
        return None

    try:
        dt = parse_time(timestamp)

        values = [
            float(open_price),
            float(high),
            float(low),
            float(close)
        ]

        if dt is None:
            return None

        if not all(math.isfinite(x) for x in values):
            return None

        op, hi, lo, cl = values

        if min(op, hi, lo, cl) <= 0 or hi < lo:
            return None

        return {
            "time": dt,
            "open": op,
            "high": hi,
            "low": lo,
            "close": cl
        }

    except (TypeError, ValueError):
        return None


def fetch_candles(symbol):

    raw = api_get(
        "/v1/candles",
        {
            "venue": VENUE,
            "symbol": symbol,
            "tf": TIMEFRAME,
            "limit": CANDLE_LIMIT
        }
    )

    if raw is None:
        return []

    rows = as_list(raw)

    parsed = [
        candle_fields(row)
        for row in rows
    ]

    parsed = [
        candle for candle in parsed
        if candle is not None
    ]

    if not parsed:
        log.warning(
            "%s: no valid OHLC candles. API rows=%d",
            symbol,
            len(rows)
        )

        if rows:
            log.info(
                "%s RAW SAMPLE: %s",
                symbol,
                str(rows[-1])[:400]
            )

        return []

    parsed.sort(
        key=lambda candle: candle["time"]
    )

    dedup = {
        candle["time"]: candle
        for candle in parsed
    }

    parsed = list(dedup.values())

    now = now_utc()
    latest = parsed[-1]["time"]

    if latest > now + timedelta(minutes=5):
        log.warning(
            "%s: future candle timestamp. latest=%s now=%s",
            symbol,
            latest.isoformat(),
            now.isoformat()
        )
        return []

    # Candle timestamp is treated as candle OPEN.
    closed = [
        candle
        for candle in parsed
        if candle["time"] + timedelta(seconds=TIMEFRAME) <= now
    ]

    log.info(
        "%s: API candles=%d | confirmed closed=%d | latest=%s",
        symbol,
        len(parsed),
        len(closed),
        latest.isoformat()
    )

    return closed


# =========================================================
# INDICATORS
# =========================================================

def ema(values, period):
    if len(values) < period:
        return None

    alpha = 2.0 / (period + 1)
    result = values[0]

    for value in values[1:]:
        result = (
            value * alpha
            + result * (1 - alpha)
        )

    return result


def rsi(values, period=7):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [
        max(change, 0.0)
        for change in changes
    ]

    losses = [
        max(-change, 0.0)
        for change in changes
    ]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        ranges.append(
            max(
                current["high"] - current["low"],
                abs(current["high"] - previous["close"]),
                abs(current["low"] - previous["close"])
            )
        )

    return sum(ranges[-period:]) / period


# =========================================================
# STRATEGY WITH REJECTION DIAGNOSTICS
# =========================================================

def analyze(symbol, payout, candles):

    if len(candles) < MIN_CANDLES:
        log.info(
            "%s REJECTED: not enough closed candles (%d/%d)",
            symbol,
            len(candles),
            MIN_CANDLES
        )
        return None

    closes = [
        candle["close"]
        for candle in candles
    ]

    e9 = ema(closes, EMA_FAST)
    e21 = ema(closes, EMA_MID)
    e20 = ema(closes, EMA_SLOW)

    rsi_value = rsi(closes, RSI_PERIOD)
    atr_value = atr(candles, ATR_PERIOD)

    if any(
        value is None
        for value in (e9, e21, e20, rsi_value, atr_value)
    ):
        log.info(
            "%s REJECTED: indicator data unavailable",
            symbol
        )
        return None

    if atr_value <= 0:
        log.info(
            "%s REJECTED: ATR is zero",
            symbol
        )
        return None

    last = candles[-1]

    body = abs(
        last["close"] - last["open"]
    )

    if body < atr_value * 0.08:
        log.info(
            "%s REJECTED: candle body too weak (body/ATR=%.3f)",
            symbol,
            body / atr_value
        )
        return None

    if body > atr_value * 1.35:
        log.info(
            "%s REJECTED: candle body too large (body/ATR=%.3f)",
            symbol,
            body / atr_value
        )
        return None

    bullish_trend = (
        e9 > e21
        and e21 >= e20
        and last["close"] > e9
    )

    bearish_trend = (
        e9 < e21
        and e21 <= e20
        and last["close"] < e9
    )

    bullish_candle = (
        last["close"] > last["open"]
    )

    bearish_candle = (
        last["close"] < last["open"]
    )

    bullish_rsi = (
        52 <= rsi_value <= 72
    )

    bearish_rsi = (
        28 <= rsi_value <= 48
    )

    bullish = (
        bullish_trend
        and bullish_candle
        and bullish_rsi
    )

    bearish = (
        bearish_trend
        and bearish_candle
        and bearish_rsi
    )

    if bullish:
        direction = "CALL"

    elif bearish:
        direction = "PUT"

    else:
        reasons = []

        if not bullish_trend and not bearish_trend:
            reasons.append("EMA trend not aligned")

        if not bullish_candle and not bearish_candle:
            reasons.append("neutral candle")

        if not bullish_rsi and not bearish_rsi:
            reasons.append(
                f"RSI outside entry zones ({rsi_value:.1f})"
            )

        elif bullish_trend and not bullish_rsi:
            reasons.append(
                f"CALL RSI not confirmed ({rsi_value:.1f})"
            )

        elif bearish_trend and not bearish_rsi:
            reasons.append(
                f"PUT RSI not confirmed ({rsi_value:.1f})"
            )

        if bullish_trend and not bullish_candle:
            reasons.append("CALL candle not bullish")

        if bearish_trend and not bearish_candle:
            reasons.append("PUT candle not bearish")

        log.info(
            "%s REJECTED: %s",
            symbol,
            "; ".join(reasons) if reasons else "no complete setup"
        )

        return None

    # Technical score only; NOT win probability.
    score = 65

    if body >= atr_value * 0.15:
        score += 15

    score += 10 if payout >= 90 else 5

    if body <= atr_value:
        score += 10

    if score < 75:
        log.info(
            "%s REJECTED: score too low (%d)",
            symbol,
            score
        )
        return None

    log.info(
        "%s SETUP: %s | score=%d | RSI=%.1f | ATR=%.6f",
        symbol,
        direction,
        score,
        rsi_value,
        atr_value
    )

    return {
        "symbol": symbol,
        "direction": direction,
        "payout": payout,
        "score": score,
        "candle_time": last["time"],
        "rsi": rsi_value
    }


# =========================================================
# TELEGRAM
# =========================================================

def telegram_send(message):

    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("Telegram secrets missing.")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": message
            },
            timeout=15
        )

        if response.status_code != 200:
            log.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                response.text[:250]
            )
            return False

        result = response.json()

        if not result.get("ok", False):
            log.error(
                "Telegram rejected message: %s",
                result
            )
            return False

        return True

    except (
        requests.RequestException,
        ValueError
    ) as exc:
        log.error(
            "Telegram send error: %s",
            exc
        )
        return False


def signal_message(signal, entry_time, sent_time):

    icon = (
        "🟢 CALL"
        if signal["direction"] == "CALL"
        else "🔴 PUT"
    )

    return (
        f"🤖 {VERSION}\n\n"
        f"{icon}\n\n"
        f"💱 PAIR: {signal['symbol']}\n"
        f"⭐ QUALITY SCORE: {signal['score']}/100\n"
        f"⏱ TIMEFRAME: 1M\n"
        f"⌛ EXPIRATION: 1M\n"
        f"💰 PAYOUT: {signal['payout']:.1f}%\n"
        f"📊 RSI: {signal['rsi']:.1f}\n"
        f"📩 SIGNAL SENT: {fmt_et(sent_time)}\n"
        f"🎯 ENTRY: {fmt_et(entry_time)}\n\n"
        f"EMA + RSI + ATR CONFIRMATION\n"
        f"⚠️ Score is not win probability.\n"
        f"Manual entry only; test on demo first."
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not API_KEY:
        log.error("OTCHARTS_API_KEY is missing.")
        return

    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return

    log.info("=======================================================")
    log.info("%s starting", VERSION)

    log.info(
        "SETTINGS: M1 | expiry=%ds | payout>=%.1f%% | "
        "min candles=%d | markets=%d | API cap=%d | scan=%ds",
        EXPIRATION,
        MIN_PAYOUT,
        MIN_CANDLES,
        MAX_MARKETS,
        MAX_API_REQUESTS,
        MAX_SCAN_SECONDS
    )

    markets = fetch_symbols()

    if not markets:
        log.error("No eligible OTC symbols found.")
        return

    chosen = rotate_markets(markets)

    log.info(
        "Eligible markets=%d | selected=%d",
        len(markets),
        len(chosen)
    )

    for market in chosen:
        log.info(
            "WATCH: %s | PAYOUT %.1f%%",
            market["symbol"],
            market["payout"]
        )

    deadline = (
        time.monotonic()
        + MAX_SCAN_SECONDS
    )

    scan_number = 0

    while (
        time.monotonic() < deadline
        and request_count < MAX_API_REQUESTS
    ):

        scan_number += 1

        log.info(
            "----- SCAN CYCLE %d | API requests=%d/%d -----",
            scan_number,
            request_count,
            MAX_API_REQUESTS
        )

        for market in chosen:

            if request_count >= MAX_API_REQUESTS:
                break

            if time.monotonic() >= deadline:
                break

            symbol = market["symbol"]

            candles = fetch_candles(symbol)

            if not candles:
                log.info(
                    "%s skipped: no confirmed closed candles",
                    symbol
                )
                continue

            signal = analyze(
                symbol,
                market["payout"],
                candles
            )

            if signal is None:
                continue

            # Candle timestamp marks its OPEN.
            # Entry is 15 seconds after the candle closes.
            entry_time = (
                signal["candle_time"]
                + timedelta(
                    seconds=TIMEFRAME + ENTRY_DELAY
                )
            )

            sent_time = now_utc()

            lead = (
                entry_time - sent_time
            ).total_seconds()

            if (
                lead < MIN_ENTRY_LEAD
                or lead > MAX_ENTRY_LEAD
            ):
                log.info(
                    "%s setup rejected: entry is %.1f seconds away",
                    symbol,
                    lead
                )
                continue

            message = signal_message(
                signal,
                entry_time,
                sent_time
            )

            log.info(
                "SIGNAL FOUND: %s %s | score=%d | entry=%s",
                symbol,
                signal["direction"],
                signal["score"],
                fmt_et(entry_time)
            )

            if telegram_send(message):
                log.info(
                    "SIGNAL SENT successfully. Stopping this run."
                )
            else:
                log.error(
                    "Telegram delivery failed. Stopping this run."
                )

            return

        if request_count >= MAX_API_REQUESTS:
            log.warning(
                "Stopped because API request cap was reached."
            )
            break

        time.sleep(SCAN_INTERVAL)

    log.info(
        "SCAN FINISHED | cycles=%d | API requests=%d | "
        "reason=%s",
        scan_number,
        request_count,
        (
            "API request cap"
            if request_count >= MAX_API_REQUESTS
            else "scan time ended"
        )
    )

    log.info(
        "No valid signal was found during this run."
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        log.info("Stopped by user.")

    except Exception:
        log.exception("Unexpected fatal error.")
