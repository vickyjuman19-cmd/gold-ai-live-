import os
import time
import threading
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Gold AI Live v6")

PRICE_API_KEY = os.getenv("PRICE_API_KEY", "").strip()
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "").strip()

SYMBOL = "XAU/USD"
YAHOO_SYMBOL = "GC=F"

PRICE_CACHE_SECONDS = 15
CANDLE_CACHE_SECONDS = 60
NEWS_CACHE_SECONDS = 1800

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


def get_candles(timeframe):
    timeframe = timeframe if timeframe in TIMEFRAME_MAP else "1h"

    with lock:
        item = candle_cache.get(timeframe)
        if item and cache_fresh(item, CANDLE_CACHE_SECONDS):
            return item["data"]

    interval, range_ = TIMEFRAME_MAP[timeframe]

    # 4H is constructed from Yahoo 1H candles.
    rows, err = yahoo_candles(interval, range_)

    if timeframe == "4h" and rows:
        grouped = []
        bucket = None
        current = None

        for row in rows:
            dt = datetime.fromisoformat(row["datetime"])
            hour_bucket = dt.replace(
                hour=(dt.hour // 4) * 4,
                minute=0,
                second=0,
                microsecond=0
            ).isoformat()

            if bucket != hour_bucket:
                if current:
                    grouped.append(current)
                bucket = hour_bucket
                current = {
                    "datetime": hour_bucket,
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"]
                }
            else:
                current["high"] = max(current["high"], row["high"])
                current["low"] = min(current["low"], row["low"])
                current["close"] = row["close"]
                current["volume"] += row["volume"]

        if current:
            grouped.append(current)
        rows = grouped

    # Primary Twelve Data fallback for candles if Yahoo failed.
    if not rows:
        td_interval = {
            "1m": "1min",
            "5m": "5min",
            "15m": "15min",
            "30m": "30min",
            "1h": "1h",
            "4h": "4h",
            "1d": "1day",
        }[timeframe]

        td, td_err = td_get(
            "time_series",
            {
                "symbol": SYMBOL,
                "interval": td_interval,
                "outputsize": 300
            }
        )

        if td and td.get("values"):
            rows = []
            for v in reversed(td["values"]):
                try:
                    rows.append({
                        "datetime": v.get("datetime"),
                        "open": float(v["open"]),
                        "high": float(v["high"]),
                        "low": float(v["low"]),
                        "close": float(v["close"]),
                        "volume": float(v.get("volume") or 0)
                    })
                except (KeyError, TypeError, ValueError):
                    pass
            err = None

    result = {
        "symbol": SYMBOL,
        "timeframe": timeframe,
        "candles": rows,
        "source": "Yahoo Finance GC=F backup" if rows and err else "Twelve Data",
        "status": "ok" if rows else "error",
        "error": None if rows else err,
        "updated": now_iso()
    }

    with lock:
        candle_cache[timeframe] = {"time": time.time(), "data": result}

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


def build_signal(candle_data):
    candles = candle_data.get("candles") or []
    if len(candles) < 50:
        return {
            "call": "WAIT",
            "buy_probability": 0.0,
            "sell_probability": 0.0,
            "confidence": 0.0,
            "reason": "Not enough candle data",
            "technical": {}
        }

    closes = [x["close"] for x in candles]
    last = closes[-1]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    rv = rsi(closes, 14)
    ml, ms = macd(closes)
    av = atr(candles, 14)

    score = 0.0
    reasons = []

    if e20 is not None and e50 is not None:
        if e20 > e50:
            score += 1.0
            reasons.append("EMA20 > EMA50")
        else:
            score -= 1.0
            reasons.append("EMA20 < EMA50")

    if e200 is not None:
        if last > e200:
            score += 1.0
            reasons.append("Price above EMA200")
        else:
            score -= 1.0
            reasons.append("Price below EMA200")

    if rv is not None:
        if rv > 55:
            score += 0.7
            reasons.append("RSI bullish")
        elif rv < 45:
            score -= 0.7
            reasons.append("RSI bearish")

    if ml is not None and ms is not None:
        if ml > ms:
            score += 0.8
            reasons.append("MACD bullish")
        else:
            score -= 0.8
            reasons.append("MACD bearish")

    max_score = 3.5
    buy = max(0.0, min(100.0, 50 + (score / max_score) * 50))
    sell = 100 - buy
    confidence = min(99.0, abs(buy - sell) + 35)

    if buy >= 62:
        call = "BUY"
    elif sell >= 62:
        call = "SELL"
    else:
        call = "WAIT"

    return {
        "call": call,
        "buy_probability": round(buy, 1),
        "sell_probability": round(sell, 1),
        "confidence": round(confidence, 1),
        "reason": ", ".join(reasons),
        "technical": {
            "EMA20": round(e20, 3) if e20 is not None else None,
            "EMA50": round(e50, 3) if e50 is not None else None,
            "EMA200": round(e200, 3) if e200 is not None else None,
            "RSI": round(rv, 2) if rv is not None else None,
            "MACD": round(ml, 4) if ml is not None else None,
            "ATR": round(av, 4) if av is not None else None,
        }
    }


def get_news():
    with lock:
        if cache_fresh(news_cache, NEWS_CACHE_SECONDS):
            return news_cache["data"]

    if not NEWS_API_KEY:
        return {
            "status": "unavailable",
            "articles": [],
            "message": "NEWS_API_KEY not configured"
        }

    try:
        query = (
            '"gold price" OR gold OR XAU OR bullion OR "gold futures" '
            'OR "precious metals" OR "Federal Reserve" OR Fed '
            'OR "interest rate" OR inflation OR CPI OR "US dollar" '
            'OR USD OR "Treasury yields" OR "central bank" '
            'OR RBI OR ECB OR BOJ OR tariff OR sanctions '
            'OR "safe haven" OR geopolitics'
        )

        r = session.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 50,
                "apiKey": NEWS_API_KEY
            },
            timeout=10
        )
        payload = r.json()

        if r.status_code >= 400 or payload.get("status") != "ok":
            data = {
                "status": "error",
                "articles": [],
                "message": payload.get("message", f"HTTP_{r.status_code}")
            }
        else:
            gold_terms = [
                "gold", "xau", "bullion", "precious metal",
                "gold price", "gold futures"
            ]

            macro_terms = [
                "federal reserve", "fed", "interest rate",
                "inflation", "cpi", "us dollar", "usd",
                "treasury yield", "bond yield", "central bank",
                "rbi", "ecb", "boj", "tariff", "sanction",
                "safe haven", "geopolit", "war", "oil", "crude"
            ]

            blocked_terms = [
                "crab", "goldfish", "tap water", "water conditioner",
                "appliance", "mental health", "nutrition",
                "social media star", "celebrity", "medical bills",
                "farming", "recipe", "football", "cricket",
                "movie", "music", "entertainment"
            ]

            scored = []
            seen = set()

            for article in payload.get("articles", []):
                title = (article.get("title") or "").strip()
                description = (article.get("description") or "").strip()
                text_blob = f"{title} {description}".lower()

                if not title:
                    continue

                if any(term in text_blob for term in blocked_terms):
                    continue

                title_lower = title.lower()

                gold_score = sum(
                    3 if term in title_lower else 1
                    for term in gold_terms
                    if term in text_blob
                )

                macro_score = sum(
                    2 if term in title_lower else 1
                    for term in macro_terms
                    if term in text_blob
                )

                if gold_score == 0 and macro_score < 2:
                    continue

                url = article.get("url") or ""
                key = url or title_lower

                if key in seen:
                    continue
                seen.add(key)

                score = gold_score + macro_score

                if any(term in title_lower for term in ["gold", "xau", "bullion"]):
                    score += 4

                if any(term in title_lower for term in [
                    "fed", "federal reserve", "interest rate",
                    "inflation", "cpi", "dollar", "treasury yield"
                ]):
                    score += 2

                scored.append((
                    score,
                    {
                        "title": title,
                        "url": url,
                        "source": (article.get("source") or {}).get("name"),
                        "publishedAt": article.get("publishedAt")
                    }
                ))

            scored.sort(key=lambda item: item[0], reverse=True)

            data = {
                "status": "ok",
                "articles": [item[1] for item in scored[:10]]
            }

        with lock:
            news_cache["time"] = time.time()
            news_cache["data"] = data

        return data

    except Exception as e:
        return {
            "status": "error",
            "articles": [],
            "message": type(e).__name__
        }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Gold AI Live v6",
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
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gold AI Live v6</title>
<style>
body{margin:0;background:#0b1020;color:#f3f5f7;font-family:Arial,sans-serif}
.wrap{max-width:1200px;margin:auto;padding:20px}
h1{margin:0 0 6px;font-size:28px}
.sub{color:#aab3c2;margin-bottom:20px}
.tabs{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;margin-bottom:18px}
button{background:#1b2540;color:white;border:0;border-radius:10px;padding:13px 6px;font-weight:700}
button.active{background:#2477ff}
.card{background:#111a30;border:1px solid #273454;border-radius:16px;padding:18px;margin-bottom:14px}
.price{font-size:34px;font-weight:800;margin-top:8px}
.source{font-size:12px;color:#8fa1bd;margin-top:7px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.call{font-size:34px;font-weight:900;margin:10px 0}
.wait{color:#ffd36b}.buy{color:#45e38c}.sell{color:#ff6b78}
.row{display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid #26324e}
.small{color:#aab3c2;font-size:13px}
.news a{color:#8db8ff;text-decoration:none}
@media(max-width:700px){.tabs{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1fr}.price{font-size:30px}}
</style>
</head>
<body>
<div class="wrap">
<h1>🥇 Gold AI Live v6</h1>
<div class="sub">XAU/USD • Live Price • AI Technical Signal • Global News</div>

<div class="tabs">
<button data-t="1m">1m</button><button data-t="5m">5m</button>
<button data-t="15m">15m</button><button data-t="30m">30m</button>
<button data-t="1h">1H</button><button data-t="4h">4H</button>
<button data-t="1d">1D</button>
</div>

<div class="card">
<div>XAU/USD <span id="status">LIVE</span></div>
<div class="price" id="price">Loading...</div>
<div class="source" id="source">Connecting to market data...</div>
</div>

<div class="grid">
<div class="card">
<div class="small">AI CALL</div>
<div id="call" class="call wait">WAIT</div>
<div class="row"><span>BUY probability</span><b id="buy">0.0%</b></div>
<div class="row"><span>SELL probability</span><b id="sell">0.0%</b></div>
<div class="row"><span>Confidence</span><b id="confidence">0.0%</b></div>
<div class="small" id="reason" style="margin-top:12px">Waiting for market data...</div>
</div>

<div class="card">
<div class="small">TECHNICAL DATA</div>
<div class="row"><span>EMA20</span><b id="ema20">-</b></div>
<div class="row"><span>EMA50</span><b id="ema50">-</b></div>
<div class="row"><span>EMA200</span><b id="ema200">-</b></div>
<div class="row"><span>RSI</span><b id="rsi">-</b></div>
<div class="row"><span>MACD</span><b id="macd">-</b></div>
<div class="row"><span>ATR</span><b id="atr">-</b></div>
</div>
</div>

<div class="card news">
<div class="small">GLOBAL GOLD NEWS</div>
<div id="news">Loading news...</div>
</div>
</div>

<script>
let timeframe="1h";

function setActive(){
 document.querySelectorAll("button[data-t]").forEach(b=>{
   b.classList.toggle("active",b.dataset.t===timeframe);
 });
}
document.querySelectorAll("button[data-t]").forEach(b=>{
 b.onclick=()=>{timeframe=b.dataset.t;setActive();loadAll()};
});
setActive();

function put(id,v){document.getElementById(id).textContent=(v===null||v===undefined)?"-":v}

async function loadPrice(){
 try{
   const r=await fetch("/api/live-price?x="+Date.now());
   const d=await r.json();
   if(d.price!==null && d.price!==undefined){
     document.getElementById("price").textContent=Number(d.price).toFixed(2);
     document.getElementById("source").textContent="Source: "+(d.source||"market data");
     document.getElementById("status").textContent=d.status==="backup"?"BACKUP":"LIVE";
   }else{
     document.getElementById("price").textContent="Waiting for data";
     document.getElementById("source").textContent="Market provider rate-limited/unavailable";
   }
 }catch(e){document.getElementById("price").textContent="Connection error"}
}

async function loadSignal(){
 try{
   const r=await fetch("/api/live-signal?timeframe="+encodeURIComponent(timeframe)+"&x="+Date.now());
   const d=await r.json();
   const c=document.getElementById("call");
   c.textContent=d.call||"WAIT";
   c.className="call "+((d.call==="BUY")?"buy":(d.call==="SELL")?"sell":"wait");
   put("buy",d.buy_probability!=null?d.buy_probability+"%":"0.0%");
   put("sell",d.sell_probability!=null?d.sell_probability+"%":"0.0%");
   put("confidence",d.confidence!=null?d.confidence+"%":"0.0%");
   put("reason",d.reason||"Waiting for enough candle data");
   const t=d.technical||{};
   put("ema20",t.EMA20);put("ema50",t.EMA50);put("ema200",t.EMA200);
   put("rsi",t.RSI);put("macd",t.MACD);put("atr",t.ATR);
 }catch(e){
   document.getElementById("reason").textContent="Signal temporarily unavailable";
 }
}

async function loadNews(){
 try{
   const r=await fetch("/api/news?x="+Date.now());
   const d=await r.json();
   const el=document.getElementById("news");
   if(!d.articles || !d.articles.length){el.textContent=d.message||"News unavailable";return}
   el.innerHTML=d.articles.map(a=>`<div style="padding:8px 0"><a target="_blank" href="${a.url||"#"}">${a.title||"Gold news"}</a><div class="small">${a.source||""}</div></div>`).join("");
 }catch(e){document.getElementById("news").textContent="News unavailable"}
}

function loadAll(){loadPrice();loadSignal()}
loadAll();loadNews();
setInterval(loadPrice,15000);
setInterval(loadSignal,60000);
setInterval(loadNews,300000);
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
    print(" GOLD AI LIVE v6 STARTED")
    print(" Twelve Data primary")
    print(" Yahoo GC=F backup")
    print(" Price cache: 15 sec")
    print(" Candle cache: 60 sec")
    print(" News cache: 5 min")
    print("====================================")
