"""Daily breakout stock selection for the dashboard.

Downloads daily prices for the Nifty 500, finds stocks that broke out above
their 52-week high and have held it, scores each one on the strategy checks,
and writes breakouts.json for stock-picks.html to display.
"""
import io
import json
import datetime as dt

import numpy as np
import pandas as pd
import requests

UNIVERSE_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
MAX_AGE = 80          # breakout may be up to ~80 trading days old (3-4 months)
LOOKBACK = 250        # 52 weeks of trading days
MIN_VOL_RATIO = 1.5   # breakout-day volume vs 20-day average
MIN_CLOSE_POS = 0.6   # close in the top 40% of the breakout day's range
MAX_BASE_RANGE = 0.20 # prior 20-day high-low range, as a fraction of price
MAX_ABOVE_LEVEL = 0.12  # max distance above the breakout level
MAX_ABOVE_EMA20 = 0.08  # max distance above the 20 EMA
MAX_RISK = 0.10       # stop no more than 10% below entry
SECTOR_MIN = 0.55     # share of the industry's stocks above their 50 EMA


def load_universe():
    """Nifty 500 list from NSE; falls back to universe.csv (columns: Symbol, Company Name, Industry)."""
    try:
        r = requests.get(UNIVERSE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
    except Exception as e:
        print("Universe download failed, using universe.csv:", e)
        df = pd.read_csv("universe.csv")
    return df[["Symbol", "Company Name", "Industry"]].dropna()


def analyze(d, nifty):
    """d: daily OHLCV for one stock. nifty: Nifty 50 close series. Returns a dict or None."""
    d = d.dropna(subset=["Close", "High", "Low", "Volume"])
    n = len(d)
    if n < LOOKBACK + 10:
        return None
    close, high, low, vol = d["Close"], d["High"], d["Low"], d["Volume"]
    level = high.rolling(LOOKBACK).max().shift(1)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    vol20 = vol.rolling(20).mean().shift(1)

    # first day in the window that closed above the prior 52-week high and was never lost since
    i0 = None
    for i in range(max(n - MAX_AGE, LOOKBACK), n):
        if close.iloc[i] > level.iloc[i] and (close.iloc[i:] >= level.iloc[i]).all():
            i0 = i
            break
    if i0 is None:
        return None

    lvl = float(level.iloc[i0])
    last = float(close.iloc[-1])
    rng = float(high.iloc[i0] - low.iloc[i0])
    base = d.iloc[i0 - 20:i0]
    base_range = float((base["High"].max() - base["Low"].min()) / base["Close"].iloc[-1])
    entry = float(high.iloc[-1])
    stop = float(low.iloc[i0])
    risk = (entry - stop) / entry
    r126 = last / float(close.iloc[-127]) - 1
    r63 = last / float(close.iloc[-64]) - 1
    nc = nifty.reindex(d.index).ffill()
    n126 = float(nc.iloc[-1] / nc.iloc[-127] - 1)
    n63 = float(nc.iloc[-1] / nc.iloc[-64] - 1)

    checks = {
        "volume": bool(vol.iloc[i0] / vol20.iloc[i0] >= MIN_VOL_RATIO),
        "strong_close": bool(rng > 0 and (close.iloc[i0] - low.iloc[i0]) / rng >= MIN_CLOSE_POS),
        "above_50ema": bool(last > ema50.iloc[-1]),
        "tight_base": bool(base_range <= MAX_BASE_RANGE),
        "rel_strength": bool(r126 > n126 and r63 > n63),
        "not_extended": bool(last / lvl - 1 <= MAX_ABOVE_LEVEL and last / ema20.iloc[-1] - 1 <= MAX_ABOVE_EMA20),
        "stop_ok": bool(0 < risk <= MAX_RISK),
    }
    return {
        "checks": checks,
        "age": int(n - 1 - i0),
        "breakout_level": round(lvl, 2),
        "close": round(last, 2),
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "risk_pct": round(risk * 100, 1),
        "vol_ratio": round(float(vol.iloc[i0] / vol20.iloc[i0]), 1),
        "above50": bool(last > ema50.iloc[-1]),
    }


def sector_flags(results, universe):
    """Industry is 'strong' when most of its stocks are above their 50 EMA (uses all stocks, not only breakouts)."""
    s = pd.Series(results)
    ind = universe.set_index("Symbol")["Industry"]
    df = pd.DataFrame({"above": s, "industry": ind.reindex(s.index)}).dropna()
    share = df.groupby("industry")["above"].mean()
    return (share >= SECTOR_MIN).to_dict()


def main():
    import yfinance as yf

    uni = load_universe()
    tickers = [s + ".NS" for s in uni["Symbol"]]
    nifty = yf.download("^NSEI", period="15mo", auto_adjust=True, progress=False)["Close"].squeeze()

    picks, above50_all = [], {}
    for k in range(0, len(tickers), 100):
        batch = tickers[k:k + 100]
        data = yf.download(batch, period="15mo", group_by="ticker", auto_adjust=True, progress=False, threads=True)
        for t in batch:
            try:
                d = data[t]
                res = analyze(d, nifty)
                sym = t[:-3]
                dd = d.dropna(subset=["Close"])
                if len(dd) > 60:
                    above50_all[sym] = bool(dd["Close"].iloc[-1] > dd["Close"].ewm(span=50, adjust=False).mean().iloc[-1])
                if res:
                    res["symbol"] = sym
                    picks.append(res)
            except Exception:
                continue

    strong = sector_flags(above50_all, uni)
    info = uni.set_index("Symbol")
    for p in picks:
        industry = info.loc[p["symbol"], "Industry"]
        p["name"] = info.loc[p["symbol"], "Company Name"]
        p["industry"] = industry
        p["checks"]["sector"] = bool(strong.get(industry, False))
        p["score"] = int(sum(p["checks"].values()))
    picks.sort(key=lambda p: (-p["score"], p["age"]))

    out = {
        "generated": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "max_score": 8,
        "count": len(picks),
        "picks": picks,
    }
    with open("breakouts.json", "w") as f:
        json.dump(out, f, indent=1)
    print("Wrote", len(picks), "breakout candidates")


if __name__ == "__main__":
    main()
