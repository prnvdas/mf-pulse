"""Sync invested amount and units from a Groww / IndMoney screenshot.

Drop a screenshot of the mutual-fund holdings page into `screenshots/` and the
`sync-screenshot` workflow runs this. It reads the image with an offline OCR
engine (nothing leaves the runner), finds each configured fund, and extracts
Invested, Current Value and Gain/Loss.

OCR is not trusted on its own. The rupee sign is routinely misread as a digit
("₹41.25L" -> "741.25L") and gain figures get glued to the percentage, so each
number is expanded into its plausible readings and a reading set is accepted
only if the page's own arithmetic agrees:

    current value = invested + gain      and      gain / invested = the printed %

If that is ambiguous, if a figure is wildly off the current state (wrong account,
wrong screen), or if nothing validates, NOTHING is changed and the reason is
printed. A rounded display ("₹1.8L") limits precision to what the app shows.

    python src/sync_screenshot.py [image] [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import itertools
import pathlib
import re
import sys

from common import ROOT, load_portfolio, now_ist, read_json, write_json

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MULT = {"cr": 1e7, "l": 1e5, "k": 1e3}

SUFFIX_RE = re.compile(r"([-−–]?)\s*(\d[\d,]*(?:\.\d+)?)\s*(Cr|L|K)", re.I)
FULL_RE = re.compile(r"([-−–]?)\s*(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?)")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

LABELS = {
    "invested": re.compile(r"invested|investment", re.I),
    "current": re.compile(r"current|market value|^value", re.I),
    "gain": re.compile(r"gain|loss|returns?\b|p\s*&\s*l|profit", re.I),
}


def squash(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


# --- number readings ------------------------------------------------------------

def readings(token: str) -> list[tuple[float, float]]:
    """Plausible (value, rounding_half_unit) readings of the first amount in a token."""
    out: list[tuple[float, float]] = []
    m = SUFFIX_RE.search(token)
    if m:
        sign = -1 if m.group(1) else 1
        num, mult = m.group(2).replace(",", ""), MULT[m.group(3).lower()]
        dec = len(num.split(".")[1]) if "." in num else 0
        half = 0.5 * 10 ** (-dec) * mult
        out.append((sign * float(num) * mult, half))
        ip = num.split(".")[0]
        if len(ip) >= 2:   # the rupee sign may have been read as a leading digit
            out.append((sign * float(num[1:]) * mult, half))
        return out
    m = FULL_RE.search(token)
    if m:
        sign = -1 if m.group(1) else 1
        digits = m.group(2).replace(",", "")
        out.append((sign * float(digits), 0.5))
        if len(digits.split(".")[0]) >= 6:
            out.append((sign * float(digits[1:]), 0.5))
    return out


def solve(inv: list, cur: list, gain: list, pct: float | None) -> tuple[dict | None, str]:
    """Pick the one (invested, value, gain) reading set the page's own arithmetic supports."""
    if not inv or not cur:
        return None, "could not find both an Invested and a Current Value figure"
    scored = []
    for (I, rI), (V, rV) in itertools.product(inv, cur):
        if I <= 0 or V <= 0:
            continue
        for G, rG in (gain or [(None, 0.0)]):
            norms = []
            if G is not None:
                norms.append(abs((V - I) - G) / max(rI + rV + rG, 1.0))
            if pct is not None:
                tol = ((rI + rV) / I) * 100 + 0.06
                signs = [1 if G is None or G >= 0 else -1] if G is not None else [1, -1]
                norms.append(min(abs((V - I) / I * 100 - s * pct) for s in signs) / tol)
            if not norms:
                continue
            worst = max(norms)
            if worst <= 1.25:
                scored.append((worst, I, V, G))
    if not scored:
        return None, "the numbers read from the image are not consistent with each other " \
                     "(value ≠ invested + gain, or the % doesn't match)"
    scored.sort(key=lambda x: x[0])
    best = scored[0]
    rivals = [s for s in scored[1:] if abs(s[2] / best[2] - 1) > 0.01 or abs(s[1] / best[1] - 1) > 0.01]
    if rivals and rivals[0][0] < best[0] * 3 + 0.2:
        return None, "the image supports more than one reading of the numbers; not guessing"
    _, I, V, G = best
    if G is not None:
        V = I + G          # the gain is usually printed more precisely than a rounded "₹1.8L"
    return {"invested": round(I), "value": round(V), "gain": None if G is None else round(G)}, "ok"


# --- OCR + layout -----------------------------------------------------------------

def ocr_items(path: pathlib.Path) -> list[dict]:
    from rapidocr_onnxruntime import RapidOCR   # imported late: only this workflow installs it
    res, _ = RapidOCR()(str(path))
    items = []
    for box, text, conf in res or []:
        items.append({"t": text, "x": sum(p[0] for p in box) / 4, "y": sum(p[1] for p in box) / 4,
                      "x0": min(p[0] for p in box), "conf": float(conf)})
    return items


