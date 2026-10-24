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

    out = {
        "asof": idx[-1].strftime("%d %b %Y"),
        "dates": [d.strftime("%Y-%m-%d") for d in idx],
        "segments": segs,
        "stocks": stocks,
    }
    with open("data.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Saved {DAYS} days for {', '.join(s['key'] for s in segs)} - latest: {out['asof']}")


if __name__ == "__main__":
    main()
