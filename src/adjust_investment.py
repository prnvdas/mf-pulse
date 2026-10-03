"""Manual correction: record money moved in or out of a fund outside the
automated monthly SIP -- a one-off top-up, a withdrawal, or any change the
nightly reconcile.py has no way to discover on its own (there's no
brokerage or bank integration here; it only ever sees what AMFI publishes
and what's configured in portfolio.yaml).

Meant to be run via the "adjust" GitHub Actions workflow, so this is a
button on the Actions tab (workflow_dispatch inputs), not something that
needs a terminal -- but it's a plain script, so `python src/adjust_investment.py
--fund axis-small-cap --amount 20000` works locally too.

Buys units at today's last known NAV (whatever reconcile.py last stored),
same as a SIP purchase would. Use a negative --amount for a withdrawal.
"""

from __future__ import annotations

import argparse
import sys

from common import load_portfolio, now_ist, read_json, write_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fund", required=True, help="fund id, e.g. axis-small-cap")
    parser.add_argument(
        "--amount", type=float, required=True,
        help="rupees added (negative for a withdrawal)",
    )
    parser.add_argument("--note", default="", help="optional note, for the commit message")
    args = parser.parse_args()

    cfg = load_portfolio()
    fund = next((f for f in cfg["funds"] if f["id"] == args.fund), None)
    if fund is None:
        valid = ", ".join(f["id"] for f in cfg["funds"])
        print(f"[error] unknown fund id {args.fund!r} — valid ids: {valid}", file=sys.stderr)
        sys.exit(1)

    state = read_json("state.json", {})
    entry = state.setdefault(args.fund, {})
    last_nav = entry.get("last_nav")
    if not last_nav:
        print(
            f"[error] {args.fund}: no last_nav in state.json yet — "
            "run reconcile.py at least once before adjusting",
            file=sys.stderr,
        )
        sys.exit(1)

    bought_units = args.amount / last_nav
    entry["units"] = round((entry.get("units") or 0.0) + bought_units, 4)
    entry["seed_invested"] = round(
        float(entry.get("seed_invested", fund.get("seed_invested", 0))) + args.amount, 2
    )
    entry["last_adjusted"] = now_ist().date().isoformat()

    write_json("state.json", state)

    sign = "+" if args.amount >= 0 else ""
    print(
        f"[ok] {args.fund}: {sign}₹{args.amount:,.2f} @ NAV {last_nav} "
        f"-> {sign}{bought_units:.4f} units"
    )
    print(f"     units now {entry['units']:.4f}, invested now ₹{entry['seed_invested']:,.2f}")
    if args.note:
        print(f"     note: {args.note}")


if __name__ == "__main__":
    main()
