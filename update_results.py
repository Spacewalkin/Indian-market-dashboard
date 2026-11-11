"""Quarterly results for the Nifty 500: reads each stock's latest quarter from Yahoo Finance, compares it with the
same quarter last year and the previous quarter, and writes results.json with a Good / Average / Bad signal.
Each run checks a limited batch (the stocks never checked, then those whose result date has passed, then the
oldest), so the whole list fills in over a few runs. Figures are Yahoo's (usually consolidated) and unaudited."""
import json, math, os, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import yfinance as yf

MAX_PER_RUN, RECHECK_DAYS, WORKERS = 140, 6, 6
IST = timezone(timedelta(hours=5, minutes=30))


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def row(q, names):
    for n in names:
        if n in q.index:
            s = q.loc[n].dropna()
            if len(s):
                return s
    return None


def near(s, d, days, tol=25):
    """The value of series s for the quarter about `days` before date d."""
    if s is None:
        return None
    for idx, v in s.items():
        if abs((d - idx).days - days) <= tol:
            return num(v)
    return None


def growth(cur, prev):
    return round((cur / prev - 1) * 100, 1) if cur is not None and prev is not None and prev > 0 else None


def label(d):
    fy = d.year + 1 if d.month >= 4 else d.year
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    return f"{d:%b %Y}" + (f" (Q{q} FY{str(fy)[2:]})" if q else "")


def judge(ry, py, mc, pat0, pat_ly):
    why = []
    if pat0 is not None and pat0 < 0:
        return "Bad", ["Net loss this quarter"]
    if pat0 is not None and pat_ly is not None and pat_ly <= 0 < pat0:
        return "Good", ["Profit this quarter against a loss a year ago"]
    if ry is None and py is None:
        return "Unknown", ["Not enough history to compare with last year"]
    pts = 0
    if ry is not None:
        pts += 2 if ry >= 15 else 1 if ry >= 8 else 0 if ry >= 0 else -1 if ry >= -5 else -2
        why.append(f"Revenue {ry:+.1f}% vs last year")
    if py is not None:
        pts += 2 if py >= 25 else 1 if py >= 10 else 0 if py > -10 else -1 if py > -25 else -2
        why.append(f"Profit {py:+.1f}% vs last year")
    if mc is not None:
        pts += 1 if mc >= 1 else -1 if mc <= -2 else 0
        why.append(f"Operating margin {mc:+.1f} pts")
    return ("Good" if pts >= 3 else "Bad" if pts <= -2 else "Average"), why


def analyse(q):
    q = q[sorted(q.columns, reverse=True)]
    rev = row(q, ["Total Revenue", "Operating Revenue"])
    pat = row(q, ["Net Income", "Net Income Common Stockholders"])
    opi = row(q, ["Operating Income", "EBIT"])
    base = pat if pat is not None else rev
    if base is None:
        return None
    d0 = base.index[0].to_pydatetime() if hasattr(base.index[0], "to_pydatetime") else base.index[0]
    d0 = d0.replace(tzinfo=None)
    rev, pat, opi = [None if s is None else s.rename(lambda i: i.replace(tzinfo=None)) for s in (rev, pat, opi)]
    r0, p0, o0 = (near(s, d0, 0, 5) for s in (rev, pat, opi))
    ry, py = growth(r0, near(rev, d0, 365)), growth(p0, near(pat, d0, 365))
    rq, pq = growth(r0, near(rev, d0, 91)), growth(p0, near(pat, d0, 91))
    m0 = o0 / r0 * 100 if o0 is not None and r0 else None
    oly, rly = near(opi, d0, 365), near(rev, d0, 365)
    mly = oly / rly * 100 if oly is not None and rly else None
    mc = round(m0 - mly, 1) if m0 is not None and mly is not None else None
    sig, why = judge(ry, py, mc, p0, near(pat, d0, 365))
    cr = lambda v: None if v is None else round(v / 1e7, 1)
    return {"q": label(d0), "qe": d0.date().isoformat(), "sig": sig, "why": why, "rev": ry, "pat": py,
            "opm": None if m0 is None else round(m0, 1), "opmc": mc, "rev_q": rq, "pat_q": pq,
            "rev_cr": cr(r0), "pat_cr": cr(p0)}


def fetch(sym):
    try:
        q = yf.Ticker(sym + ".NS").quarterly_income_stmt
        return sym, (analyse(q) if q is not None and not q.empty else None), True
    except Exception as e:
        print("  failed", sym, type(e).__name__)
        return sym, None, False


def main():
    data = json.load(open("data.json"))
    stocks = data.get("stocks") or {}
    try:
        res = json.load(open("results.json"))
    except Exception:
        res = {}
    out = res.get("stocks") or {}
    today = datetime.now(IST).date()
    for e in (data.get("events") or {}).get("results", []):  # remember announced dates, to re-check after they pass
        res.setdefault("due", {})[e["s"]] = e["d"]
    due = res.get("due", {})

    def pri(s):
        rec = out.get(s)
        if not rec:
            return (0, 0)
        chk = rec.get("checked_ts", 0)
        passed = due.get(s) and due[s] <= today.isoformat() and datetime.fromtimestamp(chk, IST).date().isoformat() <= due[s]
        if passed:
            return (1, chk)
        return (2, chk) if time.time() - chk > RECHECK_DAYS * 86400 else (9, chk)

    todo = sorted((s for s in stocks if pri(s)[0] < 9), key=pri)[:MAX_PER_RUN]
    print(f"Checking {len(todo)} of {len(stocks)} stocks")
    with ThreadPoolExecutor(WORKERS) as ex:
        for sym, rec, ok in ex.map(fetch, todo):
            if not ok:
                continue  # a failed download keeps the old record
            keep = out.get(sym, {})
            if rec is None:
                keep.update(checked_ts=int(time.time()))
                out[sym] = keep
                continue
            rec.update(n=stocks[sym].get("n", sym), sec=stocks[sym].get("sec", ""), checked_ts=int(time.time()))
            out[sym] = rec
    res.update(stocks=out, ts=int(time.time()), updated=datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST"))
    with open("results.json", "w") as f:
        json.dump(res, f, separators=(",", ":"))
    print("Saved", sum(1 for r in out.values() if r.get("sig")), "stocks with a result signal")


if __name__ == "__main__":
    main()
