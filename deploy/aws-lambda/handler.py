"""Lambda entry point. One function, three jobs, picked by how it's invoked.

  Function URL GET /?symbols=A.NS,B.NS  -> price proxy (same contract as the
                                           Cloudflare Worker, so the dashboard
                                           can't tell them apart)
  EventBridge  {"mode": "estimate"}     -> run the estimator, write to S3
  EventBridge  {"mode": "reconcile"}    -> pull AMFI NAV, score the estimate

State lives in S3 (MFPULSE_S3_BUCKET) because Lambda has no durable disk.
Note the writer-of-record rule: if GitHub Actions is already reconciling, run
this one with SCHEDULE_RECONCILE disabled so the two don't fork your unit counts.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
NSE = "https://www.nseindia.com"
BULK_INDICES = ["NIFTY TOTAL MARKET", "NIFTY SMALLCAP 250", "NIFTY MIDCAP 150"]
INDEX_ALIASES = {
    "^NSEI": "NIFTY 50", "^CNXSC": "NIFTY SMALLCAP 100",
    "^NSEMDCP50": "NIFTY MIDCAP 100", "^CRSLDX": "NIFTY 500",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CORS = {
    "Access-Control-Allow-Origin": os.environ.get("MFPULSE_ALLOW_ORIGIN", "*"),
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Content-Type": "application/json",
}


def _get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), dict(r.headers)


# --- NSE bulk -------------------------------------------------------------

def nse_quotes():
    """Cookie handshake, then bulk index constituents. Raises if blocked."""
    req = urllib.request.Request(NSE + "/", headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=12) as r:
        cookies = "; ".join(
            c.split(";")[0] for c in r.headers.get_all("Set-Cookie") or [])
    if not cookies:
        raise RuntimeError("nse: no session cookie")

    hdr = {"Cookie": cookies, "Accept": "application/json",
           "Referer": NSE + "/market-data/live-equity-market"}
    equities, indices = {}, {}

    for name in BULK_INDICES:
        try:
            j, _ = _get(f"{NSE}/api/equity-stockIndices?index="
                        f"{urllib.parse.quote(name)}", hdr)
        except Exception:
            continue
        for row in j.get("data", []):
            sym = row.get("symbol")
            if not sym or sym == name or row.get("lastPrice") is None:
                continue
            if sym in equities:
                continue
            pct = row.get("pChange")
            if pct is None:
                continue
            equities[sym] = {"last": float(row["lastPrice"]),
                             "prev": float(row.get("previousClose") or 0),
                             "pct": float(pct), "src": "nse"}
    try:
        j, _ = _get(NSE + "/api/allIndices", hdr)
        for row in j.get("data", []):
            if row.get("index") and row.get("percentChange") is not None:
                indices[row["index"]] = {
                    "last": float(row.get("last") or 0),
                    "prev": float(row.get("previousClose") or 0),
                    "pct": float(row["percentChange"]), "src": "nse"}
    except Exception:
        pass

    return equities, indices


def yahoo_quote(symbol):
    try:
        j, _ = _get(f"{YAHOO}{urllib.parse.quote(symbol)}?range=5d&interval=1d")
        m = j["chart"]["result"][0]["meta"]
        last = m.get("regularMarketPrice")
        prev = m.get("chartPreviousClose") or m.get("previousClose")
        if not last or not prev:
            return None
        return {"last": last, "prev": prev,
                "pct": (last - prev) / prev * 100, "src": "yahoo"}
    except Exception:
        return None


def price_proxy(symbols):
    quotes, nse_error = {}, None
    try:
        equities, indices = nse_quotes()
        for t in symbols:
            hit = (indices.get(INDEX_ALIASES.get(t, "")) if t.startswith("^")
                   else equities.get(t[:-3] if t.endswith(".NS") else t))
            if hit:
                quotes[t] = hit
    except Exception as exc:  # noqa: BLE001
        nse_error = str(exc)

    from concurrent.futures import ThreadPoolExecutor

    missing = [s for s in symbols if s not in quotes]
    if missing:
        with ThreadPoolExecutor(max_workers=12) as pool:
            for sym, q in zip(missing, pool.map(yahoo_quote, missing)):
                if q:
                    quotes[sym] = q

    counts = {"nse": 0, "yahoo": 0}
    for q in quotes.values():
        counts[q["src"]] += 1

    return {"quotes": quotes, "resolved": len(quotes),
            "requested": len(symbols), "sources": counts, "nse_error": nse_error}


# --- handler --------------------------------------------------------------

def handler(event, context):
    # EventBridge invocation
    mode = (event or {}).get("mode")
    if mode in ("estimate", "reconcile"):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
        if mode == "estimate":
            import estimate
            sys.argv = ["estimate", "--force"]
            estimate.main()
        else:
            import reconcile
            reconcile.main()
        return {"ok": True, "mode": mode}

    # Function URL invocation
    method = (event.get("requestContext", {}).get("http", {}).get("method", "GET"))
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    raw = (event.get("queryStringParameters") or {}).get("symbols", "")
    symbols = [s.strip() for s in raw.split(",") if s.strip()]

    if not symbols:
        return {"statusCode": 400, "headers": CORS,
                "body": json.dumps({"error": "pass ?symbols=A.NS,B.NS"})}
    if len(symbols) > 150:
        return {"statusCode": 400, "headers": CORS,
                "body": json.dumps({"error": "max 150 symbols per call"})}

    return {"statusCode": 200,
            "headers": {**CORS, "Cache-Control": "public, max-age=30"},
            "body": json.dumps(price_proxy(symbols))}
