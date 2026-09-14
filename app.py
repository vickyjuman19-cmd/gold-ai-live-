import os
import time
import threading
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Gold AI Live v5")

PRICE_API_KEY = os.getenv("PRICE_API_KEY", "").strip()
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "").strip()

SYMBOL = "XAU/USD"

# =========================================================
# CACHE / THROTTLING
# =========================================================

PRICE_CACHE = {
    "data": None,
    "time": 0.0,
}

CANDLE_CACHE = {}

NEWS_CACHE = {
    "data": None,
    "time": 0.0,
}

CACHE_LOCK = threading.Lock()

PRICE_CACHE_SECONDS = 15
CANDLE_CACHE_SECONDS = 60
NEWS_CACHE_SECONDS = 300

MIN_PRICE_REQUEST_GAP = 15
MIN_CANDLE_REQUEST_GAP = 60

LAST_PRICE_REQUEST = 0.0
LAST_CANDLE_REQUEST = 0.0


# =========================================================
# HTTP SESSION
# =========================================================

session = requests.Session()
session.headers.update({
    "User-Agent": "Gold-AI-Live/5.0"
})


# =========================================================
# SAFE REQUEST
# =========================================================

def safe_get(url, params=None, timeout=12):
    try:
        response = session.get(
            url,
            params=params,
            timeout=timeout
        )

        if response.status_code == 429:
            return {
                "ok": False,
                "error": "RATE_LIMIT",
                "status": 429
            }

        if response.status_code != 200:
            return {
                "ok": False,
                "error": f"HTTP_{response.status_code}",
                "status": response.status_code
            }

        return {
            "ok": True,
            "data": response.json()
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


# =========================================================
# PRICE
# =========================================================

def get_live_price():

    global LAST_PRICE_REQUEST

    now = time.time()

    with CACHE_LOCK:
        if (
            PRICE_CACHE["data"] is not None
            and now - PRICE_CACHE["time"] < PRICE_CACHE_SECONDS
        ):
            return PRICE_CACHE["data"]

        # Prevent multiple simultaneous API calls
        if now - LAST_PRICE_REQUEST < MIN_PRICE_REQUEST_GAP:
            if PRICE_CACHE["data"] is not None:
                return PRICE_CACHE["data"]

            return {
                "symbol": SYMBOL,
                "price": None,
                "status": "waiting"
            }

        LAST_PRICE_REQUEST = now

    if not PRICE_API_KEY:
        return {
            "symbol": SYMBOL,
            "price": None,
            "status": "missing_api_key"
        }

    url = "https://api.twelvedata.com/price"

    result = safe_get(
        url,
        params={
            "symbol": SYMBOL,
            "apikey": PRICE_API_KEY
        }
    )

    if not result["ok"]:

        if result.get("error") == "RATE_LIMIT":
            with CACHE_LOCK:
                if PRICE_CACHE["data"] is not None:
                    return PRICE_CACHE["data"]

        return {
            "symbol": SYMBOL,
            "price": None,
            "status": "error",
            "error": result.get("error")
        }

    data = result["data"]

    try:
        price = float(data.get("price"))
    except Exception:
        price = None

    output = {
        "symbol": SYMBOL,
        "price": price,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok"
    }

    with CACHE_LOCK:
        PRICE_CACHE["data"] = output
        PRICE_CACHE["time"] = time.time()

    return output


# =========================================================
# CANDLES
# =========================================================

def get_candles(interval="1h", outputsize=200):

    global LAST_CANDLE_REQUEST

    now = time.time()

    cache_key = f"{interval}_{outputsize}"

    with CACHE_LOCK:

        cached = CANDLE_CACHE.get(cache_key)

        if cached:
            if now - cached["time"] < CANDLE_CACHE_SECONDS:
                return cached["data"]

        if now - LAST_CANDLE_REQUEST < MIN_CANDLE_REQUEST_GAP:

            if cached:
                return cached["data"]

            return {
                "status": "waiting",
                "values": []
            }

        LAST_CANDLE_REQUEST = now

    if not PRICE_API_KEY:
        return {
            "status": "missing_api_key",
            "values": []
        }

    url = "https://api.twelvedata.com/time_series"

    result = safe_get(
        url,
        params={
            "symbol": SYMBOL,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": PRICE_API_KEY,
            "format": "JSON"
        },
        timeout=15
    )

    if not result["ok"]:

        if result.get("error") == "RATE_LIMIT":

            with CACHE_LOCK:
                cached = CANDLE_CACHE.get(cache_key)

            if cached:
                return cached["data"]

        return {
            "status": "error",
            "values": [],
            "error": result.get("error")
        }

    raw = result["data"]

    values = raw.get("values", [])

    if not values:
        return {
            "status": "error",
            "values": [],
            "error": raw.get("message", "No candle data")
        }

    candles = []

    for item in reversed(values):

        try:

            candles.append({
                "datetime": item.get("datetime"),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            })

        except Exception:
            continue

    output = {
        "status": "ok",
        "interval": interval,
        "values": candles
    }

    with CACHE_LOCK:
        CANDLE_CACHE[cache_key] = {
            "data": output,
            "time": time.time()
        }

    return output


# =========================================================
# INDICATORS
# =========================================================

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
        result = (
            (price - result) * multiplier
        ) + result

    return result


def calculate_rsi(values, period=14):

    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):

        avg_gain = (
            (avg_gain * (period - 1))
            + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def calculate_macd(values):

    e12 = ema(values, 12)
    e26 = ema(values, 26)

    if e12 is None or e26 is None:
        return None

    macd = e12 - e26

    return macd


def calculate_atr(candles, period=14):

    if len(candles) <= period:
        return None

    true_ranges = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


# =========================================================
# SIGNAL ENGINE
# =========================================================

def generate_signal(candles):

    if len(candles) < 50:

        return {
            "signal": "WAIT",
            "buy_probability": 0,
            "sell_probability": 0,
            "confidence": 0,
            "reason": "Not enough market data"
        }

    closes = [x["close"] for x in candles]

    current = closes[-1]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)

    rsi = calculate_rsi(closes)
    macd = calculate_macd(closes)
    atr = calculate_atr(candles)

    buy_score = 0
    sell_score = 0

    reasons = []

    # EMA 20 / 50
    if ema20 and ema50:

        if ema20 > ema50:
            buy_score += 20
            reasons.append("EMA20 above EMA50")

        elif ema20 < ema50:
            sell_score += 20
            reasons.append("EMA20 below EMA50")

    # EMA 200
    if ema200:

        if current > ema200:
            buy_score += 20
            reasons.append("Price above EMA200")

        else:
            sell_score += 20
            reasons.append("Price below EMA200")

    # RSI
    if rsi is not None:

        if rsi < 30:
            buy_score += 20
            reasons.append("RSI oversold")

        elif rsi > 70:
            sell_score += 20
            reasons.append("RSI overbought")

        elif rsi >= 50:
            buy_score += 10

        else:
            sell_score += 10

    # MACD
    if macd is not None:

        if macd > 0:
            buy_score += 20
            reasons.append("MACD positive")

        else:
            sell_score += 20
            reasons.append("MACD negative")

    # Momentum
    if len(closes) >= 6:

        if current > closes[-6]:
            buy_score += 10

        else:
            sell_score += 10

    total = buy_score + sell_score

    if total <= 0:

        buy_probability = 50
        sell_probability = 50

    else:

        buy_probability = round(
            (buy_score / total) * 100,
            1
        )

        sell_probability = round(
            (sell_score / total) * 100,
            1
        )

    if buy_probability >= 60:

        signal = "BUY"

    elif sell_probability >= 60:

        signal = "SELL"

    else:

        signal = "WAIT"

    confidence = round(
        max(buy_probability, sell_probability),
        1
    )

    return {
        "signal": signal,
        "buy_probability": buy_probability,
        "sell_probability": sell_probability,
        "confidence": confidence,
        "price": current,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "macd": macd,
        "atr": atr,
        "reason": reasons
    }


