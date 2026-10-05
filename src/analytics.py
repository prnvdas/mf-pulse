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

from common import CONFIG, load_holdings, load_portfolio, now_ist, read_json, write_json

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
            tickers=" ".join(tickers), period="max", interval="1d",
            group_by="ticker", progress=False, threads=True, auto_adjust=False, timeout=30,
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


# --- "if a bull market comes": what this fund's own history says -----------------

def rolling_year_returns(level: pd.Series) -> pd.Series:
    """Every overlapping 1-year return the series has actually produced, in %."""
    level = level.dropna().sort_index()
    if len(level) < 300:
        return pd.Series(dtype=float)
    prior = level.asof(level.index - pd.Timedelta(days=365))
    ok = level.index >= level.index[0] + pd.Timedelta(days=365)
    return ((level[ok] / prior[ok].values - 1) * 100).dropna()


def history_scenarios(nav: pd.Series | None, bench: pd.Series | None) -> dict | None:
    """Typical / strong / best 1-year outcomes from real history.

    Uses the fund's own NAV when it has 3+ years; a young fund borrows its benchmark's
    history instead (and says so). This is "what has happened", not a forecast.
    """
    for basis, series in (("fund", nav), ("benchmark", bench)):
        if series is None or len(series) < 2:
            continue
        span = (series.index[-1] - series.index[0]).days
        if basis == "fund" and span < 3 * 365:
            continue
        r = rolling_year_returns(series)
        if len(r) < 100:
            continue
        return {
            "basis": basis, "since": series.index[0].date().isoformat(), "years": round(span / 365, 1),
            "worst": round(float(r.min()), 1), "p25": round(float(np.percentile(r, 25)), 1),
            "median": round(float(r.median()), 1), "p75": round(float(np.percentile(r, 75)), 1),
            "best": round(float(r.max()), 1), "share_positive": round(float((r > 0).mean() * 100)),
        }
    return None


def recovery(nav: pd.Series | None) -> dict | None:
    """How far the latest NAV sits below its 6-month high."""
    if nav is None or len(nav) < 30:
        return None
    win = nav[nav.index > nav.index[-1] - pd.Timedelta(days=182)]
    peak, last = float(win.max()), float(nav.iloc[-1])
    return {"nav_last": round(last, 4), "nav_peak_6m": round(peak, 4),
            "gap_pct": round((peak / last - 1) * 100, 1), "peak_date": win.idxmax().date().isoformat()}


# --- XIRR -----------------------------------------------------------------------

