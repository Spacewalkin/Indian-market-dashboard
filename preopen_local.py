"""Pre-open numbers from YOUR OWN computer (NSE blocks GitHub's servers but normally allows a home connection).
Run it every weekday at about 9:10 AM IST (see the steps in the chat). It asks NSE for the pre-open prices of the
Nifty 500 every 30 seconds until 9:16, and saves them to a small file on a branch called "preopen" in your
GitHub repo. The website reads that file. Needs: pip install yfinance pandas requests
Put your GitHub token (Contents: read and write) in a file called token.txt next to this script."""
import base64, json, os, sys, time
import requests
import update_data as u  # same folder: reuses the exact NSE code and the gap thresholds of the website

REPO = "Spacewalkin/Indian-market-dashboard"
BRANCH = "preopen"
API = "https://api.github.com/repos/" + REPO


def headers():
    t = os.environ.get("GITHUB_TOKEN")
    if not t:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
        t = open(p).read().strip() if os.path.exists(p) else None
    if not t:
        sys.exit("No token: put your GitHub token in token.txt next to this script.")
    return {"Authorization": "Bearer " + t, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def ensure_branch(h):
    if requests.get(f"{API}/git/ref/heads/{BRANCH}", headers=h, timeout=30).status_code == 200:
        return
    default = requests.get(API, headers=h, timeout=30).json().get("default_branch", "main")
    sha = requests.get(f"{API}/git/ref/heads/{default}", headers=h, timeout=30).json()["object"]["sha"]
    r = requests.post(f"{API}/git/refs", headers=h, json={"ref": f"refs/heads/{BRANCH}", "sha": sha}, timeout=30)
    r.raise_for_status()


def upload(h, payload):
    r = requests.get(f"{API}/contents/preopen.json", headers=h, params={"ref": BRANCH}, timeout=30)
    body = {"message": "pre-open " + payload["time"], "branch": BRANCH,
            "content": base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()}
    if r.status_code == 200:
        body["sha"] = r.json()["sha"]
    r = requests.put(f"{API}/contents/preopen.json", headers=h, json=body, timeout=30)
    r.raise_for_status()


def main():
    h = headers()
    ensure_branch(h)
    live = requests.get(f"https://raw.githubusercontent.com/{REPO}/data/data.json?t={int(time.time())}", timeout=30).json()
    universe = list((live.get("stocks") or {}).keys())
    picks = [c["s"] for c in (live.get("swing") or [])[:15] if isinstance(c, dict) and c.get("s")]
    print(f"{len(universe)} stocks and {len(picks)} picks loaded. Waiting for NSE pre-open numbers...")
    while True:
        now = u.ist_now()
        if (now.hour, now.minute) >= (9, 16):
            print("Done: the market is about to open.")
            return
        nse = u.fetch_nse_preopen(picks, universe=universe)
        if nse and (nse.get("breadth") or nse.get("nifty")):
            b, nf = nse.get("breadth"), nse.get("nifty") or {}
            med = b["median"] if b else nf.get("pc")
            payload = {"date": nse["date"], "time": nse["time"], "nse": nse, "src": "local",
                       "key": u.stock_gap_key(med) if med is not None else None,
                       "basis": "stocks" if b else "nifty"}
            try:
                upload(h, payload)
                print(now.strftime("%H:%M:%S"), "saved:", payload["key"], payload["basis"], (b or {}).get("n"))
            except Exception as e:
                print("upload failed:", e)
        else:
            print(now.strftime("%H:%M:%S"), "no NSE numbers yet")
        time.sleep(30)


if __name__ == "__main__":
    main()
