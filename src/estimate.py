"""Estimate today's NAV move for each fund from live prices of its holdings.

The model, per fund:

    tracked_move  = SUM(weight_i * pct_change_i) / SUM(weight_i)
    tail_move     = benchmark index pct_change        (tail_model: benchmark)
    nav_move      = tracked_w * tracked_move
                  + tail_w    * tail_move
                  + cash_w    * 0
                  - daily TER accrual

Coverage (tracked_w) is reported alongside every number, because a 40%-covered
estimate and a 90%-covered estimate deserve very different levels of trust.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

import yfinance as yf

from common import (
    is_market_window,
    session_end,
    load_holdings,
    load_portfolio,
    now_ist,
    read_json,
    write_json,
)


# Shown in the dashboard's news ticker. fast_info is used because Yahoo's
# daily history for the large-midcap index holds only one bar,
# but it still reports the latest level and the previous close for them.
INDICES = [
    ("Sensex", "^BSESN"),
    ("Nifty 50", "^NSEI"),
    ("Nifty Smallcap 250", "NIFTYSMLCAP250.NS"),
    ("Nifty LargeMidcap 250", "NIFTY_LARGEMID250.NS"),
]


def fetch_indices() -> list[dict]:
    out = []
    for name, ticker in INDICES:
        try:
            fi = yf.Ticker(ticker).fast_info
            last, prev = float(fi["lastPrice"]), float(fi["previousClose"])
            if last and prev:
                out.append({
                    "name": name, "ticker": ticker, "level": round(last, 2),
                    "change": round(last - prev, 2),
                    "move_pct": round((last / prev - 1) * 100, 2),
                })
        except Exception as exc:  # noqa: BLE001 -- a missing chip must not fail the run
            print(f"[warn] index {name} unavailable: {exc}", file=sys.stderr)
    return out


def fetch_moves(
    tickers: list[str], retries: int = 2
) -> tuple[dict[str, float | None], dt.date | None, dict[str, float]]:
    """Percent change vs previous close for each ticker. None when unavailable.

    Also returns the most recent bar date seen across all tickers — the
    simplest available signal for "did the market actually trade today."
    NSE has holidays (Gandhi Jayanti, Diwali, etc.) that fall on weekdays,
    so is_market_window()'s weekday-only check doesn't catch them; deriving
    it straight from the data avoids needing a maintained holiday calendar.

    Retries a couple of times on failure — Yahoo has enough transient blips
    that a single failed call would otherwise silently cost a whole trading
    day's data point (and, if it's the post-close settle run, that day's
    accuracy grading too).
    """
    if not tickers:
        return {}, None, {}

    moves: dict[str, float | None] = {t: None for t in tickers}
    week: dict[str, float] = {}
    data = None
    for attempt in range(retries + 1):
        try:
            data = yf.download(
                tickers=" ".join(tickers),
                period="5d",
                interval="1d",
                group_by="ticker",
                progress=False,
                threads=True,
                auto_adjust=False, timeout=30,
            )
            break
        except Exception as exc:  # noqa: BLE001 — never let a data blip kill the run
            print(f"[warn] price fetch failed (attempt {attempt + 1}/{retries + 1}): {exc}",
                  file=sys.stderr)
            if attempt < retries:
                time.sleep(5)
    if data is None:
        return moves, None, week

    latest_bar_date: dt.date | None = None
    for ticker in tickers:
        try:
            frame = data[ticker] if len(tickers) > 1 else data
            closes = frame["Close"].dropna()
            if len(closes) < 2:
                continue
            prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
            if prev:
                moves[ticker] = (last - prev) / prev * 100.0
            first = float(closes.iloc[0])
            if first:
                week[ticker] = (last - first) / first * 100.0
            bar_date = closes.index[-1].date()
            if latest_bar_date is None or bar_date > latest_bar_date:
                latest_bar_date = bar_date
        except Exception:  # noqa: BLE001
            continue
    return moves, latest_bar_date, week


def build_movers(funds: list[dict], results: list[dict],
                 moves: dict[str, float | None], day: str) -> dict:
    """Top 10 gainers and laggards across the whole portfolio, by rupee impact.

    A stock held in several funds is summed across them -- what matters is how
    much it moved *your* money, not its % move on a tiny position. This reports
    what already happened in the session; it is not a forecast of which stocks
    will rise.
    """
    agg: dict[str, dict] = {}
    for f, r in zip(funds, results):
        for h in load_holdings(f["id"]).get("holdings") or []:
            move = moves.get(h["ticker"])
            if move is None:
                continue
            exposure = r["current_value"] * float(h["weight_pct"]) / 100.0
            row = agg.setdefault(h["ticker"], {
                "name": h["name"], "ticker": h["ticker"], "move_pct": round(move, 2),
                "exposure": 0.0, "impact": 0.0, "funds": [],
            })
            row["exposure"] += exposure
            row["impact"] += exposure * move / 100.0
            row["funds"].append(f["name"].replace(" Direct Growth", "").replace(" Fund", ""))
    rows = [{**v, "exposure": round(v["exposure"]), "impact": round(v["impact"])}
            for v in agg.values()]
    gainers = sorted((x for x in rows if x["impact"] > 0), key=lambda x: -x["impact"])[:10]
    laggards = sorted((x for x in rows if x["impact"] < 0), key=lambda x: x["impact"])[:10]
    return {"date": day, "gainers": gainers, "laggards": laggards}


def estimate_fund(fund: dict, cfg: dict, moves: dict[str, float | None]) -> dict:
    holdings_doc = load_holdings(fund["id"])
    holdings = holdings_doc.get("holdings") or []
    cash_pct = float(holdings_doc.get("cash_pct") or 0.0)

    tracked_weight = 0.0
    weighted_sum = 0.0
    contributors = []

    for h in holdings:
        move = moves.get(h["ticker"])
        if move is None:
            continue
        weight = float(h["weight_pct"])
        tracked_weight += weight
        weighted_sum += weight * move
        contributors.append(
            {
                "name": h["name"],
                "ticker": h["ticker"],
                "weight_pct": round(weight, 2),
                "move_pct": round(move, 2),
                "contribution_pct": round(weight * move / 100.0, 4),
            }
        )

    tracked_move = (weighted_sum / tracked_weight) if tracked_weight else 0.0
    tail_weight = max(0.0, 100.0 - tracked_weight - cash_pct)

    bench_move = moves.get(fund.get("benchmark_ticker"))
    if cfg["estimator"]["tail_model"] == "benchmark" and bench_move is not None:
        tail_move = bench_move
    else:
        tail_move = tracked_move  # fall back to scaling the tracked portion

    ter_daily = float(fund.get("ter_annual_pct", 0.0)) / 365.0

    nav_move = (
        (tracked_weight / 100.0) * tracked_move
        + (tail_weight / 100.0) * tail_move
        - ter_daily
    )

    units = fund.get("units")
    last_nav = fund.get("last_nav")
    if units and last_nav:
        current_value = units * last_nav
    else:
        current_value = float(fund.get("seed_value") or 0.0)

    rupee_impact = current_value * nav_move / 100.0
    projected_nav = last_nav * (1 + nav_move / 100.0) if last_nav else None

    # Wider coverage gap -> wider error band. Rough but honest.
    coverage = tracked_weight / max(1e-9, (100.0 - cash_pct))
    band_pct = 0.10 + (1.0 - coverage) * 0.60

    contributors.sort(key=lambda c: abs(c["contribution_pct"]), reverse=True)

    return {
        "id": fund["id"],
        "name": fund["name"],
        "current_value": round(current_value, 2),
        "invested": float(fund.get("seed_invested") or 0.0),
        "nav_move_pct": round(nav_move, 3),
        "rupee_impact": round(rupee_impact, 2),
        "last_nav": last_nav,
        "projected_nav": round(projected_nav, 4) if projected_nav else None,
        "band_rupees": round(abs(current_value) * band_pct / 100.0, 0),
        "coverage_pct": round(tracked_weight, 1),
        "cash_pct": cash_pct,
        "tail_pct": round(tail_weight, 1),
        "tail_move_pct": round(tail_move, 2) if tail_move is not None else None,
        "holdings_as_of": holdings_doc.get("as_of"),
        "holdings_count": len(holdings),
        "priced_count": len(contributors),
        "top_contributors": contributors[:8],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="run outside market hours")
    args = parser.parse_args()

    cfg = load_portfolio()
    ts = now_ist()

    if not is_market_window(cfg, ts) and not args.force:
        print("[info] outside market window; marking last estimate stale")
        latest = read_json("latest.json", None)
        locked_today = (
            latest
            and latest.get("phase") == "final"
            and str(latest.get("generated_at", ""))[:10] == ts.date().isoformat()
        )
        if latest and not locked_today:
            latest["stale"] = True
            write_json("latest.json", latest)
        return

    # Units and last NAV are written by reconcile.py; merge them in if present.
    state = read_json("state.json", {})
    funds = []
    for f in cfg["funds"]:
        merged = dict(f)
        merged.update(state.get(f["id"], {}))
        funds.append(merged)

    tickers: set[str] = set()
    for f in funds:
        for h in load_holdings(f["id"]).get("holdings") or []:
            tickers.add(h["ticker"])
        if f.get("benchmark_ticker"):
            tickers.add(f["benchmark_ticker"])

    moves, latest_bar_date, week = fetch_moves(sorted(tickers))
    resolved = sum(1 for v in moves.values() if v is not None)
    print(f"[info] resolved {resolved}/{len(moves)} tickers, "
          f"latest bar date {latest_bar_date}")

    # Fail-safe: a near-total fetch failure (Yahoo outage, network blip) must
    # not silently overwrite today's estimate with a bogus near-0% number
    # computed from empty data. Refuse to publish instead — the previous
    # latest.json stays in place, its date won't match tonight's NAV, and
    # reconcile.py correctly skips grading rather than scoring a data outage
    # as if it were a real (and misleadingly good-looking) prediction.
    MIN_COVERAGE = 0.20
    if tickers and resolved / len(moves) < MIN_COVERAGE:
        print(f"[error] only {resolved}/{len(moves)} tickers resolved — "
              "refusing to publish a degraded estimate", file=sys.stderr)
        sys.exit(1)

    # NSE holidays (Gandhi Jayanti, Diwali, etc.) land on ordinary weekdays,
    # so is_market_window()'s weekday check alone doesn't catch them. If
    # nothing newer than the last known close exists yet, nothing actually
    # traded today — today's move is 0%, not whatever yesterday's move
    # happened to be (using moves as fetched would silently attribute
    # yesterday's price action to today). current_value/invested still need
    # to reflect the latest state.json, though — a manual correction via
    # adjust_investment.py shouldn't have to wait for the next trading day
    # to show up just because today happens to be a holiday. So: zero out
    # the moves (nav_move_pct comes out to just the day's TER drag, coverage
    # correctly shows 0%) rather than skipping the computation entirely.
    is_holiday = bool(latest_bar_date and latest_bar_date < ts.date())
    if is_holiday:
        print(f"[info] no trading data newer than {latest_bar_date} — "
              f"{ts.date()} looks like a market holiday; today's move is 0%")
        moves = {t: None for t in moves}
        resolved = 0

    results = [estimate_fund(f, cfg, moves) for f in funds]

    # "final" = a run after the close with today's bar present: the number
    # reconcile.py will grade tonight. Everything else is a live batch.
    market = fetch_indices()
    held = {h["ticker"] for f in funds for h in load_holdings(f["id"]).get("holdings") or []}
    pts = [moves[t] for t in held if moves.get(t) is not None]
    breadth = None if is_holiday or not pts else {
        "up": sum(1 for m in pts if m > 0), "down": sum(1 for m in pts if m < 0), "total": len(pts),
    }
    movers = None if is_holiday else build_movers(funds, results, moves, ts.date().isoformat())
    after_close = ts >= session_end(cfg, ts) + dt.timedelta(minutes=15)   # 15 min after the day's last session
    phase = "holiday" if is_holiday else ("final" if after_close else "live")

    total_value = sum(r["current_value"] for r in results)
    total_invested = sum(r["invested"] for r in results)
    total_impact = sum(r["rupee_impact"] for r in results)
    # Errors are partly independent across funds, so add bands in quadrature
    # rather than straight — straight summing overstates the uncertainty.
    total_band = sum(r["band_rupees"] ** 2 for r in results) ** 0.5

    if movers:
        # kept with each day's record so the "why did the market move" view can
        # still explain a past session (e.g. on a holiday) from the same facts
        movers.update({
            "market": market, "breadth": breadth,
            "portfolio_pct": round(total_impact / total_value * 100.0, 3) if total_value else 0.0,
            "portfolio_impact": round(total_impact),
            "funds": [{"id": r["id"], "name": r["name"], "nav_move_pct": r["nav_move_pct"],
                       "impact": round(r["rupee_impact"])} for r in results],
        })

    payload = {
        "generated_at": ts.isoformat(),
        "generated_label": ts.strftime("%d %b %Y, %H:%M IST"),
        "stale": is_holiday,
        "phase": phase,
        "market_open": is_market_window(cfg, ts) and not is_holiday,
        "totals": {
            "current_value": round(total_value, 2),
            "invested": round(total_invested, 2),
            "total_returns": round(total_value - total_invested, 2),
            "total_returns_pct": round(
                (total_value - total_invested) / total_invested * 100.0, 2
            )
            if total_invested
            else 0.0,
            "today_impact": round(total_impact, 2),
            "today_pct": round(total_impact / total_value * 100.0, 3)
            if total_value
            else 0.0,
            "band_rupees": round(total_band, 0),
        },
        "funds": results,
        "movers": movers,
        "market": market,
        "breadth": breadth,
        "accuracy": read_json("accuracy.json", {"samples": 0}),
        "tickers_resolved": resolved,
        "tickers_total": len(moves),
    }

    write_json("latest.json", payload)

    # One record per trading day, written only from the post-close run so the
    # history holds each session's final picture, not an intraday snapshot.
    if phase == "final" and movers:
        hist = [m for m in read_json("movers.json", []) if m["date"] != movers["date"]]
        hist.append(movers)
        write_json("movers.json", hist[-60:])
    print(
        f"[ok] today {payload['totals']['today_impact']:+,.0f} "
        f"({payload['totals']['today_pct']:+.2f}%) "
        f"±{payload['totals']['band_rupees']:,.0f}"
    )


if __name__ == "__main__":
    main()
