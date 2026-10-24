"""Market trend check -> docs/data/trend.json: is the market in a bull or a bear phase, what do those phases usually look like,
and what would simply carrying the trend forward give. Nifty 50 (and the Nifty 500 for one signal), daily closes since 2007.

Nothing here predicts. It reads the classic technical signals (trend, momentum, distance from the high) as a description of
today, shows how the signals have fared at telling the year ahead (they have not: see `edge`), and shows what a year has looked
like in this history (`year`, bad years included). Those windows overlap, so the numbers describe the past, not a forecast. Non-blocking in the nightly workflow.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from analytics import fetch_prices
from common import now_ist, write_json

FWD = 252                      # trading days in the "next twelve months"


def rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def macd_hist(c: pd.Series) -> pd.Series:
    m = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    return m - m.ewm(span=9, adjust=False).mean()


def indicators(c: pd.Series) -> pd.DataFrame:
    """Every signal as a +1 (bullish) / 0 / -1 (bearish) column, per day, from closes only."""
    ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
    df = pd.DataFrame({"c": c, "ma50": ma50, "ma200": ma200, "rsi": rsi(c), "macd": macd_hist(c),
                       "r3": c.pct_change(63), "r12": c.pct_change(252), "dd": c / c.rolling(252, min_periods=60).max() - 1,
                       "slope": ma200 / ma200.shift(21) - 1})
    s = pd.DataFrame(index=c.index)
    s["above200"] = np.sign(c - ma200)
    s["cross"] = np.sign(ma50 - ma200)
    s["rising200"] = np.sign(df["slope"])
    s["mom12"] = np.sign(df["r12"])
    s["mom3"] = np.sign(df["r3"])
    s["rsi"] = np.sign(df["rsi"] - 50)
    s["macd"] = np.sign(df["macd"])
    s["high"] = np.where(df["dd"] >= -0.10, 1, np.where(df["dd"] <= -0.20, -1, 0))
    s = s.where(ma200.notna())
    return df.join(s.add_prefix("s_"))


def score(row: pd.Series, extra: int = 0) -> int:
    """Bullish signals minus bearish ones, from the eight signals on the index itself (plus `extra`, the Nifty 500 check)."""
    return int(sum(row[k] for k in row.index if k.startswith("s_")) + extra)


def regime(sc: int, of: int, dd: float) -> tuple[str, str]:
    if dd <= -0.20:
        return "bear", "Bear market"
    r = sc / of
    if r >= 0.75:
        return "strong", "Strong uptrend"
    if r >= 0.35:
        return "up", "Uptrend"
    if r > -0.35:
        return "mixed", "Mixed"
    if r > -0.75:
        return "down", "Downtrend"
    return "weak", "Weak"


def phases(c: pd.Series, thr: float = 0.20) -> tuple[list[dict], list[dict]]:
    """Bull and bear markets by the usual rule: a bear starts after a fall of `thr` from the high and ends after a rise of `thr`
    from the low; a bull runs from that low to the next high. Returns (bulls, bears), each a list of dicts."""
    bulls, bears = [], []
    peak, peak_d = float(c.iloc[0]), c.index[0]
    trough = peak
    bull_from = None                         # (date, level) of the low that began the current bull; None for the first stretch
    state = "bull"
    trough_d = peak_d
    for d, p in c.items():
        p = float(p)
        if state == "bull":
            if p > peak:
                peak, peak_d = p, d
            elif p <= peak * (1 - thr):
                if bull_from:
                    bulls.append({"start": bull_from[0], "end": peak_d, "gain": peak / bull_from[1] - 1})
                state, trough, trough_d = "bear", p, d
        else:
            if p < trough:
                trough, trough_d = p, d
            elif p >= trough * (1 + thr):
                bears.append({"start": peak_d, "end": trough_d, "fall": trough / peak - 1})
                bull_from, state, peak, peak_d = (trough_d, trough), "bull", p, d
    if state == "bull" and bull_from:
        bulls.append({"start": bull_from[0], "end": peak_d, "gain": peak / bull_from[1] - 1, "ongoing": True, "now": float(c.iloc[-1]) / bull_from[1] - 1})
    elif state == "bear":
        bears.append({"start": peak_d, "end": trough_d, "fall": trough / peak - 1, "ongoing": True})
    return bulls, bears


def months(a, b) -> float:
    return (pd.Timestamp(b) - pd.Timestamp(a)).days / 30.44


def pullbacks(seg: pd.Series, thr: float = 0.10) -> int:
    """How many separate falls of `thr` or more happened inside one stretch of the market."""
    n, peak, low, falling = 0, float(seg.iloc[0]), float(seg.iloc[0]), False
    for p in seg.astype(float):
        if not falling:
            peak = max(peak, p)
            if p <= peak * (1 - thr):
                falling, low, n = True, p, n + 1
        else:
            low = min(low, p)
            if p >= low * (1 + thr):
                falling, peak = False, p
    return n


def history_summary(c: pd.Series) -> dict:
    bulls, bears = phases(c)
    done_b = [b for b in bulls if not b.get("ongoing")]
    done_r = [b for b in bears if not b.get("ongoing")]
    inside = [pullbacks(c[b["start"]:b["end"]]) for b in bulls if b["end"] > b["start"]]
    iso = lambda t: pd.Timestamp(t).date().isoformat()
    out = {"since": iso(c.index[0]), "bull": {"n": len(bulls)}, "bear": {"n": len(bears)}}
    if done_b:
        out["bull"].update(median_gain_pct=round(float(np.median([b["gain"] for b in done_b])) * 100), median_months=round(float(np.median([months(b["start"], b["end"]) for b in done_b]))),
                           pullbacks_median=int(np.median(inside)) if inside else None)
    if done_r:
        out["bear"].update(median_fall_pct=round(float(np.median([b["fall"] for b in done_r])) * 100), median_months=round(float(np.median([months(b["start"], b["end"]) for b in done_r]))))
    if bulls:
        b = bulls[-1]
        out["bull"]["last"] = {"start": iso(b["start"]), "end": iso(b["end"]), "gain_pct": round(b["gain"] * 100), "now_pct": round(b["now"] * 100) if "now" in b else None, "ongoing": bool(b.get("ongoing"))}
    if bears:
        b = bears[-1]
        out["bear"]["last"] = {"peak": iso(b["start"]), "trough": iso(b["end"]), "fall_pct": round(b["fall"] * 100), "months": round(months(b["start"], b["end"])), "ongoing": bool(b.get("ongoing"))}
    return out


def bucket(sc, of: int = 8):
    """Up (clearly more bullish signals than bearish), down, or mixed: coarse on purpose, so there are enough past days to look at."""
    return np.where(sc >= of * 0.375, "up", np.where(sc <= -of * 0.375, "down", "mixed"))


def year_range(c: pd.Series) -> dict | None:
    """Every 12-month stretch since the start of the data (it begins just before the 2008 crash, so bad years are in it): how the next year
    could look from today's level if it behaved like a year from this history. Not conditioned on the signals: they have not predicted the year ahead."""
    f = (c.shift(-FWD) / c - 1).dropna() * 100
    if len(f) < 750:
        return None
    q = np.percentile(f, [10, 25, 50, 75, 90])
    last = float(c.iloc[-1])
    return {"since": c.index[0].date().isoformat(), "days": int(len(f)), "years": round(len(f) / 252, 1), "up_pct": round(float((f > 0).mean() * 100)),
            "p10": round(float(q[0]), 1), "p50": round(float(q[2]), 1), "p90": round(float(q[4]), 1), "worst": round(float(f.min()), 1), "best": round(float(f.max()), 1),
            "level_p10": round(last * (1 + q[0] / 100)), "level_p50": round(last * (1 + q[2] / 100)), "level_p90": round(last * (1 + q[4] / 100))}


