import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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

app = FastAPI(title="Gold AI Live v10")

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


# ---------------------------
# Fast multi-timeframe signal engine (v10)
# ---------------------------
TF_ORDER = ["1m", "5m", "15m", "30m", "1h", "1d"]
TF_WEIGHTS = {"1m":0.05, "5m":0.10, "15m":0.15, "30m":0.20, "1h":0.25, "1d":0.25}

def news_direction(news):
    """Return -1..+1 from the live gold-news feed."""
    articles = news.get("articles") or []
    if not articles:
        return 0.0
    weighted = 0.0
    total = 0.0
    for a in articles[:20]:
        direction = float(a.get("direction") or 0)
        impact = max(1.0, float(a.get("impact") or 1))
        weighted += direction * impact
        total += abs(impact)
    if total <= 0:
        return 0.0
    return max(-1.0, min(1.0, weighted / (total * 1.5)))

def timeframe_signal(timeframe, candles, current_price, news):
    """Score one timeframe from technicals + live news. No order is executed."""
    t = technical_snapshot(candles)
    closes = [x["close"] for x in candles]
    if current_price is None or len(closes) < 50:
        return {"timeframe": timeframe, "signal":"WAIT", "strength":0,
                "buy_probability":50.0, "sell_probability":50.0,
                "reason":"Waiting for enough live candle data"}

    score = 0.0
    reasons = []
    e20, e50, e200 = t.get("EMA20"), t.get("EMA50"), t.get("EMA200")
    rv, ml, ms = t.get("RSI"), t.get("MACD"), t.get("MACDSignal")

    if e20 is not None and e50 is not None:
        if e20 > e50: score += 20; reasons.append("EMA bullish")
        else: score -= 20; reasons.append("EMA bearish")
    if e200 is not None:
        if current_price > e200: score += 15; reasons.append("above EMA200")
        else: score -= 15; reasons.append("below EMA200")
    if rv is not None:
        if rv >= 60: score += 15; reasons.append("RSI bullish")
        elif rv <= 40: score -= 15; reasons.append("RSI bearish")
        elif rv >= 52: score += 7; reasons.append("RSI positive")
        elif rv <= 48: score -= 7; reasons.append("RSI negative")
    if ml is not None and ms is not None:
        if ml > ms: score += 15; reasons.append("MACD bullish")
        else: score -= 15; reasons.append("MACD bearish")

    # Short momentum: direction of the latest 3 closes.
    if len(closes) >= 4:
        mom = closes[-1] - closes[-4]
        if mom > 0: score += 10; reasons.append("momentum up")
        elif mom < 0: score -= 10; reasons.append("momentum down")

    # 20-candle breakout/breakdown confirmation.
    recent = candles[-21:-1] if len(candles) >= 21 else candles[:-1]
    if recent:
        hi = max(x["high"] for x in recent)
        lo = min(x["low"] for x in recent)
        if current_price > hi: score += 10; reasons.append("breakout")
        elif current_price < lo: score -= 10; reasons.append("breakdown")

    nd = news_direction(news)
    if nd > 0.25: score += 15; reasons.append("news bullish")
    elif nd < -0.25: score -= 15; reasons.append("news bearish")
    else: reasons.append("news mixed")

    score = max(-100.0, min(100.0, score))
    buy = max(0.0, min(100.0, 50.0 + score * 0.5))
    sell = 100.0 - buy

    if score >= 60: signal = "STRONG BUY"
    elif score >= 30: signal = "BUY"
    elif score <= -60: signal = "STRONG SELL"
    elif score <= -30: signal = "SELL"
    else: signal = "WAIT"

    return {
        "timeframe": timeframe,
        "signal": signal,
        "strength": round(abs(score), 1),
        "score": round(score, 1),
        "buy_probability": round(buy, 1),
        "sell_probability": round(sell, 1),
        "price": round(float(current_price), 2),
        "rsi": round(rv, 2) if rv is not None else None,
        "ema20": round(e20, 2) if e20 is not None else None,
        "ema50": round(e50, 2) if e50 is not None else None,
        "ema200": round(e200, 2) if e200 is not None else None,
        "macd": round(ml, 4) if ml is not None else None,
        "macd_signal": round(ms, 4) if ms is not None else None,
        "news_bias": news.get("news_bias", "MIXED"),
        "news_score": news.get("news_score", 0),
        "reason": ", ".join(reasons[:8]),
    }

