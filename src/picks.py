"""Weekly ideas: five crypto coins and five Indian stocks that screen best for upside, with the reasons, the risks and a
running track record of how earlier picks did.  ->  docs/data/picks.json (+ docs/data/crypto.json for the ticker)

READ THIS FIRST. Nobody can reliably pick multibaggers in advance, and this does not claim to. It is a transparent,
rules-based screen over public data (CoinGecko for crypto; Yahoo Finance prices and fundamentals for the Nifty 500):
it ranks assets that are already showing strength and have room left, then lists exactly which numbers earned the
rank. Most screened names will not multiply; some will fall hard. The track record (every week's picks scored against
Bitcoin and the Nifty 500 once they are 4+ weeks old) is there so you can see, with evidence, whether the screen
deserves any trust at all. It is not investment advice.

    python src/picks.py            # crypto + stocks
    python src/picks.py --crypto   # only one half
    python src/picks.py --stocks
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from common import CONFIG, load_holdings, load_portfolio, now_ist, read_json, write_json

CG = "https://api.coingecko.com/api/v3"
NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
PICKS = 5
HISTORY_KEEP = 60                                    # weekly snapshots kept for the track record


# ------------------------------------------------------------------------------------------------ helpers
def pct_rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True).fillna(0.5)


def fnum(x) -> float | None:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def cg(path: str, **params):
    for attempt in range(4):
        try:
            r = requests.get(CG + path, params=params, headers=UA, timeout=30)
            if r.status_code == 429:
                time.sleep(25)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] coingecko {path} attempt {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(4)
    return None


def human(n: float) -> str:
    return f"${n / 1e9:.1f}B" if n >= 1e9 else f"${n / 1e6:.0f}M"


# ------------------------------------------------------------------------------------------------ crypto
EXCLUDE_NAME = re.compile(r"wrapped|staked|bridged|peg|\busd|usd\b|tether|stable|restaked|liquid staking|\bwbtc\b|\bsteth\b|\bweth\b", re.I)


def crypto_universe() -> tuple[list[dict], dict]:
    """Top coins by market cap, minus stablecoins, wrapped/staked/bridged copies and memecoins."""
    rows = cg("/coins/markets", vs_currency="usd", order="market_cap_desc", per_page=250, page=1, price_change_percentage="7d,30d,200d,1y", sparkline="false")
    if not rows:
        return [], {}
    skip: set[str] = set()
    for cat in ("stablecoins", "wrapped-tokens", "liquid-staking-tokens", "meme-token"):
        time.sleep(3)
        part = cg("/coins/markets", vs_currency="usd", category=cat, order="market_cap_desc", per_page=250, page=1)
        skip |= {c["id"] for c in (part or [])}
    return [c for c in rows if c["id"] not in skip and not EXCLUDE_NAME.search(f"{c['id']} {c['name']} {c['symbol']}")], {c["id"]: c for c in rows}


def crypto_picks(prev_btc: dict) -> dict | None:
    universe, everything = crypto_universe()
    if not universe:
        return None
    btc = everything.get("bitcoin") or {}
    b30, b200 = fnum(btc.get("price_change_percentage_30d_in_currency")) or 0.0, fnum(btc.get("price_change_percentage_200d_in_currency")) or 0.0
    df = pd.DataFrame([{
        "id": c["id"], "symbol": str(c["symbol"]).upper(), "name": c["name"], "rank": c.get("market_cap_rank"), "price": fnum(c.get("current_price")),
        "mcap": fnum(c.get("market_cap")), "vol": fnum(c.get("total_volume")), "ath": fnum(c.get("ath")), "ath_chg": fnum(c.get("ath_change_percentage")),
        "ath_date": c.get("ath_date"), "d1": fnum(c.get("price_change_percentage_24h")), "d7": fnum(c.get("price_change_percentage_7d_in_currency")),
        "d30": fnum(c.get("price_change_percentage_30d_in_currency")), "d200": fnum(c.get("price_change_percentage_200d_in_currency")),
        "y1": fnum(c.get("price_change_percentage_1y_in_currency"))} for c in universe])
    n_all = len(df)
    df = df.dropna(subset=["price", "mcap", "vol", "d30", "d200", "ath_chg"])
    df = df[(df["rank"] <= 150) & (df["mcap"] >= 1e9) & (df["vol"] / df["mcap"] >= 0.01)]
    df = df[(df["d200"] > -20) & (df["d30"] > -15)]                       # not a falling knife
    if len(df) < PICKS:
        return None
    df["rs30"], df["rs200"] = df["d30"] - b30, df["d200"] - b200
    df["room"] = (-df["ath_chg"]).clip(0, 85)
    df["liq"] = df["vol"] / df["mcap"]
    df["small"] = -np.log(df["mcap"])                                      # smaller coins have more room to multiply (and more risk)
    heat = (df["d7"].fillna(0) > 60) | (df["d30"] > 150)                   # already vertical: chasing it is how people lose money
    df["score"] = (0.30 * pct_rank(df["rs200"]) + 0.25 * pct_rank(df["rs30"]) + 0.15 * pct_rank(df["small"])
                   + 0.15 * pct_rank(df["room"]) + 0.10 * pct_rank(df["liq"]) + 0.05 * pct_rank(-df["d7"].abs())) * 100
    df.loc[heat, "score"] -= 15
    out = []
    for _, r in df.sort_values("score", ascending=False).head(PICKS).iterrows():
        why = [f"{r['d200']:+.0f}% over 200 days against Bitcoin's {b200:+.0f}%: it has been beating the market, not just riding it",
               f"{r['d30']:+.0f}% in 30 days (Bitcoin {b30:+.0f}%)",
               f"Market cap {human(r['mcap'])}, rank #{int(r['rank'])}: " + ("small enough to grow a lot, big enough to be liquid" if r["mcap"] < 2.5e10 else "large, so the upside is more limited"),
               f"{r['room']:.0f}% below its all-time high" + (f" of ${r['ath']:,.4g}" if r["ath"] else ""),
               f"Trades about {human(r['vol'])} a day ({r['liq'] * 100:.0f}% of its size): easy to buy and sell"]
        risk = ["Coins this size routinely fall 60-80% in a bad market; only money you can lose entirely belongs here"]
        if r["d30"] > 60:
            risk.append(f"Already up {r['d30']:.0f}% in a month: some of the move may be done")
        if r["room"] >= 75:
            risk.append("Far below its high: ask why before assuming it will come back")
        if r["mcap"] < 3e9:
            risk.append("Small market cap: thinner markets, bigger swings, higher failure risk")
        out.append({"id": r["id"], "symbol": r["symbol"], "name": r["name"], "price_usd": r["price"], "rank": int(r["rank"]), "mcap_usd": r["mcap"], "score": round(float(r["score"]), 1),
                    "d30": round(r["d30"], 1), "d200": round(r["d200"], 1), "y1": None if pd.isna(r["y1"]) else round(r["y1"], 1), "why": why[:4], "risk": risk[:3]})
    majors = {k: {"usd": fnum(everything[k].get("current_price")), "chg": fnum(everything[k].get("price_change_percentage_24h"))} for k in ("bitcoin", "ethereum") if k in everything}
    return {"picks": out, "screened": n_all, "eligible": int(len(df)), "bitcoin": {"price": fnum(btc.get("current_price")), "d30": b30, "d200": b200}, "majors": majors,
            "prices": {c["id"]: fnum(c.get("current_price")) for c in everything.values()}}


# ------------------------------------------------------------------------------------------------ stocks
def nifty500() -> pd.DataFrame:
    """Nifty 500 constituents (symbol, name, industry) from NSE; last good copy cached; funds' own holdings as a last resort."""
    cache = CONFIG / "universe_nifty500.csv"
    try:
        r = requests.get(NIFTY500_URL, headers=UA, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df = df.rename(columns={"Company Name": "name", "Industry": "industry", "Symbol": "symbol"})[["symbol", "name", "industry"]]
        if len(df) > 300:
            df.to_csv(cache, index=False)
            return df
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] NSE Nifty 500 list: {exc}", file=sys.stderr)
    if cache.exists():
        return pd.read_csv(cache)
    seen, rows = set(), []
    for f in load_portfolio()["funds"]:
        for h in load_holdings(f["id"]).get("holdings") or []:
            sym = str(h["ticker"]).replace(".NS", "")
            if sym not in seen:
                seen.add(sym)
                rows.append({"symbol": sym, "name": h["name"], "industry": ""})
    return pd.DataFrame(rows)


