"""
Fundamentals scan for Nifty 500 (yfinance data) + news flag for the shortlist.
Output: data/fundamentals.json and data/fundamentals.csv
Universe: data/nifty500.csv (column "Symbol"; "Company Name" optional).
If the file is missing the script tries to download it from NSE.
"""
import os, io, json, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import pandas as pd
import yfinance as yf

UNIVERSE_FILE = "data/nifty500.csv"
OUT_JSON = "data/fundamentals.json"
OUT_CSV = "data/fundamentals.csv"
NSE_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

# ---- thresholds (change here if you want stricter / looser) ----
MIN_REV_YOY = 15      # % sales growth YoY
MIN_PAT_YOY = 20      # % profit growth YoY
MIN_CFO_PAT = 0.7     # operating cash flow / PAT, last 3 years
MAX_DE = 1.0          # debt / equity (not applied to banks/NBFCs)
MAX_FROM_HIGH = 25    # % below 52-week high allowed

POS_WORDS = ["order", "bags", "wins", "contract", "acquisition", "approval",
             "expansion", "upgrade", "capacity", "deal", "buyback", "dividend"]
NEG_WORDS = ["pledge", "resign", "probe", "fraud", "raid", "downgrade",
             "penalty", "sebi", "default", "loss", "cut", "fire", "notice"]


