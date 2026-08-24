# MF Pulse

A one-glance answer to "is my mutual fund portfolio red or green today?" — hours
before AMFI publishes the actual NAV.

## Why this shape

Two ways the number gets computed, because they solve different problems:

- **Refresh live** (the button) — the browser prices every holding through a
  Cloudflare Worker and runs the inference itself. Two seconds, on demand, whenever
  you want to look. This is the primary path.
- **GitHub Actions cron** — the same math server-side every 15 min, so the page has
  a number waiting even before you press anything, and so the nightly reconciler can
  score the estimate against published NAV.

Both run the identical model. `src/estimate.py` and the `inferFund()` function in
`docs/index.html` are line-for-line equivalent — verified to agree to three decimals.

Hosting is GitHub Pages serving `docs/`: static, always up, no cold start, no server
to keep alive. Prices come through a Cloudflare Worker. Both free tiers, permanently.

Two caveats worth knowing up front:

1. GitHub's scheduler is best-effort — a run can lag 5–20 minutes under load. Fine
   for estimating an end-of-day number; useless for trading.
2. GitHub disables scheduled workflows in repos with **60 days of no user activity**.
   Bot commits don't always reset that timer. Push something once a month, or set a
   calendar nudge.

If the lag ever bothers you, Cloudflare Workers + Cron Triggers + KV is the upgrade:
precise scheduling, also genuinely free.

**Deployment is in [DEPLOY.md](DEPLOY.md)** — three paths, none needing a terminal.

## Setup

```bash
git clone <your-repo> && cd mf-pulse
pip install -r requirements.txt
```

**1. Fix the AMFI scheme codes.** These are the only values that must be exactly right.

```bash
curl -s https://www.amfiindia.com/spages/NAVAll.txt | grep -i "axis small cap.*direct.*growth"
```

The first field is the code. Put it in `config/portfolio.yaml`. Repeat for all three.
JioBlackRock is new, so search loosely.

**2. Load real holdings.** The seed file for Mirae has 20 names covering ~39% of NAV,
which is why the sample run shows a ±₹21k band. Axis and Jio have none at all.
Download each AMC's monthly portfolio disclosure XLSX and import it:

```bash
python src/import_holdings.py --fund axis-small-cap --xlsx axis-portfolio-jul26.xlsx
```

The importer maps ISIN → NSE symbol using NSE's equity master, so name-spelling
differences don't matter. It prints coverage and lists anything it couldn't map
(unlisted names, debt, BSE-only) so you can see exactly what's missing.

Do this once a month. It's the single biggest lever on accuracy.

**3. Set your SIP dates.** `config/portfolio.yaml` has your ₹1.1L split across the
three funds on the 5th. Correct the split and dates — wrong SIP data means units
drift and the P&L is quietly wrong from then on.

**4. First run.**

```bash
python src/reconcile.py       # pulls NAV, back-solves your units
python src/estimate.py --force
python -m http.server -d docs 8000
```

**5. Pick a price source.** Browsers can't call Yahoo *or* Google Finance directly —
CORS blocks both, and Google's Finance API has been dead since 2012. Two ways round it.

*Option A — Google Sheets (no deploy).* `GOOGLEFINANCE()` still works and covers NSE.

```bash
python src/make_sheet.py > prices.tsv
```

New sheet, rename the tab to `Prices`, paste at A1, then File → Share → Publish to web
→ Prices → CSV. Paste that URL into the dashboard under **Price source → Google Sheet**.

Two things to watch. Google recalculates GOOGLEFINANCE on its own schedule (~20 min),
so pressing Refresh gets you Google's last computed value, not a fresh quote. And thin
NSE smallcaps often come back `#N/A` — the dashboard drops those rows silently, which
quietly lowers your coverage on Axis Small Cap. Check the sheet for blanks after
publishing.

*Option B — Cloudflare Worker (recommended).* Tries NSE first, falls back to Yahoo.

```bash
npm install -g wrangler
cd worker && wrangler login && wrangler deploy
```

You get back a `*.workers.dev` URL. Open the dashboard, paste it into the box under
**Price source**, and press **Refresh live**. The URL saves into the page hash, so
bookmark it afterwards and it sticks.

### Why NSE first

