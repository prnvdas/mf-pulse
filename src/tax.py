"""Capital-gains view of the portfolio -> docs/data/tax.json.

What it shows (Indian equity-oriented mutual funds): the gain on every purchase "lot" split into long-term
(held more than 12 months) and short-term, the tax if everything were sold today, how much long-term gain can
still be booked tax-free this financial year, and which short-term lots are about to turn long-term.

Where the lots come from, in order of trust:
  1. config/transactions.csv (date,fund_id,amount): every purchase/withdrawal. Units are priced at the NAV of
     that date. If the amounts fall short of the fund's invested total, the remainder is an "opening" lot assumed
     to be older than 12 months.
  2. Otherwise an ESTIMATE: the monthly SIP at its allotment day for the last `sip_months_assumed` months
     (assuming the SIP amount was constant), plus one opening lot (long-term) for the rest of the units/cost.
     Only the last 12 months matter for the short/long split, which is why this is a usable approximation.

The rates live in config/portfolio.yaml (`tax:`). The defaults are the equity-fund rules as last known to the
author (STCG 20%, LTCG 12.5% above Rs 1.25 lakh a year, plus 4% cess); tax law changes, so CHECK THEM and edit
the config. This is an estimate for planning, not tax advice: it ignores surcharge, exit loads, and gains
or losses elsewhere.
"""
from __future__ import annotations

import datetime as dt
import sys

import pandas as pd

from analytics import fetch_nav_series, load_folios, load_transactions
from common import load_portfolio, now_ist, read_json, write_json

DEFAULT = {"stcg_pct": 20.0, "ltcg_pct": 12.5, "ltcg_exempt_inr": 125000.0, "cess_pct": 4.0, "long_term_days": 365,
           "realized_ltcg_this_fy": 0.0, "realized_stcg_this_fy": 0.0, "sip_months_assumed": 13}


def fy_bounds(today: dt.date) -> tuple[dt.date, dt.date]:
    start = dt.date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    return start, dt.date(start.year + 1, 3, 31)


def nav_on(nav: pd.Series, d: dt.date) -> float | None:
    """NAV applied to money allotted on `d`: that day's NAV, or the next published one."""
    ix = nav.index.searchsorted(pd.Timestamp(d))
    return float(nav.iloc[ix]) if ix < len(nav) else None


def months_back(today: dt.date, n: int, day: int) -> list[dt.date]:
    out = []
    for i in range(n):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        try:
            out.append(dt.date(y, m, min(day, 28)) if day > 28 else dt.date(y, m, day))
        except ValueError:
            continue
    return sorted(d for d in out if d <= today)


def build_lots(fid: str, cfg: dict, tx: dict, nav: pd.Series, units: float, cost: float, last_nav_date: dt.date,
               tcfg: dict, today: dt.date) -> tuple[list[dict], str, list[str]]:
    warns: list[str] = []
    lots: list[dict] = []
    if fid in tx and tx[fid]:
        src = "your transactions.csv"
        for d, amt in sorted(tx[fid]):
            if amt > 0:
                nv = nav_on(nav, d)
                if nv:
                    lots.append({"date": d, "units": amt / nv, "cost": amt, "nav": nv})
            else:   # withdrawal: FIFO redemption of the oldest units
                need = -amt / (nav_on(nav, d) or 1)
                for lot in lots:
                    take = min(lot["units"], need)
                    frac = take / lot["units"] if lot["units"] else 0
                    lot["cost"] -= lot["cost"] * frac
                    lot["units"] -= take
                    need -= take
                    if need <= 1e-9:
                        break
        lots = [x for x in lots if x["units"] > 1e-9]
        got_cost = sum(x["cost"] for x in lots)
        if cost and got_cost > cost * 1.01:
            warns.append("transactions.csv adds up to more than the invested total; ignoring it and estimating instead")
            lots, src = [], ""
    else:
        src = ""
    if not lots:
        src = "estimated from your SIP"
        sips = [s for s in cfg.get("sips", []) if s["fund_id"] == fid]
        months = int(tcfg["sip_months_assumed"])
        for s in sips:
            since = dt.date.fromisoformat(str(s["since"])) if s.get("since") else None
            for d in months_back(today, months, int(s["day_of_month"])):
                if d > last_nav_date or (since and d < since) or d < nav.index[0].date():
                    continue
                nv = nav_on(nav, d)
                if nv:
                    lots.append({"date": d, "units": float(s["amount"]) / nv, "cost": float(s["amount"]), "nav": nv})
    # whatever the lots don't explain is an older holding: long-term by assumption
    units_left, cost_left = units - sum(x["units"] for x in lots), cost - sum(x["cost"] for x in lots)
    inception = nav.index[0].date()
    if units_left > 1e-6 and cost_left > 0:
        if (today - inception).days <= tcfg["long_term_days"]:
            # a fund younger than 12 months cannot hold older units: date the rest at launch (short-term)
            lots.insert(0, {"date": inception, "units": units_left, "cost": cost_left, "nav": cost_left / units_left})
            warns.append(f"fund launched {inception.isoformat()}: units not explained by your SIP are dated at launch")
        else:
            lots.insert(0, {"date": None, "units": units_left, "cost": cost_left, "nav": cost_left / units_left, "opening": True})
    else:
        warns.append("the lots explain more units/cost than the fund holds, so the split is unreliable; add config/transactions.csv")
        if lots:
            k = units / sum(x["units"] for x in lots)
            for x in lots:
                x["units"] *= k
                x["cost"] = x["units"] * x["nav"]
    return lots, src, warns


