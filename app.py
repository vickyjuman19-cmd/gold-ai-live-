import os, requests
from datetime import datetime, timezone
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Gold AI Live News PWA")

PRICE_API_KEY = os.getenv("PRICE_API_KEY", "")

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1H", "4H", "1D"]

TD_INTERVALS = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1H": "1h",
    "4H": "4h",
    "1D": "1day"
}

TD = "https://api.twelvedata.com"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


def td_get(path, params):
    if not PRICE_API_KEY:
        raise RuntimeError("PRICE_API_KEY is missing")

    p = dict(params)
    p["apikey"] = PRICE_API_KEY

    r = requests.get(
        TD + path,
        params=p,
        timeout=10
    )

    r.raise_for_status()

    d = r.json()

    if d.get("status") == "error" or d.get("code"):
        raise RuntimeError(
            d.get("message", "Twelve Data error")
        )

    return d


def get_price():
    d = td_get(
        "/quote",
        {"symbol": "XAU/USD"}
    )

    p = d.get("close") or d.get("price")

    if p is None:
        raise RuntimeError("No current XAU/USD price")

    p = float(p)

    return {
        "price": p,
        "bid": float(d.get("bid") or p),
        "ask": float(d.get("ask") or p),
        "source": "Twelve Data LIVE",
        "datetime": d.get("datetime", "")
    }


def get_candles(tf, outputsize=250):

    d = td_get(
        "/time_series",
        {
            "symbol": "XAU/USD",
            "interval": TD_INTERVALS[tf],
            "outputsize": outputsize,
            "order": "ASC"
        }
    )

    vals = d.get("values") or []

    rows = []

    for x in vals:

        try:

            rows.append({
                "datetime": x.get("datetime", ""),
                "open": float(x["open"]),
                "high": float(x["high"]),
                "low": float(x["low"]),
                "close": float(x["close"])
            })

        except (KeyError, TypeError, ValueError):
            pass

    if len(rows) < 60:
        raise RuntimeError(
            f"Not enough candles for {tf}: {len(rows)}"
        )

    return rows


def ema(v, n):

    k = 2 / (n + 1)

    out = [v[0]]

    for x in v[1:]:
        out.append(
            x * k + out[-1] * (1 - k)
        )

    return out


def rsi(v, n=14):

    if len(v) < n + 1:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(v)):

        ch = v[i] - v[i - 1]

        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))

    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n

    for i in range(n, len(gains)):

        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n

    if al == 0:
        return 100.0 if ag > 0 else 50.0

    rs = ag / al

    return 100 - (100 / (1 + rs))


def atr(rows, n=14):

    tr = []

    for i, x in enumerate(rows):

        if i == 0:

            t = x["high"] - x["low"]

        else:

            pc = rows[i - 1]["close"]

            t = max(
                x["high"] - x["low"],
                abs(x["high"] - pc),
                abs(x["low"] - pc)
            )

        tr.append(t)

    if len(tr) < n:
        return 0.01

    a = sum(tr[:n]) / n

    for x in tr[n:]:
        a = (a * (n - 1) + x) / n

    return max(a, 0.01)


def macd(v):

    e12 = ema(v, 12)
    e26 = ema(v, 26)

    line = [
        a - b
        for a, b in zip(e12, e26)
    ]

    sig = ema(line, 9)

    return (
        line[-1],
        sig[-1],
        line[-1] - sig[-1]
    )


def technical(rows):

    c = [x["close"] for x in rows]

    p = c[-1]

    e20 = ema(c, 20)[-1]
    e50 = ema(c, 50)[-1]

    if len(c) >= 200:
        e200 = ema(c, 200)[-1]
    else:
        e200 = ema(
            c,
            min(100, len(c))
        )[-1]

    r = rsi(c)

    ml, ms, mh = macd(c)

    a = atr(rows)

    score = 0.0

    # Price vs EMA20
    score += 1 if p > e20 else -1

    # EMA20 vs EMA50
    score += 1 if e20 > e50 else -1

    # Price vs EMA200
    score += 1 if p > e200 else -1

    # RSI
    if 52 <= r <= 68:
        score += 1

    elif 32 <= r <= 48:
        score -= 1

    elif r > 72:
        score -= 0.5

    elif r < 28:
        score += 0.5

    # MACD
    score += 1 if mh > 0 else -1

    # Short momentum
    if len(c) >= 4:

        if c[-1] > c[-4]:
            score += 0.5

        elif c[-1] < c[-4]:
            score -= 0.5

    return {
        "price": p,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "rsi": r,
        "macd": ml,
        "macd_signal": ms,
        "macd_hist": mh,
        "atr": a,
        "norm": max(
            -1,
            min(1, score / 5.5)
        )
    }


