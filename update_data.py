"""Builds data.json for the India market dashboard.

One price download covers every stock; each segment (cap size or sector) is then
worked out from the same prices. A segment whose NSE list can't be downloaded is
skipped, and the rest still work.
"""
import io, json, math, os, re, sys, time
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


def fetch_headlines(names):
    """Stock catalyst, domestic and global headlines (fast, no NSE calls)."""
    ev = {"glob": {}}
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
    ev["ts"] = int(time.time())
    return ev


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

    ev.update(fetch_headlines(names))
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


def clean(o):
    """Replace NaN/infinity with null so data.json is always valid JSON."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


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
    old["ts"] = int(time.time())
    with open("data.json", "w") as f:
        json.dump(clean(old), f, separators=(",", ":"))
    print("Pre-open preview:", old["preopen"]["key"], old["preopen"]["score"])


def clamp(v, a, b):
    return max(a, min(b, v))


def sector_map(lists):
    out = {}
    for key, name, group, _ in SEGMENTS:
        if group == "sector" and key in lists:
            for sym in lists[key]:
                out.setdefault(sym, name)
    return out


BULL_WORDS = ("rally", "surge", "jump", "gain", "record high", "rebound", "soar", "rise")
BEAR_WORDS = ("fall", "slump", "plunge", "crash", "sell-off", "selloff", "tumble", "drop", "slide", "fear", "sink")


def mood_index(nf, segs, macro, ev):
    """Market Mood Index (0-100) from Nifty trend, breadth, volatility, sectors and headline tone."""
    if nf is None:
        return None
    x = nf.dropna()
    al = next((g for g in segs if g["key"] == "all"), None)
    if len(x) < 210 or not al:
        return None
    c = float(x.iloc[-1])
    ema = lambda n: x.ewm(span=n, adjust=False, min_periods=n).mean()
    e = {n: float(ema(n).iloc[-1]) for n in (20, 50, 200)}
    slope50 = e[50] / float(ema(50).iloc[-11]) - 1
    mom = c / float(x.iloc[-22]) - 1
    evid, missing = [], []
    note = lambda t, d: evid.append({"t": t, "d": d})

    # 1) price trend, 35 points
    above = [n for n in (20, 50, 200) if c > e[n]]
    tp = 10 * len(above) + 5 * clamp((mom * 100 + 5) / 10, 0, 1)
    if len(above) == 3:
        note("Nifty 50 is above its 20, 50 and 200 EMA", 1)
    elif not above:
        note("Nifty 50 is below its 20, 50 and 200 EMA", -1)
    else:
        note("Nifty 50 is above its " + " and ".join(map(str, above)) + " EMA but below the others", 1 if len(above) == 2 else -1)
    note(f"Nifty 50 moved {mom * 100:+.1f}% over the last month", 1 if mom > 0 else -1)

    # 2) breadth, 30 points
    p20, p50, p200 = al["p20"][-1], al["p50"][-1], al["p200"][-1]
    adv, dec = al["adv"][-1], al["dec"][-1]
    ad = adv / (adv + dec) * 100 if adv + dec else 50
    bp = 30 * (0.25 * p20 + 0.35 * p50 + 0.25 * p200 + 0.15 * ad) / 100
    note(f"{p50:.0f}% of Nifty 500 stocks are above the 50 EMA and {p200:.0f}% above the 200 EMA", 1 if p50 >= 50 else -1)
    note(f"{adv} stocks rose and {dec} fell today", 1 if adv > dec else -1)
    n5 = c / float(x.iloc[-6]) - 1
    if n5 > 0.003 and (p50 < 40 or ad < 40):
        note(f"Weak participation: Nifty is up {n5 * 100:.1f}% over 5 days, but most stocks are not following", -1)
    elif n5 < -0.003 and p50 >= 55:
        note(f"Nifty is down {abs(n5) * 100:.1f}% over 5 days, but most stocks are holding up", 1)

    # 3) volatility and risk, 15 points
    vix = next((m["v"] for m in macro if m["k"] == "vix"), None)
    if vix is None:
        vix = float(x.pct_change().tail(20).std() * (252 ** 0.5) * 100)
        missing.append("India VIX (realised volatility used instead)")
    vp = 15 if vix <= 13 else 12 if vix <= 16 else 8 if vix <= 20 else 4 if vix <= 25 else 0
    note(f"Volatility reading is {vix:.1f}: " + ("calm" if vix <= 16 else "normal" if vix <= 20 else "nervous" if vix <= 25 else "fearful"),
         1 if vix <= 16 else -1 if vix > 20 else 0)

    # 4) sector strength, 15 points
    secs = [g for g in segs if g["group"] == "sector"]
    if secs:
        a = sum(g["p50"][-1] >= 50 for g in secs) / len(secs)
        pos = sum(g["line"][-1] / g["line"][-22] > 1 for g in secs)
        sp = 15 * (0.5 * a + 0.5 * pos / len(secs))
        note(f"{pos} of {len(secs)} sectors are positive over the last month", 1 if pos / len(secs) >= 0.5 else -1)
    else:
        sp = 7.5
        missing.append("sector data")

    # 5) headline tone, 5 points (keyword based, unconfirmed)
    titles = [r["t"].lower() for r in (ev or {}).get("dom", [])]
    if titles:
        po = sum(any(w in t for w in BULL_WORDS) for t in titles)
        ne = sum(any(w in t for w in BEAR_WORDS) for t in titles)
        npts = 5 * (po + 1) / (po + ne + 2)
        note(f"Headline tone is {'mostly positive' if po > ne else 'mostly negative' if ne > po else 'mixed'} "
             f"({po} positive, {ne} negative; keyword based, unconfirmed)", 1 if po > ne else -1 if ne > po else 0)
    else:
        npts = 2.5
        missing.append("headlines")

    parts = [{"n": "Price trend", "pts": round(tp, 1), "max": 35}, {"n": "Market breadth", "pts": round(bp, 1), "max": 30},
             {"n": "Volatility and risk", "pts": round(vp, 1), "max": 15}, {"n": "Sector strength", "pts": round(sp, 1), "max": 15},
             {"n": "News tone", "pts": round(npts, 1), "max": 5}]
    score = int(round(sum(p["pts"] for p in parts)))
    fr = [tp / 35, bp / 30, sp / 15]
    bulls, bears = sum(f > 0.6 for f in fr), sum(f < 0.4 for f in fr)
    mixed = bulls > 0 and bears > 0
    conf = "Low" if mixed else ("High" if (bulls == 3 or bears == 3) and not missing else "Medium")
    label = "Bearish" if score < 30 else "Weak" if score < 45 else "Neutral" if score < 55 else "Positive" if score < 70 else "Bullish"

    if c > e[50] and e[20] > e[50] > e[200]:
        regime, rnote = "Trending up", "Nifty is above a rising stack of EMAs. Pullback buys and breakouts have the best odds."
    elif c < e[50] and e[20] < e[50] < e[200]:
        regime, rnote = "Trending down", "Nifty is below falling EMAs. Rallies tend to fail, so stay mostly in cash."
    elif abs(c / e[50] - 1) < 0.03 and abs(slope50) < 0.01:
        regime, rnote = "Ranging", "Nifty is moving sideways around its 50 EMA. Breakouts often fail, so trade smaller and take profits early."
    else:
        regime, rnote = "Uncertain", "The trend is in transition. Wait for Nifty to reclaim or lose its 50 EMA before committing."

    checks = [{"t": "Nifty is in a confirmed uptrend (above stacked, rising EMAs)", "ok": regime == "Trending up"},
              {"t": "Market Mood Index is 55 or higher", "ok": score >= 55},
              {"t": "Signals agree with each other (not mixed)", "ok": not mixed},
              {"t": "Volatility is below 20", "ok": vix < 20}]
    if regime == "Trending down" or score < 35 or vix >= 25:
        decision = "NO TRADE"
    elif all(k["ok"] for k in checks):
        decision = "TRADE"
    else:
        decision = "WAIT"
    return {"score": score, "label": label, "conf": conf, "mixed": mixed, "regime": regime, "regime_note": rnote,
            "dec": decision, "checks": checks, "evidence": evid, "parts": parts, "missing": missing,
            "lv": {"c": round(c, 2), "e20": round(e[20], 2), "e50": round(e[50], 2), "e200": round(e[200], 2)}}


def scan_more(close, high, low, vol, emas, nf, stocks, sector_of):
    """EMA pullbacks, breakout retests, relative strength, volume surges and weak stocks."""
    pc = close.shift(1)
    avgv = vol.shift(1).rolling(20).mean()
    vx_s = (vol / avgv).replace([np.inf, -np.inf], np.nan)
    last = close.iloc[-1]
    turn = avgv.iloc[-1] * last / 1e7
    nret = float(nf.iloc[-1] / nf.iloc[-64] - 1) if nf is not None and len(nf) > 64 else 0.0
    rs = (close.iloc[-1] / close.iloc[-64] - 1 - nret) * 100
    rank = rs.rank(pct=True) * 100
    e20, e50, e200 = emas[20], emas[50], emas[200]
    ph = close.shift(1).rolling(20).max()
    brk_e = (close > ph) & (vx_s >= 1.3)
    out = {k: [] for k in ("pull", "retest", "rs", "vol", "weak")}
    for sym, st in stocks.items():
        try:
            if pd.isna(turn[sym]) or turn[sym] < MIN_TURNOVER_CR or pd.isna(rs[sym]):
                continue
            c, prev = float(last[sym]), float(pc[sym].iloc[-1])
            ht, lt = float(high[sym].iloc[-1]), float(low[sym].iloc[-1])
            v = float(vx_s[sym].iloc[-1]) if pd.notna(vx_s[sym].iloc[-1]) else 0.0
            base = {"s": sym, "n": st["n"], "sec": sector_of.get(sym, ""), "p": st["p"], "d": st["d"], "m": st["m"],
                    "rs": round(float(rs[sym]), 1), "vx": round(v, 1), "ht": round(ht, 2)}
            a50, a200 = float(e50[sym].iloc[-1]), float(e200[sym].iloc[-1])
            if c > a50 > a200 and a50 > float(e50[sym].iloc[-6]):  # uptrend: pullback to the 20 or 50 EMA that held
                lo3 = float(low[sym].iloc[-3:].min())
                for n, e in ((20, e20), (50, e50)):
                    ev = float(e[sym].iloc[-1])
                    if lo3 <= ev * 1.01 and c > ev and c > prev and c >= (ht + lt) / 2:
                        stop = round(min(lo3, ev) * 0.99, 2)
                        out["pull"].append({**base, "ema": n, "lvl": round(ev, 2), "stop": stop})
                        break
            b = brk_e[sym].iloc[-12:-2]  # breakout 3-12 sessions ago, now retesting the level
            if b.any():
                lvl = float(ph[sym].loc[b[b].index[-1]])
                if c > lvl and float(low[sym].iloc[-3:].min()) <= lvl * 1.02 and c > prev and c <= lvl * 1.08:
                    out["retest"].append({**base, "lvl": round(lvl, 2), "stop": round(lvl * 0.97, 2)})
            if rank[sym] >= 80 and c > a50 and c > a200 and st["m"] > 0:
                out["rs"].append({**base, "rank": int(rank[sym])})
            if v >= 2 and abs(st["d"]) >= 3:
                out["vol"].append(base)
            if c < a50 and c < a200 and rs[sym] < 0 and st["m"] < 0:
                out["weak"].append(base)
        except Exception:
            continue
    out["pull"].sort(key=lambda r: -r["rs"])
    out["retest"].sort(key=lambda r: -r["rs"])
    out["rs"].sort(key=lambda r: -r["rank"])
    out["vol"].sort(key=lambda r: -r["vx"])
    out["weak"].sort(key=lambda r: r["rs"])
    return {k: v[:25] for k, v in out.items()}


def build_prices(close, opn, high, low, vol, emas, syms, n=90):
    """Compact candle, volume and EMA history for the stocks that get a chart (prices in paise, volume in thousands)."""
    idx = close.index[-n:]
    px = lambda col: [None if pd.isna(v) else int(round(float(v) * 100)) for v in col.loc[idx]]
    out = {}
    for sym in syms:
        if sym not in close.columns:
            continue
        out[sym] = {"o": px(opn[sym]), "h": px(high[sym]), "l": px(low[sym]), "c": px(close[sym]),
                    "v": [None if pd.isna(v) else int(v / 1000) for v in vol[sym].loc[idx]],
                    "a": px(emas[20][sym]), "b": px(emas[50][sym]), "d": px(emas[200][sym])}
    return {"ts": int(time.time()), "dates": [d.strftime("%Y-%m-%d") for d in idx], "s": out}


MIN_TURNOVER_CR = 5  # skip thinly traded stocks (average daily value in crore rupees)

MACRO = [  # key, name, Yahoo ticker, group
    ("nifty", "Nifty 50", "^NSEI", "India"), ("sensex", "Sensex", "^BSESN", "India"), ("banknifty", "Bank Nifty", "^NSEBANK", "India"),
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
    bl = close.shift(3).rolling(20).max().iloc[-1]
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
                        "bl": round(float(bl[sym]), 2) if pd.notna(bl[sym]) else None,
                        "h52": round(float(hi52[sym]), 2), "ht": round(float(high.iloc[-1][sym]), 2),
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
    mode = ("preopen" if sched == "15 3 * * 1-5" else "midday" if sched == "30 7 * * 1-5"
            else "live" if sched == "*/5 3-10 * * 1-5" else "full")
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
    opn, high, low, vol = aligned("Open"), aligned("High"), aligned("Low"), aligned("Volume")
    if len(close) < DAYS:
        sys.exit("Not enough price history downloaded - keeping the old data.json")

    if close.shape[1] < 0.7 * len(symbols):
        sys.exit("Too many stocks failed to download (Yahoo may be limiting requests) - keeping the old data.json")

    emas = {n: close.ewm(span=n, adjust=False, min_periods=n).mean() for n in (20, 50, 200)}
    valid = {n: emas[n].notna() for n in emas}
    above = {n: (close > emas[n]) & valid[n] for n in emas}
    chg = close.pct_change()
    idx = close.index[-DAYS:]

    nifty, nifty_full = None, None
    try:
        nf = yf.download("^NSEI", period="1y", auto_adjust=True, progress=False)["Close"]
        if isinstance(nf, pd.DataFrame):
            nf = nf.iloc[:, 0]
        nifty_full = nf.reindex(close.index).ffill()
        nifty = nifty_full.loc[idx].round(2).tolist()
    except Exception as e:
        print("Nifty 50 download failed:", e)

    hi52, lo52 = high.max(), low.min()
    r2 = lambda v: round(float(v), 2) if pd.notna(v) else None
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
            "pc": r2(close[s].iloc[-2]),
            "h": r2(hi52[s]),
            "l": r2(lo52[s]),
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
    news = old.get("news", {}) if mode in ("midday", "live") else fetch_news(wanted)
    macro = fetch_macro()
    countries = fetch_countries()
    fiidii = old["fiidii"] if mode in ("midday", "live") and old.get("fiidii") else fetch_fiidii(old.get("fiidii"))
    events = old["events"] if mode in ("midday", "live") and old.get("events") else build_events(names, wanted[:15], lists.get("large", []))
    if mode == "live":
        events.update(fetch_headlines(names))
    scans = scan_more(close, high, low, vol, emas, nifty_full, stocks, sector_map(lists))
    mood = mood_index(nifty_full, segs, macro, events)
    mood_hist = [h for h in old.get("mood_hist", []) if h["d"] != idx[-1].date().isoformat()]
    if mood and mode != "midday":
        mood_hist.append({"d": idx[-1].date().isoformat(), "s": mood["score"], "r": mood["regime"], "k": mood["dec"]})
    mood_hist = mood_hist[-250:]
    ranked = sorted(stocks, key=lambda k: stocks[k]["d"])
    focus = list(dict.fromkeys([c["s"] for c in swing] + [c["s"] for v in scans.values() for c in v]
                               + [x["s"] for x in events.get("results", [])] + ranked[:20] + ranked[-20:]
                               + lists.get("large", [])))[:260]
    with open("prices.json", "w") as f:
        json.dump(build_prices(close, opn, high, low, vol, emas, focus), f, separators=(",", ":"))
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
        "ts": int(time.time()),
        "mood": mood,
        "mood_hist": mood_hist,
        "scans": scans,
    }
    with open("data.json", "w") as f:
        json.dump(clean(out), f, separators=(",", ":"))
    print(f"Saved {DAYS} days for {', '.join(s['key'] for s in segs)} - latest: {out['asof']}")


if __name__ == "__main__":
    main()
