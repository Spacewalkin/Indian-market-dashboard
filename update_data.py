"""Builds data.json for the India market dashboard.

One price download covers every stock; each segment (cap size or sector) is then
worked out from the same prices. A segment whose NSE list can't be downloaded is
skipped, and the rest still work.
"""
import io, json, os, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
import yfinance as yf

DAYS = 130  # trading days kept in data.json
BASE = "https://nsearchives.nseindia.com/content/indices/"

# key, display name, group, NSE list file
SEGMENTS = [
    ("all", "All Nifty 500", "cap", "ind_nifty500list.csv"),
    ("large", "Large cap", "cap", "ind_nifty100list.csv"),
    ("mid", "Mid cap", "cap", "ind_niftymidcap150list.csv"),
    ("small", "Small cap", "cap", "ind_niftysmallcap250list.csv"),
    ("bank", "Bank", "sector", "ind_niftybanklist.csv"),
    ("pvtbank", "Private bank", "sector", "ind_nifty_privatebanklist.csv"),
    ("psubank", "PSU bank", "sector", "ind_niftypsubanklist.csv"),
    ("finserv", "Financial services", "sector", "ind_niftyfinancelist.csv"),
    ("it", "IT", "sector", "ind_niftyitlist.csv"),
    ("pharma", "Pharma", "sector", "ind_niftypharmalist.csv"),
    ("health", "Healthcare", "sector", "ind_niftyhealthcarelist.csv"),
    ("auto", "Auto", "sector", "ind_niftyautolist.csv"),
    ("fmcg", "FMCG", "sector", "ind_niftyfmcglist.csv"),
    ("metal", "Metal", "sector", "ind_niftymetallist.csv"),
    ("realty", "Realty", "sector", "ind_niftyrealtylist.csv"),
    ("energy", "Energy", "sector", "ind_niftyenergylist.csv"),
    ("oilgas", "Oil & gas", "sector", "ind_niftyoilgaslist.csv"),
    ("media", "Media", "sector", "ind_niftymedialist.csv"),
    ("durables", "Consumer durables", "sector", "ind_niftyconsumerdurableslist.csv"),
    ("infra", "Infrastructure", "sector", "ind_niftyinfralist.csv"),
    ("pse", "PSU", "sector", "ind_niftypselist.csv"),
    ("commod", "Commodities", "sector", "ind_niftycommoditieslist.csv"),
    ("consump", "Consumption", "sector", "ind_niftyconsumptionlist.csv"),
    ("services", "Services", "sector", "ind_niftyservicelist.csv"),
    ("chem", "Chemicals", "sector", "ind_niftychemicalslist.csv"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}


def get_list(fname):
    for _ in range(3):
        try:
            r = requests.get(BASE + fname, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                df = pd.read_csv(io.StringIO(r.text))
                df.columns = [c.strip() for c in df.columns]
                out = {}
                for _, row in df.iterrows():
                    sym = str(row["Symbol"]).strip()
                    if sym and not sym.upper().startswith("DUMMY"):
                        out[sym] = str(row.get("Company Name", sym)).strip()
                return out
            print(f"  {fname}: HTTP {r.status_code}")
        except Exception as e:
            print(f"  {fname}: {e}")
        time.sleep(2)
    return None


# ---------------- news and events ----------------
CAT_RULES = [  # category, tone, keywords checked in this order
    ("Risks and red flags", "neg", ["downgrade", "probe", "fraud", "penalty", "raid", "resigns", "default", "loss widens",
                                    "sebi order", "cuts target", "recall", "warning letter", "delisting", "show cause", "insolvency"]),
    ("Splits, bonus and buybacks", "pos", ["stock split", "share split", "sub-division", "bonus issue", "bonus share", "buyback", "buy back"]),
    ("Orders and deals", "pos", ["bags order", "wins order", "order win", "order inflow", "secures order", "bags contract",
                                 "wins contract", "lowest bidder", "block deal", "bulk deal", "order worth", "orders worth"]),
    ("Investments and expansion", "neu", ["capex", "to invest", "investment of", "acquire", "acquisition", "stake", "expansion",
                                          "new plant", "joint venture", "fundraise", "fund raise", "qip", "preferential"]),
    ("Approvals and upgrades", "pos", ["approval", "usfda", "upgrade", "initiates coverage", "target price"]),
    ("Results and dividends", "neu", ["results", "net profit", "dividend", "earnings"]),
]
CATALYST_QUERIES = ["stock bags order", "wins order worth crore", "stock split record date", "bonus issue record date",
                    "buyback record date", "board approves acquisition OR investment crore", "block deal stake sale",
                    "share price jumps order win", "USFDA approval stock", "brokerage upgrade target price stock"]
DOMESTIC_QUERIES = ["Sensex Nifty stock market today", "India economy RBI inflation", "FII DII flows India markets", "India stocks to watch"]
GLOBAL_TOPICS = {
    "Central banks and rates": ["Federal Reserve interest rates", "ECB OR Bank of Japan interest rates"],
    "Oil and geopolitics": ["crude oil prices OPEC", "geopolitical tensions markets war sanctions"],
    "Wall Street and Asia": ["Wall Street stocks close", "Asian markets Nikkei Hang Seng"],
    "Trade and policy": ["US tariffs trade deal", "China economy stimulus"],
    "Gold and dollar": ["gold price dollar index"],
}


def rss(query, n=12, days=2):
    url = "https://news.google.com/rss/search?q=" + quote(f"{query} when:{days}d") + "&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        root = ET.fromstring(requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=25).content)
    except Exception as e:
        print("News query failed:", query, e)
        return []
    out = []
    for it in root.iter("item"):
        title, src = (it.findtext("title") or "").strip(), (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)]
        try:
            d = datetime.strptime((it.findtext("pubDate") or "")[:25], "%a, %d %b %Y %H:%M:%S").strftime("%Y-%m-%d")
        except Exception:
            d = ""
        if title:
            out.append({"t": title, "p": src, "l": (it.findtext("link") or "").strip(), "d": d})
        if len(out) >= n:
            break
    return out


def uniq(rows, n):
    seen, out = set(), []
    for r in rows:
        k = re.sub(r"\W+", "", r["t"].lower())[:60]
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out[:n]


def norm(s):
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]", " ", s.lower())).strip() + " "


