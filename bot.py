import os, time, math, logging, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# =========================================================
# ROMARIO TRADER PRO CLEAN 1.3
# OTCharts OTC | M1 candles | 1-minute expiry | Telegram alerts
# Scans a rotating pool of more than 5 markets, not auto-trading.
# API endpoints/response schema are assumed from the user's prior setup;
# verify against OTCharts documentation if responses differ.
# =========================================================

VERSION = "ROMARIO TRADER PRO CLEAN 1.3"
BASE_URL = "https://otcharts.com"
VENUE = "otc"
TIMEFRAME = 60
EXPIRATION = 60
MIN_PAYOUT = 85.0
MIN_CANDLES = 25
CANDLE_LIMIT = 50
MAX_MARKETS = 15            # markets analyzed per run, rotating pool
MAX_API_REQUESTS = 100      # guard against burning API quota
SCAN_INTERVAL = 5
MAX_SCAN_SECONDS = 120
ENTRY_DELAY = 15            # seconds after the next candle closes
MIN_ENTRY_LEAD = 3
MAX_ENTRY_LEAD = 80
EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 20
RSI_PERIOD = 7
ATR_PERIOD = 14

ET = ZoneInfo("America/New_York")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("romario")

API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SESSION = requests.Session()
SESSION.headers.update({"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"})
request_count = 0



def now_utc():
    return datetime.now(timezone.utc)

def now_et():
    return now_utc().astimezone(ET)

def fmt_et(dt):
    return dt.astimezone(ET).strftime("%I:%M:%S %p ET")

def api_get(path, params=None):
    global request_count
    if request_count >= MAX_API_REQUESTS:
        log.warning("API request guard reached (%s).", MAX_API_REQUESTS)
        return None
    request_count += 1
    try:
        r = SESSION.get(BASE_URL + path, params=params, timeout=20)
        if r.status_code != 200:
            log.warning("API %s returned HTTP %s: %s", path, r.status_code, r.text[:250])
            return None
        return r.json()
    except Exception as e:
        log.warning("API request error: %s", e)
        return None

def unwrap(obj):
    # Common OTCharts response wrappers
    if isinstance(obj, dict):
        for key in ("data", "result", "results", "symbols", "candles", "items"):
            v = obj.get(key)
            if isinstance(v, (list, dict)):
                return v
    return obj

def as_list(obj):
    obj = unwrap(obj)
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        # Some APIs return symbol->metadata dictionaries
        out = []
        for k, v in obj.items():
            if isinstance(v, dict):
                v.setdefault("symbol", k)
                out.append(v)
        return out
    return []

