"""Builds data.json for the India market dashboard.

One price download covers every stock; each segment (cap size or sector) is then
worked out from the same prices. A segment whose NSE list can't be downloaded is
skipped, and the rest still work.
"""
import io, json, sys, time
from datetime import datetime

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
    news = fetch_news(wanted)
    macro = fetch_macro()
    print(f"{len(swing)} swing setups, news for {len(news)} stocks, {len(macro)} market factors")

    out = {
        "asof": idx[-1].strftime("%d %b %Y"),
        "dates": [d.strftime("%Y-%m-%d") for d in idx],
        "segments": segs,
        "stocks": stocks,
        "swing": swing,
        "news": news,
        "macro": macro,
    }
    with open("data.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Saved {DAYS} days for {', '.join(s['key'] for s in segs)} - latest: {out['asof']}")


if __name__ == "__main__":
    main()
