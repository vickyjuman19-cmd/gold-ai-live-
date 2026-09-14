import os
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response
from dotenv import load_dotenv


# ============================================================
# GOLD AI LIVE v4
# ============================================================

load_dotenv()

app = FastAPI(
    title="Gold AI Live v4",
    version="4.0"
)


# ============================================================
# API CONFIG
# ============================================================

PRICE_API_KEY = os.getenv(
    "PRICE_API_KEY",
    ""
)

TD = "https://api.twelvedata.com"

GDELT = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
)


TIMEFRAMES = [
    "1m",
    "5m",
    "15m",
    "30m",
    "1H",
    "4H",
    "1D"
]


TD_INTERVALS = {

    "1m": "1min",

    "5m": "5min",

    "15m": "15min",

    "30m": "30min",

    "1H": "1h",

    "4H": "4h",

    "1D": "1day"
}


# ============================================================
# REFRESH SETTINGS
# ============================================================

PRICE_CACHE_SECONDS = 3

SIGNAL_CACHE_SECONDS = 10

NEWS_CACHE_SECONDS = 60


# ============================================================
# CACHES
# ============================================================

CACHE_LOCK = threading.Lock()


PRICE_CACHE = {

    "data": None,

    "updated": 0
}


NEWS_CACHE = {

    "items": [],

    "updated": 0
}


TECH_CACHE = {}


# ============================================================
# TWELVE DATA REQUEST
# ============================================================

def td_get(path, params):

    if not PRICE_API_KEY:

        raise RuntimeError(
            "PRICE_API_KEY is missing"
        )


    p = dict(params)

    p["apikey"] = PRICE_API_KEY


    r = requests.get(

        TD + path,

        params=p,

        timeout=5
    )


    r.raise_for_status()


    d = r.json()


    if (

        d.get("status") == "error"

        or

        d.get("code")

    ):

        raise RuntimeError(

            d.get(
                "message",
                "Twelve Data error"
            )

        )


    return d


# ============================================================
# LIVE PRICE
# ============================================================

def get_price():

    now = datetime.now(
        timezone.utc
    ).timestamp()


    with CACHE_LOCK:

        if (

            PRICE_CACHE["data"]

            is not None

            and

            now
            -
            PRICE_CACHE["updated"]

            <
            PRICE_CACHE_SECONDS

        ):

            return PRICE_CACHE["data"]


    d = td_get(

        "/quote",

        {
            "symbol":
                "XAU/USD"
        }

    )


    p = (

        d.get("price")

        or

        d.get("close")

    )


    if p is None:

        raise RuntimeError(
            "No current XAU/USD price"
        )


    p = float(p)


    result = {

        "price":
            p,

        "bid":
            float(
                d.get("bid")
                or p
            ),

        "ask":
            float(
                d.get("ask")
                or p
            ),

        "source":
            "Twelve Data LIVE",

        "datetime":
            d.get(
                "datetime",
                ""
            )
    }


    with CACHE_LOCK:

        PRICE_CACHE["data"] = result

        PRICE_CACHE["updated"] = now


    return result


# ============================================================
# CANDLES
# ============================================================

def get_candles(
    tf,
    outputsize=250
):

    d = td_get(

        "/time_series",

        {

            "symbol":
                "XAU/USD",

            "interval":
                TD_INTERVALS[tf],

            "outputsize":
                outputsize,

            "order":
                "ASC"

        }

    )


    vals = (
        d.get("values")
        or []
    )


    rows = []


    for x in vals:

        try:

            rows.append({

                "datetime":
                    x.get(
                        "datetime",
                        ""
                    ),

                "open":
                    float(
                        x["open"]
                    ),

                "high":
                    float(
                        x["high"]
                    ),

                "low":
                    float(
                        x["low"]
                    ),

                "close":
                    float(
                        x["close"]
                    )

            })


        except (
            KeyError,
            TypeError,
            ValueError
        ):

            pass


    if len(rows) < 60:

        raise RuntimeError(

            f"Not enough candles "
            f"for {tf}: "
            f"{len(rows)}"

        )


    return rows


