"""Fund health check: is each fund still doing its job? Written to docs/data/fund_health.json.

For every fund: 1, 3 and 5 year returns against its benchmark and against a handful of comparable direct-plan
funds (config/portfolio.yaml -> funds[].peers), where it ranks among them, and in what share of recent
rolling one-year periods it beat the benchmark.

Honest limits, repeated on the page: the peer list is a hand-picked sample, not the whole category; past returns
say little about the next year; and a one-year dip is normal -- judge a fund over three years or more. The
benchmark is a price index (no dividends), so it is given a small head start (BENCH_YIELD_PP) to be fair.
Non-blocking in the nightly workflow: a failure must never hold up NAV reconciliation.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from analytics import blended_level, fetch_nav_series, fetch_prices
from common import load_portfolio, now_ist, write_json

BENCH_YIELD_PP = 1.0                 # a price index leaves out ~1%/yr of dividends
WINDOWS = {"1Y": 1, "3Y": 3, "5Y": 5}


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
    def top_half(k):
        x = w.get(k)
        return None if not x or not x.get("rank") else x["rank"] <= (x["of"] + 1) // 2
    long_k = "3Y" if w.get("3Y", {}).get("fund") is not None else "1Y"
    a1, a3, t = ahead("1Y"), ahead("3Y"), top_half(long_k)
    flags = [a for a in (a1, a3) if a is not None]
    if flags and all(flags) and t is not False:
        return "good", "On track", "It is ahead of its benchmark over the periods checked" + (" and in the top half of comparable funds." if t else ".")
    if flags and not any(flags) and t is False:
        return "weak", "Behind — keep an eye on it", "It trails its benchmark and sits in the lower half of comparable funds. Not a reason to sell on its own; look at the 3-year numbers before deciding."
    if flags and all(flags) and t is False:
        return "mixed", "Beats its benchmark, trails its peers", "It is ahead of its own benchmark but in the lower half of comparable funds over the longer run, so the peers did better. Worth watching, not a reason to act."
    return "mixed", "Mixed", "It is ahead on some measures and behind on others, which is normal for a fund. Nothing to act on."


def main() -> None:
    cfg, ts = load_portfolio(), now_ist()
    tickers = sorted({p["ticker"] for f in cfg["funds"] for p in bench_parts(f)})
    prices = fetch_prices(tickers)
    codes = {f["amfi_code"] for f in cfg["funds"]} | {p["code"] for f in cfg["funds"] for p in f.get("peers", [])}
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
        peers = [(p["name"], navs.get(p["code"])) for p in f.get("peers", [])]
        # a fund that stopped publishing (merged or renamed) would read as a flat line: leave it out
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
        out.append({
            "id": f["id"], "name": f["name"], "verdict": code, "label": label, "text": text,
            "benchmark": (f.get("analytics_benchmark") or {}).get("name"), "years_of_history": round(years, 1),
            "windows": win, "beat_benchmark_pct": share, "ter_pct": f.get("ter_annual_pct"),
            "peers": [{"name": n, **{k: cagr(s, end, y) for k, y in WINDOWS.items()}} for n, s in peers],
            "as_of": end.date().isoformat(),
        })
        print(f"[ok] {f['id']}: {label}  " + "  ".join(f"{k} {v['fund']}% vs bench {v['bench']}% (rank {v['rank']}/{v['of']})" for k, v in win.items()))
    if not out:
        sys.exit("[error] no fund could be checked; file left as it was")
    write_json("fund_health.json", {
        "generated_at": ts.isoformat(), "bench_yield_pp": BENCH_YIELD_PP, "funds": out,
        "note": "Peers are a hand-picked sample of comparable direct-plan funds, not the whole category. Returns are annualised for 3 and 5 years. Past returns do not predict the future.",
    })


if __name__ == "__main__":
    main()
