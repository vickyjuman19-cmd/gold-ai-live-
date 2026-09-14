import os
import requests
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Gold AI Live")
PRICE_API_KEY = os.getenv("PRICE_API_KEY", "")
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1H", "4H", "1D"]

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


def get_price():
    # Twelve Data only. No fake/hardcoded price fallback.
    if not PRICE_API_KEY:
        return {
            "price": None, "bid": None, "ask": None,
            "source": "Twelve Data API key missing"
        }

    try:
        r = requests.get(
            "https://api.twelvedata.com/quote",
            params={"symbol": "XAU/USD", "apikey": PRICE_API_KEY},
            timeout=8,
        )
        r.raise_for_status()
        d = r.json()

        if d.get("status") == "error":
            raise RuntimeError(d.get("message", "Twelve Data error"))

        raw_price = d.get("close") or d.get("price")
        if raw_price is None:
            raise RuntimeError("Twelve Data returned no price")

        price = float(raw_price)
        bid = float(d.get("bid") or price)
        ask = float(d.get("ask") or price)

        return {
            "price": price,
            "bid": bid,
            "ask": ask,
            "source": "Twelve Data • XAU/USD",
        }

    except Exception as e:
        print(f"Twelve Data price error: {type(e).__name__}: {e}")
        return {
            "price": None, "bid": None, "ask": None,
            "source": "Twelve Data unavailable"
        }


def gdelt_news():
    q = '(gold OR XAUUSD OR bullion) AND (Fed OR inflation OR CPI OR PPI OR yields OR dollar OR "Treasury" OR geopolitics)'
    try:
        r = requests.get(
            GDELT,
            params={
                "query": q,
                "mode": "artlist",
                "maxrecords": 25,
                "format": "json",
                "sort": "datedesc",
            },
            timeout=8,
        )
        r.raise_for_status()
        arts = r.json().get("articles", [])
        return [{
            "title": a.get("title", ""),
            "url": a.get("url", ""),
            "source": a.get("domain", ""),
            "published": a.get("seendate", ""),
        } for a in arts[:20]]
    except Exception as e:
        print(f"News error: {type(e).__name__}: {e}")
        return []


def score_news(items):
    bullish_words = [
        "rate cut", "dovish", "lower yields", "weaker dollar",
        "safe haven", "war", "conflict", "geopolitical",
        "central bank buying"
    ]
    bearish_words = [
        "rate hike", "hawkish", "higher yields", "strong dollar",
        "hot inflation", "cpi above", "ppi above", "fed tightening"
    ]

    bull = bear = 0
    for item in items:
        title = item.get("title", "").lower()
        bull += sum(word in title for word in bullish_words)
        bear += sum(word in title for word in bearish_words)

    total = bull + bear
    if total == 0:
        return {"score": 50, "bias": "NEUTRAL", "bullish": 0, "bearish": 0}

    score = max(0, min(100, 50 + 45 * (bull - bear) / max(1, total)))
    return {
        "score": round(score, 1),
        "bias": "BULLISH" if score >= 60 else "BEARISH" if score <= 40 else "NEUTRAL",
        "bullish": bull,
        "bearish": bear,
    }


def signal(price, news):
    if price is None:
        return {
            "buy": 0,
            "sell": 0,
            "confidence": 0,
            "signal": "NO TRADE",
            "reason": "Live Twelve Data price is unavailable. No trade signal is generated."
        }

    ns = news["score"]
    trend = 52
    buy = 0.45 * trend + 0.35 * ns + 0.20 * 50
    sell = 100 - buy
    conf = max(buy, sell)

    return {
        "buy": round(buy, 1),
        "sell": round(sell, 1),
        "confidence": round(conf, 1),
        "signal": "NO TRADE" if conf < 70 else "BUY" if buy > sell else "SELL",
        "reason": (
            "News and technical factors are not sufficiently aligned."
            if conf < 70
            else "News/technical bias supports BUY."
            if buy > sell
            else "News/technical bias supports SELL."
        ),
    }