# ============================================================
# EMA
# ============================================================

def ema(v, n):

    if not v:

        return []


    k = 2 / (n + 1)


    out = [
        v[0]
    ]


    for x in v[1:]:

        out.append(

            x * k

            +

            out[-1]
            *
            (1 - k)

        )


    return out


# ============================================================
# RSI
# ============================================================

def rsi(v, n=14):

    if len(v) < n + 1:

        return 50.0


    gains = []

    losses = []


    for i in range(
        1,
        len(v)
    ):

        ch = (
            v[i]
            -
            v[i - 1]
        )


        gains.append(
            max(ch, 0)
        )


        losses.append(
            max(-ch, 0)
        )


    ag = (
        sum(
            gains[:n]
        )
        /
        n
    )


    al = (
        sum(
            losses[:n]
        )
        /
        n
    )


    for i in range(
        n,
        len(gains)
    ):

        ag = (

            ag * (n - 1)

            +

            gains[i]

        ) / n


        al = (

            al * (n - 1)

            +

            losses[i]

        ) / n


    if al == 0:

        return (
            100.0
            if ag > 0
            else 50.0
        )


    rs = ag / al


    return (

        100

        -

        (
            100
            /
            (1 + rs)
        )

    )


# ============================================================
# ATR
# ============================================================

def atr(rows, n=14):

    tr = []


    for i, x in enumerate(rows):

        if i == 0:

            t = (

                x["high"]
                -
                x["low"]

            )

        else:

            pc = rows[
                i - 1
            ]["close"]


            t = max(

                x["high"]
                -
                x["low"],

                abs(
                    x["high"]
                    -
                    pc
                ),

                abs(
                    x["low"]
                    -
                    pc
                )

            )


        tr.append(t)


    if len(tr) < n:

        return 0.01


    a = (

        sum(
            tr[:n]
        )
        /
        n

    )


    for x in tr[n:]:

        a = (

            a * (n - 1)

            +

            x

        ) / n


    return max(
        a,
        0.01
    )


# ============================================================
# MACD
# ============================================================

def macd(v):

    e12 = ema(
        v,
        12
    )


    e26 = ema(
        v,
        26
    )


    line = [

        a - b

        for a, b
        in zip(
            e12,
            e26
        )

    ]


    sig = ema(
        line,
        9
    )


    return (

        line[-1],

        sig[-1],

        line[-1]
        -
        sig[-1]

    )


# ============================================================
# TECHNICAL ENGINE
# ============================================================

def technical(rows):

    c = [

        x["close"]

        for x in rows

    ]


    p = c[-1]


    e20 = ema(
        c,
        20
    )[-1]


    e50 = ema(
        c,
        50
    )[-1]


    e200 = ema(
        c,
        200
    )[-1]


    r = rsi(c)


    ml, ms, mh = macd(c)


    a = atr(rows)


    score = 0.0


    # Price vs EMA20

    score += (

        1
        if p > e20
        else -1

    )


    # EMA20 vs EMA50

    score += (

        1
        if e20 > e50
        else -1

    )


    # Price vs EMA200

    score += (

        1
        if p > e200
        else -1

    )


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

    score += (

        1
        if mh > 0
        else -1

    )


    # Momentum

    if len(c) >= 4:

        if c[-1] > c[-4]:

            score += 0.5

        elif c[-1] < c[-4]:

            score -= 0.5


    norm = max(

        -1,

        min(
            1,
            score / 5.5
        )

    )


    return {

        "price":
            p,

        "ema20":
            e20,

        "ema50":
            e50,

        "ema200":
            e200,

        "rsi":
            r,

        "macd":
            ml,

        "macd_signal":
            ms,

        "macd_hist":
            mh,

        "atr":
            a,

        "norm":
            norm

    }


# ============================================================
# GOLD NEWS
# ============================================================

