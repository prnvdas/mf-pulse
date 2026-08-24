"""Generate the contents of a Google Sheet that prices your holdings.

Google killed the Finance API in 2012, but the GOOGLEFINANCE() spreadsheet
function survives and covers NSE. This writes a TSV you paste into a sheet;
the formulas then do the pricing, and "Publish to web" turns the result into
a CORS-friendly CSV the dashboard can read.

    python src/make_sheet.py > prices.tsv

Then:
  1. sheets.new -> rename the tab to "Prices"
  2. Paste into A1 (Paste special > values only keeps the formulas intact)
  3. File > Share > Publish to web -> "Prices" -> CSV -> Publish
  4. Paste that pub?output=csv URL into the dashboard

Ticker note: NSE equities are NSE:SYMBOL. Indices use INDEXNSE: and Google's
index names don't always match NSE's own — verify each one resolves in the
sheet before trusting it. Anything showing #N/A needs its symbol corrected
or should be dropped.
"""

from __future__ import annotations

import sys

from common import load_holdings, load_portfolio

# Yahoo index tickers -> Google's INDEXNSE equivalents.
# These are the shakiest part of the file — confirm in the sheet.
INDEX_MAP = {
    "^CNXSC": "INDEXNSE:NIFTY_SMLCAP_100",
    "^NSEMDCP50": "INDEXNSE:NIFTY_MIDCAP_100",
    "^CRSLDX": "INDEXNSE:NIFTY_500",
    "^NSEI": "INDEXNSE:NIFTY_50",
}


def to_google(ticker: str) -> str | None:
    if ticker.startswith("^"):
        return INDEX_MAP.get(ticker)
    if ticker.endswith(".NS"):
        # GOOGLEFINANCE chokes on '&' in symbols like M&M — it needs no escaping
        # in the ticker itself, but flag it so you notice if it fails.
        return "NSE:" + ticker[:-3]
    if ticker.endswith(".BO"):
        return "BOM:" + ticker[:-3]
    return None


def main() -> None:
    cfg = load_portfolio()

    seen: dict[str, str] = {}
    for f in cfg["funds"]:
        for h in load_holdings(f["id"]).get("holdings") or []:
            seen.setdefault(h["ticker"], h["name"])
        if f.get("benchmark_ticker"):
            seen.setdefault(f["benchmark_ticker"], f"{f['id']} benchmark")

    out = sys.stdout
    out.write("ticker\tgoogle\tlast\tprev\tpct\tname\n")

    skipped = []
    row = 2
    for ticker, name in sorted(seen.items()):
        g = to_google(ticker)
        if not g:
            skipped.append((ticker, name))
            continue
        # IFERROR keeps one bad symbol from poisoning the whole column.
        last = f'=IFERROR(GOOGLEFINANCE("{g}","price"),"")'
        prev = f'=IFERROR(GOOGLEFINANCE("{g}","closeyest"),"")'
        pct = f'=IFERROR(IF(D{row}="","",(C{row}-D{row})/D{row}*100),"")'
        out.write(f"{ticker}\t{g}\t{last}\t{prev}\t{pct}\t{name}\n")
        row += 1

    print(f"\n# {row - 2} tickers written", file=sys.stderr)
    if skipped:
        print(f"# {len(skipped)} had no Google equivalent:", file=sys.stderr)
        for t, n in skipped:
            print(f"#   {t}  ({n})", file=sys.stderr)
    print("# After publishing, check for #N/A rows — those are usually thin "
          "smallcaps Google doesn't quote.", file=sys.stderr)


if __name__ == "__main__":
    main()
