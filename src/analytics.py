"""Fund-vs-benchmark analytics: returns, beta, alpha, volatility, Sharpe,
drawdown and portfolio-weighted P/E, written to docs/data/analytics.json.

Sources (all free): AMFI NAV history via mfapi.in for the funds, Yahoo for
benchmark prices and per-stock P/E, P/B. Benchmarks are price-return proxies
(ETFs / index levels), not total-return indices, so a benchmark's return is
understated by roughly its dividend yield (~1%/yr) and alpha is flattered by
about that much -- the dashboard says so.

Runs nightly from reconcile.yml as a non-blocking step: a failure here must
never hold up NAV reconciliation. Each fund and each metric fails on its own
and is written as null rather than guessed.
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from common import load_holdings, load_portfolio, now_ist, read_json, write_json

FUNDAMENTALS_MAX_AGE_DAYS = 7
PERIODS = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365}


# --- NAV history --------------------------------------------------------------

def fetch_nav_series(code, retries: int = 2) -> pd.Series | None:
    for attempt in range(retries + 1):
        try:
            r = requests.get(f"https://api.mfapi.in/mf/{code}", timeout=30)
            r.raise_for_status()
            rows = r.json()["data"]
            s = pd.Series(
                {pd.to_datetime(x["date"], format="%d-%m-%Y"): float(x["nav"]) for x in rows}
            ).sort_index()
            return s[s > 0]
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] NAV history {code} attempt {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(3)
    return None


# --- benchmark ----------------------------------------------------------------

def fetch_prices(tickers: list[str]) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    if not tickers:
        return out
    try:
        data = yf.download(
            tickers=" ".join(tickers), period="2y", interval="1d",
            group_by="ticker", progress=False, threads=True, auto_adjust=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] benchmark download failed: {exc}", file=sys.stderr)
        return out
    for t in tickers:
        try:
            frame = data[t] if len(tickers) > 1 else data
            c = frame["Close"].dropna()
            c.index = pd.to_datetime(c.index).tz_localize(None).normalize()
            if len(c) > 50:
                out[t] = c
        except Exception:  # noqa: BLE001
            continue
    return out


def blended_level(parts: list[dict], prices: dict[str, pd.Series]) -> pd.Series | None:
    """Weighted blend of daily returns -> a synthetic level series."""
    rets = []
    for p in parts:
        s = prices.get(p["ticker"])
        if s is None:
            return None
        rets.append(s.pct_change().rename(p["ticker"]) * float(p["weight"]))
    df = pd.concat(rets, axis=1, sort=True).dropna()
    if df.empty:
        return None
    return (1 + df.sum(axis=1)).cumprod()


# --- metrics --------------------------------------------------------------------

def period_return(level: pd.Series, end: pd.Timestamp, days: int) -> float | None:
    start = end - pd.Timedelta(days=days)
    if start < level.index[0]:
        return None
    base = level.asof(start)
    return round((float(level.loc[end]) / float(base) - 1) * 100, 2) if base else None


def risk_metrics(levels: pd.DataFrame, rf_pct: float) -> dict | None:
    win = levels[levels.index > levels.index[-1] - pd.Timedelta(days=365)]
    rets = win.pct_change().dropna()
    if len(rets) < 100:
        return None
    f, b = rets["fund"], rets["bench"]
    rf_d = (1 + rf_pct / 100) ** (1 / 252) - 1
    var_b = float(np.var(b, ddof=1))
    if var_b == 0:
        return None
    beta = float(np.cov(f, b, ddof=1)[0, 1] / var_b)
    alpha = ((f - rf_d).mean() - beta * (b - rf_d).mean()) * 252 * 100
    vol = float(f.std(ddof=1) * np.sqrt(252) * 100)
    sharpe = float((f - rf_d).mean() / f.std(ddof=1) * np.sqrt(252))

    def max_dd(x: pd.Series) -> float:
        return float((x / x.cummax() - 1).min() * 100)

    return {
        "window_days": int((win.index[-1] - win.index[0]).days),
        "beta": round(beta, 2), "alpha_pct": round(float(alpha), 2),
        "vol_pct": round(vol, 1),
        "bench_vol_pct": round(float(b.std(ddof=1) * np.sqrt(252) * 100), 1),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd(win["fund"]), 1),
        "bench_max_drawdown_pct": round(max_dd(win["bench"]), 1),
    }


# --- valuation ----------------------------------------------------------------

def refresh_fundamentals(tickers: list[str], today: dt.date) -> dict:
    cache = read_json("fundamentals.json", {})
    stale = [
        t for t in tickers
        if t not in cache
        or (today - dt.date.fromisoformat(cache[t].get("date", "2000-01-01"))).days
        > FUNDAMENTALS_MAX_AGE_DAYS
    ]
    print(f"[info] fundamentals: {len(stale)} of {len(tickers)} tickers need a refresh")

    def one(t: str):
        try:
            info = yf.Ticker(t).info
            return t, {"pe": info.get("trailingPE"), "pb": info.get("priceToBook"),
                       "date": today.isoformat()}
        except Exception:  # noqa: BLE001 -- keep the old value, retry next run
            return t, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, v in pool.map(one, stale):
            if v is not None and (v["pe"] is not None or v["pb"] is not None):
                cache[t] = v
            elif t not in cache:
                cache[t] = {"pe": None, "pb": None, "date": today.isoformat()}
    write_json("fundamentals.json", cache)
    return cache


def weighted_multiple(pairs: list[tuple[float, float | None]], lo=0.0, hi=500.0):
    """Harmonic (earnings-weighted) average of P/E-style multiples.

    Returns (multiple, share of total weight that had a usable value).
    Loss-makers (negative P/E) are excluded and reported through the coverage.
    """
    total = sum(w for w, _ in pairs)
    ok = [(w, m) for w, m in pairs if m is not None and lo < m < hi]
    if not ok or not total:
        return None, 0.0
    return round(sum(w for w, _ in ok) / sum(w / m for w, m in ok), 1), round(
        sum(w for w, _ in ok) / total * 100, 0)


# --- main -----------------------------------------------------------------------

def main() -> None:
    cfg = load_portfolio()
    ts = now_ist()
    rf = float(cfg.get("analytics", {}).get("risk_free_pct", 6.5))
    state = read_json("state.json", {})

    bench_tickers = sorted({
        p["ticker"] for f in cfg["funds"]
        for p in (f.get("analytics_benchmark", {}).get("returns", [])
                  + f.get("analytics_benchmark", {}).get("pe", []))
    })
    prices = fetch_prices(bench_tickers)

    holdings = {f["id"]: load_holdings(f["id"]).get("holdings") or [] for f in cfg["funds"]}
    all_tickers = sorted({h["ticker"] for hs in holdings.values() for h in hs} | set(bench_tickers))
    funda = refresh_fundamentals(all_tickers, ts.date())

    funds_out = []
    for f in cfg["funds"]:
        fid = f["id"]
        ab = f.get("analytics_benchmark") or {}
        st = state.get(fid, {})
        invested = st.get("seed_invested", f.get("seed_invested"))
        value = (st.get("units") or 0) * (st.get("last_nav") or 0) or None
        out: dict = {
            "id": fid, "name": f["name"],
            "benchmark": {"name": ab.get("name"), "note": ab.get("note")},
            "mine": {
                "invested": invested, "value": round(value) if value else None,
                "gain_pct": round((value / invested - 1) * 100, 2) if value and invested else None,
            },
            "returns": None, "risk": None, "valuation": None, "history_days": None,
        }

        try:
            nav = fetch_nav_series(f["amfi_code"])
            bench = blended_level(ab.get("returns", []), prices)
            if nav is not None and bench is not None:
                levels = pd.concat([nav.rename("fund"), bench.rename("bench")], axis=1, sort=True).dropna()
                if len(levels) > 30:
                    end = levels.index[-1]
                    out["as_of"] = end.date().isoformat()
                    out["history_days"] = int((end - levels.index[0]).days)
                    rets = {}
                    for label, days in PERIODS.items():
                        fr = period_return(levels["fund"], end, days)
                        br = period_return(levels["bench"], end, days)
                        rets[label] = {
                            "fund": fr, "bench": br,
                            "diff": round(fr - br, 2) if fr is not None and br is not None else None,
                        }
                    out["returns"] = rets
                    out["risk"] = risk_metrics(levels, rf)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {fid}: return/risk metrics failed: {exc}", file=sys.stderr)

        try:
            hs = holdings[fid]
            fpe, fcover = weighted_multiple(
                [(float(h["weight_pct"]), (funda.get(h["ticker"]) or {}).get("pe")) for h in hs])
            # stocks with a P/E above 100 (one-off or depressed earnings) can move the fund
            # figure by a couple of points on their own, so report it both ways
            fpe_core, _ = weighted_multiple(
                [(float(h["weight_pct"]), (funda.get(h["ticker"]) or {}).get("pe")) for h in hs], hi=100.0)
            extreme = [(float(h["weight_pct"])) for h in hs
                       if (funda.get(h["ticker"]) or {}).get("pe") and 100 < funda[h["ticker"]]["pe"] < 500]
            fpb, _ = weighted_multiple(
                [(float(h["weight_pct"]), (funda.get(h["ticker"]) or {}).get("pb")) for h in hs],
                hi=100.0)
            bpe, _ = weighted_multiple(
                [(float(p["weight"]), (funda.get(p["ticker"]) or {}).get("pe"))
                 for p in ab.get("pe", [])])
            out["valuation"] = {"fund_pe": fpe, "fund_pe_cover_pct": fcover, "fund_pb": fpb,
                                "fund_pe_ex_extreme": fpe_core, "extreme_count": len(extreme),
                                "extreme_weight_pct": round(sum(extreme), 1), "bench_pe": bpe}
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {fid}: valuation failed: {exc}", file=sys.stderr)

        funds_out.append(out)

    write_json("analytics.json", {
        "generated_at": ts.isoformat(), "risk_free_pct": rf, "funds": funds_out,
    })
    for o in funds_out:
        r = o["risk"] or {}
        print(f"[ok] {o['id']}: 1Y fund/bench "
              f"{(o['returns'] or {}).get('1Y')}  beta {r.get('beta')} alpha {r.get('alpha_pct')} "
              f"PE {(o['valuation'] or {}).get('fund_pe')} vs {(o['valuation'] or {}).get('bench_pe')}")


if __name__ == "__main__":
    main()