def gdelt_news():

    now = datetime.now(
        timezone.utc
    ).timestamp()


    # Fast cache

    with CACHE_LOCK:

        if (

            NEWS_CACHE["items"]

            and

            now
            -
            NEWS_CACHE["updated"]

            <
            NEWS_CACHE_SECONDS

        ):

            return {

                "items":
                    NEWS_CACHE["items"],

                "status":
                    "CACHE",

                "updated":
                    NEWS_CACHE["updated"]

            }


    query = (

        '(gold OR XAUUSD OR '
        '"XAU/USD" OR bullion) AND '

        '(Fed OR "Federal Reserve" '
        'OR inflation OR CPI OR PPI '
        'OR yields OR dollar OR USD '
        'OR Treasury OR geopolitics '
        'OR war OR conflict OR tariff '
        'OR "central bank" '
        'OR "interest rate" '
        'OR recession)'

    )


    try:

        r = requests.get(

            GDELT,

            params={

                "query":
                    query,

                "mode":
                    "artlist",

                "maxrecords":
                    20,

                "format":
                    "json",

                "sort":
                    "datedesc",

                "timespan":
                    "24h"

            },

            timeout=4

        )


        r.raise_for_status()


        data = r.json()


        articles = (
            data.get(
                "articles",
                []
            )
        )


        items = []


        for a in articles[:20]:

            title = (

                a.get(
                    "title",
                    ""
                )

                or ""

            ).strip()


            url = (

                a.get(
                    "url",
                    ""
                )

                or ""

            ).strip()


            if not title or not url:

                continue


            items.append({

                "title":
                    title,

                "url":
                    url,

                "source":
                    a.get(
                        "domain",
                        ""
                    ),

                "published":
                    a.get(
                        "seendate",
                        ""
                    )

            })


        if items:

            with CACHE_LOCK:

                NEWS_CACHE["items"] = items

                NEWS_CACHE["updated"] = now


            return {

                "items":
                    items,

                "status":
                    "LIVE",

                "updated":
                    now

            }


        with CACHE_LOCK:

            if NEWS_CACHE["items"]:

                return {

                    "items":
                        NEWS_CACHE["items"],

                    "status":
                        "CACHE",

                    "updated":
                        NEWS_CACHE["updated"]

                }


        return {

            "items": [],

            "status":
                "NO_DATA",

            "updated":
                0

        }


    except Exception:

        with CACHE_LOCK:

            if NEWS_CACHE["items"]:

                return {

                    "items":
                        NEWS_CACHE["items"],

                    "status":
                        "CACHE",

                    "updated":
                        NEWS_CACHE["updated"]

                }


        return {

            "items": [],

            "status":
                "UNAVAILABLE",

            "updated":
                0

        }


# ============================================================
# NEWS SCORE
# ============================================================

def news_score(items):

    bull = [

        "rate cut",
        "rate cuts",
        "dovish",
        "lower yields",
        "falling yields",
        "weaker dollar",
        "weak dollar",
        "safe haven",
        "war",
        "conflict",
        "geopolitical",
        "geopolitics",
        "central bank buying",
        "central banks buying",
        "tariff",
        "trade war",
        "recession",
        "economic slowdown",
        "rate reduction"

    ]


    bear = [

        "rate hike",
        "rate hikes",
        "hawkish",
        "higher yields",
        "rising yields",
        "strong dollar",
        "strong usd",
        "hot inflation",
        "cpi above",
        "ppi above",
        "fed tightening",
        "monetary tightening",
        "rate increase",
        "higher interest rates"

    ]


    bullish = 0

    bearish = 0


    for item in items:

        title = (

            item.get(
                "title",
                ""
            )

            or ""

        ).lower()


        bullish += sum(

            word in title

            for word in bull

        )


        bearish += sum(

            word in title

            for word in bear

        )


    total = (
        bullish
        +
        bearish
    )


    if total == 0:

        return {

            "score":
                50,

            "bias":
                "NEUTRAL",

            "bullish":
                0,

            "bearish":
                0

        }


    score = max(

        0,

        min(

            100,

            50
            +
            45
            *
            (
                bullish
                -
                bearish
            )
            /
            max(
                1,
                total
            )

        )

    )


    if score >= 60:

        bias = "BULLISH"

    elif score <= 40:

        bias = "BEARISH"

    else:

        bias = "NEUTRAL"


    return {

        "score":
            round(
                score,
                1
            ),

        "bias":
            bias,

        "bullish":
            bullish,

        "bearish":
            bearish

    }


