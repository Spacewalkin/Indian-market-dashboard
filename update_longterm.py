"""Long-term fundamentals for the Nifty 500 (Yahoo Finance): 3-year revenue and profit growth, return on equity,
debt, cash flow and P/E. Writes longterm.json. The page combines this with the live price trend and the latest
quarterly result to rank long-term ideas. Each run checks a batch, so the list fills in over a few runs."""
import json, math, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import yfinance as yf

MAX_PER_RUN, RECHECK_DAYS, WORKERS = 120, 28, 6
IST = timezone(timedelta(hours=5, minutes=30))


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def series(q, names):
    for n in names:
        if n in q.index:
            s = q.loc[n].dropna()
            if len(s):
                return [float(v) for _, v in sorted(s.items(), key=lambda kv: kv[0], reverse=True)]  # newest first
    return None


def cagr(vals):
    """Yearly growth between the newest and the value up to 3 years earlier; None if either end is not positive."""
    if not vals or len(vals) < 2:
        return None, 0
    n = min(3, len(vals) - 1)
    a, b = vals[0], vals[n]
    return (round(((a / b) ** (1 / n) - 1) * 100, 1) if a > 0 and b > 0 else None), n


def fetch(sym):
    try:
        t = yf.Ticker(sym + ".NS")
        info = t.info or {}
        q = t.income_stmt
        rev = series(q, ["Total Revenue", "Operating Revenue"]) if q is not None and not q.empty else None
        pat = series(q, ["Net Income", "Net Income Common Stockholders"]) if q is not None and not q.empty else None
        rc, yrs = cagr(rev)
        pc, _ = cagr(pat)
        pct = lambda v: None if num(v) is None else round(num(v) * 100, 1)
        ocf, ni = num(info.get("operatingCashflow")), num(info.get("netIncomeToCommon"))
        de = num(info.get("debtToEquity"))
        pe = num(info.get("trailingPE"))
        mc = num(info.get("marketCap"))
        rec = {"rev_cagr": rc, "pat_cagr": pc, "yrs": yrs,
               "loss_year": bool(pat and any(v <= 0 for v in pat[:3])),
               "roe": pct(info.get("returnOnEquity")), "de": None if de is None else round(de / 100, 2),
               "opm": pct(info.get("operatingMargins")), "pe": None if pe is None or pe <= 0 else round(pe, 1),
               "mcap_cr": None if mc is None else round(mc / 1e7),
               "ocf_ok": None if ocf is None or ni is None or ni <= 0 else bool(ocf >= 0.5 * ni),
               "ins": pct(info.get("heldPercentInsiders")), "checked_ts": int(time.time())}
        return sym, rec, True
    except Exception as e:
        print("  failed", sym, type(e).__name__)
        return sym, None, False


def main():
    stocks = json.load(open("data.json")).get("stocks") or {}
    try:
        out = json.load(open("longterm.json")).get("stocks") or {}
    except Exception:
        out = {}
    stale = time.time() - RECHECK_DAYS * 86400
    todo = sorted((s for s in stocks if s not in out or out[s].get("checked_ts", 0) < stale),
                  key=lambda s: out.get(s, {}).get("checked_ts", 0))[:MAX_PER_RUN]
    print(f"Checking {len(todo)} of {len(stocks)} stocks")
    with ThreadPoolExecutor(WORKERS) as ex:
        for sym, rec, ok in ex.map(fetch, todo):
            if ok and rec:
                out[sym] = rec
    res = {"stocks": {s: v for s, v in out.items() if s in stocks}, "ts": int(time.time()),
           "updated": datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")}
    with open("longterm.json", "w") as f:
        json.dump(res, f, separators=(",", ":"))
    print("Saved", len(res["stocks"]), "stocks")


if __name__ == "__main__":
    main()
