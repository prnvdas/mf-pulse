"""Add a fund that appears in your holdings statement but not in portfolio.yaml, with no typing.

import_statement.py calls register() for every scheme it can't match. Everything is looked up, nothing guessed:
  * AMFI scheme code, current expense ratio, category and benchmark name: from Groww's page for the fund (fund_meta.py);
  * the benchmark used for the comparison: the category's usual index (BENCHMARKS below: a mapping from category to
    index, not a per-fund setting);
  * holdings: fetched on the next `fetch_holdings.py` run (the statement workflow runs it right after).
The result is appended to config/funds_auto.json, which load_portfolio() layers over portfolio.yaml.
"""
from __future__ import annotations

import json
import re

from common import CONFIG
from fund_meta import fetch_one

NIFTY500 = {"name": "Nifty 500", "note": "index level (price return); benchmark P/E approximated from a 75/15/10 large/mid/small ETF mix",
            "returns": [{"ticker": "^CRSLDX", "weight": 1.0}],
            "pe": [{"ticker": "HDFCNIF100.NS", "weight": 0.75}, {"ticker": "HDFCMID150.NS", "weight": 0.15}, {"ticker": "HDFCSML250.NS", "weight": 0.10}]}
BENCHMARKS = {
    "Small Cap": ("NIFTYSMLCAP250.NS", {"name": "Nifty Smallcap 250", "note": "index level (price return); P/E from the HDFC Nifty Smallcap 250 ETF",
                                        "returns": [{"ticker": "NIFTYSMLCAP250.NS", "weight": 1.0}], "pe": [{"ticker": "HDFCSML250.NS", "weight": 1.0}]}),
    "Mid Cap": ("^NSEMDCP50", {"name": "Nifty Midcap 150", "note": "HDFC Nifty Midcap 150 ETF (price return)",
                               "returns": [{"ticker": "HDFCMID150.NS", "weight": 1.0}], "xirr_returns": [{"ticker": "^NSEMDCP50", "weight": 1.0}],
                               "pe": [{"ticker": "HDFCMID150.NS", "weight": 1.0}]}),
    "Large Cap": ("^NSEI", {"name": "Nifty 100", "note": "HDFC Nifty 100 ETF (price return)",
                            "returns": [{"ticker": "HDFCNIF100.NS", "weight": 1.0}], "xirr_returns": [{"ticker": "^NSEI", "weight": 1.0}],
                            "pe": [{"ticker": "HDFCNIF100.NS", "weight": 1.0}]}),
    "Large & MidCap": ("^NSEMDCP50", {"name": "Nifty LargeMidcap 250", "note": "approx. 70/30 blend of the Nifty 100 and Nifty Midcap 150 ETFs; price return",
                                      "returns": [{"ticker": "HDFCNIF100.NS", "weight": 0.7}, {"ticker": "HDFCMID150.NS", "weight": 0.3}],
                                      "xirr_returns": [{"ticker": "^NSEI", "weight": 0.7}, {"ticker": "^NSEMDCP50", "weight": 0.3}],
                                      "pe": [{"ticker": "HDFCNIF100.NS", "weight": 0.7}, {"ticker": "HDFCMID150.NS", "weight": 0.3}]}),
}
INDEX_FUNDS = ("^NSEI", {"name": "Nifty 50", "note": "index level (price return)", "returns": [{"ticker": "^NSEI", "weight": 1.0}],
                         "pe": [{"ticker": "HDFCNIF100.NS", "weight": 1.0}]})


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def register(scheme: str, invested: float, value: float) -> dict | None:
    slug = slugify(scheme)
    stub = {"id": slug, "name": scheme, "groww_slug": slug}
    m = fetch_one(stub)
    if not m:
        print(f"[warn] couldn't look up '{scheme}' on Groww (tried /mutual-funds/{slug}); it can't be added automatically")
        return None
    cat = m.get("sub_category") or ""
    ticker, bench = BENCHMARKS.get(cat) or (INDEX_FUNDS if "index" in cat.lower() else ("^CRSLDX", NIFTY500))
    fid = re.sub(r"-(fund-)?direct(-plan)?-growth$", "", slug)
    fund = {"id": fid, "name": scheme, "amfi_code": m["scheme_code"], "ter_annual_pct": m.get("expense_ratio") or 1.0,
            "benchmark_ticker": ticker, "groww_slug": slug, "analytics_benchmark": bench,
            "units": None, "seed_value": round(value), "seed_invested": round(invested), "auto_added": True, "category": cat}
    path = CONFIG / "funds_auto.json"
    try:
        have = json.loads(path.read_text())
    except (OSError, ValueError):
        have = []
    if fid not in {f.get("id") for f in have}:
        have.append(fund)
        path.write_text(json.dumps(have, indent=2, ensure_ascii=False) + "\n")
    print(f"[new fund] added '{scheme}' as {fid}: {cat}, expense ratio {fund['ter_annual_pct']}%, compared with {bench['name']}; "
          "holdings and metadata are fetched next (fetch_holdings.py, fund_meta.py)")
    return fund