# ============================================================
# SIGNAL ENGINE
# ============================================================

def build_signal(
    t,
    n
):

    tech = t["norm"]


    news = (

        n["score"]
        -
        50
    ) / 50


    combined = (

        0.75 * tech

        +

        0.25 * news

    )


    buy = (

        50
        +
        combined
        *
        38

    )


    sell = (
        100
        -
        buy
    )


    # --------------------------------------------------------
    # NORMAL SIGNAL
    # --------------------------------------------------------

    if abs(combined) < 0.22:

        action = "NO TRADE"


    elif combined > 0:

        action = "BUY"


    else:

        action = "SELL"


    # --------------------------------------------------------
    # STRONG SIGNAL
    # --------------------------------------------------------

    if combined >= 0.70:

        strength = "STRONG BUY"

    elif combined >= 0.35:

        strength = "BUY"

    elif combined <= -0.70:

        strength = "STRONG SELL"

    elif combined <= -0.35:

        strength = "SELL"

    else:

        strength = "NO TRADE"


    confidence = min(

        95,

        max(

            50,

            50
            +
            abs(combined)
            *
            45

        )

    )


    entry = t["price"]


    risk = (
        t["atr"]
        *
        1.5
    )


    if strength in (
        "BUY",
        "STRONG BUY"
    ):

        sl = (
            entry
            -
            risk
        )

        tp1 = (
            entry
            +
            risk * 1.5
        )

        tp2 = (
            entry
            +
            risk * 2.5
        )


    elif strength in (
        "SELL",
        "STRONG SELL"
    ):

        sl = (
            entry
            +
            risk
        )

        tp1 = (
            entry
            -
            risk * 1.5
        )

        tp2 = (
            entry
            -
            risk * 2.5
        )


    else:

        sl = None

        tp1 = None

        tp2 = None


    return {

        "buy":
            round(
                buy,
                1
            ),

        "sell":
            round(
                sell,
                1
            ),

        "confidence":
            round(
                confidence,
                1
            ),

        "signal":
            action,

        "strength":
            strength,

        "entry":
            round(
                entry,
                2
            ),

        "stop_loss":

            round(
                sl,
                2
            )
            if sl is not None
            else None,

        "tp1":

            round(
                tp1,
                2
            )
            if tp1 is not None
            else None,

        "tp2":

            round(
                tp2,
                2
            )
            if tp2 is not None
            else None,

        "risk_reward":

            "1:1.5 / 1:2.5"
            if strength != "NO TRADE"
            else "—",

        "reason":

            (
                "Strong technical + "
                "news confluence."
                if strength.startswith(
                    "STRONG"
                )
                else
                "Technical + news "
                "confluence supports "
                + strength
                + "."
                if strength != "NO TRADE"
                else
                "Factors are not "
                "sufficiently aligned."
            )

    }


# ============================================================
# FAST TECHNICAL CACHE
# ============================================================

def get_signal_data(
    timeframe
):

    now = datetime.now(
        timezone.utc
    ).timestamp()


    with CACHE_LOCK:

        cached = TECH_CACHE.get(
            timeframe
        )


        if cached:

            if (

                now
                -
                cached["updated"]

                <
                SIGNAL_CACHE_SECONDS

            ):

                return cached


    rows = get_candles(
        timeframe
    )


    t = technical(
        rows
    )


    with CACHE_LOCK:

        TECH_CACHE[
            timeframe
        ] = {

            "technical":
                t,

            "updated":
                now

        }


    return TECH_CACHE[
        timeframe
    ]


# ============================================================
# INITIAL DASHBOARD
# ============================================================

