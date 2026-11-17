"""
longterm_scanner.py  -  Long-term (investing, NOT swing) stock research scanner for NSE stocks.

What it does for every stock in watchlist.csv:
  1. Pulls earnings, margins, cash flow, debt, valuation and price data (yfinance, free)
  2. Scores it out of 100:
        Growth 25 + Quality 15 + Valuation 15 + Price setup 10   = 65  (measured by this script)
        News/order triggers 20 + Sector tailwind 15              = 35  (YOUR input in watchlist.csv)
  3. Raises RED FLAGS (loss-making, revenue falling, weak cash flow, high debt, downtrend, ...)
  4. Gives a verdict + plain-English "why" lines
  5. Saves research.json (for the website) and longterm_report.csv (open in Excel)

Run:
    python longterm_scanner.py                 -> scans everything in watchlist.csv
    python longterm_scanner.py TITAN BEL       -> scans only these (NSE symbols, no .NS needed)

This is research support only - the final decision is always yours.
"""

import csv
import json
import math
import sys
import re
import time
import urllib.request
import datetime as dt
from html.parser import HTMLParser
from pathlib import Path

WATCHLIST = Path("watchlist.csv")
OUT_JSON = Path("research.json")
OUT_CSV = Path("longterm_report.csv")

FINANCIAL_SECTORS = {"Financial Services"}  # banks / NBFCs: debt & cash-flow rules don't apply

# weights
W_GROWTH, W_QUALITY, W_VALUATION, W_SETUP = 25, 15, 15, 10
W_NEWS, W_SECTOR = 20, 15


# --------------------------------------------------------------------------- helpers
def safe(x):
    """Convert to float, return None for missing / NaN."""
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def pct(new, old):
    """% change from old to new. None if not meaningful."""
    if new is None or old is None or old <= 0:
        return None
    return (new - old) / old * 100.0


def row(df, names):
    """Return values of the first matching row, newest period first."""
    if df is None or getattr(df, "empty", True):
        return []
    df = df.sort_index(axis=1, ascending=False)
    for n in names:
        if n in df.index:
            return [safe(v) for v in df.loc[n].tolist()]
    return []


def at(vals, i):
    return vals[i] if len(vals) > i else None


def cagr(vals, max_years=3):
    n = min(max_years, len(vals) - 1)
    if n < 1:
        return None
    new, old = vals[0], vals[n]
    if new is None or old is None or new <= 0 or old <= 0:
        return None
    return ((new / old) ** (1.0 / n) - 1) * 100.0


def fmt(x, nd=1, suffix=""):
    return "n/a" if x is None else f"{x:.{nd}f}{suffix}"


class Bucket:
    """Collects points for one score bucket; unavailable metrics are skipped, not punished."""

    def __init__(self, weight):
        self.w, self.earned, self.avail, self.missed = weight, 0.0, 0.0, 0.0

    def add(self, pts, maxpts):
        self.earned += pts
        self.avail += maxpts

    def miss(self, maxpts):
        self.missed += maxpts

    @property
    def score(self):
        # if nothing could be measured, give a neutral 50% and let coverage show it
        return self.earned / self.avail * self.w if self.avail else self.w * 0.5

    @property
    def possible(self):
        return self.avail + self.missed


