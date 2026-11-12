
import os
import re
import time
import logging
import requests

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# =====================================================
# ROMARIO TRADER PRO 10.3
# Pocket Option OTC | M30s | Expiration 60s
# Telegram alerts only
# =====================================================

VERSION = "ROMARIO TRADER PRO 10.3"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 30
EXPIRATION = 60
CANDLE_LIMIT = 150
MIN_CANDLES = 60
MIN_PAYOUT = 92.0

ENTRY_LEAD = 15
STOCH_TOLERANCE = 10

# Pocket Option OTC candle clock is UTC+2
OTC_CLOCK_OFFSET = 7200

FLORIDA = ZoneInfo("America/New_York")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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

FIAT_CODES = set("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN
BAM BBD BDT BGN BHD BIF BMD BND BOB BRL
BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY
COP CRC CUP CVE CZK DJF DKK DOP DZD EGP
ERN ETB EUR FJD GBP GEL GHS GIP GMD GNF
GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR
IQD IRR ISK JMD JOD JPY KES KGS KHR KMF
KRW KWD KZT LAK LBP LKR LRD LSL LYD MAD
MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK
MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR
PAB PEN PGK PHP PKR PLN PYG QAR RON RSD
RUB RWF SAR SBD SCR SDG SEK SGD SLE SOS
SRD SSP STN SVC SYP SZL THB TJS TMT TND
TOP TRY TTD TWD TZS UAH UGX USD UYU UZS
VES VND VUV WST XAF XCD XOF XPF YER ZAR
""".split())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "User-Agent": "RomarioTrader/10.3",
    "Accept": "application/json"
})


def validate_secrets():
    missing = []

    if not API_KEY:
        missing.append("OTCHARTS_API_KEY")
    if not TG_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not TG_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise RuntimeError(
            "Missing GitHub Secrets: " + ", ".join(missing)
        )


def api_get(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=25
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"OTCharts HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    return response.json()


# -----------------------------------------------------
# SYMBOLS: REAL FIAT FOREX OTC ONLY
# -----------------------------------------------------

def is_forex_pair(item):
    symbol = str(item.get("symbol", "")).strip()
    name = str(item.get("name", "")).strip().upper()

    if not symbol.upper().endswith("_OTC"):
        return False

    if item.get("isOtc") is not True:
        return False

    if item.get("open") is not True:
        return False

    match = re.match(
        r"^([A-Z]{3})\s*/\s*([A-Z]{3})"
        r"(?:\s*\(?OTC\)?)$",
        name
    )

    if not match:
        return False

    base, quote = match.groups()

    if base not in FIAT_CODES or quote not in FIAT_CODES:
        return False

    try:
        payout = float(item.get("payout"))
    except (TypeError, ValueError):
        return False

    return payout >= MIN_PAYOUT


def get_symbols():
    data = api_get(
        "/v1/symbols",
        {"venue": VENUE}
    )

    items = data.get("symbols", [])

    if not isinstance(items, list):
        raise RuntimeError("Invalid symbols response")

    result = []

    for item in items:
        if isinstance(item, dict) and is_forex_pair(item):
            result.append({
                "symbol": item["symbol"],
                "name": item.get("name", item["symbol"]),
                "payout": float(item["payout"])
            })

    result.sort(key=lambda x: x["symbol"])

    logging.info(
        "Eligible Forex OTC pairs: %s",
        len(result)
    )

    return result


# -----------------------------------------------------
# CANDLES
# -----------------------------------------------------

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

    if data.get("interval") != "30s":
        logging.warning(
            "%s: wrong interval: %s",
            symbol,
            data.get("interval")
        )
        return None

    gaps = data.get("gaps") or {}

    if int(gaps.get("missingBars", 0) or 0) > 0:
        logging.warning(
            "%s: missing candles: %s",
            symbol,
            gaps.get("missingBars")
        )
        return None

    if data.get("open") is False:
        return None

    raw = data.get("candles", [])

    if not isinstance(raw, list):
        return None

    candles = []

    for c in raw:
        try:
            candle = {
                "time": int(c["time"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"])
            }

            values = [
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"]
            ]

            if not all(
                value == value and abs(value) != float("inf")
                for value in values
            ):
                continue

            if candle["high"] < candle["low"]:
                continue

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    unique = {}
    for candle in candles:
        unique[candle["time"]] = candle

    candles = list(unique.values())

    if len(candles) < MIN_CANDLES:
        logging.warning(
            "%s: only %s candles",
            symbol,
            len(candles)
        )
        return None

    # Remove a possibly unfinished latest candle.
    broker_now = time.time() + OTC_CLOCK_OFFSET

    if candles[-1]["time"] + TIMEFRAME > broker_now - 1:
        candles = candles[:-1]

    if len(candles) < MIN_CANDLES:
        logging.warning(
            "%s: insufficient completed candles",
            symbol
        )
        return None

    # Check continuity in the latest 10 bars.
    recent = candles[-10:]

    for previous, current in zip(recent, recent[1:]):
        if current["time"] - previous["time"] != TIMEFRAME:
            logging.warning(
                "%s: irregular 30s candle spacing",
                symbol
            )
            return None

    logging.info(
        "%s: valid candles=%s | interval=30s",
        symbol,
        len(candles)
    )

    return candles


# -----------------------------------------------------
# EMA
# -----------------------------------------------------

def ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    current = sum(values[:period]) / period

    output = [None] * (period - 1)
    output.append(current)

    for value in values[period:]:
        current = (
            value * multiplier
            + current * (1 - multiplier)
        )
        output.append(current)

    return output


# -----------------------------------------------------
# STOCHASTIC 14,3,3
# -----------------------------------------------------

def stochastic(candles):
    raw_k = []

    for i in range(len(candles)):
        if i < 13:
            raw_k.append(None)
            continue

        window = candles[i - 13:i + 1]

        highest = max(x["high"] for x in window)
        lowest = min(x["low"] for x in window)

        if highest == lowest:
            raw_k.append(50.0)
        else:
            raw_k.append(
                100 * (
                    candles[i]["close"] - lowest
                ) / (highest - lowest)
            )

    smooth_k = []

    for i in range(len(raw_k)):
        if i < 15:
            smooth_k.append(None)
            continue

        values = raw_k[i - 2:i + 1]

        if any(v is None for v in values):
            smooth_k.append(None)
        else:
            smooth_k.append(sum(values) / 3)

    smooth_d = []

    for i in range(len(smooth_k)):
        if i < 17:
            smooth_d.append(None)
            continue

        values = smooth_k[i - 2:i + 1]

        if any(v is None for v in values):
            smooth_d.append(None)
        else:
            smooth_d.append(sum(values) / 3)

    return smooth_k, smooth_d


# -----------------------------------------------------
# REFERENCE PAIRS: BONUS, NOT A REQUIRED FILTER
# -----------------------------------------------------

def reference_match(k, d, direction):
    references = (
        CALL_PAIRS if direction == "CALL"
        else PUT_PAIRS
    )

    for ref_k, ref_d in references:
        if (
            abs(k - ref_k) <= STOCH_TOLERANCE
            and abs(d - ref_d) <= STOCH_TOLERANCE
        ):
            return True

    return False


# -----------------------------------------------------
# SIGNAL ANALYSIS
# -----------------------------------------------------

def analyze(pair):
    symbol = pair["symbol"]
    candles = get_candles(symbol)

    if candles is None:
        return None

    closes = [x["close"] for x in candles]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)

    k_values, d_values = stochastic(candles)

    i = len(candles) - 1

    k = k_values[i]
    d = d_values[i]

    if k is None or d is None:
        logging.info("%s: Stochastic not ready", symbol)
        return None

    e9 = ema9[i]
    e21 = ema21[i]
    e50 = ema50[i]

    previous_e9 = ema9[i - 1]
    previous_k = k_values[i - 1]

    if any(
        x is None
        for x in [e9, e21, e50, previous_e9, previous_k]
    ):
        return None

    candle = candles[i]

    bullish = candle["close"] > candle["open"]
    bearish = candle["close"] < candle["open"]

    # CALL: bullish trend + stochastic direction.
    call_trend = (
        e9 > e21
        and candle["close"] > e21
    )

    call_stoch = k > d

    if call_trend and call_stoch:
        score = 2

        if e9 > previous_e9:
            score += 1

        if k > previous_k:
            score += 1

        if candle["close"] > candle["open"]:
            score += 1

        if e9 > e50:
            score += 1

        matched = reference_match(k, d, "CALL")

        if matched:
            score += 1

        logging.info(
            "%s CALL | K=%.1f D=%.1f | score=%s/7 | ref=%s",
            symbol, k, d, score, matched
        )

        return {
            "symbol": symbol,
            "direction": "CALL",
            "payout": pair["payout"],
            "k": k,
            "d": d,
            "score": score,
            "reference": matched
        }

    # PUT: bearish trend + stochastic direction.
    put_trend = (
        e9 < e21
        and candle["close"] < e21
    )

    put_stoch = k < d

    if put_trend and put_stoch:
        score = 2

        if e9 < previous_e9:
            score += 1

        if k < previous_k:
            score += 1

        if candle["close"] < candle["open"]:
            score += 1

        if e9 < e50:
            score += 1

        matched = reference_match(k, d, "PUT")

        if matched:
            score += 1

        logging.info(
            "%s PUT | K=%.1f D=%.1f | score=%s/7 | ref=%s",
            symbol, k, d, score, matched
        )

        return {
            "symbol": symbol,
            "direction": "PUT",
            "payout": pair["payout"],
            "k": k,
            "d": d,
            "score": score,
            "reference": matched
        }

    logging.info(
        "%s NO SETUP | K=%.1f D=%.1f | EMA9=%.6f EMA21=%.6f",
        symbol, k, d, e9, e21
    )

    return None


# -----------------------------------------------------
# ENTRY TIME
# -----------------------------------------------------

def florida_time(epoch):
    return datetime.fromtimestamp(
        epoch,
        tz=timezone.utc
    ).astimezone(FLORIDA)


def get_entry_time():
    now = time.time()

    entry = (
        int((now + ENTRY_LEAD) // TIMEFRAME) + 1
    ) * TIMEFRAME

    send_at = entry - ENTRY_LEAD

    delay = send_at - time.time()

    if delay > 0:
        logging.info(
            "Waiting %.1f seconds for entry alert",
            delay
        )
        time.sleep(delay)

    if time.time() >= entry:
        raise RuntimeError("Entry time passed; cancel signal")

    return entry


# -----------------------------------------------------
# TELEGRAM
# -----------------------------------------------------

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        },
        timeout=20
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message")


# -----------------------------------------------------
# MAIN
# -----------------------------------------------------

def main():
    logging.info("%s STARTING", VERSION)

    validate_secrets()

    pairs = get_symbols()

    if not pairs:
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            "⚪ <b>NO VALID SIGNAL</b>\n"
            "No open Forex OTC pair passed payout filter.\n"
            f"Minimum payout: {MIN_PAYOUT}%"
        )
        return

    candidates = []
    errors = 0

    for number, pair in enumerate(pairs, 1):
        logging.info(
            "Scanning %s (%s/%s)",
            pair["symbol"],
            number,
            len(pairs)
        )

        try:
            result = analyze(pair)

            if result:
                candidates.append(result)

        except Exception as exc:
            errors += 1
            logging.exception(
                "%s scan error: %s",
                pair["symbol"],
                exc
            )

    logging.info(
        "SCAN COMPLETE | scanned=%s | candidates=%s | errors=%s",
        len(pairs),
        len(candidates),
        errors
    )

    if not candidates:
        send_telegram(
            f"🤖 <b>{VERSION}</b>\n\n"
            "⚪ <b>NO VALID SIGNAL</b>\n"
            "No pair passed trend and Stochastic direction.\n\n"
            f"Pairs checked: {len(pairs)}\n"
            f"Minimum payout: {MIN_PAYOUT}%\n"
            f"Timeframe: {TIMEFRAME} seconds\n"
            f"Expiration: {EXPIRATION} seconds\n"
            f"Errors: {errors}\n"
            "No signal was forced."
        )
        return

    candidates.sort(
        key=lambda x: (
            -x["score"],
            -int(x["reference"]),
            -x["payout"]
        )
    )

    best = candidates[0]

    entry_epoch = get_entry_time()
    signal_epoch = time.time()

    signal_time = florida_time(signal_epoch)
    entry_time = florida_time(entry_epoch)

    icon = "🟢" if best["direction"] == "CALL" else "🔴"

    reference_text = (
        "YES" if best["reference"] else "NO"
    )

    message = (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"{icon} <b>{best['direction']}</b>\n\n"
        f"💱 PAIR: <b>{best['symbol']}</b>\n"
        f"💰 PAYOUT: <b>{best['payout']:.1f}%</b>\n"
        f"⏱ TIMEFRAME: <b>30 seconds</b>\n"
        f"⌛ EXPIRATION: <b>60 seconds</b>\n"
        f"📊 STOCH K: <b>{best['k']:.1f}</b>\n"
        f"📊 STOCH D: <b>{best['d']:.1f}</b>\n"
        f"⭐ SETUP SCORE: <b>{best['score']}/7</b>\n"
        f"🎯 REFERENCE MATCH: <b>{reference_text}</b>\n\n"
        f"📨 SIGNAL TIME: <b>{signal_time.strftime('%I:%M:%S %p')}</b>\n"
        f"🚀 ENTRY TIME: <b>{entry_time.strftime('%I:%M:%S %p')}</b>\n"
        "🌴 FLORIDA TIME\n\n"
        "⚠️ Verify the live chart before trading."
    )

    send_telegram(message)

    logging.info(
        "SIGNAL SENT | %s | %s | ENTRY %s",
        best["direction"],
        best["symbol"],
        entry_time.strftime("%I:%M:%S %p")
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("BOT FAILED")
        raise