@app.get(
    "/api/dashboard"
)
def dashboard(

    timeframe: str = Query(
        "1H"
    )

):

    if timeframe not in TIMEFRAMES:

        timeframe = "1H"


    # Parallel initial loading

    with ThreadPoolExecutor(
        max_workers=3
    ) as executor:

        price_future = (
            executor.submit(
                get_price
            )
        )

        technical_future = (
            executor.submit(
                get_signal_data,
                timeframe
            )
        )

        news_future = (
            executor.submit(
                gdelt_news
            )
        )


        market = (
            price_future.result()
        )


        technical_data = (
            technical_future.result()
        )


        news_data = (
            news_future.result()
        )


    t = (
        technical_data[
            "technical"
        ]
    )


    news_items = (
        news_data["items"]
    )


    n = news_score(
        news_items
    )


    signal = build_signal(
        t,
        n
    )


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

        "news_status":
            news_data["status"],

        "news_updated":
            news_data["updated"],

        "signal":
            signal,

        "news":
            news_items

    }


# ============================================================
# FAST PRICE API
# ============================================================

@app.get(
    "/api/live-price"
)
def live_price():

    try:

        market = get_price()


        return {

            "ok":
                True,

            "price":
                market["price"],

            "bid":
                market["bid"],

            "ask":
                market["ask"],

            "source":
                market["source"],

            "server_time":
                datetime.now(
                    timezone.utc
                ).isoformat()

        }


    except Exception as e:

        return {

            "ok":
                False,

            "error":
                str(e)

        }


# ============================================================
# FAST SIGNAL API
# ============================================================

@app.get(
    "/api/live-signal"
)
def live_signal(

    timeframe: str = Query(
        "1H"
    )

):

    if timeframe not in TIMEFRAMES:

        timeframe = "1H"


    try:

        technical_data = (
            get_signal_data(
                timeframe
            )
        )


        news_data = (
            gdelt_news()
        )


        t = (
            technical_data[
                "technical"
            ]
        )


        n = news_score(
            news_data["items"]
        )


        signal = build_signal(
            t,
            n
        )


        return {

            "ok":
                True,

            "timeframe":
                timeframe,

            "technical":
                t,

            "news_bias":
                n,

            "news_status":
                news_data[
                    "status"
                ],

            "signal":
                signal,

            "server_time":
                datetime.now(
                    timezone.utc
                ).isoformat()

        }


    except Exception as e:

        return {

            "ok":
                False,

            "error":
                str(e)

        }


# ============================================================
# HTML
# ============================================================