# =========================================================
# NEWS
# =========================================================

def get_news():

    now = time.time()

    with CACHE_LOCK:

        if (
            NEWS_CACHE["data"] is not None
            and now - NEWS_CACHE["time"] < NEWS_CACHE_SECONDS
        ):
            return NEWS_CACHE["data"]

    if not NEWS_API_KEY:

        return {
            "status": "disabled",
            "articles": []
        }

    url = "https://newsapi.org/v2/everything"

    result = safe_get(
        url,
        params={
            "q": "gold OR XAUUSD OR Federal Reserve OR inflation",
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 10,
            "apiKey": NEWS_API_KEY
        },
        timeout=15
    )

    if not result["ok"]:

        return {
            "status": "error",
            "articles": []
        }

    raw = result["data"]

    articles = []

    for item in raw.get("articles", [])[:10]:

        articles.append({
            "title": item.get("title"),
            "description": item.get("description"),
            "url": item.get("url"),
            "publishedAt": item.get("publishedAt")
        })

    output = {
        "status": "ok",
        "articles": articles
    }

    with CACHE_LOCK:
        NEWS_CACHE["data"] = output
        NEWS_CACHE["time"] = time.time()

    return output


# =========================================================
# API ROUTES
# =========================================================

@app.get("/")
def home():

    return HTMLResponse(HTML_PAGE)


@app.get("/api/live-price")
def live_price():

    return JSONResponse(
        get_live_price()
    )


