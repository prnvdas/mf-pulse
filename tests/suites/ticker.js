// Live index/Brent quotes pulled by the page itself, and honest labels for a feed that hasn't traded today.
const { chromium } = require('playwright');
const URL = process.env.BASE; let n = 0, fail = 0; const ok = (c, name, d) => { n++; if (!c) { fail++; console.log('FAIL', name, d || ''); } };
(async () => {
  const b = await chromium.launch();
  // `quotes`: ticker -> {price, prev, t (ISO)}; anything else gets a 404
  async function page(iso, quotes, mode) {
    const ctx = await b.newContext({ viewport: { width: 1200, height: 900 } }), p = await ctx.newPage(), errs = [];
    p.on('pageerror', e => errs.push(e.message));
    await p.clock.install({ time: new Date(iso) });
    await ctx.route(/api\.allorigins\.win/, r => {
      if (mode === 'abort') return r.abort();
      if (mode === 'garbage') return r.fulfill({ status: 200, contentType: 'application/json', headers: { 'access-control-allow-origin': '*' }, body: 'not json' });
      const u = decodeURIComponent(new URL2(r.request().url()).searchParams.get('url') || ''), tk = decodeURIComponent((u.match(/chart\/([^?]+)/) || [])[1] || ''), q = quotes[tk];
      if (!q) return r.fulfill({ status: 404, body: '' });
      r.fulfill({ status: 200, contentType: 'application/json', headers: { 'access-control-allow-origin': '*' },
        // chartPreviousClose is a deliberate decoy (Yahoo's own field is unreliable): the page must use the daily bars
        body: JSON.stringify({ chart: { result: [(() => { const t = Math.floor(Date.parse(q.t) / 1000); return { meta: { regularMarketPrice: q.price, chartPreviousClose: 123456, regularMarketTime: t, gmtoffset: 19800 },
          timestamp: [t - 3 * 86400, t - 86400, t], indicators: { quote: [{ close: [q.prev * 0.9, q.prev, q.price] }] } }; })()] } }) });
    });
    await p.goto(URL, { waitUntil: 'networkidle' }); await p.waitForTimeout(500); await p.clock.fastForward(3000); await p.waitForTimeout(500);
    return { p, ctx, errs };
  }
  const URL2 = globalThis.URL, tape = p => p.$eval('#tkRun', e => e.innerText.replace(/\n/g, ' ')), label = p => p.$eval('#tkLab', e => e.innerText);
  const OPEN = '2026-10-13T05:30:00Z';                                  // Tue 11:00 IST
  // fresh quotes win over the batch and the tape says LIVE
  { const { p, ctx, errs } = await page(OPEN, { '^NSEI': { price: 22500, prev: 22400, t: '2026-10-13T05:29:00Z' }, '^BSESN': { price: 72000, prev: 71924.68, t: '2026-10-13T05:28:00Z' } });
    const t = await tape(p); ok(/Nifty 50\s+22,500/.test(t) && /\+0\.45%/.test(t), 'fresh Nifty quote shown', t.slice(0, 300)); ok(/Sensex\s+72,000/.test(t) && !/prev close/.test(t.split('Sensex')[1].slice(0, 40)), 'fresh Sensex quote shown');
    ok(/LIVE/.test(await label(p)), 'tape says LIVE', await label(p)); ok(!errs.length, 'live errors', errs[0]); await ctx.close(); }
  // an index whose feed last traded yesterday is labelled, not passed off as live
  { const { p, ctx } = await page(OPEN, { '^NSEI': { price: 22500, prev: 22400, t: '2026-10-13T05:29:00Z' }, '^BSESN': { price: 71924.68, prev: 71924.68, t: '2026-10-12T10:02:00Z' } }, undefined);
    const t = await tape(p); ok(/Sensex \(prev close\)/.test(t), 'stale Sensex feed labelled (prev close)', t.slice(0, 300)); await ctx.close(); }
  // the relay is down: the batch numbers stay, nothing breaks
  for (const mode of ['abort', 'garbage']) { const { p, ctx, errs } = await page(OPEN, {}, mode); const t = await tape(p);
    ok(/Sensex/.test(t) && !/NaN|undefined/.test(t) && !errs.length, 'relay ' + mode + ' leaves the batch tape', errs[0] || t.slice(0, 120)); ok(!/LIVE/.test(await label(p)), 'no LIVE tag without live quotes'); await ctx.close(); }
  // hostile quote values are ignored
  { const { p, ctx, errs } = await page(OPEN, { '^NSEI': { price: 0, prev: 22400, t: '2026-10-13T05:29:00Z' }, '^BSESN': { price: null, prev: 1, t: 'x' } });
    const t = await tape(p); ok(/Nifty 50/.test(t) && !/NaN|undefined|Infinity/.test(t) && !errs.length, 'hostile quotes ignored', errs[0] || t.slice(0, 200)); await ctx.close(); }
  // Saturday: indices are not refreshed, Brent is
  { const { p, ctx } = await page('2026-10-10T05:30:00Z', { '^NSEI': { price: 99999, prev: 22400, t: '2026-10-10T05:29:00Z' }, 'BZ=F': { price: 111.11, prev: 110, t: '2026-10-10T05:25:00Z' } });
    const t = await tape(p); ok(!/99,999/.test(t), 'indices not refreshed on a weekend'); ok(/Brent crude\s+\$111\.11/.test(t), 'Brent still refreshed', t.slice(0, 160)); ok(!/prev close/.test(t), 'no prev-close label on a weekend'); await ctx.close(); }
  console.log(`${n - fail}/${n} passed`); await b.close(); process.exit(fail ? 1 : 0);
})();