def classify(title):
    tl = title.lower()
    for cat, tone, kws in CAT_RULES:
        if any(k in tl for k in kws):
            return cat, tone
    return None, None


def build_events(names, watch, large):
    ev = {"results": [], "actions": [], "catalysts": [], "dom": [], "glob": {}}
    today = datetime.now().date()
    horizon = today + timedelta(days=30)

    # 1) NSE calendars (NSE sometimes blocks GitHub servers; then this part is skipped)
    try:
        s = requests.Session()
        s.headers.update({**HEADERS, "Accept": "application/json"})
        s.get("https://www.nseindia.com", timeout=20)
        r = s.get("https://www.nseindia.com/api/event-calendar", timeout=25)
        if r.status_code == 200:
            for e in r.json():
                try:
                    sym = str(e.get("symbol", "")).strip()
                    txt = (str(e.get("purpose", "")) + " " + str(e.get("bmDesc", ""))).lower()
                    d = datetime.strptime(e["date"], "%d-%b-%Y").date()
                    if sym in names and "result" in txt and today <= d <= horizon:
                        ev["results"].append({"s": sym, "n": names[sym], "d": d.isoformat(), "src": "NSE"})
                except Exception:
                    continue
        else:
            print("NSE event calendar: HTTP", r.status_code)
        r = s.get("https://www.nseindia.com/api/corporates-corporateActions",
                  params={"index": "equities", "from_date": today.strftime("%d-%m-%Y"),
                          "to_date": horizon.strftime("%d-%m-%Y")}, timeout=25)
        if r.status_code == 200:
            for e in r.json():
                try:
                    sym, subj = str(e.get("symbol", "")).strip(), str(e.get("subject", ""))
                    if sym in names and any(k in subj.lower() for k in ("split", "sub-division", "bonus", "buyback", "buy back", "dividend", "rights")):
                        d = datetime.strptime(e.get("exDate") or e.get("recDate"), "%d-%b-%Y").date()
                        ev["actions"].append({"s": sym, "n": names[sym], "what": subj, "d": d.isoformat()})
                except Exception:
                    continue
        else:
            print("NSE corporate actions: HTTP", r.status_code)
    except Exception as e:
        print("NSE calendars skipped:", e)

    # 2) Fallback for results dates: Yahoo calendar for the large caps and swing candidates
    if not ev["results"]:
        t0 = time.time()
        for sym in list(dict.fromkeys(list(watch) + list(large)))[:180]:
            if time.time() - t0 > 150:
                break
            try:
                cal = yf.Ticker(sym + ".NS").calendar
                for d in (cal.get("Earnings Date") or []) if isinstance(cal, dict) else []:
                    if today <= d <= horizon:
                        ev["results"].append({"s": sym, "n": names.get(sym, sym), "d": d.isoformat(), "src": "Yahoo"})
                        break
            except Exception:
                pass
    ev["results"].sort(key=lambda x: x["d"])
    ev["actions"].sort(key=lambda x: x["d"])
    ev["actions"] = ev["actions"][:60]

    # 3) Headlines: stock catalysts, domestic news, global news
    idx = sorted(((norm(re.sub(r"\b(ltd|limited)\b\.?", "", n, flags=re.I)), sym, n) for sym, n in names.items()),
                 key=lambda x: -len(x[0]))
    idx = [x for x in idx if len(x[0].strip()) >= 5]
    rows = []
    for q in CATALYST_QUERIES:
        rows += rss(q, 15, days=3)
    cats = []
    for r in uniq(rows, 200):
        cat, tone = classify(r["t"])
        if not cat:
            continue
        nt = norm(r["t"])
        hit = next(((sym, n) for c, sym, n in idx if c in nt), ("", ""))
        cats.append({**r, "cat": cat, "tone": tone, "s": hit[0], "n": hit[1]})
    cats.sort(key=lambda x: x["d"], reverse=True)
    cats.sort(key=lambda x: x["s"] == "")  # headlines matched to a stock first
    ev["catalysts"] = cats[:45]
    ev["dom"] = uniq([r for q in DOMESTIC_QUERIES for r in rss(q, 10)], 14)
    for topic, qs in GLOBAL_TOPICS.items():
        ev["glob"][topic] = uniq([r for q in qs for r in rss(q, 8)], 5)
    return ev