@app.get("/api/candles")
def candles(
    timeframe: str = Query("1h")
):

    allowed = {
        "1min",
        "5min",
        "15min",
        "30min",
        "1h",
        "4h",
        "1day"
    }

    if timeframe not in allowed:
        timeframe = "1h"

    return JSONResponse(
        get_candles(
            interval=timeframe,
            outputsize=200
        )
    )


@app.get("/api/live-signal")
def live_signal(
    timeframe: str = Query("1h")
):

    allowed = {
        "1min",
        "5min",
        "15min",
        "30min",
        "1h",
        "4h",
        "1day"
    }

    if timeframe not in allowed:
        timeframe = "1h"

    data = get_candles(
        interval=timeframe,
        outputsize=250
    )

    candles_data = data.get("values", [])

    signal = generate_signal(
        candles_data
    )

    signal["timeframe"] = timeframe

    return JSONResponse(signal)


@app.get("/api/news")
def news():

    return JSONResponse(
        get_news()
    )


@app.get("/health")
def health():

    return {
        "status": "ok",
        "service": "Gold AI Live v5",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# =========================================================
# FRONTEND
# =========================================================

HTML_PAGE = r"""
<!DOCTYPE html>
<html>
<head>

<meta name="viewport"
content="width=device-width, initial-scale=1">

<title>Gold AI Live v5</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #07111f;
    color: white;
    font-family: Arial, sans-serif;
}

.header {
    padding: 22px;
    background: #0b1729;
    border-bottom: 1px solid #23334a;
}

.title {
    font-size: 27px;
    font-weight: bold;
}

.subtitle {
    margin-top: 6px;
    color: #8fa4bf;
}

.container {
    padding: 15px;
    max-width: 1200px;
    margin: auto;
}

.timeframes {
    display: grid;
    grid-template-columns:
    repeat(7, 1fr);
    gap: 8px;
    margin-bottom: 15px;
}

button {
    border: 0;
    border-radius: 10px;
    padding: 13px 5px;
    background: #172942;
    color: white;
    font-weight: bold;
}

button.active {
    background: #1769ff;
}

.card {
    background: #0d1b2e;
    border: 1px solid #243852;
    border-radius: 15px;
    padding: 18px;
    margin-bottom: 15px;
}

.price {
    font-size: 38px;
    font-weight: bold;
}

.status {
    color: #61e6a1;
    margin-left: 8px;
}

.signal {
    font-size: 34px;
    font-weight: bold;
    margin-bottom: 12px;
}

.grid {
    display: grid;
    grid-template-columns:
    repeat(2, 1fr);
    gap: 15px;
}

.row {
    display: flex;
    justify-content: space-between;
    padding: 9px 0;
    border-bottom: 1px solid #20324b;
}

.label {
    color: #8fa4bf;
}

.buy {
    color: #45e59a;
}

.sell {
    color: #ff647c;
}

.wait {
    color: #ffc857;
}

.news {
    margin-top: 10px;
}

.news-item {
    padding: 12px 0;
    border-bottom: 1px solid #20324b;
}

.small {
    color: #8095ae;
    font-size: 12px;
}

@media(max-width:700px) {

    .timeframes {
        grid-template-columns:
        repeat(4, 1fr);
    }

    .grid {
        grid-template-columns: 1fr;
    }

}

</style>

</head>

<body>

<div class="header">

    <div class="title">
        🥇 Gold AI Live v5
    </div>

    <div class="subtitle">
        XAU/USD • Live Price • AI Technical Signal • Global News
    </div>

</div>

<div class="container">

    <div class="timeframes">

        <button onclick="changeTF('1min',this)">
            1m
        </button>

        <button onclick="changeTF('5min',this)">
            5m
        </button>

        <button onclick="changeTF('15min',this)">
            15m
        </button>

        <button onclick="changeTF('30min',this)">
            30m
        </button>

        <button class="active"
        onclick="changeTF('1h',this)">
            1H
        </button>

        <button onclick="changeTF('4h',this)">
            4H
        </button>

        <button onclick="changeTF('1day',this)">
            1D
        </button>

    </div>


    <div class="card">

        <div>
            XAU/USD
            <span class="status"
            id="priceStatus">
                LIVE
            </span>
        </div>

        <div class="price"
        id="price">
            Loading...
        </div>

    </div>


    <div class="grid">

        <div class="card">

            <div class="small">
                AI CALL
            </div>

            <div class="signal"
            id="signal">
                LOADING
            </div>

            <div class="row">
                <span class="label">
                    BUY probability
                </span>

                <span id="buy">
                    -
                </span>
            </div>

            <div class="row">
                <span class="label">
                    SELL probability
                </span>

                <span id="sell">
                    -
                </span>
            </div>

            <div class="row">
                <span class="label">
                    Confidence
                </span>

                <span id="confidence">
                    -
                </span>
            </div>

        </div>


        <div class="card">

            <div class="small">
                TECHNICAL DATA
            </div>

            <div class="row">
                <span class="label">
                    EMA20
                </span>

                <span id="ema20">
                    -
                </span>
            </div>

            <div class="row">
                <span class="label">
                    EMA50
                </span>

                <span id="ema50">
                    -
                </span>
            </div>

            <div class="row">
                <span class="label">
                    EMA200
                </span>

                <span id="ema200">
                    -
                </span>
            </div>

            <div class="row">
                <span class="label">
                    RSI
                </span>

                <span id="rsi">
                    -
                </span>
            </div>

            <div class="row">
                <span class="label">
                    MACD
                </span>

                <span id="macd">
                    -
                </span>
            </div>

            <div class="row">
                <span class="label">
                    ATR
                </span>

                <span id="atr">
                    -
                </span>
            </div>

        </div>

    </div>


    <div class="card">

        <div class="small">
            GLOBAL GOLD NEWS
        </div>

        <div class="news"
        id="news">
            Loading news...
        </div>

    </div>

</div>


<script>

let timeframe = "1h";


function number(value, digits=2) {

    if (value === null ||
        value === undefined) {

        return "-";
    }

    return Number(value)
        .toFixed(digits);
}


function changeTF(tf, button) {

    timeframe = tf;

    document
        .querySelectorAll(".timeframes button")
        .forEach(
            b => b.classList.remove("active")
        );

    button.classList.add("active");

    loadAll();
}


async function loadPrice() {

    try {

        const response =
            await fetch(
                "/api/live-price"
            );

        const data =
            await response.json();

        if (data.price !== null &&
            data.price !== undefined) {

            document
                .getElementById("price")
                .innerText =
                number(data.price, 2);

            document
                .getElementById("priceStatus")
                .innerText = "LIVE";

        }

    } catch(e) {

        document
            .getElementById("priceStatus")
            .innerText = "ERROR";
    }

}


async function loadSignal() {

    try {

        const response =
            await fetch(
                "/api/live-signal?timeframe="
                + timeframe
            );

        const data =
            await response.json();


        const signal =
            document.getElementById("signal");

        signal.innerText =
            data.signal || "WAIT";


        signal.className =
            "signal " +
            (
                data.signal === "BUY"
                ? "buy"
                : data.signal === "SELL"
                ? "sell"
                : "wait"
            );


        document
            .getElementById("buy")
            .innerText =
            number(data.buy_probability, 1)
            + "%";


        document
            .getElementById("sell")
            .innerText =
            number(data.sell_probability, 1)
            + "%";


        document
            .getElementById("confidence")
            .innerText =
            number(data.confidence, 1)
            + "%";


        document
            .getElementById("ema20")
            .innerText =
            number(data.ema20);


        document
            .getElementById("ema50")
            .innerText =
            number(data.ema50);


        document
            .getElementById("ema200")
            .innerText =
            number(data.ema200);


        document
            .getElementById("rsi")
            .innerText =
            number(data.rsi, 1);


        document
            .getElementById("macd")
            .innerText =
            number(data.macd, 4);


        document
            .getElementById("atr")
            .innerText =
            number(data.atr, 3);


    } catch(e) {

        document
            .getElementById("signal")
            .innerText =
            "ERROR";

    }

}


async function loadNews() {

    try {

        const response =
            await fetch("/api/news");

        const data =
            await response.json();

        const box =
            document.getElementById("news");

        if (!data.articles ||
            data.articles.length === 0) {

            box.innerHTML =
                "<div class='small'>"
                + "News unavailable"
                + "</div>";

            return;
        }


        box.innerHTML =
            data.articles.map(
                article => {

                    const title =
                        article.title ||
                        "Gold market news";

                    return `
                        <div class="news-item">
                            <div>
                                ${title}
                            </div>
                            <div class="small">
                                ${article.publishedAt || ""}
                            </div>
                        </div>
                    `;

                }
            ).join("");

    } catch(e) {

        document
            .getElementById("news")
            .innerText =
            "News unavailable";

    }

}


function loadAll() {

    loadPrice();
    loadSignal();
    loadNews();

}


loadAll();


// IMPORTANT:
// Do NOT request Twelve Data every second.
// Backend cache handles the API.
// Browser refreshes display only every 15 seconds.

setInterval(
    loadPrice,
    15000
);

setInterval(
    loadSignal,
    60000
);

setInterval(
    loadNews,
    300000
);

</script>

</body>
</html>
"""


# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
def startup_event():

    print("====================================")
    print(" GOLD AI LIVE v5 STARTED")
    print(" Twelve Data cache: 15 sec")
    print(" Candle cache: 60 sec")
    print(" News cache: 5 min")
    print("====================================")