@app.get("/api/dashboard")
def dashboard(timeframe: str = Query("1H")):
    if timeframe not in TIMEFRAMES:
        timeframe = "1H"

    market = get_price()
    news = gdelt_news()
    news_score = score_news(news)

    return {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "timeframe": timeframe,
        "market": market,
        "news_bias": news_score,
        "signal": signal(market["price"], news_score),
        "news": news,
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML_PAGE


@app.get("/manifest.json")
def manifest():
    return Response(MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return Response(SW_JS, media_type="application/javascript")


HTML_PAGE = r'''
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#08111f">
<link rel="manifest" href="/manifest.json">
<title>Gold AI Live</title>
<style>
body{margin:0;background:#08111f;color:#f2f6ff;font-family:system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:950px;margin:auto;padding:16px}
.top{display:flex;justify-content:space-between;gap:10px;align-items:center}
h1{margin:5px 0;font-size:25px}.muted{color:#91a0b7;font-size:13px}
.price{font-size:42px;font-weight:850;margin-top:10px}
.card{background:#101b2d;border:1px solid #243552;border-radius:18px;padding:16px;margin-top:12px}
.tf{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-top:14px}
button{background:#17243a;border:1px solid #30435f;color:#eaf0fb;border-radius:10px;padding:10px 4px;font-weight:750}
button.active{background:#2187ff;border-color:#2187ff}
.signal{font-size:34px;font-weight:900}.buy{color:#4ee0ad}.sell{color:#ff7188}.wait{color:#ffd166}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1f2b40}
.news{padding:10px 0;border-bottom:1px solid #202e44}
.news a{color:#dbe8ff;text-decoration:none}
.badge{display:inline-block;border-radius:20px;padding:5px 9px;font-size:12px;font-weight:800;background:#26354d}
@media(max-width:650px){.tf{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1fr}.price{font-size:34px}}
</style>
</head>
<body>
<main>
<div class="top">
<div><h1>🥇 Gold AI Live</h1><div class="muted">XAUUSD • Twelve Data live price + multi-timeframe signal</div></div>
<button onclick="load()">Refresh</button>
</div>

<div class="tf" id="tf"></div>

<div class="card">
<div class="muted">XAU/USD LIVE PRICE</div>
<div class="price" id="price">—</div>
<div class="muted" id="src">Connecting to Twelve Data…</div>
</div>

<div class="grid">
<div class="card">
<div class="muted">AI CALL • <span id="tfl">1H</span></div>
<div id="sig" class="signal wait">NO TRADE</div>
<div class="row"><span>BUY probability</span><b id="buy">—</b></div>
<div class="row"><span>SELL probability</span><b id="sell">—</b></div>
<div class="row"><span>Confidence</span><b id="conf">—</b></div>
<p class="muted" id="reason">—</p>
</div>

<div class="card">
<div class="muted">GLOBAL GOLD NEWS</div>
<p><span class="badge" id="nb">NEUTRAL</span> <span class="muted" id="ns">Score 50</span></p>
<div id="news">Loading news…</div>
</div>
</div>

<div class="card">
<b>⚠️ Live Feed Protection</b>
<p class="muted">This dashboard does not use a fake/hardcoded gold price. If Twelve Data is unavailable, the price is shown as unavailable and the app returns NO TRADE.</p>
</div>

<div class="card">
<b>⚠️ Important</b>
<p class="muted">This app provides a model signal, not guaranteed future direction. It must never claim 95%/100% certainty. When factors conflict it intentionally returns NO TRADE.</p>
</div>

<div class="muted" id="updated">—</div>
</main>

<script>
const T=['1m','5m','15m','30m','1H','4H','1D'];let tf='1H';
const box=document.getElementById('tf');

T.forEach(x=>{
let b=document.createElement('button');b.textContent=x;
b.onclick=()=>{tf=x;paint();load()};b.id='x'+x;box.appendChild(b);
});

function paint(){T.forEach(x=>document.getElementById('x'+x).classList.toggle('active',x===tf))}

async function load(){
try{
const response=await fetch('/api/dashboard?timeframe='+encodeURIComponent(tf));
const d=await response.json(),s=d.signal,n=d.news_bias,price=d.market.price;

if(price===null||price===undefined){
document.getElementById('price').textContent='—';
document.getElementById('src').textContent=d.market.source;
}else{
document.getElementById('price').textContent=Number(price).toFixed(2);
document.getElementById('src').textContent=d.market.source+' • '+tf;
}

document.getElementById('tfl').textContent=tf;
let el=document.getElementById('sig');
el.textContent=s.signal;
el.className='signal '+(s.signal==='BUY'?'buy':s.signal==='SELL'?'sell':'wait');

document.getElementById('buy').textContent=s.buy+'%';
document.getElementById('sell').textContent=s.sell+'%';
document.getElementById('conf').textContent=s.confidence+'%';
document.getElementById('reason').textContent=s.reason;
document.getElementById('nb').textContent=n.bias;
document.getElementById('ns').textContent='Score '+n.score;

const newsBox=document.getElementById('news');
if(d.news&&d.news.length){
newsBox.innerHTML=d.news.map(a=>{
const title=(a.title||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const source=(a.source||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
return `<div class="news"><a href="${a.url||'#'}" target="_blank" rel="noopener">${title}</a><div class="muted">${source}</div></div>`;
}).join('');
}else{
newsBox.innerHTML='<span class="muted">No articles returned.</span>';
}

document.getElementById('updated').textContent='Updated '+new Date(d.server_time).toLocaleString();
}catch(e){
document.getElementById('updated').textContent='Connection error: '+e;
document.getElementById('src').textContent='Dashboard/API error';
}
}

paint();load();setInterval(load,15000);
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(()=>{})}
</script>
</body>
</html>
'''

MANIFEST = r'''{
"name":"Gold AI Live",
"short_name":"GoldAI",
"start_url":"/",
"display":"standalone",
"background_color":"#08111f",
"theme_color":"#08111f",
"description":"Gold AI live XAU/USD dashboard"
}'''

SW_JS = r'''
const CACHE="gold-ai-v2";
self.addEventListener("install",e=>{
e.waitUntil(caches.open(CACHE).then(c=>c.addAll(["/","/manifest.json"])));
});
self.addEventListener("fetch",e=>{
e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});
'''
