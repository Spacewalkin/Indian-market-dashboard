"""Downloads Nifty 500 prices, works out market breadth, saves data.json."""
import io
import json

import pandas as pd
import requests
import yfinance as yf

LIST_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

# 1. Get the list of Nifty 500 stocks from NSE
resp = requests.get(LIST_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
resp.raise_for_status()
symbols = pd.read_csv(io.StringIO(resp.text))["Symbol"].dropna().tolist()
tickers = [s + ".NS" for s in symbols]

# 2. Download about 2 years of daily closing prices
px = yf.download(tickers, period="2y", interval="1d", auto_adjust=True, progress=False)["Close"]
px = px.dropna(axis=1, how="all")
idx = yf.download("^NSEI", period="2y", interval="1d", auto_adjust=True, progress=False)["Close"].squeeze()


# 3. For each day: what share of stocks closed above their EMA
def pct_above(n):
    ema = px.ewm(span=n, adjust=False, min_periods=n).mean()
    valid = ema.notna() & px.notna()
    return ((px > ema) & valid).sum(axis=1) / valid.sum(axis=1) * 100


# 4. For each day: how many stocks rose and how many fell
change = px.diff()
adv = (change > 0).sum(axis=1)
dec = (change < 0).sum(axis=1)

df = pd.DataFrame({
    "index": idx,
    "p20": pct_above(20),
    "p50": pct_above(50),
    "p200": pct_above(200),
    "adv": adv,
    "dec": dec,
}).replace([float("inf"), float("-inf")], pd.NA).dropna().tail(130)

out = {
    "updated": df.index[-1].strftime("%d %b %Y"),
    "dates": [d.strftime("%Y-%m-%d") for d in df.index],
    "index": df["index"].astype(float).round(2).tolist(),
    "p20": df["p20"].astype(float).round(1).tolist(),
    "p50": df["p50"].astype(float).round(1).tolist(),
    "p200": df["p200"].astype(float).round(1).tolist(),
    "adv": df["adv"].astype(int).tolist(),
    "dec": df["dec"].astype(int).tolist(),
}

with open("data.json", "w") as f:
    json.dump(out, f)

print("Saved", len(out["dates"]), "days, latest:", out["updated"])
