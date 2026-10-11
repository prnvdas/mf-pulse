"""Daily portfolio snapshots: what the portfolio was worth, and how much you had put in, on every NAV date.

Written to docs/data/portfolio_history.json and drawn by the "Your money over time" chart.

    python src/snapshot.py              # add today's point (nightly, after reconcile.py); first ever run backfills
    python src/snapshot.py --backfill   # rebuild the file from scratch

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
from common import load_portfolio, now_ist, read_json, write_json

OUT = "portfolio_history.json"
DEFAULT_SINCE = "2026-08-21"          # first NAV date the dashboard recorded


def _day(s: str) -> str:
    return dt.datetime.strptime(s, "%d-%b-%Y").date().isoformat()


def point(date: str, rows: dict) -> dict:
    return {"d": date, "v": round(sum(r[0] for r in rows.values()), 2), "i": round(sum(r[1] for r in rows.values()), 2),
            "f": {k: [round(r[0], 2), round(r[1], 2)] for k, r in rows.items()}}


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
        pts = backfill(cfg, state, args.since)
        doc = {"since": args.since, "basis": "reconstructed from today's units, taking the configured SIPs back out", "points": pts}
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
