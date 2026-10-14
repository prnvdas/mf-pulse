// Loaded into every suite (node --require): every browser context gets a canned CoinGecko answer, so the ticker's
// Bitcoin/Ethereum prices are the same on every run and no test depends on the real internet.
const pw = require('playwright');
const origLaunch = pw.chromium.launch.bind(pw.chromium);
pw.chromium.launch = async (...a) => {
  const b = await origLaunch(...a), nc = b.newContext.bind(b);
  b.newContext = async (...x) => {
    const c = await nc(...x);
    await c.routeWebSocket(/streamer\.finance\.yahoo\.com/, ws => ws.close());                // suites that test the stream add their own handler
    await c.route(/fonts\.(googleapis|gstatic)\.com/, r => r.fulfill({ status: 200, contentType: 'text/css', body: '' }));   // no web fonts: same layout every run, nothing to wait for
    await c.route(/priceapi\.moneycontrol\.com/, r => r.abort());       // suites that test the Sensex feed add their own
    await c.route(/api\.allorigins\.win/, r => r.abort());          // suites that test live quotes add their own, later (and so winning) route
    await c.route(/api\.coingecko\.com/, r => r.fulfill({ status: 200, contentType: 'application/json', headers: { 'access-control-allow-origin': '*' },
      body: JSON.stringify({ bitcoin: { usd: 83000, usd_24h_change: 0.5 }, ethereum: { usd: 2500, usd_24h_change: -0.3 } }) }));
    return c;
  };
  return b;
};