def bucket(sc, of: int = 8):
    """Up (clearly more bullish signals than bearish), down, or mixed."""
    return np.where(sc >= of * 0.375, "up", np.where(sc <= -of * 0.375, "down", "mixed"))


def signal_edge(df: pd.DataFrame) -> dict | None:
    """Did the signals ever tell what the next 12 months would bring? The median next-12-month return after each kind of reading."""
    cols = [k for k in df.columns if k.startswith("s_")]
    sc = df[cols].sum(axis=1, min_count=len(cols))
    ok = sc.notna()
    fwd = (df["c"].shift(-FWD) / df["c"] - 1) * 100
    b = pd.Series(bucket(sc.fillna(0)), index=df.index)
    out = {"since": df.index[ok][0].date().isoformat()}
    for k in ("up", "mixed", "down"):
        x = fwd[ok & (b == k)].dropna()
        if len(x) < 100:
            return None
        out[k] = {"days": int(len(x)), "median": round(float(np.median(x)), 1), "up_pct": round(float((x > 0).mean() * 100))}
    return out


def trend_pace(c: pd.Series, years: int = 3) -> dict | None:
    """Annual pace of the straight line (in log terms) through the last `years` of closes, carried forward one year."""
    seg = c[c.index > c.index[-1] - pd.DateOffset(years=years)]
    if len(seg) < 400:
        return None
    y = np.log(seg.values.astype(float))
    slope = np.polyfit(np.arange(len(y)), y, 1)[0]
    annual = float(np.exp(slope * 252) - 1)
    return {"years": years, "annual_pct": round(annual * 100, 1), "level_12m": round(float(c.iloc[-1]) * (1 + annual))}


