"""Turn a Groww mutual-fund TRANSACTION report (.xlsx or .csv) into config/transactions.csv.

That one file unlocks the exact numbers: per-fund XIRR, exact tax lots, and a "money over time" chart that starts
at your very first purchase instead of 21 Aug 2026.

    python src/import_transactions.py [report.xlsx] [--dry-run] [--force] [--out FILE]

Only the scheme, date, type, amount, units and NAV columns are read. Your name, PAN, phone and folio numbers are
never copied anywhere. The output has one line per transaction:

    date,fund_id,amount,units,nav       (amount > 0: money in, < 0: money out)

Before writing it checks the report against the holdings statement: each fund's units must add up to what you hold
(within 1%). A report covering only part of your history therefore refuses to import instead of drawing a wrong
chart; download it again with the full date range ("since inception").
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import os
import re
import sys

from common import ROOT, load_portfolio, read_json
from import_statement import norm, num

CSV_OUT = ROOT / "config" / "transactions.csv"
ALIASES = {
    "scheme": ["scheme name", "scheme", "fund name", "fund", "scheme_name"],
    "date": ["transaction date", "order date", "trade date", "allotment date", "processed on", "date"],
    "type": ["transaction type", "order type", "txn type", "type", "transaction", "description", "order"],
    "amount": ["amount", "transaction amount", "order amount", "value", "amount (₹)", "amount(rs)", "amount (rs)"],
    "units": ["units", "unit", "units allotted", "no. of units"],
    "nav": ["nav", "price", "nav (₹)", "nav price", "purchase nav"],
    "status": ["status", "order status", "transaction status"],
}
OK_STATUS = re.compile(r"success|complet|allot|confirm|process|settled|executed|done", re.I)
BAD_TEXT = re.compile(r"reject|fail|cancel|pending|rever", re.I)
OUT_TEXT = re.compile(r"redem|sell|sale|withdraw|switch[\s_-]*out|swp|stp[\s_-]*out", re.I)
NOCASH_TEXT = re.compile(r"reinvest|bonus", re.I)                  # units arrive without money
PAYOUT_TEXT = re.compile(r"payout|dividend|idcw", re.I)            # cash comes out as dividend: not a purchase or sale
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d %B %Y", "%b %d, %Y", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S")


def match_fund(scheme: str, funds: list[dict]) -> dict | None:
    """Strict: the report's name and the configured name must start the same (a report may truncate long names).
    Looser word-overlap matching would file a different fund with a similar name (another Flexi Cap) under yours."""
    s = norm(scheme)
    for f in funds:
        n = norm(f["name"])
        k = min(len(s), len(n))
        if k >= 20 and s[:k] == n[:k]:
            return f
    return None


def to_date(v) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    t = str(v or "").strip()
    for f in DATE_FORMATS:
        try:
            return dt.datetime.strptime(t, f).date()
        except ValueError:
            continue
    return None


def load_rows(path: str) -> list[list]:
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return [list(r) for r in csv.reader(fh)]
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    rows: list[list] = []
    for ws in wb.worksheets:
        rows += [[c.value for c in r] for r in ws.iter_rows()]
    return rows


def find_header(rows: list[list]) -> tuple[int, dict] | None:
    for i, r in enumerate(rows):
        cells = [str(c).strip().lower() if c is not None else "" for c in r]
        col = {}
        for key, names in ALIASES.items():
            for name in names:                       # earlier alias wins: "transaction date" before plain "date"
                if name in cells:
                    col[key] = cells.index(name)
                    break
        if "scheme" in col and "date" in col and ("amount" in col or "units" in col):
            return i, col
    return None


def is_transaction_report(path: str) -> bool:
    try:
        return find_header(load_rows(path)) is not None
    except Exception:  # noqa: BLE001
        return False


def parse(path: str, cfg: dict) -> tuple[list[dict], dict]:
    rows = load_rows(path)
    h = find_header(rows)
    if not h:
        sys.exit("Couldn't find the transaction table (a row with scheme, date and amount/units columns). "
                 "Is this Groww's mutual-fund transaction report? Run with --dry-run and tell me the column names.")
    hi, col = h
    out, skipped = [], {"status": 0, "other": 0, "payout": 0, "unmatched": set()}
    for r in rows[hi + 1:]:
        get = lambda k: r[col[k]] if k in col and col[k] < len(r) else None
        scheme, d = get("scheme"), to_date(get("date"))
        if not scheme or d is None:
            continue
        kind = str(get("type") or "")
        status = str(get("status") or "")
        if BAD_TEXT.search(kind + " " + status) or (status.strip() and not OK_STATUS.search(status)):
            skipped["status"] += 1
            continue
        if PAYOUT_TEXT.search(kind) and not NOCASH_TEXT.search(kind):
            skipped["payout"] += 1
            continue
        f = match_fund(str(scheme), cfg["funds"])
        if not f:
            skipped["unmatched"].add(str(scheme).strip()[:40])
            continue
        amt, units, nav = num(get("amount")), num(get("units")), num(get("nav"))
        sign = -1 if (OUT_TEXT.search(kind) or (units is not None and units < 0) or (amt is not None and amt < 0)) else 1
        if amt is None and units is not None and nav:
            amt = abs(units) * nav
        if NOCASH_TEXT.search(kind):
            amt = 0.0
        if amt is None:
            skipped["other"] += 1
            continue
        out.append({"date": d, "fund_id": f["id"], "amount": round(sign * abs(amt), 2),
                    "units": None if units is None else round(sign * abs(units), 4), "nav": nav})
    out.sort(key=lambda x: (x["date"], x["fund_id"]))
    return out, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="report (.xlsx/.csv); default: the newest transaction report in MF-Holding/")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="write even if the totals don't match the holdings")
    ap.add_argument("--out", default=str(CSV_OUT))
    args = ap.parse_args()
    path = args.file or next(iter(sorted((p for p in glob.glob(str(ROOT / "MF-Holding" / "*.*")) if p.lower().endswith((".xlsx", ".csv")) and is_transaction_report(p)),
                                         key=os.path.getmtime, reverse=True)), None)
    if not path:
        sys.exit("No transaction report found: put Groww's mutual-fund transaction report in MF-Holding/.")
    cfg, state = load_portfolio(), read_json("state.json", {})
    tx, skipped = parse(path, cfg)
    if not tx:
        sys.exit("No usable transactions found in that file.")
    print(f"[info] {len(tx)} transactions from {tx[0]['date']} to {tx[-1]['date']}  (skipped: {skipped['status']} not completed, {skipped['payout']} dividend payouts, {skipped['other']} without amount)")
    if skipped["unmatched"]:
        print("[info] ignored funds that aren't in your portfolio: " + ", ".join(sorted(skipped["unmatched"])))
    problems, patched = [], []
    # A report a little short of your holding usually means a small purchase made outside Groww (or an old transfer).
    # Up to 3% of the units: add one balancing "opening" line instead of refusing, and say so.
    for f in cfg["funds"]:
        rs = [t for t in tx if t["fund_id"] == f["id"]]
        st = state.get(f["id"]) or {}
        held = st.get("units") or 0
        if rs and held and all(t["units"] is not None for t in rs):
            gap = held - sum(t["units"] for t in rs)
            if 0.0005 * held < gap <= 0.03 * held:
                net = sum(t["amount"] for t in rs)
                amt = max((st.get("seed_invested") or 0) - net, 0) or gap * (rs[0]["nav"] or 0)
                tx.append({"date": rs[0]["date"], "fund_id": f["id"], "amount": round(amt, 2), "units": round(gap, 4), "nav": None})
                patched.append(f"{f['id']}: the report is {gap:,.1f} units ({gap / held * 100:.1f}%) short of your holding, so I added one opening line of those units and about ₹{amt:,.0f}, dated {rs[0]['date']} (likely bought outside Groww)")
    tx.sort(key=lambda x: (x["date"], x["fund_id"]))
    print(f"\n{'fund':<24}{'txns':>6}{'first':>12}{'units (report)':>16}{'units (held)':>14}{'net money in':>16}")
    for f in cfg["funds"]:
        rs = [t for t in tx if t["fund_id"] == f["id"]]
        have = (state.get(f["id"]) or {}).get("units") or 0
        u = sum(t["units"] for t in rs if t["units"] is not None)
        have_units = all(t["units"] is not None for t in rs) and rs
        print(f"{f['id']:<24}{len(rs):>6}{str(rs[0]['date']) if rs else '-':>12}{u:>16,.3f}{have:>14,.3f}{sum(t['amount'] for t in rs):>16,.0f}")
        if not rs:
            problems.append(f"{f['id']}: no transactions in the report")
        elif have_units and have and abs(u / have - 1) > 0.01:
            problems.append(f"{f['id']}: the report's units ({u:,.1f}) don't match your holding ({have:,.1f}); is the date range the full history?")
    for m in patched:
        print("[note] " + m)
    if problems and not args.force:
        print("\n[refused] the report doesn't add up to your holdings:\n  " + "\n  ".join(problems) + "\nDownload it again for the full history (since inception), or use --force.")
        sys.exit(1)
    if args.dry_run:
        print("\n[dry-run] nothing written")
        return
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        fh.write("# Generated by src/import_transactions.py from a Groww transaction report: dates and amounts only.\n")
        w.writerow(["date", "fund_id", "amount", "units", "nav"])
        for t in tx:
            w.writerow([t["date"].isoformat(), t["fund_id"], f"{t['amount']:.2f}", "" if t["units"] is None else f"{t['units']:.4f}", "" if t["nav"] is None else t["nav"]])
    print(f"\n[ok] wrote {args.out}: exact XIRR, tax lots and the full money-over-time history will use it from the next nightly run")


if __name__ == "__main__":
    main()
