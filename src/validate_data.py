"""Never publish a broken data file. Run by every workflow just before it commits.

The page reads docs/data/*.json straight from GitHub Pages. One file that is not valid JSON (Python writes NaN for a
missing number, which is not JSON) makes the page silently drop that whole feature, as happened with the first
portfolio-history rebuild. This script parses every file strictly (NaN/Infinity rejected), checks the top-level
type for the files the page depends on, and then:

    python3 src/validate_data.py          report only; exit 1 if anything is wrong
    python3 src/validate_data.py --fix    put the last committed (known good) copy back for each bad file, warn, exit 0

so a bad run costs one stale file, not a broken page.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

DATA = pathlib.Path(__file__).resolve().parent.parent / "docs" / "data"
LISTS = {"history.json", "movers.json", "outlook_history.json"}          # everything else the page reads is an object


def _bad_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def check(path: pathlib.Path) -> str | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"), parse_constant=_bad_constant)
    except (ValueError, OSError) as exc:
        return str(exc)[:120]
    want = list if path.name in LISTS else dict
    if not isinstance(doc, want):
        return f"expected a JSON {'list' if want is list else 'object'}, got {type(doc).__name__}"
    if path.name == "portfolio_history.json":
        pts = doc.get("points")
        if not isinstance(pts, list) or any(not isinstance(p, dict) or not all(isinstance(p.get(k), (int, float)) for k in "vi") for p in pts):
            return "points must be a list of {d, v, i} with numeric v and i"
    return None


def main() -> int:
    fix = "--fix" in sys.argv
    bad = [(p, why) for p in sorted(DATA.glob("*.json")) if (why := check(p))]
    for p, why in bad:
        print(f"::warning file={p.relative_to(DATA.parent.parent)}::{p.name} is invalid ({why})" + (": restoring the last good copy" if fix else ""))
        if fix:
            r = subprocess.run(["git", "checkout", "HEAD", "--", str(p)], capture_output=True, text=True, cwd=DATA.parent.parent)
            if r.returncode:                                              # never committed before: drop it rather than publish it
                p.unlink(missing_ok=True)
    if not bad:
        print(f"[ok] {len(list(DATA.glob('*.json')))} data files are valid")
    return 0 if (fix or not bad) else 1


if __name__ == "__main__":
    sys.exit(main())
