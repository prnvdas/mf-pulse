"""Fund health check: is each fund still doing its job? Written to docs/data/fund_health.json.

For every fund: 1, 3 and 5 year returns against its benchmark and against EVERY other direct-growth fund in the same
AMFI category (found automatically from AMFI's published list, nothing hand-picked), where it ranks among them, and in
what share of recent rolling one-year periods it beat the benchmark. The category is fetched about weekly (a run
inside six days of the last one is skipped unless --force), because ranking ~40 funds means ~40 NAV histories.

Honest limits, repeated on the page: a category contains funds of very different ages and sizes; past returns
say little about the next year; and a one-year dip is normal -- judge a fund over three years or more. The
benchmark is a price index (no dividends), so it is given a small head start (BENCH_YIELD_PP) to be fair.
Non-blocking in the nightly workflow: a failure must never hold up NAV reconciliation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

from analytics import blended_level, fetch_nav_series, fetch_prices
from common import AMFI_NAV_URL, load_portfolio, now_ist, read_json, write_json

BENCH_YIELD_PP = 1.0                 # a price index leaves out ~1%/yr of dividends
WINDOWS = {"1Y": 1, "3Y": 3, "5Y": 5}


MAX_CATEGORY = 70


def category_members(code, navtext: str) -> list[tuple[int, str]]:
    """Every direct-growth fund in the same AMFI category as `code` (it is included), from AMFI's own list."""
    meta = requests.get(f"https://api.mfapi.in/mf/{code}/latest", timeout=30).json()["meta"]
    cat = str(meta.get("scheme_category") or "").split(" - ")[-1].strip()
    if not cat:
        return []
    out, inside = [], False
    for line in navtext.splitlines():
        if ";" not in line:
            t = line.strip()
            if re.match(r"(Open|Close|Interval)", t):            # a category header: "Open Ended Schemes(Equity Scheme - Small Cap Fund)"
                inside = t.startswith("Open Ended") and re.search(r"\bEquity Schemes?\s*-\s*" + re.escape(cat) + r"\)", t) is not None
            continue                                             # (fund-house name lines in between are ignored)
        if not inside:
            continue
        p = [x.strip() for x in line.split(";")]
        if len(p) == 8 and "direct" in p[4].lower() and "growth" in p[5].lower() and not re.search(r"idcw|dividend|bonus|payout", " ".join(p[3:6]), re.I):
            try:
                out.append((int(p[0]), p[3]))
            except ValueError:
                pass
    return out[:MAX_CATEGORY]


def bench_parts(f: dict) -> list[dict]:
    """The longer-history proxy when one is configured (some ETFs behind the benchmark blend are only a year or two old)."""
    ab = f.get("analytics_benchmark") or {}
    return ab.get("xirr_returns") or ab.get("returns") or []


def cagr(s: pd.Series, end: pd.Timestamp, years: int) -> float | None:
    start = end - pd.DateOffset(years=years)
    if s is None or len(s) < 2 or s.index[0] > start + pd.Timedelta(days=7):
        return None
    a, b = float(s.asof(start)), float(s.asof(end))
    return round(((b / a) ** (1 / years) - 1) * 100, 1) if a > 0 and b > 0 else None


def beat_share(fund: pd.Series, bench: pd.Series, end: pd.Timestamp) -> tuple[int | None, int]:
    """Share of the last three years' days on which the trailing one-year return beat the (yield-adjusted) benchmark."""
    both = pd.concat([fund.rename("f"), bench.rename("b")], axis=1, sort=True).dropna()
    both = both[both.index > end - pd.DateOffset(years=4)]
    if len(both) < 750:
        return None, 0
    r = both.pct_change(252).dropna()
    r = r[r.index > end - pd.DateOffset(years=3)]
    if len(r) < 250:
        return None, 0
    return round(float(((r["f"] - r["b"]) * 100 > BENCH_YIELD_PP).mean() * 100)), len(r)


def rank_of(mine: float | None, others: list[float]) -> tuple[int, int] | None:
    if mine is None or not others:
        return None
    return 1 + sum(o > mine for o in others), len(others) + 1


