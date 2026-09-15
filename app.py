import os
import time
import threading
import re
import csv
import xml.etree.ElementTree as ET
import html as html_lib
from urllib.parse import quote_plus
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from fastapi import FastAPI, Query
from fastapi.responses import Response
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Gold AI Live v9")

PRICE_API_KEY = os.getenv("PRICE_API_KEY", "").strip()
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "").strip()

SYMBOL = "XAU/USD"
YAHOO_SYMBOL = "GC=F"

PRICE_CACHE_SECONDS = 10
CANDLE_CACHE_SECONDS = 30
NEWS_CACHE_SECONDS = 120
XM360_CSV_PATH = os.getenv("XM360_CSV_PATH", "xm360_gold.csv").strip()

TIMEFRAME_MAP = {
    "1m": ("1m", "1d"),
    "5m": ("5m", "5d"),
    "15m": ("15m", "1mo"),
    "30m": ("30m", "1mo"),
    "1h": ("1h", "3mo"),
    "4h": ("1h", "6mo"),
    "1d": ("1d", "2y"),
}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 GoldAI-Live/6.0"
})

lock = threading.Lock()
price_cache = {"time": 0.0, "data": None}
candle_cache = {}
news_cache = {"time": 0.0, "data": None}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def cache_fresh(item, seconds):
    return item.get("data") is not None and (time.time() - item.get("time", 0)) < seconds


def td_get(endpoint, params):
    if not PRICE_API_KEY:
        return None, "NO_PRICE_API_KEY"

    p = dict(params)
    p["apikey"] = PRICE_API_KEY

    try:
        r = session.get(
            "https://api.twelvedata.com/" + endpoint,
            params=p,
            timeout=8
        )
        if r.status_code == 429:
            return None, "RATE_LIMIT"
        if r.status_code >= 400:
            return None, f"HTTP_{r.status_code}"

        data = r.json()
        if isinstance(data, dict) and data.get("status") == "error":
            return None, str(data.get("code") or data.get("message") or "TD_ERROR")

        return data, None
    except Exception as e:
        return None, type(e).__name__


def yahoo_chart(symbol, interval, range_):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = session.get(
            url,
            params={
                "interval": interval,
                "range": range_,
                "includePrePost": "true",
                "events": "div,splits"
            },
            timeout=10
        )
        if r.status_code >= 400:
            return None, f"YAHOO_HTTP_{r.status_code}"

        data = r.json()
        result = data.get("chart", {}).get("result")
        if not result:
            err = data.get("chart", {}).get("error")
            return None, str(err) if err else "YAHOO_NO_DATA"

        return result[0], None
    except Exception as e:
        return None, f"YAHOO_{type(e).__name__}"


def yahoo_candles(interval, range_):
    data, err = yahoo_chart(YAHOO_SYMBOL, interval, range_)
    if not data:
        return [], err

    timestamps = data.get("timestamp") or []
    q = (data.get("indicators", {}).get("quote") or [{}])[0]

    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    volumes = q.get("volume") or []

    rows = []
    for i, ts in enumerate(timestamps):
        try:
            o = opens[i]
            h = highs[i]
            l = lows[i]
            c = closes[i]
            if None in (o, h, l, c):
                continue
            rows.append({
                "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": int(volumes[i] or 0)
            })
        except (IndexError, TypeError, ValueError, OverflowError):
            continue

    return rows, None


def get_price():
    with lock:
        if cache_fresh(price_cache, PRICE_CACHE_SECONDS):
            return price_cache["data"]

    # Primary: Twelve Data
    td, err = td_get("price", {"symbol": SYMBOL})
    if td:
        raw = td.get("price")
        try:
            price = float(raw)
            data = {
                "symbol": SYMBOL,
                "price": price,
                "source": "Twelve Data",
                "status": "live",
                "updated": now_iso()
            }
            with lock:
                price_cache["time"] = time.time()
                price_cache["data"] = data
            return data
        except (TypeError, ValueError):
            pass

    # Backup: Yahoo Gold futures
    yahoo, yerr = yahoo_chart(YAHOO_SYMBOL, "1m", "1d")
    if yahoo:
        meta = yahoo.get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            ts = yahoo.get("timestamp") or []
            q = (yahoo.get("indicators", {}).get("quote") or [{}])[0]
            closes = q.get("close") or []
            if ts and closes:
                for value in reversed(closes):
                    if value is not None:
                        price = value
                        break

        if price is not None:
            data = {
                "symbol": SYMBOL,
                "price": float(price),
                "source": "Yahoo Finance GC=F backup",
                "status": "backup",
                "updated": now_iso(),
                "primary_error": err
            }
            with lock:
                price_cache["time"] = time.time()
                price_cache["data"] = data
            return data

    data = {
        "symbol": SYMBOL,
        "price": None,
        "status": "waiting",
        "source": None,
        "primary_error": err,
        "backup_error": yerr,
        "updated": now_iso()
    }
    return data


