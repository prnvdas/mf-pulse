"""Latest AI / tech headlines for the dashboard's second ticker bar.

Writes docs/data/ai_news.json. Run once a day (ai-news.yml); a dead feed is
skipped, and if every feed fails the previous file is left in place.
"""

from __future__ import annotations

import datetime as dt
import re
import sys

from common import now_ist, read_json, write_json
from news import fetch_feed, public

FEEDS = [
    ("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("Economic Times", "https://economictimes.indiatimes.com/tech/rssfeeds/13357270.cms"),
    ("Google News", "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q=artificial+intelligence+when:2d"),
]
MAX_AGE_HOURS = 48
KEEP = 24
AI = re.compile(r"\bai\b|artificial intelligence|openai|anthropic|chatgpt|gemini|claude|llm|"
                r"machine learning|deepmind|copilot|nvidia|\bgpu|chatbot|generative|robot|agentic|"
                r"ai agent|superintelligence|data cent", re.I)


PROMO = re.compile(r"\$\d|% off|\bdeal\b|discount|lifetime|coupon|\bsale\b|giveaway|best buy|review:", re.I)


def main() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=MAX_AGE_HOURS)
    seen, rows = set(), []
    for source, url in FEEDS:
        for r in fetch_feed(source, url):
            key = re.sub(r"[^a-z0-9]", "", r["title"].lower())[:60]
            if key in seen or r["published"] < cutoff or len(r["title"]) < 20 or not AI.search(r["title"]) or PROMO.search(r["title"]):
                continue
            seen.add(key)
            rows.append(r)
    rows.sort(key=lambda r: r["published"], reverse=True)
    if not rows:
        print("[warn] no AI headlines found; leaving the previous ai_news.json in place", file=sys.stderr)
        return
    items = public(rows[:KEEP])
    if (read_json("ai_news.json", {}) or {}).get("items") == items:
        print("[info] AI news unchanged")
        return
    write_json("ai_news.json", {"generated_at": now_ist().isoformat(), "items": items})
    print(f"[ok] ai_news.json — {len(items)} headlines")


if __name__ == "__main__":
    main()
