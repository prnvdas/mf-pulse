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
import datetime as dt
import json
import sys
import time

import requests

from common import CONFIG, load_portfolio, now_ist, write_json
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
                    "lead_manager": m.get("fund_manager"),
                    "managers": sorted({str(d.get("person_name")).strip() for d in (m.get("fund_manager_details") or []) if isinstance(d, dict) and d.get("person_name")}),
                    "sub_category": m.get("sub_category"), "benchmark_name": m.get("benchmark_name"), "aum_cr": num(m.get("aum")),
                    "launch_date": m.get("launch_date"), "closed": bool(m.get("closed_scheme"))}
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {f['id']} attempt {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(3)
    return None


def track_changes(old: dict | None, new: dict, today: str) -> dict:
    """Keep a snapshot history per fund and turn differences into plain-English events: a manager change, a new expense ratio,
    a big move in fund size. Returns `new` with `history` and `events` carried over / extended."""
    hist = list((old or {}).get("history") or [])
    events = list((old or {}).get("events") or [])
    snap = {"date": today, "expense_ratio": new.get("expense_ratio"), "lead": new.get("lead_manager"), "managers": new.get("managers") or [], "aum_cr": new.get("aum_cr")}
    prev = hist[-1] if hist else None
    if prev and prev.get("date") != today:
        pm, nm = set(prev.get("managers") or []), set(snap["managers"])
        if prev.get("lead") != snap["lead"] and snap["lead"] and prev.get("lead"):
            events.append({"date": today, "kind": "manager", "text": f"Lead fund manager changed: {prev['lead']} → {snap['lead']}."})
        elif pm and nm and pm != nm:
            left, joined = sorted(pm - nm), sorted(nm - pm)
            events.append({"date": today, "kind": "manager", "text": "Manager team changed" + (f"; left: {', '.join(left)}" if left else "") + (f"; joined: {', '.join(joined)}" if joined else "") + "."})
        a, b = prev.get("expense_ratio"), snap["expense_ratio"]
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(b - a) >= 0.02:
            events.append({"date": today, "kind": "cost", "text": f"Expense ratio {'rose' if b > a else 'fell'}: {a:.2f}% → {b:.2f}%."})
    old_snap = next((h for h in reversed(hist) if (dt.date.fromisoformat(today) - dt.date.fromisoformat(h["date"])).days >= 28), None)
    if old_snap and isinstance(old_snap.get("aum_cr"), (int, float)) and isinstance(snap["aum_cr"], (int, float)) and old_snap["aum_cr"] > 0:
        ch = snap["aum_cr"] / old_snap["aum_cr"] - 1
        if abs(ch) >= 0.20 and not any(e["kind"] == "size" and (dt.date.fromisoformat(today) - dt.date.fromisoformat(e["date"])).days < 28 for e in events):
            events.append({"date": today, "kind": "size", "text": f"Fund size {'grew' if ch > 0 else 'shrank'} {abs(ch) * 100:.0f}% in about a month: ₹{old_snap['aum_cr']:,.0f} cr → ₹{snap['aum_cr']:,.0f} cr."})
    if not hist or hist[-1].get("date") != today:
        hist.append(snap)
    new["history"], new["events"] = hist[-60:], events[-30:]
    return new


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
        meta[f["id"]] = track_changes(meta.get(f["id"]), m, now_ist().date().isoformat())
        got += 1
        print(f"[ok] {f['id']}: expense ratio {m['expense_ratio']}% (was {old if old is not None else f.get('ter_annual_pct')}), "
              f"{m['sub_category']}, benchmark {m['benchmark_name']}")
        time.sleep(1)
    if not got:
        sys.exit("[error] nothing fetched; file left as it was")
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    # the public view of the same thing, for the page
    since = min((h["date"] for f in cfg["funds"] for h in (meta.get(f["id"]) or {}).get("history", [])), default=None)
    write_json("fund_changes.json", {"generated_at": now_ist().isoformat(), "tracking_since": since, "funds": [
        {"id": f["id"], "name": f["name"], "lead": (meta.get(f["id"]) or {}).get("lead_manager"), "managers": (meta.get(f["id"]) or {}).get("managers", []),
         "aum_cr": (meta.get(f["id"]) or {}).get("aum_cr"), "expense_ratio": (meta.get(f["id"]) or {}).get("expense_ratio"), "events": (meta.get(f["id"]) or {}).get("events", [])}
        for f in cfg["funds"] if f["id"] in meta]})


if __name__ == "__main__":
    main()