def parse(items: list[dict], funds: list[dict]) -> tuple[dict, list[str]]:
    notes: list[str] = []
    heads = []
    for f in funds:
        want = squash(f["name"].replace("Direct Growth", "").replace("Fund", ""))
        for it in items:
            if want and want in squash(it["t"]):
                heads.append((it["y"], f["id"]))
                break
    heads.sort()
    found: dict = {}
    for i, (y0, fid) in enumerate(heads):
        y1 = heads[i + 1][0] if i + 1 < len(heads) else y0 + 260
        block = [it for it in items if y0 < it["y"] < y1 and it["y"] - y0 < 260]
        labels = [(role, it) for it in block for role, rx in LABELS.items()
                  if rx.search(it["t"]) and not (readings(it["t"]) and role != "gain")]
        vals = {"invested": [], "current": [], "gain": []}
        pct = None
        for it in block:
            if not readings(it["t"]):
                continue
            near = min(labels, key=lambda lr: abs(lr[1]["x"] - it["x"]) + 1.3 * abs(lr[1]["y"] - it["y"]),
                       default=None)
            if not near:
                continue
            vals[near[0]] = readings(it["t"])
            if near[0] == "gain":
                m = PCT_RE.findall(it["t"])
                pct = float(m[-1]) if m else pct
        got, why = solve(vals["invested"], vals["current"], vals["gain"], pct)
        if got:
            # Groww prints an XIRR on holdings pages; keep it if the label is there
            xp = None
            for it in block:
                if not re.search(r"xirr", it["t"], re.I):
                    continue
                text, m = it["t"], PCT_RE.search(it["t"])
                if not m:   # the % may be a separate text region just beside/below the label
                    near = sorted((o for o in block if o is not it and PCT_RE.search(o["t"])
                                   and abs(o["y"] - it["y"]) < 60),
                                  key=lambda o: abs(o["x"] - it["x"]) + abs(o["y"] - it["y"]))
                    if near:
                        text, m = near[0]["t"], PCT_RE.search(near[0]["t"])
                if m:
                    v = float(m.group(1))
                    xp = -v if re.search(r"[-−–]\s*" + re.escape(m.group(1)), text) else v
            if xp is not None and -60 <= xp <= 150:
                got["xirr"] = xp
            found[fid] = got
        else:
            notes.append(f"{fid}: {why}")
    for f in funds:
        if f["id"] not in found and not any(n.startswith(f["id"]) for n in notes):
            notes.append(f"{f['id']}: fund name not found in the image")
    return found, notes


# --- apply ------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="skip the plausibility guard against current state")
    args = ap.parse_args()

    shots = ROOT / "screenshots"
    if args.image:
        path = pathlib.Path(args.image)
    else:
        cands = [p for p in shots.glob("*") if p.suffix.lower() in IMG_EXT] if shots.exists() else []
        if not cands:
            print("[info] no screenshot found in screenshots/ — nothing to do")
            return
        path = max(cands, key=lambda p: p.stat().st_mtime)
    print(f"[info] reading {path.name}")

    cfg = load_portfolio()
    state = read_json("state.json", {})
    items = ocr_items(path)
    print(f"[info] OCR found {len(items)} text regions")
    found, notes = parse(items, cfg["funds"])

    ts = now_ist()
    sips = {s["fund_id"]: s["day_of_month"] for s in cfg.get("sips", [])}
    changes, rejected = [], list(notes)
    for fid, got in found.items():
        st = state.get(fid, {})
        nav = st.get("last_nav")
        old_val = (st.get("units") or 0) * (nav or 0)
        old_inv = st.get("seed_invested") or next((f.get("seed_invested") for f in cfg["funds"] if f["id"] == fid), 0)
        if not nav:
            rejected.append(f"{fid}: no last_nav in state yet — run reconcile first")
            continue
        if not args.force and old_val and old_inv and not (
                0.65 <= got["value"] / old_val <= 1.35 and 0.65 <= got["invested"] / old_inv <= 1.45):
            rejected.append(f"{fid}: ₹{got['invested']:,} invested / ₹{got['value']:,} value is far from the current "
                            f"₹{old_inv:,.0f} / ₹{old_val:,.0f} — wrong screen or misread? (use --force to override)")
            continue
        changes.append((fid, st, nav, old_inv, old_val, got))

    for fid, st, nav, oi, ov, got in changes:
        print(f"[ok]   {fid}: invested ₹{oi:,.0f} -> ₹{got['invested']:,}   value ₹{ov:,.0f} -> ₹{got['value']:,}"
              + ("" if got["gain"] is None else f"   (gain ₹{got['gain']:,})"))
    for r in rejected:
        print(f"[skip] {r}", file=sys.stderr)

    if not changes:
        print("[error] nothing validated — no data was changed", file=sys.stderr)
        sys.exit(1)
    if args.dry_run:
        print("[info] dry run — nothing written")
        return

    for fid, st, nav, oi, ov, got in changes:
        entry = state.setdefault(fid, {})
        entry["units"] = round(got["value"] / nav, 4)
        entry["seed_invested"] = float(got["invested"])
        entry["last_synced"] = ts.date().isoformat()
        if got.get("xirr") is not None:
            entry["xirr_pct"], entry["xirr_date"] = got["xirr"], ts.date().isoformat()
        # A screenshot taken after this month's SIP allocation day already includes that SIP;
        # before it, leave the flag alone so reconcile.py still adds it. Prevents double counting.
        if ts.day > sips.get(fid, 3):
            entry["sip_applied_month"] = ts.strftime("%Y-%m")
    write_json("state.json", state)
    print(f"[ok] state.json updated for {len(changes)} fund(s); {len(rejected)} skipped")
    if rejected:
        print("[warn] some funds were skipped; the rest were applied", file=sys.stderr)


if __name__ == "__main__":
    main()
