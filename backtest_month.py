"""
MONTH BACKTEST - how would the system have done in ONE month?

Run:   python backtest_month.py 2025-07     -> every trade for July 2025
       python backtest_month.py all         -> a summary table of every month
       python backtest_month.py 2025-07 none   -> no limit on open trades (or give a number, e.g. 10)

It starts the month with NO open trades and takes only signals that trigger inside that month,
then follows each trade to its real exit (even if that is after the month ends), and also shows
where it stood at the last day of the month (open trades are valued at that day's close).

THE SYSTEM (same rules as the site)
  Market filter : new trades only if more than 50% of Nifty 500 stocks closed above their
                  50-day average on the day before the entry.
  Signal        : fresh 52-week-high breakout (close above the highest high of the previous 252
                  sessions, first day only) on volume >= 1.5x the 20-day average.
  Entry         : first session within 6 sessions after the breakout candle in which price
                  trades above the previous session's high.
  Stop          : below the breakout candle's low (trade skipped if that is more than 8% away).
  Size          : quantity so that a stop-out costs about Rs 200.
  Slots         : 6 open trades by default (set MAX_POSITIONS below, or add 'none' or a number
                  after the month); with more signals than free slots, highest volume first.
  Exit          : daily close below the 30 EMA -> sell at the next open. No profit target.
                  A trade is also closed after 60 sessions.
  Costs         : 0.30% round trip (brokerage, taxes, slippage) taken off every trade.

Needs the data folder from backtest.py (data/prices.pkl). If it is missing, it downloads it.
"""

import io
import os
import sys

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ----------------------------- SETTINGS --------------------------------
START = "2015-06-01"
RISK_PER_TRADE = 200
LOOKBACK = 252
VOL_MULT = 1.5
MAX_RISK_PCT = 8.0
MAX_HOLD_DAYS = 60
COST_PCT = 0.30
EMA_EXIT = 30
MAX_POSITIONS = 6
MAX_DELAY = 6              # entry allowed up to 6 sessions after the breakout candle
BREADTH_MIN = 50.0         # % of stocks above their 50-day average
# -----------------------------------------------------------------------


# ------------------------------ DATA -----------------------------------
def load_universe():
    url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
    except Exception:
        if os.path.exists("ind_nifty500list.csv"):
            df = pd.read_csv("ind_nifty500list.csv")
        else:
            raise SystemExit("Could not get the Nifty 500 list. Save "
                             "ind_nifty500list.csv in this folder and retry.")
    return [s.strip() + ".NS" for s in df["Symbol"].dropna()]


