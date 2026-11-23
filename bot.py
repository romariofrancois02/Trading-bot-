import asyncio
import logging
import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("ROMARIO_PRO")


# =====================================================================
# 1. KONFIGIRASYON BOT LA
# =====================================================================
CONFIG = {
    "pairs": ["PLTR_otc", "MARA_otc", "USDCHF_otc", "EURUSD_otc"],
    "timeframe_sec": 60,           # M1 (60 segonn)
    "expiry_sec": 60,              # Ekspirasyon 60 segonn (1m)
    "min_payout": 85.0,            # Filtè payout minimòm (>= 85%)
    "base_amount": 1.0,            # Montan baz (PA GEN MARTINGALE OTOMATIK)
    "eval_sec_start": 55,          # Evalye apati segonn :55
    "eval_sec_end": 58,            # Jiska segonn :58
    "entry_sec": 0,                # Pase lòd nan segonn :00
    "min_adx": 25.0,               # Fòs tandans ADX >= 25
    "min_di_diff": 4.0,            # Diferans DI+ ak DI- >= 4
    "min_body_ratio": 0.55,        # Kò bouji >= 55% gwosè total bouji a
    "candle_lookback": 120,        # 120 bouji M1 pou EMA40, ATR14, ADX14, Stoch
    "poll_interval": 0.25,         # Frekans tcheke revèy la
    "use_mock_fallback": True,     # True = si API broker pa konekte, li bay bouji tès pou voye siyal
    "force_test_signal": None,     # Mete "CALL" oswa "PUT" sèlman si w vle fòse yon tès lòd nan :00; otreman None
}


# =====================================================================
# 2. BROKER ADAPTER (KONEKTE API BROKER OU A ISIT LA)
# =====================================================================
class BrokerClient:
    def __init__(self, pairs: List[str], cfg: Dict[str, Any]):
        self.pairs = pairs
        self.cfg = cfg
        self.connected = False

    async def connect(self) -> None:
        logger.info(f"Connecting stream for pairs: {', '.join(self.pairs)}...")
        # TODO: Konekte API broker ou a isit la si w genyen l
        await asyncio.sleep(0.2)
        self.connected = True
        logger.info(f"Connected: {', '.join(sorted(self.pairs))}")

    async def get_payout(self, pair: str) -> float:
        # TODO: Ranplase avèk payout reyèl pè a soti nan broker la
        return 88.0

    def _generate_mock_trending_candles(self, pair: str, count: int = 120, bull: bool = True) -> List[Dict[str, float]]:
        """
        Jenere 120 bouji M1 ki gen tandans klè, pullback sou EMA9/EMA20,
        bon kò bouji, ak kwazman Stochastic nan zòn 20-65 (pou CALL) oswa 35-80 (pou PUT).
        """
        base_map = {
            "EURUSD_otc": 1.0870,
            "USDCHF_otc": 0.8285,
            "MARA_otc": 7.9500,
            "PLTR_otc": 135.5500,
        }
        price = base_map.get(pair, 100.0)
        step = max(price * 0.00045, 0.00025)
        direction = 1.0 if bull else -1.0

        candles: List[Dict[str, float]] = []
        for i in range(count):
            open_p = price

            # Fè dènye bouji yo fòme pullback + bon bouji siyal
            if i == count - 2:
                # Bouji anvan (prev): ti retrace / pullback anvè EMA9/EMA20
                delta = -direction * step * 0.65
                close_p = open_p + delta
                high_p = max(open_p, close_p) + step * 0.25
                low_p = min(open_p, close_p) - step * 0.45
            elif i == count - 1:
                # Bouji aktyèl (curr): bon kò bouji (body_r ~ 0.72) nan direksyon tandans lan
                delta = direction * step * 1.15
                close_p = open_p + delta
                if bull:
                    low_p = min(open_p, close_p) - step * 0.15
                    high_p = max(open_p, close_p) + step * 0.25
                else:
                    high_p = max(open_p, close_p) + step * 0.15
                    low_p = min(open_p, close_p) - step * 0.25
            else:
                # Ti osilasyon ki kenbe tandans lan solid (ADX wo, DI separe)
                wave = math.sin(i * 0.35) * step * 0.35
                delta = (direction * step * 0.85) + wave
                close_p = open_p + delta
                high_p = max(open_p, close_p) + step * 0.22
                low_p = min(open_p, close_p) - step * 0.22

            candles.append({
                "open": float(open_p),
                "high": float(high_p),
                "low": float(low_p),
                "close": float(close_p),
            })
            price = close_p

        return candles

    async def fetch_candles(self, pair: str, count: int = 120) -> List[Dict[str, float]]:
        # TODO: Ranplase avèk apèl bouji M1 reyèl broker ou a:
        # raw = await api.get_candles(pair, timeframe=60, count=count)
        raw: List[Dict[str, float]] = []

        if raw:
            return [
                {
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                }
                for c in raw[-count:]
            ]

        if self.cfg.get("use_mock_fallback", False):
            # Alterné CALL ak PUT pa minit pou tès la montre siyal klè
            minute_bucket = int(datetime.now(timezone.utc).timestamp()) // 60
            bull = (minute_bucket % 2 == 0)
            return self._generate_mock_trending_candles(pair, count=count, bull=bull)

        return []

    async def place_order(
        self,
        pair: str,
        direction: str,
        amount: float,
        expiry_sec: int = 60,
    ) -> Dict[str, Any]:
        # TODO: Ranplase avèk fonksyon acha reyèl broker ou a
        logger.info(
            f"ORDER EXECUTED -> pair={pair} | dir={direction} | amount={amount:.2f} | expiry={expiry_sec}s"
        )
        return {"status": "filled", "pair": pair, "direction": direction, "amount": amount}