def read_xm360_csv():
    if not XM360_CSV_PATH or not os.path.exists(XM360_CSV_PATH):
        return [], "XM360_CSV_NOT_FOUND"
    try:
        rows = []
        with open(XM360_CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                d = {str(k).strip().lower(): v for k, v in raw.items()}
                def pick(*names):
                    for n in names:
                        if n in d and d[n] not in (None, ""):
                            return d[n]
                    return None
                dt, o, h, l, c = pick("datetime","date","time","timestamp"), pick("open","o"), pick("high","h"), pick("low","l"), pick("close","c")
                if not all(v is not None for v in (dt,o,h,l,c)):
                    continue
                try:
                    ds = str(dt).replace("Z", "+00:00")
                    if re.fullmatch(r"\d+(\.\d+)?", ds):
                        ds = datetime.fromtimestamp(float(ds), timezone.utc).isoformat()
                    elif "T" not in ds and " " in ds:
                        ds = ds.replace(" ", "T")
                    rows.append({"datetime": ds, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(pick("volume","v") or 0)})
                except Exception:
                    continue
        rows.sort(key=lambda x: x["datetime"])
        return rows, None if rows else "XM360_CSV_EMPTY"
    except Exception as e:
        return [], f"XM360_CSV_{type(e).__name__}"

def resample_4h(rows):
    grouped, bucket, current = [], None, None
    for row in rows:
        try:
            dt = datetime.fromisoformat(row["datetime"].replace("Z", "+00:00"))
            key = dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0, microsecond=0).isoformat()
        except Exception:
            continue
        if key != bucket:
            if current: grouped.append(current)
            bucket = key
            current = dict(row); current["datetime"] = key
        else:
            current["high"] = max(current["high"], row["high"])
            current["low"] = min(current["low"], row["low"])
            current["close"] = row["close"]
            current["volume"] += row.get("volume", 0)
    if current: grouped.append(current)
    return grouped

def get_candles(timeframe):
    timeframe = timeframe if timeframe in TIMEFRAME_MAP else "1h"
    with lock:
        item = candle_cache.get(timeframe)
        if item and cache_fresh(item, CANDLE_CACHE_SECONDS):
            return item["data"]

    xm_rows, xm_err = read_xm360_csv()
    if xm_rows:
        rows = resample_4h(xm_rows) if timeframe == "4h" else xm_rows
        result = {"symbol": SYMBOL, "timeframe": timeframe, "candles": rows[-500:], "source": "XM360 CSV", "status": "ok", "error": None, "updated": now_iso()}
        with lock: candle_cache[timeframe] = {"time": time.time(), "data": result}
        return result

    interval, range_ = TIMEFRAME_MAP[timeframe]
    rows, err = yahoo_candles(interval, range_)
    if timeframe == "4h" and rows:
        rows = resample_4h(rows)
    source = "Yahoo Finance GC=F"

    if not rows:
        td_interval = {"1m":"1min","5m":"5min","15m":"15min","30m":"30min","1h":"1h","4h":"4h","1d":"1day"}[timeframe]
        td, td_err = td_get("time_series", {"symbol": SYMBOL, "interval": td_interval, "outputsize": 500})
        if td and td.get("values"):
            rows = []
            for v in reversed(td["values"]):
                try:
                    rows.append({"datetime": v["datetime"], "open": float(v["open"]), "high": float(v["high"]), "low": float(v["low"]), "close": float(v["close"]), "volume": float(v.get("volume") or 0)})
                except Exception:
                    pass
            err, source = None, "Twelve Data"
        else:
            err = td_err or err

    result = {"symbol": SYMBOL, "timeframe": timeframe, "candles": rows[-500:], "source": source, "status": "ok" if rows else "error", "error": None if rows else err, "xm360_csv_status": xm_err, "updated": now_iso()}
    with lock: candle_cache[timeframe] = {"time": time.time(), "data": result}
    return result

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for price in values[period:]:
        result = (price - result) * multiplier + result
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(values):
    if len(values) < 35:
        return None, None
    fast = ema(values, 12)
    slow = ema(values, 26)
    if fast is None or slow is None:
        return None, None
    line = fast - slow

    macd_values = []
    start = 26
    for i in range(start, len(values) + 1):
        f = ema(values[:i], 12)
        s = ema(values[:i], 26)
        if f is not None and s is not None:
            macd_values.append(f - s)

    signal = ema(macd_values, 9) if len(macd_values) >= 9 else None
    return line, signal


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    return sum(trs[-period:]) / period if len(trs) >= period else None


