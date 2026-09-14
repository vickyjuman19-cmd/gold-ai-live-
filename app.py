
import os, time, requests, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from fastapi import FastAPI, Query
from dotenv import load_dotenv

load_dotenv()
app = FastAPI(title="Gold AI Live News PWA")
PRICE_API_KEY = os.getenv("PRICE_API_KEY", "")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

TIMEFRAMES = ["1m","5m","15m","30m","1H","4H","1D"]

# GDELT is used as a no-key news fallback. For production, a paid/licensed
# market-news provider is recommended for lower latency and stronger source control.
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

def get_price():
    # Production: connect this to the exact XAUUSD feed used by the user's broker.
    # Optional Twelve Data adapter:
    if PRICE_API_KEY:
        try:
            r = requests.get(
                "https://api.twelvedata.com/quote",
                params={"symbol":"XAU/USD","apikey":PRICE_API_KEY},
                timeout=5
            )
            d = r.json()
            if d.get("close"):
                p=float(d["close"])
                return {"price":p, "bid":float(d.get("bid") or p),
                        "ask":float(d.get("ask") or p), "source":"Twelve Data"}
        except Exception:
            pass
    return {"price":4348.75,"bid":4348.75,"ask":4349.72,
            "source":"Demo/XM360 screenshot fallback"}

def gdelt_news():
    q='(gold OR XAUUSD OR bullion) AND (Fed OR inflation OR CPI OR PPI OR yields OR dollar OR "Treasury" OR geopolitics)'
    try:
        r=requests.get(GDELT, params={
            "query":q, "mode":"artlist", "maxrecords":25,
            "format":"json", "sort":"datedesc"
        }, timeout=8)
        arts=r.json().get("articles",[])
        out=[]
        for a in arts[:20]:
            out.append({
                "title":a.get("title",""),
                "url":a.get("url",""),
                "source":a.get("domain",""),
                "published":a.get("seendate","")
            })
        return out
    except Exception:
        return []

def score_news(items):
    # Transparent rule layer; not a claim of prediction certainty.
    bullish_words=["rate cut","dovish","lower yields","weaker dollar","safe haven","war","conflict","geopolitical","central bank buying"]
    bearish_words=["rate hike","hawkish","higher yields","strong dollar","hot inflation","cpi above","ppi above","fed tightening"]
    bull=bear=0
    for x in items:
        t=x["title"].lower()
        bull += sum(w in t for w in bullish_words)
        bear += sum(w in t for w in bearish_words)
    total=bull+bear
    if total==0: return {"score":50,"bias":"NEUTRAL","bullish":0,"bearish":0}
    score=max(0,min(100,50+45*(bull-bear)/max(1,total)))
    return {"score":round(score,1),
            "bias":"BULLISH" if score>=60 else "BEARISH" if score<=40 else "NEUTRAL",
            "bullish":bull,"bearish":bear}

def signal(price, news):
    # Starter scoring engine. Real production model should use OHLC data,
    # DXY/yields/calendar and walk-forward validated probabilities.
    ns=news["score"]
    trend=52 + (3 if price>=4350 else -3)
    buy=0.45*trend+0.35*ns+0.20*50
    sell=100-buy
    conf=max(buy,sell)
    return {
        "buy":round(buy,1),"sell":round(sell,1),
        "confidence":round(conf,1),
        "signal":"NO TRADE" if conf<70 else ("BUY" if buy>sell else "SELL"),
        "reason":"News and technical factors are not sufficiently aligned." if conf<70
                 else ("News/technical bias supports BUY." if buy>sell else "News/technical bias supports SELL.")
    }

@app.get("/api/dashboard")
def dashboard(timeframe: str=Query("1H")):
    if timeframe not in TIMEFRAMES: timeframe="1H"
    m=get_price()
    n=gdelt_news()
    ns=score_news(n)
    return {
        "server_time":datetime.now(timezone.utc).isoformat(),
        "timeframe":timeframe,
        "market":m,
        "news_bias":ns,
        "signal":signal(m["price"],ns),
        "news":n
    }


from fastapi.responses import HTMLResponse, Response

@app.get("/", response_class=HTMLResponse)
def home():
    return HTML_PAGE

@app.get("/manifest.json")
def manifest():
    return Response(MANIFEST, media_type="application/manifest+json")

@app.get("/sw.js")
def service_worker():
    return Response(SW_JS, media_type="application/javascript")