# --------------------------------------------------------------------------- data fetch
def fetch_metrics(symbol):
    import yfinance as yf  # imported here so the scoring logic can be tested offline

    tk = yf.Ticker(symbol + ".NS")
    info = tk.info or {}
    q = tk.quarterly_income_stmt
    a = tk.income_stmt
    cf = tk.cashflow
    hist = tk.history(period="2y", auto_adjust=False)

    q_rev = row(q, ["Total Revenue", "Operating Revenue"])
    q_pat = row(q, ["Net Income", "Net Income Common Stockholders"])
    q_op = row(q, ["Operating Income", "EBIT"])
    a_rev = row(a, ["Total Revenue", "Operating Revenue"])
    a_pat = row(a, ["Net Income", "Net Income Common Stockholders"])
    ocf = row(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"])

    m = {
        "symbol": symbol,
        "name": info.get("shortName") or symbol,
        "sector": info.get("sector") or "Unknown",
        "industry": info.get("industry") or "",
        "mcap_cr": (safe(info.get("marketCap")) or 0) / 1e7 or None,
        "pe": safe(info.get("trailingPE")),
        "fwd_pe": safe(info.get("forwardPE")),
        "roe": None,
        "de": None,
    }
    roe = safe(info.get("returnOnEquity"))
    m["roe"] = roe * 100 if roe is not None else None
    de = safe(info.get("debtToEquity"))
    m["de"] = de / 100.0 if de is not None else None  # yfinance gives it in %

    # earnings (latest quarter vs same quarter last year)
    m["q_rev_yoy"] = pct(at(q_rev, 0), at(q_rev, 4))
    m["q_pat_yoy"] = pct(at(q_pat, 0), at(q_pat, 4))
    m["prev_q_rev_yoy"] = pct(at(q_rev, 1), at(q_rev, 5))
    m["rev_cagr"] = cagr(a_rev)
    m["pat_cagr"] = cagr(a_pat)

    # operating margin change vs year-ago quarter (percentage points)
    def opm(i):
        r, o = at(q_rev, i), at(q_op, i)
        return o / r * 100 if (r and o is not None and r > 0) else None

    m["opm"], opm_old = opm(0), opm(4)
    m["opm_chg"] = m["opm"] - opm_old if (m["opm"] is not None and opm_old is not None) else None

    # cash flow quality: operating cash flow / net profit (latest year)
    o, p = at(ocf, 0), at(a_pat, 0)
    m["ocf_pat"] = o / p if (o is not None and p is not None and p > 0) else None
    m["ocf_negative"] = o is not None and o < 0

    # price setup
    close = [safe(x) for x in hist["Close"].tolist()] if not hist.empty else []
    close = [c for c in close if c is not None]
    price = safe(info.get("currentPrice")) or (close[-1] if close else None)
    m["price"] = price
    if close and price:
        last_year = close[-252:]
        m["hi52"], m["lo52"] = max(last_year), min(last_year)
        m["dist_high"] = (price / m["hi52"] - 1) * 100
        m["above200"] = price > sum(close[-200:]) / len(close[-200:]) if len(close) >= 200 else None
        m["ret6m"] = pct(price, close[-126]) if len(close) > 126 else None
    else:
        m["hi52"] = m["lo52"] = m["dist_high"] = m["above200"] = m["ret6m"] = None
    return m


# --------------------------------------------------------------------------- Screener.in fallback
class _SectionTables(HTMLParser):
    """Reads every table row of a Screener page, grouped by the <section id=...> it sits in."""

    def __init__(self):
        super().__init__()
        self.section, self.rows, self._row, self._cell = None, {}, None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and a.get("id"):
            self.section = a["id"]
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and self.section:
                self.rows.setdefault(self.section, []).append(self._row)
            self._row = None


def _num(s):
    return safe(str(s).replace(",", "").replace("%", "").strip()) if s not in (None, "") else None


def _find_row(rows, names):
    for r in rows:
        if r and r[0].rstrip("+ ").strip().lower() in names:
            return [_num(x) for x in r[1:]]
    return None


def _headers(rows):
    for r in rows:
        cells = r[1:]
        if len(cells) >= 2 and all(re.fullmatch(r"[A-Za-z]{3} \d{4}", c) for c in cells):
            return cells
    return None


def parse_screener(html):
    """Pull latest-quarter growth, ROE and debt/equity out of a Screener.in company page."""
    p = _SectionTables()
    p.feed(html)
    out = {}

    # latest quarter vs the same quarter last year
    qrows = p.rows.get("quarters", [])
    hdr = _headers(qrows)
    sales = _find_row(qrows, {"sales", "revenue"})
    profit = _find_row(qrows, {"net profit"})
    if hdr and len(hdr) >= 5:
        same_qtr = hdr[-1][:3] == hdr[-5][:3] and int(hdr[-1][-4:]) - int(hdr[-5][-4:]) == 1
        if same_qtr:
            if sales and len(sales) >= 5:
                out["q_rev_yoy"] = pct(sales[-1], sales[-5])
            if profit and len(profit) >= 5:
                out["q_pat_yoy"] = pct(profit[-1], profit[-5])

    # ROE from the top ratios box
    m = re.search(r'id="top-ratios"(.*?)</ul>', html, re.S)
    if m:
        for li in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.S):
            nm = re.search(r'class="name">\s*(.*?)\s*</span>', li, re.S)
            val = re.search(r'class="number">\s*([-\d,\.]+)\s*</span>', li, re.S)
            if nm and val and nm.group(1).strip().upper() == "ROE":
                out["roe"] = _num(val.group(1))

    # debt/equity = borrowings / (equity capital + reserves), latest year
    brows = p.rows.get("balance-sheet", [])
    eq, res, bor = (_find_row(brows, {n}) for n in ("equity capital", "reserves", "borrowings"))
    if eq and res and bor and eq[-1] is not None and res[-1] is not None and bor[-1] is not None:
        net_worth = eq[-1] + res[-1]
        if net_worth > 0:
            out["de"] = bor[-1] / net_worth
    return out


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def fill_from_screener(m, symbol, skip=()):
    """Fill ONLY the numbers still missing after yfinance. Never overwrites a number we already have."""
    keys = ("q_rev_yoy", "q_pat_yoy", "roe", "de")
    fin = m.get("sector") in FINANCIAL_SECTORS
    need = [k for k in keys if m.get(k) is None and k not in skip and not (k == "de" and fin)]
    if not need:
        return
    got = {}
    for suffix in ("/consolidated/", "/"):
        try:
            got = parse_screener(_get(f"https://www.screener.in/company/{symbol}{suffix}"))
            if got:
                break
        except Exception as e:
            print(f"  (Screener.in {suffix} not available for {symbol}: {e})")
    filled = [k for k in need if got.get(k) is not None]
    for k in filled:
        m[k] = got[k]
    if filled:
        m["auto_filled"] = filled
        print(f"  filled from Screener.in: {', '.join(filled)}")
    elif need:
        print(f"  Screener.in gave nothing for: {', '.join(need)}")


