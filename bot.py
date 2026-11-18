import math

# =====================================================
# PARAMÈT POU BOUJI 30 SEGONN + EKSPIRASYON 1 MINIT
# =====================================================
TIMEFRAME = 30            # Bouji 30 segonn
EXPIRATION = 60           # Ekspirasyon 1 minit (2 bouji 30s)
ENTRY_LEAD = 10           # Alèt 10 segonn anvan antre (:20 ak :50)
CANDLE_LIMIT = 100
MIN_CANDLES = 30
MIN_PAYOUT = 85.0

# 5 Pi Bon Pè Likid pou Live Stream OTCharts Lite la
ALLOWED_PAIRS = {
    "EURUSD_otc",
    "GBPUSD_otc",
    "AUDUSD_otc",
    "USDCAD_otc",
    "EURJPY_otc"
}


# =====================================================
# FONKSYON BAZ (EMA AK RSI)
# =====================================================
def ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (value * multiplier) + (result * (1 - multiplier))
    return result


def calculate_rsi(closes, period=7):
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# =====================================================
# ENDIKATÈ RAPID POU 30 SEGONN (BOLLINGER, STOCH, KELTNER, CCI)
# =====================================================
def bollinger_bands(closes, period=20, std_dev=2.2):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    sd = math.sqrt(variance)
    return sma + (std_dev * sd), sma, sma - (std_dev * sd)


def fast_stochastic(candles, period=5):
    if len(candles) < period + 2:
        return None, None
    k_values = []
    for i in range(-3, 0):
        subset = candles[i - period + 1 : i + 1] if i != -1 else candles[-period:]
        low_min = min(c["low"] for c in subset)
        high_max = max(c["high"] for c in subset)
        if high_max == low_min:
            k_values.append(50.0)
        else:
            k_values.append(100.0 * (subset[-1]["close"] - low_min) / (high_max - low_min))
    k = k_values[-1]
    d = sum(k_values) / len(k_values)
    return k, d


def keltner_channels(candles, period=20, multiplier=1.5):
    if len(candles) < period + 1:
        return None, None, None
    closes = [c["close"] for c in candles]
    mid = ema(closes, period)
    if mid is None:
        return None, None, None
    trs = []
    for i in range(-period, 0):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / period
    return mid + (multiplier * atr), mid, mid - (multiplier * atr)


def calculate_cci(candles, period=14):
    if len(candles) < period:
        return None
    tps = [(c["high"] + c["low"] + c["close"]) / 3.0 for c in candles[-period:]]
    sma_tp = sum(tps) / period
    mean_dev = sum(abs(tp - sma_tp) for tp in tps) / period
    if mean_dev == 0:
        return 0.0
    return (tps[-1] - sma_tp) / (0.015 * mean_dev)


# =====================================================
# ESTRATEJI 3-EN-1 POU REZILTA SOLID NAN 30 SEGONN
# =====================================================
def analyze_candles(candles):
    if len(candles) < MIN_CANDLES:
        return None

    closes = [c["close"] for c in candles]
    bb_upper, bb_mid, bb_lower = bollinger_bands(closes, period=20, std_dev=2.2)
    kc_upper, kc_mid, kc_lower = keltner_channels(candles, period=20, multiplier=1.5)
    ema20 = ema(closes, 20)
    rsi = calculate_rsi(closes, 7)
    k, d = fast_stochastic(candles, period=5)
    cci = calculate_cci(candles, period=14)

    if any(v is None for v in (bb_upper, kc_upper, ema20, rsi, k, d, cci)):
        return None

    prev = candles[-2]   # Bouji 30s ki sot fèmen anvan an
    last = candles[-1]   # Bouji 30s aktyèl la

    p_open, p_close = prev["open"], prev["close"]
    l_open, l_close = last["open"], last["close"]
    l_high, l_low = last["high"], last["low"]

    # Evite bouji plat ki pa gen volim (doji)
    if abs(l_close - l_open) == 0:
        return None

    direction = None

    # ---------------- 1. BOLLINGER REVERSAL (Rebondisman) ----------------
    if (p_close <= bb_lower or l_low <= bb_lower) and l_close > l_open and l_close > bb_lower and l_close < bb_mid:
        if 24 <= rsi <= 38:
            direction = "CALL"
    elif (p_close >= bb_upper or l_high >= bb_upper) and l_close < l_open and l_close < bb_upper and l_close > bb_mid:
        if 62 <= rsi <= 76:
            direction = "PUT"

    # ---------------- 2. EMA 20 + STOCHASTIC PULLBACK (Tandans) ----------------
    if direction is None:
        if l_close > ema20 and p_close < p_open and prev["low"] <= ema20 and l_close > l_open:
            if k > d and (20 <= k <= 65) and (48 <= rsi <= 65):
                direction = "CALL"
        elif l_close < ema20 and p_close > p_open and prev["high"] >= ema20 and l_close < l_open:
            if k < d and (35 <= k <= 80) and (35 <= rsi <= 52):
                direction = "PUT"

    # ---------------- 3. KELTNER CHANNEL + CCI BREAKOUT (Kasur Rapid) ----------------
    if direction is None:
        # Kasur monte: Pri a kase liy anwo Keltner nan avèk CCI ant +100 ak +220
        if p_close <= kc_upper and l_close > kc_upper and l_close > l_open:
            if (100 <= cci <= 220) and (52 <= rsi <= 68):
                direction = "CALL"
        # Kasur desann: Pri a kase liy anba Keltner nan avèk CCI ant -100 ak -220
        elif p_close >= kc_lower and l_close < kc_lower and l_close < l_open:
            if (-220 <= cci <= -100) and (32 <= rsi <= 48):
                direction = "PUT"

    if direction is None:
        return None

    return {
        "direction": direction,
        "score": 6,
        "rsi": rsi,
        "k": k,
        "d": d,
        "adx": cci,
        "plus_di": 0.0,
        "minus_di": 0.0,
        "ema9": ema20,
        "ema21": bb_lower,
        "ema50": bb_upper,
        "close": l_close,
        "candle_time": last["time"]
    }