HTML_PAGE = '\n<!doctype html><html lang="en"><head>\n<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<meta name="theme-color" content="#08111f"><link rel="manifest" href="/manifest.json">\n<title>Gold AI Live</title>\n<style>\nbody{margin:0;background:#08111f;color:#f2f6ff;font-family:system-ui,-apple-system,Segoe UI,sans-serif}\nmain{max-width:950px;margin:auto;padding:16px}.top{display:flex;justify-content:space-between;gap:10px;align-items:center}\nh1{margin:5px 0;font-size:25px}.muted{color:#91a0b7;font-size:13px}.price{font-size:42px;font-weight:850;margin-top:10px}\n.card{background:#101b2d;border:1px solid #243552;border-radius:18px;padding:16px;margin-top:12px}\n.tf{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-top:14px}\nbutton{background:#17243a;border:1px solid #30435f;color:#eaf0fb;border-radius:10px;padding:10px 4px;font-weight:750}\nbutton.active{background:#2187ff;border-color:#2187ff}.signal{font-size:34px;font-weight:900}\n.buy{color:#4ee0ad}.sell{color:#ff7188}.wait{color:#ffd166}\n.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1f2b40}\n.news{padding:10px 0;border-bottom:1px solid #202e44}.news a{color:#dbe8ff;text-decoration:none}\n.badge{display:inline-block;border-radius:20px;padding:5px 9px;font-size:12px;font-weight:800;background:#26354d}\n@media(max-width:650px){.tf{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1fr}.price{font-size:34px}}\n</style></head><body><main>\n<div class="top"><div><h1>🥇 Gold AI Live</h1><div class="muted">XAUUSD • Live news + multi-timeframe signal</div></div>\n<button onclick="load()">Refresh</button></div>\n<div class="tf" id="tf"></div>\n<div class="card"><div class="muted">XAUUSD</div><div class="price" id="price">—</div><div class="muted" id="src">—</div></div>\n<div class="grid">\n<div class="card"><div class="muted">AI CALL • <span id="tfl">1H</span></div><div id="sig" class="signal wait">NO TRADE</div>\n<div class="row"><span>BUY probability</span><b id="buy">—</b></div>\n<div class="row"><span>SELL probability</span><b id="sell">—</b></div>\n<div class="row"><span>Confidence</span><b id="conf">—</b></div><p class="muted" id="reason">—</p></div>\n<div class="card"><div class="muted">GLOBAL GOLD NEWS</div><p><span class="badge" id="nb">NEUTRAL</span> <span class="muted" id="ns">Score 50</span></p>\n<div id="news">Loading news…</div></div></div>\n<div class="card"><b>⚠️ Important</b><p class="muted">This app provides a model signal, not guaranteed future direction. It must never claim 95%/100% certainty. When factors conflict it intentionally returns NO TRADE.</p></div>\n<div class="muted" id="updated">—</div>\n</main>\n<script>\nconst T=[\'1m\',\'5m\',\'15m\',\'30m\',\'1H\',\'4H\',\'1D\'];let tf=\'1H\';\nconst box=document.getElementById(\'tf\');\nT.forEach(x=>{let b=document.createElement(\'button\');b.textContent=x;b.onclick=()=>{tf=x;paint();load()};b.id=\'x\'+x;box.appendChild(b)});\nfunction paint(){T.forEach(x=>document.getElementById(\'x\'+x).classList.toggle(\'active\',x===tf))}\nasync function load(){\n try{let d=await (await fetch(\'/api/dashboard?timeframe=\'+tf)).json(),s=d.signal,n=d.news_bias;\n document.getElementById(\'price\').textContent=Number(d.market.price).toFixed(2);\n document.getElementById(\'src\').textContent=d.market.source+\' • \'+tf;\n document.getElementById(\'tfl\').textContent=tf;\n let el=document.getElementById(\'sig\');el.textContent=s.signal;el.className=\'signal \'+(s.signal===\'BUY\'?\'buy\':s.signal===\'SELL\'?\'sell\':\'wait\');\n document.getElementById(\'buy\').textContent=s.buy+\'%\';document.getElementById(\'sell\').textContent=s.sell+\'%\';document.getElementById(\'conf\').textContent=s.confidence+\'%\';document.getElementById(\'reason\').textContent=s.reason;\n document.getElementById(\'nb\').textContent=n.bias;document.getElementById(\'ns\').textContent=\'Score \'+n.score;\n document.getElementById(\'news\').innerHTML=d.news.map(a=>`<div class="news"><a href="${a.url}" target="_blank" rel="noopener">${a.title}</a><div class="muted">${a.source}</div></div>`).join(\'\')||\'<span class="muted">No articles returned.</span>\';\n document.getElementById(\'updated\').textContent=\'Updated \'+new Date(d.server_time).toLocaleString();\n }catch(e){document.getElementById(\'updated\').textContent=\'Error: \'+e}}\npaint();load();setInterval(load,15000);\nif(\'serviceWorker\' in navigator){navigator.serviceWorker.register(\'/sw.js\').catch(()=>{})}\n</script></body></html>\n'
MANIFEST = '{\n"name":"Gold AI Live","short_name":"GoldAI","start_url":"/","display":"standalone",\n"background_color":"#08111f","theme_color":"#08111f","description":"Gold news and multi-timeframe signal dashboard"\n}'
SW_JS = '\nconst CACHE="gold-ai-v1";\nself.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(["/","/manifest.json"]))));\nself.addEventListener("fetch",e=>e.respondWith(fetch(e.request).catch(()=>caches.match(e.request))));\n'
