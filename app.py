import os
import time
import threading
import re
import csv
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Gold AI Live v7")

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
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gold AI Live v7</title>
<style>body{margin:0;background:#09101d;color:#f4f6f8;font-family:Arial,sans-serif}.wrap{max-width:1200px;margin:auto;padding:14px}h1{font-size:25px;margin:0 0 5px}.sub,.small{color:#9eabc0;font-size:13px}.tabs{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin:14px 0}button{background:#1a2740;color:#fff;border:0;border-radius:9px;padding:11px 3px;font-weight:700}button.active{background:#2677ff}.card{background:#101a2c;border:1px solid #263653;border-radius:15px;padding:15px;margin-bottom:12px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.price{font-size:34px;font-weight:900;margin-top:7px}.call{font-size:32px;font-weight:900;margin:8px 0}.buy{color:#43e28c}.sell{color:#ff6c79}.wait{color:#ffd36c}.row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #24314a}.news a{color:#8fbaff;text-decoration:none}@media(max-width:700px){.tabs{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1fr}.price{font-size:30px}}</style></head><body><div class="wrap">
<h1>🥇 Gold AI Live v7</h1><div class="sub">XAU/USD • XM360-focused candles • Technical + Multi-Timeframe + Global News</div>
<div class="tabs"><button data-t="1m">1m</button><button data-t="5m">5m</button><button data-t="15m">15m</button><button data-t="30m">30m</button><button data-t="1h">1H</button><button data-t="4h">4H</button><button data-t="1d">1D</button></div>
<div class="card"><div>XAU/USD <span id="status">LIVE</span></div><div id="price" class="price">Loading...</div><div id="source" class="small">Market data...</div></div>
<div class="grid"><div class="card"><div class="small">AI MARKET CALL</div><div id="call" class="call wait">WAIT</div><div class="row"><span>BUY possibility</span><b id="buy">-</b></div><div class="row"><span>SELL possibility</span><b id="sell">-</b></div><div class="row"><span>Model confidence</span><b id="conf">-</b></div><div class="row"><span>Global news bias</span><b id="nbias">-</b></div><div class="row"><span>News articles</span><b id="ncount">-</b></div><div class="small" id="reason">Waiting...</div></div>
<div class="card"><div class="small">XM360 / MARKET CANDLE ANALYSIS</div><div class="row"><span>Candle source</span><b id="csource">-</b></div><div class="row"><span>EMA20</span><b id="e20">-</b></div><div class="row"><span>EMA50</span><b id="e50">-</b></div><div class="row"><span>EMA200</span><b id="e200">-</b></div><div class="row"><span>RSI</span><b id="rsi">-</b></div><div class="row"><span>MACD</span><b id="macd">-</b></div><div class="row"><span>ATR</span><b id="atr">-</b></div></div></div>
<div class="card"><div class="small">STOP LOSS / TAKE PROFIT / PROFIT LOCK</div><div class="row"><span>Entry</span><b id="entry">-</b></div><div class="row"><span>Stop Loss</span><b id="sl">-</b></div><div class="row"><span>TP1</span><b id="tp1">-</b></div><div class="row"><span>TP2</span><b id="tp2">-</b></div><div class="row"><span>TP3</span><b id="tp3">-</b></div><div class="row"><span>Profit Lock</span><b id="plock">-</b></div><div class="small">Analytical levels only. The app does not place broker orders.</div></div>
<div class="card"><div class="small">GLOBAL GOLD NEWS</div><div id="news">Loading...</div></div></div>
<script>let tf="1h";const $=id=>document.getElementById(id);function put(id,v){$(id).textContent=v===null||v===undefined?"-":v}document.querySelectorAll("[data-t]").forEach(b=>b.onclick=()=>{tf=b.dataset.t;document.querySelectorAll("[data-t]").forEach(x=>x.classList.toggle("active",x===b));loadSignal()});document.querySelector('[data-t="1h"]').classList.add("active");
async function loadPrice(){try{let d=await(await fetch("/api/live-price?x="+Date.now())).json();put("price",d.price!=null?Number(d.price).toFixed(2):"Waiting");put("source","Source: "+(d.source||"-"));put("status",d.status==="backup"?"BACKUP":"LIVE")}catch(e){put("price","Error")}}
async function loadSignal(){try{let d=await(await fetch("/api/live-signal?timeframe="+encodeURIComponent(tf)+"&x="+Date.now())).json();let c=$("call");c.textContent=d.call||"WAIT";c.className="call "+(d.call==="BUY"?"buy":d.call==="SELL"?"sell":"wait");put("buy",(d.buy_probability??"-")+"%");put("sell",(d.sell_probability??"-")+"%");put("conf",(d.confidence??"-")+"%");put("nbias",d.news?.bias||"-");put("ncount",d.news?.article_count??"-");put("reason",d.reason||"-");put("csource",d.data_source||"-");let t=d.technical||{};put("e20",t.EMA20);put("e50",t.EMA50);put("e200",t.EMA200);put("rsi",t.RSI);put("macd",t.MACD);put("atr",t.ATR);let r=d.risk||{};put("entry",r.entry);put("sl",r.stop_loss);put("tp1",r.tp1);put("tp2",r.tp2);put("tp3",r.tp3);put("plock",r.profit_lock)}catch(e){put("reason","Signal temporarily unavailable")}}
async function loadNews(){try{let d=await(await fetch("/api/news?x="+Date.now())).json();if(!d.articles?.length){$("news").textContent=d.message||"News unavailable";return}$("news").innerHTML=d.articles.map(a=>`<div style="padding:8px 0;border-bottom:1px solid #24314a"><a target="_blank" rel="noopener" href="${a.url||"#"}">${a.title||"Gold news"}</a><div class="small">${a.source||""} • impact ${a.impact||"-"} • ${a.direction>0?"bullish":a.direction<0?"bearish":"neutral"}</div></div>`).join("")}catch(e){$("news").textContent="News unavailable"}}
function loadAll(){loadPrice();loadSignal();loadNews()}loadAll();setInterval(loadPrice,15000);setInterval(loadSignal,60000);setInterval(loadNews,600000);</script></body></html>
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
