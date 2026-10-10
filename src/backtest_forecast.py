"""Re-run the walk-forward backtest of the next-session model and write docs/data/forecast_backtest.json.

The dashboard shows these numbers next to the live forecast, so the evidence sits beside the claim.
Run it occasionally (monthly is plenty): python src/backtest_forecast.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

import forecast as fc
from common import now_ist, write_json

FIRST_TEST = 2015


def main() -> None:
    d = yf.download(["^NSEI", "^GSPC"], period="max", interval="1d", progress=False, auto_adjust=False,
                    timeout=60, group_by="ticker")
    nifty_ohlc = d["^NSEI"][["Open", "Close"]].dropna()
    nifty_ohlc = nifty_ohlc[nifty_ohlc.index >= "2008-01-01"]
    if nifty_ohlc.index[-1].date() >= now_ist().date():         # unfinished bar
        nifty_ohlc = nifty_ohlc.iloc[:-1]
    nifty, spx = nifty_ohlc["Close"], d["^GSPC"]["Close"].dropna()
    wf = fc.walk_forward(nifty, spx, FIRST_TEST - 1)            # 2015 only seeds the error pool
    wf = wf.copy()
    allq = list(fc.Q70) + list(fc.Q95) + list(fc.PROB_QS)
    years = sorted(wf["year"].unique())
    ev = wf[wf["year"] >= FIRST_TEST]
    y = ev["r"].values
    n = len(ev)

    def pinball(q):
        e = y[:, None] - q
        return float(np.mean(np.maximum(fc.PROB_QS * e, (fc.PROB_QS - 1) * e)))

    def summarise(mu, sg, col):
        zq = np.vstack([fc.pool_quantiles(wf, col, Y, allq) for Y in ev["year"].values])
        lo70, hi70 = mu + sg * zq[:, 0], mu + sg * zq[:, 1]
        lo95, hi95 = mu + sg * zq[:, 2], mu + sg * zq[:, 3]
        q = mu[:, None] + sg[:, None] * zq[:, 4:]
        return {"pinball_bp": round(pinball(q) * 1e4, 3),
                "coverage70_pct": round(float(np.mean((y >= lo70) & (y <= hi70)) * 100), 1),
                "width70_pct": round(float(np.mean(hi70 - lo70) * 100), 2),
                "coverage95_pct": round(float(np.mean((y >= lo95) & (y <= hi95)) * 100), 1),
                "width95_pct": round(float(np.mean(hi95 - lo95) * 100), 2)}

    # the old model (EWMA 0.94, centred on zero) gets the SAME quantile treatment, so the comparison is fair
    zero = np.zeros(n)
    old = summarise(zero, ev["sigma_ewma"].values, "z_old")
    new = summarise(ev["mu"].values, ev["sigma_gjr"].values, "z")
    wf = ev

    p = wf["p_up"].values
    up = (y > 0)
    def hit(mask):
        return {"hit_pct": round(float(((p[mask] > .5) == up[mask]).mean() * 100), 1), "share_pct": round(float(mask.mean() * 100), 1), "n": int(mask.sum())}
    tiers = [
        {"tier": "leans up / leans down", "rule": "P(up) ≥ 60% or ≤ 40%", **hit(np.abs(p - .5) >= .10)},
        {"tier": "slight tilt or stronger", "rule": "P(up) ≥ 55% or ≤ 45%", **hit(np.abs(p - .5) >= .05)},
        {"tier": "every session", "rule": "any", **hit(np.ones(n, bool))},
    ]
    bins = [0, .40, .45, .50, .55, .60, .65, 1]
    cat = pd.cut(p, bins)
    rel = []
    for b, g in pd.DataFrame({"p": p, "u": up, "c": cat}).groupby("c", observed=True):
        rel.append({"bin": f"{b.left:.0%}–{b.right:.0%}", "n": int(len(g)), "predicted_pct": round(float(g.p.mean() * 100), 1), "actual_pct": round(float(g.u.mean() * 100), 1)})
    # how the Nifty's open gap relates to its full-day move (used for the GIFT-conditional range)
    opn = nifty_ohlc["Open"]; cl = nifty_ohlc["Close"]
    gap = (opn / cl.shift(1) - 1).dropna(); r_cc = cl.pct_change().dropna(); roc = (cl / opn - 1)
    idx = gap.index.intersection(r_cc.index); idx = idx[idx.year >= FIRST_TEST]
    slope = float(np.polyfit(gap[idx], r_cc[idx], 1)[0]); r2 = float(np.corrcoef(gap[idx], r_cc[idx])[0, 1] ** 2)
    # ---- red/green call evidence, per basis and confidence tier (walk-forward, same discipline)
    def tier_stats(p_arr, up_arr, basis):
        res = {}
        for name in (("very high", "good", "weak") if basis == "gift" else ("strong", "moderate", "weak")):
            mask = np.array([fc.call_tier(basis, q) == name for q in p_arr])
            if mask.sum() >= 30:
                res[name] = {"hit_pct": round(float(((p_arr[mask] > .5) == up_arr[mask]).mean() * 100), 1),
                             "share_pct": round(float(mask.mean() * 100), 1), "n": int(mask.sum())}
        res["all"] = {"hit_pct": round(float(((p_arr > .5) == up_arr).mean() * 100), 1), "share_pct": 100.0, "n": int(len(p_arr))}
        return res
    cc = nifty_ohlc["Close"].pct_change().dropna()
    gp_all = (nifty_ohlc["Open"] / nifty_ohlc["Close"].shift(1) - 1).reindex(cc.index) * 100
    yrs_all = cc.index.year.values; up_all = (cc.values > 0).astype(float)
    gift_p = np.full(len(cc), np.nan)
    NOISE = 0.15
    for Y in range(FIRST_TEST, int(yrs_all.max()) + 1):
        tr, te = (yrs_all < Y) & np.isfinite(gp_all.values), yrs_all == Y
        if te.sum() == 0:
            continue
        gm = fc.fit_gap_call(gp_all.values[tr], up_all[tr], NOISE)
        rng = np.random.default_rng(100 + Y)
        gift_p[te] = fc.logit_p(gm, np.nan_to_num(gp_all.values[te]) + rng.normal(0, NOISE, te.sum()))
    ok = np.isfinite(gift_p)
    call_evidence = {"us": tier_stats(p, up, "us"),
                     "gift": tier_stats(gift_p[ok], up_all[ok], "gift"),
                     "gift_noise_assumed_pct": NOISE,
                     "note": "gift: trained and tested on the Nifty's real opening gap blurred by the assumed noise; the live GIFT-to-open error will be measured from the log"}
    out = {"generated_at": now_ist().isoformat(), "method": "walk-forward: fitted on years before each test year, tested on that year",
           "test_period": [wf.index[0].date().isoformat(), wf.index[-1].date().isoformat()], "n_sessions": n,
           "calls": call_evidence,
           "range": {"old_ewma_normal": old, "new_gjr_sp500": new,
                     "pinball_improvement_pct": round((1 - new["pinball_bp"] / old["pinball_bp"]) * 100, 1)},
           "direction": {"always_up_pct": round(float(up.mean() * 100), 1), "tiers": tiers, "reliability": rel,
                         "brier": round(float(np.mean((p - up) ** 2)), 4), "brier_base_rate": round(float(np.mean((up.mean() - up) ** 2)), 4)},
           "gap": {"slope": round(slope, 2), "r2": round(r2, 2), "intraday_to_total_vol": round(float(roc[idx].std() / r_cc[idx].std()), 2),
                   "note": "estimated from the actual open gap; GIFT Nifty only approximates it, so the dashboard adds noise (see outlook.py)"}}
    write_json("forecast_backtest.json", out)
    print("old:", old); print("new:", new); print("improvement in pinball loss:", out["range"]["pinball_improvement_pct"], "%")
    for t in tiers: print(t)
    print("gap:", out["gap"])


if __name__ == "__main__":
    main()
