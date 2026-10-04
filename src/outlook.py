"""Next-session setup: how big a move to expect, and a lean from the overnight US market.

Deliberately NOT technical analysis. Backtested on ~4,400 Nifty days, RSI / MACD /
moving averages / momentum did no better than guessing "up" every day (53%). Two
things did carry information, and only those are used here:

  * volatility clusters, so a RiskMetrics-style EWMA (lambda 0.94) of recent daily
    moves is a good guide to the *size* of tomorrow's move. Calibration is measured
    each run (about 70% of days land inside 1 sigma, ~94.5% inside 2 sigma).
  * the S&P 500's move on the date of the Nifty close precedes the next Nifty session
    (the US closes ~01:30 IST) and shifts the odds: after a big US fall Nifty rose
    only ~30% of the next days; after a big rise ~69%. The odds come straight from
    history (bucketed by the S&P move), never from a fitted rule.

It is a lean with stated odds, not a prediction. Every call is logged and scored
against the real next close (docs/data/outlook_history.json), so the dashboard shows
its own track record.

Writes docs/data/outlook.json. Runs at ~07:30 IST (US move known) and ~17:30 IST
(range for the next session; the lean is 'pending' until the US has closed).
"""

from __future__ import annotations

import datetime as dt
import sys

import numpy as np
import pandas as pd
import yfinance as yf

from analytics import fetch_nav_series
from common import load_portfolio, now_ist, read_json, write_json

LAMBDA = 0.94
BINS = [-99, -1.0, -0.3, 0.3, 1.0, 99]
LABELS = ["fell more than 1%", "fell 0.3–1%", "was flat (within 0.3%)", "rose 0.3–1%", "rose more than 1%"]
SPLIT = pd.Timestamp("2020-01-01")


def ewma_sigma(returns: pd.Series, lam: float = LAMBDA) -> pd.Series:
    """sigma[i] = forecast for day i from returns strictly before day i."""
    v = np.empty(len(returns))
    v[0] = returns.iloc[: min(30, len(returns))].var()
    for i in range(1, len(returns)):
        v[i] = lam * v[i - 1] + (1 - lam) * returns.iloc[i - 1] ** 2
    return pd.Series(np.sqrt(v), index=returns.index)


def next_sigma(returns: pd.Series, lam: float = LAMBDA) -> float:
    s = ewma_sigma(returns, lam)
    last_var = lam * s.iloc[-1] ** 2 + (1 - lam) * returns.iloc[-1] ** 2
    return float(np.sqrt(last_var))


def portfolio_sigma(cfg: dict, state: dict) -> tuple[float | None, float]:
    """EWMA sigma of the value-weighted portfolio's daily return (from fund NAVs), and its value."""
    cols, weights = {}, {}
    for f in cfg["funds"]:
        st = state.get(f["id"], {})
        val = (st.get("units") or 0) * (st.get("last_nav") or 0)
        nav = fetch_nav_series(f["amfi_code"])
        if nav is not None and val > 0:
            cols[f["id"]], weights[f["id"]] = nav.pct_change().tail(300), val
    total = sum(weights.values())
    if not cols or not total:
        return None, total
    df = pd.DataFrame(cols).dropna()
    if len(df) < 60:
        return None, total
    port = sum(df[k] * (weights[k] / total) for k in df.columns)
    return next_sigma(port), total