# =====================================================================
# 3. KALKIL ENDIKATÈ MATEMATIK (EMA, ATR, ADX/DMI, STOCHASTIC)
# =====================================================================
def calc_ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    ema_vals: List[float] = [sum(values[:period]) / period]
    for price in values[period:]:
        ema_vals.append((price * k) + (ema_vals[-1] * (1.0 - k)))
    return ema_vals


def calc_atr_and_dmi(candles: List[Dict[str, float]], period: int = 14) -> Optional[Dict[str, float]]:
    n = len(candles)
    if n < (period * 2) + 2:
        return None

    tr_list: List[float] = []
    plus_dm_list: List[float] = []
    minus_dm_list: List[float] = []

    for i in range(1, n):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        prev_close = float(candles[i - 1]["close"])
        prev_high = float(candles[i - 1]["high"])
        prev_low = float(candles[i - 1]["low"])

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = up_move if (up_move > down_move and up_move > 0.0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0.0) else 0.0

        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    atr = sum(tr_list[:period])
    smooth_plus_dm = sum(plus_dm_list[:period])
    smooth_minus_dm = sum(minus_dm_list[:period])

    dx_list: List[float] = []

    di_plus = (100.0 * smooth_plus_dm / atr) if atr > 0 else 0.0
    di_minus = (100.0 * smooth_minus_dm / atr) if atr > 0 else 0.0
    if (di_plus + di_minus) > 0:
        dx_list.append(100.0 * abs(di_plus - di_minus) / (di_plus + di_minus))

    atr_avg = atr / period

    for i in range(period, len(tr_list)):
        atr = atr - (atr / period) + tr_list[i]
        smooth_plus_dm = smooth_plus_dm - (smooth_plus_dm / period) + plus_dm_list[i]
        smooth_minus_dm = smooth_minus_dm - (smooth_minus_dm / period) + minus_dm_list[i]

        atr_avg = atr / period
        di_plus = (100.0 * smooth_plus_dm / atr) if atr > 0 else 0.0
        di_minus = (100.0 * smooth_minus_dm / atr) if atr > 0 else 0.0

        if (di_plus + di_minus) > 0:
            dx_list.append(100.0 * abs(di_plus - di_minus) / (di_plus + di_minus))

    if len(dx_list) < period:
        return None

    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = ((adx * (period - 1)) + dx) / period

    return {
        "atr": atr_avg,
        "adx": adx,
        "di_plus": di_plus,
        "di_minus": di_minus,
    }


