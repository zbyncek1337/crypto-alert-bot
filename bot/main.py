import os
import asyncio
import requests
from telegram import Bot
from telegram.error import TelegramError

TOKEN   = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

COINS = {
    "SOL/USDT":  "SOLUSD",
    "ETH/USDT":  "ETHUSD",
    "DOGE/USDT": "XDGUSD",
    "ADA/USDT":  "ADAUSD",
    "XRP/USDT":  "XXRPZUSD",
}

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"

EMA_SHORT        = 9
EMA_LONG         = 21
RSI_PERIOD       = 14
ATR_PERIOD       = 14
RSI_LOW          = 45
RSI_HIGH         = 65
VOL_SPIKE_MULT   = 1.5
PRICE_CHANGE_MIN = 0.8
EMA_TREND        = 50
ATR_SL_MULT      = 1.5
ATR_TP_MULT      = 2.5
ALERT_COOLDOWN   = 3600
CHECK_INTERVAL   = 900

def calc_ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    changes  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [max(c, 0) for c in changes]
    losses   = [abs(min(c, 0)) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    return sum(trs[-period:]) / period

def fetch_closed_candles(pair, interval, count=100):
    resp = requests.get(KRAKEN_URL.format(pair=pair, interval=interval), timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken: {data['error']}")
    key     = next(k for k in data["result"] if k != "last")
    candles = data["result"][key][:-1][-count:]
    return {
        "opens":   [float(c[1]) for c in candles],
        "highs":   [float(c[2]) for c in candles],
        "lows":    [float(c[3]) for c in candles],
        "closes":  [float(c[4]) for c in candles],
        "volumes": [float(c[6]) for c in candles],
    }

def analyze_15m(ohlcv):
    closes, highs, lows, volumes = ohlcv["closes"], ohlcv["highs"], ohlcv["lows"], ohlcv["volumes"]
    price    = closes[-1]
    change   = ((closes[-1] - closes[-2]) / closes[-2]) * 100
    ema9     = calc_ema(closes, EMA_SHORT)
    ema21    = calc_ema(closes, EMA_LONG)
    rsi      = calc_rsi(closes, RSI_PERIOD)
    atr      = calc_atr(highs, lows, closes, ATR_PERIOD)
    avg_vol  = sum(volumes[-25:-1]) / 24 if len(volumes) >= 25 else None
    vol_ratio = volumes[-1] / avg_vol if avg_vol and avg_vol > 0 else None
    return {
        "price": price, "change": change, "ema9": ema9, "ema21": ema21,
        "rsi": rsi, "atr": atr, "vol_ratio": vol_ratio,
        "trend_15m": ema9 is not None and ema21 is not None and ema9 > ema21,
        "rsi_ok":    rsi is not None and RSI_LOW <= rsi <= RSI_HIGH,
        "rsi_hot":   rsi is not None and rsi > RSI_HIGH,
        "vol_ok":    vol_ratio is not None and vol_ratio >= VOL_SPIKE_MULT,
        "pump_ok":   change >= PRICE_CHANGE_MIN,
    }

def analyze_1h(ohlcv):
    closes = ohlcv["closes"]
    ema50  = calc_ema(closes, EMA_TREND)
    return {"ema50": ema50, "trend_1h": ema50 is not None and closes[-1] > ema50}

def determine_tier(a15, a1h):
    if a15["trend_15m"] and a15["rsi_ok"] and a15["vol_ok"] and a15["pump_ok"] and a1h["trend_1h"]:
        return "VIP"
    if a15["trend_15m"] and a15["pump_ok"] and (a15["rsi_hot"] or not a15["vol_ok"]):
        return "WATCH"
    if (a15["vol_ok"] or a15["pump_ok"]) and not a15["trend_15m"]:
        return "WARNING"
    return None

def esc(text):
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

def fmt(value, decimals=4):
    return esc(f"{value:,.{decimals}f}")

async def send_vip(bot, symbol, a15, a1h):
    price = a15["price"]
    sl    = round(price - ATR_SL_MULT * a15["atr"], 6)
    tp    = round(price + ATR_TP_MULT * a15["atr"], 6)
    msg = (
        f"🚀 *VIP INTRADAY SIGNAL* 🚀\n\n"
        f"Pár: `{esc(symbol)}`\n"
        f"Vstup: `{fmt(price)} $` 📈\n"
        f"15min změna: `{esc(f'{a15[chr(99)+chr(104)+chr(97)+chr(110)+chr(103)+chr(101)]:+.2f}')}%`\n\n"
        f"🛑 Stop\\-loss:   `{fmt(sl)} $`\n"
        f"🎯 Take\\-profit: `{fmt(tp)} $`\n"
        f"⚖️ R/R ratio:   `1 : {esc(str(round(ATR_TP_MULT/ATR_SL_MULT, 2)))}`\n\n"
        f"EMA {EMA_SHORT}/{EMA_LONG} \\(15m\\): ✅\n"
        f"EMA {EMA_TREND} \\(1h\\): ✅\n"
        f"RSI 14: `{a15['rsi']}` ✅\n"
        f"Objem: `{esc(f'{a15[chr(118)+chr(111)+chr(108)+chr(95)+chr(114)+chr(97)+chr(116)+chr(105)+chr(111)]:.1f}')}×` ✅\n\n"
        f"Dva timeframy potvrzují — čistý vstup\\!"
    )
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="MarkdownV2")

async def send_watch(bot, symbol, a15):
    reason = "RSI přepálené" if a15["rsi_hot"] else "volume chybí"
    msg = (
        f"👀 *SLEDUJ — {esc(symbol)}*\n\n"
        f"Cena: `{fmt(a15['price'])} $` 📈\n"
        f"15min změna: `{esc(f'{a15[chr(99)+chr(104)+chr(97)+chr(110)+chr(103)+chr(101)]:+.2f}')}%`\n\n"
        f"EMA {EMA_SHORT}/{EMA_LONG} \\(15m\\): ✅  RSI: `{a15['rsi']}` ⚠️\n\n"
        f"Pohyb probíhá, ale {esc(reason)} — možný pozdní vstup\\."
    )
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="MarkdownV2")