def load_universe():
    if not os.path.exists(UNIVERSE_FILE):
        os.makedirs("data", exist_ok=True)
        try:
            req = urllib.request.Request(NSE_URL, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
            open(UNIVERSE_FILE, "w", encoding="utf-8").write(raw)
        except Exception as e:
            raise SystemExit(
                f"Could not download Nifty 500 list ({e}).\n"
                f"Download it from your laptop and save as {UNIVERSE_FILE}"
            )
    df = pd.read_csv(UNIVERSE_FILE)
    df.columns = [c.strip() for c in df.columns]
    name_col = "Company Name" if "Company Name" in df.columns else "Symbol"
    return list(zip(df["Symbol"].astype(str).str.strip(), df[name_col].astype(str)))


def row(df, names):
    """First matching row of a yfinance statement, newest first, NaNs dropped."""
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            return df.loc[n].dropna()
    return None


def yoy(s):
    if s is None or len(s) < 5:
        return None
    new, old = float(s.iloc[0]), float(s.iloc[4])
    if old <= 0:
        return None
    return (new - old) / old * 100


def analyze(sym, name):
    t = yf.Ticker(sym + ".NS")
    q = t.quarterly_income_stmt
    rev = row(q, ["Total Revenue", "Operating Revenue"])
    op = row(q, ["Operating Income"])
    pat = row(q, ["Net Income", "Net Income Common Stockholders"])

    rev_yoy, pat_yoy = yoy(rev), yoy(pat)
    if rev_yoy is None or pat_yoy is None:
        return {"symbol": sym, "name": name, "status": "data missing"}

    # operating margin now vs a year ago
    opm_now = opm_old = None
    if op is not None:
        m = (op / rev).dropna()
        if len(m) >= 5:
            opm_now, opm_old = float(m.iloc[0]) * 100, float(m.iloc[4]) * 100

    # financials (banks/NBFCs) are judged differently, skip cash flow & D/E
    try:
        sector = t.info.get("sector", "") or ""
    except Exception:
        sector = ""
    is_fin = "financial" in sector.lower()

    # operating cash flow vs profit, last up to 3 years
    cfo_pat = None
    if not is_fin:
        cfo = row(t.cashflow, ["Operating Cash Flow",
                               "Cash Flow From Continuing Operating Activities"])
        pat_a = row(t.income_stmt, ["Net Income", "Net Income Common Stockholders"])
        if cfo is not None and pat_a is not None:
            cols = [c for c in cfo.index if c in pat_a.index][:3]
            if cols and pat_a[cols].sum() > 0:
                cfo_pat = float(cfo[cols].sum() / pat_a[cols].sum())

    # debt / equity
    de = None
    if not is_fin:
        bs = t.balance_sheet
        debt = row(bs, ["Total Debt"])
        eq = row(bs, ["Stockholders Equity", "Common Stock Equity"])
        if debt is not None and eq is not None and float(eq.iloc[0]) > 0:
            de = float(debt.iloc[0]) / float(eq.iloc[0])

    # price position
    h = t.history(period="1y")["Close"].dropna()
    if len(h) < 200:
        return {"symbol": sym, "name": name, "status": "data missing"}
    price, dma200, hi = float(h.iloc[-1]), float(h.tail(200).mean()), float(h.max())
    from_high = (hi - price) / hi * 100

    # ---- hard filters ----
    fails = []
    if rev_yoy < MIN_REV_YOY: fails.append("sales growth low")
    if pat_yoy < MIN_PAT_YOY: fails.append("profit growth low")
    if opm_now is not None and opm_now < opm_old - 1: fails.append("margin shrinking")
    if cfo_pat is not None and cfo_pat < MIN_CFO_PAT: fails.append("weak cash conversion")
    if de is not None and de > MAX_DE: fails.append("high debt")
    if price < dma200: fails.append("below 200 DMA")
    if from_high > MAX_FROM_HIGH: fails.append("far below 52w high")

    # ---- score (0-100) ----
    score = min(rev_yoy, 40) / 40 * 20 + min(pat_yoy, 80) / 80 * 30
    if opm_now is not None and opm_now >= opm_old: score += 10
    if cfo_pat is not None: score += 15 if cfo_pat >= 1 else 8 if cfo_pat >= MIN_CFO_PAT else 0
    if de is not None: score += 10 if de < 0.5 else 5 if de <= MAX_DE else 0
    score += 15 if from_high <= 10 else 8 if from_high <= MAX_FROM_HIGH else 0

    unverified = []
    if not is_fin and cfo_pat is None: unverified.append("cash flow")
    if not is_fin and de is None: unverified.append("debt")
    if opm_now is None: unverified.append("margin")

    r = lambda x: None if x is None else round(x, 2)
    return {
        "symbol": sym, "name": name, "sector": sector,
        "status": "pass" if not fails else "fail",
        "fails": fails, "unverified": unverified,
        "score": round(score, 1),
        "sales_yoy": r(rev_yoy), "pat_yoy": r(pat_yoy),
        "opm_now": r(opm_now), "opm_year_ago": r(opm_old),
        "cfo_to_pat": r(cfo_pat), "debt_equity": r(de),
        "price": r(price), "pct_from_52w_high": r(from_high),
        "above_200dma": price >= dma200,
        "holdings": "CHECK (promoter / pledge / FII-DII)",
    }


def news_flag(name):
    """Last ~15 days of Google News headlines + keyword flags."""
    try:
        q = urllib.parse.quote(f'"{name}" share when:15d')
        url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(urllib.request.urlopen(req, timeout=20).read())
        titles = [i.findtext("title", "") for i in root.iter("item")][:8]
    except Exception:
        return {"headlines": [], "positive": [], "negative": [], "note": "news fetch failed"}
    low = " ".join(titles).lower()
    return {
        "headlines": titles[:3],
        "positive": sorted({w for w in POS_WORDS if w in low}),
        "negative": sorted({w for w in NEG_WORDS if w in low}),
    }


def main():
    universe = load_universe()
    results, missing = [], 0
    for i, (sym, name) in enumerate(universe, 1):
        try:
            r = analyze(sym, name)
        except Exception as e:
            r = {"symbol": sym, "name": name, "status": "data missing", "error": str(e)[:80]}
        if r["status"] == "data missing":
            missing += 1
        results.append(r)
        if i % 25 == 0:
            print(f"{i}/{len(universe)} scanned")
        time.sleep(0.2)

    picks = sorted([r for r in results if r["status"] == "pass"],
                   key=lambda x: x["score"], reverse=True)[:20]
    for p in picks:
        p["news"] = news_flag(p["name"])
        time.sleep(0.5)

    os.makedirs("data", exist_ok=True)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scanned": len(universe),
        "passed": len([r for r in results if r["status"] == "pass"]),
        "data_missing": missing,
        "picks": picks,
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    pd.DataFrame([r for r in results if r["status"] != "data missing"]).to_csv(OUT_CSV, index=False)
    print(f"Done. scanned={out['scanned']} passed={out['passed']} missing={missing}")
    for p in picks:
        print(f"{p['score']:>5}  {p['symbol']:<14} sales {p['sales_yoy']}%  PAT {p['pat_yoy']}%")


if __name__ == "__main__":
    main()
