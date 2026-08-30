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


def fetch_moves(tickers: list[str], retries: int = 2) -> dict[str, float | None]:
    """Percent change vs previous close for each ticker. None when unavailable.

    Retries a couple of times on failure — Yahoo has enough transient blips
    that a single failed call would otherwise silently cost a whole trading
    day's data point (and, if it's the post-close settle run, that day's
    accuracy grading too).
    """
    if not tickers:
        return {}

    moves: dict[str, float | None] = {t: None for t in tickers}
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
        return moves

    for ticker in tickers:
        try:
            frame = data[ticker] if len(tickers) > 1 else data
            closes = frame["Close"].dropna()
            if len(closes) < 2:
                continue
            prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
            if prev:
                moves[ticker] = (last - prev) / prev * 100.0
        except Exception:  # noqa: BLE001
            continue
    return moves


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
        if latest:
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

    moves = fetch_moves(sorted(tickers))
    resolved = sum(1 for v in moves.values() if v is not None)
    print(f"[info] resolved {resolved}/{len(moves)} tickers")

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

    results = [estimate_fund(f, cfg, moves) for f in funds]

    total_value = sum(r["current_value"] for r in results)
    total_invested = sum(r["invested"] for r in results)
    total_impact = sum(r["rupee_impact"] for r in results)
    # Errors are partly independent across funds, so add bands in quadrature
    # rather than straight — straight summing overstates the uncertainty.
    total_band = sum(r["band_rupees"] ** 2 for r in results) ** 0.5

    payload = {
        "generated_at": ts.isoformat(),
        "generated_label": ts.strftime("%d %b %Y, %H:%M IST"),
        "stale": False,
        "market_open": is_market_window(cfg, ts),
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
