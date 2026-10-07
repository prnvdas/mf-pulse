"""GIFT Nifty (formerly SGX Nifty): the Nifty futures that trade ~21 hours a day on NSE IX in GIFT City.

Its price outside Indian market hours is the best available hint of where the Nifty will open.
Writes data/gift.json with the near-month contract's last price. The dashboard turns that into
"x% from the Nifty's last close" itself, using the close from outlook.json.

Sources, in order:
  1. NSE IX's own JSON feed (official).
  2. The copy embedded in Groww's GIFT Nifty page (fallback, if the official feed is unreachable).

Fail-safe: if both fail, or the number is implausible, the previous gift.json is left untouched
(the page hides the row once it is old) and the script exits non-zero.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys

import requests

from common import now_ist, read_json, write_json

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
NSEIX = "https://www.nseix.com/api/market-rate?type=derivatives"
GROWW = "https://groww.in/indices/global-indices/sgx-nifty"


def _num(x) -> float | None:
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def from_nseix() -> dict | None:
    r = requests.get(NSEIX, headers={**UA, "Accept": "application/json"}, timeout=25)
    r.raise_for_status()
    rows = [x for x in (r.json().get("data") or [])
            if x.get("SYMBOL") == "NIFTY" and x.get("INSTRUMENTTYPE") == "FUTIDX"]
    best = None
    for x in rows:
        try:
            exp = dt.datetime.strptime(x["EXPIRYDATE"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        if exp < now_ist().date():
            continue
        if best is None or exp < best[0]:       # nearest expiry = the front-month contract
            best = (exp, x)
    if not best:
        return None
    exp, x = best
    last, chg = _num(x.get("LASTPRICE")), _num(x.get("DAYCHANGE_1", x.get("DAYCHANGE")))
    return {"source": "NSE IX", "contract": f"NIFTY {exp.strftime('%b %Y')} future", "expiry": exp.isoformat(),
            "last": last, "change": chg, "quote_time": x.get("TIMESTMP")}


def from_groww() -> dict | None:
    r = requests.get(GROWW, headers={**UA, "Accept": "text/html"}, timeout=25)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return None
    gi = json.loads(m.group(1))["props"]["pageProps"]["globalIndicesData"]
    for inst in gi.get("globalInstruments", []):
        if "SGX NIFTY" in str(inst.get("instrumentDetailDto", {}).get("symbol")):
            p = inst["livePriceDto"]
            ts = dt.datetime.fromtimestamp(p["tsInMillis"] / (1000 if p["tsInMillis"] > 1e11 else 1), dt.timezone.utc)
            return {"source": "Groww (copy of NSE IX)", "contract": "GIFT Nifty (front month)", "expiry": None,
                    "last": _num(p.get("value")), "change": _num(p.get("dayChange")),
                    "quote_time": ts.astimezone(now_ist().tzinfo).strftime("%d-%b-%Y %H:%M:%S")}
    return None


def main() -> None:
    got, errs = None, []
    for fn in (from_nseix, from_groww):
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001 -- any failure just moves on to the next source
            errs.append(f"{fn.__name__}: {exc}")
            continue
        if got and got["last"] and 5000 < got["last"] < 100000:
            break
        errs.append(f"{fn.__name__}: nothing usable")
        got = None
    if not got:
        print("[error] no GIFT Nifty price; leaving gift.json unchanged: " + " | ".join(errs), file=sys.stderr)
        sys.exit(1)

    last, chg = got["last"], got["change"]
    prev = last - chg if chg is not None else None
    out = {"fetched_at": now_ist().isoformat(), **got,
           "change_pct": round(chg / prev * 100, 3) if prev else None}
    old = read_json("gift.json", {})
    if {k: old.get(k) for k in ("last", "quote_time", "source")} == {k: out.get(k) for k in ("last", "quote_time", "source")}:
        print(f"[info] GIFT Nifty unchanged at {last:,.2f}")
        return
    write_json("gift.json", out)
    print(f"[ok] GIFT Nifty {last:,.2f} ({chg:+,.2f}, {out['change_pct']:+.2f}% on its own prior close) via {got['source']}, quote {got['quote_time']}")


if __name__ == "__main__":
    main()
