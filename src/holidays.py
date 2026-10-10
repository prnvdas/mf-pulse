"""NSE trading-holiday calendar -> docs/data/holidays.json, and an evening reminder before a closure.

Source: NSE's own holiday master (equity "CM" segment, with the reason for each holiday, e.g. "Mahatma Gandhi
Jayanti", "Dussehra"; the "MF" segment lists days mutual-fund NAVs are not declared). The dashboard and the
pipeline use it so that a closed market is explained ("closed today: Dussehra"), not just detected.

    python src/holidays.py            refresh docs/data/holidays.json (kept as-is if NSE can't be reached)
    python src/holidays.py --remind   open a GitHub issue (-> email/phone notification) if a weekday closure
                                      is coming before the next trading day

NSE only publishes next year's list late in the year; the refresh simply picks it up when it appears.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys

from common import load_portfolio, now_ist, read_json, write_json


def parse_day(txt: str) -> dt.date:
    return dt.datetime.strptime(txt, "%d-%b-%Y").date()


def refresh() -> None:
    import why                                         # reuses the browser-style NSE session
    s = why.nse_session()
    if not s:
        print("[error] NSE unreachable; leaving holidays.json unchanged", file=sys.stderr)
        sys.exit(1)
    try:
        r = s.get("https://www.nseindia.com/api/holiday-master?type=trading", timeout=30)
        r.raise_for_status()
        j = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[error] holiday list failed ({exc}); leaving holidays.json unchanged", file=sys.stderr)
        sys.exit(1)

    def rows(seg: str) -> list[dict]:
        out = []
        for x in j.get(seg) or []:
            try:
                d = parse_day(x["tradingDate"])
            except (KeyError, ValueError):
                continue
            name = str(x.get("description") or "").strip()
            out.append({"date": d.isoformat(), "day": d.strftime("%A"), "name": name.rstrip("*").strip(),
                        "weekend": d.weekday() >= 5, "special_session": name.endswith("*")})
        return sorted(out, key=lambda r: r["date"])

    cm = rows("CM")
    if not cm:
        print("[error] NSE returned no equity holidays; leaving holidays.json unchanged", file=sys.stderr)
        sys.exit(1)
    cfg = load_portfolio()
    configured = {str(x.get("date")) for x in cfg.get("special_sessions") or []}
    for h in cm:
        if h["special_session"] and h["date"] not in configured:
            print(f"[warn] NSE marks {h['date']} ({h['name']}) as having a special session, but config/portfolio.yaml has none: "
                  "add it once NSE announces the timing", file=sys.stderr)
    write_json("holidays.json", {"fetched_at": now_ist().isoformat(), "source": "NSE holiday master", "holidays": cm, "mf_holidays": rows("MF")})
    print(f"[ok] holidays.json: {len(cm)} equity holidays ({cm[0]['date']} to {cm[-1]['date']}); "
          f"{sum(1 for h in cm if not h['weekend'])} fall on weekdays")


def next_closure(today: dt.date, cfg: dict, hol: dict) -> tuple[list[dict], dt.date]:
    """Weekday holidays that fall between tomorrow and the next trading day, and that next trading day."""
    by = {h["date"]: h for h in hol.get("holidays", []) if not h["weekend"]}
    special = {str(x.get("date")) for x in cfg.get("special_sessions") or []}
    d, found = today + dt.timedelta(days=1), []
    while True:
        iso = d.isoformat()
        if iso in special:
            return found, d
        if d.weekday() < 5 and iso in by:
            found.append(by[iso])
        elif d.weekday() < 5:
            return found, d
        d += dt.timedelta(days=1)


def remind() -> None:
    cfg, hol = load_portfolio(), read_json("holidays.json", {})
    today = now_ist().date()
    found, nxt = next_closure(today, cfg, hol)
    if not found:
        print("[info] no weekday closure before the next trading day; no reminder")
        return
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    for h in found:
        d = dt.date.fromisoformat(h["date"])
        title = f"Market closed on {d.strftime('%a %-d %b %Y')}: {h['name']}"
        body = (f"The NSE is closed on **{d.strftime('%A %-d %B %Y')}** for **{h['name']}**. "
                f"Prices and your estimate won't move that day. The next trading day is {nxt.strftime('%A %-d %B')}.")
        print(f"[remind] {title}")
        if not (repo and token):
            print("   (no GITHUB_REPOSITORY/GH_TOKEN: not creating an issue)")
            continue
        have = subprocess.run(["gh", "issue", "list", "-R", repo, "--state", "all", "--search", f'in:title "{title}"', "--json", "number", "-q", "length"],
                              capture_output=True, text=True).stdout.strip()
        if have and have != "0":
            print("   already reminded")
            continue
        subprocess.run(["gh", "issue", "create", "-R", repo, "--assignee", os.environ.get("GITHUB_REPOSITORY_OWNER", ""), "--title", title, "--body", body], check=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remind", action="store_true")
    args = ap.parse_args()
    remind() if args.remind else refresh()


if __name__ == "__main__":
    main()