def download_data(tickers):
    os.makedirs("data", exist_ok=True)
    cache = "data/prices.pkl"
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    out = {}
    for i in range(0, len(tickers), 50):
        batch = tickers[i:i + 50]
        print(f"Downloading {i + 1}-{i + len(batch)} of {len(tickers)}...")
        raw = yf.download(batch, start=START, auto_adjust=True,
                          group_by="ticker", progress=False, threads=True)
        for t in batch:
            try:
                d = raw[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(d) > LOOKBACK + 60:
                    out[t] = d
            except Exception:
                continue
    pd.to_pickle(out, cache)
    return out


def prepare(data):
    """Per-stock arrays and the breadth series (% of stocks above their 50-day average)."""
    closes = pd.DataFrame({t: d["Close"] for t, d in data.items()})
    ma = closes.rolling(50).mean()
    valid = ma.notna()
    breadth = ((closes > ma) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan) * 100
    go = (breadth > BREADTH_MIN)
    P = {}
    for t, df in data.items():
        idx = df.index
        prior_high = df["High"].rolling(LOOKBACK).max().shift(1)
        vol_avg = df["Volume"].rolling(20).mean().shift(1)
        brk = df["Close"] > prior_high
        P[t] = {
            "sym": t.replace(".NS", ""), "idx": idx,
            "o": df["Open"].values, "h": df["High"].values,
            "l": df["Low"].values, "c": df["Close"].values,
            "ph": prior_high.values, "vr": (df["Volume"] / vol_avg).values,
            "fresh": np.where((brk & ~brk.shift(1, fill_value=False)).values)[0],
            "ema": df["Close"].ewm(span=EMA_EXIT, adjust=False).mean().values,
            "ok": (go.astype(float).reindex(idx, method="ffill").fillna(0).values > 0.5),
        }
    return P, closes, breadth


# --------------------------- TRADE ENGINE ------------------------------
def simulate(S, j, trigger, stop):
    o, h, l, c, ema = S["o"], S["h"], S["l"], S["c"], S["ema"]
    n = len(c)
    if j >= n or h[j] < trigger:
        return None
    entry = max(o[j], trigger)
    risk = entry - stop
    if risk <= 0 or risk / entry * 100 > MAX_RISK_PCT:
        return None
    qty = int(RISK_PER_TRADE // risk)
    if qty < 1:
        return None

    last = min(j + MAX_HOLD_DAYS, n - 1)
    exit_price, exit_idx, reason = None, None, None
    for k in range(j, last + 1):
        if l[k] <= stop:
            exit_price = stop if k == j else min(o[k], stop)
            exit_idx, reason = k, "Stop hit"
            break
        if k > j and c[k] < ema[k]:
            if k + 1 < n:
                exit_price, exit_idx = o[k + 1], k + 1
            else:
                exit_price, exit_idx = c[k], k
            reason = "Closed below 30 EMA"
            break
    if exit_price is None:
        exit_price, exit_idx = c[last], last
        reason = "60-session limit" if j + MAX_HOLD_DAYS <= n - 1 else "Still open (latest data)"

    cost = COST_PCT / 100 * entry * qty
    pnl = qty * (exit_price - entry) - cost
    return {"entry_idx": j, "exit_idx": exit_idx, "entry": entry, "stop": stop,
            "exit": exit_price, "qty": qty, "risk": risk, "cost": cost,
            "pnl": pnl, "R": pnl / (qty * risk), "reason": reason}


def month_trades(P, start, end):
    """Every trade (before the 6-slot limit) whose ENTRY falls inside the month."""
    rows = []
    for t, S in P.items():
        idx = S["idx"]
        n = len(idx)
        lo = int(idx.searchsorted(start))
        hi = int(idx.searchsorted(end, side="right")) - 1
        if lo >= n or hi < lo:
            continue
        busy = -1
        for b in S["fresh"]:
            if b >= hi or b + MAX_DELAY < lo or b <= busy:
                continue
            if not (S["vr"][b] >= VOL_MULT):
                continue
            line, stop = S["ph"][b], S["l"][b]
            for tt in range(b + 1, min(b + MAX_DELAY, n - 1) + 1):
                if tt > hi:
                    break
                if tt - 1 > b and (S["c"][tt - 1] < line or S["l"][tt - 1] <= stop):
                    break
                if not S["ok"][tt - 1]:
                    continue
                res = simulate(S, tt, S["h"][tt - 1], stop)
                if res is None:
                    continue
                if tt < lo:        # would already have been bought before this month
                    break
                busy = res["exit_idx"]
                held_open = res["exit_idx"] > hi
                if held_open:      # value at the last day of the month
                    pnl_end = res["qty"] * (S["c"][hi] - res["entry"]) - res["cost"]
                else:
                    pnl_end = res["pnl"]
                rows.append({
                    "symbol": S["sym"],
                    "entry_date": idx[res["entry_idx"]], "exit_date": idx[res["exit_idx"]],
                    "entry": res["entry"], "stop": res["stop"], "exit": res["exit"],
                    "qty": res["qty"], "reason": res["reason"],
                    "days": res["exit_idx"] - res["entry_idx"],
                    "R": res["R"], "pnl": res["pnl"],
                    "open_at_end": held_open,
                    "R_end": pnl_end / (res["qty"] * res["risk"]), "pnl_end": pnl_end,
                    "vol_ratio": float(S["vr"][b]),
                })
                break
    return pd.DataFrame(rows)


def apply_cap(t, max_pos):
    """At most max_pos open trades; the highest breakout volume gets the free slots."""
    if t.empty or not max_pos:   # None or 0 = no limit on open trades
        return t
    t = t.sort_values(["entry_date", "vol_ratio"], ascending=[True, False])
    open_exits, keep = [], []
    for row in t.itertuples():
        open_exits = [e for e in open_exits if e > row.entry_date]
        if len(open_exits) < max_pos:
            keep.append(row.Index)
            open_exits.append(row.exit_date)
    return t.loc[keep]


def month_bounds(ym):
    start = pd.Timestamp(ym + "-01")
    return start, start + pd.offsets.MonthEnd(0)


# ------------------------------ REPORTS --------------------------------
def report_month(P, closes, breadth, ym):
    start, end = month_bounds(ym)
    first_data, last_data = closes.index[0], closes.index[-1]
    if start < first_data + pd.Timedelta(days=400) or start > last_data:
        raise SystemExit(f"No usable data for {ym}. Data runs from {first_data.date()} "
                         f"to {last_data.date()}, and the first year is needed for the 52-week line.")
    cand = month_trades(P, start, end)
    taken = apply_cap(cand, MAX_POSITIONS)

    print("\n" + "=" * 78)
    print(f"  {start.strftime('%B %Y').upper()} - the system, starting the month with no open trades")
    print("=" * 78)

    b = breadth.loc[start:end].dropna()
    go_days = int((b > BREADTH_MIN).sum())
    print(f"\nMARKET FILTER: {go_days} of {len(b)} sessions had breadth above {BREADTH_MIN:.0f}% "
          f"(new trades allowed)")
    items = [f"{d.strftime('%d-%b')} {v:.0f}%{'*' if v > BREADTH_MIN else ''}" for d, v in b.items()]
    for i in range(0, len(items), 6):
        print("  " + "  ".join(items[i:i + 6]))
    print("  (* = above the line, trades allowed the next session)")

    before = closes.loc[:start - pd.Timedelta(days=1)].iloc[-1]
    after = closes.loc[:end].iloc[-1]
    mv = (after / before - 1).dropna() * 100
    print(f"\nFor context: the average Nifty 500 stock moved {mv.mean():+.1f}% in the month "
          f"(median {mv.median():+.1f}%).")

    limit_txt = f"a limit of {MAX_POSITIONS} open positions" if MAX_POSITIONS else "no limit on open positions"
    print(f"\nSIGNALS: {len(cand)} trades triggered in the month; "
          f"{len(taken)} taken with {limit_txt}.")
    if taken.empty:
        print("No trades this month.")
        return

    t = taken.sort_values("entry_date")
    show = pd.DataFrame({
        "Stock": t["symbol"],
        "Bought": t["entry_date"].dt.strftime("%d-%b"),
        "Buy": t["entry"].round(2), "Stop": t["stop"].round(2), "Qty": t["qty"],
        "Exit": t.apply(lambda r: r["exit_date"].strftime("%d-%b-%y") if not r["open_at_end"]
                        or r["exit_date"].month != end.month else r["exit_date"].strftime("%d-%b"), axis=1),
        "Sold at": t["exit"].round(2),
        "Why": t["reason"], "Days": t["days"],
        "R at month end": t["R_end"].round(2), "Final R": t["R"].round(2),
        "Final Rs": t["pnl"].round(0).astype(int),
    })
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print("\nTRADES (R = multiples of your Rs %d risk; 'month end' values open trades at that day's close)\n" % RISK_PER_TRADE)
    print(show.to_string(index=False))

    closed = t[~t["open_at_end"]]
    opened = t[t["open_at_end"]]
    print("\n--- RESULT AT THE LAST DAY OF THE MONTH ---")
    if len(closed):
        w = int((closed["R"] > 0).sum())
        print(f"Closed during the month : {len(closed)} trades, {w} winners, "
              f"{closed['R'].sum():+.1f}R, Rs {closed['pnl'].sum():+,.0f}")
    print(f"Still open at month end : {len(opened)} trades, "
          f"{opened['R_end'].sum():+.1f}R, Rs {opened['pnl_end'].sum():+,.0f} (unrealised)")
    print(f"TOTAL AT MONTH END      : {t['R_end'].sum():+.1f}R, Rs {t['pnl_end'].sum():+,.0f}")

    print("\n--- RESULT WHEN EVERY TRADE HAS FINISHED (they run past the month) ---")
    w = int((t["R"] > 0).sum())
    print(f"{len(t)} trades, {w} winners ({w / len(t) * 100:.0f}%), "
          f"{t['R'].sum():+.1f}R, Rs {t['pnl'].sum():+,.0f}. "
          f"Best {t['R'].max():+.1f}R, worst {t['R'].min():+.1f}R, average hold {t['days'].mean():.0f} sessions.")
    if (t["reason"] == "Still open (latest data)").any():
        print("Note: some trades are still open in the latest data, valued at the last close.")
    print("\nOne month is a very small sample. Judge the system on many months (run: python backtest_month.py all).")


def report_all(P, closes, breadth):
    first = closes.index[0] + pd.Timedelta(days=400)
    months = pd.period_range(first, closes.index[-1], freq="M")
    rows = []
    print(f"Running {len(months)} months (a few minutes)...")
    for pm in months:
        ym = str(pm)
        start, end = month_bounds(ym)
        taken = apply_cap(month_trades(P, start, end), MAX_POSITIONS)
        b = breadth.loc[start:end].dropna()
        row = {"Month": ym, "Go days": f"{int((b > BREADTH_MIN).sum())}/{len(b)}",
               "Trades": len(taken)}
        if len(taken):
            row.update({"R at month end": round(taken["R_end"].sum(), 1),
                        "Final R": round(taken["R"].sum(), 1),
                        "Final Rs": int(taken["pnl"].sum()),
                        "Win %": round((taken["R"] > 0).mean() * 100)})
        else:
            row.update({"R at month end": 0.0, "Final R": 0.0, "Final Rs": 0, "Win %": ""})
        rows.append(row)
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 500)
    print("\n=== MONTH BY MONTH (each month starts with no open trades; %s) ===\n"
          % (f"max {MAX_POSITIONS} positions" if MAX_POSITIONS else "no position limit"))
    print(df.to_string(index=False))
    traded = df[df["Trades"] > 0]
    pos = int((traded["Final R"] > 0).sum())
    print(f"\n{len(df)} months, {len(traded)} with trades; {pos} positive, "
          f"{len(traded) - pos} negative on a final basis.")
    print(f"Sum of final R: {df['Final R'].sum():+.1f}R (about Rs {df['Final Rs'].sum():+,}); "
          f"average per traded month {traded['Final R'].mean():+.1f}R; "
          f"worst month {df['Final R'].min():+.1f}R, best {df['Final R'].max():+.1f}R.")
    df.to_csv("months_summary.csv", index=False)
    print("Saved to months_summary.csv")


def main():
    global MAX_POSITIONS
    arg = sys.argv[1] if len(sys.argv) > 1 else "2025-07"
    if len(sys.argv) > 2:
        a = sys.argv[2].lower()
        MAX_POSITIONS = None if a in ("none", "0", "unlimited") else int(a)
    if arg.lower() != "all":
        try:
            month_bounds(arg)
        except Exception:
            raise SystemExit("Use the format YYYY-MM, for example: python backtest_month.py 2025-07")
    tickers = load_universe()
    data = download_data(tickers)
    for k in data:
        data[k].index = pd.to_datetime(data[k].index).tz_localize(None).normalize()
    print(f"Data ready for {len(data)} stocks.")
    P, closes, breadth = prepare(data)
    if arg.lower() == "all":
        report_all(P, closes, breadth)
    else:
        report_month(P, closes, breadth, arg)


if __name__ == "__main__":
    main()