def build_all_signals():
    price_data = get_price()
    current_price = price_data.get("price")
    news = get_news()
    results = {}

    def one(tf):
        d = get_candles(tf)
        return tf, timeframe_signal(tf, d.get("candles") or [], current_price, news)

    # Fetch all requested timeframes in parallel so the dashboard does not hang.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(one, tf) for tf in TF_ORDER]
        for f in as_completed(futures):
            tf, sig = f.result()
            results[tf] = sig

    weighted = sum(results[tf].get("score", 0) * TF_WEIGHTS[tf] for tf in TF_ORDER if tf in results)
    higher = [results.get(tf, {}).get("score", 0) for tf in ["30m","1h","1d"] if tf in results]
    higher_avg = sum(higher) / len(higher) if higher else 0

    # Strong overall direction needs both the weighted score and higher-timeframe confirmation.
    overall_score = (weighted * 0.6) + (higher_avg * 0.4)
    if overall_score >= 60: overall = "STRONG BUY"
    elif overall_score >= 30: overall = "BUY"
    elif overall_score <= -60: overall = "STRONG SELL"
    elif overall_score <= -30: overall = "SELL"
    else: overall = "WAIT"

    return {
        "symbol": SYMBOL,
        "price": price_data,
        "overall": overall,
        "overall_score": round(overall_score, 1),
        "news": {
            "bias": news.get("news_bias", "MIXED"),
            "score": news.get("news_score", 0),
            "article_count": len(news.get("articles", [])),
            "updated": news.get("updated"),
        },
        "timeframes": {tf: results.get(tf, {"timeframe":tf,"signal":"WAIT","reason":"No data"}) for tf in TF_ORDER},
        "updated": now_iso(),
        "disclaimer": "Analytical signal only. No broker order is executed and no signal is guaranteed.",
    }

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
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(str(value))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def score_news_article(article):
    title = (article.get("title") or "").strip()
    desc = (article.get("description") or "").strip()
    if not title:
        return None

    text = re.sub(r"\s+", " ", html_lib.unescape(f"{title} {desc}")).lower().strip()
    title_text = title.lower()

    if any(x in text for x in BLOCKED_PHRASES):
        return None

    gold_hits = [p for p in GOLD_PHRASES if p in text]
    macro_hits = [p for p in MACRO_PHRASES if p in text]

    # Accept normal gold headlines as well as macro headlines that explicitly
    # mention gold/XAU. This avoids the old filter hiding useful news.
    is_gold = bool(gold_hits) or bool(re.search(r"\b(xau|gold|bullion|precious metals)\b", text))
    is_macro_gold = bool(macro_hits) and bool(re.search(r"\b(gold|xau|bullion)\b", text))
    if not (is_gold or is_macro_gold):
        return None

    source = ((article.get("source") or {}).get("name") or "").strip()
    source_l = source.lower()
    quality = max([v for k, v in SOURCE_WEIGHTS.items() if k in source_l] or [1])

    freshness = 0.0
    dt = parse_news_datetime(article.get("publishedAt"))
    if dt:
        age_hours = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
        freshness = max(0.0, 5.0 - age_hours / 6.0)

    bullish = sum(1 for p in BULLISH_PHRASES if p in text)
    bearish = sum(1 for p in BEARISH_PHRASES if p in text)
    direction = bullish - bearish

    impact = min(
        100,
        round(
            35
            + len(gold_hits) * 6
            + len(macro_hits) * 4
            + quality * 5
            + freshness * 5
            + min(abs(direction), 3) * 4
        )
    )

    return {
        "title": title,
        "url": article.get("url") or "",
        "source": source or "News feed",
        "publishedAt": article.get("publishedAt"),
        "impact": impact,
        "direction": direction,
        "relevance": round(len(gold_hits) * 2 + len(macro_hits) + quality, 1),
    }


def fetch_newsapi():
    if not NEWS_API_KEY:
        return []
    q = (
        '("gold price" OR "spot gold" OR "gold futures" OR bullion OR XAU OR XAUUSD) '
        'OR (("Federal Reserve" OR Fed OR "interest rates" OR inflation OR '
        '"US dollar" OR "Treasury yields") AND (gold OR XAU OR bullion))'
    )
    try:
        r = session.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": q,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 100,
                "apiKey": NEWS_API_KEY,
            },
            timeout=10,
        )
        payload = r.json()
        if r.status_code >= 400 or payload.get("status") != "ok":
            return []
        return payload.get("articles", [])
    except Exception:
        return []


def fetch_google_news_rss():
    # Several independent queries improve coverage when one Google News
    # result page is temporarily empty.
    queries = [
        "gold XAU price when:1d",
        "gold Federal Reserve interest rates when:1d",
        "gold US dollar Treasury yields when:1d",
        "gold central bank geopolitical oil China India when:1d",
        "gold market XAUUSD when:1d",
    ]
    articles = []

    for q in queries:
        try:
            url = (
                "https://news.google.com/rss/search?q="
                + quote_plus(q)
                + "&hl=en-US&gl=US&ceid=US:en"
            )
            r = session.get(url, timeout=8)
            if r.status_code >= 400:
                continue

            root = ET.fromstring(r.text)
            for item in root.findall(".//item")[:30]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                pub = item.findtext("pubDate", "")
                source_name = item.findtext("source", "Google News")
                articles.append(
                    {
                        "title": title,
                        "description": item.findtext("description", ""),
                        "url": link,
                        "source": {"name": source_name or "Google News"},
                        "publishedAt": pub,
                    }
                )
        except Exception:
            continue

    return articles


