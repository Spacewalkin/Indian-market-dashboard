"""
update_company.py  -  builds the Company feed for the dashboard, with NO manual stock list.

Input  : EQUITY_L.csv  (the NSE "list of securities" you downloaded; put it in the repo's main folder)
Output : company.json      what each active company does + its tagged headlines (last 90 days)
         nse_names.json    symbol -> company name for every NSE company (powers the site search)

How it finds companies by itself
  1. It reads about 20 Google News searches for the last 3 days (order wins, USFDA nods, tariffs, results,
     acquisitions, capex ...) and matches every headline to an NSE company by name.
  2. Companies in the news today get a fuller refresh (company profile + their own 90-day headlines).
  3. Every run it also refreshes the 40 companies not checked for the longest time, so the whole NSE list
     is covered over a few weeks.
  4. company_watch.txt is optional: anything listed there is always refreshed first.

Run on GitHub (workflow company.yml) or on your laptop:   python update_company.py
"""
import csv, json, os, re, sys, time, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
OUT, NAMES_OUT = os.path.join(HERE, "company.json"), os.path.join(HERE, "nse_names.json")
NEWS_DAYS, MAX_NEWS = 90, 6
TOP_ACTIVE, ROTATE, KEEP_MAX, MAX_SECONDS, PAUSE = 70, 120, 650, 1700, 1.0

DISCOVERY = [
    "bags order worth crore", "wins order worth crore", "secures contract worth crore", "receives order India company",
    "letter of intent LoI crore", "defence order India company", "railway order company crore", "export order India company",
    "USFDA approval", "gets USFDA nod", "USFDA warning letter OR observations", "anti-dumping duty India",
    "tariff Indian exporters shares", "capacity expansion capex crore", "acquisition announces India company",
    "Q2 results net profit jumps", "Q2 results profit falls", "shares surge on order", "block deal stake sale",
    "buyback record date", "strategic partnership MoU India company", "new plant commissioned India",
]

POS = r"\b(jump|jumps|surge|surges|soar|soars|rally|rallies|gain|gains|rise|rises|rose|record|strong|beat|beats|upgrade|upgrades|bags|wins|secures|secured|awarded|approval|approved|nod|relief|boost|boosts|growth|grows|expands|receives|gets)\b"
NEG = r"\b(fall|falls|fell|drop|drops|plunge|plunges|slump|slumps|crash|decline|declines|dip|dips|tumble|tumbles|miss|misses|downgrade|downgrades|loss|losses|halves|halve|stumble|stumbles|penalty|probe|fraud|raid|ban|banned|imposes|imposed|weak|lowers|slashes|resigns|default|rejects|rejected|delay|delays|denied|withdraws|warning)\b"

RULES = [
    ("Market roundup", r"\b(stocks to watch|stocks in focus|stocks to buy|stock picks|buzzing stocks|top gainers|top losers|market wrap|closing bell|opening bell|stocks that moved)\b", 0,
     "A list article that mentions many companies. Open it and read only the paragraph about this one."),
    ("Regulatory or legal risk", r"\b(sebi|probe|fraud|raid|show.cause|nclt|insolvency|default|penalty|court|enforcement directorate|cbi|warning letter|form 483|import alert|observations|inspection)\b", -1,
     "Regulatory or legal news can hurt trust and sometimes cash flow. Read the filing for the amount at stake and whether it is a one-off."),
    ("Product approval", r"\b(usfda|us fda|u\.s\. fda|fda|cdsco|dcgi|ema|approval|approves|approved|nod|tentative approval)\b", 1,
     "A regulator's approval lets the company sell that product in that market. What it is worth depends on the market size and how many rivals already sell it."),
    ("Order or contract win", r"\b(order|orders|bags|wins|win|secures|secured|awarded|contract|loi|letter of intent|work order|tender|empanel|mou|partnership|supply agreement)\b", 1,
     "A new order adds to the order book and gives revenue visibility. Check its size against yearly sales and how long it takes to deliver."),
    ("Exports, imports and tariffs", r"\b(tariff|tariffs|anti.dumping|countervailing|duty|duties|export|exports|exporter|import|imports|trade deal|fta|shipment|freight)\b", 0,
     "Trade rules change what an exporter can charge and sell. Lower duties usually help volumes and margins; higher duties or freight hurt them."),
    ("Results", r"\b(q[1-4]|results|profit|pat|ebitda|revenue|earnings|net profit|margin)\b", 0,
     "Results show whether sales and margins are really growing. Compare with the same quarter last year and check the margin, not only the profit."),
    ("Expansion or capex", r"\b(capacity|plant|capex|expansion|new facility|acquire|acquires|acquisition|stake|invest|investment|merger|demerger|launch|launches)\b", 1,
     "Expansion can lift future sales, but it costs cash first. Check how it is funded and when it starts contributing."),
    ("Broker view", r"\b(target price|target|rating|brokerage|buy|sell|overweight|underweight|initiates coverage)\b", 0,
     "A broker view is an opinion. Look at the reasons given, not the target price alone."),
    ("Dividend or buyback", r"\b(dividend|buyback|bonus|split)\b", 1,
     "Cash returned to shareholders signals confidence, but it says little about future growth."),
]

