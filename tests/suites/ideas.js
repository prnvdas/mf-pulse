// Weekly ideas section and the Bitcoin/Ethereum ticker items.
const { chromium } = require('playwright');
const URL = process.env.BASE; let n = 0, fail = 0; const ok = (c, name, d) => { n++; if (!c) { fail++; console.log('FAIL', name, d || ''); } };
(async () => {
  const b = await chromium.launch();
  async function page(routes = {}, iso = '2026-10-12T08:30:00Z', w = 1200, extra) {
    const ctx = await b.newContext({ viewport: { width: w, height: 900 } }), p = await ctx.newPage(), errs = [];
    p.on('pageerror', e => errs.push(e.message));
    await p.clock.install({ time: new Date(iso) });
    if (extra) await extra(ctx);
    for (const [f, fn] of Object.entries(routes)) await p.route(new RegExp('/data/' + f + '\\.json'), async r => { const j = await (await r.fetch()).json(); r.fulfill({ json: fn(j) }); });
    await p.goto(URL, { waitUntil: 'networkidle' }); await p.waitForTimeout(400); await p.clock.fastForward(3000); await p.waitForTimeout(300);
    return { p, ctx, errs };
  }
  const tick = p => p.$eval('#tkRun', e => e.innerText);
  // --- section
  { const { p, ctx, errs } = await page();
    ok(await p.$eval('#ideasWrap', e => !e.hidden), 'ideas visible');
    ok(await p.$$eval('#ideasBody .pick', a => a.length) === 5, 'five crypto picks');
    ok(/Bitcoin/.test(await p.$eval('#ideasRecord', e => e.innerText)) || /Building/.test(await p.$eval('#ideasRecord', e => e.innerText)), 'track record shown');
    ok(/30% with no set-off/.test(await p.$eval('#ideasBody', e => e.innerText)), 'crypto tax note');
    await p.click('#ideasSeg button[data-k=stocks]'); ok(await p.$$eval('#ideasBody .pick', a => a.length) === 5, 'five stock picks');
    ok(/Watch out/i.test(await p.$eval('#ideasBody', e => e.innerText)) && /How these were chosen/.test(await p.$eval('#ideasBody', e => e.innerText)), 'risks and method shown');
    ok(/not a prediction/i.test(await p.$eval('#ideasWrap', e => e.innerText)), 'disclaimer shown');
    ok(!/undefined|NaN|\[object/.test(await p.$eval('#ideasWrap', e => e.innerText)), 'no undefined in ideas');
    await p.click('#snavIn button:nth-child(7)'); await p.waitForTimeout(1200); ok(await p.$eval('#ideasWrap', e => e.getBoundingClientRect().top < 400), 'menu jumps to ideas');
    ok(!errs.length, 'ideas errors', errs[0]); await ctx.close(); }
  // --- hostile data
  for (const [m, fn] of Object.entries({
    nopicks: j => { j.crypto.picks = []; j.stocks.picks = []; return j; },
    nulls: j => { j.crypto.picks.forEach(p => { p.price_usd = null; p.d30 = null; p.why = null; p.risk = 'x'; p.score = 'abc'; }); j.stocks = null; return j; },
    strings: j => { j.crypto = { picks: ['x', 5, null] }; j.track_record = 'x'; return j; },
    badtrack: j => { j.track_record = { rows: [null, { kind: 'crypto' }], summary: { crypto: { avg_pct: null } } }; return j; },
    list: () => [] })) {
    const { p, ctx, errs } = await page({ picks: fn });
    const t = await p.evaluate(() => document.body.innerText);
    ok(!errs.length && !/\bNaN\b|undefined|\[object/.test(t), 'picks ' + m, errs[0] || (t.match(/.{20}(NaN|undefined|\[object).{20}/) || [])[0]); await ctx.close(); }
  // --- ticker
  { const { p, ctx } = await page(); const t = await tick(p);
    ok(/Bitcoin\s*\$83,000/.test(t) && /Ethereum\s*\$2,500/.test(t), 'bitcoin and ethereum in ticker', t.slice(0, 200));
    ok(t.indexOf('Brent') < t.indexOf('Bitcoin') || t.indexOf('Brent') < 0, 'brent still leads'); await ctx.close(); }
  { const { p, ctx } = await page({}, '2026-10-12T08:30:00Z', 1200, c => c.route(/api\.coingecko\.com/, r => r.abort()));
    const t = await tick(p); ok(/Bitcoin \(/.test(t.replace(/\n/g,' ')), 'falls back to the saved snapshot, labelled with its time', t.slice(0, 160)); await ctx.close(); }
  { const { p, ctx } = await page({}, '2026-11-30T08:30:00Z', 1200, c => c.route(/api\.coingecko\.com/, r => r.abort()));
    ok(!/Bitcoin/.test(await tick(p)), 'a saved price older than 4 days is not shown'); await ctx.close(); }
  for (const body of ['{}', '{"bitcoin":{"usd":"x"}}', 'garbage', '{"bitcoin":{"usd":0,"usd_24h_change":1}}']) {
    const { p, ctx, errs } = await page({}, '2026-10-12T08:30:00Z', 1200, c => c.route(/api\.coingecko\.com/, r => r.fulfill({ status: 200, contentType: 'application/json', headers: { 'access-control-allow-origin': '*' }, body })));
    ok(!errs.length && !/NaN|undefined/.test(await tick(p)), 'coingecko answer ' + body.slice(0, 20), errs[0]); await ctx.close(); }
  console.log(`${n - fail}/${n} passed`); await b.close(); process.exit(fail ? 1 : 0);
})();