COUNTRIES = [  # flag, name, Yahoo ticker
    ("🇺🇸", "United States", "^GSPC"), ("🇰🇷", "South Korea", "^KS11"), ("🇮🇩", "Indonesia", "^JKSE"),
    ("🇹🇷", "Turkey", "XU100.IS"), ("🇨🇳", "China", "000001.SS"), ("🇭🇰", "Hong Kong", "^HSI"),
    ("🇮🇳", "India", "^NSEI"), ("🇯🇵", "Japan", "^N225"), ("🇹🇼", "Taiwan", "^TWII"),
    ("🇩🇪", "Germany", "^GDAXI"), ("🇬🇧", "United Kingdom", "^FTSE"), ("🇫🇷", "France", "^FCHI"),
    ("🇧🇷", "Brazil", "^BVSP"), ("🇦🇺", "Australia", "^AXJO"), ("🇸🇬", "Singapore", "^STI"),
    ("🇸🇦", "Saudi Arabia", "^TASI.SR"),
]


def fetch_countries():
    """How far each country's main index is below its highest close this year."""
    try:
        px = yf.download([c[2] for c in COUNTRIES], period="1y", auto_adjust=True, progress=False, threads=True)["Close"]
    except Exception as e:
        print("Country download failed:", e)
        return []
    yr, rows = datetime.now().year, []
    for f, n, t in COUNTRIES:
        if t not in px.columns:
            print("Country skipped:", n)
            continue
        x = px[t].dropna()
        this_year = x[x.index.year == yr]
        if len(x) < 23 or len(this_year) < 2:
            continue
        v = float(x.iloc[-1])
        rows.append({"f": f, "n": n, "t": t, "d": round((v / x.iloc[-2] - 1) * 100, 2),
                     "m": round((v / x.iloc[-22] - 1) * 100, 1),
                     "ytd": round((v / float(this_year.iloc[0]) - 1) * 100, 1),
                     "dd": round((v / float(this_year.max()) - 1) * 100, 1)})
    return rows