# --------------------------------------------------------------------------- scoring
def score_stock(m, news_score=None, sector_score=None):
    fin = m.get("sector") in FINANCIAL_SECTORS
    flags, why = [], []
    severe = False

    # ---- Growth (25)
    g = Bucket(W_GROWTH)
    v = m.get("q_rev_yoy")
    if v is None: g.miss(8)
    else: g.add(8 if v >= 25 else 6 if v >= 15 else 4 if v >= 8 else 2 if v >= 0 else 0, 8)
    v2 = m.get("q_pat_yoy")
    if v2 is None: g.miss(9)
    else: g.add(9 if v2 >= 30 else 7 if v2 >= 15 else 5 if v2 >= 5 else 3 if v2 >= 0 else 0, 9)
    v3 = m.get("rev_cagr")
    if v3 is None: g.miss(4)
    else: g.add(4 if v3 >= 20 else 3 if v3 >= 12 else 2 if v3 >= 6 else 1 if v3 >= 0 else 0, 4)
    v4 = m.get("pat_cagr")
    if v4 is None: g.miss(4)
    else: g.add(4 if v4 >= 25 else 3 if v4 >= 12 else 2 if v4 >= 5 else 1 if v4 >= 0 else 0, 4)

    why.append(f"Latest quarter: revenue {fmt(v, 0, '%')} YoY, profit {fmt(v2, 0, '%')} YoY; "
               f"3-yr CAGR revenue {fmt(v3, 0, '%')}, profit {fmt(v4, 0, '%')}.")
    if v is not None and v < 0:
        flags.append("Revenue falling YoY"); severe = True
    if v2 is not None and v2 < -10:
        flags.append("Profit down >10% YoY")
    if m.get("pe") is None and (v2 is not None and v2 < 0):
        flags.append("Possibly loss-making"); severe = True

    # ---- Quality (15)
    qb = Bucket(W_QUALITY)
    r = m.get("roe")
    if r is None: qb.miss(5)
    else: qb.add(5 if r >= 20 else 4 if r >= 15 else 2 if r >= 10 else 0, 5)
    oc = m.get("opm_chg")
    if oc is None: qb.miss(4)
    else: qb.add(4 if oc >= 1 else 2 if oc >= -1 else 0, 4)
    if not fin:
        ocp = m.get("ocf_pat")
        if ocp is None: qb.miss(4)
        else: qb.add(4 if ocp >= 0.8 else 2 if ocp >= 0.5 else 0, 4)
        de = m.get("de")
        if de is None: qb.miss(2)
        else: qb.add(2 if de <= 0.3 else 1 if de <= 1 else 0, 2)
        if m.get("ocf_negative"):
            flags.append("Operating cash flow negative"); severe = True
        elif ocp is not None and ocp < 0.5:
            flags.append(f"Weak cash conversion (cash flow only {ocp:.2f}x profit) - check why on Screener")
        if de is not None and de > 2:
            flags.append(f"High debt (D/E {de:.1f}) - check what the debt is made of (lease / gold loans / real borrowings)")
    why.append(f"ROE {fmt(r, 0, '%')}; operating margin change {fmt(oc, 1, ' pts')} YoY"
               + ("" if fin else f"; cash flow/profit {fmt(m.get('ocf_pat'), 2, 'x')}; debt/equity {fmt(m.get('de'), 2)}") + ".")

    gaps = [lbl for key, lbl in (("q_rev_yoy", "latest-quarter revenue growth"), ("q_pat_yoy", "latest-quarter profit growth"),
                                 ("roe", "ROE"), ("de", "debt/equity")) if m.get(key) is None and not (fin and key == "de")]
    if m.get("auto_filled"):
        names = {"q_rev_yoy": "quarter revenue growth", "q_pat_yoy": "quarter profit growth", "roe": "ROE", "de": "debt/equity"}
        why.append("Auto-filled from Screener.in: " + ", ".join(names[k] for k in m["auto_filled"]) + ".")
    if gaps:
        why.append("MISSING DATA: " + ", ".join(gaps) + " - fill these in watchlist.csv from Screener.in, then re-run (auto-fetch could not get them).")

    # ---- Valuation (15)
    vb = Bucket(W_VALUATION)
    pe = m.get("pe")
    if pe is None: vb.miss(7)
    else: vb.add(7 if pe <= 15 else 6 if pe <= 25 else 4 if pe <= 40 else 2 if pe <= 60 else 1 if pe <= 90 else 0, 7)
    growth_for_peg = v4 if (v4 is not None and v4 > 0) else (v2 if (v2 is not None and v2 > 0) else None)
    peg = pe / growth_for_peg if (pe and growth_for_peg) else None
    if peg is None: vb.miss(5)
    else: vb.add(5 if peg <= 1 else 4 if peg <= 1.5 else 2 if peg <= 2.5 else 1 if peg <= 4 else 0, 5)
    fpe = m.get("fwd_pe")
    if pe is None or fpe is None: vb.miss(3)
    else: vb.add(3 if fpe < pe * 0.85 else 2 if fpe < pe else 0, 3)
    m["peg"] = peg
    why.append(f"Valuation: P/E {fmt(pe, 0)}, forward P/E {fmt(fpe, 0)}, PEG {fmt(peg, 1)}.")
    if pe is not None and pe > 80:
        flags.append("Very expensive (P/E > 80)")

    # ---- Price setup (10)
    sb = Bucket(W_SETUP)
    a200 = m.get("above200")
    if a200 is None: sb.miss(3)
    else: sb.add(3 if a200 else 0, 3)
    d = m.get("dist_high")
    if d is None: sb.miss(5)
    else: sb.add(3 if d >= -8 else 5 if d >= -30 else 1, 5)
    r6 = m.get("ret6m")
    if r6 is None: sb.miss(2)
    else: sb.add(2 if r6 > 0 else 0, 2)
    why.append(f"Price: {fmt(d, 0, '%')} from 52-week high; "
               f"{'above' if a200 else 'below' if a200 is not None else '?'} 200-day average; 6-month return {fmt(r6, 0, '%')}.")
    if a200 is False and r6 is not None and r6 < -20:
        flags.append("In a downtrend (below 200-DMA, 6m < -20%)")

    # ---- Manual overlay (from watchlist.csv)
    manual_missing = news_score is None or sector_score is None
    ns = W_NEWS * 0.5 if news_score is None else max(0, min(W_NEWS, news_score))
    ss = W_SECTOR * 0.5 if sector_score is None else max(0, min(W_SECTOR, sector_score))

    quant = g.score + qb.score + vb.score + sb.score
    total = quant + ns + ss
    possible = g.possible + qb.possible + vb.possible + sb.possible
    avail = g.avail + qb.avail + vb.avail + sb.avail
    coverage = avail / possible * 100 if possible else 0
    if coverage < 60:
        flags.append(f"Low data coverage ({coverage:.0f}%) - verify manually")

    # ---- Verdict
    growth_pct = g.earned / g.avail if g.avail else 0
    is_dip = d is not None and d <= -15
    if severe:
        verdict = "AVOID - thesis check needed"
    elif is_dip and growth_pct >= 0.7 and not flags:
        verdict = "DIP WITH INTACT FUNDAMENTALS - small tranche"
    elif total >= 80 and not flags:
        verdict = "STRONG CANDIDATE - accumulate in tranches"
    elif total >= 70 and len(flags) <= 1:
        verdict = "GOOD - buy small / add on dips"
    elif total >= 60:
        verdict = "WATCH - wait for better price or data"
    else:
        verdict = "AVOID FOR NOW"

    if verdict.startswith("AVOID"):
        entry = "Do not enter now. Revisit after the next quarterly result or once the red flags clear."
    elif d is not None and d >= -5:
        entry = "Near 52-week high: prefer waiting for a 5-8% pullback or buy only 1/3 now."
    elif d is not None and d <= -15:
        entry = "Well below high: start with 1/3, add on stabilisation (higher low / result confirmation)."
    else:
        entry = "Build in 2-3 tranches over a few weeks."

    out = dict(m)
    out.update({
        "scores": {"growth": round(g.score, 1), "quality": round(qb.score, 1), "valuation": round(vb.score, 1),
                   "setup": round(sb.score, 1), "news": round(ns, 1), "sector": round(ss, 1)},
        "quant_score": round(quant, 1),
        "total_score": round(total),
        "coverage_pct": round(coverage),
        "manual_overlay_missing": manual_missing,
        "flags": flags,
        "verdict": verdict,
        "entry_note": entry,
        "why": why,
    })
    return out


