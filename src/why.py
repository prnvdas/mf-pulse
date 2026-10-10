"""Why did the market move? Built from measured numbers and real headlines, not canned sentences.

Four layers, each independent (one failing source just removes its section):

  1. SECTORS and BREADTH from NSE's own index feed (every sectoral index's move, advances/declines).
  2. MONEY FLOWS: FII/FPI and DII net buying or selling for the session (NSE's provisional figures).
  3. GLOBAL AND MACRO CUES with their actual numbers (S&P 500 overnight, Nikkei, Hang Seng, Brent,
     USD/INR, US 10-year yield, India VIX). Each is flagged "notable" only if the move was large
     compared with that series' own recent swings.
  4. REAL NEWS: for each of the user's biggest movers the freshest headlines about that company, and
     the market-wrap headlines of the session. Always shown as the original headline with source,
     time and link, so the reader sees what was reported rather than a summary of it.

Nothing here asserts a cause the data does not show. The summary is assembled only from the
measured items; headlines are shown as "reported", never as proof.
"""
from __future__ import annotations

import datetime as dt
import re
import time

import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}

# sectoral indices worth showing, in NSE's own naming
SECTORS = {"NIFTY BANK": "Banks", "NIFTY PRIVATE BANK": "Private banks", "NIFTY PSU BANK": "PSU banks",
           "NIFTY FINANCIAL SERVICES": "Financial services", "NIFTY IT": "IT", "NIFTY AUTO": "Auto",
           "NIFTY FMCG": "FMCG", "NIFTY METAL": "Metals", "NIFTY PHARMA": "Pharma", "NIFTY REALTY": "Realty",
           "NIFTY OIL & GAS": "Oil & gas", "NIFTY ENERGY": "Energy", "NIFTY MEDIA": "Media",
           "NIFTY CONSUMER DURABLES": "Consumer durables", "NIFTY INFRASTRUCTURE": "Infrastructure"}
BROAD = {"NIFTY 50": "Nifty 50", "NIFTY NEXT 50": "Nifty Next 50", "NIFTY MIDCAP 100": "Midcap 100", "NIFTY SMALLCAP 100": "Smallcap 100"}

# Yahoo series for the macro layer: key -> (label, ticker, which bar: "same" day or "prior" US session, unit)
MACRO = [
    ("S&P 500 (overnight)", "^GSPC", "prior", ""),
    ("Nikkei 225", "^N225", "same", ""),
    ("Hang Seng", "^HSI", "same", ""),
    ("Brent crude", "BZ=F", "same", "$"),
    ("USD/INR", "INR=X", "same", "₹"),
    ("US 10-yr yield", "^TNX", "prior", "%"),
    ("India VIX", "^INDIAVIX", "same", ""),
]

GOOD_SOURCES = {"the economic times", "economic times", "moneycontrol", "mint", "livemint", "business standard", "businessline",
                "the hindu businessline", "cnbc-tv18", "cnbctv18", "ndtv profit", "financial express", "the financial express",
                "reuters", "bloomberg", "business today", "the times of india", "hindustan times", "zee business", "the hindu",
                "bfsi.economictimes.indiatimes.com", "et markets", "et now", "news18", "firstpost", "the new indian express", "pti"}
SPAM = re.compile(r"prediction|price target for|should you buy|stocks to buy|buy or sell|horoscope|astro|"
                  r"what does its .* mean|compliance certificate|reg\.? ?74|certificate|technical (view|analysis)|stock radar|top picks|"
                  r"bank holiday|holiday|open or closed|trading window|share price and|dividend history|shareholding pattern|navratri|samvat|muhurat picks", re.I)
SPAM_SOURCES = {"kalkine india", "univest", "tradingview", "psu connect", "marketscreener"}
DOWN_W = re.compile(r"\b(fall|falls|fell|falling|drop|drops|dropped|slump|slumps|tumble|tumbles|plunge|plunges|crash|crashes|sink|sinks|slide|slides|"
                    r"decline|declines|cracking|cracks|sell-?off|weak|lower|tank|tanks|cut|cuts|slashes|slashed|downgrade)\b", re.I)
UP_W = re.compile(r"\b(rise|rises|rose|rising|rally|rallies|rallied|jump|jumps|jumped|surge|surges|surged|gain|gains|gained|climb|climbs|soar|soars|"
                  r"higher|upgrade|bounce|bounces|rebound|rebounds|bulls|record high|strike back)\b", re.I)