def main() -> None:
    cfg, ts = load_portfolio(), now_ist()
    state = read_json("state.json", {})
    d = yf.download(["^NSEI", "^GSPC"], period="max", interval="1d", progress=False,
                    auto_adjust=False, group_by="ticker")
    nifty = d["^NSEI"]["Close"].dropna()
    nifty = nifty[nifty.index >= "2008-01-01"]
    sp = d["^GSPC"]["Close"].dropna()
    if len(nifty) < 500 or len(sp) < 500:
        print("[error] not enough index history; leaving outlook.json unchanged", file=sys.stderr)
        sys.exit(1)

    ret = nifty.pct_change().dropna()
    base_date, base_close = nifty.index[-1], float(nifty.iloc[-1])
    sigma = next_sigma(ret)

    # --- calibration of the range, measured not assumed
    s_hist = ewma_sigma(ret)
    m = s_hist.index >= ret.index[60]
    a = ret.abs()
    calib = {"within1_pct": round(float((a[m] <= s_hist[m]).mean() * 100), 1),
             "within2_pct": round(float((a[m] <= 2 * s_hist[m]).mean() * 100), 1), "n": int(m.sum())}

    # --- lean: bucket history by the S&P move that preceded each next Nifty session
    sp_ret = sp.pct_change()
    hist = pd.DataFrame({"nxt": nifty.pct_change().shift(-1)}).dropna()
    hist["sp"] = sp_ret.reindex(hist.index) * 100
    hist = hist.dropna()
    hist["b"] = pd.cut(hist.sp, bins=BINS, labels=LABELS)
    table = []
    for lab in LABELS:
        g = hist[hist.b == lab]
        table.append({"bucket": lab, "n": int(len(g)), "p_up": round(float((g.nxt > 0).mean() * 100), 1),
                      "avg_next_pct": round(float(g.nxt.mean() * 100), 2)})
    tr, te = hist[hist.index < SPLIT], hist[hist.index >= SPLIT]
    p_tr = tr.groupby("b", observed=True).nxt.apply(lambda x: (x > 0).mean())
    guess = te.b.map(lambda b: 1 if p_tr.get(b, 0.5) > 0.5 else 0).astype(int)
    oos = {"hit_pct": round(float(((te.nxt > 0).astype(int) == guess).mean() * 100), 1),
           "always_up_pct": round(float((te.nxt > 0).mean() * 100), 1), "n": int(len(te)),
           "test_from": SPLIT.date().isoformat()}

    # Today's feature: the S&P move across every US session that has closed since the last
    # Nifty close. Normally that is one session (identical to the backtested rule); after a
    # Nifty holiday there can be two, and ignoring the extra one would throw away news.
    lean = {"status": "pending", "reason": "The US market hasn't closed since the last Nifty close yet; this updates around 07:30 IST."}
    done = [d for d in sp.index if d >= base_date and
            ts >= (pd.Timestamp(d) + pd.Timedelta(days=1, hours=2, minutes=30)).tz_localize(ts.tzinfo)]
    before = sp[sp.index < base_date]
    if done and len(before):
        last_us = max(done)
        move = float(sp.loc[last_us] / before.iloc[-1] - 1) * 100
        lab = pd.cut([move], bins=BINS, labels=LABELS)[0]
        row = next(r for r in table if r["bucket"] == lab)
        label = "leans up" if row["p_up"] >= 60 else "leans down" if row["p_up"] <= 40 else "no clear lean"
        lean = {"status": "ok", "label": label, "p_up": row["p_up"], "n": row["n"], "bucket": lab,
                "avg_next_pct": row["avg_next_pct"], "sp_date": last_us.date().isoformat(),
                "sp_move_pct": round(move, 2), "us_sessions": len(done)}

    # --- ranges
    def rng(sig: float, k: float) -> list[float]:
        return [round(base_close * (1 - k * sig), 1), round(base_close * (1 + k * sig), 1)]

    psig, pvalue = portfolio_sigma(cfg, state)
    portfolio = None
    if psig:
        portfolio = {"value": round(pvalue), "sigma_pct": round(psig * 100, 2),
                     "typical_rupees": round(pvalue * psig), "wide_rupees": round(pvalue * psig * 2)}

    # --- track record: log this call, score earlier ones against the real next close
    hist_log = read_json("outlook_history.json", [])
    by_base = {h["base_date"]: h for h in hist_log}
    open_session = ts.replace(hour=9, minute=15, second=0, microsecond=0)
    key = base_date.date().isoformat()
    entry = {"base_date": key, "made_at": ts.isoformat(), "sigma_pct": round(sigma * 100, 3),
             "lean": lean.get("label"), "p_up": lean.get("p_up")}
    if key not in by_base:
        hist_log.append(entry)
    elif by_base[key].get("actual_pct") is None and ts < open_session and ts.date() > base_date.date():
        by_base[key].update(entry)      # the morning run refines the call, only before the open
    for h in hist_log:
        if h.get("actual_pct") is not None:
            continue
        bd = pd.Timestamp(h["base_date"])
        later = nifty[nifty.index > bd]
        if len(later) and bd in nifty.index:
            act = float(later.iloc[0] / nifty.loc[bd] - 1) * 100
            h["actual_date"] = later.index[0].date().isoformat()
            h["actual_pct"] = round(act, 2)
            h["inside_1s"] = abs(act) <= h["sigma_pct"]
            h["inside_2s"] = abs(act) <= 2 * h["sigma_pct"]
            if h.get("lean") in ("leans up", "leans down"):
                h["lean_hit"] = (act > 0) == (h["lean"] == "leans up")
    hist_log = hist_log[-250:]
    scored = [h for h in hist_log if h.get("actual_pct") is not None]
    called = [h for h in scored if "lean_hit" in h]
    record = {"sessions": len(scored), "range_1s_pct": round(100 * sum(h["inside_1s"] for h in scored) / len(scored), 0) if scored else None,
              "range_2s_pct": round(100 * sum(h["inside_2s"] for h in scored) / len(scored), 0) if scored else None,
              "calls": len(called), "lean_hit_pct": round(100 * sum(h["lean_hit"] for h in called) / len(called), 0) if called else None}
    write_json("outlook_history.json", hist_log)

    write_json("outlook.json", {
        "generated_at": ts.isoformat(), "base_close_date": key, "base_close": round(base_close, 2),
        "sigma_pct": round(sigma * 100, 2),
        "nifty": {"typical": rng(sigma, 1), "wide": rng(sigma, 2)},
        "portfolio": portfolio, "lean": lean, "buckets": table, "calibration": calib,
        "lean_out_of_sample": oos, "record": record,
    })
    print(f"[ok] outlook: Nifty {base_close:,.0f} typical ±{sigma*100:.2f}%, lean={lean.get('label', lean['status'])}, "
          f"portfolio ±{portfolio['typical_rupees']:,}" if portfolio else f"[ok] outlook: sigma {sigma*100:.2f}%")
    print(f"     calibration {calib}  out-of-sample {oos}  record {record}")


if __name__ == "__main__":
    main()
