"""Market mood and valuation -> docs/data/mood.json (+ docs/data/valuation_history.json).

1. MOOD: Tickertape's public Market Mood Index (0-100: below 30 extreme fear, 30-50 fear, 50-70 greed, above
   70 extreme greed), with yesterday / last week / last month / last year. It is their proprietary index, shown with
   attribution. If their feed is ever unreachable, a simple fallback of our own (volatility, trend, position in the
   52-week range) is used and labelled as such.
2. VALUATION: is each part of the market expensive or cheap *relative to its own history*? P/E, P/B and dividend
   yield for the index categories your funds are benchmarked to, from NSE's static end-of-day files (one sample per
   month since 2012), ranked against each index's own past. A back-test on the Nifty 50 shows whether cheap/expensive
   readings have actually preceded good/poor returns, so the label comes with its evidence.

Everything is public data, nothing is predicted: "expensive" means "high compared with its own past", not "about to fall".
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from common import load_portfolio, now_ist, read_json, write_json

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
ARCHIVE = "https://archives.nseindia.com/content/indices/ind_close_all_{d}.csv"
# canonical name -> (label for people, kind, names it has had in NSE's files over the years)
INDICES = {
    "Nifty 50": ("Large caps (Nifty 50)", ["nifty 50", "s&p cnx nifty"]),
    "Nifty Next 50": ("Large caps, next 50", ["nifty next 50", "cnx nifty junior"]),
    "Nifty LargeMidcap 250": ("Large + midcaps (Nifty LargeMidcap 250)", ["nifty largemidcap 250"]),
    "Nifty Midcap 100": ("Midcaps (Nifty Midcap 100)", ["nifty midcap 100", "cnx midcap"]),
    "Nifty Midcap 150": ("Midcaps (Nifty Midcap 150)", ["nifty midcap 150"]),
    "Nifty Smallcap 100": ("Small caps (Nifty Smallcap 100)", ["nifty smallcap 100", "cnx smallcap"]),
    "Nifty Smallcap 250": ("Small caps (Nifty Smallcap 250)", ["nifty smallcap 250"]),
    "Nifty 500": ("Whole market (Nifty 500)", ["nifty 500", "cnx 500"]),
}
ALIAS = {a: c for c, (_, al) in INDICES.items() for a in al}
SHOW = ["Nifty 50", "Nifty LargeMidcap 250", "Nifty Midcap 150", "Nifty Smallcap 250", "Nifty 500", "Nifty Next 50", "Nifty Midcap 100", "Nifty Smallcap 100"]
HISTORY_FROM = dt.date(2012, 12, 1)


# ----------------------------------------------------------------------------- NSE static files
def fetch_day(d: dt.date) -> dict | None:
    """{canonical name: [pe, pb, dividend yield, close]} for one trading day, or None if NSE has no file for it."""
    r = requests.get(ARCHIVE.format(d=d.strftime("%d%m%Y")), headers=UA, timeout=25)
    if r.status_code != 200 or not r.text.startswith("Index Name"):
        return None
    out = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        c = ALIAS.get(row["Index Name"].strip().lower())
        if not c:
            continue
        try:
            pe, pb, dy, close = (float(row[k]) for k in ("P/E", "P/B", "Div Yield", "Closing Index Value"))
        except (KeyError, ValueError):
            continue
        if pe > 0:
            out[c] = [pe, pb, dy, close]
    return out or None


def month_sample(year: int, month: int) -> tuple[dt.date, dict] | None:
    """The last trading day of a month that has a file (walk back from the 31st)."""
    last = (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1))
    for back in range(0, 8):
        d = last - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        try:
            got = fetch_day(d)
        except requests.RequestException:
            continue
        if got:
            return d, got
    return None


def update_history(today: dt.date) -> dict:
    hist = read_json("valuation_history.json", {}) or {}
    series = hist.get("series") or {}
    have = {d for s in series.values() for d in s}
    months = []
    y, m = HISTORY_FROM.year, HISTORY_FROM.month
    while (y, m) < (today.year, today.month):                       # completed months only
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    have_months = {d[:7] for d in have}
    todo = [(y, m) for y, m in months if f"{y}-{m:02d}" not in have_months]
    if todo:
        print(f"[info] fetching {len(todo)} month-end valuation samples from NSE")
        with ThreadPoolExecutor(max_workers=8) as ex:
            for res in ex.map(lambda ym: month_sample(*ym), todo):
                if res:
                    d, got = res
                    for c, v in got.items():
                        series.setdefault(c, {})[d.isoformat()] = v
    # the latest trading day (kept apart from the monthly samples; replaced each time)
    latest = None
    for back in range(0, 7):
        d = today - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        got = fetch_day(d)
        if got:
            latest = (d, got)
            break
    hist = {"updated": now_ist().isoformat(), "series": {c: dict(sorted(s.items())) for c, s in series.items()},
            "latest": {"date": latest[0].isoformat(), "values": latest[1]} if latest else (hist.get("latest") or None)}
    write_json("valuation_history.json", hist)
    return hist


# -------------------------------------------------------------------------------- valuation
def pct_rank(sample: list[float], x: float) -> float:
    return 100.0 * sum(1 for v in sample if v <= x) / len(sample)


def verdict(p: float) -> str:
    return "Cheap" if p < 15 else "Below average" if p < 35 else "Fair" if p < 65 else "Above average" if p < 85 else "Expensive"


def valuation(hist: dict, cfg: dict) -> tuple[list[dict], dict]:
    series, latest = hist["series"], hist.get("latest") or {}
    mine = {}
    for f in cfg["funds"]:
        ab = f.get("analytics_benchmark") or {}
        mine[ab.get("name", "")] = f["name"]
    # which category each fund is benchmarked against (by the benchmark's name in the config)
    bench_map = {"Nifty Smallcap 250": "Nifty Smallcap 250", "Nifty LargeMidcap 250": "Nifty LargeMidcap 250", "Nifty 500": "Nifty 500"}
    rows = []
    for c in SHOW:
        s = series.get(c) or {}
        cur = (latest.get("values") or {}).get(c)
        if not s or not cur:
            continue
        pes = [v[0] for v in s.values() if 0 < v[0] < 120]       # tame the earnings-collapse spikes of 2020
        pbs, dys = [v[1] for v in s.values()], [v[2] for v in s.values()]
        ten = [v[0] for d, v in s.items() if d >= (dt.date.fromisoformat(latest["date"]) - dt.timedelta(days=3653)).isoformat() and 0 < v[0] < 120]
        five = [v[0] for d, v in s.items() if d >= (dt.date.fromisoformat(latest["date"]) - dt.timedelta(days=1826)).isoformat() and 0 < v[0] < 120]
        pe, pb, dy = cur[0], cur[1], cur[2]
        p_all, p10, p5 = pct_rank(pes, pe), pct_rank(ten or pes, pe), pct_rank(five or pes, pe)
        med = statistics.median(ten or pes)
        score_p = (p5 + p10) / 2                                  # against the last 5 years AND the last 10
        rows.append({"index": c, "label": INDICES[c][0], "pe": round(pe, 1), "pb": round(pb, 2), "div_yield": round(dy, 2),
                     "median_pe": round(med, 1), "premium_pct": round((pe / med - 1) * 100, 0),
                     "pct_5y": round(p5), "pct_10y": round(p10), "pct_all": round(p_all), "since": min(s)[:7], "samples": len(pes),
                     "pb_pct": round(pct_rank(pbs, pb)), "dy_pct": round(pct_rank(dys, dy)),
                     "verdict": verdict(score_p), "score_pct": round(score_p),
                     "mine": [mine[k] for k in bench_map if bench_map[k] == c and k in mine]})
    return rows, {"as_of": latest.get("date")}


def valuation_backtest(hist: dict) -> dict | None:
    """Nifty 50: what followed when its P/E was in the cheapest / middle / dearest part of its own past?"""
    s = hist["series"].get("Nifty 50") or {}
    dates = sorted(s)
    if len(dates) < 60:
        return None
    pe = {d: s[d][0] for d in dates}
    px = {d: s[d][3] for d in dates}
    rows = []
    for i, d in enumerate(dates):
        if i < 36 or not (0 < pe[d] < 120):
            continue
        hist_pes = [pe[x] for x in dates[:i] if 0 < pe[x] < 120][-120:]       # only the past, at most 10 years
        p = pct_rank(hist_pes, pe[d])
        r12 = (px[dates[i + 12]] / px[d] - 1) * 100 if i + 12 < len(dates) else None
        r36 = ((px[dates[i + 36]] / px[d]) ** (1 / 3) - 1) * 100 if i + 36 < len(dates) else None
        rows.append((p, r12, r36))
    out = []
    for name, lo, hi in (("Cheapest third", 0, 33.3), ("Middle third", 33.3, 66.7), ("Dearest third", 66.7, 100.1)):
        sel = [r for r in rows if lo <= r[0] < hi]
        r12 = [r[1] for r in sel if r[1] is not None]
        r36 = [r[2] for r in sel if r[2] is not None]
        if len(r12) >= 8 and len(r36) >= 8:
            out.append({"bucket": name, "months": len(sel), "median_1y": round(statistics.median(r12), 1), "median_3y": round(statistics.median(r36), 1),
                        "worst_1y": round(min(r12), 1), "best_1y": round(max(r12), 1), "worst_3y": round(min(r36), 1),
                        "positive_1y_pct": round(100 * sum(1 for v in r12 if v > 0) / len(r12))})
    return {"index": "Nifty 50", "from": dates[0][:7], "to": dates[-1][:7], "buckets": out,
            "note": "Price returns only (no dividends). Windows overlap and cover just one market cycle, so read this as a tendency, not a rule."} if out else None


# ------------------------------------------------------------------------------------- mood
def zone(v: float) -> str:
    return "Extreme fear" if v < 30 else "Fear" if v < 50 else "Greed" if v < 70 else "Extreme greed"


def tickertape() -> dict | None:
    r = requests.get("https://api.tickertape.in/mmi/now", headers={**UA, "Accept": "application/json"}, timeout=25)
    r.raise_for_status()
    d = r.json()["data"]
    f = lambda x: round(float(x["indicator"]), 1) if x and x.get("indicator") is not None else None
    cur = float(d["indicator"])
    nifty, fma, sma = d.get("nifty"), d.get("fma"), d.get("sma")
    return {"source": "Tickertape Market Mood Index", "value": round(cur, 1), "zone": zone(cur), "as_of": (d.get("date") or "")[:10],
            "day_ago": f(d.get("lastDay")), "week_ago": f(d.get("lastWeek")), "month_ago": f(d.get("lastMonth")), "year_ago": f(d.get("lastYear")),
            "nifty": nifty, "short_avg": round(fma) if fma else None, "long_avg": round(sma) if sma else None,
            "vs_short_avg_pct": round((nifty / fma - 1) * 100, 1) if nifty and fma else None,
            "vs_long_avg_pct": round((nifty / sma - 1) * 100, 1) if nifty and sma else None,
            "vix": abs(d["vix"]) if d.get("vix") is not None else None, "fallback": False}


def own_mood() -> dict | None:
    """Fallback if Tickertape is unreachable: volatility, trend and 52-week position, each scaled 0-100 (100 = greedy)."""
    import yfinance as yf
    d = yf.download(["^NSEI", "^INDIAVIX"], period="3y", interval="1d", progress=False, auto_adjust=False, timeout=30, group_by="ticker")
    n, v = d["^NSEI"]["Close"].dropna(), d["^INDIAVIX"]["Close"].dropna()
    if len(n) < 250 or len(v) < 250:
        return None
    vol = 100 - pct_rank(list(v.values), float(v.iloc[-1]))
    trend = float(np.clip((n.iloc[-1] / n.iloc[-200:].mean() - 1) * 100 / 8, -1, 1) * 50 + 50)
    pos = float((n.iloc[-1] - n.iloc[-250:].min()) / (n.iloc[-250:].max() - n.iloc[-250:].min()) * 100)
    cur = round((vol + trend + pos) / 3, 1)
    return {"source": "Our own estimate (volatility, trend, 52-week position)", "value": cur, "zone": zone(cur), "as_of": n.index[-1].date().isoformat(),
            "day_ago": None, "week_ago": None, "month_ago": None, "year_ago": None, "nifty": round(float(n.iloc[-1])), "short_avg": None,
            "long_avg": round(float(n.iloc[-200:].mean())), "vs_short_avg_pct": None,
            "vs_long_avg_pct": round(float((n.iloc[-1] / n.iloc[-200:].mean() - 1) * 100), 1), "vix": round(float(v.iloc[-1]), 1), "fallback": True}


def main() -> None:
    cfg, ts = load_portfolio(), now_ist()
    mood = None
    try:
        mood = tickertape()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Tickertape unreachable ({exc}); using our own estimate", file=sys.stderr)
    if not mood:
        try:
            mood = own_mood()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] own mood failed: {exc}", file=sys.stderr)
    try:
        hist = update_history(ts.date())
        rows, meta = valuation(hist, cfg)
        back = valuation_backtest(hist)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] valuation failed: {exc}", file=sys.stderr)
        old = read_json("mood.json", {}) or {}
        rows, meta, back = old.get("valuation") or [], {"as_of": old.get("valuation_as_of")}, old.get("valuation_backtest")
    if not mood and not rows:
        print("[error] nothing computed; leaving mood.json unchanged", file=sys.stderr)
        sys.exit(1)
    old = read_json("mood.json", {}) or {}
    write_json("mood.json", {"generated_at": ts.isoformat(), "mood": mood or old.get("mood"), "valuation": rows, "valuation_as_of": meta.get("as_of"),
                             "valuation_backtest": back,
                             "note": "Valuation is relative to each index's own history since 2012; the mood index is Tickertape's."})
    print(f"[ok] mood.json: mood {mood and mood['value']} ({mood and mood['zone']}){' [fallback]' if mood and mood['fallback'] else ''}; "
          f"valuation for {len(rows)} categories as of {meta.get('as_of')}")
    for r in rows:
        print(f"     {r['label']:<44} P/E {r['pe']:>5}  {r['pct_10y']:>3}th pct (10y)  {r['verdict']}")


if __name__ == "__main__":
    main()