# --------------------------------------------------------------------------- run
def load_watchlist(only=None):
    items = []
    if WATCHLIST.exists():
        with WATCHLIST.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                sym = (r.get("symbol") or "").strip().upper()
                if sym:
                    items.append({
                        "symbol": sym,
                        "news": safe(r.get("news_score")),
                        "sector": safe(r.get("sector_score")),
                        "note": (r.get("note") or "").strip(),
                        "override": {k: safe(r.get(k)) for k in ("q_rev_yoy", "q_pat_yoy", "roe", "de")
                                     if safe(r.get(k)) is not None},
                    })
    if only:
        known = {i["symbol"]: i for i in items}
        items = [known.get(s.upper(), {"symbol": s.upper(), "news": None, "sector": None, "note": "", "override": {}}) for s in only]
    return items


def main(argv):
    items = load_watchlist(argv[1:] or None)
    if not items:
        print("watchlist.csv is empty or missing. Add symbols (see the template).")
        return 1
    results = []
    for it in items:
        print(f"Scanning {it['symbol']} ...", flush=True)
        try:
            m = fetch_metrics(it["symbol"])
            fill_from_screener(m, it["symbol"], skip=it.get("override", {}))  # auto-fill gaps
            m.update(it.get("override", {}))  # numbers you typed in watchlist.csv win over yfinance
            res = score_stock(m, it["news"], it["sector"])
            res["note"] = it["note"]
            results.append(res)
        except Exception as e:  # one bad symbol must not stop the run
            print(f"  ! {it['symbol']} failed: {e}")
        time.sleep(1.0)

    results.sort(key=lambda r: r["total_score"], reverse=True)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "note": "Research support only. Final decision is the investor's.",
               "stocks": results}
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Symbol", "Sector", "Score/100", "Verdict", "Price", "From 52W high %", "P/E", "PEG",
                    "Qtr rev YoY %", "Qtr profit YoY %", "ROE %", "Red flags", "Data coverage %"])
        for r in results:
            w.writerow([r["symbol"], r["sector"], r["total_score"], r["verdict"], fmt(r["price"], 1),
                        fmt(r["dist_high"], 0), fmt(r["pe"], 0), fmt(r.get("peg"), 1),
                        fmt(r["q_rev_yoy"], 0), fmt(r["q_pat_yoy"], 0), fmt(r["roe"], 0),
                        "; ".join(r["flags"]), r["coverage_pct"]])

    print("\n" + "=" * 78)
    for r in results:
        print(f"{r['symbol']:<12} {r['total_score']:>3}/100  {r['verdict']}")
        print(f"   Sector: {r['sector']}   Coverage: {r['coverage_pct']}%"
              + ("   (news/sector scores missing - neutral used)" if r["manual_overlay_missing"] else ""))
        for line in r["why"]:
            print("   - " + line)
        if r["flags"]:
            print("   ! RED FLAGS: " + "; ".join(r["flags"]))
        print("   > " + r["entry_note"])
        print()
    print(f"Saved {OUT_JSON} and {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
