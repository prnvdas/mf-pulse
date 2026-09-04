"""Nightly job: pull the real NAV from AMFI, then grade today's estimate.

This is the part that makes the dashboard trustworthy. Every night it asks:
"what did I predict at 15:30, and what actually happened?" — and keeps score.
After a month you know the real error band instead of guessing at one.

Also back-solves units on first run, and applies SIP purchases.
"""

from __future__ import annotations

import datetime as dt

from common import (
    fetch_amfi_navs,
    load_portfolio,
    now_ist,
    read_json,
    write_json,
)


def _parse_amfi_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%d-%b-%Y").date()


def main() -> None:
    cfg = load_portfolio()
    ts = now_ist()
    today = ts.date().isoformat()

    navs = fetch_amfi_navs()
    print(f"[info] AMFI returned {len(navs)} schemes")

    state = read_json("state.json", {})
    latest = read_json("latest.json", None)
    history = read_json("history.json", [])

    updated_any = False

    for fund in cfg["funds"]:
        fid = fund["id"]
        code = fund.get("amfi_code")
        if code is None:
            print(f"[warn] {fid}: no amfi_code set, skipping")
            continue

        row = navs.get(str(code))
        if not row:
            print(f"[warn] {fid}: scheme code {code} not found in AMFI file")
            continue

        entry = state.setdefault(fid, {})
        prev_nav = entry.get("last_nav")
        new_nav = row["nav"]

        if entry.get("last_nav_date") == row["date"]:
            print(f"[info] {fid}: NAV unchanged ({row['date']}), no new trading day")
            continue

        # First run: back-solve units from the seed value in config.
        if not entry.get("units"):
            seed = float(fund.get("seed_value") or 0.0)
            entry["units"] = round(seed / new_nav, 4) if new_nav else 0.0
            print(f"[info] {fid}: seeded {entry['units']:.4f} units @ {new_nav}")

        # Apply the SIP around its allocation day. A strict "today == exact
        # day" match means a single missed run (a workflow gap, a weekend,
        # AMFI publishing late) silently loses that whole month's purchase
        # forever, with no way to recover it later. Instead: buy it the
        # first time reconcile.py runs on or within a few days after the
        # target day, and use `sip_applied_month` to guarantee it happens
        # exactly once per month even if reconcile.py runs daily across
        # that whole window.
        SIP_GRACE_DAYS = 4
        this_month = ts.strftime("%Y-%m")
        for sip in cfg.get("sips", []):
            if sip["fund_id"] != fid:
                continue
            day = sip["day_of_month"]
            due = day <= ts.day <= day + SIP_GRACE_DAYS
            if due and entry.get("sip_applied_month") != this_month:
                bought = sip["amount"] / new_nav
                entry["units"] = round(entry["units"] + bought, 4)
                entry["seed_invested"] = float(
                    entry.get("seed_invested", fund.get("seed_invested", 0))
                ) + sip["amount"]
                entry["sip_applied_month"] = this_month
                print(f"[info] {fid}: SIP +{bought:.4f} units (day {ts.day}, target {day})")

        # Grade the estimate we made earlier today. `stale` is a display-only
        # flag (estimate.py's post-close run sets it on every close estimate
        # to warn the UI it's not live) — it is NOT a signal that the number
        # underneath is untrustworthy, so grading must not gate on it. What
        # actually matters is whether `latest.json` holds an estimate from
        # the same trading day AMFI just published a NAV for.
        graded_day = _parse_amfi_date(row["date"])
        generated_at = latest.get("generated_at") if latest else None
        generated_day = (
            dt.datetime.fromisoformat(generated_at).date() if generated_at else None
        )
        if prev_nav and latest and generated_day == graded_day:
            actual_pct = (new_nav - prev_nav) / prev_nav * 100.0
            predicted = next(
                (f["nav_move_pct"] for f in latest["funds"] if f["id"] == fid), None
            )
            if predicted is not None:
                history.append(
                    {
                        "date": today,
                        "fund_id": fid,
                        "predicted_pct": predicted,
                        "actual_pct": round(actual_pct, 3),
                        "error_pct": round(abs(predicted - actual_pct), 3),
                        "direction_hit": (predicted >= 0) == (actual_pct >= 0),
                    }
                )
                print(
                    f"[score] {fid}: predicted {predicted:+.3f}% "
                    f"actual {actual_pct:+.3f}%"
                )

        entry["last_nav"] = new_nav
        entry["last_nav_date"] = row["date"]
        entry["amfi_name"] = row["name"]
        updated_any = True

    # Keep a rolling year; the git history holds the rest.
    cutoff = (ts.date() - dt.timedelta(days=365)).isoformat()
    history = [h for h in history if h["date"] >= cutoff]

    write_json("state.json", state)
    write_json("history.json", history)
    write_json("accuracy.json", summarise(history))

    if updated_any:
        print("[ok] reconciled")


def summarise(history: list[dict]) -> dict:
    """Rolling 30-sample accuracy, overall and per fund."""
    if not history:
        return {"samples": 0}

    def stats(rows: list[dict]) -> dict:
        rows = rows[-30:]
        n = len(rows)
        return {
            "samples": n,
            "mae_pct": round(sum(r["error_pct"] for r in rows) / n, 3),
            "direction_accuracy_pct": round(
                sum(1 for r in rows if r["direction_hit"]) / n * 100.0, 1
            ),
            "worst_error_pct": round(max(r["error_pct"] for r in rows), 3),
        }

    per_fund = {}
    for row in history:
        per_fund.setdefault(row["fund_id"], []).append(row)

    return {
        **stats(history),
        "per_fund": {fid: stats(rows) for fid, rows in per_fund.items()},
    }


if __name__ == "__main__":
    main()