async def send_warning(bot, symbol, a15):
    msg = (
        f"⚠️ *VAROVÁNÍ — {esc(symbol)}*\n\n"
        f"Cena: `{fmt(a15['price'])} $` 📈\n"
        f"15min změna: `{esc(f'{a15[chr(99)+chr(104)+chr(97)+chr(110)+chr(103)+chr(101)]:+.2f}')}%`\n\n"
        f"EMA {EMA_SHORT}/{EMA_LONG} \\(15m\\): ❌ \\(downtrend\\)  RSI: `{a15['rsi']}`\n\n"
        f"Pump jde proti trendu — vyšší riziko\\!"
    )
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="MarkdownV2")

async def main():
    if not TOKEN or not CHAT_ID:
        print("❌ Chybí TELEGRAM_TOKEN nebo TELEGRAM_CHAT_ID.")
        return
    bot       = Bot(token=TOKEN)
    cooldowns = {s: 0.0 for s in COINS}
    print("💰 Intraday radar zapnut: " + ", ".join(COINS.keys()))
    while True:
        now = asyncio.get_event_loop().time()
        for symbol, pair in COINS.items():
            try:
                ohlcv_15m = fetch_closed_candles(pair, 15, 100)
                await asyncio.sleep(0.5)
                ohlcv_1h  = fetch_closed_candles(pair, 60, 60)
                a15  = analyze_15m(ohlcv_15m)
                a1h  = analyze_1h(ohlcv_1h)
                tier = determine_tier(a15, a1h)
                print(f"  {symbol:<12} {a15['price']:>12,.4f} $  {a15['change']:+.2f}%  15m {'✅' if a15['trend_15m'] else '❌'}  1h {'✅' if a1h['trend_1h'] else '❌'}  RSI {a15['rsi']}  {'→ '+tier if tier else ''}")
                if tier and now >= cooldowns[symbol]:
                    if tier == "VIP":
                        await send_vip(bot, symbol, a15, a1h)
                    elif tier == "WATCH":
                        await send_watch(bot, symbol, a15)
                    elif tier == "WARNING":
                        await send_warning(bot, symbol, a15)
                    cooldowns[symbol] = now + ALERT_COOLDOWN
            except Exception as e:
                print(f"  ❌ {symbol}: {e}")
            await asyncio.sleep(1)
        print()
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