def fetch_fiidii(old):
    """Daily FII/FPI and DII cash-market buying and selling (rupees crore), kept as a running history."""
    hist = list((old or {}).get("hist", []))
    fresh = False
    try:
        s = requests.Session()
        s.headers.update({**HEADERS, "Accept": "application/json"})
        s.get("https://www.nseindia.com", timeout=20)
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=25)
        if r.status_code == 200:
            row = {}
            for e in r.json():
                cat = str(e.get("category", "")).upper()
                key = "f" if ("FII" in cat or "FPI" in cat) else "d" if "DII" in cat else None
                if not key:
                    continue
                num = lambda k: float(str(e.get(k, "0")).replace(",", "") or 0)
                row[key + "b"], row[key + "s"], row[key + "n"] = num("buyValue"), num("sellValue"), num("netValue")
                row["d"] = datetime.strptime(e["date"], "%d-%b-%Y").date().isoformat()
            if all(k in row for k in ("fb", "db", "d")):
                hist = [h for h in hist if h["d"] != row["d"]] + [row]
                fresh = True
        else:
            print("NSE FII/DII: HTTP", r.status_code)
    except Exception as e:
        print("NSE FII/DII skipped:", e)
    hist = sorted(hist, key=lambda h: h["d"])[-60:]
    return {"latest": hist[-1] if hist else None, "hist": hist, "fresh": fresh,
            "news": uniq(rss("FII DII provisional data net sold bought crore", 8), 5)}


def ist_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)


def gap_preview(macro, fiidii):
    """Rough read of how Indian stocks may open, from overnight global moves."""
    m = {x["k"]: x for x in macro}
    spec = [("sp500", "US stocks (S&P 500)", 0.5), ("nasdaq", "US tech (Nasdaq)", 0.3),
            ("nikkei", "Japan (Nikkei)", 0.2), ("hangseng", "Hong Kong (Hang Seng)", 0.2),
            ("brent", "Brent crude (higher is negative)", -0.3), ("usdinr", "USD/INR (higher means weaker rupee)", -1.0),
            ("dxy", "Dollar index (higher is negative)", -0.5)]
    pts, rows = 0.0, []
    for k, name, w in spec:
        if k in m:
            p = m[k]["d"] * w
            pts += p
            rows.append({"n": name, "v": m[k]["d"], "p": round(p, 2)})
    last = (fiidii or {}).get("latest")
    if last:
        p = max(-1, min(1, last["fn"] / 4000)) * 0.5
        pts += p
        rows.append({"n": "FII net in the last session", "v": last["fn"], "p": round(p, 2), "cr": True})
    key = ("gapup2" if pts >= 0.7 else "gapup1" if pts >= 0.25 else "flat" if pts > -0.25
           else "gapdown1" if pts > -0.7 else "gapdown2")
    now = ist_now()
    return {"date": now.date().isoformat(), "time": now.strftime("%I:%M %p").lstrip("0"),
            "score": round(pts, 2), "key": key, "rows": rows}