def technical_snapshot(candles):
    closes = [x["close"] for x in candles]
    if len(closes) < 50:
        return {"score": 0.0, "ATR": None, "reason": "Not enough candle data"}
    e20, e50, e200 = ema(closes,20), ema(closes,50), ema(closes,200)
    rv, ml, ms, av = rsi(closes,14), *macd(closes), atr(candles,14)
    score = 0.0; reasons = []
    if e20 is not None and e50 is not None:
        score += 1 if e20 > e50 else -1
        reasons.append("EMA20>EMA50" if e20 > e50 else "EMA20<EMA50")
    if e200 is not None:
        score += 1 if closes[-1] > e200 else -1
        reasons.append("Price>EMA200" if closes[-1] > e200 else "Price<EMA200")
    if rv is not None:
        if rv > 55: score += .7; reasons.append("RSI bullish")
        elif rv < 45: score -= .7; reasons.append("RSI bearish")
    if ml is not None and ms is not None:
        score += .8 if ml > ms else -.8
        reasons.append("MACD bullish" if ml > ms else "MACD bearish")
    return {"score": score, "ATR": av, "EMA20": e20, "EMA50": e50, "EMA200": e200, "RSI": rv, "MACD": ml, "MACDSignal": ms, "reason": ", ".join(reasons)}

def multi_timeframe():
    out = {}; scores = []
    for tf in ["5m","15m","1h","4h"]:
        d = get_candles(tf); t = technical_snapshot(d.get("candles") or [])
        out[tf] = t; scores.append(t.get("score",0.0))
    return out, (sum(scores)/len(scores) if scores else 0.0)

def trade_setup(candles, current_price):
    """ Calculates conditional setup levels. These are not guarantees or broker orders. BUY trigger = recent high breakout + small ATR buffer. SELL trigger = recent low breakdown - small ATR buffer. """
    if not candles or current_price is None or len(candles) < 20:
        return {"available": False, "status": "WAITING FOR CANDLE DATA"}

    t = technical_snapshot(candles)
    atr_value = t.get("ATR")
    if atr_value is None or atr_value <= 0:
        return {"available": False, "status": "WAITING FOR ATR"}

    recent = candles[-20:]
    recent_high = max(x["high"] for x in recent[:-1])
    recent_low = min(x["low"] for x in recent[:-1])
    buffer = max(atr_value * 0.10, current_price * 0.00015)

    buy_trigger = round(recent_high + buffer, 2)
    sell_trigger = round(recent_low - buffer, 2)

    bullish = (
        t.get("EMA20") is not None
        and t.get("EMA50") is not None
        and t.get("RSI") is not None
        and t.get("MACD") is not None
        and t.get("MACDSignal") is not None
        and t["EMA20"] > t["EMA50"]
        and current_price > t["EMA20"]
        and t["RSI"] >= 50
        and t["MACD"] > t["MACDSignal"]
    )
    bearish = (
        t.get("EMA20") is not None
        and t.get("EMA50") is not None
        and t.get("RSI") is not None
        and t.get("MACD") is not None
        and t.get("MACDSignal") is not None
        and t["EMA20"] < t["EMA50"]
        and current_price < t["EMA20"]
        and t["RSI"] <= 50
        and t["MACD"] < t["MACDSignal"]
    )

    buy_state = "CONFIRMED ABOVE TRIGGER" if current_price >= buy_trigger and bullish else (
        "WATCH BUY BREAKOUT" if bullish else "WAIT FOR BULLISH CONFIRMATION"
    )
    sell_state = "CONFIRMED BELOW TRIGGER" if current_price <= sell_trigger and bearish else (
        "WATCH SELL BREAKDOWN" if bearish else "WAIT FOR BEARISH CONFIRMATION"
    )

    buy_sl = round(buy_trigger - atr_value * 1.25, 2)
    buy_tp1 = round(buy_trigger + atr_value * 1.25, 2)
    buy_tp2 = round(buy_trigger + atr_value * 2.0, 2)

    sell_sl = round(sell_trigger + atr_value * 1.25, 2)
    sell_tp1 = round(sell_trigger - atr_value * 1.25, 2)
    sell_tp2 = round(sell_trigger - atr_value * 2.0, 2)

    return {
        "available": True,
        "current_price": round(current_price, 2),
        "buy": {
            "trigger": buy_trigger,
            "stop_loss": buy_sl,
            "tp1": buy_tp1,
            "tp2": buy_tp2,
            "status": buy_state,
        },
        "sell": {
            "trigger": sell_trigger,
            "stop_loss": sell_sl,
            "tp1": sell_tp1,
            "tp2": sell_tp2,
            "status": sell_state,
        },
        "note": "Conditional analytical levels only; wait for confirmation and use your own risk management.",
    }


