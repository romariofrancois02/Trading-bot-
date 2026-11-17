
import math


# =====================================================
# ROMARIO TRADER PRO
# BOLLINGER REVERSAL + EMA PULLBACK + ENGULFING
# Pocket Option OTC | M1 | Expiration 60 seconds
# =====================================================

TIMEFRAME = 60
EXPIRATION = 60

CANDLE_LIMIT = 300
MIN_CANDLES = 100

BB_PERIOD = 20
BB_STD = 1.9

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 7

MIN_SCORE = 4


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def calculate_rsi(closes, period=7):
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def bollinger_bands(closes, period=20, std_dev=1.9):

    if len(closes) < period:
        return None, None, None

    window = closes[-period:]

    sma = sum(window) / period

    variance = sum(
        (x - sma) ** 2 for x in window
    ) / period

    sd = math.sqrt(variance)

    upper = sma + std_dev * sd
    lower = sma - std_dev * sd

    return upper, sma, lower


def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    ranges = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        ranges.append(tr)

    return sum(ranges[-period:]) / period


def analyze_candles(candles):

    if not candles or len(candles) < MIN_CANDLES:
        return None

    closes = [float(c["close"]) for c in candles]

    upper, middle, lower = bollinger_bands(
        closes, BB_PERIOD, BB_STD
    )

    ema9 = ema(closes, EMA_FAST)
    ema21 = ema(closes, EMA_MID)
    ema50 = ema(closes, EMA_SLOW)

    rsi = calculate_rsi(closes, RSI_PERIOD)

    atr = calculate_atr(candles)

    if any(
        value is None
        for value in (
            upper, middle, lower,
            ema9, ema21, ema50, rsi, atr
        )
    ):
        return None

    prev = candles[-2]
    last = candles[-1]

    p_open = float(prev["open"])
    p_close = float(prev["close"])

    l_open = float(last["open"])
    l_close = float(last["close"])

    l_high = float(last["high"])
    l_low = float(last["low"])

    if atr <= 0:
        return None

    # Evite bouji ki twò piti oswa mouvman ki pa nòmal
    candle_range = l_high - l_low

    if candle_range <= 0:
        return None

    direction = None
    strategy = None

    score = 0

    # =================================================
    # STRATEGY 1: BOLLINGER REVERSAL
    # =================================================

    if (
        (p_close <= lower or l_low <= lower)
        and l_close > l_open
        and 24 <= rsi <= 40
        and l_close < middle
    ):

        direction = "CALL"
        strategy = "BOLLINGER REVERSAL"
        score = 4

    elif (
        (p_close >= upper or l_high >= upper)
        and l_close < l_open
        and 60 <= rsi <= 76
        and l_close > middle
    ):

        direction = "PUT"
        strategy = "BOLLINGER REVERSAL"
        score = 4

    # =================================================
    # STRATEGY 2: EMA 9/21 PULLBACK
    # =================================================

    if direction is None:

        # Tandans monte
        if (
            ema9 > ema21 > ema50
            and p_close < p_open
            and prev["low"] <= ema9
            and l_close > l_open
            and l_close > ema9
            and 45 <= rsi <= 65
        ):

            direction = "CALL"
            strategy = "EMA PULLBACK"
            score = 4

        # Tandans desann
        elif (
            ema9 < ema21 < ema50
            and p_close > p_open
            and prev["high"] >= ema9
            and l_close < l_open
            and l_close < ema9
            and 35 <= rsi <= 55
        ):

            direction = "PUT"
            strategy = "EMA PULLBACK"
            score = 4

    # =================================================
    # STRATEGY 3: ENGULFING MOMENTUM
    # =================================================

    if direction is None:

        # Bullish engulfing
        if (
            p_close < p_open
            and l_close > l_open
            and l_close > p_open
            and l_open <= p_close
            and 42 <= rsi <= 62
        ):

            direction = "CALL"
            strategy = "ENGULFING MOMENTUM"
            score = 4

        # Bearish engulfing
        elif (
            p_close > p_open
            and l_close < l_open
            and l_close < p_open
            and l_open >= p_close
            and 38 <= rsi <= 58
        ):

            direction = "PUT"
            strategy = "ENGULFING MOMENTUM"
            score = 4

    if direction is None:
        return None

    # =================================================
    # CONFIRMATION
    # =================================================

    confirmations = []

    if direction == "CALL":

        if ema9 > ema21:
            confirmations.append("EMA TREND")

        if l_close > ema9:
            confirmations.append("PRICE ABOVE EMA9")

        if l_close > l_open:
            confirmations.append("BULLISH CANDLE")

        if 40 <= rsi <= 65:
            confirmations.append("RSI CONFIRMATION")

    elif direction == "PUT":

        if ema9 < ema21:
            confirmations.append("EMA TREND")

        if l_close < ema9:
            confirmations.append("PRICE BELOW EMA9")

        if l_close < l_open:
            confirmations.append("BEARISH CANDLE")

        if 35 <= rsi <= 60:
            confirmations.append("RSI CONFIRMATION")

    score += len(confirmations)

    if score < MIN_SCORE:
        return None

    # =================================================
    # PREVENT EXTREME LATE ENTRIES
    # =================================================

    ema_distance = abs(l_close - ema21) / atr

    # Pa bloke siyal otomatikman sou yon sèl kritè;
    # bay done distans lan pou siveyans.
    return {
        "direction": direction,
        "strategy": strategy,

        "score": min(score, 8),

        "rsi": round(rsi, 2),

        "ema9": round(ema9, 8),
        "ema21": round(ema21, 8),
        "ema50": round(ema50, 8),

        "ema_distance_atr": round(
            ema_distance, 3
        ),

        "bb_upper": round(upper, 8),
        "bb_middle": round(middle, 8),
        "bb_lower": round(lower, 8),

        "atr": round(atr, 8),

        "close": l_close,

        "confirmations": confirmations,

        "candle_time": last.get("time")
    }
