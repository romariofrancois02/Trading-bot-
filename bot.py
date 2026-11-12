
import os
import re
import time
import math
import logging
import requests

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# =====================================================
# ROMARIO TRADER PRO 10.6
# Pocket Option OTC | M30s | Expiration 60s
# Two-stage Telegram alerts
# =====================================================

VERSION = "ROMARIO TRADER PRO 10.6"

BASE_URL = "https://otcharts.com"
VENUE = "otc"

TIMEFRAME = 30
EXPIRATION = 60
CANDLE_LIMIT = 150
MIN_CANDLES = 60
MIN_PAYOUT = 92.0

WARNING_LEAD = 20
FINAL_LEAD = 5
RECHECK_BUFFER = 4

OTC_CLOCK_OFFSET = 7200

MIN_CONFIRMATIONS = 3
STOCH_TOLERANCE = 10

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
    "User-Agent": "RomarioTrader/10.6",
    "Accept": "application/json"
})


# =====================================================
# TIME
# =====================================================

def florida_time(epoch):
    return datetime.fromtimestamp(
        epoch,
        tz=timezone.utc
    ).astimezone(FLORIDA)


def wait_until(target):
    while True:
        remaining = target - time.time()

        if remaining <= 0:
            return

        time.sleep(min(remaining, 0.25))


