"""Market and mutual-fund headlines, plus a plain-language "why did the market
move" explanation, written to docs/data/news.json.

Two kinds of points, kept deliberately separate so neither is passed off as
the other:

  * "fact" points come from this project's own data (index moves, how many of
    your stocks rose or fell, which holdings moved your money). They are
    computed, so they are right whenever the underlying data is.
  * "reason" points are what the news is *saying*, found by matching keywords
    in recent headlines to a short list of common causes (foreign selling,
    global markets, crude oil, rupee, results, interest rates...). They are
    shown with the headline that triggered them so you can read the source.
    This is pattern matching, not verified causation -- the page says so.

Runs after each estimate as a non-blocking step; a feed that is down is
skipped, and if every feed fails the previous news.json is left in place.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import re
import sys
import xml.etree.ElementTree as ET

import requests

from common import DATA_DIR, now_ist, read_json, write_json

UA = {"User-Agent": "Mozilla/5.0 (mf-pulse news fetch)"}
GN = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="

MARKET_FEEDS = [
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("Mint", "https://www.livemint.com/rss/markets"),
    ("Google News", GN + "Sensex+Nifty+today"),
]
MAX_AGE_HOURS = 72
KEEP = 30


# --- headlines ------------------------------------------------------------------

def fetch_feed(default_source: str, url: str) -> list[dict]:
    try:
        r = requests.get(url, timeout=20, headers=UA)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as exc:  # noqa: BLE001 -- one dead feed must not stop the rest
        print(f"[warn] {default_source} feed failed ({url[:60]}...): {exc}", file=sys.stderr)
        return []
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        source = default_source
        if default_source == "Google News":
            # Google titles end with " - Publisher"
            m = re.match(r"^(.*) - ([^-]{2,40})$", title)
            if m:
                title, source = m.group(1).strip(), m.group(2).strip()
        try:
            published = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            continue
        out.append({"title": title.replace("&#39;", "'").replace("&amp;", "&"),
                    "link": link, "source": source, "published": published})
    return out


RELEVANT = re.compile(r"sensex|nifty|stock|share|market|fund|sip\b|nav\b|mutual|sebi|rbi|fii|fpi|ipo|equity|"
                      r"invest|rupee|crude|dividend|etf|bank|earnings|results", re.I)
JUNK = re.compile(r"live share|stock market news & updates|quotes[- ]|official site|homepage|^nse\b", re.I)


def is_good(row: dict) -> bool:
    """Google News search also returns site landing pages and loose matches."""
    t = row["title"]
    return len(t) >= 25 and not JUNK.search(t) and bool(RELEVANT.search(t))


def collect(feeds: list[tuple[str, str]], now: dt.datetime) -> list[dict]:
    seen, rows = set(), []
    cutoff = now - dt.timedelta(hours=MAX_AGE_HOURS)
    for source, url in feeds:
        for row in fetch_feed(source, url):
            key = re.sub(r"[^a-z0-9]", "", row["title"].lower())[:60]
            if key in seen or row["published"] < cutoff or not is_good(row):
                continue
            seen.add(key)
            rows.append(row)
    rows.sort(key=lambda r: r["published"], reverse=True)
    return rows[:KEEP]


def public(rows: list[dict]) -> list[dict]:
    return [{"title": r["title"], "link": r["link"], "source": r["source"],
             "published": r["published"].astimezone(dt.timezone.utc).isoformat()} for r in rows]


# --- "what the news is saying" ---------------------------------------------------

DOWN_WORDS = ["fall", "drop", "slump", "tumble", "sell-off", "selloff", "slide", "plunge",
              "weak", "declin", "lower", "crash", "tank", "slip"]
UP_WORDS = ["rally", "gain", "surge", "jump", "rise", "rises", "climb", "higher", "record",
            "upbeat", "soar", "rebound", "recover", "green"]

# up/down words are checked against the headline to decide which way each cause
# pushed things; a theme with no up/down text is neutral ("in focus").
THEMES = [
    {"id": "fii", "kw": ["fii", "fpi", "foreign investor", "foreign portfolio", "foreign fund"],
     "down_w": ["sell", "selling", "sold", "outflow", "pull", "dump", "exit", "withdr"],
     "up_w": ["buy", "bought", "inflow", "pour", "infus"],
     "down": "Foreign investors were reported selling Indian shares. Big overseas funds own a lot of the market, so when they sell, prices tend to fall.",
     "up": "Foreign investors were reported buying Indian shares. When big overseas funds buy, prices tend to rise."},
    {"id": "global", "kw": ["wall street", "us market", "us stocks", "dow ", "nasdaq", "s&p 500",
                            "global cues", "global market", "asian market", "federal reserve", "us fed",
                            "treasury yield", "asian stocks"],
     "down_w": DOWN_WORDS, "up_w": UP_WORDS,
     "down": "Shares fell in other big markets (like the US and Asia) and India followed. Markets around the world often move together.",
     "up": "Shares rose in other big markets (like the US and Asia), which usually lifts the mood in India too."},
    {"id": "crude", "kw": ["crude", "brent", "oil price"],
     "down_w": ["surge", "jump", "rise", "rises", "spike", "climb", "higher", "soar", "hike"],
     "up_w": ["fall", "drop", "slip", "cool", "ease", "lower", "slump", "decline"],
     "down": "Crude oil got costlier. India buys most of its oil from abroad, so pricier oil hurts company profits and the rupee.",
     "up": "Crude oil got cheaper, which helps India because we import most of our oil."},
    {"id": "rupee", "kw": ["rupee"],
     "down_w": ["fall", "weak", "slip", "slide", "record low", "depreciat", "drop", "lower"],
     "up_w": ["gain", "strong", "rise", "appreciat", "recover", "higher"],
     "down": "The rupee weakened against the dollar. That makes imports costlier and can make foreign investors nervous.",
     "up": "The rupee strengthened against the dollar, which usually helps sentiment."},
    {"id": "geo", "kw": ["war", "tension", "tariff", "trade deal", "sanction", "geopolit", "conflict", "attack"],
     "down_w": ["tension", "tariff", "war", "sanction", "conflict", "attack", "fear", "worry", "worries"],
     "up_w": ["deal", "ceasefire", "truce", "ease", "talks", "relief"],
     "down": "Global tension or trade worries (like tariffs or conflict) made investors cautious. When people are nervous they sell shares and move money somewhere safer.",
     "up": "Easing global tension or hopes of a trade deal lifted the mood."},
    {"id": "profit", "kw": ["profit booking", "profit-taking", "profit taking", "overvalued", "expensive valuation", "stretched valuation"],
     "down_w": ["profit", "valuation", "overvalued", "expensive", "stretched"], "up_w": [],
     "down": "After a rise, many investors sold to lock in their gains (\"profit booking\"), or felt prices had become too expensive.",
     "up": ""},
    {"id": "rates", "kw": ["rbi", "repo rate", "interest rate", "inflation", "cpi", "mpc"],
     "neutral": "Interest rates or inflation are in the news. Higher rates make loans costlier and can pull money out of shares; lower rates usually help."},
    {"id": "results", "kw": ["q1 result", "q2 result", "q3 result", "q4 result", "earnings", "quarterly result",
                             "net profit", "revenue rises", "q2 revenue", "q2 update"],
     "neutral": "Companies are reporting results. A share usually jumps when profits beat expectations and drops when they disappoint."},
    {"id": "policy", "kw": ["sebi", "budget", " gst", "tax rule", "new rules"],
     "neutral": "A rule or policy announcement (regulator, tax or government) is in the news and can move the shares it affects."},
]


def has(text: str, words: list[str]) -> bool:
    """True if any word appears starting at a word boundary (so 'cut' won't match 'execute')."""
    return any(re.search(r"\b" + re.escape(w), text) for w in words if w)


def reasons(direction: str, headlines: list[dict]) -> list[dict]:
    scored = []
    for th in THEMES:
        hits = []
        for h in headlines:
            t = " " + h["title"].lower() + " "
            if not any(k in t for k in th["kw"]):
                continue
            if "neutral" in th:
                hits.append(h)
                continue
            d = "down" if has(t, th["down_w"]) else "up" if has(t, th["up_w"]) else None
            if d == direction and th.get(d):
                hits.append(h)
        if hits:
            neutral = "neutral" in th
            scored.append((0 if neutral else 1, len(hits), th, hits))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out, neutral_used = [], 0
    for _, _, th, hits in scored:
        if len(out) == 4 or ("neutral" in th and neutral_used == 2):
            continue
        neutral_used += "neutral" in th
        text = th["neutral"] if "neutral" in th else th[direction]
        out.append({"kind": "reason", "text": text,
                    "evidence": [{"title": h["title"], "link": h["link"], "source": h["source"]}
                                 for h in hits[:2]]})
    return out


def explainers(headlines: list[dict]) -> list[dict]:
    """Articles whose own headline asks 'why did the market ...'."""
    out = []
    for h in headlines:
        t = h["title"].lower()
        if "why" in t and any(k in t for k in ("market", "sensex", "nifty", "stocks")) and \
                any(k in t for k in ("fall", "fell", "crash", "drop", "rise", "rally", "surge", "jump", "tank", "slump")):
            out.append({"title": h["title"], "link": h["link"], "source": h["source"]})
    return out[:2]


# --- facts from our own data ------------------------------------------------------

def money(n: float) -> str:
    n = abs(n)
    return f"₹{n / 100000:.1f}L" if n >= 100000 else f"₹{round(n):,}"


def sg(v: float, d: int = 1) -> str:
    return f"{'+' if v >= 0 else '−'}{abs(v):.{d}f}%"


def short(name: str) -> str:
    return name.replace(" Direct Growth", "").replace(" Fund", "")


def pick_session(latest: dict | None, movers_hist: list[dict]) -> tuple[dict | None, str, str | None]:
    """Today's data if the market traded, else the most recent stored session."""
    if latest and latest.get("phase") in ("live", "final") and latest.get("movers"):
        m = dict(latest["movers"])
        m.setdefault("market", latest.get("market"))
        m.setdefault("breadth", latest.get("breadth"))
        m.setdefault("portfolio_pct", latest["totals"]["today_pct"])
        m.setdefault("portfolio_impact", latest["totals"]["today_impact"])
        m["funds"] = [{"id": f["id"], "name": f["name"], "nav_move_pct": f["nav_move_pct"],
                       "impact": round(f["rupee_impact"])} for f in latest["funds"]]
        return m, "today", m.get("date")
    if movers_hist:
        m = sorted(movers_hist, key=lambda x: x["date"])[-1]
        return m, "last", m["date"]
    return None, "none", None


def facts(m: dict) -> tuple[list[dict], str]:
    pts: list[dict] = []
    market = m.get("market") or []
    nifty = next((x for x in market if x["name"] == "Nifty 50"), None)
    gain_sum = sum(x["impact"] for x in m.get("gainers", [])) + sum(x["impact"] for x in m.get("laggards", []))
    if nifty:
        basis = nifty["move_pct"]
    elif m.get("portfolio_pct") is not None:
        basis = m["portfolio_pct"]
    else:
        basis = 1.0 if gain_sum > 0 else -1.0
    direction = "up" if basis > 0.15 else "down" if basis < -0.15 else "flat"
    word = {"up": "rose", "down": "fell", "flat": "barely moved"}[direction]

    if market:
        line = ", ".join(f"{x['name']} {sg(x['move_pct'])}" for x in market)
        pts.append({"kind": "fact", "text": f"The market {word}: {line}."})
        small = next((x for x in market if "Smallcap" in x["name"]), None)
        if nifty and small and nifty["move_pct"] * small["move_pct"] > 0 \
                and abs(small["move_pct"]) > abs(nifty["move_pct"]) + 0.3:
            pts.append({"kind": "fact", "text": "Small companies moved more than big ones "
                        f"({sg(small['move_pct'])} vs {sg(nifty['move_pct'])}). Small and mid-sized stocks swing harder, both up and down."})
        elif nifty and small and nifty["move_pct"] * small["move_pct"] < 0:
            pts.append({"kind": "fact", "text": "Big and small companies went in different directions "
                        f"(Nifty 50 {sg(nifty['move_pct'])}, smallcaps {sg(small['move_pct'])}), so the day felt different depending on what you own."})

    b = m.get("breadth")
    if b and b["total"]:
        up, down, tot = b["up"], b["down"], b["total"]
        if direction == "down" and up / tot < 0.25:
            txt = f"Almost everything fell: only {up} of the {tot} stocks in your funds rose. This was a fall across the board, not one bad stock."
        elif direction == "down" and down > up:
            txt = f"More stocks fell than rose in your funds ({down} down, {up} up)."
        elif direction == "up" and up / tot > 0.75:
            txt = f"Almost everything rose: {up} of the {tot} stocks in your funds went up. This was a rise across the board."
        elif direction == "up" and up > down:
            txt = f"More stocks rose than fell in your funds ({up} up, {down} down)."
        else:
            txt = f"A mixed day: {up} of your funds' stocks rose and {down} fell."
        pts.append({"kind": "fact", "text": txt})

    lag = m.get("laggards") or []
    gai = m.get("gainers") or []
    if lag:
        x = lag[0]
        pts.append({"kind": "fact", "text": f"Biggest drag on your money: {x['name']} ({sg(x['move_pct'])}), "
                    f"which cost you about {money(x['impact'])} because you hold roughly {money(x['exposure'])} of it."})
    if gai and (direction != "up" or len(pts) < 5):
        x = gai[0]
        pts.append({"kind": "fact", "text": f"Helping most: {x['name']} ({sg(x['move_pct'])}), adding about {money(x['impact'])} to your money."})
    if len(lag) >= 3 and direction == "down":
        names = ", ".join(x["name"] for x in lag[:3])
        pts.append({"kind": "fact", "text": f"Your three worst stocks ({names}) together cost about {money(sum(x['impact'] for x in lag[:3]))}."})

    funds = m.get("funds") or []
    if funds and m.get("portfolio_pct") is not None:
        parts = ", ".join(f"{short(f['name'])} {sg(f['nav_move_pct'], 2)}" for f in funds)
        total = m.get("portfolio_impact") or 0
        pts.append({"kind": "fact", "text": f"Estimated effect on your funds: {parts}. In total your portfolio moved about "
                    f"{'+' if total >= 0 else '−'}{money(total)} ({sg(m['portfolio_pct'], 2)})."})
        worst = min(funds, key=lambda f: f["nav_move_pct"]) if direction == "down" else max(funds, key=lambda f: f["nav_move_pct"])
        spread = max(f["nav_move_pct"] for f in funds) - min(f["nav_move_pct"] for f in funds)
        if spread > 0.2:
            pts.append({"kind": "fact", "text": f"{short(worst['name'])} moved the most. Funds holding more small and mid-sized stocks swing more than funds holding mostly large ones."})
    return pts, direction


def build_why(latest, movers_hist, headlines, now) -> dict | None:
    m, which, date = pick_session(latest, movers_hist)
    if not m:
        return None
    pts, direction = facts(m)
    rs = reasons(direction, headlines) if direction != "flat" else []
    day = dt.date.fromisoformat(date).strftime("%a %-d %b") if date else ""
    word = {"up": "rose", "down": "fell", "flat": "was flat"}[direction]
    if which == "today":
        title = {"up": "Why the market rose today", "down": "Why the market fell today", "flat": "How the market did today"}[direction]
        session = "Today"
    else:
        title = f"Why the market {word} in the last session ({day})" if direction != "flat" else f"How the market did in the last session ({day})"
        session = f"Last trading session ({day}) — the market is closed today"
    top = rs[0]["text"].split(".")[0] if rs else None
    summary = (f"In plain words: the market {word}" +
               (f", mainly because: {top[0].lower() + top[1:]}." if top else ".") +
               " The news reasons below are matched automatically from headlines, so treat them as what's being reported, not proof.")
    return {"session": session, "date": date, "direction": direction, "title": title,
            "summary": summary, "points": (pts + rs)[:10], "reads": explainers(headlines)}


# --- main -------------------------------------------------------------------------

def main() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    market = collect(MARKET_FEEDS, now)
    if not market:
        print("[warn] every news feed failed; leaving the previous news.json in place", file=sys.stderr)
        return

    why = build_why(read_json("latest.json", None), read_json("movers.json", []), market, now)
    body = {"market": public(market), "why": why}

    old = read_json("news.json", {})
    if {k: old.get(k) for k in body} == body:
        print("[info] news unchanged")
        return
    write_json("news.json", {"generated_at": now_ist().isoformat(), **body})
    print(f"[ok] news.json — {len(market)} market headlines; "
          f"why: {why['direction'] if why else None}, {len(why['points']) if why else 0} points")


if __name__ == "__main__":
    main()