def stock_info(sym: str) -> dict | None:
    for attempt in range(4):
        try:
            i = yf.Ticker(sym + ".NS").info
            if not i or i.get("marketCap") is None:
                return None
            return {"symbol": sym, "mcap": fnum(i.get("marketCap")), "pe": fnum(i.get("trailingPE")), "roe": fnum(i.get("returnOnEquity")), "rg": fnum(i.get("revenueGrowth")),
                    "eg": fnum(i.get("earningsGrowth")), "de": fnum(i.get("debtToEquity")), "margin": fnum(i.get("profitMargins")), "price": fnum(i.get("currentPrice") or i.get("regularMarketPrice"))}
        except Exception as exc:  # noqa: BLE001
            if "Rate" in repr(exc) or "429" in repr(exc):
                time.sleep(8 * (attempt + 1))
                continue
            return None
    return None


def held_share(fund_values: dict, symbol: str) -> float:
    """What share of your portfolio sits in this stock through your funds (fund value x its weight there)."""
    tot = sum(fund_values.values()) or 1.0
    amt = 0.0
    for fid, val in fund_values.items():
        for h in load_holdings(fid).get("holdings") or []:
            if str(h["ticker"]).replace(".NS", "") == symbol:
                amt += val * float(h["weight_pct"]) / 100
    return amt / tot * 100


