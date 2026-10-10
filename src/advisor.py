"""A plain-language read of "should I buy or sell today?" -> docs/data/advisor.json.

Rules-based and fully transparent: every line shows the number it rests on. It combines
  * the market mood (Tickertape's index, a popular contrarian gauge: fear tends to be a better time to invest than greed),
  * how expensive each part of the market is against its own history, weighted by what you actually hold,
  * the trend (Nifty against its moving averages) and foreign/domestic flows,
  * your own position: drawdown, SIP, tax situation.
and then says what each means for a long-term SIP investor. It never says "sell everything": selling is only raised
for money needed soon, or when mood and valuations are both stretched.

Honest about its limits: the back-test in mood.json shows valuation has been a weak guide to average returns (though
a better guide to how bad the worst year could be), and the mood index's own history is not public to test. So the
output is a reasoned tilt for patience and staging, not a prediction. Educational, not personalised financial advice.
"""
from __future__ import annotations

from common import load_portfolio, now_ist, read_json, write_json


def lakh(n: float) -> str:
    n = abs(n)
    if n >= 1e7:
        c = n / 1e7
        return f"₹{c:,.0f} crore" if c >= 100 else f"₹{c:.2f} crore".replace(".00 ", " ")
    if n >= 1e5:
        return f"₹{n / 1e5:.2f} lakh".replace(".00 ", " ")
    return f"₹{round(n):,}"


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def main() -> None:
    cfg, ts = load_portfolio(), now_ist()
    mood_d = read_json("mood.json", {}) or {}
    mood, val = mood_d.get("mood") or {}, mood_d.get("valuation") or []
    risk, tax, outlook = read_json("risk.json", {}) or {}, read_json("tax.json", {}) or {}, read_json("outlook.json", {}) or {}
    latest, news = read_json("latest.json", {}) or {}, read_json("news.json", {}) or {}
    flows = ((news.get("why") or {}).get("flows")) or {}
    sip_total = sum(float(s["amount"]) for s in cfg.get("sips", []))
    if not mood and not val:
        print("[error] no mood or valuation data; leaving advisor.json unchanged")
        raise SystemExit(1)

    # ---- 1. mood: contrarian mapping
    mv = mood.get("value")
    if mv is None:
        m_score = 0.0
    else:
        m_score = 2.0 if mv < 20 else 1.5 if mv < 30 else 0.5 if mv < 45 else 0.0 if mv < 55 else -0.5 if mv < 70 else -1.5 if mv < 80 else -2.0

    # ---- 2. valuation, weighted by what you hold (each fund's own benchmark)
    by = {r["index"]: r for r in val}
    fund_rows, wsum, v_score = [], 0.0, 0.0
    for f in cfg["funds"]:
        name = (f.get("analytics_benchmark") or {}).get("name", "")
        r = next((x for x in val if f["name"] in (x.get("mine") or [])), None)
        value = (read_json("state.json", {}).get(f["id"], {}).get("units") or 0) * (read_json("state.json", {}).get(f["id"], {}).get("last_nav") or 0)
        if r and value:
            fund_rows.append((f, r, value))
    total_val = sum(v for _, _, v in fund_rows) or 1
    for f, r, v in fund_rows:
        v_score += (v / total_val) * clamp((50 - r["score_pct"]) / 25, -2, 2)

    # ---- 3. trend
    vs_long, vs_short = mood.get("vs_long_avg_pct"), mood.get("vs_short_avg_pct")
    t_score = 0.0
    if vs_long is not None:
        t_score = -0.5 if vs_long < -2 and (vs_short is None or vs_short < 0) else 0.3 if vs_long > 2 else 0.0

    total = m_score + v_score + t_score
    if total >= 2.5:
        key, stance = "buy", "A good time to invest more, in stages"
    elif total >= 1.0:
        key, stance = "lean_buy", "Lean towards investing more, in stages"
    elif total > -1.0:
        key, stance = "hold", "Hold and keep your SIPs running"
    elif total > -2.5:
        key, stance = "hold_no_add", "Hold; skip lump sums for now"
    else:
        key, stance = "trim", "Be cautious: consider booking some profit in stages"
    headline = {
        "buy": "Don't sell. Keep your SIPs going and, if you have spare cash, invest it in three or four instalments over the next couple of months.",
        "lean_buy": "Don't sell. Keep your SIPs going and, if you have spare cash, invest it in three or four instalments rather than all at once.",
        "hold": "Nothing to do today. Keep your SIPs running and leave your holdings alone.",
        "hold_no_add": "Keep your SIPs running, but don't add lump sums right now.",
        "trim": "Markets look stretched. Keep your SIPs, avoid lump sums, and consider booking some profit in stages if you need money in the next few years.",
    }[key]

    # ---- reasons, each with its number
    reasons = []
    if mv is not None:
        extra = f" It was {mood['month_ago']:.0f} a month ago and {mood['year_ago']:.0f} a year ago." if mood.get("month_ago") is not None and mood.get("year_ago") is not None else ""
        reasons.append({"key": "mood", "title": "Market mood", "value": f"{mv:.0f} · {mood['zone']}", "effect": "+" if m_score > 0 else "-" if m_score < 0 else "=",
                        "text": f"The Market Mood Index ({mood['source'].split(' (')[0]}) reads {mv:.0f} of 100: {mood['zone'].lower()}.{extra} Investors often get most nervous near market lows and most confident near highs, so fear is usually the better time to invest and greed the time to be careful. That is a tendency, not a promise."})
    for f, r, v in sorted(fund_rows, key=lambda x: -x[2]):
        reasons.append({"key": "val_" + f["id"], "title": f"{r['label'].split(' (')[0]} · your {f['name'].split(' Fund')[0].replace(' Direct Growth', '')}",
                        "value": f"P/E {r['pe']} · {r['verdict'].lower()}", "effect": "+" if r["score_pct"] < 35 else "-" if r["score_pct"] > 65 else "=",
                        "text": f"Its benchmark trades at a P/E of {r['pe']}, against a typical {r['median_pe']} over the last ten years ({'a discount' if r['premium_pct'] < 0 else 'a premium'} of {abs(r['premium_pct']):.0f}%). Only {r['pct_10y']}% of the last ten years were cheaper, and {r['pct_5y']}% of the last five. Verdict: {r['verdict'].lower()}."})
    if vs_long is not None:
        reasons.append({"key": "trend", "title": "Trend", "value": f"Nifty {vs_long:+.1f}% vs its long average", "effect": "-" if t_score < 0 else "+" if t_score > 0 else "=",
                        "text": f"The Nifty ({mood.get('nifty'):,.0f}) is {abs(vs_long):.1f}% {'below' if vs_long < 0 else 'above'} its longer-term average ({mood.get('long_avg'):,})"
                                + (f" and {abs(vs_short):.1f}% {'below' if vs_short < 0 else 'above'} its shorter-term one" if vs_short is not None else "")
                                + (". Prices are still falling, so cheap can get cheaper: this is why new money should go in over weeks, not on one day." if t_score < 0 else ".")})
    if flows.get("fii") is not None or flows.get("dii") is not None:
        fii, dii = flows.get("fii"), flows.get("dii")
        bits = [f"foreign investors {'sold' if fii < 0 else 'bought'} {lakh(abs(fii) * 1e7)}" if fii is not None else None, f"domestic funds {'sold' if dii < 0 else 'bought'} {lakh(abs(dii) * 1e7)}" if dii is not None else None]
        reasons.append({"key": "flows", "title": "Who is buying and selling", "value": " · ".join(b for b in bits if b), "effect": "=",
                        "text": "On the last session " + " while ".join(b for b in bits if b) + ". Heavy foreign selling is a big part of why the market is under pressure; domestic buyers have been absorbing it. This explains the mood, it does not tell you what happens next."})
    dd = (risk.get("risk") or {}).get("current_drawdown_pct")
    if dd is not None:
        worst = (risk.get("risk") or {}).get("max_drawdown_pct")
        reasons.append({"key": "you", "title": "Your portfolio", "value": f"{dd:+.1f}% from its peak", "effect": "=",
                        "text": f"Your mix is {abs(dd):.1f}% below its most recent peak. That is an ordinary dip: this same mix has fallen as much as {abs(worst):.0f}% before and recovered. Selling after a fall is how a temporary dip becomes a permanent loss."})
    call = outlook.get("call") or {}
    if call:
        reasons.append({"key": "tomorrow", "title": "Tomorrow", "value": f"{call['colour'].upper()} · {call['p_call']:.0f}% ({call['tier']})", "effect": "=",
                        "text": "The short-term call for the next session is a coin-flip with a small tilt. It is useful context for a day trader and irrelevant for a long-term investor: don't buy or sell on it."})

    # ---- where new money looks best (cheapest first among the categories your funds map to)
    where = sorted([{"label": r["label"], "fund": [m.split(" Fund")[0].replace(" Direct Growth", "") for m in r.get("mine") or []], "pe": r["pe"], "verdict": r["verdict"], "score_pct": r["score_pct"]}
                    for r in val], key=lambda x: x["score_pct"])
    mine_where = [w for w in where if w["fund"]]
    tilt = ""
    if len(mine_where) >= 2:
        a, z = mine_where[0], mine_where[-1]
        if z["score_pct"] - a["score_pct"] >= 25:
            tilt = (f"Of the parts of the market your funds track, {a['label'].split(' (')[0].lower()} looks cheapest ({a['verdict'].lower()}) and {z['label'].split(' (')[0].lower()} the dearest ({z['verdict'].lower()}). "
                    f"If you add extra money, tilting it towards {', '.join(a['fund'])} rather than {', '.join(z['fund'])} follows that. Your regular SIPs can stay as they are.")

    sell = []
    plan = (tax.get("harvest") or {}).get("plan") or []
    sa = tax.get("sell_all") or {}
    if tax:
        sell.append(f"If you sold everything today you would pay about {lakh(sa.get('tax', 0))} in tax, which is a strong reason not to sell on a market view.")
    sell.append("Only sell money you will need within about three years; money for later goals is better left invested through the dip.")
    if plan:
        p = plan[0]
        sell.append(f"If you do need cash, sell from the older folio first (e.g. {p['fund'].split(' Fund')[0].replace(' Direct Growth', '')} folio {p.get('folio') or ''}): its gains are long-term, and the first {lakh((tax.get('rates') or {}).get('ltcg_exempt_inr', 125000))} of long-term gain each year is tax-free. The newer folios may still be short-term (taxed at 20%).")
    sell.append("If you must raise a large sum, sell in two or three parts over a few weeks instead of in one go.")

    do_today = [f"Keep your {lakh(sip_total)} monthly SIP running. When prices are low it buys more units for the same money, which is exactly what a SIP is for." if sip_total else "Keep your SIPs running."]
    if key in ("buy", "lean_buy"):
        do_today.append("If you have spare cash: split it into three or four equal parts and invest one part now and the others every two to three weeks. The trend is still down, so spreading it out protects you if prices fall further.")
    elif key in ("hold_no_add", "trim"):
        do_today.append("Hold off on lump sums until mood and valuations cool down.")
    else:
        do_today.append("If you have spare cash, there is no urgency; investing it gradually is fine.")
    if tilt:
        do_today.append(tilt)
    do_today.append("Do not sell because of today's fall or tomorrow's red or green call.")

    watch = ["If the Market Mood rises above 70 (extreme greed) and small caps are expensive against their own history, stop lump sums and think about booking some profit.",
             "If it stays below 30, nothing changes: keep investing in stages. Recoveries from fear usually take months, not days.",
             (f"If the Nifty closes back above its longer-term average ({mood['long_avg']:,}) the trend has turned: invest any remaining instalments." if mood.get("long_avg") else "Watch whether the Nifty climbs back above its longer-term average.")]

    bt = mood_d.get("valuation_backtest") or {}
    evidence = None
    if bt.get("buckets"):
        b = {x["bucket"]: x for x in bt["buckets"]}
        c, d = b.get("Cheapest third"), b.get("Dearest third")
        if c and d:
            evidence = (f"How much to trust this: on the Nifty 50 since {bt['from'][:4]}, months when it looked cheap against its own history were followed by a median {c['median_3y']:.1f}% a year over three years, "
                        f"against {d['median_3y']:.1f}% after dear months. So cheap or dear has told us little about average returns. It did tell us about the bad cases: the worst one-year result after cheap months was {c['worst_1y']:.0f}%, after dear months {d['worst_1y']:.0f}%. "
                        "Treat the signals above as a reason for patience and for spreading out new money, not as a forecast.")

    write_json("advisor.json", {"generated_at": ts.isoformat(), "mood_as_of": mood.get("as_of"), "stance": stance, "stance_key": key, "headline": headline,
                                "score": {"mood": round(m_score, 1), "valuation": round(v_score, 1), "trend": round(t_score, 1), "total": round(total, 1)},
                                "reasons": reasons, "do_today": do_today, "where_to_add": where, "if_you_sell": sell, "watch": watch, "evidence": evidence,
                                "disclaimer": "Educational analysis of public market data, not personalised or SEBI-registered investment advice. It cannot know your goals, time horizon or income. Markets can stay cheap or expensive for long periods."})
    print(f"[ok] advisor.json: {stance} (score {total:+.1f}: mood {m_score:+.1f}, valuation {v_score:+.1f}, trend {t_score:+.1f})")


if __name__ == "__main__":
    main()