def consistent(title: str, direction: str) -> bool:
    """Reject a headline that argues the opposite of what happened (e.g. 'why is Sensex falling' on a rally day)."""
    if direction not in ("up", "down"):
        return True
    d, u = bool(DOWN_W.search(title)), bool(UP_W.search(title))
    return not (d and not u) if direction == "up" else not (u and not d)
LEGAL_SUFFIX = re.compile(r"\b(ltd|limited|pvt|private|corporation|corp|co|inc|of india|india)\b\.?", re.I)


# ---------------------------------------------------------------------------------- NSE
def nse_session() -> requests.Session | None:
    """NSE needs a cookie from its home page first. Returns None if NSE can't be reached (e.g. blocked)."""
    page = {"User-Agent": UA["User-Agent"], "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"}
    for attempt in range(3):               # NSE's bot protection is flaky: a plain browser-like first request, a few tries
        s = requests.Session()
        try:
            r = s.get("https://www.nseindia.com/", headers=page, timeout=20)
            if r.status_code == 200:
                s.headers.update({**UA, "Accept": "application/json, text/plain, */*", "Referer": "https://www.nseindia.com/"})
                time.sleep(0.8)
                return s
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2 * (attempt + 1))
    return None


def nse_sectors(s: requests.Session, session_date: dt.date) -> dict | None:
    try:
        r = s.get("https://www.nseindia.com/api/allIndices", timeout=25)
        r.raise_for_status()
        j = r.json()
    except Exception:  # noqa: BLE001
        return None
    stamp = str(j.get("timestamp") or "")
    try:
        when = dt.datetime.strptime(stamp[:11], "%d-%b-%Y").date()
    except ValueError:
        when = None
    if when and when != session_date:       # NSE shows another day's close: not this session's story
        return None
    by = {x["index"]: x for x in j.get("data", [])}
    def row(k, label):
        x = by.get(k)
        if not x or x.get("percentChange") is None:
            return None
        return {"name": label, "pct": round(float(x["percentChange"]), 2), "last": float(x["last"]),
                "up": int(x["advances"]) if str(x.get("advances") or "").isdigit() else None,
                "down": int(x["declines"]) if str(x.get("declines") or "").isdigit() else None}
    sectors = [r for k, l in SECTORS.items() if (r := row(k, l))]
    broad = [r for k, l in BROAD.items() if (r := row(k, l))]
    n50 = next((r for r in broad if r["name"] == "Nifty 50"), None)
    return {"sectors": sorted(sectors, key=lambda r: -r["pct"]), "broad": broad,
            "breadth": {"up": n50["up"], "down": n50["down"]} if n50 and n50["up"] is not None else None}


def nse_flows(s: requests.Session, session_date: dt.date) -> dict | None:
    try:
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=25)
        r.raise_for_status()
        rows = r.json()
    except Exception:  # noqa: BLE001
        return None
    out = {}
    for x in rows:
        try:
            d = dt.datetime.strptime(x["date"], "%d-%b-%Y").date()
            net = float(str(x["netValue"]).replace(",", ""))
        except (KeyError, ValueError):
            continue
        if d != session_date:
            continue
        cat = "fii" if x.get("category", "").upper().startswith("FII") else "dii" if x.get("category", "").upper().startswith("DII") else None
        if cat:
            out[cat] = round(net, 0)
    return {"date": session_date.isoformat(), **out} if "fii" in out or "dii" in out else None


