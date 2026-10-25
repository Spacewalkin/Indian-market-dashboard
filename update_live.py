"""Live (about 15 minutes delayed) market snapshot, saved as live.json.

Runs every few minutes while the Indian market is open. Prices come from Yahoo Finance.
The pre-open table comes from NSE and may be blocked on some days; the rest still works.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
now = datetime.now(IST)
minute = now.hour * 60 + now.minute
force = os.environ.get("FORCE") == "1"   # set when you press "Run workflow" by hand
if not force and (now.weekday() > 4 or not (8 * 60 + 55 <= minute <= 15 * 60 + 50)):
    print("Market is closed, nothing to do.")
    sys.exit(0)

today = now.date()
data = json.load(open("data.json"))
segs = {s["key"]: s["stocks"] for s in data["segments"]}
try:
    prev = json.load(open("prev_live.json"))
except Exception:
    prev = {}


# ---------- 1. NSE pre-open (9:00 to 9:15). NSE may block this; that is fine ----------
def pre_open():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json,text/plain,*/*",
                      "Accept-Language": "en-US,en;q=0.9",
                      "Referer": "https://www.nseindia.com/market-data/pre-open-market-cm-and-emerge-market"})
    s.get("https://www.nseindia.com", timeout=20)
    for key in ("FO", "NIFTY"):
        r = s.get("https://www.nseindia.com/api/market-data-pre-open", params={"key": key}, timeout=25)
        if r.status_code != 200:
            print("NSE pre-open", key, "HTTP", r.status_code)
            continue
        j = r.json()
        rows = []
        for it in j.get("data", []):
            m = it.get("metadata", {})
            try:
                rows.append((m["symbol"], float(m["pChange"]), float(m["lastPrice"])))
            except Exception:
                pass
        if not rows:
            continue
        rows.sort(key=lambda x: -x[1])
        nf = j.get("niftyPreopenStatus") or {}
        nifty = None
        try:
            nifty = {"p": float(nf["lastPrice"]), "d": float(nf.get("perChange", nf.get("pChange")))}
        except Exception:
            pass
        return {"ok": True, "at": now.strftime("%H:%M"), "date": today.isoformat(), "key": key,
                "n": len(rows), "adv": sum(1 for x in rows if x[1] > 0), "dec": sum(1 for x in rows if x[1] < 0),
                "nifty": nifty,
                "gain": [[a, round(b, 2), c] for a, b, c in rows[:5]],
                "lose": [[a, round(b, 2), c] for a, b, c in rows[::-1][:5]]}
    return {"ok": False, "date": today.isoformat(), "at": now.strftime("%H:%M")}


pre = None
if force or minute <= 9 * 60 + 20:
    try:
        pre = pre_open()
    except Exception as e:
        print("NSE pre-open failed:", e)
        pre = {"ok": False, "date": today.isoformat(), "at": now.strftime("%H:%M")}
old = prev.get("pre") or {}
if (not pre or not pre.get("ok")) and old.get("ok") and old.get("date") == today.isoformat():
    pre = old   # keep this morning's pre-open view after the market opens


# ---------- 2. Today's prices in 5-minute bars (Yahoo Finance, about 15 minutes delayed) ----------
def split(df):
    """Return (today's bars, previous session's bars), or None if today has no bars yet."""
    df = df.copy()
    df.index = df.index.tz_convert(IST) if df.index.tz is not None else df.index.tz_localize("UTC").tz_convert(IST)
    d = df.index.date
    days = sorted(set(d))
    if len(days) < 2 or days[-1] != today:
        return None
    return df[d == days[-1]], df[d == days[-2]]


def index_snap(tk, line=False):
    try:
        df = yf.download(tk, period="5d", interval="5m", auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        sp = split(df.dropna(subset=["Close"]))
        if not sp:
            return None
        t, y = sp
        p, pc, o = float(t["Close"].iloc[-1]), float(y["Close"].iloc[-1]), float(t["Open"].iloc[0])
        out = {"p": round(p, 2), "d": round((p / pc - 1) * 100, 2), "gap": round((o / pc - 1) * 100, 2),
               "open": round(o, 2), "prev": round(pc, 2)}
        if line:
            out["t"] = [i.strftime("%H:%M") for i in t.index]
            out["v"] = [round(float(v), 2) for v in t["Close"]]
        return out
    except Exception as e:
        print("Index", tk, "failed:", e)
        return None


idx = {k: index_snap(tk, line=(k == "nifty")) for k, tk in
       (("nifty", "^NSEI"), ("bank", "^NSEBANK"), ("vix", "^INDIAVIX"))}

seg_out, last_bar = {}, None
try:
    px = yf.download([s + ".NS" for s in segs["all"]], period="5d", interval="5m",
                     auto_adjust=False, progress=False, threads=True)
    sp = split(px)
    if sp:
        t, y = sp
        cur, pc, op = t["Close"].ffill().iloc[-1], y["Close"].ffill().iloc[-1], t["Open"].bfill().iloc[0]
        chg = ((cur / pc - 1) * 100).replace([float("inf"), float("-inf")], float("nan")).dropna()
        gap = ((op / pc - 1) * 100).reindex(chg.index)
        for key, syms in segs.items():
            c = chg.reindex([s + ".NS" for s in syms]).dropna()
            if len(c) < 3:
                continue
            g = gap.reindex(c.index).dropna()
            seg_out[key] = {"n": int(len(c)), "adv": int((c > 0).sum()), "dec": int((c < 0).sum()),
                            "flat": int((c == 0).sum()), "avg": round(float(c.mean()), 2),
                            "gap": round(float(g.mean()), 2) if len(g) else 0}
        last_bar = t.index[-1].strftime("%H:%M")
except Exception as e:
    print("Stock prices failed:", e)

out = {"date": today.isoformat(), "time": now.strftime("%H:%M"), "epoch": int(time.time()),
       "live": bool(seg_out), "bar": last_bar,
       "idx": {k: v for k, v in idx.items() if v}, "seg": seg_out, "pre": pre}
with open("live.json", "w") as f:
    json.dump(out, f, separators=(",", ":"))
print("live.json saved:", "live" if out["live"] else "no live bars yet", "| last bar", last_bar,
      "| pre-open:", (pre or {}).get("ok"))