def gdelt_news():

    q = (
        '(gold OR XAUUSD OR bullion) AND '
        '(Fed OR inflation OR CPI OR PPI OR yields '
        'OR dollar OR "Treasury" OR geopolitics '
        'OR tariff)'
    )

    try:

        r = requests.get(
            GDELT,
            params={
                "query": q,
                "mode": "artlist",
                "maxrecords": 20,
                "format": "json",
                "sort": "datedesc"
            },
            timeout=8
        )

        data = r.json()

        return [
            {
                "title": a.get("title", ""),
                "url": a.get("url", ""),
                "source": a.get("domain", ""),
                "published": a.get("seendate", "")
            }
            for a in data.get("articles", [])[:20]
        ]

    except Exception:
        return []


def news_score(items):

    bull = [
        "rate cut",
        "dovish",
        "lower yields",
        "weaker dollar",
        "safe haven",
        "war",
        "conflict",
        "geopolitical",
        "central bank buying",
        "tariff",
        "trade war"
    ]

    bear = [
        "rate hike",
        "hawkish",
        "higher yields",
        "strong dollar",
        "hot inflation",
        "cpi above",
        "ppi above",
        "fed tightening"
    ]

    b = sum(
        sum(
            w in x["title"].lower()
            for w in bull
        )
        for x in items
    )

    s = sum(
        sum(
            w in x["title"].lower()
            for w in bear
        )
        for x in items
    )

    total = b + s

    if not total:

        return {
            "score": 50,
            "bias": "NEUTRAL",
            "bullish": 0,
            "bearish": 0
        }

    score = max(
        0,
        min(
            100,
            50 + 45 * (b - s) / max(1, total)
        )
    )

    return {
        "score": round(score, 1),
        "bias": (
            "BULLISH"
            if score >= 60
            else "BEARISH"
            if score <= 40
            else "NEUTRAL"
        ),
        "bullish": b,
        "bearish": s
    }


def build_signal(t, n):

    combined = (
        0.75 * t["norm"]
        +
        0.25 * ((n["score"] - 50) / 50)
    )

    buy = 50 + combined * 38
    sell = 100 - buy

    if abs(combined) < 0.22:
        action = "NO TRADE"

    elif combined > 0:
        action = "BUY"

    else:
        action = "SELL"

    confidence = min(
        90,
        max(
            50,
            50 + abs(combined) * 40
        )
    )

    entry = t["price"]

    risk = t["atr"] * 1.5

    if action == "BUY":

        sl = entry - risk
        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.5

    elif action == "SELL":

        sl = entry + risk
        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.5

    else:

        sl = None
        tp1 = None
        tp2 = None

    return {

        "buy": round(buy, 1),

        "sell": round(sell, 1),

        "confidence": round(
            confidence,
            1
        ),

        "signal": action,

        "entry": round(
            entry,
            2
        ),

        "stop_loss": (
            round(sl, 2)
            if sl is not None
            else None
        ),

        "tp1": (
            round(tp1, 2)
            if tp1 is not None
            else None
        ),

        "tp2": (
            round(tp2, 2)
            if tp2 is not None
            else None
        ),

        "risk_reward": (
            "1:1.5 / 1:2.5"
            if action != "NO TRADE"
            else "—"
        ),

        "reason": (
            "Technical + news confluence supports "
            + action
            + "."
            if action != "NO TRADE"
            else
            "Technical and news factors are not sufficiently aligned."
        )
    }


@app.get("/api/dashboard")
def dashboard(
    timeframe: str = Query("1H")
):

    if timeframe not in TIMEFRAMES:
        timeframe = "1H"

    market = get_price()

    rows = get_candles(timeframe)

    news_items = gdelt_news()

    n = news_score(news_items)

    t = technical(rows)

    return {

        "server_time":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "timeframe":
            timeframe,

        "market":
            market,

        "technical":
            t,

        "news_bias":
            n,

        "signal":
            build_signal(t, n),

        "news":
            news_items
    }