def prepare_schedule():
    """
    Select a future full-minute entry.
    Warning: entry - 20 seconds.
    Final signal: entry - 5 seconds.
    """

    now = time.time()

    entry = (int(now // 60) + 1) * 60

    # Ensure the warning time has not passed.
    while entry - WARNING_LEAD <= now + 1:
        entry += 60

    warning_at = entry - WARNING_LEAD
    final_at = entry - FINAL_LEAD
    recheck_at = final_at - RECHECK_BUFFER

    logging.info(
        "SCHEDULE | WARNING=%s | FINAL=%s | ENTRY=%s",
        florida_time(warning_at).strftime("%I:%M:%S %p"),
        florida_time(final_at).strftime("%I:%M:%S %p"),
        florida_time(entry).strftime("%I:%M:%S %p")
    )

    return warning_at, recheck_at, final_at, entry


# =====================================================
# SECRETS AND API
# =====================================================

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


# =====================================================
# TELEGRAM
# =====================================================

def send_telegram(message):
    url = (
        f"https://api.telegram.org/bot"
        f"{TG_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        },
        timeout=10
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected message")


# =====================================================
# FOREX OTC SYMBOLS
# =====================================================

def is_forex_pair(item):
    symbol = str(item.get("symbol", "")).strip()

    match = re.fullmatch(
        r"([A-Z]{3})([A-Z]{3})_OTC",
        symbol.upper()
    )

    if not match:
        return False

    base, quote = match.groups()

    if base not in FIAT_CODES or quote not in FIAT_CODES:
        return False

    if item.get("isOtc") is not True:
        return False

    if item.get("open") is not True:
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

    pairs = []

    for item in items:
        if isinstance(item, dict) and is_forex_pair(item):
            pairs.append({
                "symbol": item["symbol"],
                "payout": float(item["payout"])
            })

    pairs.sort(key=lambda x: x["symbol"])

    logging.info(
        "OPEN FOREX OTC PAIRS >= %.1f%%: %s",
        MIN_PAYOUT,
        len(pairs)
    )

    return pairs


# =====================================================
# CANDLES
# =====================================================

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
            "%s: expected 30s, received %s",
            symbol,
            data.get("interval")
        )
        return None

    gaps = data.get("gaps") or {}

    if int(gaps.get("missingBars", 0) or 0) > 0:
        logging.warning(
            "%s: missing candles=%s",
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

    for item in raw:
        try:
            c = {
                "time": int(item["time"]),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"])
            }

            values = [
                c["open"], c["high"],
                c["low"], c["close"]
            ]

            if not all(math.isfinite(v) for v in values):
                continue

            if c["high"] < c["low"]:
                continue

            if c["high"] < max(c["open"], c["close"]):
                continue

            if c["low"] > min(c["open"], c["close"]):
                continue

            candles.append(c)

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = list(unique.values())

    # Assumes OTCharts timestamps use UTC+2.
    broker_now = time.time() + OTC_CLOCK_OFFSET

    # Remove unfinished candles.
    candles = [
        c for c in candles
        if c["time"] + TIMEFRAME <= broker_now - 1
    ]

    if len(candles) < MIN_CANDLES:
        logging.warning(
            "%s: only %s completed candles",
            symbol,
            len(candles)
        )
        return None

    recent = candles[-10:]

    for previous, current in zip(recent, recent[1:]):
        difference = current["time"] - previous["time"]

        if difference != TIMEFRAME:
            logging.warning(
                "%s: irregular candle spacing %ss",
                symbol,
                difference
            )
            return None

    logging.info(
        "%s: valid completed candles=%s",
        symbol,
        len(candles)
    )

    return candles


# =====================================================
# EMA
# =====================================================

def ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period

    result = [None] * (period - 1)
    result.append(current)

    for value in values[period:]:
        current = (
            value * multiplier
            + current * (1 - multiplier)
        )
        result.append(current)

    return result


# =====================================================
# STOCHASTIC 14,3,3
# =====================================================

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

        if any(x is None for x in values):
            smooth_k.append(None)
        else:
            smooth_k.append(sum(values) / 3)

    smooth_d = []

    for i in range(len(smooth_k)):
        if i < 17:
            smooth_d.append(None)
            continue

        values = smooth_k[i - 2:i + 1]

        if any(x is None for x in values):
            smooth_d.append(None)
        else:
            smooth_d.append(sum(values) / 3)

    return smooth_k, smooth_d


# =====================================================
# REFERENCE PAIRS
# =====================================================

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


# =====================================================
# CANDLE QUALITY
# =====================================================

def candle_quality(candle, direction):
    candle_range = candle["high"] - candle["low"]

    if candle_range <= 0:
        return False

    body = abs(candle["close"] - candle["open"])
    body_ratio = body / candle_range

    close_position = (
        candle["close"] - candle["low"]
    ) / candle_range

    if direction == "CALL":
        return (
            candle["close"] > candle["open"]
            and body_ratio >= 0.40
            and close_position >= 0.65
        )

    return (
        candle["close"] < candle["open"]
        and body_ratio >= 0.40
        and close_position <= 0.35
    )


# =====================================================
# SIGNAL ANALYSIS
# =====================================================

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
    previous_k = k_values[i - 1]

    e9 = ema9[i]
    e21 = ema21[i]
    e50 = ema50[i]

    previous_e9 = ema9[i - 1]
    previous_e21 = ema21[i - 1]

    required = [
        k, d, previous_k,
        e9, e21, e50,
        previous_e9, previous_e21
    ]

    if any(x is None for x in required):
        logging.info("%s: indicators incomplete", symbol)
        return None

    candle = candles[i]
    close = candle["close"]

    # ---------------- CALL ----------------

    call_trend = e9 > e21 and close > e21
    call_stoch = k > d

    if call_trend and call_stoch and k < 90:

        confirmations = {
            "EMA9_RISING": e9 > previous_e9,
            "EMA21_RISING": e21 > previous_e21,
            "STOCH_RISING": k > previous_k,
            "STRONG_CANDLE": candle_quality(candle, "CALL")
        }

        count = sum(confirmations.values())
        matched = reference_match(k, d, "CALL")
        score = 2 + count + int(matched)

        logging.info(
            "%s CALL | K=%.1f D=%.1f | confirms=%s/4 | score=%s/7",
            symbol, k, d, count, score
        )

        if count >= MIN_CONFIRMATIONS:
            return {
                "symbol": symbol,
                "direction": "CALL",
                "payout": pair["payout"],
                "k": k,
                "d": d,
                "score": score,
                "confirmations": count,
                "reference": matched
            }

    # ---------------- PUT ----------------

    put_trend = e9 < e21 and close < e21
    put_stoch = k < d

    if put_trend and put_stoch and k > 10:

        confirmations = {
            "EMA9_FALLING": e9 < previous_e9,
            "EMA21_FALLING": e21 < previous_e21,
            "STOCH_FALLING": k < previous_k,
            "STRONG_CANDLE": candle_quality(candle, "PUT")
        }

        count = sum(confirmations.values())
        matched = reference_match(k, d, "PUT")
        score = 2 + count + int(matched)

        logging.info(
            "%s PUT | K=%.1f D=%.1f | confirms=%s/4 | score=%s/7",
            symbol, k, d, count, score
        )

        if count >= MIN_CONFIRMATIONS:
            return {
                "symbol": symbol,
                "direction": "PUT",
                "payout": pair["payout"],
                "k": k,
                "d": d,
                "score": score,
                "confirmations": count,
                "reference": matched
            }

    logging.info(
        "%s NO SETUP | K=%.1f D=%.1f",
        symbol, k, d
    )

    return None


# =====================================================
# SCAN ALL PAIRS
# =====================================================

def scan_pairs(pairs):
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

        except Exception:
            errors += 1
            logging.exception(
                "%s scan failed",
                pair["symbol"]
            )

    logging.info(
        "SCAN COMPLETE | scanned=%s | candidates=%s | errors=%s",
        len(pairs),
        len(candidates),
        errors
    )

    return candidates, errors


def choose_best(candidates):
    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            -x["confirmations"],
            -x["score"],
            -int(x["reference"]),
            -x["payout"]
        )
    )

    return candidates[0]


# =====================================================
# TWO-STAGE ALERT SYSTEM
# =====================================================

def send_pair_warning(best, entry_epoch):
    entry_time = florida_time(entry_epoch)

    message = (
        f"🤖 <b>{VERSION}</b>\n\n"
        "🟡 <b>PAIR WARNING</b>\n\n"
        f"💱 PAIR: <b>{best['symbol']}</b>\n"
        f"💰 PAYOUT: <b>{best['payout']:.1f}%</b>\n"
        f"⏱ TIMEFRAME: <b>30 SECONDS</b>\n"
        f"⌛ EXPIRATION: <b>60 SECONDS</b>\n\n"
        f"🚀 PLANNED ENTRY: <b>{entry_time.strftime('%I:%M:%S %p')}</b>\n"
        "📨 FINAL CALL/PUT: <b>5 SECONDS BEFORE ENTRY</b>\n\n"
        "⚠️ WAIT FOR THE FINAL SIGNAL."
    )

    send_telegram(message)

    logging.info(
        "PAIR WARNING SENT | %s | ENTRY=%s",
        best["symbol"],
        entry_time.strftime("%I:%M:%S %p")
    )


def send_cancelled(best, reason, entry_epoch):
    entry_time = florida_time(entry_epoch)

    message = (
        f"🤖 <b>{VERSION}</b>\n\n"
        "⚪ <b>SIGNAL CANCELLED</b>\n\n"
        f"💱 PAIR: <b>{best['symbol']}</b>\n"
        f"🚀 PLANNED ENTRY: <b>{entry_time.strftime('%I:%M:%S %p')}</b>\n\n"
        f"Reason: {reason}\n"
        "No trade signal was issued."
    )

    send_telegram(message)

    logging.warning(
        "SIGNAL CANCELLED | %s | %s",
        best["symbol"],
        reason
    )


def send_no_signal(pair_count, errors):
    send_telegram(
        f"🤖 <b>{VERSION}</b>\n\n"
        "⚪ <b>NO VALID SIGNAL</b>\n"
        "No pair passed the minimum confirmation rules.\n\n"
        f"Pairs checked: {pair_count}\n"
        f"Minimum payout: {MIN_PAYOUT:.1f}%\n"
        f"Timeframe: {TIMEFRAME} seconds\n"
        f"Expiration: {EXPIRATION} seconds\n"
        f"Errors: {errors}\n"
        "No signal was forced."
    )


def send_final_signal(best, entry_epoch):
    signal_epoch = time.time()

    signal_time = florida_time(signal_epoch)
    entry_time = florida_time(entry_epoch)

    icon = "🟢" if best["direction"] == "CALL" else "🔴"
    reference = "YES" if best["reference"] else "NO"

    message = (
        f"🤖 <b>{VERSION}</b>\n\n"
        f"{icon} <b>{best['direction']}</b>\n\n"
        f"💱 PAIR: <b>{best['symbol']}</b>\n"
        f"💰 PAYOUT: <b>{best['payout']:.1f}%</b>\n"
        f"⏱ TIMEFRAME: <b>30 SECONDS</b>\n"
        f"⌛ EXPIRATION: <b>60 SECONDS</b>\n\n"
        f"📊 STOCH K: <b>{best['k']:.1f}</b>\n"
        f"📊 STOCH D: <b>{best['d']:.1f}</b>\n"
        f"⭐ SETUP SCORE: <b>{best['score']}/7</b>\n"
        f"✅ CONFIRMATIONS: <b>{best['confirmations']}/4</b>\n"
        f"🎯 REFERENCE MATCH: <b>{reference}</b>\n\n"
        f"📨 SIGNAL TIME: <b>{signal_time.strftime('%I:%M:%S %p')}</b>\n"
        f"🚀 ENTRY TIME: <b>{entry_time.strftime('%I:%M:%S %p')}</b>\n"
        "🌴 FLORIDA TIME\n\n"
        "⚠️ Demo-test first. No result is guaranteed."
    )

    send_telegram(message)

    logging.info(
        "FINAL SIGNAL SENT | %s | %s | SCORE=%s/7 | ENTRY=%s",
        best["direction"],
        best["symbol"],
        best["score"],
        entry_time.strftime("%I:%M:%S %p")
    )


def run_two_stage_alert(best):
    warning_at, recheck_at, final_at, entry = prepare_schedule()

    # Stage 1: wait until 20 seconds before entry.
    wait_until(warning_at)

    if time.time() >= final_at:
        logging.warning("Warning window missed. Cancelled.")
        return False

    send_pair_warning(best, entry)

    # Stage 2: recheck the selected pair shortly before final alert.
    wait_until(recheck_at)

    pair = {
        "symbol": best["symbol"],
        "payout": best["payout"]
    }

    try:
        fresh = analyze(pair)
    except Exception:
        logging.exception("Final pair verification failed")
        fresh = None

    if fresh is None:
        send_cancelled(
            best,
            "The pair no longer passes the strategy rules.",
            entry
        )
        return False

    if fresh["direction"] != best["direction"]:
        send_cancelled(
            best,
            "The direction changed during final verification.",
            entry
        )
        return False

    # Wait for exactly five seconds before entry.
    wait_until(final_at)

    # Do not send a late signal.
    if time.time() >= entry - 1:
        send_cancelled(
            best,
            "Final verification took too long. Entry window missed.",
            entry
        )
        return False

    send_final_signal(fresh, entry)
    return True


# =====================================================
# MAIN
# =====================================================

def main():
    logging.info("%s STARTING", VERSION)

    validate_secrets()

    pairs = get_symbols()

    if not pairs:
        send_no_signal(0, 0)
        return

    candidates, errors = scan_pairs(pairs)
    best = choose_best(candidates)

    if best is None:
        send_no_signal(len(pairs), errors)
        return

    logging.info(
        "BEST CANDIDATE | %s | %s | SCORE=%s/7 | CONFIRMATIONS=%s/4",
        best["symbol"],
        best["direction"],
        best["score"],
        best["confirmations"]
    )

    success = run_two_stage_alert(best)

    if success:
        logging.info("TWO-STAGE SIGNAL COMPLETED")
    else:
        logging.warning("NO FINAL SIGNAL SENT")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("BOT FAILED")
        raise
