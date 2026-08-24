"""Flatten portfolio.yaml + holdings/*.yaml into one JSON the browser can read.

The dashboard does its own inference on demand, so it needs the weights
client-side. Run this after any holdings import.
"""

from __future__ import annotations

from common import load_holdings, load_portfolio, read_json, write_json


def main() -> None:
    cfg = load_portfolio()
    state = read_json("state.json", {})

    funds = []
    for f in cfg["funds"]:
        doc = load_holdings(f["id"])
        holdings = doc.get("holdings") or []
        st = state.get(f["id"], {})

        funds.append(
            {
                "id": f["id"],
                "name": f["name"],
                "amfi_code": f.get("amfi_code"),
                "ter_annual_pct": f.get("ter_annual_pct", 0.0),
                "benchmark_ticker": f.get("benchmark_ticker"),
                "cash_pct": doc.get("cash_pct") or 0.0,
                "holdings_as_of": doc.get("as_of"),
                "units": st.get("units"),
                "last_nav": st.get("last_nav"),
                "last_nav_date": st.get("last_nav_date"),
                "seed_value": f.get("seed_value"),
                "invested": st.get("seed_invested", f.get("seed_invested")),
                "holdings": [
                    {"n": h["name"], "t": h["ticker"], "w": float(h["weight_pct"])}
                    for h in holdings
                ],
            }
        )

    write_json(
        "config.json",
        {"tail_model": cfg["estimator"]["tail_model"], "funds": funds},
    )

    total = sum(len(f["holdings"]) for f in funds)
    tickers = {h["t"] for f in funds for h in f["holdings"]}
    print(f"[ok] docs/data/config.json — {len(funds)} funds, "
          f"{total} holdings, {len(tickers)} unique tickers")
    for f in funds:
        cov = sum(h["w"] for h in f["holdings"])
        flag = "" if cov > 70 else "   <- thin, reimport holdings"
        print(f"     {f['id']:24} {len(f['holdings']):3} names  {cov:5.1f}% cover{flag}")


if __name__ == "__main__":
    main()
