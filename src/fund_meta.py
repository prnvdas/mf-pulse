"""What the market says about each fund, straight from Groww's own page: nothing here is typed in by hand.

Writes config/fund_meta.json with, per fund: the current total expense ratio (and the base one), the AMFI scheme code,
the category, the benchmark's name, fund size, launch date, and whether the scheme is closed.
`load_portfolio()` layers the expense ratio over portfolio.yaml.

    python src/fund_meta.py            # every fund
    python src/fund_meta.py --fund axis-small-cap

Scraping, like fetch_holdings.py: if Groww changes its page, a fund's entry simply keeps its last good values.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests

from common import CONFIG, load_portfolio, now_ist
from fetch_holdings import GROWW, UA, extract_next_data


def slug_for(f: dict) -> str:
    from import_statement import norm  # noqa: F401  (kept for symmetry with the statement importer)
    import re
    return f.get("groww_slug") or re.sub(r"[^a-z0-9]+", "-", f["name"].lower()).strip("-")


def fetch_one(f: dict) -> dict | None:
    for attempt in range(3):
        try:
            r = requests.get(GROWW.format(slug=slug_for(f)), headers=UA, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            nd = extract_next_data(r.text) or {}
            pp = nd.get("props", {}).get("pageProps", {})
            m = pp.get("mfServerSideData") or {}
            if not m.get("scheme_code"):
                raise RuntimeError("no scheme data in the page")
            num = lambda v: float(v) if v not in (None, "", "None") else None
            return {"fetched": now_ist().date().isoformat(), "scheme_code": int(m["direct_scheme_code"] or m["scheme_code"]),
                    "name": m.get("scheme_name"), "expense_ratio": num(m.get("expense_ratio")), "base_expense_ratio": num(m.get("base_expense_ratio")),
                    "sub_category": m.get("sub_category"), "benchmark_name": m.get("benchmark_name"), "aum_cr": num(m.get("aum")),
                    "launch_date": m.get("launch_date"), "closed": bool(m.get("closed_scheme"))}
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {f['id']} attempt {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(3)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fund")
    args = ap.parse_args()
    path = CONFIG / "fund_meta.json"
    try:
        meta = json.loads(path.read_text())
    except (OSError, ValueError):
        meta = {}
    cfg = load_portfolio()
    got = 0
    for f in cfg["funds"]:
        if args.fund and f["id"] != args.fund:
            continue
        m = fetch_one(f)
        if not m:
            print(f"[warn] {f['id']}: no fresh metadata; keeping the last good values")
            continue
        old = (meta.get(f["id"]) or {}).get("expense_ratio")
        meta[f["id"]] = m
        got += 1
        print(f"[ok] {f['id']}: expense ratio {m['expense_ratio']}% (was {old if old is not None else f.get('ter_annual_pct')}), "
              f"{m['sub_category']}, benchmark {m['benchmark_name']}")
        time.sleep(1)
    if not got:
        sys.exit("[error] nothing fetched; file left as it was")
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
