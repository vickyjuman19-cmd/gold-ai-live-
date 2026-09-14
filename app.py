import os
import time
import threading
import re
import csv
import xml.etree.ElementTree as ET
import html as html_lib
from urllib.parse import quote_plus
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Gold AI Live v8")

PRICE_API_KEY = os.getenv("PRICE_API_KEY", "").strip()
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "").strip()

SYMBOL = "XAU/USD"
YAHOO_SYMBOL = "GC=F"

PRICE_CACHE_SECONDS = 15
CANDLE_CACHE_SECONDS = 60
NEWS_CACHE_SECONDS = 600
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
    confidence = round(min(95.0, 50 + abs(buy-sell)*.45), 1)
    technical_out = {k:(round(v,4) if isinstance(v,(int,float)) else v) for k,v in technical.items() if k != "score"}
    return {"call":call,"buy_probability":round(buy,1),"sell_probability":round(sell,1),"confidence":confidence,"timeframe":candle_data.get("timeframe"),"price":price,"technical":technical_out,"mtf":{k:{"score":round(v.get("score",0),2)} for k,v in mtf.items()},"news":{"bias":news.get("news_bias"),"score":news.get("news_score"),"article_count":len(news.get("articles",[]))},"risk":risk,"reason":"Technical + multi-timeframe + global gold-news bias","data_source":candle_data.get("source"),"updated":now_iso()}

GOLD_PHRASES = ["gold price","gold prices","spot gold","gold futures","gold bullion","gold market","xau/usd","xauusd","precious metals","bullion","comex gold","gold etf","gold demand"]
MACRO_PHRASES = ["federal reserve","fed meeting","fed decision","interest rate","interest rates","rate hike","rate cut","inflation","consumer price index","cpi","treasury yield","treasury yields","bond yields","us dollar","dollar index","central bank","safe haven","oil prices","crude oil","geopolitical","middle east","sanctions","tariff","trade war","bank of japan","ecb","rbi","pboc"]
BLOCKED_PHRASES = ["goldfish","golden retriever","golden state","golden boot","golden globe","gold medal","gold coast","golden visa","golden gate","golden ratio"]
BULLISH_PHRASES = ["gold rises","gold rose","gold climbs","gold climbed","gold gains","gold higher","gold rebounds","gold surge","safe haven demand","rate cut","dovish","weaker dollar","dollar falls","yields fall","central bank buying"]
BEARISH_PHRASES = ["gold falls","gold fell","gold drops","gold dropped","gold lower","gold declines","gold declined","gold slips","gold slid","gold down","rate hike","rate hikes","hawkish","stronger dollar","dollar rises","yields rise","higher yields"]
SOURCE_WEIGHTS = {"reuters":5,"bloomberg":5,"wall street journal":4.5,"financial times":4.5,"cnbc":4,"kitco":4,"marketwatch":4,"investing.com":3.5,"fxstreet":3.5,"yahoo finance":3}

def score_news_article(article):
    title=(article.get("title") or "").strip(); desc=(article.get("description") or "").strip()
    if not title: return None
    text=re.sub(r"\s+"," ",(html_lib.unescape(f"{title} {desc}")).lower()).strip()
    title_text=title.lower()
    if any(x in text for x in BLOCKED_PHRASES): return None
    gold=sum(4 if p in title_text else 2 for p in GOLD_PHRASES if p in text)
    macro=sum(1.5 for p in MACRO_PHRASES if p in text)
    if gold < 2 and macro < 3: return None
    source=((article.get("source") or {}).get("name") or "").lower()
    quality=max([v for k,v in SOURCE_WEIGHTS.items() if k in source] or [1])
    freshness=0
    try:
        dt=datetime.fromisoformat((article.get("publishedAt") or "").replace("Z","+00:00"))
        age=max(0,(datetime.now(timezone.utc)-dt).total_seconds()/3600)
        freshness=max(0,4-age/12)
    except Exception: pass
    impact=min(100, round(25+gold*5+macro*4+quality*6+freshness*5))
    direction=sum(1 for p in BULLISH_PHRASES if p in text)-sum(1 for p in BEARISH_PHRASES if p in text)
    return {"title":title,"url":article.get("url") or "","source":source or "Unknown","publishedAt":article.get("publishedAt"),"impact":impact,"direction":direction,"relevance":round(gold+macro+quality,1)}

