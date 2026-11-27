"""
update_company.py  -  builds company_brief.json for the "Company brief" part of the dashboard.

For every stock listed in company_watch.txt (plus every symbol found in research.json) it collects:
  * what the company does (Yahoo Finance business summary, industry, sector, website, size)
  * the last 90 days of headlines from Google News
  * a plain-language tag on each headline: order win, results, tariffs/exports, expansion, regulatory, etc.
  * for headlines that quote a rupee amount: how big that deal is compared with the company's market cap

It does NOT invent export/import numbers. Those go into company_notes.json, which you (or Claude) fill in
after reading the annual report or investor presentation.

Run on GitHub (workflow company.yml) or on your laptop:   python update_company.py
Optional:  python update_company.py AVANTIFEED KPIL TITAN     (only these symbols)
"""
import json, os, re, sys, time, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "company_brief.json")
NEWS_DAYS, MAX_NEWS = 90, 12

# ---------- headline reading (plain keyword rules; they are hints, not facts) ----------
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

# ---------- company facts ----------
def fetch_profile(sym):
    import yfinance as yf
    info = yf.Ticker(sym + ".NS").info or {}
    mc = info.get("marketCap")
    return {
        "n": info.get("longName") or info.get("shortName") or sym,
        "summary": (info.get("longBusinessSummary") or "").strip(),
        "industry": info.get("industry") or "", "sector": info.get("sector") or "",
        "website": info.get("website") or "", "employees": info.get("fullTimeEmployees"),
        "city": info.get("city") or "", "mcap_cr": round(mc / 1e7) if mc else None,
    }

def search_name(sym, prof, override=None):
    if override: return override
    n = re.sub(r"\b(limited|ltd\.?|corporation|corp\.?)\b", "", prof.get("n") or sym, flags=re.I).strip(" .,")
    return n or sym

def symbols():
    args = [a.upper() for a in sys.argv[1:]]
    if args: return [(a, None) for a in args]
    out, seen = [], set()
    p = os.path.join(HERE, "company_watch.txt")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.split("#")[0].strip()
            if not line: continue
            parts = [x.strip() for x in line.split(",", 1)]
            s = parts[0].upper()
            if s not in seen: seen.add(s); out.append((s, parts[1] if len(parts) > 1 and parts[1] else None))
    p = os.path.join(HERE, "research.json")
    if os.path.exists(p):
        try:
            for r in json.load(open(p, encoding="utf-8")).get("stocks", []):
                s = (r.get("symbol") or "").upper()
                if s and s not in seen: seen.add(s); out.append((s, None))
        except Exception as e: print("research.json skipped:", e)
    return out

def main():
    old = {}
    if os.path.exists(OUT):
        try: old = json.load(open(OUT, encoding="utf-8")).get("stocks", {})
        except Exception: pass
    res, todo = dict(old), symbols()
    print(len(todo), "stocks")
    for sym, nm in todo:
        try:
            prof = fetch_profile(sym)
            if not prof["summary"] and sym in old: prof["summary"] = old[sym].get("summary", "")
            news = tag_news(fetch_news(search_name(sym, prof, nm)), prof.get("mcap_cr"))
            prof["news"] = news
            res[sym] = prof
            print("ok ", sym, len(news), "headlines")
        except Exception as e:
            print("FAILED", sym, e)          # keep the previous entry, if any
        time.sleep(1.5)
    json.dump({"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "stocks": res},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("wrote", OUT)

if __name__ == "__main__":
    main()
