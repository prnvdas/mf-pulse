/**
 * MF Pulse price proxy.
 *
 * Two sources, tried in order:
 *
 *   1. NSE (www.nseindia.com/api/...) — real-time, and crucially it has BULK
 *      endpoints: one call to equity-stockIndices returns every constituent of
 *      an index with its % change. Three calls cover ~750 stocks. This is by
 *      far the best source when it works.
 *
 *   2. Yahoo chart API — 15-min delayed, one request per symbol, but reliable
 *      from datacenter IPs. Used only for symbols NSE didn't return.
 *
 * The NSE caveat: it requires a cookie handshake (hit the homepage first, reuse
 * the Set-Cookie), a browser-shaped User-Agent, and a Referer. Even with all
 * that, NSE blocks datacenter ranges aggressively and Cloudflare's egress IPs
 * are a common target. Treat NSE success as a bonus, not a guarantee — the
 * response tells you which source each quote came from so you can see what's
 * actually happening.
 *
 * GET /?symbols=RELIANCE.NS,HDFCBANK.NS,^NSEMDCP50
 * -> { quotes: { "RELIANCE.NS": { prev, last, pct, src } }, sources: {...} }
 */

const NSE = "https://www.nseindia.com";
const YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/";
const MAX_SYMBOLS = 120;

// Broadest first — NIFTY TOTAL MARKET alone covers most of what an equity
// fund can hold. The rest are cheap insurance for names outside it.
const BULK_INDICES = [
  "NIFTY TOTAL MARKET",
  "NIFTY SMALLCAP 250",
  "NIFTY MIDCAP 150",
];

// Yahoo index tickers -> the names NSE uses in /api/allIndices
const INDEX_ALIASES = {
  "^NSEI": "NIFTY 50",
  "^CNXSC": "NIFTY SMALLCAP 100",
  "^NSEMDCP50": "NIFTY MIDCAP 100",
  "^CRSLDX": "NIFTY 500",
};

const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  "Accept": "application/json, text/plain, */*",
  "Accept-Language": "en-US,en;q=0.9",
  "Referer": NSE + "/market-data/live-equity-market",
};

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const toNseSymbol = t => (t.endsWith(".NS") ? t.slice(0, -3) : null);

// --- NSE ------------------------------------------------------------------

/** Hit the homepage to collect the session cookies NSE's API insists on. */
async function nseCookies() {
  const res = await fetch(NSE + "/", {
    headers: { ...BROWSER_HEADERS, Accept: "text/html,application/xhtml+xml" },
    redirect: "follow",
  });
  const raw = res.headers.getAll
    ? res.headers.getAll("set-cookie")
    : [res.headers.get("set-cookie")].filter(Boolean);

  return raw
    .map(c => c.split(";")[0])
    .filter(Boolean)
    .join("; ");
}

async function nseJson(path, cookie) {
  const res = await fetch(NSE + path, {
    headers: { ...BROWSER_HEADERS, Cookie: cookie },
    cf: { cacheTtl: 30, cacheEverything: true },
  });
  if (!res.ok) throw new Error(`nse ${res.status} on ${path}`);
  return res.json();
}

/**
 * Pull bulk index constituents plus the index levels themselves.
 * Returns { equities: {SYMBOL: {...}}, indices: {"NIFTY 50": {...}} }.
 */