def xirr(flows: list[tuple[dt.date, float]]) -> float | None:
    """Annualised internal rate of return for dated cash flows (outflows negative).

    Plain bisection, so no numerical-library dependency. None when it has no solution.
    """
    if not flows or not any(v < 0 for _, v in flows) or not any(v > 0 for _, v in flows):
        return None
    flows = sorted(flows)
    t0 = flows[0][0]

    def npv(r: float) -> float:
        return sum(v / (1 + r) ** ((d - t0).days / 365.0) for d, v in flows)

    lo, hi = -0.95, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def parse_date(txt: str) -> dt.date:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(txt.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {txt!r}")


def load_transactions() -> dict[str, list[tuple[dt.date, float]]]:
    """config/transactions.csv: date,fund_id,amount (positive = invested, negative = withdrawn)."""
    path = CONFIG / "transactions.csv"
    out: dict[str, list[tuple[dt.date, float]]] = {}
    if not path.exists():
        return out
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or line.lower().startswith("date"):
            continue
        try:
            d, fid, amt = [x.strip() for x in line.split(",")[:3]]
            out.setdefault(fid, []).append((parse_date(d), float(amt.replace("₹", ""))))
        except ValueError as exc:
            print(f"[warn] transactions.csv line {n} skipped: {exc}", file=sys.stderr)
    return out


def fund_xirr(fid: str, txns: dict, invested, value, as_of: dt.date, bench: pd.Series | None,
              bench_last: float | None, screenshot: dict) -> dict | None:
    """Exact XIRR from logged transactions; else the figure an app printed; else None."""
    rows = txns.get(fid)
    if rows:
        logged = sum(a for _, a in rows)
        if invested and abs(logged / invested - 1) > 0.01:
            return {"status": "mismatch", "logged": round(logged), "invested": round(invested)}
        flows = [(d, -a) for d, a in rows] + [(as_of, float(value))]
        r = xirr(flows)
        if r is None:
            return None
        out = {"pct": round(r * 100, 1), "source": "transactions", "since": min(d for d, _ in rows).isoformat(),
               "bench_pct": None}
        # the same cash flows invested in the benchmark instead -- an apples-to-apples yardstick
        if bench is not None and bench_last and min(d for d, _ in rows) >= bench.index[0].date():
            units = sum(a / float(bench.asof(pd.Timestamp(d))) for d, a in rows)
            rb = xirr([(d, -a) for d, a in rows] + [(as_of, units * bench_last)])
            out["bench_pct"] = None if rb is None else round(rb * 100, 1)
        return out
    if screenshot.get("xirr_pct") is not None:
        return {"pct": screenshot["xirr_pct"], "source": "screenshot", "as_of": screenshot.get("xirr_date"),
                "bench_pct": None}
    return None


# --- valuation ----------------------------------------------------------------

def refresh_fundamentals(tickers: list[str], today: dt.date) -> dict:
    cache = read_json("fundamentals.json", {})
    stale = [
        t for t in tickers
        if t not in cache
        or "ey" not in cache[t]
        or (today - dt.date.fromisoformat(cache[t].get("date", "2000-01-01"))).days
        > FUNDAMENTALS_MAX_AGE_DAYS
    ]
    print(f"[info] fundamentals: {len(stale)} of {len(tickers)} tickers need a refresh")

    def one(t: str):
        try:
            info = yf.Ticker(t).info
            eps = info.get("trailingEps")
            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
            pe = info.get("trailingPE")
            # earnings yield keeps the sign: a loss-maker has negative earnings, which
            # a P/E (None for losses) throws away
            ey = eps / price if eps is not None and price else (1 / pe if pe else None)
            return t, {"pe": pe, "pb": info.get("priceToBook"), "ey": ey, "date": today.isoformat()}
        except Exception:  # noqa: BLE001 -- keep the old value, retry next run
            return t, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, v in pool.map(one, stale):
            if v is not None and (v["pe"] is not None or v["pb"] is not None or v["ey"] is not None):
                cache[t] = v
            elif t not in cache:
                cache[t] = {"pe": None, "pb": None, "ey": None, "date": today.isoformat()}
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


def aggregate_pe(pairs: list[tuple[float, float | None]]):
    """Price-to-earnings of the whole basket: total price / total yearly earnings.

    Each holding contributes weight * earnings-yield, loss-makers with their negative
    earnings, so there is no averaging of P/Es and a stock with a tiny profit (P/E in
    the hundreds) contributes almost nothing instead of distorting the figure.
    Returns (P/E, share of weight that had earnings data).
    """
    have = [(w, ey) for w, ey in pairs if ey is not None]
    total = sum(w for w, _ in pairs)
    if not have or not total:
        return None, 0.0
    earnings = sum(w * ey for w, ey in have)
    if earnings <= 0:
        return None, 0.0
    return round(sum(w for w, _ in have) / earnings, 1), round(sum(w for w, _ in have) / total * 100, 0)


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

    txns = load_transactions()
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
                    out["_bench"] = (bench, float(bench.iloc[-1]))
                out["scenarios"] = history_scenarios(nav, bench)
                out["recovery"] = recovery(nav)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {fid}: return/risk metrics failed: {exc}", file=sys.stderr)

        try:
            b = out.pop("_bench", None)
            as_of = (pd.Timestamp(out["as_of"]).date() if out.get("as_of") else ts.date())
            out["xirr"] = fund_xirr(fid, txns, invested, value, as_of, b[0] if b else None,
                                    b[1] if b else None, st)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {fid}: xirr failed: {exc}", file=sys.stderr)
            out.pop("_bench", None)
            out["xirr"] = None

        try:
            hs = holdings[fid]
            fpe, fcover = aggregate_pe(
                [(float(h["weight_pct"]), (funda.get(h["ticker"]) or {}).get("ey")) for h in hs])
            fpb, _ = weighted_multiple(
                [(float(h["weight_pct"]), (funda.get(h["ticker"]) or {}).get("pb")) for h in hs],
                hi=100.0)
            bpe, _ = weighted_multiple(
                [(float(p["weight"]), (funda.get(p["ticker"]) or {}).get("pe"))
                 for p in ab.get("pe", [])])
            out["valuation"] = {"fund_pe": fpe, "fund_pe_cover_pct": fcover, "fund_pb": fpb,
                                "bench_pe": bpe}
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {fid}: valuation failed: {exc}", file=sys.stderr)

        funds_out.append(out)

    portfolio_xirr = None
    if txns and all(o.get("xirr") and o["xirr"].get("source") == "transactions" for o in funds_out):
        flows = [(d, -a) for rows in txns.values() for d, a in rows]
        flows += [(pd.Timestamp(o["as_of"]).date() if o.get("as_of") else ts.date(), float(o["mine"]["value"] or 0))
                  for o in funds_out]
        r = xirr(flows)
        portfolio_xirr = None if r is None else round(r * 100, 1)

    write_json("analytics.json", {
        "generated_at": ts.isoformat(), "risk_free_pct": rf, "portfolio_xirr_pct": portfolio_xirr,
        "funds": funds_out,
    })
    for o in funds_out:
        r = o["risk"] or {}
        print(f"[ok] {o['id']}: 1Y fund/bench "
              f"{(o['returns'] or {}).get('1Y')}  beta {r.get('beta')} alpha {r.get('alpha_pct')} "
              f"PE {(o['valuation'] or {}).get('fund_pe')} vs {(o['valuation'] or {}).get('bench_pe')}")


if __name__ == "__main__":
    main()
