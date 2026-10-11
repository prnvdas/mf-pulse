"""Find your SIPs in your own order history, so the amounts and dates never need typing in.

Reads config/transactions.csv (written by import_transactions.py, extended nightly by reconcile.py) and writes
config/sips_detected.json. A SIP is an amount that was bought in each of the last two months, or in at least three of
the last six, rounded to the nearest 100 (Groww nets a tiny stamp duty off: 49,997.50 is a 50,000 SIP). A SIP that has
not run in the last two months counts as stopped. For every fund that appears in the history, this file replaces
the `sips:` list in portfolio.yaml; funds without a history keep the yaml values.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import statistics

from common import CONFIG, load_portfolio, now_ist


def months_back(d: dt.date, n: int) -> dt.date:
    y, m = d.year, d.month - n
    while m < 1:
        y, m = y - 1, m + 12
    return dt.date(y, m, 1)


def detect(rows: list[tuple[dt.date, str, float]]) -> tuple[list[dict], list[str]]:
    by: dict[str, list[tuple[dt.date, float]]] = collections.defaultdict(list)
    for d, f, a in rows:
        if a > 0:
            by[f].append((d, a))
    ref = max((d for d, _, _ in rows), default=None)
    out = []
    if not ref:
        return out, []
    cur = ref.replace(day=1)
    recent = {months_back(cur, 0), months_back(cur, 1)}
    window = {months_back(cur, k) for k in range(6)}
    for f, xs in by.items():
        groups: dict[int, list[dt.date]] = collections.defaultdict(list)
        for d, a in xs:
            if d.replace(day=1) in window:
                groups[int(round(a / 100.0)) * 100].append(d)
        best = None
        for amt, ds in groups.items():
            months = {d.replace(day=1) for d in ds}
            active = months & recent
            if not active or not (recent <= months or len(months) >= 3):
                continue
            key = (max(months), len(months))
            if best is None or key > best[0]:
                best = (key, amt, ds)
        if best:
            _, amt, ds = best
            out.append({"fund_id": f, "amount": amt, "day_of_month": int(statistics.median(sorted(d.day for d in ds)))})
    return out, sorted(by)


def main() -> None:
    path = CONFIG / "transactions.csv"
    if not path.exists():
        print("[info] no config/transactions.csv: SIPs stay as set in portfolio.yaml")
        return
    rows = []
    for r in csv.reader(l for l in path.read_text().splitlines() if l and not l.startswith("#")):
        try:
            rows.append((dt.date.fromisoformat(r[0]), r[1], float(r[2])))
        except (ValueError, IndexError):
            continue                                       # the header line
    sips, checked = detect(rows)
    # compare with the yaml, for the log
    yaml_sips = {s["fund_id"]: s for s in (load_portfolio().get("sips") or [])}
    for s in sips:
        y = yaml_sips.get(s["fund_id"])
        print(f"[ok] {s['fund_id']}: SIP ₹{s['amount']:,} around day {s['day_of_month']}" + (f" (yaml said ₹{y['amount']:,.0f} on day {y['day_of_month']})" if y else ""))
    for f in checked:
        if f not in {s["fund_id"] for s in sips}:
            print(f"[ok] {f}: no active SIP in the last two months")
    import json
    (CONFIG / "sips_detected.json").write_text(json.dumps({"as_of": now_ist().date().isoformat(), "basis": "your order history (config/transactions.csv)",
                                                        "funds_checked": checked, "sips": sips}, indent=2) + "\n")


if __name__ == "__main__":
    main()
