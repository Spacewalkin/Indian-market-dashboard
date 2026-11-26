"""
discover_company.py - finds companies in the news BY ITSELF, then builds company.json.

What it does each run:
  1. Reads EQUITY_L.csv (the NSE list of all listed companies).
  2. Searches Google News for deal / order / results / expansion / regulatory headlines.
  3. Matches company names in those headlines against the NSE list.
  4. Picks the most-mentioned companies (plus anything in company_watch.txt).
  5. Runs update_company.py for just those companies -> company.json
  6. Saves what it found in discovered.json

Run on GitHub (workflow company.yml) or on your laptop:  python discover_company.py
Test without building company.json:                       python discover_company.py --no-run
"""
import csv, json, os, re, subprocess, sys, time, datetime as dt
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LIST_CSV = os.path.join(HERE, "EQUITY_L.csv")
WATCH = os.path.join(HERE, "company_watch.txt")
OUT = os.path.join(HERE, "discovered.json")

DAYS = 3            # how many days of news to scan each run
MAX_STOCKS = 60     # most companies to build a brief for each run
MAX_HEADLINES = 8   # headlines kept per company in discovered.json

# Broad searches. Add or remove phrases freely.
QUERIES = [
    "bags order India company", "wins contract India company", "order worth crore",
    "letter of intent crore India", "capacity expansion India company",
    "export order India company", "tariff exports India company",
    "quarterly results profit India company", "acquisition crore India company",
    "board approves fund raise India", "block deal stake India company",
    "USFDA approval India company", "SEBI order company India",
    "dividend bonus split India company", "new plant commissioning India",
]

# Words dropped from company names so "Titan Company Ltd" can match "Titan"
DROP = {"limited", "ltd", "pvt", "private", "the"}
GENERIC = {"company", "corporation", "corp", "industries", "industry", "enterprises",
           "international", "holdings", "group", "services", "systems", "co", "and", "of"}
# One-word names too common in normal English to trust on their own
COMMON = {"india", "indian", "global", "bank", "power", "steel", "gold", "future",
          "universal", "national", "general", "united", "digital", "texture", "premier",
          "model", "supreme", "prime", "royal", "star", "sun", "rise", "world", "asian",
          "bharat", "state", "union", "central", "capital", "finance", "energy",
          "infra", "tech", "motors", "foods", "health", "life", "media", "retail"}


def clean(text):
    text = text.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def load_companies():
    if not os.path.exists(LIST_CSV):
        sys.exit("EQUITY_L.csv not found. Upload it to the same folder as this script.")
    companies = {}
    with open(LIST_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            if row.get("SERIES") != "EQ":
                continue
            companies[row["SYMBOL"]] = row["NAME OF COMPANY"]
    return companies


def build_aliases(companies):
    """alias text -> symbol. Ambiguous short aliases are dropped."""
    full_alias, short_alias, seen = {}, {}, {}
    for sym, name in companies.items():
        tokens = [t for t in clean(name).split() if t not in DROP]
        full = " ".join(tokens)
        short = " ".join(t for t in tokens if t not in GENERIC)
        for alias, bucket in ((full, full_alias), (short, short_alias)):
            if not alias:
                continue
            n = len(alias.split())
            if n == 1 and (len(alias) < 5 or alias in COMMON):
                continue
            if n > 1 and len(alias) < 6:
                continue
            seen.setdefault(alias, set()).add(sym)
            bucket[alias] = sym
    aliases = {}
    for alias, sym in short_alias.items():
        if len(seen[alias]) == 1:
            aliases[alias] = sym
    for alias, sym in full_alias.items():      # full names win
        if len(seen[alias]) == 1:
            aliases[alias] = sym
    return aliases


def find_companies(title, aliases, symbols):
    found = set()
    tokens = clean(title).split()
    for n in range(1, 6):
        for i in range(len(tokens) - n + 1):
            sym = aliases.get(" ".join(tokens[i:i + n]))
            if sym:
                found.add(sym)
    for word in re.findall(r"\b[A-Z][A-Z0-9&-]{4,}\b", title):   # e.g. AVANTIFEED
        if word in symbols:
            found.add(word)
    return found


def search_news(query):
    url = ("https://news.google.com/rss/search?q=" + quote(f"{query} when:{DAYS}d")
           + "&hl=en-IN&gl=IN&ceid=IN:en")
    items = []
    for it in ET.fromstring(fetch(url)).iter("item"):
        title = (it.findtext("title") or "").strip()
        title = title.rsplit(" - ", 1)[0]          # drop " - Source name"
        try:
            date = parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
        except Exception:
            date = ""
        items.append({"title": title, "link": it.findtext("link") or "", "date": date})
    return items


def main():
    run_after = "--no-run" not in sys.argv
    companies = load_companies()
    aliases = build_aliases(companies)
    print(f"{len(companies)} companies loaded, {len(aliases)} name patterns ready")

    hits = {}
    for q in QUERIES:
        try:
            items = search_news(q)
        except Exception as e:
            print(f"  search failed for '{q}': {e}")
            continue
        print(f"  '{q}': {len(items)} headlines")
               for item in items:
            found = find_companies(item["title"], aliases, companies)
            if len(found) > 3 or re.search(r"stocks? to watch|stocks? in news|stocks? in focus|buzzing|top gainers|top losers|market wrap", item["title"], re.I):
                continue
            for sym in found:
                lst = hits.setdefault(sym, [])
                if all(h["title"] != item["title"] for h in lst):
                    lst.append({**item, "query": q})
        time.sleep(1.5)

    ranked = sorted(hits, key=lambda s: len(hits[s]), reverse=True)[:MAX_STOCKS]

    watch = []
    if os.path.exists(WATCH):
        with open(WATCH, encoding="utf-8") as f:
            watch = [w.strip().upper() for w in f
                     if w.strip() and not w.startswith("#") and w.strip().upper() in companies]
    symbols = list(dict.fromkeys(ranked + watch))

    result = {
        "generated": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "days_scanned": DAYS,
        "companies": [
            {"symbol": s, "name": companies[s], "mentions": len(hits.get(s, [])),
             "headlines": hits.get(s, [])[:MAX_HEADLINES]}
            for s in symbols
        ],
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Found {len(symbols)} companies -> discovered.json")

    if not symbols:
        print("Nothing found, leaving company.json unchanged.")
        return
    if run_after:
        subprocess.run([sys.executable, os.path.join(HERE, "update_company.py"), *symbols],
                       check=True)


if __name__ == "__main__":
    main()
