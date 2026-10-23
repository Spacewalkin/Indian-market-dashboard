"""Downloads Nifty 500 prices, works out market breadth for each segment, saves data.json."""
import io
import json

import pandas as pd
import requests
import yfinance as yf

BASE = "https://archives.nseindia.com/content/indices/"
# key, short name, full name, NSE list file
SEGMENTS = [
    ("all", "All", "All Nifty 500", "ind_nifty500list.csv"),
    ("large", "Large cap", "Large cap (Nifty 100)", "ind_nifty100list.csv"),
    ("mid", "Mid cap", "Mid cap (Nifty Midcap 150)", "ind_niftymidcap150list.csv"),
    ("small", "Small cap", "Small cap (Nifty Smallcap 250)", "ind_niftysmallcap250list.csv"),
]


def get_symbols(file):
    r = requests.get(BASE + file, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))["Symbol"].dropna().tolist()


# 1. Stock lists for each segment (a segment that fails to download is skipped)
lists = {}
for key, short, label, file in SEGMENTS:
    try:
        lists[key] = get_symbols(file)
    except Exception as e:
        if key == "all":
            raise
        print("Skipping", label, "-", e)

# 2. Download about 2 years of daily closing prices for all Nifty 500 stocks
px = yf.download([s + ".NS" for s in lists["all"]], period="2y", interval="1d",
                 auto_adjust=True, progress=False)["Close"]
px = px.dropna(axis=1, how="all")
nifty = yf.download("^NSEI", period="2y", interval="1d", auto_adjust=True,
                    progress=False)["Close"].squeeze()


# 3. For one segment: daily share of stocks above each EMA, plus advances and declines
def breadth(key):
    cols = [s + ".NS" for s in lists[key] if s + ".NS" in px.columns]
    p = px[cols]

    def pct_above(n):
        ema = p.ewm(span=n, adjust=False, min_periods=n).mean()
        valid = ema.notna() & p.notna()
        return ((p > ema) & valid).sum(axis=1) / valid.sum(axis=1) * 100

    change = p.diff()
    if key == "all":
        index, note = nifty, "Nifty 50 index"
    else:
        index = (1 + p.pct_change(fill_method=None).mean(axis=1).fillna(0)).cumprod() * 1000
        note = "Equal-weighted average of these stocks (not the official index)"
    df = pd.DataFrame({
        "index": index,
        "p20": pct_above(20),
        "p50": pct_above(50),
        "p200": pct_above(200),
        "adv": (change > 0).sum(axis=1),
        "dec": (change < 0).sum(axis=1),
    }).replace([float("inf"), float("-inf")], float("nan")).dropna()
    return df, len(cols), note


results = {k: breadth(k) for k in lists}
dates = results["all"][0].tail(130).index

segments = {}
for key, short, label, file in SEGMENTS:
    if key not in results:
        continue
    df, n, note = results[key]
    df = df.reindex(dates).ffill().bfill()
    segments[key] = {
        "label": label,
        "short": short,
        "n": n,
        "note": note,
        "index": df["index"].astype(float).round(2).tolist(),
        "p20": df["p20"].astype(float).round(1).tolist(),
        "p50": df["p50"].astype(float).round(1).tolist(),
        "p200": df["p200"].astype(float).round(1).tolist(),
        "adv": df["adv"].astype(int).tolist(),
        "dec": df["dec"].astype(int).tolist(),
    }

out = {
    "updated": dates[-1].strftime("%d %b %Y"),
    "dates": [d.strftime("%Y-%m-%d") for d in dates],
    "segments": segments,
}
with open("data.json", "w") as f:
    json.dump(out, f)

print("Saved", len(dates), "days for", ", ".join(segments), "- latest:", out["updated"])