def stock_picks() -> dict | None:
    uni = nifty500()
    if len(uni) < 50:
        return None
    syms = list(uni["symbol"])
    px = yf.download([s + ".NS" for s in syms], period="14mo", interval="1d", group_by="ticker", progress=False, threads=True, auto_adjust=False, timeout=90)
    rows = []
    for s in syms:                                                      # price-based tests first: they need no per-stock request
        try:
            c, v = px[s + ".NS"]["Close"].dropna(), px[s + ".NS"]["Volume"].dropna()
        except Exception:  # noqa: BLE001
            continue
        if len(c) < 200:
            continue
        last = float(c.iloc[-1])
        rows.append({"symbol": s, "last": last, "m6": (last / float(c.iloc[-126]) - 1) * 100, "m12_1": (float(c.iloc[-21]) / float(c.iloc[-252]) - 1) * 100 if len(c) > 252 else np.nan,
                     "dma200": float(c.tail(200).mean()), "hi": float(c.tail(252).max()), "value_cr": float((c.tail(60) * v.tail(60)).mean()) / 1e7})
    pre = pd.DataFrame(rows)
    n_all = len(pre)
    pre = pre[(pre["last"] > pre["dma200"]) & (pre["m6"] > 0) & (pre["last"] >= 0.70 * pre["hi"]) & (pre["value_cr"] >= 3)]
    print(f"[info] stocks: {n_all} priced; {len(pre)} are in an uptrend, near their highs and liquid: fetching fundamentals for those")
    with ThreadPoolExecutor(max_workers=4) as pool:
        info = [x for x in pool.map(stock_info, list(pre["symbol"])) if x]
    df = pre.merge(pd.DataFrame(info), on="symbol").merge(uni, on="symbol")
    df["price"] = df["price"].fillna(df["last"])
    df["eg"] = df["eg"].clip(upper=1.0)                                 # one-off base effects (+900%) must not dominate the growth rank
    df["rg"] = df["rg"].clip(upper=1.0)
    df["mcap_cr"] = df["mcap"] / 1e7
    fin = df["industry"].fillna("").str.contains("Financial", case=False)
    ok = (df["mcap_cr"].between(2000, 80000) & df["pe"].between(0.1, 100) & ((df["roe"] >= 0.12) | (df["roe"].isna() & (df["margin"] >= 0.08))) & ((df["de"].fillna(0) <= 120) | fin) & (df["rg"].fillna(0) >= 0.10) & (df["eg"].fillna(0) >= 0.10))
    print(f"[info] stocks: fundamentals for {len(df)}; passing size {int(df['mcap_cr'].between(2000, 80000).sum())}, roe {int((df['roe'] >= 0.12).sum())} (missing {int(df['roe'].isna().sum())}), "
          f"revenue growth {int((df['rg'].fillna(0) >= 0.10).sum())}, earnings growth {int((df['eg'].fillna(0) >= 0.10).sum())}, ALL {int(ok.sum())}")
    df = df[ok].copy()
    if len(df) < PICKS:
        return None
    df["peg"] = (df["pe"] / (df["eg"].clip(lower=0.05) * 100)).clip(0.1, 5)
    df["score"] = (0.30 * (pct_rank(df["eg"]) + pct_rank(df["rg"])) / 2 + 0.20 * (pct_rank(df["roe"]) + pct_rank(df["margin"])) / 2
                   + 0.25 * (pct_rank(df["m6"]) + pct_rank(df["m12_1"])) / 2 + 0.15 * pct_rank(-df["peg"]) + 0.10 * pct_rank(-np.log(df["mcap_cr"]))) * 100
    state = read_json("state.json", {})
    fund_values = {f["id"]: (state.get(f["id"], {}).get("units") or 0) * (state.get(f["id"], {}).get("last_nav") or 0) for f in load_portfolio()["funds"]}
    out = []
    for _, r in df.sort_values("score", ascending=False).head(PICKS).iterrows():
        near = (1 - r["last"] / r["hi"]) * 100
        why = [f"Earnings growing {'100%+' if r['eg'] >= 1 else format(r['eg'] * 100, '.0f') + '%'} and revenue {'100%+' if r['rg'] >= 1 else format(r['rg'] * 100, '.0f') + '%'} a year",
               (f"Return on equity {r['roe'] * 100:.0f}%, profit margin " if pd.notna(r["roe"]) else "Profit margin ") + f"{r['margin'] * 100:.0f}%" + (f", debt/equity {r['de'] / 100:.2f}" if pd.notna(r["de"]) and not pd.isna(r["de"]) else ""),
               f"Valuation: P/E {r['pe']:.0f}, about {r['peg']:.1f}x its earnings growth (lower is cheaper for the growth)",
               f"Price {r['m6']:+.0f}% in six months, above its 200-day average and {near:.0f}% below its 52-week high: the market is already agreeing",
               f"Market cap about ₹{r['mcap_cr']:,.0f} cr: " + ("small/mid size, room to grow" if r["mcap_cr"] < 30000 else "mid/large size")]
        risk = ["Small and mid-cap stocks can fall 40-60% in a weak market, and quickly"]
        if r["pe"] > 40:
            risk.append(f"P/E of {r['pe']:.0f} already prices in a lot of growth")
        if r["m6"] > 60:
            risk.append(f"Up {r['m6']:.0f}% in six months: a pullback is likely at some point")
        risk.append("Yahoo's fundamentals can lag the latest quarterly results by weeks: read the filings before acting")
        mine = held_share(fund_values, r["symbol"])
        out.append({"symbol": r["symbol"], "name": r["name"], "industry": r["industry"], "price": round(r["price"], 2), "mcap_cr": round(r["mcap_cr"]), "score": round(float(r["score"]), 1),
                    "m6": round(r["m6"], 1), "pe": round(r["pe"], 1), "roe_pct": None if pd.isna(r["roe"]) else round(r["roe"] * 100, 1), "eg_pct": round(r["eg"] * 100, 1), "held_pct": round(mine, 2), "why": why[:4], "risk": risk[:3]})
    return {"picks": out, "screened": n_all, "eligible": int(len(df)), "prices": {r["symbol"]: round(r["last"], 2) for r in rows}}