def tentative_view(code: str, dd: float, hist: dict) -> dict:
    """What the trend looks like right now, in plain words. A description, not a forecast: the page says so next to the evidence."""
    bear_m = (hist.get("bear") or {}).get("median_months")
    if code == "bear":
        return {"label": "Bear market", "text": "Prices are 20% or more below their high." + (f" Past bear markets took about {bear_m} months from top to bottom." if bear_m else "")}
    if code == "strong":
        return {"label": "Strong uptrend", "text": "The market is rising and almost every signal agrees: bull-run conditions. Falls of 10% along the way are normal, even in a bull run."}
    if code == "up":
        return {"label": "Healthy uptrend", "text": "More signals point up than down: conditions look like a bull run, though a pullback can come at any time."}
    if code == "mixed":
        return {"label": "Undecided", "text": "The signals disagree, so there is no clear bull or bear market yet. A direction is confirmed when the price moves clearly above or below its 200-day average."}
    return {"label": "Weak, but not a bear market" if dd > -0.20 else "Weak", "text": "Prices are below their long-term average and more signals point down than up. A bear market needs a fall of 20% from the high; this is smaller than that."}


def signposts(last: pd.Series) -> list[dict]:
    """What would turn the picture bullish, as plain ticks."""
    hi = float(last["c"] / (1 + last["dd"]))
    return [
        {"text": "Price back above its 200-day average", "level": round(float(last["ma200"])), "done": bool(last["c"] > last["ma200"])},
        {"text": "50-day average moves above the 200-day", "level": None, "done": bool(last["ma50"] > last["ma200"])},
        {"text": "Higher than a year ago", "level": round(float(last["c"] / (1 + last["r12"]))) if pd.notna(last["r12"]) else None, "done": bool(last["r12"] > 0)},
        {"text": "Back within 10% of the 52-week high", "level": round(hi * 0.9), "done": bool(last["dd"] >= -0.10)},
    ]


def weekly(c: pd.Series, years: int = 3) -> list[list]:
    seg = c[c.index > c.index[-1] - pd.DateOffset(years=years)]
    w = seg.resample("W-FRI").last().dropna()
    if w.index[-1] != seg.index[-1]:
        w = pd.concat([w, seg.iloc[[-1]]])
    return [[d.date().isoformat(), round(float(v))] for d, v in w.items()]


def build(nifty: pd.Series, n500: pd.Series | None) -> dict:
    df = indicators(nifty)
    last = df.iloc[-1]
    extra, of = 0, 8
    n5_above = None
    if n500 is not None and len(n500) > 250:
        n5_above = bool(n500.iloc[-1] > n500.rolling(200).mean().iloc[-1])
        extra, of = (1 if n5_above else -1), 9
    sc = score(last, extra)
    code, label = regime(sc, of, float(last["dd"]))
    pct = lambda x: round(float(x) * 100, 1)
    sig = lambda k, lab, val: {"k": k, "label": lab, "state": {1: "bull", -1: "bear", 0: "neutral"}[int(last["s_" + k])], "value": val}
    signals = [
        sig("above200", "Price above its 200-day average", f"{pct(last['c'] / last['ma200'] - 1):+.1f}%"),
        sig("cross", "50-day average above the 200-day", "yes" if last["ma50"] > last["ma200"] else "no"),
        sig("rising200", "200-day average rising", f"{pct(last['slope']):+.1f}% in a month"),
        sig("mom12", "Higher than a year ago", f"{pct(last['r12']):+.1f}%"),
        sig("mom3", "Higher than three months ago", f"{pct(last['r3']):+.1f}%"),
        sig("rsi", "Momentum (RSI) above 50", f"{last['rsi']:.0f}"),
        sig("macd", "MACD above its signal line", "yes" if last["macd"] > 0 else "no"),
        sig("high", "Within 10% of the 52-week high", f"{pct(last['dd']):+.1f}%"),
    ]
    if n5_above is not None:
        signals.append({"k": "n500", "label": "Nifty 500 above its 200-day average", "state": "bull" if n5_above else "bear", "value": "yes" if n5_above else "no"})
    stretched = bool(last["rsi"] > 70 or last["c"] / last["ma200"] - 1 > 0.15)
    out = {
        "generated_at": now_ist().isoformat(), "as_of": nifty.index[-1].date().isoformat(), "index": "Nifty 50", "level": round(float(last["c"]), 2),
        "score": sc, "of": of, "regime": {"code": code, "label": label}, "stretched": stretched,
        "drawdown_pct": pct(last["dd"]), "rsi": round(float(last["rsi"]), 0), "signals": signals,
        "levels": {"ma200": round(float(last["ma200"])), "high52": round(float(last["c"] / (1 + last["dd"]))), "bear_line": round(float(last["c"] / (1 + last["dd"]) * 0.8))},
        "history": history_summary(nifty), "year": year_range(nifty), "edge": signal_edge(df), "pace": trend_pace(nifty),
    }
    out["view"] = tentative_view(code, float(last["dd"]), out["history"])
    out["signposts"] = signposts(last)
    out["series"] = weekly(nifty)
    return out


def main() -> None:
    prices = fetch_prices(["^NSEI", "^CRSLDX"])
    nifty = prices.get("^NSEI")
    if nifty is None or len(nifty) < 1000:
        sys.exit("[error] not enough Nifty history; trend.json left as it was")
    doc = build(nifty, prices.get("^CRSLDX"))
    write_json("trend.json", doc)
    print(f"[ok] {doc['regime']['label']} ({doc['score']}/{doc['of']}), as of {doc['as_of']}")


if __name__ == "__main__":
    main()
