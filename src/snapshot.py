"""Daily portfolio snapshots: what the portfolio was worth, and how much you had put in, on every NAV date.

Written to docs/data/portfolio_history.json and drawn by the "Your money over time" chart.

    python src/snapshot.py              # add today's point (nightly, after reconcile.py); first ever run backfills
    python src/snapshot.py --backfill   # rebuild the file from scratch

With config/transactions.csv (see import_transactions.py) the history is exact and starts at your first purchase.
Without it, it is rebuilt from today's holdings as described below.

Where the history comes from. state.json has your exact units and invested amount today (they come from the Groww
statement). Walking backwards, the only things that changed them are the SIPs in config/portfolio.yaml, so each
SIP is taken back out at its own NAV: units(t) = units now - the units bought after t. That is exact as long as the
SIPs ran as configured (a lump sum or a missed month would show up as a small step at the next statement
import). It stops at --since because earlier purchases are not recorded anywhere. Value is units x the published NAV.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

import pandas as pd

from analytics import fetch_nav_series
from analytics import load_transactions
from common import CONFIG, load_portfolio, now_ist, read_json, write_json

OUT = "portfolio_history.json"
DEFAULT_SINCE = "2026-08-21"          # first NAV date the dashboard recorded


def _day(s: str) -> str:
    return dt.datetime.strptime(s, "%d-%b-%Y").date().isoformat()


def point(date: str, rows: dict) -> dict:
    return {"d": date, "v": round(sum(r[0] for r in rows.values()), 2), "i": round(sum(r[1] for r in rows.values()), 2)}


def backfill(cfg: dict, state: dict, since: str) -> list[dict]:
    navs = {}
    for f in cfg["funds"]:
        s = fetch_nav_series(f["amfi_code"])
        if s is None:
            sys.exit(f"[error] couldn't fetch NAV history for {f['id']}; nothing written")
        navs[f["id"]] = s[s.index >= pd.Timestamp(since)]
    last_day = max(max(s.index) for s in navs.values())
    # SIP purchases after `since`: the first NAV date on/after the SIP day, the same rule reconcile.py applies
    events: dict[str, list[tuple[pd.Timestamp, float, float]]] = {f["id"]: [] for f in cfg["funds"]}
    for sip in cfg.get("sips", []):
        s = navs.get(sip["fund_id"])
        if s is None:
            continue
        m = pd.Timestamp(since).replace(day=1)
        while m <= last_day:
            due = m.replace(day=int(sip["day_of_month"]))
            hit = s[s.index >= due]
            if len(hit) and hit.index[0] > pd.Timestamp(since) and hit.index[0] - due <= pd.Timedelta(days=4):
                events[sip["fund_id"]].append((hit.index[0], float(sip["amount"]), float(sip["amount"]) / float(hit.iloc[0])))
            m = (m + pd.Timedelta(days=32)).replace(day=1)
    dates = sorted(set.intersection(*[set(s.index) for s in navs.values()]))
    out = []
    for d in dates:
        rows = {}
        for f in cfg["funds"]:
            fid, st = f["id"], state[f["id"]]
            after = [e for e in events[fid] if e[0] > d]
            units = float(st["units"]) - sum(e[2] for e in after)
            invested = float(st.get("seed_invested", f.get("seed_invested", 0))) - sum(e[1] for e in after)
            rows[fid] = (units * float(navs[fid].loc[d]), invested)
        out.append(point(d.date().isoformat(), rows))
    return out


def tx_units() -> dict[str, dict[pd.Timestamp, float]]:
    """config/transactions.csv's units column (written by import_transactions.py), summed per fund and day; {} if absent."""
    path, out = CONFIG / "transactions.csv", {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 4 and parts[3] and parts[0][:1].isdigit():
            try:
                d = pd.Timestamp(parts[0]); out.setdefault(parts[1], {})
                out[parts[1]][d] = out[parts[1]].get(d, 0.0) + float(parts[3])
            except ValueError:
                continue
    return out


def backfill_from_transactions(cfg: dict, state: dict) -> list[dict] | None:
    """Exact history from the first purchase: units and money in are running sums of the real transactions.
    Returns None (so the caller falls back to the SIP reconstruction) when the file doesn't explain today's units."""
    tx, un = load_transactions(), tx_units()
    if not tx:
        return None
    navs, cum = {}, {}
    for f in cfg["funds"]:
        fid = f["id"]
        rows = tx.get(fid)
        s = fetch_nav_series(f["amfi_code"])
        if not rows or s is None:
            print(f"[warn] transactions: nothing usable for {fid}; using the SIP reconstruction", file=sys.stderr)
            return None
        navs[fid] = s
        money = pd.Series({pd.Timestamp(d): 0.0 for d, _ in rows})
        for d, a in rows:
            money[pd.Timestamp(d)] += a
        units = pd.Series(un.get(fid, {}), dtype=float)
        if units.empty:                                  # no units column: price each purchase at that day's NAV
            units = pd.Series({d: float(a) / float(s.asof(d)) for d, a in money.items()})
        cum[fid] = (units.sort_index().cumsum(), money.sort_index().cumsum())
        held = float((state.get(fid) or {}).get("units") or 0)
        if held and abs(cum[fid][0].iloc[-1] / held - 1) > 0.01:
            print(f"[warn] transactions: {fid} units {cum[fid][0].iloc[-1]:,.1f} vs holding {held:,.1f}; using the SIP reconstruction", file=sys.stderr)
            return None
    start = min(c[0].index[0] for c in cum.values())
    dates = sorted(set.union(*[set(s.index[s.index >= start]) for s in navs.values()]))
    out = []
    for d in dates:
        rows = {}
        for fid, (u, m) in cum.items():
            uu, mm = u[u.index <= d], m[m.index <= d]
            units = float(uu.iloc[-1]) if len(uu) else 0.0
            rows[fid] = (units * float(navs[fid].asof(d)) if units else 0.0, float(mm.iloc[-1]) if len(mm) else 0.0)
        out.append(point(d.date().isoformat(), rows))
    return out


def today_point(cfg: dict, state: dict) -> dict | None:
    rows, days = {}, []
    for f in cfg["funds"]:
        st = state.get(f["id"]) or {}
        if not (st.get("units") and st.get("last_nav") and st.get("last_nav_date")):
            return None
        rows[f["id"]] = (st["units"] * st["last_nav"], float(st.get("seed_invested", f.get("seed_invested", 0))))
        days.append(_day(st["last_nav_date"]))
    return point(min(days), rows)          # a fund whose NAV is a day late must not be shown as today's value


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--since", default=DEFAULT_SINCE)
    args = ap.parse_args()
    cfg, state = load_portfolio(), read_json("state.json", {})
    doc = read_json(OUT, None)
    if args.backfill or not (isinstance(doc, dict) and doc.get("points")):
        pts = backfill_from_transactions(cfg, state)
        basis = "your transaction report"
        if pts is None:
            pts, basis = backfill(cfg, state, args.since), "reconstructed from today's units, taking the configured SIPs back out"
        doc = {"since": pts[0]["d"], "basis": basis, "points": pts}
        print(f"[ok] backfilled {len(pts)} points from {pts[0]['d']} to {pts[-1]['d']}")
    p = today_point(cfg, state)
    if p:
        by = {x["d"]: x for x in doc["points"]}
        by[p["d"]] = p
        doc["points"] = [by[k] for k in sorted(by)]
    doc["generated_at"] = now_ist().isoformat()
    write_json(OUT, doc)
    last = doc["points"][-1]
    print(f"[ok] {len(doc['points'])} points; latest {last['d']}: value ₹{last['v']:,.0f}, invested ₹{last['i']:,.0f}")


if __name__ == "__main__":
    main()
