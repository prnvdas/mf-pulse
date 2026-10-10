"""Next-session setup: how big a move to expect, and a lean from the overnight US market (+ GIFT Nifty).

Deliberately NOT technical analysis (RSI / MACD / moving averages / momentum tested no better than
guessing "up" every day). The model, and the evidence for each part, lives in forecast.py and in
docs/data/forecast_backtest.json (walk-forward, 2015-2026):

  * SIZE: GJR-GARCH(1,1)-t volatility. Bands are the empirical quantiles of the model's own
    out-of-sample errors over the last five years, so they track the current regime.
  * DIRECTION / CENTRE: the S&P 500's last US session (the only overnight input that helped).
    Output is a probability of "up", worded in tiers that map to measured hit rates.
  * GIFT NIFTY: once its pre-open price is known it tightens the range (the Nifty's day is the
    open gap plus an intraday move that is ~19% less volatile). This part is NOT validated yet
    (no free GIFT history), so every reading is logged to be scored against the real open.

It is a probability, not a prediction. Every forecast is logged and scored against the real next
close (docs/data/outlook_history.json) and the dashboard shows the record.

Writes docs/data/outlook.json. Runs ~07:15 IST (US move known), after the close, and with the
GIFT snapshots.
"""

from __future__ import annotations

import datetime as dt
import sys

import numpy as np
import pandas as pd
import yfinance as yf

import forecast as fc
from analytics import fetch_nav_series
from common import load_portfolio, now_ist, read_json, session_end, write_json

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


GIFT_NOISE = 0.0015       # assumed sd of (GIFT-implied gap - actual open gap); to be measured from the log


