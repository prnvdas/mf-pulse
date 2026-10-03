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
    load_holdings,
    load_portfolio,
    now_ist,
    read_json,
    write_json,
)


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
                auto_adjust=False,
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


def fetch_trend(tickers: list[str]) -> dict[str, dict | None]:
    """1w / 1m / 3m change and drawdown from the 3-month high, per index."""
    out: dict[str, dict | None] = {t: None for t in tickers}
    if not tickers:
        return out
    try:
        data = yf.download(
            tickers=" ".join(tickers), period="6mo", interval="1d",
            group_by="ticker", progress=False, threads=True, auto_adjust=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] trend fetch failed: {exc}", file=sys.stderr)
        return out
    for t in tickers:
        try:
            frame = data[t] if len(tickers) > 1 else data
            c = frame["Close"].dropna()
            if len(c) < 25:
                continue
            last = float(c.iloc[-1])

            def chg(n: int) -> float | None:
                return round((last / float(c.iloc[-1 - n]) - 1) * 100, 2) if len(c) > n else None

            hi = float(c.iloc[-63:].max())
            out[t] = {
                "w1": chg(5), "m1": chg(21), "m3": chg(63),
                "drawdown_pct": round((last / hi - 1) * 100, 2),
            }
        except Exception:  # noqa: BLE001
            continue
    return out


def build_health(funds: list[dict], results: list[dict],
                 week: dict[str, float], trend: dict, ts: dt.datetime) -> dict:
    """Plain, inspectable checks against the current market -- not a score.

    Each fund gets traffic-light flags with the numbers behind them, so the
    dashboard can show *why* something is amber rather than a bare verdict.
    """
    rank = {"healthy": 0, "watch": 1, "caution": 2}
    cards = []
    for f, r in zip(funds, results):
        doc = load_holdings(f["id"])
        hs = doc.get("holdings") or []
        flags: list[dict] = []

        def flag(sev: int, text: str) -> None:
            flags.append({"severity": sev, "text": text})

        t = trend.get(f.get("benchmark_ticker"))
        if t:
            if t["m1"] is not None and t["m1"] <= -8:
                flag(2, f"Benchmark down {abs(t['m1']):.1f}% over 1 month")
            elif t["m1"] is not None and t["m1"] <= -3:
                flag(1, f"Benchmark down {abs(t['m1']):.1f}% over 1 month")
            if t["drawdown_pct"] <= -12:
                flag(2, f"Benchmark {abs(t['drawdown_pct']):.1f}% below its 3-month high")
            elif t["drawdown_pct"] <= -7:
                flag(1, f"Benchmark {abs(t['drawdown_pct']):.1f}% below its 3-month high")

        with_week = [(float(h["weight_pct"]), week[h["ticker"]]) for h in hs if h["ticker"] in week]
        wsum = sum(w for w, _ in with_week)
        breadth = round(sum(w for w, m in with_week if m > 0) / wsum * 100, 1) if wsum else None
        if breadth is not None:
            if breadth < 25:
                flag(2, f"Only {breadth:.0f}% of holdings (by weight) are up over 5 days")
            elif breadth < 40:
                flag(1, f"Only {breadth:.0f}% of holdings (by weight) are up over 5 days")

        top10 = round(sum(sorted((float(h["weight_pct"]) for h in hs), reverse=True)[:10]), 1)
        if top10 > 65:
            flag(2, f"Top 10 holdings are {top10:.0f}% of the fund")
        elif top10 > 50:
            flag(1, f"Top 10 holdings are {top10:.0f}% of the fund")

        age = None
        if doc.get("as_of"):
            try:
                age = (ts.date() - dt.date.fromisoformat(str(doc["as_of"]))).days
            except ValueError:
                age = None
        if age is not None:
            if age > 75:
                flag(2, f"Holdings data is {age} days old — estimate accuracy is degrading")
            elif age > 45:
                flag(1, f"Holdings data is {age} days old")

        if r["coverage_pct"] and r["coverage_pct"] < 70:
            flag(1, f"Only {r['coverage_pct']:.0f}% of the fund is tracked by name")

        worst = max((x["severity"] for x in flags), default=0)
        status = {0: "healthy", 1: "watch", 2: "caution"}[worst]
        cards.append({
            "id": f["id"], "name": f["name"], "status": status,
            "value": r["current_value"], "benchmark": f.get("benchmark_ticker"),
            "trend": t, "breadth_pct": breadth, "top10_pct": top10,
            "cash_pct": float(doc.get("cash_pct") or 0.0), "holdings_age_days": age,
            "flags": flags,
        })

    total = sum(c["value"] for c in cards) or 1.0
    share = {s: sum(c["value"] for c in cards if c["status"] == s) / total * 100
             for s in rank}
    if share["caution"] >= 40:
        overall = "caution"
    elif share["caution"] > 0 or share["watch"] > 0:
        overall = "watch"
    else:
        overall = "healthy"
    return {
        "overall": overall,
        "value_share_pct": {k: round(v, 1) for k, v in share.items()},
        "funds": cards,
    }


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

    benchmarks = sorted({f["benchmark_ticker"] for f in funds if f.get("benchmark_ticker")})
    health = build_health(funds, results, week, fetch_trend(benchmarks), ts)

    # "final" = a run after the close with today's bar present: the number
    # reconcile.py will grade tonight. Everything else is a live batch.
    after_close = ts.time() >= dt.time(15, 45)
    phase = "holiday" if is_holiday else ("final" if after_close else "live")

    total_value = sum(r["current_value"] for r in results)
    total_invested = sum(r["invested"] for r in results)
    total_impact = sum(r["rupee_impact"] for r in results)
    # Errors are partly independent across funds, so add bands in quadrature
    # rather than straight — straight summing overstates the uncertainty.
    total_band = sum(r["band_rupees"] ** 2 for r in results) ** 0.5

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
        "health": health,
        "accuracy": read_json("accuracy.json", {"samples": 0}),
        "tickers_resolved": resolved,
        "tickers_total": len(moves),
    }

    write_json("latest.json", payload)
    print(
        f"[ok] today {payload['totals']['today_impact']:+,.0f} "
        f"({payload['totals']['today_pct']:+.2f}%) "
        f"±{payload['totals']['band_rupees']:,.0f}"
    )


if __name__ == "__main__":
    main()