def _num(x) -> float | None:
    try:
        return float(str(x).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


def flows_moneycontrol(session_date: dt.date) -> dict | None:
    """Same provisional FII/DII cash figures NSE publishes, from Moneycontrol's page data (reachable when NSE blocks us)."""
    import json
    r = requests.get("https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/index.php", headers=UA, timeout=25)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return None
    rows = json.loads(m.group(1))["props"]["pageProps"]["FiiDiiData"]["fiiDiiData"]
    for x in rows:
        if x.get("date") == session_date.isoformat():
            fii, dii = _num(x.get("fiiCM")), _num(x.get("diiCM"))
            if fii is None and dii is None:
                return None
            out = {"date": session_date.isoformat(), "source": "Moneycontrol"}
            if fii is not None:
                out["fii"] = round(fii, 0)
            if dii is not None:
                out["dii"] = round(dii, 0)
            if _num(x.get("fiiIdxFut")) is not None:
                out["fii_index_futures"] = round(_num(x["fiiIdxFut"]), 0)
            return out
    return None


def sectors_archive(session_date: dt.date) -> dict | None:
    """NSE's static end-of-day file with every index's close and % change (published after the close)."""
    import csv, io
    url = f"https://archives.nseindia.com/content/indices/ind_close_all_{session_date.strftime('%d%m%Y')}.csv"
    r = requests.get(url, headers=UA, timeout=25)
    if r.status_code != 200:
        return None
    by = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            by[row["Index Name"].strip().upper()] = (float(row["Change(%)"]), float(row["Closing Index Value"]))
        except (KeyError, ValueError):
            continue

    def pick(table: dict) -> list[dict]:
        return [{"name": label, "pct": round(by[k][0], 2), "last": by[k][1], "up": None, "down": None} for k, label in table.items() if k in by]
    sectors, broad = pick(SECTORS), pick(BROAD)
    return {"sectors": sorted(sectors, key=lambda r: -r["pct"]), "broad": broad, "breadth": None} if sectors else None


def cached_flows(day: dt.date) -> dict | None:
    from common import read_json
    return ((read_json("flows.json", {}) or {}).get("days") or {}).get(day.isoformat())


def save_flows(f: dict) -> None:
    from common import read_json, write_json
    cur = read_json("flows.json", {}) or {}
    days = cur.get("days") or {}
    days[f["date"]] = {k: v for k, v in f.items() if k != "date"}
    write_json("flows.json", {"days": dict(sorted(days.items())[-20:])})


# -------------------------------------------------------------------------------- Yahoo
def yahoo_macro(session_date: dt.date) -> list[dict]:
    import numpy as np
    import yfinance as yf
    tickers = [t for _, t, _, _ in MACRO]
    try:
        d = yf.download(tickers, period="3mo", interval="1d", auto_adjust=False, progress=False, group_by="ticker", timeout=40)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for label, t, which, unit in MACRO:
        try:
            c = d[t]["Close"].dropna()
        except KeyError:
            continue
        ret = c.pct_change().dropna()
        if len(c) < 25:
            continue
        # the bar to use: same calendar day as the Indian session, or the last US bar before it
        idx = [i for i, ts in enumerate(c.index) if (ts.date() == session_date if which == "same" else ts.date() < session_date)]
        if not idx:
            continue
        i = idx[-1]
        if which == "same" and c.index[i].date() != session_date:
            continue
        if i < 1:
            continue
        move = float(c.iloc[i] / c.iloc[i - 1] - 1) * 100
        sd = float(ret.iloc[max(0, i - 21):i].std()) * 100 or 1e-9
        out.append({"name": label, "level": round(float(c.iloc[i]), 2), "pct": round(move, 2), "unit": unit,
                    "notable": bool(abs(move) >= 1.3 * sd), "z": round(move / sd, 1), "date": c.index[i].date().isoformat()})
    return out


# ---------------------------------------------------------------------------------- news
def _gn(query: str) -> list[dict]:
    from news import GN, fetch_feed          # lazy: news.py imports this module
    return fetch_feed("Google News", GN + requests.utils.quote(query))


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", LEGAL_SUFFIX.sub(" ", name)).strip(" .,")


def _good(row: dict) -> bool:
    return not SPAM.search(row["title"]) and len(row["title"]) >= 25 and row["source"].lower() not in SPAM_SOURCES


def _pack(r: dict) -> dict:
    return {"title": r["title"], "source": r["source"], "link": r["link"],
            "published": r["published"].astimezone(dt.timezone.utc).isoformat()}


def company_news(name: str, start: dt.datetime, end: dt.datetime, move_pct: float = 0.0, limit: int = 2) -> list[dict]:
    """The freshest real headlines naming this company inside the window, trusted sources first."""
    short = _clean_name(name)
    if len(short) < 3:
        return []
    key = re.compile(r"\b" + re.escape(short.split(" ")[0] if len(short.split(" ")[0]) >= 5 else " ".join(short.split(" ")[:2])) , re.I)
    side = "up" if move_pct > 0.5 else "down" if move_pct < -0.5 else "flat"
    rows = [r for r in _gn(f'"{short}" shares when:3d') if start <= r["published"] <= end and _good(r) and key.search(r["title"])
            and consistent(r["title"], side)]
    newsy = re.compile(r"shares?|stock|target|rating|results?|profit|order|deal|stake|ipo|block|probe|sebi|ban|launch|approval|loss|revenue|brokerage|upgrade|downgrade", re.I)
    rows.sort(key=lambda r: (r["source"].lower() not in GOOD_SOURCES, not newsy.search(r["title"]), -r["published"].timestamp()))
    seen, out = set(), []
    for r in rows:
        k = re.sub(r"[^a-z0-9]", "", r["title"].lower())[:50]
        if k in seen:
            continue
        seen.add(k)
        out.append(_pack(r))
        if len(out) == limit:
            break
    return out


def wrap_headlines(extra: list[dict], day: dt.date, direction: str, now: dt.datetime, limit: int = 5) -> list[dict]:
    """Market-wrap style headlines for the session: index + a reason, from trusted outlets."""
    open_t = dt.datetime.combine(day, dt.time(6, 0), IST)                  # the session day's news cycle:
    close_t = dt.datetime.combine(day, dt.time(15, 25), IST)               # 06:00 on the day ... 06:00 the next day
    end = min(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(6, 0), IST), now.astimezone(IST))
    rows = list(extra)
    for q in ("Sensex Nifty closing today why market", "Sensex Nifty today stock market", "stock market today Nifty Sensex"):
        rows += _gn(q + " when:2d")
        time.sleep(0.4)
    reason = re.compile(r"\b(as|amid|after|on|tracking|led by|dragged|boost|lifted|hit by|supported|fears|hopes|rally|rallies|fall|falls|jump|surge|slump|tumble|gain|snap)\b", re.I)
    scored = []
    for r in rows:
        t = r["title"]
        if not (open_t <= r["published"] <= end) or not _good(r):
            continue
        if not re.search(r"sensex|nifty", t, re.I) or not consistent(t, direction):
            continue
        if r["published"] < close_t and not re.search(r"why|reason|key|rally|fall|surge|slump|jump|tumble", t, re.I):
            continue            # before the close only explainer-style pieces; closing wraps are preferred
        s = 3 + (2 if reason.search(t) else 0) + (2 if r["source"].lower() in GOOD_SOURCES else -2) + \
            (1 if re.search(r"close|closing|wrap|ends|settle|why", t, re.I) else 0)
        scored.append((s, r["published"].timestamp(), r))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    seen, out = set(), []
    for _, _, r in scored:
        k = re.sub(r"[^a-z0-9]", "", r["title"].lower())[:50]
        if k in seen:
            continue
        seen.add(k)
        out.append(_pack(r))
        if len(out) == limit:
            break
    return out