HTML_PAGE = r"""
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

<title>Gold AI Live v4</title>


<style>

*{
box-sizing:border-box
}


body{

margin:0;

background:#08111f;

color:#f2f6ff;

font-family:
system-ui,
-apple-system,
BlinkMacSystemFont,
"Segoe UI",
sans-serif

}


main{

max-width:950px;

margin:auto;

padding:16px

}


.top{

display:flex;

justify-content:
space-between;

align-items:center;

gap:10px

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

border:
1px solid #243552;

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

border:
1px solid #30435f;

color:#eaf0fb;

border-radius:10px;

padding:10px 4px;

font-weight:750;

cursor:pointer

}


button.active{

background:#2187ff

}


button:disabled{

opacity:.6

}


.signal{

font-size:34px;

font-weight:900;

margin-top:8px

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


.strong{

font-size:39px

}


.grid{

display:grid;

grid-template-columns:
1fr 1fr;

gap:12px

}


.row{

display:flex;

justify-content:
space-between;

padding:8px 0;

border-bottom:
1px solid #1f2b40

}


.news{

padding:11px 0;

border-bottom:
1px solid #202e44

}


.news a{

color:#dbe8ff;

text-decoration:none;

font-weight:600;

line-height:1.35

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


.cache{

color:#ffd166

}


.error{

color:#ff7188

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

border:
1px solid #23344f;

border-radius:12px;

padding:10px

}


.metric{

font-size:18px;

font-weight:800

}


#lastSignal{

margin-top:8px

}


@media(max-width:650px){

.tf{

grid-template-columns:
repeat(4,1fr)

}


.grid{

grid-template-columns:
1fr

}


.price{

font-size:34px

}


.signal{

font-size:30px

}


.strong{

font-size:34px

}


.levels{

grid-template-columns:
1fr

}

}

</style>

</head>


<body>

<main>


<div class="top">

<div>

<h1>
🥇 Gold AI Live v4
</h1>

<div class="muted">

XAUUSD • Fast Live AI
Technical + Global News

</div>

</div>


<button
onclick="fullRefresh()"
id="refreshBtn">

Refresh

</button>

</div>


<div
class="tf"
id="tf">
</div>


<!-- PRICE -->

<div class="card">

<div class="muted">

XAUUSD

<span
class="badge live"
id="status">

LIVE

</span>

</div>


<div
class="price"
id="price">

—

</div>


<div
class="muted"
id="src">

—

</div>

</div>


<div class="grid">


<!-- SIGNAL -->

<div class="card">

<div class="muted">

AI CALL •
<span id="tfl">
1H
</span>

</div>


<div
id="sig"
class="signal wait">

LOADING

</div>


<div
id="lastSignal"
class="muted">

—

</div>


<div class="row">

<span>
BUY probability
</span>

<b id="buy">
—
</b>

</div>


<div class="row">

<span>
SELL probability
</span>

<b id="sell">
—
</b>

</div>


<div class="row">

<span>
Confidence
</span>

<b id="conf">
—
</b>

</div>


<div class="levels">


<div class="level">

<div class="muted">
ENTRY
</div>

<div
class="metric"
id="entry">

—

</div>

</div>


<div class="level">

<div class="muted">
STOP LOSS
</div>

<div
class="metric"
id="sl">

—

</div>

</div>


<div class="level">

<div class="muted">
TP1 / TP2
</div>

<div
class="metric"
id="tp">

—

</div>

</div>


</div>


<p
class="muted"
id="rr">

Risk/Reward: —

</p>


<p
class="muted"
id="reason">

—

</p>

</div>


<!-- TECHNICAL -->

<div class="card">

<div class="muted">

TECHNICAL DATA

</div>


<div class="row">

<span>
EMA20
</span>

<b id="e20">
—
</b>

</div>


<div class="row">

<span>
EMA50
</span>

<b id="e50">
—
</b>

</div>


<div class="row">

<span>
EMA200
</span>

<b id="e200">
—
</b>

</div>


<div class="row">

<span>
RSI
</span>

<b id="rsi">
—
</b>

</div>


<div class="row">

<span>
MACD Histogram
</span>

<b id="macd">
—
</b>

</div>


<div class="row">

<span>
ATR
</span>

<b id="atr">
—
</b>

</div>

</div>

</div>


<!-- NEWS -->

<div class="card">

<div class="muted">

GLOBAL GOLD NEWS

<span
class="badge"
id="newsStatus">

—

</span>

</div>


<p>

<span
class="badge"
id="nb">

NEUTRAL

</span>


<span
class="muted"
id="ns">

Score 50

</span>

</p>


<div
id="news">

Loading news…

</div>


<div
class="muted"
id="newsUpdated">

—

</div>

</div>


<!-- RISK -->

<div class="card">

<b>
⚠️ Risk notice
</b>


<p class="muted">

This system gives
probabilistic market
signals. BUY/SELL or
STRONG BUY/STRONG SELL
is not a guaranteed
prediction.

</p>


<p class="muted">

Always verify market
conditions and use
appropriate risk
management.

</p>

</div>


<div
class="muted"
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
document.getElementById(
"tf"
);


T.forEach(
function(x){

const b =
document.createElement(
"button"
);


b.textContent = x;


b.onclick =
function(){

tf = x;

paint();

fullRefresh();

};


b.id =
"x" + x;


box.appendChild(b);

});


function paint(){

T.forEach(
function(x){

document
.getElementById(
"x" + x
)
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

:

Number(v)
.toFixed(2);

}


function setSignal(
s
){

const el =
document.getElementById(
"sig"
);


el.textContent =
s.strength;


if(
s.strength ===
"STRONG BUY"
){

el.className =
"signal buy strong";

}

else if(
s.strength ===
"BUY"
){

el.className =
"signal buy";

}

else if(
s.strength ===
"STRONG SELL"
){

el.className =
"signal sell strong";

}

else if(
s.strength ===
"SELL"
){

el.className =
"signal sell";

}

else{

el.className =
"signal wait";

}


document
.getElementById(
"buy"
)
.textContent =
s.buy + "%";


document
.getElementById(
"sell"
)
.textContent =
s.sell + "%";


document
.getElementById(
"conf"
)
.textContent =
s.confidence + "%";


document
.getElementById(
"entry"
)
.textContent =
fmt(s.entry);


document
.getElementById(
"sl"
)
.textContent =
fmt(s.stop_loss);


document
.getElementById(
"tp"
)
.textContent =

s.tp1 == null

? "—"

:

fmt(s.tp1)
+
" / "
+
fmt(s.tp2);


document
.getElementById(
"rr"
)
.textContent =

"Risk/Reward: "
+
s.risk_reward;


document
.getElementById(
"reason"
)
.textContent =
s.reason;


document
.getElementById(
"lastSignal"
)
.textContent =

"Signal updated: "
+
new Date()
.toLocaleTimeString();

}


function setTechnical(
t
){

document
.getElementById(
"e20"
)
.textContent =
fmt(t.ema20);


document
.getElementById(
"e50"
)
.textContent =
fmt(t.ema50);


document
.getElementById(
"e200"
)
.textContent =
fmt(t.ema200);


document
.getElementById(
"rsi"
)
.textContent =
Number(
t.rsi
)
.toFixed(1);


document
.getElementById(
"macd"
)
.textContent =
Number(
t.macd_hist
)
.toFixed(3);


document
.getElementById(
"atr"
)
.textContent =
fmt(t.atr);

}


function setNews(
d
){

const n =
d.news_bias;


document
.getElementById(
"nb"
)
.textContent =
n.bias;


document
.getElementById(
"ns"
)
.textContent =

"Score "
+
n.score;


const status =
document.getElementById(
"newsStatus"
);


status.textContent =
d.news_status;


status.className =
"badge " +

(

d.news_status ===
"LIVE"

? "live"

:

d.news_status ===
"CACHE"

? "cache"

: "error"

);


const container =
document.getElementById(
"news"
);


container.innerHTML = "";


if(
!d.news ||
!d.news.length
){

container.innerHTML =
'<span class="muted">' +
'No recent gold news available.' +
'</span>';

}

else{

d.news.forEach(
function(a){

const div =
document.createElement(
"div"
);

div.className =
"news";


const link =
document.createElement(
"a"
);

link.href =
a.url || "#";

link.target =
"_blank";

link.rel =
"noopener noreferrer";

link.textContent =
a.title ||
"Gold news";


const meta =
document.createElement(
"div"
);

meta.className =
"muted";


meta.textContent =

(
a.source ||
"Global source"
)

+

" • "

+

(
a.published ||
"Recent"
);


div.appendChild(
link
);

div.appendChild(
meta
);

container.appendChild(
div
);

});

}


if(
d.news_updated
){

document
.getElementById(
"newsUpdated"
)
.textContent =

"News updated: "

+

new Date(
d.news_updated * 1000
)
.toLocaleString();

}

}


function setPrice(
d
){

if(
!d ||
!d.ok
){

return;

}


document
.getElementById(
"price"
)
.textContent =
fmt(d.price);


document
.getElementById(
"status"
)
.textContent =
"LIVE";


document
.getElementById(
"src"
)
.textContent =
d.source
+
" • "
+
tf;

}


async function fastPrice(){

try{

const r =
await fetch(

"/api/live-price",

{
cache:
"no-store"
}

);


const d =
await r.json();


if(d.ok){

setPrice(d);

}

}

catch(e){

// Keep previous valid price

}

}


async function fastSignal(){

try{

const r =
await fetch(

"/api/live-signal?timeframe="
+
encodeURIComponent(tf),

{
cache:
"no-store"
}

);


const d =
await r.json();


if(
!r.ok ||
!d.ok
){

return;

}


setSignal(
d.signal
);


setTechnical(
d.technical
);


const newsData = {

news_bias:
d.news_bias,

news_status:
d.news_status,

news_updated:
0,

news: []

};


document
.getElementById(
"nb"
)
.textContent =
d.news_bias.bias;


document
.getElementById(
"ns"
)
.textContent =

"Score "
+
d.news_bias.score;


document
.getElementById(
"lastSignal"
)
.textContent =

"Signal updated: "
+
new Date()
.toLocaleTimeString();

}

catch(e){

// Keep previous valid signal

}

}


async function fullRefresh(){

const refresh =
document.getElementById(
"refreshBtn"
);


try{

refresh.disabled =
true;


document
.getElementById(
"status"
)
.textContent =
"LOADING";


const r =
await fetch(

"/api/dashboard?timeframe="
+
encodeURIComponent(tf),

{
cache:
"no-store"
}

);


const d =
await r.json();


if(
!r.ok ||
d.detail
){

throw new Error(
d.detail ||
"API error"
);

}


setPrice({

ok:
true,

price:
d.market.price,

bid:
d.market.bid,

ask:
d.market.ask,

source:
d.market.source

});


setSignal(
d.signal
);


setTechnical(
d.technical
);


setNews(
d
);


document
.getElementById(
"tfl"
)
.textContent =
tf;


document
.getElementById(
"updated"
)
.textContent =

"Dashboard updated: "

+

new Date(
d.server_time
)
.toLocaleString();


}

catch(e){

document
.getElementById(
"status"
)
.textContent =
"ERROR";


document
.getElementById(
"updated"
)
.textContent =

"Error: "
+
e.message;

}

finally{

refresh.disabled =
false;

}

}


paint();

fullRefresh();


/*
============================================================
FAST AUTO REFRESH
============================================================
*/


// Price every 3 seconds

setInterval(

fastPrice,

3000

);


// Signal / technical every 10 seconds

setInterval(

fastSignal,

10000

);


// Full dashboard + news every 60 seconds

setInterval(

fullRefresh,

60000

);


if(
"serviceWorker"
in navigator
){

navigator
.serviceWorker
.register(
"/sw.js"
)
.catch(
function(){}
);

}

</script>


</body>

</html>
"""