def verdict(w: dict, years: float) -> tuple[str, str, str]:
    """(code, label, one plain sentence)."""
    if years < 1.1:
        return "new", "Too new to judge", "It has under a year of history, so there isn't enough to say whether it is doing well. Look again after it has three years."
    def ahead(k):
        x = w.get(k)
        return None if not x or x.get("fund") is None or x.get("bench") is None else x["fund"] >= x["bench"] + BENCH_YIELD_PP
    long_k = next((k for k in ("3Y", "1Y") if (w.get(k) or {}).get("rank")), None)
    pos = None if long_k is None else w[long_k]["rank"] / w[long_k]["of"]            # 0 = best in the category, 1 = worst
    where = "" if long_k is None else f" It ranks {w[long_k]['rank']} of {w[long_k]['of']} in its category over {long_k.replace('Y', ' year' + ('s' if long_k == '3Y' else ''))}."
    flags = [a for a in (ahead("1Y"), ahead("3Y")) if a is not None]
    if flags and all(flags):
        if pos is None or pos <= 0.5:
            return "good", "On track", "It is ahead of its benchmark over the periods checked and in the better half of its category." + where
        if pos <= 0.67:
            return "mixed", "Beats its benchmark, mid-table", "It is ahead of its own benchmark but only mid-table among its category." + where
        return "mixed", "Beats its benchmark, trails its category", "It is ahead of its own benchmark, but most funds in its category did better over the longer run. Worth watching, not a reason to act." + where
    if flags and not any(flags) and pos is not None and pos > 0.5:
        return "weak", "Behind: keep an eye on it", "It trails its benchmark and sits in the lower half of its category. Not a reason to sell on its own; look at the 3-year numbers before deciding." + where
    return "mixed", "Mixed", "It is ahead on some measures and behind on others, which is normal for a fund. Nothing to act on." + where


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="run even if the last result is under six days old")
    args = ap.parse_args()
    cfg, ts = load_portfolio(), now_ist()
    prev = read_json("fund_health.json", None)
    if not args.force and isinstance(prev, dict) and prev.get("generated_at") and (ts - dt.datetime.fromisoformat(prev["generated_at"])).days < 6 \
            and {x.get("id") for x in prev.get("funds", [])} >= {f["id"] for f in cfg["funds"]}:
        print("[info] fund health is under six days old; skipping (use --force)")
        return
    tickers = sorted({p["ticker"] for f in cfg["funds"] for p in bench_parts(f)})
    prices = fetch_prices(tickers)
    navtext = requests.get(AMFI_NAV_URL, timeout=60).text
    members = {}
    for f in cfg["funds"]:
        try:
            members[f["id"]] = category_members(f["amfi_code"], navtext)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {f['id']}: category lookup failed: {exc}", file=sys.stderr)
            members[f["id"]] = []
    codes = {f["amfi_code"] for f in cfg["funds"]} | {c for ms in members.values() for c, _ in ms}
    with ThreadPoolExecutor(max_workers=6) as pool:
        navs = dict(zip(codes, pool.map(fetch_nav_series, codes)))

    out = []
    for f in cfg["funds"]:
        mine = navs.get(f["amfi_code"])
        if mine is None:
            print(f"[warn] {f['id']}: no NAV history, skipped", file=sys.stderr)
            continue
        end = mine.index[-1]
        bench = blended_level(bench_parts(f), prices)
        # a fund that stopped publishing (merged or renamed) would read as a flat line: leave it out
        peers = [(n, navs.get(c)) for c, n in members.get(f["id"], []) if c != f["amfi_code"]]
        peers = [(n, s) for n, s in peers if s is not None and (end - s.index[-1]).days <= 10]
        win = {}
        for k, y in WINDOWS.items():
            mv, bv = cagr(mine, end, y), cagr(bench, end, y) if bench is not None else None
            pv = [cagr(s, end, y) for _, s in peers]
            pv = [x for x in pv if x is not None]
            rk = rank_of(mv, pv)
            win[k] = {"fund": mv, "bench": bv, "peer_median": round(float(np.median(pv)), 1) if pv else None,
                      "rank": rk[0] if rk else None, "of": rk[1] if rk else None}
        years = (end - mine.index[0]).days / 365.25
        share, n = beat_share(mine, bench, end) if bench is not None else (None, 0)
        code, label, text = verdict(win, years)
        rows = [{"name": n, **{k: cagr(s, end, y) for k, y in WINDOWS.items()}} for n, s in peers]
        key = "3Y" if any(r["3Y"] is not None for r in rows) else "1Y"
        best = sorted((r for r in rows if r[key] is not None), key=lambda r: -r[key])[:5]
        out.append({
            "id": f["id"], "name": f["name"], "verdict": code, "label": label, "text": text,
            "benchmark": (f.get("analytics_benchmark") or {}).get("name"), "years_of_history": round(years, 1),
            "windows": win, "beat_benchmark_pct": share, "ter_pct": f.get("ter_annual_pct"),
            "category_size": len(peers) + 1, "best_window": key, "peers": best,
            "as_of": end.date().isoformat(),
        })
        print(f"[ok] {f['id']}: {label}, category of {len(peers) + 1}  " + "  ".join(f"{k} {v['fund']}% vs bench {v['bench']}% (rank {v['rank']}/{v['of']})" for k, v in win.items()))
    if not out:
        sys.exit("[error] no fund could be checked; file left as it was")
    write_json("fund_health.json", {
        "generated_at": ts.isoformat(), "bench_yield_pp": BENCH_YIELD_PP, "funds": out,
        "note": "Ranked against every direct-growth fund in the same AMFI category, funds of very different age and size included. Returns are annualised for 3 and 5 years. Past returns do not predict the future.",
    })


if __name__ == "__main__":
    main()