def get_news():
    with lock:
        if cache_fresh(news_cache, NEWS_CACHE_SECONDS):
            return news_cache["data"]

    raw = fetch_newsapi() + fetch_google_news_rss()
    scored = []
    seen = set()

    for article in raw:
        item = score_news_article(article)
        if not item:
            continue

        key = re.sub(
            r"[^a-z0-9]+",
            " ",
            (item["url"] or item["title"]).lower(),
        ).strip()

        if not key or key in seen:
            continue
        seen.add(key)
        scored.append(item)

    # Freshest/high-impact headlines first.
    scored.sort(
        key=lambda x: (
            x["impact"],
            x["relevance"],
            parse_news_datetime(x.get("publishedAt")) or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )

    top = scored[:20]
    news_score = sum(x["direction"] for x in top)

    if news_score >= 3:
        bias = "BULLISH"
    elif news_score <= -3:
        bias = "BEARISH"
    else:
        bias = "MIXED"

    data = {
        "status": "ok" if top else "unavailable",
        "articles": top,
        "news_bias": bias,
        "news_score": news_score,
        "updated": now_iso(),
        "sources_used": ["NewsAPI", "Google News RSS"] if top else [],
        "message": (
            f"{len(top)} live gold-related headlines loaded"
            if top
            else "Live gold news temporarily unavailable"
        ),
    }

    with lock:
        news_cache["time"] = time.time()
        news_cache["data"] = data

    return data


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Gold AI Live v10",
        "time": now_iso()
    }


@app.get("/api/live-price")
def live_price():
    return get_price()


@app.get("/api/candles")
def candles(timeframe: str = Query("1h")):
    return get_candles(timeframe)


@app.get("/api/live-signal")
def live_signal(timeframe: str = Query("1h")):
    data = get_candles(timeframe)
    signal = build_signal(data)
    signal["timeframe"] = timeframe
    signal["price"] = get_price()
    signal["updated"] = now_iso()
    return signal


@app.get("/api/signals")
def signals():
    return build_all_signals()


@app.get("/api/news")
def news():
    return get_news()


@app.get("/api/dashboard")
def legacy_dashboard(timeframe: str = Query("1h")):
    """Backward-compatible combined endpoint for older cached frontends."""
    price = get_price()
    candles_data = get_candles(timeframe)
    signal = build_signal(candles_data)
    signal["timeframe"] = timeframe
    signal["price"] = price
    signal["updated"] = now_iso()
    news_data = get_news()
    return {
        **signal,
        "symbol": price.get("symbol", SYMBOL),
        "price": price.get("price"),
        "live_price": price,
        "quote": price,
        "market": price,
        "source": price.get("source"),
        "status": price.get("status"),
        "signal": signal,
        "news": news_data,
        "news_bias": news_data.get("news_bias", "NEUTRAL"),
        "news_score": news_data.get("news_score", 50),
        "articles": news_data.get("articles", []),
        "updated": now_iso(),
    }


@app.get("/sw.js")
def service_worker_reset():
    # Remove any older service worker that may be serving stale dashboard HTML/JS.
    js = """ self.addEventListener('install', () => self.skipWaiting()); self.addEventListener('activate', event => { event.waitUntil((async () => { await self.registration.unregister(); const clients = await self.clients.matchAll(); clients.forEach(client => client.navigate(client.url)); })()); }); """
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )



