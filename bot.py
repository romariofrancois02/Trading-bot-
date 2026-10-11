
import os
import json
import time
import requests
import pandas as pd
from datetime import datetime, timezone
from otcharts import Client

# ROMARIO TRADER
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("OTCHARTS_API_KEY")

STATE_FILE = "last_signals.json"
TIMEFRAME_SECONDS = 60
CANDLE_LIMIT = 120
RSI_BUY_MIN, RSI_BUY_MAX = 50, 68
RSI_PUT_MIN, RSI_PUT_MAX = 32, 50


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {message}", flush=True)


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        log("ERÈ: Telegram Secrets yo manke.")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=15
        )

        data = response.json()

        if response.ok and data.get("ok"):
            log("Telegram konfime mesaj la.")
            return True

        log(f"Telegram erè: {data}")
        return False

    except Exception as error:
        log(f"Telegram pa reponn: {error}")
        return False


def get_candles(client, venue, symbol):
    log(f"Ap chèche {venue} / {symbol}")

    try:
        bars = client.candles(
            venue,
            symbol,
            tf=TIMEFRAME_SECONDS,
            limit=CANDLE_LIMIT
        )

        rows = []

        for bar in bars:
            row = {
                "time": field(bar, "time"),
                "Open": field(bar, "open"),
                "High": field(bar, "high"),
                "Low": field(bar, "low"),
                "Close": field(bar, "close")
            }

            if all(row[k] is not None for k in
                   ["time", "Open", "High", "Low", "Close"]):
                rows.append(row)

        if len(rows) < 60:
            log(f"Pa gen ase bouji: {venue}/{symbol}")
            return None

        df = pd.DataFrame(rows)

        for col in ["Open", "High", "Low", "Close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna().reset_index(drop=True)

        if len(df) < 60:
            log(f"Done yo pa ase oswa yo pa valab: {symbol}")
            return None

        log(f"Done resevwa: {symbol}, {len(df)} bouji")
        return df

    except Exception as error:
        log(f"ERÈ DONE {venue}/{symbol}: {error}")
        return None


def analyze(df):
    df = df.copy()

    df["EMA20"] = df["Close"].ewm(
        span=20, adjust=False
    ).mean()

    df["EMA50"] = df["Close"].ewm(
        span=50, adjust=False
    ).mean()

    change = df["Close"].diff()
    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14, adjust=False, min_periods=14
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14, adjust=False, min_periods=14
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"] = 100 - (100 / (1 + rs))
    df = df.dropna().reset_index(drop=True)

    if len(df) < 55:
        return None

    # Dènye bouji a ka poko fini.
    # Nou itilize dènye bouji anvan li kòm bouji konfime.
    previous = df.iloc[-3]
    candle = df.iloc[-2]

    close = float(candle["Close"])
    open_price = float(candle["Open"])
    high = float(candle["High"])
    low = float(candle["Low"])
    ema20 = float(candle["EMA20"])
    ema50 = float(candle["EMA50"])
    rsi = float(candle["RSI"])

    prev_close = float(previous["Close"])
    prev_ema20 = float(previous["EMA20"])

    candle_range = high - low
    if candle_range <= 0:
        return None

    body_ratio = abs(close - open_price) / candle_range

    # Filtre ki pèmèt trend continuation san egzije
    # breakout/retest pou chak siyal.
    call_ok = (
        ema20 > ema50
        and close > ema20
        and prev_close >= prev_ema20
        and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX
        and close > open_price
        and body_ratio >= 0.25
    )

    put_ok = (
        ema20 < ema50
        and close < ema20
        and prev_close <= prev_ema20
        and RSI_PUT_MIN <= rsi <= RSI_PUT_MAX
        and close < open_price
        and body_ratio >= 0.25
    )

    if call_ok:
        side = "CALL 🟢"
    elif put_ok:
        side = "PUT 🔴"
    else:
        return None

    return {
        "side": side,
        "price": close,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "time": str(candle["time"])
    }


def get_symbols(client, venue):
    try:
        items = client.symbols(venue)
        symbols = []

        for item in items:
            symbol = field(item, "symbol")
            name = field(item, "name", symbol)

            if symbol:
                symbols.append({
                    "symbol": str(symbol),
                    "name": str(name or symbol)
                })

        log(f"{venue}: {len(symbols)} enstriman disponib pou API key sa a.")
        return symbols

    except Exception as error:
        log(f"ERÈ KATALÒG {venue}: {error}")
        return []


def main():
    log("ROMARIO TRADER AP DEMARE")

    if not TOKEN or not CHAT_ID or not API_KEY:
        log(
            "STOP: Ajiste TELEGRAM_TOKEN, TELEGRAM_CHAT_ID "
            "ak OTCHARTS_API_KEY nan GitHub Secrets."
        )
        return

    # Client la itilize OTCHARTS_API_KEY nan anviwònman an.
    client = Client()
    state = load_state()

    venues = [
        ("forex", "FOREX"),
        ("otc", "POCKET OPTION OTC")
    ]

    pending = []
    updated_keys = {}

    for venue, label in venues:
        log(f"Ap chaje lis pè pou {label}")
        symbols = get_symbols(client, venue)

        for item in symbols:
            symbol = item["symbol"]
            name = item["name"]
            key = f"{venue}:{symbol}"

            df = get_candles(client, venue, symbol)
            if df is None:
                continue

            signal = analyze(df)

            if signal is None:
                log(f"Pa gen siyal konfime: {label} / {name}")
                continue

            # Evite voye menm siyal la plizyè fwa
            # pou menm bouji a.
            candle_key = signal["time"] + "|" + signal["side"]

            if state.get(key) == candle_key:
                log(f"Siyal deja voye: {name} / {candle_key}")
                continue

            message = (
                "👑 ROMARIO TRADER\n\n"
                f"Mache: {label}\n"
                f"Pè: {name}\n"
                f"Siyal: {signal['side']}\n"
                f"Pri bouji a: {signal['price']}\n"
                f"EMA20: {signal['ema20']}\n"
                f"EMA50: {signal['ema50']}\n"
                f"RSI(14): {signal['rsi']:.1f}\n"
                f"Bouji: {signal['time']}\n\n"
                "Estrateji: EMA20/50 + RSI + bouji direksyon.\n"
                "Siyal eksperimantal; pa gen garanti pwofi.\n"
                "Verifye pri ak bouji sou platfòm ou anvan trade."
            )

            pending.append((key, candle_key, message))
            log(f"SIYAL JWENN: {label} / {name} / {signal['side']}")

            # Pa kite yon sèl egzekisyon depase twòp tan.
            time.sleep(0.2)

    if not pending:
        log("ANALIZ FINI: pa gen nouvo siyal.")
        return

    # Voye siyal yo youn apre lòt pou evite mesaj twò long.
    for key, candle_key, message in pending:
        if send_telegram(message):
            state[key] = candle_key
            updated_keys[key] = candle_key

    if updated_keys:
        save_state(state)
        log(f"Nouvo siyal trete: {len(updated_keys)}")

    log("ANALIZ FINI")


if __name__ == "__main__":
    main()