def fetch_newsapi():
    if not NEWS_API_KEY: return []
    q='("gold price" OR "spot gold" OR "gold futures" OR bullion OR XAU OR XAUUSD) OR (("Federal Reserve" OR Fed OR "interest rates" OR inflation OR "US dollar" OR "Treasury yields") AND (gold OR XAU OR bullion))'
    try:
        r=session.get("https://newsapi.org/v2/everything",params={"q":q,"language":"en","sortBy":"publishedAt","pageSize":100,"apiKey":NEWS_API_KEY},timeout=10)
        payload=r.json()
        if r.status_code>=400 or payload.get("status")!="ok": return []
        return payload.get("articles",[])
    except Exception: return []

def fetch_google_news_rss():
    queries=["gold XAU price","gold Federal Reserve interest rates","gold Australia Canada Dubai Russia China India","gold Middle East oil dollar Treasury yields"]
    articles=[]
    for q in queries:
        try:
            url="https://news.google.com/rss/search?q="+quote_plus(q)+"&hl=en-US&gl=US&ceid=US:en"
            r=session.get(url,timeout=8)
            if r.status_code>=400: continue
            root=ET.fromstring(r.text)
            for item in root.findall(".//item")[:20]:
                articles.append({"title":item.findtext("title", ""),"description":item.findtext("description", ""),"url":item.findtext("link", ""),"source":{"name":item.findtext("source", "Google News")},"publishedAt":item.findtext("pubDate", "")})
        except Exception: continue
    return articles

def get_news():
    with lock:
        if cache_fresh(news_cache, NEWS_CACHE_SECONDS): return news_cache["data"]
    raw=fetch_newsapi()+fetch_google_news_rss(); scored=[]; seen=set()
    for article in raw:
        item=score_news_article(article)
        if not item: continue
        key=re.sub(r"[^a-z0-9]+"," ",(item["url"] or item["title"]).lower()).strip()
        if key in seen: continue
        seen.add(key); scored.append(item)
    scored.sort(key=lambda x:(x["impact"],x["relevance"],x.get("publishedAt") or ""),reverse=True)
    top=scored[:20]; news_score=sum(x["direction"] for x in top)
    bias="BULLISH" if news_score>=3 else "BEARISH" if news_score<=-3 else "MIXED"
    data={"status":"ok" if top else "unavailable","articles":top,"news_bias":bias,"news_score":news_score,"updated":now_iso(),"sources_used":["NewsAPI","Google News RSS"] if top else []}
    with lock: news_cache["time"]=time.time(); news_cache["data"]=data
    return data

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Gold AI Live v7",
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


@app.get("/api/news")
def news():
    return get_news()