# ------------------------------------------------------------------------------ assembly
def _sg(v: float, d: int = 1) -> str:
    return f"{'+' if v >= 0 else '−'}{abs(v):.{d}f}%"


def _cr(v: float) -> str:
    return f"₹{abs(v):,.0f} cr"


def summary(direction: str, nifty_pct: float | None, sec: dict | None, flows: dict | None, macro: list[dict]) -> str:
    """One paragraph assembled only from measured items."""
    word = {"up": "rose", "down": "fell", "flat": "was flat"}[direction]
    parts = []
    head = f"The Nifty {word}" + (f" {_sg(nifty_pct, 2)}" if nifty_pct is not None else "")
    if sec and sec["sectors"]:
        s = sec["sectors"]
        lead, lag = s[:2], s[-2:]
        if direction == "down":
            head += f", dragged by {', '.join(f'{x['name']} ({_sg(x['pct'])})' for x in lag[::-1])}"
        else:
            head += f", led by {', '.join(f'{x['name']} ({_sg(x['pct'])})' for x in lead)}"
        if direction == "up" and lag and lag[-1]["pct"] < 0:
            head += f", with {lag[-1]['name']} ({_sg(lag[-1]['pct'])}) the weakest"
        if direction == "down" and lead and lead[0]["pct"] > 0:
            head += f", while {lead[0]['name']} ({_sg(lead[0]['pct'])}) held up"
    parts.append(head + ".")
    if sec and sec.get("breadth"):
        b = sec["breadth"]
        parts.append(f"{b['up']} of the {b['up'] + b['down']} Nifty stocks rose.")
    if flows and ("fii" in flows or "dii" in flows):
        f = []
        if "fii" in flows:
            f.append(f"FIIs {'bought' if flows['fii'] >= 0 else 'sold'} {_cr(flows['fii'])}")
        if "dii" in flows:
            f.append(f"DIIs {'bought' if flows['dii'] >= 0 else 'sold'} {_cr(flows['dii'])}")
        src = flows.get("source") or "NSE"
        parts.append(" while ".join(f) + f" (net, provisional, per {src}).")
    notable = [m for m in macro if m["notable"]]
    if notable:
        parts.append("Notable global and macro moves: " + "; ".join(f"{m['name']} {_sg(m['pct'])}" for m in notable[:4]) + ".")
    return " ".join(parts)


