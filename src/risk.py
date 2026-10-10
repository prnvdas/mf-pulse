"""Risk and reward of the whole portfolio, from the funds' own NAV history -> docs/data/risk.json.

Everything is measured on what the portfolio you hold *today* would have done: each fund's real daily NAV
returns, combined with today's value weights (re-balanced daily). That is a "what if I had always held this
mix" view, which is the honest way to ask how risky today's mix is.

Sections: reward (CAGR and rolling-return spread), risk (volatility, drawdowns, value at risk in rupees,
downside beta), stress tests (Nifty shocks through each fund's downside beta; the portfolio's own worst
historical episodes replayed on today's money) and concentration (top stocks, overlap, cash).

Fail-safe: if the NAV history or the index can't be fetched, risk.json is left as it was.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yfinance as yf

from analytics import fetch_nav_series
from common import load_holdings, load_portfolio, now_ist, read_json, write_json

TRADING_DAYS = 252


def combine(returns: dict[str, pd.Series], weights: dict[str, float]) -> pd.Series:
    """Daily portfolio return with value weights, re-normalised on days where a fund has no history yet."""
    df = pd.DataFrame(returns)
    w = pd.Series(weights).reindex(df.columns).fillna(0.0)
    avail = df.notna() * w
    tot = avail.sum(axis=1).replace(0, np.nan)
    return (df.fillna(0.0) * w).sum(axis=1).div(tot).dropna()


def cagr(cum: pd.Series, years: float) -> float | None:
    n = int(round(years * TRADING_DAYS))
    if len(cum) <= n:
        return None
    return float((cum.iloc[-1] / cum.iloc[-1 - n]) ** (1 / years) - 1) * 100


def episodes(cum: pd.Series, min_depth: float = -0.07, top: int = 5) -> list[dict]:
    """The portfolio's own drawdown episodes (peak -> trough -> recovery), deepest first."""
    out, peak_i, peak_v = [], 0, cum.iloc[0]
    trough_i, trough_v = 0, cum.iloc[0]
    vals, idx = cum.values, cum.index
    for i in range(1, len(cum)):
        v = vals[i]
        if v >= peak_v:
            depth = trough_v / peak_v - 1
            if depth <= min_depth:
                out.append({"peak": idx[peak_i], "trough": idx[trough_i], "recovered": idx[i], "depth": depth})
            peak_i, peak_v, trough_i, trough_v = i, v, i, v
        elif v < trough_v:
            trough_i, trough_v = i, v
    depth = trough_v / peak_v - 1                       # an episode still open today
    if depth <= min_depth:
        out.append({"peak": idx[peak_i], "trough": idx[trough_i], "recovered": None, "depth": depth})
    out.sort(key=lambda e: e["depth"])
    res = []
    for e in out[:top]:
        res.append({"peak": e["peak"].date().isoformat(), "trough": e["trough"].date().isoformat(),
                    "recovered": e["recovered"].date().isoformat() if e["recovered"] is not None else None,
                    "depth_pct": round(e["depth"] * 100, 1),
                    "days_to_trough": int((e["trough"] - e["peak"]).days),
                    "days_to_recover": int((e["recovered"] - e["trough"]).days) if e["recovered"] is not None else None})
    return res


def downside_beta(port_w: pd.Series, mkt_w: pd.Series) -> tuple[float | None, float | None, float | None]:
    """(beta, downside beta, correlation) on weekly returns; downside beta uses only weeks the market fell."""
    d = pd.concat([port_w, mkt_w], axis=1, sort=True).dropna()
    if len(d) < 40:
        return None, None, None
    p, m = d.iloc[:, 0].values, d.iloc[:, 1].values
    beta = float(np.cov(p, m)[0, 1] / np.var(m, ddof=1))
    dn = m < 0
    dbeta = float(np.cov(p[dn], m[dn])[0, 1] / np.var(m[dn], ddof=1)) if dn.sum() >= 20 else beta
    return beta, dbeta, float(np.corrcoef(p, m)[0, 1])


def weekly(r: pd.Series) -> pd.Series:
    return (1 + r).resample("W-FRI").prod() - 1