CR = {"crore": 1.0, "cr": 1.0, "lakh crore": 100000.0, "billion": 100.0, "bn": 100.0, "million": 0.1, "mn": 0.1}
AMT = re.compile(r"(?:₹|rs\.?|inr|\$|us\$)\s?([\d,]+(?:\.\d+)?)\s?(lakh crore|crore|cr|billion|bn|million|mn)\b", re.I)

def clean_title(t):
    return re.sub(r"\s+-\s+[^-]{2,40}$", "", t or "").strip()

def read_headline(title):
    """Return (category, tone, why) for one headline. tone: 1 positive, -1 negative, 0 neutral/unclear."""
    low = (title or "").lower()
    pos, neg = bool(re.search(POS, low)), bool(re.search(NEG, low))
    for cat, pat, base, why in RULES:
        if not re.search(pat, low): continue
        tone = 1 if pos and not neg else -1 if neg and not pos else 0
        if cat == "Market roundup": tone = 0
        elif cat == "Regulatory or legal risk": tone = -1
        elif cat in ("Product approval", "Dividend or buyback"): tone = -1 if neg and not pos else 1
        elif cat == "Order or contract win" and tone == 0: tone = 1
        elif cat == "Exports, imports and tariffs":
            if re.search(r"\b(tariff|tariffs|duty|duties|anti.dumping|countervailing|trade deal|fta)\b", low):
                why = ("Lower or removed duties help an exporter: buyers pay less, so volumes and margins can recover." if tone > 0 else
                       "Higher duties hurt an exporter: buyers pay more or switch supplier, so volumes and margins can fall." if tone < 0 else why)
            else:
                tone = 0
        elif cat == "Results":
            why = ("Better results than before. Check whether the margin also improved and whether profit is from the core business." if tone > 0 else
                   "Weaker results. Check whether it is a one-off (an exceptional item) or a lasting margin problem." if tone < 0 else why)
        return cat, tone, why
    return "General news", 0, "No clear business effect found in the headline. Open it to see whether it changes sales, costs or risk."

def deal_value_cr(title):
    """Rupee amount quoted in a headline, in crore. Dollar amounts are skipped (no safe conversion)."""
    m = AMT.search(title or "")
    if not m or "$" in m.group(0): return None
    return round(float(m.group(1).replace(",", "")) * CR[m.group(2).lower()], 1)