# ------------------------------------------------------------------------------------------------ track record
def last_close(ticker: str) -> float | None:
    try:
        c = yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=False, timeout=30)["Close"].dropna()
        return float(c.iloc[-1].iloc[0] if hasattr(c.iloc[-1], "iloc") else c.iloc[-1])
    except Exception:  # noqa: BLE001
        return None


def track_record(history: list[dict], crypto_now: dict, stock_now: dict, nifty_now: float | None, btc_now: float | None) -> dict:
    """Every earlier week's picks, marked to today's prices against Bitcoin / the Nifty 500 over the same stretch."""
    rows = []
    for h in history:
        try:
            age = (now_ist().date() - dt.date.fromisoformat(str(h.get("generated_at") or h["week_of"])[:10])).days
        except (KeyError, ValueError):
            continue
        for kind, key, now, bench_then, bench_now, bname in (("crypto", "crypto", crypto_now, (h.get("bench") or {}).get("btc"), btc_now, "Bitcoin"),
                                                           ("stock", "stocks", stock_now, (h.get("bench") or {}).get("nifty500"), nifty_now, "Nifty 500")):
            rets = []
            for p in h.get(key, []):
                cur = now.get(p.get("id") or p.get("symbol"))
                if cur and p.get("price"):
                    rets.append((cur / p["price"] - 1) * 100)
            if rets and bench_then and bench_now:
                bench = (bench_now / bench_then - 1) * 100
                rows.append({"week_of": h["week_of"], "kind": kind, "age_days": age, "avg_pct": round(float(np.mean(rets)), 1), "bench_pct": round(bench, 1), "bench": bname,
                             "beat": int(sum(r > bench for r in rets)), "n": len(rets)})
    mature = [r for r in rows if r["age_days"] >= 28]
    summ = {}
    for kind in ("crypto", "stock"):
        m = [r for r in mature if r["kind"] == kind]
        if m:
            summ[kind] = {"weeks": len(m), "avg_pct": round(float(np.mean([r["avg_pct"] for r in m])), 1), "bench_pct": round(float(np.mean([r["bench_pct"] for r in m])), 1),
                          "beat": int(sum(r["beat"] for r in m)), "n": int(sum(r["n"] for r in m))}
    return {"rows": sorted(rows, key=lambda r: (r["week_of"], r["kind"]))[-24:], "summary": summ}