def main() -> None:
    cfg, ts = load_portfolio(), now_ist()
    state = read_json("state.json", {})
    rf = float(cfg.get("analytics", {}).get("risk_free_pct", 6.5)) / 100
    fd = float((cfg.get("profile") or {}).get("fd_rate_pct", 7)) / 100

    navs, values = {}, {}
    for f in cfg["funds"]:
        st = state.get(f["id"], {})
        val = (st.get("units") or 0) * (st.get("last_nav") or 0)
        s = fetch_nav_series(f["amfi_code"])
        if s is None or val <= 0:
            continue
        navs[f["id"]], values[f["id"]] = s, val
    if not navs:
        print("[error] no NAV history; leaving risk.json unchanged", file=sys.stderr)
        sys.exit(1)
    total = sum(values.values())
    weights = {k: v / total for k, v in values.items()}
    rets = {k: v.pct_change().dropna() for k, v in navs.items()}
    port = combine(rets, weights)
    cum = (1 + port).cumprod()
    as_of = port.index[-1]

    try:
        d = yf.download("^NSEI", period="max", interval="1d", progress=False, auto_adjust=False, timeout=40)
        mkt = d["Close"].squeeze().dropna()
        mkt_r = mkt.pct_change().dropna()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Nifty history unavailable ({exc}); beta and stress tests skipped", file=sys.stderr)
        mkt_r = pd.Series(dtype=float)

    last3 = port.iloc[-3 * TRADING_DAYS:]
    vol1 = float(port.iloc[-TRADING_DAYS:].std() * np.sqrt(TRADING_DAYS)) * 100
    vol3 = float(last3.std() * np.sqrt(TRADING_DAYS)) * 100
    dd = cum / cum.cummax() - 1
    c1, c3, c5 = cagr(cum, 1), cagr(cum, 3), cagr(cum, 5)
    since_years = len(cum) / TRADING_DAYS
    c_all = float((cum.iloc[-1]) ** (1 / since_years) - 1) * 100 if since_years >= 1 else None
    dn_dev = float(np.sqrt(np.mean(np.minimum(last3 - rf / TRADING_DAYS, 0) ** 2)) * np.sqrt(TRADING_DAYS)) * 100
    ref = c3 if c3 is not None else c1
    sharpe = round((ref / 100 - rf) / (vol3 / 100), 2) if ref is not None else None
    sortino = round((ref / 100 - rf) / (dn_dev / 100), 2) if ref is not None and dn_dev else None
    max_dd = float(dd.min()) * 100
    calmar = round(ref / abs(max_dd), 2) if ref is not None and max_dd else None

    # rolling-return spread: the honest "what range of 1-, 3-, 5-year outcomes has this mix delivered"
    def rolling(years: float) -> dict | None:
        n = int(years * TRADING_DAYS)
        if len(cum) <= n + 20:
            return None
        r = (cum / cum.shift(n)).dropna() ** (1 / years) - 1
        r = r * 100
        return {"years": years, "windows": int(len(r)), "worst": round(float(r.min()), 1), "p5": round(float(r.quantile(.05)), 1),
                "p25": round(float(r.quantile(.25)), 1), "median": round(float(r.median()), 1), "p75": round(float(r.quantile(.75)), 1),
                "best": round(float(r.max()), 1), "positive_pct": round(float((r > 0).mean() * 100), 0),
                "beat_fd_pct": round(float((r > fd * 100).mean() * 100), 0)}
    rolls = [x for x in (rolling(1), rolling(3), rolling(5)) if x]

    # value at risk, in rupees, on today's money (historical, last 3 years)
    q = lambda p: float(np.quantile(last3, p))
    var95, var99 = -q(0.05), -q(0.01)
    es95 = -float(last3[last3 <= q(0.05)].mean())
    weekly_r, monthly_r = weekly(port), (1 + port).resample("ME").prod() - 1

    # beta to the Nifty 50 and stress tests
    beta = dbeta = corr = None
    stress, fund_rows = [], []
    if len(mkt_r):
        mw = weekly(mkt_r).iloc[-3 * 52:]
        beta, dbeta, corr = downside_beta(weekly_r.iloc[-3 * 52:], mw)
        betas = {}
        for k, r in rets.items():
            b, db, c = downside_beta(weekly(r).iloc[-3 * 52:], mw)
            betas[k] = (b, db, c)
        if dbeta is not None:
            for shock in (-10, -20, -30):
                row = {"nifty_pct": shock, "portfolio_pct": round(dbeta * shock, 1), "rupees": round(total * dbeta * shock / 100)}
                row["funds"] = [{"id": k, "pct": round((betas[k][1] or dbeta) * shock, 1), "rupees": round(values[k] * (betas[k][1] or dbeta) * shock / 100)}
                                for k in values]
                stress.append(row)
        # share of the portfolio's variance each fund contributes (needs a common window of at least a year)
        # only funds with a real track record (>= 2 years) enter the covariance; the rest are shown without a share
        long = {k: r for k, r in rets.items() if len(r) >= 2 * TRADING_DAYS}
        contrib = {}
        if len(long) >= 2:
            df = pd.DataFrame(long).dropna()
            ww = pd.Series({k: weights[k] for k in df.columns}); ww = (ww / ww.sum()).values
            cov = df.iloc[-3 * TRADING_DAYS:].cov().values
            pv = float(ww @ cov @ ww)
            share = {c: float(ww[i] * (cov @ ww)[i] / pv * 100) for i, c in enumerate(df.columns)}
            scale = sum(weights[c] for c in df.columns)             # the funds left out hold the rest of the money
            contrib = {c: v * scale for c, v in share.items()}
        for f in cfg["funds"]:
            k = f["id"]
            if k not in navs:
                continue
            r = rets[k].iloc[-3 * TRADING_DAYS:]
            fc = (1 + rets[k]).cumprod()
            fund_rows.append({"id": k, "name": f["name"], "weight_pct": round(weights[k] * 100, 1), "value": round(values[k]),
                              "vol_pct": round(float(r.std() * np.sqrt(TRADING_DAYS)) * 100, 1),
                              "max_drawdown_pct": round(float((fc / fc.cummax() - 1).min()) * 100, 1),
                              "beta": round(betas[k][0], 2) if betas.get(k) and betas[k][0] is not None else None,
                              "downside_beta": round(betas[k][1], 2) if betas.get(k) and betas[k][1] is not None else None,
                              "risk_share_pct": round(contrib[k], 0) if k in contrib else None,
                              "history_years": round(len(rets[k]) / TRADING_DAYS, 1)})
    # the portfolio's own worst episodes, replayed on today's money
    eps = episodes(cum)
    for e in eps:
        e["rupees_today"] = round(total * e["depth_pct"] / 100)

    # concentration: top stocks across all three funds, overlap, cash
    agg: dict[str, dict] = {}
    cash = 0.0
    for f in cfg["funds"]:
        k = f["id"]
        if k not in values:
            continue
        h = load_holdings(k)
        cash += values[k] * float(h.get("cash_pct") or 0) / 100
        for x in h.get("holdings") or []:
            a = agg.setdefault(x["ticker"], {"name": x["name"], "ticker": x["ticker"], "value": 0.0, "funds": set()})
            a["value"] += values[k] * float(x["weight_pct"]) / 100
            a["funds"].add(f["name"].split(" Fund")[0].replace(" Direct Growth", ""))
    rows = sorted(agg.values(), key=lambda a: -a["value"])
    multi = [a for a in rows if len(a["funds"]) >= 2]
    conc = {"top": [{"name": a["name"], "value": round(a["value"]), "pct": round(a["value"] / total * 100, 1), "funds": sorted(a["funds"])} for a in rows[:10]],
            "top10_pct": round(sum(a["value"] for a in rows[:10]) / total * 100, 1),
            "stocks": len(rows), "overlap_stocks": len(multi),
            "overlap_pct": round(sum(a["value"] for a in multi) / total * 100, 1),
            "cash_pct": round(cash / total * 100, 1)}

    mkt_cagr = {}
    if len(mkt_r):
        mc = (1 + mkt_r).cumprod()
        for y in (1, 3, 5):
            v = cagr(mc, y)
            if v is not None:
                mkt_cagr[f"{y}y"] = round(v, 1)
    out = {"generated_at": ts.isoformat(), "as_of": as_of.date().isoformat(), "value": round(total), "risk_free_pct": rf * 100, "fd_rate_pct": fd * 100,
           "basis": f"today's mix applied to each fund's real NAV history ({len(port):,} sessions, since {port.index[0].date().isoformat()})",
           "reward": {"cagr_1y": round(c1, 1) if c1 is not None else None, "cagr_3y": round(c3, 1) if c3 is not None else None,
                      "cagr_5y": round(c5, 1) if c5 is not None else None, "cagr_all": round(c_all, 1) if c_all is not None else None,
                      "nifty_cagr": mkt_cagr, "rolling": rolls},
           "risk": {"vol_1y_pct": round(vol1, 1), "vol_3y_pct": round(vol3, 1), "max_drawdown_pct": round(max_dd, 1),
                    "current_drawdown_pct": round(float(dd.iloc[-1]) * 100, 1), "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
                    "beta": round(beta, 2) if beta is not None else None, "downside_beta": round(dbeta, 2) if dbeta is not None else None,
                    "correlation": round(corr, 2) if corr is not None else None,
                    "var95_pct": round(var95 * 100, 2), "var99_pct": round(var99 * 100, 2), "es95_pct": round(es95 * 100, 2),
                    "var95_rupees": round(total * var95), "var99_rupees": round(total * var99), "es95_rupees": round(total * es95),
                    "worst_day_pct": round(float(port.min()) * 100, 1), "worst_week_pct": round(float(weekly_r.min()) * 100, 1),
                    "worst_month_pct": round(float(monthly_r.min()) * 100, 1)},
           "stress": stress, "episodes": eps, "funds": fund_rows, "concentration": conc}
    write_json("risk.json", out)
    print(f"[ok] risk.json: value ₹{total:,.0f}; vol {vol1:.1f}%; max drawdown {max_dd:.1f}%; 1-day 95% VaR ₹{total * var95:,.0f}; "
          f"downside beta {dbeta if dbeta is None else round(dbeta, 2)}; top-10 stocks {conc['top10_pct']}%")


if __name__ == "__main__":
    main()
