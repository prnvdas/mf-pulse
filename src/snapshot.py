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

from analytics import blended_level, fetch_nav_series, fetch_prices
from analytics import load_transactions
from common import CONFIG, load_portfolio, now_ist, read_json, write_json

OUT = "portfolio_history.json"
DEFAULT_SINCE = "2026-08-21"          # first NAV date the dashboard recorded


def _day(s: str) -> str:
    return dt.datetime.strptime(s, "%d-%b-%Y").date().isoformat()


def point(date: str, rows: dict) -> dict:
    assert all(x == x and abs(x) != float("inf") for r in rows.values() for x in r), f"non-finite value on {date}"
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
            money = float(mm.iloc[-1]) if len(mm) else 0.0
            nav = float(navs[fid].asof(d)) if units else 0.0
            # the day a fund is first bought its NAV may not be published yet (asof gives NaN): value it at cost
            rows[fid] = (units * nav if nav == nav and nav > 0 else money if units else 0.0, money)
        out.append(point(d.date().isoformat(), rows))
    return out


BENCH = {"n": "^NSEI", "m": "^CRSLDX"}          # Nifty 50 and Nifty 500, price indices (no dividends)


def add_benchmarks(points: list[dict]) -> list[str]:
    """What the same money, invested on the same dates, would be worth in an index: p["n"] (Nifty 50), p["m"] (Nifty 500).
    Each rupee you put in (or took out) on a day buys (or sells) index units at that day's close. Returns the keys added."""
    import yfinance as yf
    added = []
    flows, prev = [], 0.0
    for p in points:
        flows.append(p["i"] - prev)
        prev = p["i"]
    for key, ticker in BENCH.items():
        try:
            df = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=False, timeout=60)["Close"]
            c = (df.iloc[:, 0] if hasattr(df, "columns") else df).dropna()
            c.index = pd.to_datetime(c.index).tz_localize(None).normalize()
            if len(c) < 500:
                raise ValueError("too little index history")
            units, got = 0.0, []
            for p, fl in zip(points, flows):
                px = float(c.asof(pd.Timestamp(p["d"])))
                if not px or px != px:
                    got.append(None)
                    continue
                units = max(0.0, units + fl / px)
                got.append(round(units * px))
            if sum(g is not None for g in got) > 0.9 * len(points):
                for p, g in zip(points, got):
                    if g is not None:
                        p[key] = g
                    else:
                        p.pop(key, None)
                added.append(key)
        except Exception as exc:  # noqa: BLE001 -- the comparison is a bonus: never block the history
            print(f"[warn] benchmark {ticker}: {exc}", file=sys.stderr)
    return added


def build_fund_history(cfg: dict, state: dict) -> dict | None:
    """Fund by fund: worth, money put in, and what the same money would be worth in the fund's own benchmark (same dates).
    Needs the imported order history (config/transactions.csv with units); None without it. -> docs/data/fund_history.json"""
    tx, un = load_transactions(), tx_units()
    if not tx:
        return None
    def parts(f):                                       # the benchmark used elsewhere: the long-history proxy when one is configured
        ab = f.get("analytics_benchmark") or {}
        return ab.get("xirr_returns") or ab.get("returns") or []
    prices = fetch_prices(sorted({q["ticker"] for f in cfg["funds"] for q in parts(f)}))
    navs, cum, bench = {}, {}, {}
    for f in cfg["funds"]:
        fid, rows = f["id"], tx.get(f["id"])
        s = fetch_nav_series(f["amfi_code"])
        if not rows or s is None:
            continue
        money = pd.Series({pd.Timestamp(d): 0.0 for d, _ in rows})
        for d, a in rows:
            money[pd.Timestamp(d)] += a
        units = pd.Series(un.get(fid, {}), dtype=float)
        if units.empty:
            units = pd.Series({d: float(a) / float(s.asof(d)) for d, a in money.items()})
        navs[fid], cum[fid], bench[fid] = s, (units.sort_index().cumsum(), money.sort_index().cumsum()), blended_level(parts(f), prices)
    if not cum:
        return None
    start = min(c[0].index[0] for c in cum.values())
    dates = sorted(set.union(*[set(s.index[s.index >= start]) for s in navs.values()]))
    out = {}
    for f in cfg["funds"]:
        fid = f["id"]
        if fid not in cum:
            continue
        u, m, lv = cum[fid][0], cum[fid][1], bench[fid]
        v, i, b, bu, prev_i = [], [], [], 0.0, 0.0
        for d in dates:
            uu, mm = u[u.index <= d], m[m.index <= d]
            units_now, money_now = (float(uu.iloc[-1]) if len(uu) else 0.0), (float(mm.iloc[-1]) if len(mm) else 0.0)
            nav = float(navs[fid].asof(d)) if units_now else 0.0
            v.append(round(units_now * nav if nav == nav and nav > 0 else money_now if units_now else 0.0))
            i.append(round(money_now))
            px = float(lv.asof(d)) if lv is not None and d >= lv.index[0] else None
            if px and px == px:
                bu = max(0.0, bu + (money_now - prev_i) / px)
                b.append(round(bu * px))
            else:
                b.append(None)
            prev_i = money_now
        out[fid] = {"name": f["name"], "benchmark": (f.get("analytics_benchmark") or {}).get("name"), "v": v, "i": i, "b": b}
    return {"generated_at": now_ist().isoformat(), "dates": [d.date().isoformat() for d in dates], "funds": out,
            "note": "the same money invested on the same dates in the fund's benchmark index; price indices, so without dividends (about 1.3% a year)"}


def tx_check(cfg: dict, state: dict) -> dict:
    """Does config/transactions.csv still explain the units you hold? A purchase or sale made outside the SIPs
    (a lump sum, a redemption) makes it fall behind; the page then tells you to download the order history again."""
    path = CONFIG / "transactions.csv"
    if not path.exists():
        return {"status": "none"}
    un, gaps, last = tx_units(), {}, ""
    for f in cfg["funds"]:
        held = float((state.get(f["id"]) or {}).get("units") or 0)
        mine = un.get(f["id"], {})
        if held and mine:
            gaps[f["id"]] = round((sum(mine.values()) / held - 1) * 100, 2)
            last = max(last, max(mine).date().isoformat())
    return {"status": "stale" if any(abs(g) > 1 for g in gaps.values()) or len(gaps) < len(cfg["funds"]) else "ok", "gaps_pct": gaps, "last_transaction": last}


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
    got = add_benchmarks(doc["points"])
    doc["benchmarks"] = {"n": "Nifty 50", "m": "Nifty 500", "note": "the same money invested on the same dates; price index, so without dividends (about 1.3% a year)"} if got else None
    try:
        fh = build_fund_history(cfg, state)
        if fh:
            write_json("fund_history.json", fh)
            print(f"[ok] fund_history.json: {len(fh['dates'])} days, {len(fh['funds'])} funds")
    except Exception as exc:  # noqa: BLE001 -- a bonus view: never block the main history
        print(f"[warn] fund history: {exc}", file=sys.stderr)
    doc["tx_check"] = tx_check(cfg, state)
    doc["generated_at"] = now_ist().isoformat()
    write_json(OUT, doc)
    last = doc["points"][-1]
    print(f"[ok] {len(doc['points'])} points; latest {last['d']}: value ₹{last['v']:,.0f}, invested ₹{last['i']:,.0f}")


if __name__ == "__main__":
    main()