def getv(d, keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default

def symbol_name(row):
    if isinstance(row, str):
        return row
    return str(getv(row, ["symbol", "name", "ticker", "instrument", "pair", "asset"], "")).strip()

def payout_value(row):
    raw = getv(row, ["payout", "payout_percent", "profit", "profit_percent", "rate", "return", "percentage"], None)
    if raw is None:
        return 0.0
    try:
        val = float(str(raw).replace("%", "").strip())
        # Normalize fractional payouts (e.g. 0.92) into percentages.
        return val * 100 if 0 < val <= 1 else val
    except Exception:
        return 0.0

def is_open(row):
    status = getv(row, ["open", "is_open", "active", "enabled", "status", "tradeable", "tradable"], True)
    if isinstance(status, bool):
        return status
    s = str(status).lower()
    return s not in ("false", "0", "closed", "inactive", "disabled", "off", "unavailable")

def fetch_symbols():
    raw = api_get("/v1/symbols", {"venue": VENUE})
    rows = as_list(raw)
    markets = []
    seen = set()
    for row in rows:
        name = symbol_name(row)
        if not name:
            continue
        # Keep OTC instruments only; endpoint is already filtered, but enforce naming if possible.
        if not name.lower().endswith("_otc"):
            continue
        payout = payout_value(row)
        if payout < MIN_PAYOUT or not is_open(row):
            continue
        if name in seen:
            continue
        seen.add(name)
        markets.append({"symbol": name, "payout": payout, "raw": row})
    markets.sort(key=lambda m: (-m["payout"], m["symbol"]))
    log.info("OTC symbols received=%s | eligible payout/open=%s", len(rows), len(markets))
    return markets

def rotate_markets(markets):
    if not markets:
        return []
    # Rotate start position by current 5-minute bucket so each run explores different names.
    bucket = int(now_utc().timestamp() // 300)
    start = bucket % len(markets)
    rotated = markets[start:] + markets[:start]
    return rotated[:MAX_MARKETS]

def parse_time(raw):
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            # OTCharts timestamps may be seconds or milliseconds.
            val = float(raw)
            if val > 1e12:
                val /= 1000.0
            return datetime.fromtimestamp(val, tz=timezone.utc)
        s = str(raw).strip()
        if s.isdigit():
            val = float(s)
            if val > 1e12:
                val /= 1000.0
            return datetime.fromtimestamp(val, tz=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def candle_fields(c):
    if isinstance(c, (list, tuple)) and len(c) >= 5:
        # Common array order: timestamp, open, high, low, close, optional volume
        ts, o, h, l, cl = c[:5]
    elif isinstance(c, dict):
        ts = getv(c, ["timestamp", "time", "date", "datetime", "t"])
        o = getv(c, ["open", "o"])
        h = getv(c, ["high", "h"])
        l = getv(c, ["low", "l"])
        cl = getv(c, ["close", "c"])
    else:
        return None
    try:
        dt = parse_time(ts)
        vals = [float(o), float(h), float(l), float(cl)]
        if dt is None or not all(math.isfinite(x) for x in vals):
            return None
        op, hi, lo, close = vals
        if hi < lo or op <= 0 or hi <= 0 or lo <= 0 or close <= 0:
            return None
        return {"time": dt, "open": op, "high": hi, "low": lo, "close": close}
    except Exception:
        return None

def fetch_candles(symbol):
    raw = api_get("/v1/candles", {"venue": VENUE, "symbol": symbol, "tf": TIMEFRAME, "limit": CANDLE_LIMIT})
    rows = as_list(raw)
    parsed = [candle_fields(x) for x in rows]
    parsed = [x for x in parsed if x is not None]
    parsed.sort(key=lambda x: x["time"])
    dedup = {}
    for c in parsed:
        dedup[c["time"]] = c
    parsed = list(dedup.values())
    # Timestamp assumed to mark candle OPEN. Only candles whose full 60s have elapsed are used.
    cutoff = now_utc() - timedelta(seconds=TIMEFRAME)
    closed = [c for c in parsed if c["time"] <= cutoff]
    log.info("%s: API candles=%s | confirmed closed=%s", symbol, len(parsed), len(closed))
    return closed

def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    out = values[0]
    for x in values[1:]:
        out = x * k + out * (1-k)
    return out

def rsi(values, period=7):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))

def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i-1]
        trs.append(max(c["high"]-c["low"], abs(c["high"]-prev["close"]), abs(c["low"]-prev["close"])))
    return sum(trs[-period:]) / period

def analyze(symbol, payout, candles):
    if len(candles) < MIN_CANDLES:
        log.info("%s skipped: only %d closed candles (minimum %d)", symbol, len(candles), MIN_CANDLES)
        return None
    closes = [c["close"] for c in candles]
    e9, e21, e20 = ema(closes, EMA_FAST), ema(closes, EMA_MID), ema(closes, EMA_SLOW)
    rv, av = rsi(closes, RSI_PERIOD), atr(candles, ATR_PERIOD)
    if any(x is None for x in (e9, e21, e20, rv, av)) or av <= 0:
        return None
    last, prev = candles[-1], candles[-2]
    # Direction/trend filters with a modest pullback confirmation.
    bullish = e9 > e21 and e21 >= e20 and last["close"] > e9 and last["close"] > last["open"] and rv >= 52 and rv <= 72
    bearish = e9 < e21 and e21 <= e20 and last["close"] < e9 and last["close"] < last["open"] and rv >= 28 and rv <= 48
    # Reject candles too small/noisy or unusually large (avoid late chase candles).
    body = abs(last["close"] - last["open"])
    if body < av * 0.08 or body > av * 1.35:
        return None
    if bullish:
        direction = "CALL"
    elif bearish:
        direction = "PUT"
    else:
        return None
    score = 0
    if (e9 > e21 if direction == "CALL" else e9 < e21):
        score += 25
    if (e21 >= e20 if direction == "CALL" else e21 <= e20):
        score += 20
    if (52 <= rv <= 72 if direction == "CALL" else 28 <= rv <= 48):
        score += 20
    score += 15 if body >= av * 0.15 else 0
    score += 10 if payout >= 90 else 5
    score += 10 if abs(last["close"]-last["open"]) <= av * 1.0 else 0
    if score < 75:
        return None
    return {"symbol": symbol, "direction": direction, "payout": payout, "score": score,
            "candle_time": last["time"], "close": last["close"], "rsi": rv}