def build(movers: dict, date: str, direction: str, nifty_pct: float | None, market_headlines: list[dict], now: dt.datetime) -> dict:
    """Everything the 'why' card needs for the session `date` (ISO)."""
    session_date = dt.date.fromisoformat(date)
    start = dt.datetime.combine(session_date - dt.timedelta(days=1), dt.time(16, 0), IST)     # after the previous close
    end = min(dt.datetime.combine(session_date + dt.timedelta(days=1), dt.time(9, 0), IST), now.astimezone(IST))
    got, trace = {}, {}
    s = nse_session()
    trace["nse_session"] = "ok" if s else "blocked or unreachable"
    sec = nse_sectors(s, session_date) if s else None
    flows = nse_flows(s, session_date) if s else None
    if flows:
        flows["source"] = "NSE"
    trace["nse_sectors"], trace["nse_flows"] = bool(sec), bool(flows)
    if not flows:                                             # NSE refuses cloud servers: same numbers from Moneycontrol
        try:
            flows = flows_moneycontrol(session_date)
            trace["moneycontrol_flows"] = bool(flows)
        except Exception as exc:  # noqa: BLE001
            trace["moneycontrol_flows"] = f"error: {str(exc)[:60]}"
    if flows:
        try:
            save_flows({"date": session_date.isoformat(), **{k: v for k, v in flows.items() if k != "date"}})
        except Exception:  # noqa: BLE001
            pass
    else:
        cf = cached_flows(session_date)
        if cf:
            flows = {"date": session_date.isoformat(), **cf, "from_cache": True}
        trace["flows_cache"] = bool(cf)
    if not sec:                                               # NSE's static end-of-day file (a different server, usually reachable)
        try:
            sec = sectors_archive(session_date)
            trace["nse_archive_sectors"] = bool(sec)
        except Exception as exc:  # noqa: BLE001
            trace["nse_archive_sectors"] = f"error: {str(exc)[:60]}"
    got["Sectors"], got["FII/DII flows"] = bool(sec), bool(flows)
    macro = yahoo_macro(session_date)
    got["Yahoo macro"] = bool(macro)
    if not sec:      # NSE unreachable from here: fall back to the three sectoral indices Yahoo carries
        try:
            import yfinance as yf
            d = yf.download(["^NSEBANK", "^CNXIT", "^CNXPHARMA"], period="10d", interval="1d", auto_adjust=False, progress=False, group_by="ticker", timeout=30)
            rows = []
            for t, label in (("^NSEBANK", "Banks"), ("^CNXIT", "IT"), ("^CNXPHARMA", "Pharma")):
                c = d[t]["Close"].dropna()
                if len(c) > 1 and c.index[-1].date() == session_date:
                    rows.append({"name": label, "pct": round(float(c.iloc[-1] / c.iloc[-2] - 1) * 100, 2), "last": float(c.iloc[-1]), "up": None, "down": None})
            if rows:
                sec = {"sectors": sorted(rows, key=lambda r: -r["pct"]), "broad": [], "breadth": None}
                got["Yahoo sectors (fallback)"] = True
                trace["yahoo_sectors_fallback"] = True
        except Exception:  # noqa: BLE001
            pass

    # real headlines for the biggest movers of the user's money (by rupee impact), both directions
    movers_list = sorted((movers.get("gainers") or [])[:3] + (movers.get("laggards") or [])[:3], key=lambda x: -abs(x["impact"]))
    portfolio = []
    for m in movers_list:
        try:
            news = company_news(m["name"], start, end, m["move_pct"])
        except Exception:  # noqa: BLE001
            news = []
        time.sleep(0.4)
        portfolio.append({"name": m["name"], "move_pct": m["move_pct"], "impact": m["impact"], "exposure": m.get("exposure"),
                          "funds": m.get("funds"), "headlines": news})
    got["Company news"] = any(p["headlines"] for p in portfolio)

    try:
        press = wrap_headlines(market_headlines, session_date, direction, now)
    except Exception:  # noqa: BLE001
        press = []
    got["Market-wrap news"] = bool(press)

    nifty_row = next((r for r in (sec or {}).get("broad", []) if r["name"] == "Nifty 50"), None)
    pct = nifty_pct if nifty_pct is not None else (nifty_row["pct"] if nifty_row else None)
    return {"sectors": (sec or {}).get("sectors", []), "broad": (sec or {}).get("broad", []), "breadth": (sec or {}).get("breadth"),
            "flows": flows, "macro": macro, "portfolio": portfolio, "headlines": press,
            "summary": summary(direction, pct, sec, flows, macro), "sources_ok": got, "trace": trace}