async function fetchNse() {
  const cookie = await nseCookies();
  if (!cookie) throw new Error("nse: no session cookie");

  const equities = {};
  const indices = {};

  const jobs = BULK_INDICES.map(name =>
    nseJson(`/api/equity-stockIndices?index=${encodeURIComponent(name)}`, cookie)
      .then(j => {
        for (const row of j?.data || []) {
          const sym = row.symbol;
          // The index row itself is included in `data` — skip it.
          if (!sym || sym === name || row.lastPrice == null) continue;
          if (equities[sym]) continue;            // first (broadest) index wins
          const last = Number(row.lastPrice);
          const prev = Number(row.previousClose ?? row.prevClose);
          const pct = row.pChange != null ? Number(row.pChange)
            : (prev ? (last - prev) / prev * 100 : null);
          if (pct == null || !isFinite(pct)) continue;
          equities[sym] = { last, prev, pct, src: "nse" };
        }
      })
      .catch(() => {})                            // one bad index shouldn't kill it
  );

  jobs.push(
    nseJson("/api/allIndices", cookie)
      .then(j => {
        for (const row of j?.data || []) {
          if (row.index == null || row.percentChange == null) continue;
          indices[row.index] = {
            last: Number(row.last),
            prev: Number(row.previousClose),
            pct: Number(row.percentChange),
            src: "nse",
          };
        }
      })
      .catch(() => {})
  );

  await Promise.all(jobs);
  return { equities, indices };
}

// --- Yahoo fallback -------------------------------------------------------

async function yahooQuote(symbol) {
  const res = await fetch(
    `${YAHOO}${encodeURIComponent(symbol)}?range=5d&interval=1d`,
    { headers: { "User-Agent": BROWSER_HEADERS["User-Agent"] },
      cf: { cacheTtl: 45, cacheEverything: true } }
  );
  if (!res.ok) return [symbol, null];

  const result = (await res.json())?.chart?.result?.[0];
  const closes = result?.indicators?.quote?.[0]?.close;
  if (!Array.isArray(closes)) return [symbol, null];

  // meta.chartPreviousClose is unreliable with range=5d — it can reflect the
  // close from before the whole requested window (e.g. 4 sessions back across
  // a weekend) rather than the immediately prior session. Read the last two
  // valid daily closes from the series instead, same as estimate.py does via
  // yfinance's own OHLC history.
  const valid = closes.filter(c => c != null);
  if (valid.length < 2) return [symbol, null];
  const prev = valid[valid.length - 2];
  const last = valid[valid.length - 1];
  if (!prev || !last) return [symbol, null];

  return [symbol, { last, prev, pct: (last - prev) / prev * 100, src: "yahoo" }];
}

// --- handler --------------------------------------------------------------

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url = new URL(request.url);
    const symbols = (url.searchParams.get("symbols") || "")
      .split(",").map(s => s.trim()).filter(Boolean);

    if (!symbols.length) {
      return Response.json({ error: "pass ?symbols=A.NS,B.NS" },
        { status: 400, headers: CORS });
    }
    if (symbols.length > MAX_SYMBOLS) {
      return Response.json({ error: `max ${MAX_SYMBOLS} symbols per call` },
        { status: 400, headers: CORS });
    }

    const quotes = {};
    let nseError = null;

    // 1. bulk NSE
    if (url.searchParams.get("nse") !== "0") {
      try {
        const { equities, indices } = await fetchNse();
        for (const t of symbols) {
          if (t.startsWith("^")) {
            const hit = indices[INDEX_ALIASES[t] || ""];
            if (hit) quotes[t] = hit;
          } else {
            const hit = equities[toNseSymbol(t) || ""];
            if (hit) quotes[t] = hit;
          }
        }
      } catch (err) {
        nseError = String(err.message || err);
      }
    }

    // 2. Yahoo for whatever NSE missed
    const missing = symbols.filter(s => !quotes[s]);
    if (missing.length) {
      const settled = await Promise.allSettled(
        missing.slice(0, 45).map(yahooQuote));
      for (const s of settled) {
        if (s.status === "fulfilled" && s.value[1]) quotes[s.value[0]] = s.value[1];
      }
    }

    const counts = { nse: 0, yahoo: 0 };
    for (const q of Object.values(quotes)) counts[q.src]++;

    return Response.json({
      quotes,
      resolved: Object.keys(quotes).length,
      requested: symbols.length,
      sources: counts,
      nse_error: nseError,
    }, { headers: { ...CORS, "Cache-Control": "public, max-age=30" } });
  },
};