NSE has bulk endpoints. `/api/equity-stockIndices?index=NIFTY%20TOTAL%20MARKET` returns
~750 stocks with their % change in **one response**, and `/api/allIndices` returns every
benchmark in one more. Three calls cover essentially anything an Indian equity fund can
hold — versus ~90 separate Yahoo requests. It's also real-time rather than 15-minute
delayed, and it quotes the illiquid smallcaps that Google and Yahoo are patchy on.

Getting in requires a cookie handshake (hit the homepage, reuse the `Set-Cookie`), a
browser-shaped `User-Agent`, and a `Referer`. The Worker does all of that.

**It may still not work.** NSE blocks datacenter IP ranges aggressively and Cloudflare's
egress is a common target. If NSE 403s, the Worker falls back to Yahoo per-symbol and the
dashboard tells you so — the header stamp reads `NSE ·` or `Yahoo ·`, and the hero line
breaks down the mix, e.g. *"inferred from 71 stock prices (68 real-time via NSE, 3 delayed
via Yahoo)"*. There's nothing to fix if it fails; it either works from your Worker's edge
location or it doesn't.

Two other things worth knowing: these endpoints are undocumented and can change without
notice, and NSE's site terms discourage automated access. A single-user dashboard polling
once a minute is negligible load, but it's their call, not mine. `?nse=0` on the Worker
URL disables the NSE path entirely if you'd rather not.

### Cost

Free tier is 100k requests/day. A full refresh across ~90 holdings costs 3 requests,
so you'd have to hammer the button rather hard to notice.

Worth running both for a week and comparing how many tickers each resolves. The
dashboard prints `42/68 stocks priced` per fund, which is the number to watch.

**6. Deploy the page.** Push to GitHub → Settings → Pages → source `main` / `docs`. Then
Settings → Actions → General → Workflow permissions → **Read and write**. Trigger
`estimate` manually once to confirm.

Make the repo **private** — it contains your holdings and unit counts. Private repos
get 2,000 Actions minutes/month free; these jobs use roughly 300.

## What's in the box

```
src/estimate.py         weighted-holdings NAV estimate -> docs/data/latest.json
src/reconcile.py        nightly AMFI pull, unit tracking, scores yesterday's guess
src/import_holdings.py  AMC portfolio XLSX -> holdings YAML, via ISIN
src/export_web.py       YAML -> docs/data/config.json so the browser can infer
src/make_sheet.py       holdings -> GOOGLEFINANCE formulas for a Google Sheet
worker/worker.js        Cloudflare Worker: NSE bulk + Yahoo fallback, with CORS
docs/index.html         the dashboard — Refresh live + Auto 60s
```

## Aggregation vs extrapolation

Worth being precise, because it decides how much to trust the output.

For every stock you actually hold and have imported, nothing is predicted — it's a
weighted sum of known live prices. Arithmetic.

The extrapolation is the **tail**: the share of each fund whose holdings aren't in your
config, modelled with the benchmark index. At 90% coverage you're doing arithmetic on
nearly all of it. At 38% you're doing arithmetic on a third and inferring the rest from
an index that may not resemble what the fund actually owns.

That's the whole reason `coverage_pct` sits on every row and the error band scales off it.

## The model

```
nav_move = tracked_weight × weighted_move(holdings)
         + tail_weight    × benchmark_index_move
         + cash_weight    × 0
         − TER ÷ 365
```

`coverage_pct` on each row is the honest bit: it's `tracked_weight`, and the error
band scales inversely with it. A 90%-covered fund gets a tight band; a 40%-covered
one gets a wide one and says so.

## The accuracy panel

Every night `reconcile.py` compares the 15:30 estimate against the NAV AMFI actually
published and appends the result to `docs/data/history.json`. After ~10 sessions the
dashboard shows measured MAE, direction-hit rate, and worst miss.

Trust that panel over the headline number. If direction accuracy sits above 90% and
MAE under 0.20%, the estimate is doing its job. If MAE is above 0.40% on a fund, its
holdings file is stale — reimport.

## Roadmap

- **v2** — per-fund drill-down, sector attribution, XIRR recompute from the transaction log
- **v2.5** — overlap analysis across Axis and Mirae (worth knowing, given the ~50/49 split)
- **v3** — Telegram push at 15:35 with the day's number
- **v4** — overlap analysis across the three funds (worth knowing, given the concentration)

---

Estimates only. Not investment advice, and a ±0.3% figure is not a basis for timing anything.