def folio_lots(fid: str, folios: dict | None, today: dt.date) -> list[dict]:
    """For a fund with two folios (Groww now records new SIPs and lump sums as demat-mode units in a second
    folio): the older folio is long-term; the newer one mixes older and recent purchases, which we cannot
    split without the transaction history, so it is taxed here at the higher short-term rate to be safe.
    (The tax-if-sold figure is the same either way when the newer folio has little net gain; see `tax_if_newer_long`.)"""
    import math
    fl = [f for f in (folios or {}).get("funds", []) if f["fund_id"] == fid]
    if not fl or len(fl[0]["folios"]) < 2:
        return []
    def age(x):
        r = (x.get("xirr_pct") or 0) / 100
        return math.log(x["value"] / x["invested"]) / math.log(1 + r) if abs(r) > 1e-4 and x["value"] != x["invested"] and x["invested"] > 0 else 99
    out = []
    for k, x in enumerate(sorted(fl[0]["folios"], key=lambda x: -age(x))):
        older = k == 0
        out.append({"units": x["units"], "cost": x["invested"], "nav": x["invested"] / x["units"] if x["units"] else 0, "folio": x["label"],
                    "date": None, "opening": older, "force_term": None if older else "short", "kind": "older" if older else "newer",
                    "xirr_pct": x.get("xirr_pct")})
    return out