HTML_PAGE = r""" <!doctype html> <html lang="en"> <head> <meta charset="utf-8"> <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"> <title>Gold AI Live v10</title> <style> *{box-sizing:border-box}body{margin:0;background:#08111f;color:#f5f7fb;font-family:Arial,sans-serif}.wrap{max-width:900px;margin:auto;padding:16px}.head{display:flex;justify-content:space-between;align-items:center;gap:10px}.head h1{margin:0;font-size:25px}.live{color:#20d59a;font-weight:800}.sub{color:#94a3b8;margin-top:5px;font-size:13px}.pricebox,.overall,.card{background:#0e1b2d;border:1px solid #263a57;border-radius:18px;padding:18px;margin-top:14px}.price{font-size:42px;font-weight:900;margin:8px 0}.source{color:#8fa0b8;font-size:12px}.overall{display:flex;justify-content:space-between;align-items:center}.overall b{font-size:27px}.green{color:#20d59a}.red{color:#ff6375}.yellow{color:#ffd66b}.muted{color:#94a3b8}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:14px}.card{margin:0}.tf{font-size:16px;font-weight:800;color:#aebbd0}.sig{font-size:24px;font-weight:900;margin:7px 0}.bar{height:7px;background:#1d2a3d;border-radius:9px;overflow:hidden;margin:9px 0}.bar i{display:block;height:100%;background:#20d59a}.row{display:flex;justify-content:space-between;color:#aebbd0;font-size:13px;margin-top:5px}.news{line-height:1.35}.article{border-top:1px solid #22334d;padding:10px 0}.article a{color:#dbe6f7;text-decoration:none;font-weight:700}.refresh{margin-top:12px;color:#8292a9;font-size:12px}@media(max-width:600px){.grid{grid-template-columns:1fr}.price{font-size:38px}.overall b{font-size:23px}} </style> </head> <body><div class="wrap"> <div class="head"><div><h1>Gold AI Live <span class="live">LIVE</span></h1><div class="sub">XAU/USD • Live price + multi-timeframe technicals + gold news</div></div></div> <div class="pricebox"><div class="muted">XAU/USD</div><div id="price" class="price">Loading...</div><div id="source" class="source">Connecting to live price...</div></div> <div class="overall"><div><div class="muted">OVERALL MARKET SIGNAL</div><b id="overall">WAIT</b></div><div style="text-align:right"><div class="muted">News</div><b id="newsBias">-</b></div></div> <div class="grid" id="signals"></div> <div class="pricebox news"><div style="font-size:20px;font-weight:800">Gold News</div><div id="newsList" class="muted" style="margin-top:8px">Loading news...</div></div> <div id="status" class="refresh">Auto refresh: price 10s • signals 30s • news 2m</div> </div> <script> const $=id=>document.getElementById(id); function cls(s){return s.includes('BUY')?'green':s.includes('SELL')?'red':'yellow'} function esc(x){return String(x??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))} async function loadPrice(){try{const d=await fetch('/api/live-price?x='+Date.now(),{cache:'no-store'}).then(r=>r.json()); if(d.price!=null){$('price').textContent=Number(d.price).toFixed(2);$('source').textContent=(d.source||'Live')+' • '+(d.updated||'')}else{$('price').textContent='Unavailable';$('source').textContent='Live price unavailable; retrying...'}}catch(e){$('source').textContent='Price connection retrying...'}} async function loadSignals(){try{const d=await fetch('/api/signals?x='+Date.now(),{cache:'no-store'}).then(r=>r.json());$('overall').textContent=d.overall||'WAIT';$('overall').className=cls(d.overall||'WAIT');$('newsBias').textContent=d.news?.bias||'MIXED';$('newsBias').className=cls(d.news?.bias||'');let html='';for(const tf of ['1m','5m','15m','30m','1h','1d']){const x=d.timeframes?.[tf]||{};const buy=Number(x.buy_probability||50);html+=`<div class="card"><div class="tf">${tf}</div><div class="sig ${cls(x.signal||'WAIT')}">${esc(x.signal||'WAIT')}</div><div class="bar"><i style="width:${Math.max(0,Math.min(100,buy))}%"></i></div><div class="row"><span>BUY ${buy.toFixed(1)}%</span><span>SELL ${Number(x.sell_probability||50).toFixed(1)}%</span></div><div class="row"><span>RSI ${x.rsi??'-'}</span><span>Score ${x.score??0}</span></div><div class="row"><span colspan="2">${esc(x.reason||'Waiting for data')}</span></div></div>`}$('signals').innerHTML=html;$('status').textContent='Live • updated '+(d.updated||'')+' • auto refresh 10s/30s/2m'}catch(e){$('status').textContent='Signal data retrying automatically...'}} async function loadNews(){try{const d=await fetch('/api/news?x='+Date.now(),{cache:'no-store'}).then(r=>r.json());const a=d.articles||[];if(!a.length){$('newsList').textContent=d.message||'News temporarily unavailable';return}$('newsList').innerHTML=a.slice(0,8).map(x=>`<div class="article"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a><div class="muted">${esc(x.source||'News')} • ${esc(x.publishedAt||'')}</div></div>`).join('')}catch(e){$('newsList').textContent='News retrying automatically...'}} loadPrice();loadSignals();loadNews();setInterval(loadPrice,10000);setInterval(loadSignals,30000);setInterval(loadNews,120000); </script></body></html> """

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(
        content=HTML_PAGE,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.on_event("startup")
def startup_event():
    print("====================================")
    print(" GOLD AI LIVE v10 STARTED")
    print(" Twelve Data primary")
    print(" Yahoo GC=F backup")
    print(" Price cache: 10 sec")
    print(" Candle cache: 30 sec")
    print(" News cache: 2 min")
    print("====================================")