def _num_time(txt: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(txt, "%d-%b-%Y %H:%M:%S").replace(tzinfo=now_ist().tzinfo)
    except (TypeError, ValueError):
        return None


def gift_reading(ts: dt.datetime, base_close: float) -> dict | None:
    """GIFT Nifty's gap from the Nifty's last close, if the snapshot is fresh (<14 h)."""
    g = read_json("gift.json", {})
    qt = _num_time(g.get("quote_time")) if isinstance(g, dict) else None
    if not g or not g.get("last") or qt is None or (ts - qt) > dt.timedelta(hours=14) or qt > ts + dt.timedelta(minutes=5):
        return None
    return {"gap": float(g["last"]) / base_close - 1.0, "last": float(g["last"]), "quote_time": g["quote_time"], "source": g.get("source")}


def open_session_today(cfg: dict, ts: dt.datetime) -> dt.datetime:
    hh, mm = (int(x) for x in str(cfg["market"]["open"]).split(":"))
    return ts.replace(hour=hh, minute=mm, second=0, microsecond=0)


def main() -> None:
    cfg, ts = load_portfolio(), now_ist()
    state = read_json("state.json", {})
    d = yf.download(["^NSEI", "^GSPC"], period="max", interval="1d", progress=False,
                    auto_adjust=False, timeout=30, group_by="ticker")
    try:
        nifty_ohlc = d["^NSEI"][["Open", "Close"]].dropna()
    except KeyError:   # Yahoo returned nothing at all
        print("[error] no index data from Yahoo; leaving outlook.json unchanged", file=sys.stderr)
        sys.exit(1)
    nifty_ohlc = nifty_ohlc[nifty_ohlc.index >= "2008-01-01"]
    # During the session Yahoo's daily series ends with today's unfinished bar; that is not a
    # "last close". (A delayed morning run on 5 Oct hit exactly this.)
    if len(nifty_ohlc) and nifty_ohlc.index[-1].date() == ts.date() and ts < session_end(cfg, ts) + dt.timedelta(minutes=15):
        nifty_ohlc = nifty_ohlc.iloc[:-1]
    sp = d["^GSPC"]["Close"].dropna()
    nifty = nifty_ohlc["Close"]
    if len(nifty) < 500 or len(sp) < 500:
        print("[error] not enough index history; leaving outlook.json unchanged", file=sys.stderr)
        sys.exit(1)

    ret = nifty.pct_change().dropna()
    base_date, base_close = nifty.index[-1], float(nifty.iloc[-1])
    ewma_sigma = fc.ewma_next_sigma(ret)                       # the old forecast: fallback + scale for the portfolio

    # --- the model: GJR-GARCH sigma, bands from recent out-of-sample errors, S&P signal
    try:
        wf = fc.walk_forward(nifty, sp, 2015)
    except Exception as exc:  # noqa: BLE001 -- the card must degrade, not disappear
        print(f"[warn] walk-forward calibration failed ({exc}); using default bands", file=sys.stderr)
        wf = pd.DataFrame(columns=["year", "z", "z_old"])
    res = fc.fit_gjr(ret)
    if res is not None:
        sigma, zcol, vol_model = fc.gjr_next_sigma(res), "z", "GJR-GARCH(1,1) with Student-t errors"
    else:                                                       # never block the card on a failed fit
        sigma, zcol, vol_model = ewma_sigma, "z_old", "EWMA 0.94 (GJR fit failed this run)"
        print("[warn] GJR-GARCH did not converge; using the EWMA fallback", file=sys.stderr)
    allq = list(fc.Q70) + list(fc.Q95)
    zq = fc.pool_quantiles(wf, zcol, ts.year + 1, allq)      # last 5 years of out-of-sample errors

    x_all = fc.spx_prior(ret.index, sp)
    sm = fc.fit_signal(x_all.values, ret.values)

    # Today's feature: the S&P move across every US session that has closed since the last Nifty
    # close. Normally one session; after a Nifty holiday there can be two.
    lean = {"status": "pending", "reason": "The US market hasn't closed since the last Nifty close yet; this updates around 07:15 IST."}
    mu, x_next = 0.0, None
    done = [u for u in sp.index if u >= base_date and
            ts >= (pd.Timestamp(u) + pd.Timedelta(days=1, hours=2, minutes=30)).tz_localize(ts.tzinfo)]
    before = sp[sp.index < base_date]
    if done and len(before):
        last_us = max(done)
        x_next = float(sp.loc[last_us] / before.iloc[-1] - 1)
        mu_a, p_a = fc.predict_signal(sm, [x_next])
        mu, p_up = float(mu_a[0]), float(p_a[0])
        lean = {"status": "ok", "label": fc.tier(p_up), "p_up": round(p_up * 100, 1), "mu_pct": round(mu * 100, 2),
                "sp_date": last_us.date().isoformat(), "sp_move_pct": round(x_next * 100, 2), "us_sessions": len(done)}

    def band(centre: float, sig: float, k: tuple[float, float]) -> list[float]:
        return [centre + sig * k[0], centre + sig * k[1]]

    typ, wide = band(mu, sigma, (zq[0], zq[1])), band(mu, sigma, (zq[2], zq[3]))
    lv = lambda rel: [round(base_close * (1 + rel[0]), 1), round(base_close * (1 + rel[1]), 1)]
    pc = lambda rel: [round(rel[0] * 100, 2), round(rel[1] * 100, 2)]
    nifty_out = {"typical": lv(typ), "wide": lv(wide), "typical_pct": pc(typ), "wide_pct": pc(wide), "centre_pct": round(mu * 100, 2)}

    # --- GIFT Nifty: the open gap is mostly known before the open, leaving only the intraday move
    backtest = read_json("forecast_backtest.json", {})
    gp = (backtest.get("gap") or {})
    slope, ratio = float(gp.get("slope", 0.94)), float(gp.get("intraday_to_total_vol", 0.81))
    # Not during the next session itself: by then the gap would include that session's own move.
    in_session = base_date.date() < ts.date() and open_session_today(cfg, ts) <= ts <= session_end(cfg, ts) + dt.timedelta(minutes=15)
    gift = None if in_session else gift_reading(ts, base_close)
    if gift:
        centre_g = slope * gift["gap"]
        sig_g = float(np.sqrt((ratio * sigma) ** 2 + GIFT_NOISE ** 2))
        gt, gw = band(centre_g, sig_g, (zq[0], zq[1])), band(centre_g, sig_g, (zq[2], zq[3]))
        nifty_out["gift_adjusted"] = {"gap_pct": round(gift["gap"] * 100, 2), "last": gift["last"], "quote_time": gift["quote_time"],
                                       "centre_pct": round(centre_g * 100, 2), "typical": lv(gt), "wide": lv(gw),
                                       "typical_pct": pc(gt), "wide_pct": pc(gw), "validated": False,
                                       "assumptions": f"gap passes through x{slope:.2f}; intraday vol {ratio:.2f}x; GIFT-to-open noise {GIFT_NOISE*100:.2f}%"}

    # --- the red/green call: always one side, with the evidence for how much to trust it
    gap_hist = (nifty_ohlc["Open"] / nifty_ohlc["Close"].shift(1) - 1).reindex(ret.index) * 100
    ok_g = gap_hist.notna().values
    base_rate = float((ret > 0).mean())
    if gift:
        gm = fc.fit_gap_call(gap_hist.values[ok_g], (ret.values[ok_g] > 0).astype(float), GIFT_NOISE * 100)
        call_p, call_basis = float(fc.logit_p(gm, [gift["gap"] * 100])[0]), "gift"
        call_why = f"GIFT Nifty is {gift['gap'] * 100:+.2f}% from the Nifty's last close"
    elif lean.get("status") == "ok":
        call_p, call_basis = lean["p_up"] / 100.0, "us"
        call_why = f"the S&P 500 moved {lean['sp_move_pct']:+.2f}% overnight"
    else:
        call_p, call_basis = base_rate, "base"
        call_why = "no overnight information yet, so this is just the everyday odds"
    call_tier = fc.call_tier(call_basis, call_p)
    bt_calls = (backtest.get("calls") or {}).get(call_basis if call_basis != "base" else "us", {})
    call_hit = (bt_calls.get(call_tier) or bt_calls.get("weak") or {}).get("hit_pct")
    morning = ts.date() > base_date.date() and ts < open_session_today(cfg, ts)
    firm = morning and ts >= ts.replace(hour=8, minute=30, second=0, microsecond=0) and call_basis == "gift"
    call = {"colour": "green" if call_p >= 0.5 else "red", "p_up": round(call_p * 100, 1),
            "p_call": round(max(call_p, 1 - call_p) * 100, 1), "basis": call_basis, "tier": call_tier,
            "hit_pct": call_hit, "why": call_why, "provisional": not firm,
            "note": ("Final pre-open reading." if firm else "Provisional: it firms up once GIFT Nifty's pre-open price is in (about 08:45 IST).") if call_basis != "base"
                    else "Provisional: the US market hasn't closed yet."}

    # --- portfolio: its own volatility, scaled by how the new Nifty band compares with the old one
    psig, pvalue = portfolio_sigma(cfg, state)
    portfolio = None
    if psig:
        half70, half95 = (typ[1] - typ[0]) / 2, (wide[1] - wide[0]) / 2
        portfolio = {"value": round(pvalue), "sigma_pct": round(psig * 100, 2),
                     "typical_rupees": round(pvalue * psig * half70 / ewma_sigma),
                     "wide_rupees": round(pvalue * psig * half95 / ewma_sigma)}

    # --- track record: log this forecast, score earlier ones against the real next close
    hist_log = read_json("outlook_history.json", [])
    by_base = {h["base_date"]: h for h in hist_log}
    open_session = ts.replace(hour=9, minute=15, second=0, microsecond=0)
    key = base_date.date().isoformat()
    entry = {"base_date": key, "made_at": ts.isoformat(), "method": "gjr-v2", "sigma_pct": round(sigma * 100, 3),
             "mu_pct": round(mu * 100, 3), "lo70_pct": round(typ[0] * 100, 3), "hi70_pct": round(typ[1] * 100, 3),
             "lo95_pct": round(wide[0] * 100, 3), "hi95_pct": round(wide[1] * 100, 3),
             "lean": lean.get("label"), "p_up": lean.get("p_up"),
             "call_colour": call["colour"], "call_p_up": call["p_up"], "call_basis": call["basis"], "call_tier": call["tier"]}
    if gift:
        entry.update({"gift_gap_pct": round(gift["gap"] * 100, 3), "gift_lo70_pct": round(gt[0] * 100, 3), "gift_hi70_pct": round(gt[1] * 100, 3)})
    if key not in by_base:
        hist_log.append(entry)
    elif by_base[key].get("actual_pct") is None and ts.date() > base_date.date() and ts < open_session:
        by_base[key].update(entry)      # later runs refine the forecast, but only until the open
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
            if h.get("method") == "gjr-v2":
                h["inside_70"] = h["lo70_pct"] <= act <= h["hi70_pct"]
                h["inside_95"] = h["lo95_pct"] <= act <= h["hi95_pct"]
                op = nifty_ohlc["Open"].get(later.index[0])
                if op is not None:
                    h["actual_gap_pct"] = round(float(op / nifty.loc[bd] - 1) * 100, 3)
                if h.get("gift_lo70_pct") is not None:
                    h["gift_inside_70"] = h["gift_lo70_pct"] <= act <= h["gift_hi70_pct"]
                if h.get("call_colour"):
                    h["call_hit"] = (act > 0) == (h["call_colour"] == "green")
                if h.get("p_up") is not None and abs(h["p_up"] - 50) >= 5:
                    h["lean_hit"] = (act > 0) == (h["p_up"] > 50)
            elif h.get("lean") in ("leans up", "leans down"):
                h["lean_hit"] = (act > 0) == (h["lean"] == "leans up")
    hist_log = hist_log[-250:]
    v2 = [h for h in hist_log if h.get("method") == "gjr-v2" and h.get("actual_pct") is not None]
    v1 = [h for h in hist_log if h.get("method") != "gjr-v2" and h.get("actual_pct") is not None]
    called = [h for h in v2 if "lean_hit" in h]
    pct = lambda a, b: round(100 * a / b, 0) if b else None
    record = {"sessions": len(v2), "range_70_pct": pct(sum(h["inside_70"] for h in v2), len(v2)),
              "range_95_pct": pct(sum(h["inside_95"] for h in v2), len(v2)),
              "calls": len(called), "lean_hit_pct": pct(sum(h["lean_hit"] for h in called), len(called)),
              "earlier_model": {"sessions": len(v1), "range_1s_pct": pct(sum(h["inside_1s"] for h in v1), len(v1))}}
    cl = [h for h in v2 if "call_hit" in h]
    record["calls_all"] = {"sessions": len(cl), "hit_pct": pct(sum(h["call_hit"] for h in cl), len(cl))}
    for b in ("gift", "us", "base"):
        sub = [h for h in cl if h.get("call_basis") == b]
        if sub:
            record["calls_all"][b] = {"sessions": len(sub), "hit_pct": pct(sum(h["call_hit"] for h in sub), len(sub))}
    gifts = [h for h in v2 if "gift_inside_70" in h]
    if gifts:
        record["gift"] = {"sessions": len(gifts), "range_70_pct": pct(sum(h["gift_inside_70"] for h in gifts), len(gifts))}
    write_json("outlook_history.json", hist_log)

    bt_range = (backtest.get("range") or {})
    write_json("outlook.json", {
        "generated_at": ts.isoformat(), "base_close_date": key, "base_close": round(base_close, 2),
        "sigma_pct": round(sigma * 100, 2), "nifty": nifty_out, "portfolio": portfolio, "lean": lean, "call": call,
        "model": {"volatility": vol_model, "signal": "S&P 500 last US session (ridge for the centre, logistic for P(up))",
                  "band_errors_from": f"model's own out-of-sample errors, last {fc.POOL_YEARS} years",
                  "fitted_sessions": int(len(ret)), "ewma_sigma_pct": round(ewma_sigma * 100, 2)},
        "backtest": backtest, "calibration": {"within70_pct": (bt_range.get("new_gjr_sp500") or {}).get("coverage70_pct"),
                                              "within95_pct": (bt_range.get("new_gjr_sp500") or {}).get("coverage95_pct"),
                                              "n": backtest.get("n_sessions")},
        "record": record,
    })
    print(f"     CALL: {call['colour'].upper()} ({call['p_call']}% {'up' if call['colour']=='green' else 'down'}, {call['basis']}, {call['tier']}, backtest hit {call['hit_pct']}%){' provisional' if call['provisional'] else ''}")
    pr = f"portfolio ±{portfolio['typical_rupees']:,}" if portfolio else "portfolio n/a"
    print(f"[ok] outlook: Nifty {base_close:,.0f}; 70% band {typ[0]*100:+.2f}% to {typ[1]*100:+.2f}%, 95% {wide[0]*100:+.2f}% to {wide[1]*100:+.2f}%; "
          f"{lean.get('label', lean['status'])}" + (f" (P up {lean['p_up']}%)" if "p_up" in lean else "") + f"; {pr}"
          + (f"; GIFT gap {gift['gap']*100:+.2f}% -> centre {centre_g*100:+.2f}%" if gift else ""))
    print(f"     {vol_model}; band errors from last {fc.POOL_YEARS}y; record {record}")


if __name__ == "__main__":
    main()