HTML_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Gold AI Live v8</title>
<style>
:root{
  --bg:#07101d;--card:#0d192b;--card2:#102039;--line:#243754;
  --text:#f5f7fb;--muted:#91a0b8;--blue:#2678ff;
  --green:#20d59a;--red:#ff5d70;--gold:#f3bd3f;--yellow:#ffd66b;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at 50% -10%,#122743 0,#07101d 42%,#050b14 100%);
color:var(--text);font-family:Inter,Arial,sans-serif}
.wrap{max-width:1180px;margin:auto;padding:18px 18px 34px}
.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:14px}
.brand{display:flex;gap:12px;align-items:center}.logo{font-size:35px}
h1{font-size:27px;margin:0 0 4px;letter-spacing:-.5px}
.sub{color:#aab6c9;font-size:14px;line-height:1.35}
.livebox{text-align:right}.live{display:inline-flex;align-items:center;gap:7px;color:#00e69a;font-weight:800}
.dot{width:10px;height:10px;background:#00e69a;border-radius:50%;display:inline-block;box-shadow:0 0 12px #00e69a}
.clock{color:#91a0b8;font-size:12px;margin-top:4px}
.tabs{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;margin:16px 0}
button{background:#14243d;color:#eef3fb;border:1px solid #1c3150;border-radius:13px;padding:13px 5px;font-size:15px;font-weight:800}
button.active{background:var(--blue);border-color:#4d94ff;box-shadow:0 8px 25px #2678ff33}
.card{background:linear-gradient(145deg,#0e1b2f,#0a1525);border:1px solid var(--line);border-radius:20px;padding:18px;margin-bottom:14px;box-shadow:0 10px 35px #00000022}
.pricecard{padding:20px}
.pricegrid{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:18px;align-items:center}
.instrument{color:#dfe7f4;font-size:15px;font-weight:800}.instrument span{color:#6f819e;font-weight:600;margin-left:8px}
.price{font-size:48px;font-weight:950;letter-spacing:-1px;margin:5px 0}
.change{font-weight:800}.down{color:var(--red)}.up{color:var(--green)}
.quote{border-radius:15px;padding:15px 16px;text-align:center;border:1px solid}
.quote.sell{border-color:#ff5268;background:#341725}.quote.buy{border-color:#00c996;background:#092f2a}
.qtitle{font-weight:800;font-size:15px}.qprice{font-size:28px;font-weight:950;margin-top:4px}
.spreadrow{display:flex;justify-content:space-around;color:var(--muted);font-size:12px;margin-top:10px}
.spreadrow b{display:block;color:#e8edf5;font-size:14px;margin-top:3px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.section-title{font-size:13px;color:#a9b5c8;font-weight:800;letter-spacing:.2px}
.call{font-size:42px;font-weight:950;margin:10px 0}.call.buy{color:var(--green)}.call.sell{color:var(--red)}.call.wait{color:var(--yellow)}
.badge{float:right;border-radius:14px;padding:6px 10px;font-size:12px;font-weight:900}
.badge.bear{background:#4a1d28;color:#ff8291}.badge.bull{background:#103b31;color:#48e6b0}.badge.mix{background:#3d3217;color:#ffd66b}
.row{display:flex;justify-content:space-between;gap:15px;padding:10px 0;border-bottom:1px solid #203149}
.row:last-child{border-bottom:0}.row span{color:#dfe6f0}.row b{color:#f7f9fc}
.source{margin-top:10px;color:#8493aa;font-size:12px}
.source strong{color:#00d99b}
.notice{border:1px solid #1d785f;background:#06261f;border-radius:16px;padding:13px 15px;color:#aeeedd;margin-top:12px}
.notice.warn{border-color:#775f24;background:#2b220b;color:#ffe5a0}
.news a{color:#a9c8ff;text-decoration:none;font-weight:700}.newsitem{padding:11px 0;border-bottom:1px solid #203149}.newsitem:last-child{border-bottom:0}
.small{font-size:12px;color:var(--muted);line-height:1.4}
.refresh{display:flex;justify-content:space-between;align-items:center;color:var(--muted);font-size:12px;margin:4px 2px 12px}
.refresh strong{color:#00df9c}
@media(max-width:760px){
 .wrap{padding:12px 12px 28px}.top{align-items:flex-start}.livebox{display:none}
 h1{font-size:24px}.sub{font-size:13px}.tabs{grid-template-columns:repeat(4,1fr);gap:7px}
 .pricegrid{grid-template-columns:1fr 1fr;gap:10px}.mainprice{grid-column:1/-1}
 .price{font-size:40px}.qprice{font-size:23px}.grid{grid-template-columns:1fr}
 .card{border-radius:17px;padding:15px}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand">
      <div class="logo">🥇</div>
      <div>
        <h1>Gold AI Live v8 <span class="live"><span class="dot"></span>Live</span></h1>
        <div class="sub">XAU/USD • XM360-focused • Technical + Multi-Timeframe + Global News</div>
      </div>
    </div>
    <div class="livebox">
      <div class="live"><span class="dot"></span>Live Market</div>
      <div id="clock" class="clock">--:--:-- IST</div>
    </div>
  </div>

  <div class="tabs">
    <button data-t="1m">1m</button><button data-t="5m">5m</button>
    <button data-t="15m">15m</button><button data-t="30m">30m</button>
    <button data-t="1h">1H</button><button data-t="4h">4H</button><button data-t="1d">1D</button>
  </div>

  <div class="card pricecard">
    <div class="pricegrid">
      <div class="mainprice">
        <div class="instrument">XAU/USD (GOLD) <span id="mode">Market</span></div>
        <div id="price" class="price">Loading...</div>
        <div id="change" class="change">Live quote</div>
      </div>
      <div class="quote sell"><div class="qtitle">SELL</div><div id="sellq" class="qprice">-</div></div>
      <div class="quote buy"><div class="qtitle">BUY</div><div id="buyq" class="qprice">-</div></div>
    </div>
    <div class="spreadrow">
      <div>Spread<b id="spread">-</b></div>
      <div>Source<b id="source">-</b></div>
      <div>Updated<b id="updated">-</b></div>
    </div>
  </div>

  <div class="refresh">
    <span>Data status: <strong id="status">Connecting...</strong></span>
    <span>Auto refresh: <strong>ON</strong></span>
  </div>

  <div class="grid">
    <div class="card">
      <div class="section-title">AI MARKET CALL <span id="biasbadge" class="badge mix">WAIT</span></div>
      <div id="call" class="call wait">WAIT</div>
      <div class="row"><span>BUY possibility</span><b id="buy">-</b></div>
      <div class="row"><span>SELL possibility</span><b id="sell">-</b></div>
      <div class="row"><span>Model confidence</span><b id="conf">-</b></div>
      <div class="row"><span>Global news bias</span><b id="nbias">-</b></div>
      <div class="row"><span>News articles</span><b id="ncount">-</b></div>
      <div class="source" id="reason">Waiting for live analysis...</div>
    </div>

    <div class="card">
      <div class="section-title">MARKET CANDLE ANALYSIS <span id="candlebadge" class="badge mix">-</span></div>
      <div class="row"><span>Candle source</span><b id="csource">-</b></div>
      <div class="row"><span>EMA20</span><b id="e20">-</b></div>
      <div class="row"><span>EMA50</span><b id="e50">-</b></div>
      <div class="row"><span>EMA200</span><b id="e200">-</b></div>
      <div class="row"><span>RSI</span><b id="rsi">-</b></div>
      <div class="row"><span>MACD</span><b id="macd">-</b></div>
      <div class="row"><span>ATR</span><b id="atr">-</b></div>
    </div>
  </div>

  <div class="card">
    <div class="section-title">STOP LOSS / TAKE PROFIT / PROFIT LOCK</div>
    <div class="row"><span>Entry</span><b id="entry">-</b></div>
    <div class="row"><span>Stop Loss</span><b id="sl">-</b></div>
    <div class="row"><span>TP1</span><b id="tp1">-</b></div>
    <div class="row"><span>TP2</span><b id="tp2">-</b></div>
    <div class="row"><span>TP3</span><b id="tp3">-</b></div>
    <div class="row"><span>Profit Lock</span><b id="plock">-</b></div>
    <div class="small" style="margin-top:10px">Analytical levels only. No broker order is placed by this dashboard.</div>
  </div>

  <div class="card">
    <div class="section-title">GLOBAL GOLD NEWS</div>
    <div id="news" class="news">Loading...</div>
  </div>

  <div id="notice" class="notice warn">
    <b>Important:</b> XM360 exact live price is used only when an XM360 feed/CSV is actually connected.
    The dashboard will not pretend Yahoo futures is an exact XM360 quote.
  </div>
</div>

<script>
let tf="1h";
const $=id=>document.getElementById(id);
const put=(id,v)=>$(id).textContent=(v===null||v===undefined||v==="")?"-":v;
function setActive(){
  document.querySelectorAll("[data-t]").forEach(b=>b.classList.toggle("active",b.dataset.t===tf));
}
document.querySelectorAll("[data-t]").forEach(b=>b.onclick=()=>{tf=b.dataset.t;setActive();loadSignal()});
setActive();

function fmt(v){return v==null?"-":Number(v).toFixed(2)}
function updateClock(){
  const d=new Date();
  const s=d.toLocaleTimeString("en-IN",{hour12:false,timeZone:"Asia/Kolkata"});
  $("clock").textContent=s+" IST";
}
setInterval(updateClock,1000); updateClock();

async function loadPrice(){
  try{
    const d=await (await fetch("/api/live-price?x="+Date.now(),{cache:"no-store"})).json();
    if(d.price!=null){
      const p=Number(d.price); put("price",fmt(p));
      // For sources that expose bid/ask, use them; otherwise show the same analytical mid.
      const bid=d.sell ?? d.bid ?? p;
      const ask=d.buy ?? d.ask ?? p;
      put("sellq",fmt(bid)); put("buyq",fmt(ask));
      put("spread",fmt(Number(ask)-Number(bid)));
      put("source",d.source||"-"); put("updated",new Date().toLocaleTimeString("en-IN",{hour12:false}));
      $("status").textContent=d.status==="backup"?"BACKUP DATA":"LIVE DATA";
      $("mode").textContent=d.status==="backup"?"Backup":"Live";
      $("notice").className=d.status==="backup"?"notice warn":"notice";
      $("notice").innerHTML=d.status==="backup"
        ? "<b>Backup data:</b> The current quote is not confirmed as the exact XM360 broker quote."
        : "<b>Live data:</b> Current market feed is connected.";
    }else{
      $("status").textContent="WAITING FOR DATA";
    }
  }catch(e){$("status").textContent="CONNECTION ERROR"}
}

async function loadSignal(){
  try{
    const d=await (await fetch("/api/live-signal?timeframe="+encodeURIComponent(tf)+"&x="+Date.now(),{cache:"no-store"})).json();
    const c=$("call"); c.textContent=d.call||"WAIT";
    c.className="call "+(d.call==="BUY"?"buy":d.call==="SELL"?"sell":"wait");
    put("buy",(d.buy_probability??"-")+"%");
    put("sell",(d.sell_probability??"-")+"%");
    put("conf",(d.confidence??"-")+"%");
    put("nbias",d.news?.bias||"-");
    put("ncount",d.news?.article_count??"-");
    put("reason",d.reason||"-");
    put("csource",d.data_source||"-");
    $("candlebadge").textContent=d.data_source||"-";
    const t=d.technical||{};
    put("e20",fmt(t.EMA20)); put("e50",fmt(t.EMA50)); put("e200",fmt(t.EMA200));
    put("rsi",fmt(t.RSI)); put("macd",fmt(t.MACD)); put("atr",fmt(t.ATR));
    const r=d.risk||{};
    put("entry",fmt(r.entry)); put("sl",fmt(r.stop_loss)); put("tp1",fmt(r.tp1));
    put("tp2",fmt(r.tp2)); put("tp3",fmt(r.tp3)); put("plock",fmt(r.profit_lock));
    const bias=(d.news?.bias||"MIXED").toUpperCase();
    const bb=$("biasbadge"); bb.textContent=bias; bb.className="badge "+(bias==="BEARISH"?"bear":bias==="BULLISH"?"bull":"mix");
  }catch(e){
    $("call").textContent="WAIT"; $("call").className="call wait";
    put("reason","Signal temporarily unavailable");
  }
}

async function loadNews(){
  try{
    const d=await (await fetch("/api/news?x="+Date.now(),{cache:"no-store"})).json();
    if(!d.articles?.length){$("news").textContent=d.message||"News unavailable";return}
    $("news").innerHTML=d.articles.map(a=>{
      const dir=a.direction>0?"bullish":a.direction<0?"bearish":"neutral";
      return `<div class="newsitem"><a target="_blank" rel="noopener" href="${a.url||"#"}">${a.title||"Gold news"}</a>
      <div class="small">${a.source||""} • impact ${a.impact||"-"} • ${dir}</div></div>`;
    }).join("");
  }catch(e){$("news").textContent="News unavailable"}
}

function loadAll(){loadPrice();loadSignal();loadNews()}
loadAll();
setInterval(loadPrice,15000);
setInterval(loadSignal,60000);
setInterval(loadNews,600000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML_PAGE


@app.on_event("startup")
def startup_event():
    print("====================================")
    print(" GOLD AI LIVE v7 STARTED")
    print(" Twelve Data primary")
    print(" Yahoo GC=F backup")
    print(" Price cache: 15 sec")
    print(" Candle cache: 60 sec")
    print(" News cache: 5 min")
    print("====================================")