# ------------------------------------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--crypto", action="store_true")
    ap.add_argument("--stocks", action="store_true")
    args = ap.parse_args()
    both = not (args.crypto or args.stocks)
    ts = now_ist()
    prev = read_json("picks.json", {}) or {}
    history = read_json("picks_history.json", []) or []

    crypto = crypto_picks({}) if (both or args.crypto) else None
    stocks = stock_picks() if (both or args.stocks) else None
    if not crypto and not stocks:
        sys.exit("[error] neither screen produced picks; nothing written")

    week_of = (ts.date() - dt.timedelta(days=ts.weekday())).isoformat()            # the Monday of this week
    nifty_now = last_close("^CRSLDX")
    btc_now = (crypto or {}).get("bitcoin", {}).get("price")
    crypto_now = (crypto or {}).get("prices", {})
    stock_now = (stocks or {}).get("prices", {})
    # earlier picks that fell out of the screened universe still need a price to be scored
    need_c = {p["id"] for h in history for p in h.get("crypto", [])} - set(crypto_now)
    if need_c:
        extra = cg("/simple/price", ids=",".join(sorted(need_c)), vs_currencies="usd") or {}
        crypto_now = {**crypto_now, **{k: v.get("usd") for k, v in extra.items()}}
    for h in history:
        for p in h.get("stocks", []):
            if p["symbol"] not in stock_now:
                px = last_close(p["symbol"] + ".NS")
                if px:
                    stock_now[p["symbol"]] = px
    record = track_record([h for h in history if h.get("week_of") != week_of], crypto_now, stock_now, nifty_now, btc_now)

    snap = {"week_of": week_of, "generated_at": ts.isoformat(),
            "crypto": [{"id": p["id"], "symbol": p["symbol"], "price": p["price_usd"]} for p in (crypto or {}).get("picks", [])],
            "stocks": [{"symbol": p["symbol"], "price": p["price"]} for p in (stocks or {}).get("picks", [])],
            "bench": {"btc": btc_now, "nifty500": nifty_now}}
    history = [h for h in history if h.get("week_of") != week_of] + [snap]
    write_json("picks_history.json", history[-HISTORY_KEEP:])

    def strip(d):
        return None if not d else {k: v for k, v in d.items() if k not in ("prices", "majors")}
    write_json("picks.json", {"generated_at": ts.isoformat(), "week_of": week_of,
                              "crypto": strip(crypto) or prev.get("crypto"), "stocks": strip(stocks) or prev.get("stocks"), "track_record": record,
                              "method": {"crypto": "Top 150 coins by size, minus stablecoins, wrapped/staked copies and memecoins; market cap over $1B; liquid; not in free fall. Ranked on strength against Bitcoin over 200 and 30 days (55%), smallness (15%), distance below the all-time high (15%), liquidity (10%), calm last week (5%); coins that are already vertical lose points.",
                                         "stocks": "Nifty 500 stocks with market cap ₹2,000-80,000 cr, tradable (₹3 cr+ a day), profitable with a P/E under 100, return on equity 12%+ (or, where Yahoo has no figure, a 8%+ profit margin), modest debt, earnings and revenue growing 10%+ (growth capped at 100% so one-off jumps don't dominate), above the 200-day average, up over six months and within 30% of the 52-week high. Ranked on growth (30%), momentum (25%), quality (20%), growth-adjusted valuation (15%) and smallness (10%)."}})
    if crypto and crypto.get("majors"):
        write_json("crypto.json", {"generated_at": ts.isoformat(), "majors": crypto["majors"]})
    for p in (crypto or {}).get("picks", []):
        print(f"[crypto] {p['symbol']:<8} score {p['score']:>5}  200d {p['d200']:+.0f}%  mcap {human(p['mcap_usd'])}")
    for p in (stocks or {}).get("picks", []):
        print(f"[stock ] {p['symbol']:<12} score {p['score']:>5}  6m {p['m6']:+.0f}%  P/E {p['pe']}  mcap ₹{p['mcap_cr']:,} cr")


if __name__ == "__main__":
    main()