def preopen_run(old):
    """Light early-morning run: refresh global cues, headlines and the gap preview only."""
    old["macro"] = fetch_macro() or old.get("macro", [])
    old["countries"] = fetch_countries() or old.get("countries", [])
    ev = old.get("events") or {}
    ev["dom"] = uniq([r for q in DOMESTIC_QUERIES for r in rss(q, 10)], 14)
    ev["glob"] = {t: uniq([r for q in qs for r in rss(q, 8)], 5) for t, qs in GLOBAL_TOPICS.items()}
    old["events"] = ev
    old["preopen"] = gap_preview(old["macro"], old.get("fiidii"))
    old["updated"] = ist_now().strftime("%d %b, %I:%M %p IST").lstrip("0").replace(", 0", ", ")
    old["mode"] = "preopen"
    with open("data.json", "w") as f:
        json.dump(old, f, separators=(",", ":"))
    print("Pre-open preview:", old["preopen"]["key"], old["preopen"]["score"])


MIN_TURNOVER_CR = 5  # skip thinly traded stocks (average daily value in crore rupees)

MACRO = [  # key, name, Yahoo ticker, group
    ("nifty", "Nifty 50", "^NSEI", "India"), ("sensex", "Sensex", "^BSESN", "India"),
    ("sp500", "S&P 500", "^GSPC", "Global markets"), ("nasdaq", "Nasdaq", "^IXIC", "Global markets"),
    ("nikkei", "Nikkei 225", "^N225", "Global markets"), ("hangseng", "Hang Seng", "^HSI", "Global markets"),
    ("btc", "Bitcoin", "BTC-USD", "Global markets"),
    ("gold", "Gold", "GC=F", "Commodities"), ("silver", "Silver", "SI=F", "Commodities"),
    ("brent", "Brent crude", "BZ=F", "Commodities"), ("wti", "WTI crude", "CL=F", "Commodities"),
    ("natgas", "Natural gas", "NG=F", "Commodities"), ("copper", "Copper", "HG=F", "Commodities"),
    ("usdinr", "USD/INR", "INR=X", "Currency and bonds"), ("dxy", "Dollar index", "DX-Y.NYB", "Currency and bonds"),
    ("us10y", "US 10-year yield", "^TNX", "Currency and bonds"),
    ("vix", "India VIX", "^INDIAVIX", "Volatility"), ("usvix", "US VIX", "^VIX", "Volatility"),
]


def fetch_macro():
    try:
        px = yf.download([m[2] for m in MACRO], period="6mo", auto_adjust=True,
                         progress=False, threads=True)["Close"]
    except Exception as e:
        print("Macro download failed:", e)
        return []
    rows = []
    for k, n, t, g in MACRO:
        if t not in px.columns:
            print("Macro skipped:", n)
            continue
        x = px[t].dropna()
        if len(x) < 23:
            print("Macro skipped (too little data):", n)
            continue
        if k == "us10y" and x.iloc[-1] > 20:
            x = x / 10
        v = float(x.iloc[-1])
        rows.append({"k": k, "n": n, "g": g, "v": round(v, 2),
                     "d": round((v / x.iloc[-2] - 1) * 100, 2),
                     "w": round((v / x.iloc[-6] - 1) * 100, 2),
                     "m": round((v / x.iloc[-22] - 1) * 100, 2),
                     "a": round(v - float(x.iloc[-22]), 2)})
    return rows