# ============================================================
# PWA MANIFEST
# ============================================================

MANIFEST = '''
{
"name":"Gold AI Live v4",
"short_name":"GoldAI",
"start_url":"/",
"display":"standalone",
"background_color":"#08111f",
"theme_color":"#08111f"
}
'''


# ============================================================
# SERVICE WORKER
# ============================================================

SW_JS = '''
const CACHE="gold-ai-v4";

self.addEventListener(
"install",
event => {

event.waitUntil(

caches.open(CACHE)
.then(
cache =>

cache.addAll([
"/",
"/manifest.json"
])

)

);

});


self.addEventListener(
"activate",
event => {

event.waitUntil(
self.clients.claim()
);

});


self.addEventListener(
"fetch",
event => {

if(
event.request.url.includes(
"/api/"
)
){

return;

}


event.respondWith(

fetch(
event.request
)

.catch(

() =>
caches.match(
event.request
)

)

);

});
'''


# ============================================================
# HOME
# ============================================================

@app.get(
"/",
response_class=HTMLResponse
)
def home():

    return HTML_PAGE


# ============================================================
# MANIFEST
# ============================================================

@app.get(
"/manifest.json"
)
def manifest():

    return Response(

        MANIFEST,

        media_type=
        "application/manifest+json"

    )


# ============================================================
# SERVICE WORKER
# ============================================================

@app.get(
"/sw.js"
)
def service_worker():

    return Response(

        SW_JS,

        media_type=
        "application/javascript"

    )