def main() -> None:
    cfg, ts = load_portfolio(), now_ist()
    today = ts.date()
    t = {**DEFAULT, **(cfg.get("tax") or {})}
    state = read_json("state.json", {})
    tx = load_transactions()
    folios = load_folios()
    rate_st, rate_lt, cess = t["stcg_pct"] / 100, t["ltcg_pct"] / 100, 1 + t["cess_pct"] / 100
    fy_start, fy_end = fy_bounds(today)

    funds, all_lots, nav_dates = [], [], []
    for f in cfg["funds"]:
        st = state.get(f["id"], {})
        units, nav_now, cost = float(st.get("units") or 0), float(st.get("last_nav") or 0), float(st.get("seed_invested") or 0)
        if not units or not nav_now:
            continue
        nav = fetch_nav_series(f["amfi_code"])
        if nav is None:
            print(f"[warn] {f['id']}: no NAV history; skipped", file=sys.stderr)
            continue
        try:
            last_date = dt.datetime.strptime(st["last_nav_date"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            last_date = today
        nav_dates.append(last_date)
        flots = folio_lots(f["id"], folios, today) if folios and not tx.get(f["id"]) else []
        if flots:
            lots, src, warns = flots, f"your Groww statement of {folios.get('as_on')}", []
            cost = sum(x["cost"] for x in lots)                 # the statement is exact: use its cost, not the rounded config
        else:
            lots, src, warns = build_lots(f["id"], cfg, tx, nav, units, cost, last_date, t, today)
        rows = []
        for l in lots:
            held = (today - l["date"]).days if l["date"] else None
            lt = (l["force_term"] == "long") if l.get("force_term") else (l.get("opening") or (held is not None and held > t["long_term_days"]))
            val = l["units"] * nav_now
            rows.append({"folio": l.get("folio"), "kind": l.get("kind"), "xirr_pct": l.get("xirr_pct"),
                         "date": l["date"].isoformat() if l["date"] else None, "units": round(l["units"], 4), "cost": round(l["cost"], 2),
                         "value": round(val, 2), "gain": round(val - l["cost"], 2), "term": "long" if lt else "short",
                         "opening": bool(l.get("opening")), "held_days": held,
                         "long_term_on": (l["date"] + dt.timedelta(days=t["long_term_days"] + 1)).isoformat() if l["date"] and not lt else None})
        lt_rows, st_rows = [r for r in rows if r["term"] == "long"], [r for r in rows if r["term"] == "short"]
        funds.append({"id": f["id"], "name": f["name"], "nav": nav_now, "source": src, "warnings": warns,
                      "value": round(sum(r["value"] for r in rows)), "cost": round(sum(r["cost"] for r in rows)),
                      "gain": round(sum(r["gain"] for r in rows)),
                      "lt_gain": round(sum(r["gain"] for r in lt_rows)), "st_gain": round(sum(r["gain"] for r in st_rows)),
                      "lt_value": round(sum(r["value"] for r in lt_rows)), "st_value": round(sum(r["value"] for r in st_rows)),
                      "lots": rows})
        all_lots += [(f["id"], r) for r in rows]
    if not funds:
        print("[error] nothing to compute; leaving tax.json unchanged", file=sys.stderr)
        sys.exit(1)

    tot = lambda k: sum(x[k] for x in funds)
    lt_gain, st_gain = tot("lt_gain"), tot("st_gain")
    # ---- tax if everything were sold today (with this year's already-realised gains and loss set-off)
    st_net = st_gain + t["realized_stcg_this_fy"]
    lt_net = lt_gain + t["realized_ltcg_this_fy"]
    if st_net < 0:                      # a short-term loss can be set off against long-term gains
        lt_net, st_net = lt_net + st_net, 0.0
    lt_net = max(0.0, lt_net)
    exempt_used = min(t["ltcg_exempt_inr"], lt_net)
    lt_taxable = lt_net - exempt_used
    tax_all = (lt_taxable * rate_lt + max(0.0, st_net) * rate_st) * cess
    newer_gain = sum(r["gain"] for _, r in all_lots if r.get("kind") == "newer")
    lt_alt, st_alt = lt_gain + newer_gain + t["realized_ltcg_this_fy"], st_gain - newer_gain + t["realized_stcg_this_fy"]
    if st_alt < 0:
        lt_alt, st_alt = lt_alt + st_alt, 0.0
    lt_alt = max(0.0, lt_alt)
    tax_alt = (max(0.0, lt_alt - t["ltcg_exempt_inr"]) * rate_lt + max(0.0, st_alt) * rate_st) * cess
    remaining = max(0.0, t["ltcg_exempt_inr"] - max(0.0, t["realized_ltcg_this_fy"]))

    # ---- tax-free harvest: book up to the remaining exemption of long-term gain, oldest lots first
    target = min(remaining, max(0.0, lt_gain))
    plan, got = [], 0.0
    by_ratio = sorted(funds, key=lambda x: -(x["lt_gain"] / x["lt_value"] if x["lt_value"] else 0))
    for fnd in by_ratio:
        if got >= target - 1:
            break
        units_sell = value_sell = gain_sell = 0.0
        for r in sorted((r for r in fnd["lots"] if r["term"] == "long" and r["gain"] > 0), key=lambda r: (r["date"] is not None, r["date"] or "")):
            if got >= target - 1:
                break
            frac = min(1.0, (target - got) / r["gain"])
            units_sell += r["units"] * frac
            value_sell += r["value"] * frac
            gain_sell += r["gain"] * frac
            got += r["gain"] * frac
        if gain_sell > 0:
            folio = next((r["folio"] for r in fnd["lots"] if r["term"] == "long" and r["gain"] > 0 and r.get("folio")), None)
            plan.append({"fund": fnd["name"], "folio": folio, "units": round(units_sell, 3), "value": round(value_sell), "gain": round(gain_sell)})
    harvest = {"remaining_exempt": round(remaining), "available_lt_gain": round(max(0.0, lt_gain)), "target_gain": round(target),
               "plan": plan, "sell_value": sum(p["value"] for p in plan),
               "future_tax_saved": round(target * rate_lt * cess)}

    # ---- short-term lots about to turn long-term (waiting saves the difference in rate)
    upcoming = []
    for fid, r in all_lots:
        if r["term"] == "short" and r["long_term_on"] and r["gain"] > 0 and not r.get("kind"):
            days = (dt.date.fromisoformat(r["long_term_on"]) - today).days
            if 0 < days <= 150:
                upcoming.append({"fund": next(x["name"] for x in funds if x["id"] == fid), "folio": r.get("folio"), "approx": r.get("approx"),
                                 "value": r["value"], "date": r["date"], "turns_long_on": r["long_term_on"],
                                 "days_left": days, "gain": r["gain"], "tax_saved_by_waiting": round(r["gain"] * (rate_st - rate_lt) * cess)})
    upcoming.sort(key=lambda u: u["days_left"])

    # lots sitting at a loss: booking one produces a capital loss that can offset gains (and carries forward if unused)
    loss_lots = sorted([{"fund": x["name"], "folio": r.get("folio"), "date": r["date"], "term": r["term"], "value": r["value"], "loss": r["gain"]}
                        for x in funds for r in x["lots"] if r["gain"] < -1000], key=lambda r: r["loss"])

    out = {"generated_at": ts.isoformat(), "as_of": max(nav_dates).isoformat(),
           "rates": {"stcg_pct": t["stcg_pct"], "ltcg_pct": t["ltcg_pct"], "ltcg_exempt_inr": t["ltcg_exempt_inr"], "cess_pct": t["cess_pct"],
                     "long_term_days": t["long_term_days"], "note": "As last known to the author; edit `tax:` in config/portfolio.yaml if the law changed."},
           "fy": {"start": fy_start.isoformat(), "end": fy_end.isoformat(), "days_left": (fy_end - today).days,
                  "realized_ltcg": t["realized_ltcg_this_fy"], "realized_stcg": t["realized_stcg_this_fy"]},
           "totals": {"value": tot("value"), "cost": tot("cost"), "gain": tot("gain"), "lt_gain": lt_gain, "st_gain": st_gain,
                      "lt_value": tot("lt_value"), "st_value": tot("st_value")},
           "sell_all": {"lt_gain_after_setoff": round(lt_net), "st_gain_after_setoff": round(max(0.0, st_net)), "exempt_used": round(exempt_used),
                        "lt_taxable": round(lt_taxable), "tax": round(tax_all), "tax_if_newer_long": round(tax_alt),
                        "effective_pct_of_gain": round(tax_all / tot("gain") * 100, 1) if tot("gain") > 0 else 0.0},
           "harvest": harvest, "upcoming": upcoming[:8], "funds": funds,
           "loss_lots": loss_lots, "statement_as_on": (folios or {}).get("as_on") if folios else None,
           "confidence": "statement" if any(x["source"].startswith("your Groww statement") for x in funds)
                         else "estimated" if any(x["source"].startswith("estimated") for x in funds) else "transactions"}
    write_json("tax.json", out)
    print(f"[ok] tax.json: gain ₹{tot('gain'):,} (long ₹{lt_gain:,} / short ₹{st_gain:,}); tax if all sold ₹{tax_all:,.0f}; "
          f"harvestable tax-free gain ₹{target:,.0f}; {len(upcoming)} lots turn long-term within 150 days; {out['confidence']}")


if __name__ == "__main__":
    main()
