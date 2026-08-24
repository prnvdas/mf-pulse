"""Turn an AMC monthly portfolio disclosure (XLSX) into a holdings YAML.

Every AMC publishes these in the same broad SEBI layout: a header block, then
rows of [Name of Instrument, ISIN, Industry, Quantity, Market Value, % to NAV].
Column positions move around between fund houses, so we sniff the header row
rather than assuming.

ISIN is the key that makes this reliable — company names are spelt a dozen
different ways, ISINs are not. NSE's equity master gives us ISIN -> symbol.

Usage:
    python src/import_holdings.py --fund axis-small-cap --xlsx portfolio.xlsx
    python src/import_holdings.py --fund axis-small-cap --xlsx p.xlsx --sheet "Sheet2"
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys

import pandas as pd
import requests
import yaml

from common import HOLDINGS_DIR

NSE_EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/securities-available-for-trading",
}

CASH_HINTS = ("treps", "reverse repo", "net receivable", "cash", "clearing corp")


CACHE = HOLDINGS_DIR.parent / "nse_equity.csv"


def _parse_equity_csv(text: str) -> dict[str, str]:
    from io import StringIO

    df = pd.read_csv(StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    return {
        str(r["ISIN NUMBER"]).strip(): str(r["SYMBOL"]).strip()
        for _, r in df.iterrows()
    }


def isin_to_symbol(override: str | None = None) -> dict[str, str]:
    """ISIN -> NSE symbol. Cached, because NSE rate-limits and sometimes 403s.

    Order: explicit --isin-csv, then a cache under 30 days old, then NSE
    itself (with a cookie handshake, which their bot filter requires).
    """
    if override:
        return _parse_equity_csv(pathlib.Path(override).read_text())

    if CACHE.exists():
        age = dt.date.today() - dt.date.fromtimestamp(CACHE.stat().st_mtime)
        if age.days < 30:
            print(f"[info] using cached NSE list ({age.days}d old)")
            return _parse_equity_csv(CACHE.read_text())

    session = requests.Session()
    session.headers.update(UA)
    try:
        session.get("https://www.nseindia.com/", timeout=15)  # collect cookies
        resp = session.get(NSE_EQUITY_LIST, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        if CACHE.exists():
            print(f"[warn] NSE unreachable ({exc}); using stale cache",
                  file=sys.stderr)
            return _parse_equity_csv(CACHE.read_text())
        raise SystemExit(
            f"Couldn't fetch the NSE equity list: {exc}\n"
            f"Download it manually from\n  {NSE_EQUITY_LIST}\n"
            f"then rerun with:  --isin-csv /path/to/EQUITY_L.csv"
        ) from exc

    CACHE.write_text(resp.text)
    print(f"[info] cached NSE list -> {CACHE}")
    return _parse_equity_csv(resp.text)


def find_header(df: pd.DataFrame) -> int | None:
    """Locate the row that contains the real column headers."""
    for idx in range(min(30, len(df))):
        joined = " ".join(str(v).lower() for v in df.iloc[idx].tolist())
        if "isin" in joined and ("% to nav" in joined or "nav" in joined):
            return idx
    return None


def pick(cols: list[str], *needles: str) -> int | None:
    for i, col in enumerate(cols):
        low = col.lower()
        if all(n in low for n in needles):
            return i
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fund", required=True, help="fund id, e.g. axis-small-cap")
    ap.add_argument("--xlsx", required=True)
    ap.add_argument("--sheet", default=0)
    ap.add_argument("--list-sheets", action="store_true",
                    help="show every sheet in the file and exit")
    ap.add_argument("--scheme", default=None,
                    help="substring of the scheme name, for AMC files holding "
                         "many schemes in one sheet (e.g. 'Small Cap')")
    ap.add_argument("--isin-csv", default=None,
                    help="path to a manually downloaded NSE EQUITY_L.csv")
    ap.add_argument("--min-weight", type=float, default=0.10,
                    help="drop holdings below this %% to NAV")
    args = ap.parse_args()

    if args.list_sheets:
        xl = pd.ExcelFile(args.xlsx)
        print(f"{len(xl.sheet_names)} sheets in {args.xlsx}:")
        for s in xl.sheet_names:
            print(f"   {s}")
        return

    raw = pd.read_excel(args.xlsx, sheet_name=args.sheet, header=None)

    # AMCs publish one workbook covering every scheme they run. When --scheme
    # is given, narrow to the rows between that scheme's heading and the next
    # one, so we don't blend six funds' holdings into one file.
    if args.scheme:
        want = args.scheme.lower()
        joined = raw.apply(
            lambda r: " ".join(str(v) for v in r.tolist()).lower(), axis=1)
        starts = [i for i, t in enumerate(joined) if want in t and "isin" not in t]
        if not starts:
            names = sorted({t.strip()[:70] for t in joined
                            if "fund" in t and "isin" not in t and len(t.strip()) > 12})
            print(f"No scheme matching '{args.scheme}'. Candidates in this sheet:",
                  file=sys.stderr)
            for n in names[:40]:
                print("   " + n, file=sys.stderr)
            raise SystemExit(1)

        begin = starts[0]
        # the next scheme heading after ours marks the end of the block
        later = [i for i, t in enumerate(joined)
                 if i > begin + 3 and "fund" in t and "isin" not in t
                 and "total" not in t and len(t.strip()) > 12
                 and want not in t]
        end = later[0] if later else len(raw)
        print(f"[info] '{args.scheme}' found at row {begin}, block rows {begin}-{end}")
        raw = raw.iloc[begin:end].reset_index(drop=True)
    hdr = find_header(raw)
    if hdr is None:
        raise SystemExit(
            "Could not find a header row with ISIN and % to NAV. "
            "Open the file, note the correct sheet, and pass --sheet."
        )

    cols = [str(c).strip() for c in raw.iloc[hdr].tolist()]
    body = raw.iloc[hdr + 1:].reset_index(drop=True)

    i_name = pick(cols, "name") or 0
    i_isin = pick(cols, "isin")
    i_pct = pick(cols, "%", "nav")
    if i_isin is None or i_pct is None:
        raise SystemExit(f"Missing ISIN or %-to-NAV column. Found: {cols}")

    mapping = isin_to_symbol(args.isin_csv)
    print(f"[info] NSE master: {len(mapping)} ISINs")

    holdings, cash_pct, unmapped = [], 0.0, []

    for _, row in body.iterrows():
        name = str(row[i_name]).strip()
        isin = str(row[i_isin]).strip()
        try:
            pct = float(row[i_pct])
        except (TypeError, ValueError):
            continue
        if not name or name.lower() == "nan" or pct <= 0:
            continue

        if any(h in name.lower() for h in CASH_HINTS):
            cash_pct += pct
            continue

        symbol = mapping.get(isin)
        if not symbol:
            unmapped.append((name, isin, pct))
            continue
        if pct < args.min_weight:
            continue

        holdings.append(
            {
                "name": re.sub(r"\s+", " ", name)[:48],
                "ticker": f"{symbol}.NS",
                "weight_pct": round(pct, 3),
            }
        )

    holdings.sort(key=lambda h: h["weight_pct"], reverse=True)
    covered = sum(h["weight_pct"] for h in holdings)

    doc = {
        "fund_id": args.fund,
        "as_of": dt.date.today().isoformat(),
        "source": f"imported from {args.xlsx}",
        "cash_pct": round(cash_pct, 2),
        "holdings": holdings,
    }

    out = HOLDINGS_DIR / f"{args.fund}.yaml"
    with open(out, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)

    print(f"[ok] {out}")
    print(f"     {len(holdings)} holdings, {covered:.1f}% of NAV, "
          f"{cash_pct:.1f}% cash")
    if unmapped:
        print(f"[warn] {len(unmapped)} rows had no NSE symbol "
              f"(unlisted / BSE-only / debt). Largest:")
        for name, isin, pct in sorted(unmapped, key=lambda u: -u[2])[:5]:
            print(f"       {pct:5.2f}%  {name}  ({isin})")


if __name__ == "__main__":
    main()