def calc_stochastic(
    candles: List[Dict[str, float]],
    k_period: int = 14,
    smooth_k: int = 3,
    d_period: int = 3,
) -> Optional[Dict[str, float]]:
    needed = k_period + smooth_k + d_period + 2
    if len(candles) < needed:
        return None

    raw_k: List[float] = []
    for i in range(k_period - 1, len(candles)):
        window = candles[i - k_period + 1 : i + 1]
        highest_high = max(float(c["high"]) for c in window)
        lowest_low = min(float(c["low"]) for c in window)
        close = float(candles[i]["close"])
        span = highest_high - lowest_low
        raw_k.append(50.0 if span <= 0 else (100.0 * (close - lowest_low) / span))

    if len(raw_k) < smooth_k:
        return None

    k_line = [
        sum(raw_k[i - smooth_k + 1 : i + 1]) / smooth_k
        for i in range(smooth_k - 1, len(raw_k))
    ]

    if len(k_line) < d_period + 1:
        return None

    d_line = [
        sum(k_line[i - d_period + 1 : i + 1]) / d_period
        for i in range(d_period - 1, len(k_line))
    ]

    if len(d_line) < 2:
        return None

    return {
        "k_prev": k_line[-2],
        "k_curr": k_line[-1],
        "d_prev": d_line[-2],
        "d_curr": d_line[-1],
    }


# =====================================================================
# 4. EVALUASYON SIYAL LA
# =====================================================================
def evaluate_signal(candles: List[Dict[str, float]], payout: float, cfg: Dict[str, Any]) -> Dict[str, Any]:
    forced = cfg.get("force_test_signal")
    if forced in ("CALL", "PUT"):
        return {"signal": forced, "reason": f"FORCED_TEST_{forced}"}

    min_payout = float(cfg["min_payout"])
    min_adx = float(cfg["min_adx"])
    min_di_diff = float(cfg["min_di_diff"])
    min_body_ratio = float(cfg["min_body_ratio"])

    if payout < min_payout:
        return {"signal": None, "reason": f"low_payout({payout:.1f}<{min_payout:.1f})"}

    if len(candles) < 60:
        return {"signal": None, "reason": "not_enough_candles"}

    closes = [float(c["close"]) for c in candles]
    ema9_list = calc_ema(closes, 9)
    ema20_list = calc_ema(closes, 20)
    ema40_list = calc_ema(closes, 40)

    if not ema9_list or not ema20_list or not ema40_list:
        return {"signal": None, "reason": "ema_not_ready"}

    dmi = calc_atr_and_dmi(candles, period=14)
    stoch = calc_stochastic(candles, k_period=14, smooth_k=3, d_period=3)

    if dmi is None or stoch is None:
        return {"signal": None, "reason": "indicators_not_ready"}

    atr = dmi["atr"]
    adx = dmi["adx"]
    di_plus = dmi["di_plus"]
    di_minus = dmi["di_minus"]

    if atr <= 0 or math.isnan(atr):
        return {"signal": None, "reason": "invalid_atr"}

    ema9 = ema9_list[-1]
    ema20 = ema20_list[-1]
    ema40 = ema40_list[-1]

    ema_spread = abs(ema9 - ema20)
    if ema_spread <= (0.2 * atr):
        return {"signal": None, "reason": f"ema_flat_spread({ema_spread:.6f}<={0.2*atr:.6f})"}

    curr = candles[-1]
    prev = candles[-2]
    curr_open = float(curr["open"])
    curr_high = float(curr["high"])
    curr_low = float(curr["low"])
    curr_close = float(curr["close"])

    price_extension = abs(curr_close - ema9)
    if price_extension > (1.0 * atr):
        return {"signal": None, "reason": f"overextended({price_extension:.6f}>{1.0*atr:.6f})"}

    if adx < min_adx:
        return {"signal": None, "reason": f"weak_adx({adx:.2f}<{min_adx:.2f})"}

    candle_range = curr_high - curr_low
    if candle_range <= 0:
        return {"signal": None, "reason": "zero_range_candle"}

    body_size = abs(curr_close - curr_open)
    body_ratio = body_size / candle_range
    if body_ratio < min_body_ratio:
        return {"signal": None, "reason": f"weak_body_ratio({body_ratio:.2f}<{min_body_ratio:.2f})"}

    ema9_prev = ema9_list[-2] if len(ema9_list) >= 2 else ema9
    ema20_prev = ema20_list[-2] if len(ema20_list) >= 2 else ema20
    pullback_tol = 0.25 * atr

    prev_low = float(prev["low"])
    prev_high = float(prev["high"])

    touched_ema_zone = (
        (prev_low <= ema9_prev + pullback_tol and prev_high >= ema9_prev - pullback_tol)
        or (prev_low <= ema20_prev + pullback_tol and prev_high >= ema20_prev - pullback_tol)
    )

    k_prev, k_curr = stoch["k_prev"], stoch["k_curr"]
    d_prev, d_curr = stoch["d_prev"], stoch["d_curr"]

    bull_ema_stack = (ema9 > ema20) and (ema20 > ema40)
    bull_di = (di_plus > di_minus) and ((di_plus - di_minus) >= min_di_diff)
    bull_candle_ok = (curr_close > curr_open) and (curr_close > ema9)
    bull_stoch_ok = (k_prev <= d_prev) and (k_curr > d_curr) and (20.0 <= k_curr <= 65.0)

    if bull_ema_stack and bull_di and touched_ema_zone and bull_candle_ok and bull_stoch_ok:
        return {
            "signal": "CALL",
            "reason": f"CALL_CONFIRMED(adx={adx:.1f}, di+={di_plus:.1f}, di-={di_minus:.1f}, body_r={body_ratio:.2f}, k={k_curr:.1f})",
        }

    bear_ema_stack = (ema9 < ema20) and (ema20 < ema40)
    bear_di = (di_minus > di_plus) and ((di_minus - di_plus) >= min_di_diff)
    bear_candle_ok = (curr_close < curr_open) and (curr_close < ema9)
    bear_stoch_ok = (k_prev >= d_prev) and (k_curr < d_curr) and (35.0 <= k_curr <= 80.0)

    if bear_ema_stack and bear_di and touched_ema_zone and bear_candle_ok and bear_stoch_ok:
        return {
            "signal": "PUT",
            "reason": f"PUT_CONFIRMED(adx={adx:.1f}, di-={di_minus:.1f}, di+={di_plus:.1f}, body_r={body_ratio:.2f}, k={k_curr:.1f})",
        }

    return {
        "signal": None,
        "reason": f"no_setup(adx={adx:.1f}, di+={di_plus:.1f}, di-={di_minus:.1f}, body_r={body_ratio:.2f}, pull={touched_ema_zone}, k={k_curr:.1f}/d={d_curr:.1f})",
    }


