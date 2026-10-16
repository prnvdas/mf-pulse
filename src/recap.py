"""The daily recap: one short message about the session that has just been graded.  ->  docs/data/recap.json

Written by the nightly reconcile job once the funds' NAVs are in. Says what your money did, how the estimate did, what moved it,
what the market did and what tomorrow's call is. The page shows it as a card; with `--issue` it is also opened as a GitHub
issue, which GitHub emails to you (the previous recap issue is closed so there is only ever one open). Switch the email off with
`profile.recap_issue: false` in config/portfolio.yaml.

    python src/recap.py [--issue]
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import subprocess

from common import load_portfolio, now_ist, read_json, write_json

MARK = "Recap"


def money(n: float, signed: bool = False) -> str:
    sign = ("+" if n >= 0 else "−") if signed else ("−" if n < 0 else "")
    a = abs(n)
    return f"{sign}₹{a / 1e7:.2f} Cr" if a >= 1e7 else f"{sign}₹{a / 1e5:.2f} L" if a >= 1e5 else f"{sign}₹{a:,.0f}"


def pct(x: float, d: int = 2) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):.{d}f}%"


def next_session(after: dt.date, holidays: list[dict]) -> tuple[dt.date, str | None]:
    """The next trading day after `after`, and the holiday that pushed it back (if any)."""
    hol = {h["date"]: h.get("name") for h in holidays if not h.get("weekend")}
    d, why = after + dt.timedelta(days=1), None
    while d.weekday() >= 5 or d.isoformat() in hol:
        if d.isoformat() in hol and why is None:
            why = hol[d.isoformat()]
        d += dt.timedelta(days=1)
    return d, why


def build() -> dict | None:
    hist = read_json("history.json", []) or []
    if not hist:
        return None
    day = max(r["date"] for r in hist)
    rows = [r for r in hist if r["date"] == day]
    cfg = load_portfolio()
    name = {f["id"]: f["name"].replace(" Direct Growth", "").replace(" Fund", "") for f in cfg["funds"]}
    rows = [r for r in rows if r["fund_id"] in name]
    if not rows:
        return None
    d = dt.date.fromisoformat(day)
    state, latest = read_json("state.json", {}), read_json("latest.json", {}) or {}
    value = sum((st.get("units") or 0) * (st.get("last_nav") or 0) for st in state.values())
    invested = sum(float(st.get("seed_invested") or 0) for st in state.values())
    moved = sum(r.get("actual_pl_rupees") or 0 for r in rows)
    est = sum(r.get("predicted_pl_rupees") or 0 for r in rows)
    have_est = all(r.get("predicted_pl_rupees") is not None and r.get("actual_pl_rupees") is not None for r in rows)
    band = math.sqrt(sum((r.get("band_rupees") or 0) ** 2 for r in rows)) if all(r.get("band_rupees") for r in rows) else None
    start = value - moved
    lines = [{"k": "result", "t": f"Your portfolio moved {money(moved, True)} ({pct(moved / start * 100 if start else 0)}) and is now worth {money(value)}; "
                                  f"your total gain is {money(value - invested, True)} ({pct((value / invested - 1) * 100 if invested else 0, 1)})."}]
    if have_est:
        inside = "inside" if band and abs(est - moved) <= band else "outside" if band else None
        lines.append({"k": "estimate", "t": f"I estimated {money(est, True)}: off by {money(abs(est - moved))}" + (f", {inside} the ±{money(band)} range I gave." if inside else ".")})
    lines.append({"k": "funds", "t": "By fund: " + "; ".join(f"{name[r['fund_id']]} {pct(r['actual_pct'])} ({money(r.get('actual_pl_rupees') or 0, True)})" for r in rows) + "."})
    movers = next((m for m in reversed(read_json("movers.json", []) or []) if isinstance(m, dict) and m.get("date") == day), None)
    # the market's moves for THIS session come from the day's own record (latest.json already belongs to the next session)
    mk = {m["name"]: m for m in ((movers or {}).get("market") or []) if isinstance(m, dict) and "move_pct" in m}
    if mk:
        lines.append({"k": "market", "t": "Market: " + ", ".join(f"{n} {pct(m['move_pct'])}" for n, m in mk.items() if n in ("Nifty 50", "Sensex", "Nifty Smallcap 250")) + "."})
    if movers:
        up = [g for g in movers.get("gainers", []) if g.get("impact", 0) > 0][:2]
        dn = [g for g in movers.get("laggards", []) if g.get("impact", 0) < 0][:2]
        bits = [f"{str(g['name']).removesuffix(' EQ').removesuffix(' LIMITED').title() if str(g['name']).isupper() else g['name']} {pct(g['move_pct'], 1)} ({money(g['impact'], True)})" for g in up + dn]
        if bits:
            lines.append({"k": "movers", "t": "What moved your money most: " + "; ".join(bits) + "."})
    out = read_json("outlook.json", {}) or {}
    # the outlook is written after each close for the session after it: take its own base date, not the graded date
    base = dt.date.fromisoformat(out["base_close_date"]) if out.get("base_close_date") else d
    nxt, why = next_session(base, (read_json("holidays.json", {}) or {}).get("holidays", []))
    call = out.get("call") or {}
    nxt_txt = nxt.strftime("%a %-d %b")
    if call.get("colour") and call.get("p_call") is not None:
        lines.append({"k": "tomorrow", "t": f"Next session ({nxt_txt}): the call is {call['colour'].upper()} at about {round(call['p_call'])}%"
                                            + (f" ({call.get('why')})" if call.get("why") else "") + ". It is a tilt, not a promise."})
    if why:
        lines.append({"k": "holiday", "t": f"The market is closed for {why} before then."})
    health = (latest.get("health") or {}).get("status")
    if health in ("warn", "bad"):
        lines.append({"k": "health", "t": "Heads-up: today's price data had a problem, so the estimate may have been less reliable than usual."})
    headline = f"{d.strftime('%a %-d %b')}: {money(moved, True)} ({pct(moved / start * 100 if start else 0)})"
    md = f"**{d.strftime('%A %-d %B %Y')}**\n\n" + "\n".join(f"- {l['t']}" for l in lines) + "\n\n_MF Pulse's automatic recap. Estimates are a nowcast from closing prices, not advice._\n"
    return {"date": day, "generated_at": now_ist().isoformat(), "headline": headline, "lines": lines, "markdown": md}


def open_issue(doc: dict) -> None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (repo and os.environ.get("GH_TOKEN")):
        print("[info] no GITHUB_REPOSITORY/GH_TOKEN: not opening an issue")
        return
    title = f"{MARK} · {doc['headline']}"
    run = lambda *a: subprocess.run(["gh", *a], capture_output=True, text=True)
    listed = run("issue", "list", "-R", repo, "--state", "all", "--search", f'in:title "{MARK} · {doc["headline"].split(":")[0]}"', "--json", "number", "-q", "length").stdout.strip()
    if listed and listed != "0":
        print("[info] this day's recap was already sent")
        return
    for n in run("issue", "list", "-R", repo, "--state", "open", "--search", f'in:title "{MARK} ·"', "--json", "number", "-q", ".[].number").stdout.split():
        run("issue", "close", n, "-R", repo, "--comment", "Superseded by the next recap.")
    run("issue", "create", "-R", repo, "--assignee", os.environ.get("GITHUB_REPOSITORY_OWNER", ""), "--title", title, "--body", doc["markdown"])
    print(f"[ok] recap issue opened: {title}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--issue", action="store_true", help="also open it as a GitHub issue (GitHub emails it)")
    args = ap.parse_args()
    doc = build()
    if not doc:
        print("[info] nothing graded yet: no recap")
        return
    write_json("recap.json", doc)
    print(f"[ok] recap for {doc['date']}: {doc['headline']}")
    for l in doc["lines"]:
        print("   - " + l["t"])
    if args.issue and (load_portfolio().get("profile") or {}).get("recap_issue", True):
        open_issue(doc)


if __name__ == "__main__":
    main()
