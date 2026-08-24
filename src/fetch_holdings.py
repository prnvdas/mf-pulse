"""Fetch each fund's holdings straight off Groww. No manual downloads.

Groww server-renders the full portfolio into the page (all ~130 names for Axis
Small Cap, not just a top-10), which makes it the only source that's both
complete and scrapable without a browser.

    python src/fetch_holdings.py            # all funds
    python src/fetch_holdings.py --fund axis-small-cap

Two things worth knowing:

  * This is scraping. Groww can change their page shape and break it. The
    parser hunts for the holdings array structurally rather than by fixed key
    names, so it survives renames, but not a redesign. If it breaks,
    import_holdings.py with a downloaded XLSX still works.

  * Holdings are still a MONTH OLD. SEBI mandates monthly disclosure; there is
    no live feed of what a fund owns, from Groww or anyone. Automating the
    fetch removes the chore, not the lag.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sys

import requests
import yaml

from common import HOLDINGS_DIR, load_portfolio
from import_holdings import isin_to_symbol

GROWW = "https://groww.in/mutual-funds/{slug}"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

NAME_KEYS = ("company_name", "companyname", "name", "stock_name", "holding_name")
PCT_KEYS = ("corpus_per", "corpusper", "percentage", "percent", "weightage",
            "allocation", "holding_percentage")

# Rows that aren't tradeable equity — treated as cash/other, not as holdings.
NON_EQUITY = re.compile(
    r"tbill|t-bill|treasury|govt|government of india|repo|treps|net receivable|"
    r"cash|margin|clearing corp|gsec|g-sec|sdl|commercial paper|certificate of deposit",
    re.I)

SUFFIXES = re.compile(
    r"\b(ltd|limited|ltd\.|corp|corporation|company|co|inc|plc|"
    r"india|of india|\(india\)|the)\b", re.I)


def extract_next_data(html: str) -> dict | None:
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def find_holdings(node, best=None):
    """Walk the JSON tree for the largest array of {name, percent} objects.

    Structural rather than path-based, so a key rename upstream doesn't break
    it — only a genuine redesign would.
    """
    best = best or []

    if isinstance(node, list):
        if len(node) > len(best) and node and isinstance(node[0], dict):
            keys = {k.lower() for k in node[0]}
            has_name = any(k in keys for k in NAME_KEYS)
            has_pct = any(k in keys for k in PCT_KEYS)
            if has_name and has_pct:
                best = node
        for item in node:
            best = find_holdings(item, best)

    elif isinstance(node, dict):
        for value in node.values():
            best = find_holdings(value, best)

    return best


def pick(d: dict, candidates) -> str | None:
    lower = {k.lower(): k for k in d}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


def normalise(name: str) -> str:
    n = re.sub(r"[^\w\s&]", " ", name.lower())
    n = SUFFIXES.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def build_matcher(mapping: dict[str, str], names: dict[str, str]):
    """names: {ISIN: company name}. Returns fn(groww_name) -> symbol | None."""
    index: dict[str, str] = {}
    for isin, company in names.items():
        sym = mapping.get(isin)
        if sym:
            index.setdefault(normalise(company), sym)
    keys = list(index)

    def match(raw: str) -> str | None:
        n = normalise(raw)
        if n in index:
            return index[n]
        close = difflib.get_close_matches(n, keys, n=1, cutoff=0.88)
        return index[close[0]] if close else None

    return match


def nse_company_names() -> dict[str, str]:
    from io import StringIO

    import pandas as pd

    from import_holdings import CACHE, NSE_EQUITY_LIST

    if CACHE.exists():
        text = CACHE.read_text()
    else:
        s = requests.Session()
        s.headers.update(UA)
        s.get("https://www.nseindia.com/", timeout=15)
        r = s.get(NSE_EQUITY_LIST, timeout=30)
        r.raise_for_status()
        CACHE.write_text(r.text)
        text = r.text

    df = pd.read_csv(StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    return {str(r["ISIN NUMBER"]).strip(): str(r["NAME OF COMPANY"]).strip()
            for _, r in df.iterrows()}


def fetch_fund(fund: dict, match) -> bool:
    slug = fund.get("groww_slug")
    if not slug:
        print(f"[skip] {fund['id']}: no groww_slug in portfolio.yaml", file=sys.stderr)
        return False

    url = GROWW.format(slug=slug)
    try:
        resp = requests.get(url, headers=UA, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"[fail] {fund['id']}: {exc}", file=sys.stderr)
        return False

    data = extract_next_data(resp.text)
    if not data:
        print(f"[fail] {fund['id']}: no __NEXT_DATA__ — page shape changed",
              file=sys.stderr)
        return False

    rows = find_holdings(data)
    if not rows:
        print(f"[fail] {fund['id']}: no holdings array found", file=sys.stderr)
        return False

    name_key = pick(rows[0], NAME_KEYS)
    pct_key = pick(rows[0], PCT_KEYS)

    holdings, other_pct, unmatched = [], 0.0, []
    for row in rows:
        raw = str(row.get(name_key) or "").strip()
        try:
            pct = float(row.get(pct_key))
        except (TypeError, ValueError):
            continue
        if not raw or pct <= 0:
            continue

        if NON_EQUITY.search(raw):
            other_pct += pct
            continue

        sym = match(raw)
        if not sym:
            unmatched.append((raw, pct))
            other_pct += pct
            continue

        holdings.append({"name": raw[:48], "ticker": f"{sym}.NS",
                         "weight_pct": round(pct, 3)})

    holdings.sort(key=lambda h: h["weight_pct"], reverse=True)
    covered = sum(h["weight_pct"] for h in holdings)

    doc = {
        "fund_id": fund["id"],
        "as_of": dt.date.today().isoformat(),
        "source": f"groww:{slug}",
        "cash_pct": round(other_pct, 2),
        "holdings": holdings,
    }
    out = HOLDINGS_DIR / f"{fund['id']}.yaml"
    with open(out, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)

    flag = "" if covered > 80 else "   <- low, check unmatched names"
    print(f"[ok] {fund['id']:24} {len(holdings):3} names  {covered:5.1f}% cover{flag}")
    if unmatched:
        print(f"     {len(unmatched)} unmatched (counted as tail). Largest:")
        for raw, pct in sorted(unmatched, key=lambda u: -u[1])[:5]:
            print(f"       {pct:5.2f}%  {raw}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fund", default=None, help="only this fund id")
    args = ap.parse_args()

    cfg = load_portfolio()
    funds = [f for f in cfg["funds"] if not args.fund or f["id"] == args.fund]
    if not funds:
        raise SystemExit(f"No fund with id '{args.fund}'")

    print("[info] loading NSE symbol master")
    match = build_matcher(isin_to_symbol(), nse_company_names())

    if not sum(fetch_fund(f, match) for f in funds):
        raise SystemExit("Nothing fetched.")
    print("\nNow run:  python src/export_web.py && python src/estimate.py --force")


if __name__ == "__main__":
    main()