# =====================================================================
# 5. BOUK PRENSIPAL (EVALYE :55-:58, ANTREFÈ :00, SAN MARTINGALE)
# =====================================================================
async def run_bot() -> None:
    logger.info("ROMARIO PRO starting...")
    client = BrokerClient(CONFIG["pairs"], CONFIG)
    await client.connect()

    last_eval_minute_key: Optional[int] = None
    pending_signals: List[Dict[str, Any]] = []
    last_entry_minute_key: Optional[int] = None

    while True:
        now = datetime.now(timezone.utc)
        minute_key = int(now.timestamp()) // 60
        sec = now.second

        # Evalye ant :55 ak :58 segonn
        if (
            CONFIG["eval_sec_start"] <= sec <= CONFIG["eval_sec_end"]
            and minute_key != last_eval_minute_key
        ):
            last_eval_minute_key = minute_key
            pending_signals.clear()

            for pair in CONFIG["pairs"]:
                try:
                    payout = await client.get_payout(pair)
                    candles = await client.fetch_candles(pair, count=CONFIG["candle_lookback"])
                    res = evaluate_signal(candles, payout=payout, cfg=CONFIG)

                    if res["signal"] in ("CALL", "PUT"):
                        pending_signals.append({
                            "pair": pair,
                            "direction": res["signal"],
                            "payout": payout,
                            "reason": res["reason"],
                        })
                        logger.info(f"SIGNAL READY [{pair}] -> {res['reason']} (payout={payout:.1f}%)")
                    else:
                        logger.info(f"{pair}: no setup ({res['reason']})")
                except Exception as exc:
                    logger.error(f"Error evaluating {pair}: {exc}")

        # Pase lòd egzakteman nan segonn :00 pou 60s
        if (
            sec == CONFIG["entry_sec"]
            and pending_signals
            and minute_key != last_entry_minute_key
        ):
            last_entry_minute_key = minute_key
            signals_to_fire = list(pending_signals)
            pending_signals.clear()

            for sig in signals_to_fire:
                payout = await client.get_payout(sig["pair"])
                if payout < CONFIG["min_payout"]:
                    logger.info(f"Skipped {sig['pair']}: payout dropped to {payout:.1f}%")
                    continue

                # Toujou base_amount — pa gen Martingale otomatik
                stake = float(CONFIG["base_amount"])
                await client.place_order(
                    pair=sig["pair"],
                    direction=sig["direction"],
                    amount=stake,
                    expiry_sec=CONFIG["expiry_sec"],
                )

        await asyncio.sleep(CONFIG["poll_interval"])


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("ROMARIO PRO stopped by user.")