# ---------- Google News ----------
def parse_rss(xml_text, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    root, out, seen = ET.fromstring(xml_text), [], set()
    for it in root.iter("item"):
        t, l, d = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip(), it.findtext("pubDate")
        src = it.findtext("source") or ""
        try: when = parsedate_to_datetime(d).astimezone(dt.timezone.utc)
        except Exception: continue
        if (now - when).days > NEWS_DAYS: continue
        ct = clean_title(t); key = re.sub(r"\W+", "", ct.lower())[:60]
        if not ct or key in seen: continue
        seen.add(key); out.append({"t": ct, "l": l, "p": src, "d": when.date().isoformat()})
    out.sort(key=lambda x: x["d"], reverse=True)
    return out

def fetch_news(name):
    import requests
    q = quote('"%s" when:%dd' % (name, NEWS_DAYS))
    r = requests.get("https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en" % q, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return parse_rss(r.text)

def tag_news(items, mcap_cr):
    for n in items:
        n["cat"], n["tone"], n["why"] = read_headline(n["t"])
        v = deal_value_cr(n["t"])
        if v and n["cat"] in ("Order or contract win", "Expansion or capex"):
            n["val_cr"] = v
            if mcap_cr: n["pct_mcap"] = round(v / mcap_cr * 100, 1)
    return items[:MAX_NEWS]


GENERIC = {"limited", "ltd", "pvt", "private", "india", "international", "industries", "industry", "corporation", "corp", "company",
           "co", "enterprises", "holdings", "group", "global", "the", "of", "and"}
COMMON = {"reliance", "india", "indian", "global", "bharat", "national", "united", "state", "first", "new", "power", "bank", "finance", "capital",
          "steel", "energy", "motors", "foods", "pharma", "tech", "systems", "services", "infra", "textiles", "real", "estate"}

def norm(t):
    t = (t or "").lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", t)).strip()

def load_nse():
    path = next((p for p in (os.path.join(HERE, "EQUITY_L.csv"), os.path.join(HERE, "equity_l.csv")) if os.path.exists(p)), None)
    if not path:
        sys.exit("EQUITY_L.csv not found. Download it from the NSE website (list of securities) and put it in the repo's main folder.")
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
            sym, name, ser = r.get("SYMBOL", ""), r.get("NAME OF COMPANY", ""), r.get("SERIES", "")
            if sym and name and ser in ("EQ", "BE", "BZ", "SM", "ST"): out[sym] = name
    return out

def aliases_for(name):
    k = norm(name).split()
    cands = []
    if k: cands.append(" ".join(k))
    kk = list(k)
    while kk and kk[-1] in GENERIC: kk.pop()
    if len(kk) >= 1: cands.append(" ".join(kk))
    return [c for c in dict.fromkeys(cands) if len(c) >= 5]

ABBR = {"l and t": "LT", "m and m": "M&M", "hul": "HINDUNILVR", "sbi": "SBIN", "hdfc bank": "HDFCBANK", "icici bank": "ICICIBANK",
        "tcs": "TCS", "bpcl": "BPCL", "ongc": "ONGC", "ntpc": "NTPC", "bhel": "BHEL", "hal": "HAL", "sail": "SAIL"}
NOT_SYMBOLS = {"INDIA", "BANK", "GOLD", "LIMITED", "NSE", "BSE", "SEBI", "FDA", "USFDA", "GDP", "RBI", "IPO", "FII", "DII", "ETF", "SIP", "NIFTY",
               "SENSEX", "CRORE", "STOCK", "STOCKS", "SHARE", "SHARES", "ORDER", "ORDERS", "WINS", "BAGS", "DEAL", "NEWS", "LIVE", "TODAY", "WATCH"}
_SYMS = set()

def build_index(names):
    global _SYMS
    _SYMS = set(names)
    owners = {}
    for sym, nm in names.items():
        for a in aliases_for(nm): owners.setdefault(a, set()).add(sym)
    idx = {a: next(iter(s)) for a, s in owners.items() if len(s) == 1 and a not in COMMON}
    for a, sym in ABBR.items():
        if sym in names: idx[a] = sym
    return idx

def match_title(title, index):
    t = " " + norm(title) + " "
    hits = {a: s for a, s in index.items() if (" " + a + " ") in t}
    for a in list(hits):
        if any(a != b and a in b for b in hits): del hits[a]      # keep the longest name only
    found = set(hits.values())
    for tok in re.findall(r"\b[A-Z][A-Z&]{3,}\b", title or ""):      # a company ticker written in capitals, e.g. "KPIL bags order"
        if tok in _SYMS and tok not in NOT_SYMBOLS and tok.lower() not in COMMON: found.add(tok)
    return sorted(found)

def short_name(sym, names):
    al = aliases_for(names[sym]); return al[-1] if al else norm(names[sym])

# ---------- Google News ----------
def parse_rss(xml_text, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    out, seen = [], set()
    for it in ET.fromstring(xml_text).iter("item"):
        t, l, d = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip(), it.findtext("pubDate")
        try: when = parsedate_to_datetime(d).astimezone(dt.timezone.utc)
        except Exception: continue
        if (now - when).days > NEWS_DAYS: continue
        ct = clean_title(t); key = re.sub(r"\W+", "", ct.lower())[:60]
        if not ct or key in seen: continue
        seen.add(key); out.append({"t": ct, "l": l, "p": it.findtext("source") or "", "d": when.date().isoformat()})
    out.sort(key=lambda x: x["d"], reverse=True)
    return out

def rss(query, days):
    import requests
    q = quote("%s when:%dd" % (query, days))
    r = requests.get("https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en" % q, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return parse_rss(r.text)

def fetch_profile(sym):
    import yfinance as yf
    info = yf.Ticker(sym + ".NS").info or {}
    mc = info.get("marketCap")
    return {"summary": (info.get("longBusinessSummary") or "").strip()[:600], "industry": info.get("industry") or "",
            "sector": info.get("sector") or "", "website": info.get("website") or "", "employees": info.get("fullTimeEmployees"),
            "city": info.get("city") or "", "mcap_cr": round(mc / 1e7) if mc else None}

def tag(items, mcap):
    for n in items:
        n["cat"], n["tone"], n["why"] = read_headline(n["t"])
        v = deal_value_cr(n["t"])
        if v and n["cat"] in ("Order or contract win", "Expansion or capex"):
            n["val_cr"] = v
            if mcap: n["pct_mcap"] = round(v / mcap * 100, 1)
    return items

def optional_list(fn):
    p = os.path.join(HERE, fn)
    return [l.split("#")[0].split(",")[0].strip().upper() for l in open(p, encoding="utf-8") if l.split("#")[0].strip()] if os.path.exists(p) else []

def nifty500_symbols():
    """Symbols the dashboard already tracks (its Nifty 500 list), read from data.json."""
    try:
        import requests
        r = requests.get("https://raw.githubusercontent.com/Spacewalkin/Indian-market-dashboard/data/data.json", timeout=30)
        r.raise_for_status()
        return list(json.loads(re.sub(r"\bNaN\b", "null", re.sub(r"-?\bInfinity\b", "null", r.text))).get("stocks", {}))
    except Exception as e:
        print("could not read data.json:", e); return []

def position_symbols():
    p = os.path.join(HERE, "positions.csv")
    if not os.path.exists(p): return []
    try:
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        key = next((k for k in (rows[0] if rows else {}) if k and k.strip().lower() in ("symbol", "sym", "stock", "ticker")), None)
        return [(r.get(key) or "").strip().upper() for r in rows] if key else []
    except Exception: return []

def research_symbols():
    p = os.path.join(HERE, "research.json")
    try: return [(r.get("symbol") or "").upper() for r in json.load(open(p, encoding="utf-8")).get("stocks", [])]
    except Exception: return []

def main():
    t0 = time.time(); today = dt.date.today().isoformat()
    names = load_nse(); index = build_index(names)
    print(len(names), "NSE companies;", len(index), "name keys")
    json.dump(names, open(NAMES_OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    old = {}
    if os.path.exists(OUT):
        try: old = json.load(open(OUT, encoding="utf-8")).get("stocks", {})
        except Exception: pass
    stocks = {k: v for k, v in old.items() if k in names}

    # 1. discovery: which companies are in the news right now?
    found = {}
    for q in DISCOVERY:
        if time.time() - t0 > MAX_SECONDS / 2: break
        try:
            for h in rss(q, 3):
                m = match_title(h["t"], index)
                if 1 <= len(m) <= 3:
                    for s in m: found.setdefault(s, {})[h["t"]] = h
        except Exception as e: print("discovery failed:", q, e)
        time.sleep(PAUSE)
    print(len(found), "companies found in today's news")

    # 2. who to refresh: watch list, news-active companies, then the least recently checked
    asked = [a.upper() for a in sys.argv[1:] if a.upper() in names]
    watch = list(dict.fromkeys(asked + [s for s in optional_list("company_watch.txt") + position_symbols() + research_symbols() if s in names]))
    n500 = set(nifty500_symbols())
    active = [s for s, _ in sorted(found.items(), key=lambda kv: -len(kv[1]))][:TOP_ACTIVE]
    stale = sorted((s for s in names if s not in watch and s not in active), key=lambda s: (s not in n500, stocks.get(s, {}).get("checked", "")))[:ROTATE]
    queue = list(dict.fromkeys(watch + active + stale))
    print("refreshing", len(queue), "companies")

    for sym in queue:
        if time.time() - t0 > MAX_SECONDS: print("time limit reached"); break
        e = stocks.get(sym, {"n": names[sym]})
        try:
            if not e.get("summary") or e.get("checked", "") < (dt.date.today() - dt.timedelta(days=30)).isoformat():
                e.update(fetch_profile(sym))
            nm, got = short_name(sym, names), {}
            for h in found.get(sym, {}).values(): got[h["t"]] = h
            try:
                for h in rss('"%s" (shares OR stock OR NSE)' % nm, NEWS_DAYS):
                    if sym in match_title(h["t"], index): got.setdefault(h["t"], h)
            except Exception as ex: print("news failed", sym, ex)
            items = sorted(got.values(), key=lambda x: x["d"], reverse=True)[:MAX_NEWS]
            e["news"] = tag(items, e.get("mcap_cr")); e["n"] = e.get("n") or names[sym]; e["checked"] = today
            stocks[sym] = e; print("ok ", sym, len(e["news"]))
        except Exception as ex: print("FAILED", sym, ex)
        time.sleep(PAUSE)

    cutoff = (dt.date.today() - dt.timedelta(days=NEWS_DAYS)).isoformat()
    for e in stocks.values(): e["news"] = [n for n in e.get("news", []) if n["d"] >= cutoff]
    keep = sorted(stocks.items(), key=lambda kv: (kv[0] in watch, max([n["d"] for n in kv[1]["news"]] or [""])), reverse=True)[:KEEP_MAX]
    json.dump({"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "stocks": dict(keep)},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("wrote", OUT, len(keep), "companies in", int(time.time() - t0), "s")

if __name__ == "__main__":
    main()