HTML_PAGE = """
<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
content="width=device-width,initial-scale=1">

<meta name="theme-color"
content="#08111f">

<link rel="manifest"
href="/manifest.json">

<title>Gold AI Live</title>

<style>

body{
margin:0;
background:#08111f;
color:#f2f6ff;
font-family:system-ui
}

main{
max-width:950px;
margin:auto;
padding:16px
}

.top{
display:flex;
justify-content:space-between;
align-items:center
}

h1{
margin:5px 0;
font-size:25px
}

.muted{
color:#91a0b7;
font-size:13px
}

.price{
font-size:42px;
font-weight:850;
margin-top:10px
}

.card{
background:#101b2d;
border:1px solid #243552;
border-radius:18px;
padding:16px;
margin-top:12px
}

.tf{
display:grid;
grid-template-columns:
repeat(7,1fr);
gap:6px;
margin-top:14px
}

button{
background:#17243a;
border:1px solid #30435f;
color:#eaf0fb;
border-radius:10px;
padding:10px 4px;
font-weight:750
}

button.active{
background:#2187ff
}

.signal{
font-size:34px;
font-weight:900
}

.buy{
color:#4ee0ad
}

.sell{
color:#ff7188
}

.wait{
color:#ffd166
}

.grid{
display:grid;
grid-template-columns:
1fr 1fr;
gap:12px
}

.row{
display:flex;
justify-content:space-between;
padding:8px 0;
border-bottom:
1px solid #1f2b40
}

.news{
padding:10px 0;
border-bottom:
1px solid #202e44
}

.news a{
color:#dbe8ff;
text-decoration:none
}

.badge{
display:inline-block;
border-radius:20px;
padding:5px 9px;
font-size:12px;
font-weight:800;
background:#26354d
}

.live{
color:#4ee0ad
}

.levels{
display:grid;
grid-template-columns:
repeat(3,1fr);
gap:8px;
margin-top:10px
}

.level{
background:#0b1525;
border:1px solid #23344f;
border-radius:12px;
padding:10px
}

.metric{
font-size:18px;
font-weight:800
}

@media(max-width:650px){

.tf{
grid-template-columns:
repeat(4,1fr)
}

.grid{
grid-template-columns:1fr
}

.price{
font-size:34px
}

.levels{
grid-template-columns:1fr
}

}

</style>

</head>

<body>

<main>

<div class="top">

<div>

<h1>🥇 Gold AI Live</h1>

<div class="muted">
XAUUSD • Live market + multi-timeframe signal
</div>

</div>

<button onclick="load()">
Refresh
</button>

</div>

<div class="tf" id="tf"></div>

<div class="card">

<div class="muted">

XAUUSD

<span class="badge live"
id="status">
LIVE
</span>

</div>

<div class="price"
id="price">
—
</div>

<div class="muted"
id="src">
—
</div>

</div>

<div class="grid">

<div class="card">

<div class="muted">

AI CALL •
<span id="tfl">1H</span>

</div>

<div id="sig"
class="signal wait">
LOADING
</div>

<div class="row">
<span>BUY probability</span>
<b id="buy">—</b>
</div>

<div class="row">
<span>SELL probability</span>
<b id="sell">—</b>
</div>

<div class="row">
<span>Confidence</span>
<b id="conf">—</b>
</div>

<div class="levels">

<div class="level">

<div class="muted">
ENTRY
</div>

<div class="metric"
id="entry">
—
</div>

</div>

<div class="level">

<div class="muted">
STOP LOSS
</div>

<div class="metric"
id="sl">
—
</div>

</div>

<div class="level">

<div class="muted">
TP1 / TP2
</div>

<div class="metric"
id="tp">
—
</div>

</div>

</div>

<p class="muted"
id="rr">
Risk/Reward: —
</p>

<p class="muted"
id="reason">
—
</p>

</div>

<div class="card">

<div class="muted">
TECHNICAL DATA
</div>

<div class="row">
<span>EMA20</span>
<b id="e20">—</b>
</div>

<div class="row">
<span>EMA50</span>
<b id="e50">—</b>
</div>

<div class="row">
<span>EMA200</span>
<b id="e200">—</b>
</div>

<div class="row">
<span>RSI</span>
<b id="rsi">—</b>
</div>

<div class="row">
<span>MACD Histogram</span>
<b id="macd">—</b>
</div>

<div class="row">
<span>ATR</span>
<b id="atr">—</b>
</div>

</div>

</div>

<div class="card">

<div class="muted">
GLOBAL GOLD NEWS
</div>

<p>

<span class="badge"
id="nb">
NEUTRAL
</span>

<span class="muted"
id="ns">
Score 50
</span>

</p>

<div id="news">
Loading news…
</div>

</div>

<div class="card">

<b>⚠️ Risk notice</b>

<p class="muted">

This is a probabilistic
technical/news model,
not a guaranteed prediction.
It can be wrong.

NO TRADE is used when
factors are not sufficiently
aligned.

Test on demo/paper trading
before risking real money.

</p>

</div>

<div class="muted"
id="updated">
—
</div>

</main>

<script>

const T = [
"1m",
"5m",
"15m",
"30m",
"1H",
"4H",
"1D"
];

let tf = "1H";

const box =
document.getElementById("tf");

T.forEach(x => {

let b =
document.createElement("button");

b.textContent = x;

b.onclick = () => {

tf = x;

paint();

load();

};

b.id = "x" + x;

box.appendChild(b);

});


function paint(){

T.forEach(x => {

document
.getElementById("x" + x)
.classList
.toggle(
"active",
x === tf
);

});

}


function fmt(v){

return v == null
? "—"
: Number(v).toFixed(2);

}


async function load(){

try{

document
.getElementById("status")
.textContent =
"LOADING";

let r =
await fetch(
"/api/dashboard?timeframe="
+ tf,
{
cache:"no-store"
}
);

let d =
await r.json();

if(!r.ok || d.detail)
throw Error(
d.detail || "API error"
);

let s = d.signal;
let n = d.news_bias;
let t = d.technical;

document
.getElementById("status")
.textContent =
"LIVE";

document
.getElementById("price")
.textContent =
fmt(d.market.price);

document
.getElementById("src")
.textContent =
d.market.source
+ " • "
+ tf;

document
.getElementById("tfl")
.textContent =
tf;

let el =
document.getElementById("sig");

el.textContent =
s.signal;

el.className =
"signal "
+
(
s.signal === "BUY"
? "buy"
: s.signal === "SELL"
? "sell"
: "wait"
);

document
.getElementById("buy")
.textContent =
s.buy + "%";

document
.getElementById("sell")
.textContent =
s.sell + "%";

document
.getElementById("conf")
.textContent =
s.confidence + "%";

document
.getElementById("entry")
.textContent =
fmt(s.entry);

document
.getElementById("sl")
.textContent =
fmt(s.stop_loss);

document
.getElementById("tp")
.textContent =
s.tp1 == null
? "—"
: fmt(s.tp1)
+ " / "
+ fmt(s.tp2);

document
.getElementById("rr")
.textContent =
"Risk/Reward: "
+ s.risk_reward;

document
.getElementById("reason")
.textContent =
s.reason;

document
.getElementById("e20")
.textContent =
fmt(t.ema20);

document
.getElementById("e50")
.textContent =
fmt(t.ema50);

document
.getElementById("e200")
.textContent =
fmt(t.ema200);

document
.getElementById("rsi")
.textContent =
Number(t.rsi)
.toFixed(1);

document
.getElementById("macd")
.textContent =
Number(t.macd_hist)
.toFixed(3);

document
.getElementById("atr")
.textContent =
fmt(t.atr);

document
.getElementById("nb")
.textContent =
n.bias;

document
.getElementById("ns")
.textContent =
"Score "
+ n.score;

document
.getElementById("news")
.innerHTML =
d.news
.map(a =>
'<div class="news">'
+
'<a href="'
+ a.url
+ '" target="_blank" rel="noopener">'
+ a.title
+ '</a>'
+
'<div class="muted">'
+ a.source
+ '</div>'
+
'</div>'
)
.join("")
||
'<span class="muted">'
+ "No articles returned."
+ '</span>';

document
.getElementById("updated")
.textContent =
"Updated "
+
new Date(
d.server_time
)
.toLocaleString();

}

catch(e){

document
.getElementById("status")
.textContent =
"ERROR";

document
.getElementById("updated")
.textContent =
"Error: "
+ e.message;

}

}


paint();

load();

setInterval(
load,
60000
);

if(
"serviceWorker"
in navigator
){

navigator
.serviceWorker
.register("/sw.js")
.catch(
() => {}
);

}

</script>

</body>

</html>
"""


MANIFEST = '''
{
"name":"Gold AI Live",
"short_name":"GoldAI",
"start_url":"/",
"display":"standalone",
"background_color":"#08111f",
"theme_color":"#08111f"
}
'''


SW_JS = '''
const CACHE="gold-ai-v2";

self.addEventListener(
"install",
e =>
e.waitUntil(
caches.open(CACHE)
.then(
c =>
c.addAll([
"/",
"/manifest.json"
])
)
)
);

self.addEventListener(
"fetch",
e =>
e.respondWith(
fetch(e.request)
.catch(
() =>
caches.match(e.request)
)
)
);
'''


@app.get(
"/",
response_class=HTMLResponse
)
def home():
    return HTML_PAGE


@app.get("/manifest.json")
def manifest():

    return Response(
        MANIFEST,
        media_type="application/manifest+json"
    )


@app.get("/sw.js")
def service_worker():

    return Response(
        SW_JS,
        media_type="application/javascript"
    )
