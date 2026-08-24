"""Shared plumbing: paths, config loading, AMFI NAV fetch."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
HOLDINGS_DIR = CONFIG / "holdings"
DATA_DIR = ROOT / "docs" / "data"
IST = ZoneInfo("Asia/Kolkata")

AMFI_NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def load_portfolio() -> dict:
    with open(CONFIG / "portfolio.yaml") as fh:
        return yaml.safe_load(fh)


def load_holdings(fund_id: str) -> dict:
    path = HOLDINGS_DIR / f"{fund_id}.yaml"
    if not path.exists():
        return {"fund_id": fund_id, "holdings": [], "cash_pct": 0.0, "as_of": None}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


# Storage backend. Local files by default; S3 when MFPULSE_S3_BUCKET is set,
# so the same code runs unchanged in a Lambda with no writable disk.
S3_BUCKET = os.environ.get("MFPULSE_S3_BUCKET")
S3_PREFIX = os.environ.get("MFPULSE_S3_PREFIX", "data/")


def _s3():
    import boto3  # imported lazily — not a dependency outside AWS

    return boto3.client("s3")


def read_json(name: str, default):
    if S3_BUCKET:
        try:
            body = _s3().get_object(Bucket=S3_BUCKET, Key=S3_PREFIX + name)["Body"]
            return json.load(body)
        except Exception:  # noqa: BLE001 — missing key on first run is normal
            return default

    path = DATA_DIR / name
    if not path.exists():
        return default
    try:
        with open(path) as fh:
            return json.load(fh)
    except json.JSONDecodeError:
        return default


def write_json(name: str, payload) -> None:
    blob = json.dumps(payload, indent=2, default=str)

    if S3_BUCKET:
        _s3().put_object(
            Bucket=S3_BUCKET,
            Key=S3_PREFIX + name,
            Body=blob.encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=15",
        )
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / name, "w") as fh:
        fh.write(blob)


def fetch_amfi_navs() -> dict[str, dict]:
    """Return {scheme_code: {name, nav, date}} from AMFI's daily text file.

    The file is pipe-delimited with section headers scattered through it,
    so anything that isn't a 6-field row is skipped.
    """
    resp = requests.get(AMFI_NAV_URL, timeout=30)
    resp.raise_for_status()

    navs: dict[str, dict] = {}
    for line in resp.text.splitlines():
        parts = line.split(";")
        if len(parts) != 8 or parts[0].strip() == "Scheme Code":
            continue
        code, _isin_g, _isin_r, scheme, plan, option, nav, date = (
            p.strip() for p in parts
        )
        try:
            nav_val = float(nav)
        except ValueError:
            continue  # 'N.A.' on non-trading days
        name = f"{scheme} {plan} {option}".strip()
        navs[code] = {"name": name, "nav": nav_val, "date": date}
    return navs


def is_market_window(cfg: dict, when: dt.datetime | None = None) -> bool:
    when = when or now_ist()
    if when.weekday() >= 5:
        return False
    open_h, open_m = (int(x) for x in cfg["market"]["open"].split(":"))
    close_h, close_m = (int(x) for x in cfg["market"]["close"].split(":"))
    start = when.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    end = when.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return start <= when <= end