def telegram_send(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram secrets missing; signal not sent.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=15)
        if resp.status_code != 200:
            log.error("Telegram error %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.error("Telegram send error: %s", e)
        return False

def signal_message(sig, entry_dt, sent_dt):
    emoji = "ð¢ CALL" if sig["direction"] == "CALL" else "ð´ PUT"
    return (f"ð¤ {VERSION}\n\n{emoji}\n\n"
            f"ð± PAIR: {sig['symbol']}\n"
            f"â­ QUALITY SCORE: {sig['score']}/100\n"
            f"â± TIMEFRAME: 1M\n"
            f"â EXPIRATION: 1M\n"
            f"ð° PAYOUT: {sig['payout']:.1f}%\n"
            f"ð© SIGNAL SENT: {fmt_et(sent_dt)}\n"
            f"ð¯ ENTRY: {fmt_et(entry_dt)}\n\n"
            f"EMA + RSI + ATR CONFIRMATION\n"
            f"â ï¸ Score is not win probability. Manual entry only; test on demo first.")

def main():
    if not API_KEY:
        log.error("OTCHARTS_API_KEY is missing.")
        return
    log.info("=======================================================\n%s starting", VERSION)
    log.info("Settings: M1 | Expiration 60s | payout >= %.1f%% | min closed candles=%d | rotating pool=%d", MIN_PAYOUT, MIN_CANDLES, MAX_MARKETS)
    markets = fetch_symbols()
    if not markets:
        log.error("No eligible OTC symbols found. Check API response fields/payout values.")
        return
    chosen = rotate_markets(markets)
    log.info("Eligible OTC markets=%d | selected for this run=%d", len(markets), len(chosen))
    for m in chosen:
        log.info("WATCH: %s | PAYOUT %.1f%%", m["symbol"], m["payout"])
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    seen_candle = set()
    while time.monotonic() < deadline and request_count < MAX_API_REQUESTS:
        for m in chosen:
            if request_count >= MAX_API_REQUESTS or time.monotonic() >= deadline:
                break
            candles = fetch_candles(m["symbol"])
            if not candles:
                continue
            sig = analyze(m["symbol"], m["payout"], candles)
            if not sig:
                continue
            # Do not alert repeatedly from the same closed candle.
            key = (sig["symbol"], sig["candle_time"])
            if key in seen_candle:
                continue
            seen_candle.add(key)
            # Enter 15s after the NEXT M1 candle closes (last closed candle + 75s).
            entry_dt = sig["candle_time"] + timedelta(seconds=TIMEFRAME + ENTRY_DELAY)
            sent_dt = now_utc()
            lead = (entry_dt - sent_dt).total_seconds()
            if lead < MIN_ENTRY_LEAD or lead > MAX_ENTRY_LEAD:
                log.info("%s signal rejected: entry lead %.1fs is outside window", sig["symbol"], lead)
                continue
            msg = signal_message(sig, entry_dt, sent_dt)
            log.info("SIGNAL FOUND: %s %s score=%d entry=%s", sig["symbol"], sig["direction"], sig["score"], fmt_et(entry_dt))
            if telegram_send(msg):
                log.info("SIGNAL_SENT to Telegram; stopping run.")
            else:
                log.error("Signal detected but Telegram delivery failed; stopping to avoid duplicate alerts.")
            return
        time.sleep(SCAN_INTERVAL)
    log.info("Scan ended: no valid setup found. API requests used=%d", request_count)



if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