def risk_plan(call, entry, atr_value):
    if call not in ("BUY","SELL") or entry is None or atr_value is None:
        return {"available": False}
    risk = max(atr_value*1.25, entry*0.0015)
    if call == "BUY":
        sl, tp1, tp2, tp3, lock_profit = entry-risk, entry+risk, entry+risk*1.8, entry+risk*2.5, entry+risk*.5
    else:
        sl, tp1, tp2, tp3, lock_profit = entry+risk, entry-risk, entry-risk*1.8, entry-risk*2.5, entry-risk*.5
    return {"available": True, "entry": round(entry,2), "stop_loss": round(sl,2), "tp1": round(tp1,2), "tp2": round(tp2,2), "tp3": round(tp3,2), "profit_lock": round(lock_profit,2), "risk_points": round(risk,2), "rr_tp1":"1:1", "rr_tp2":"1:1.8", "rr_tp3":"1:2.5", "note":"Analytical levels only; no broker order is executed."}

def build_signal(candle_data):
    candles = candle_data.get("candles") or []
    technical = technical_snapshot(candles)
    news = get_news()
    mtf, mtf_avg = multi_timeframe()
    score = technical.get("score",0.0) + mtf_avg*.8
    if news.get("news_bias") == "BULLISH": score += 1.0
    elif news.get("news_bias") == "BEARISH": score -= 1.0
    buy = max(0.0, min(100.0, 50 + score*13))
    sell = 100 - buy
    if buy >= 65 and buy > sell: call = "BUY"
    elif sell >= 65 and sell > buy: call = "SELL"
    else: call = "WAIT"
    price = get_price(); entry = price.get("price")
    risk = risk_plan(call, entry, technical.get("ATR"))
    setup = trade_setup(candles, entry)
    confidence = round(min(95.0, 50 + abs(buy-sell)*.45), 1)
    technical_out = {k:(round(v,4) if isinstance(v,(int,float)) else v) for k,v in technical.items() if k != "score"}
    return {"call":call,"buy_probability":round(buy,1),"sell_probability":round(sell,1),"confidence":confidence,"timeframe":candle_data.get("timeframe"),"price":price,"technical":technical_out,"mtf":{k:{"score":round(v.get("score",0),2)} for k,v in mtf.items()},"news":{"bias":news.get("news_bias"),"score":news.get("news_score"),"article_count":len(news.get("articles",[]))},"risk":risk,"setup":setup,"reason":"Technical + multi-timeframe + global gold-news bias","data_source":candle_data.get("source"),"updated":now_iso()}

GOLD_PHRASES = ["gold price","gold prices","spot gold","gold futures","gold bullion","gold market","xau/usd","xauusd","precious metals","bullion","comex gold","gold etf","gold demand"]
MACRO_PHRASES = ["federal reserve","fed meeting","fed decision","interest rate","interest rates","rate hike","rate cut","inflation","consumer price index","cpi","treasury yield","treasury yields","bond yields","us dollar","dollar index","central bank","safe haven","oil prices","crude oil","geopolitical","middle east","sanctions","tariff","trade war","bank of japan","ecb","rbi","pboc"]
BLOCKED_PHRASES = ["goldfish","golden retriever","golden state","golden boot","golden globe","gold medal","gold coast","golden visa","golden gate","golden ratio"]
BULLISH_PHRASES = ["gold rises","gold rose","gold climbs","gold climbed","gold gains","gold higher","gold rebounds","gold surge","safe haven demand","rate cut","dovish","weaker dollar","dollar falls","yields fall","central bank buying"]
BEARISH_PHRASES = ["gold falls","gold fell","gold drops","gold dropped","gold lower","gold declines","gold declined","gold slips","gold slid","gold down","rate hike","rate hikes","hawkish","stronger dollar","dollar rises","yields rise","higher yields"]
SOURCE_WEIGHTS = {"reuters":5,"bloomberg":5,"wall street journal":4.5,"financial times":4.5,"cnbc":4,"kitco":4,"marketwatch":4,"investing.com":3.5,"fxstreet":3.5,"yahoo finance":3}

def parse_news_datetime(value):
    """Parse ISO-8601 or RSS/RFC822 timestamps into an aware UTC datetime."""
    if not value:
        return None
        