def swing_candidates(close, high, low, vol, stocks, lists):
    """Stocks near their 52-week high or with a fresh volume breakout, in an uptrend."""
    sector_of = {}
    for key, name, group, _ in SEGMENTS:
        if group == "sector" and key in lists:
            for sym in lists[key]:
                sector_of.setdefault(sym, name)
    pc = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - pc).abs(), (low - pc).abs()))
    atr = tr.rolling(14).mean().iloc[-1]
    avgv = vol.shift(1).rolling(20).mean()
    vx_s = (vol / avgv).replace([np.inf, -np.inf], np.nan)
    ph = close.shift(1).rolling(20).max()
    brk = ((close > ph) & (vx_s >= 1.5)).iloc[-3:].any()  # breakout in the last 3 sessions
    vx = vx_s.iloc[-3:].max()
    hi52 = high.max()
    last = close.iloc[-1]
    turn = avgv.iloc[-1] * last / 1e7
    out = []
    for sym, st in stocks.items():
        try:
            if not (st["e"][0] and st["e"][1]):
                continue
            if pd.isna(turn[sym]) or turn[sym] < MIN_TURNOVER_CR or pd.isna(atr[sym]):
                continue
            d52 = (last[sym] / hi52[sym] - 1) * 100
            near, b = bool(d52 >= -5), bool(brk[sym])
            if not (near or b):
                continue
            v = float(vx[sym]) if pd.notna(vx[sym]) else 0.0
            stop = float(last[sym] - 1.5 * atr[sym])
            score = (3 if b else 0) + (2 if near else 0) + (1 if v >= 1.5 else 0) + (1 if st["d"] > 0 else 0)
            out.append({"s": sym, "n": st["n"], "sec": sector_of.get(sym, ""), "p": st["p"],
                        "d": st["d"], "m": st["m"], "d52": round(float(d52), 1), "vx": round(v, 1),
                        "brk": b, "near": near, "stop": round(stop, 2),
                        "risk": round((st["p"] - stop) / st["p"] * 100, 1), "score": score})
        except Exception:
            continue
    out.sort(key=lambda c: (-c["score"], -c["vx"]))
    return out[:60]


def parse_news(it):
    c = it.get("content") or it
    url = (c.get("canonicalUrl") or {}).get("url") or c.get("link") or it.get("link") or ""
    return {"t": c.get("title") or "", "p": (c.get("provider") or {}).get("displayName") or it.get("publisher") or "",
            "l": url, "d": str(c.get("pubDate") or "")[:10]}


def fetch_news(syms, limit_s=240):
    out, t0 = {}, time.time()
    for sym in syms:
        if time.time() - t0 > limit_s:
            print("News time limit reached")
            break
        try:
            rows = [parse_news(i) for i in (yf.Ticker(sym + ".NS").news or [])[:3]]
            rows = [r for r in rows if r["t"]][:2]
            if rows:
                out[sym] = rows
        except Exception:
            pass
    return out


def main():
    try:
        old = json.load(open("data.json"))
    except Exception:
        old = {}
    sched = os.environ.get("RUN_SCHEDULE", "")
    mode = "preopen" if sched == "15 3 * * 1-5" else "midday" if sched == "30 7 * * 1-5" else "full"
    if mode == "preopen" and old.get("segments"):
        return preopen_run(old)
    lists, names = {}, {}
    for key, name, group, fname in SEGMENTS:
        got = get_list(fname)
        if not got:
            print(f"SKIPPING segment {name}: list not available")
            continue
        lists[key] = list(got)
        names.update(got)
        print(f"{name}: {len(got)} stocks")

    if "all" not in lists:
        sys.exit("Nifty 500 list failed to download - keeping the old data.json")

    symbols = sorted(names)
    raw = yf.download([s + ".NS" for s in symbols], period="1y",
                      auto_adjust=True, progress=False, threads=True)
    close = raw["Close"]
    close.columns = [c[:-3] if str(c).endswith(".NS") else c for c in close.columns]
    close = close.dropna(axis=1, how="all").dropna(axis=0, how="all").ffill(limit=3)
    def aligned(name):
        d = raw[name].copy()
        d.columns = [c[:-3] if str(c).endswith(".NS") else c for c in d.columns]
        return d.reindex(index=close.index, columns=close.columns)
    high, low, vol = aligned("High"), aligned("Low"), aligned("Volume")
    if len(close) < DAYS:
        sys.exit("Not enough price history downloaded - keeping the old data.json")

    emas = {n: close.ewm(span=n, adjust=False, min_periods=n).mean() for n in (20, 50, 200)}
    valid = {n: emas[n].notna() for n in emas}
    above = {n: (close > emas[n]) & valid[n] for n in emas}
    chg = close.pct_change()
    idx = close.index[-DAYS:]

    nifty = None
    try:
        nf = yf.download("^NSEI", period="1y", auto_adjust=True, progress=False)["Close"]
        if isinstance(nf, pd.DataFrame):
            nf = nf.iloc[:, 0]
        nifty = nf.reindex(close.index).ffill().loc[idx].round(2).tolist()
    except Exception as e:
        print("Nifty 50 download failed:", e)

    # latest-day facts for every stock
    last = close.iloc[-1]
    m1 = close.iloc[-1] / close.iloc[-22] - 1
    stocks = {}
    for s in close.columns:
        if pd.isna(last[s]):
            continue
        stocks[s] = {
            "n": names.get(s, s),
            "p": round(float(last[s]), 2),
            "d": round(float(chg[s].iloc[-1] * 100), 2) if pd.notna(chg[s].iloc[-1]) else 0,
            "m": round(float(m1[s] * 100), 1) if pd.notna(m1[s]) else 0,
            "e": [int(above[n][s].iloc[-1]) for n in (20, 50, 200)],
        }

    segs = []
    for key, name, group, _ in SEGMENTS:
        if key not in lists:
            continue
        cols = [s for s in lists[key] if s in close.columns]
        if len(cols) < 5:
            print(f"SKIPPING segment {name}: only {len(cols)} stocks have prices")
            continue
        seg = {"key": key, "name": name, "group": group, "n": len(cols), "stocks": cols}
        for n in (20, 50, 200):
            v = valid[n][cols].sum(axis=1).replace(0, np.nan)
            seg[f"p{n}"] = (above[n][cols].sum(axis=1) / v * 100).reindex(idx).fillna(0).round(1).tolist()
        seg["adv"] = (chg[cols] > 0).sum(axis=1).reindex(idx).fillna(0).astype(int).tolist()
        seg["dec"] = (chg[cols] < 0).sum(axis=1).reindex(idx).fillna(0).astype(int).tolist()
        if key == "all" and nifty:
            seg["line"], seg["real"] = nifty, True
        else:
            px = close[cols].loc[idx]
            norm = px / px.bfill().iloc[0]
            seg["line"], seg["real"] = (norm.mean(axis=1) * 100).round(2).tolist(), False
        segs.append(seg)

    swing = swing_candidates(close, high, low, vol, stocks, lists)
    wanted = [c["s"] for c in swing[:15]]
    for seg in segs:
        mv = sorted([x for x in seg["stocks"] if x in stocks], key=lambda x: stocks[x]["d"])
        wanted += mv[:2] + mv[-2:]
    wanted = list(dict.fromkeys(wanted))[:160]
    news = old.get("news", {}) if mode == "midday" else fetch_news(wanted)
    macro = fetch_macro()
    countries = fetch_countries()
    fiidii = old["fiidii"] if mode == "midday" and old.get("fiidii") else fetch_fiidii(old.get("fiidii"))
    events = old["events"] if mode == "midday" and old.get("events") else build_events(names, wanted[:15], lists.get("large", []))
    print(f"{len(swing)} swing setups, news for {len(news)} stocks, {len(macro)} market factors, "
          f"{len(events['results'])} upcoming results, {len(events['catalysts'])} stock catalysts")

    out = {
        "asof": idx[-1].strftime("%d %b %Y"),
        "dates": [d.strftime("%Y-%m-%d") for d in idx],
        "segments": segs,
        "stocks": stocks,
        "swing": swing,
        "news": news,
        "macro": macro,
        "events": events,
        "countries": countries,
        "fiidii": fiidii,
        "preopen": old.get("preopen"),
        "updated": ist_now().strftime("%d %b, %I:%M %p IST").lstrip("0").replace(", 0", ", "),
        "mode": mode,
    }
    with open("data.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Saved {DAYS} days for {', '.join(s['key'] for s in segs)} - latest: {out['asof']}")


if __name__ == "__main__":
    main()
