// The real-time price stream (Yahoo's websocket): decoding, merging into the tape, and behaving when it misbehaves.
const { chromium } = require('playwright');
const BASE = process.env.BASE; let n = 0, fail = 0; const ok = (c, name, d) => { n++; if (!c) { fail++; console.log('FAIL', name, d || ''); } };
// ---- a protobuf encoder for the few fields the page reads
const varint = v => { v = BigInt(v); const out = []; do { let b = Number(v & 127n); v >>= 7n; if (v) b |= 128; out.push(b); } while (v); return out; };
const f32 = x => { const b = Buffer.alloc(4); b.writeFloatLE(x); return [...b]; };
function msg({ id, price, chgPct, chg, t }) {
  const out = [0x0a, ...varint(id.length), ...Buffer.from(id)];
  if (price != null) out.push(0x15, ...f32(price));
  if (t != null) out.push(0x18, ...varint((BigInt(t) << 1n) ^ (BigInt(t) >> 63n)));
  if (chgPct != null) out.push(0x45, ...f32(chgPct));
  if (chg != null) out.push(0x65, ...f32(chg));
  return JSON.stringify({ type: 'pricing', message: Buffer.from(out).toString('base64') });
}
(async () => {
  const b = await chromium.launch();
  // stream(ws): called for each page's socket; quotes: relay answers (ticker -> {price, prev, t})
  async function page(iso, { stream, quotes = {}, mc, patch } = {}) {
    const ctx = await b.newContext({ viewport: { width: 1200, height: 900 } }), p = await ctx.newPage(), errs = [], subs = [];
    p.on('pageerror', e => errs.push(e.message));
    await p.clock.install({ time: new Date(iso) });
    await ctx.route(/api\.allorigins\.win/, r => {
      const u = decodeURIComponent(new globalThis.URL(r.request().url()).searchParams.get('url') || ''), tk = decodeURIComponent((u.match(/chart\/([^?]+)/) || [])[1] || ''), q = quotes[tk];
      if (!q) return r.fulfill({ status: 404, body: '' });
      const t = Math.floor(Date.parse(q.t) / 1000);
      r.fulfill({ status: 200, contentType: 'application/json', headers: { 'access-control-allow-origin': '*' },
        body: JSON.stringify({ chart: { result: [{ meta: { regularMarketPrice: q.price, chartPreviousClose: 123456, regularMarketTime: t, gmtoffset: 19800 }, timestamp: [t - 86400, t], indicators: { quote: [{ close: [q.prev, q.price] }] } }] } }) });
    });
    await ctx.route(/priceapi\.moneycontrol\.com/, r => mc ? r.fulfill({ status: 200, contentType: 'application/json', headers: { 'access-control-allow-origin': '*' }, body: JSON.stringify({ code: '200', data: mc }) }) : r.abort());
    if (patch) await ctx.route(/\/data\/latest\.json/, async r => { const j = await (await r.fetch()).json(); r.fulfill({ json: patch(j) }); });
    if (stream) await ctx.routeWebSocket(/streamer\.finance\.yahoo\.com/, ws => { ws.onMessage(m => { try { subs.push(...JSON.parse(m).subscribe); } catch (e) { /* ignore */ } stream(ws); }); });
    await p.goto(BASE, { waitUntil: 'networkidle' }); await p.waitForTimeout(600); await p.clock.fastForward(3000); await p.waitForTimeout(400);
    return { p, ctx, errs, subs };
  }
  const tape = p => p.$eval('#tkRun', e => e.innerText.replace(/\n/g, ' ')), label = p => p.$eval('#tkLab', e => e.innerText);
  const OPEN = '2026-10-13T05:30:00Z';                                                      // Tue 11:00 IST, market open
  const Q = { '^NSEI': { price: 22500, prev: 22400, t: '2026-10-13T05:29:00Z' }, '^BSESN': { price: 72000, prev: 71000, t: '2026-10-13T05:28:00Z' } };
  // 1. ticks arrive, are measured against the verified previous close, and a newer tick replaces an older one
  { const { p, ctx, errs, subs } = await page(OPEN, { quotes: Q, stream: ws => { ws.send(msg({ id: '^NSEI', price: 22600 })); setTimeout(() => ws.send(msg({ id: '^NSEI', price: 22700 })), 300); } });
    await p.waitForTimeout(500); await p.clock.fastForward(2500); await p.waitForTimeout(400);
    const t = await tape(p); ok(/Nifty 50\s+22,700/.test(t) && /\+1\.34%/.test(t), 'newest stream tick shown, measured against the daily-bar previous close (22,400)', t.slice(0, 400));
    ok(subs.includes('^NSEI') && subs.includes('BZ=F') && subs.includes('BTC-USD') && subs.includes('INR=X') && !subs.includes('^BSESN'), 'subscribes to the indices, Brent, Bitcoin and the dollar-rupee rate, but not the delayed Sensex feed', subs.join());
    ok(/LIVE/.test(await label(p)), 'tape says LIVE', await label(p)); ok(!errs.length, 'stream errors', errs[0]); await ctx.close(); }
  // 2. Sensex from Moneycontrol (real time, official previous close); Yahoo's delayed Sensex is ignored
  { const { p, ctx, errs } = await page(OPEN, { quotes: Q, mc: { pricecurrent: '73100.50', priceprevclose: '73033.78', lastupd: '2026-10-13 11:00:05' }, stream: ws => ws.send(msg({ id: '^BSESN', price: 70000 })) });
    await p.clock.fastForward(6000); await p.waitForTimeout(500); const t = await tape(p);
    ok(/Sensex\s+73,100\.50/.test(t) && /\+0\.09%/.test(t), 'Sensex from Moneycontrol, measured against the official previous close', t.slice(0, 400)); ok(!/70,000/.test(t), 'the delayed Yahoo Sensex tick is ignored'); ok(!errs.length, 'sensex errors', errs[0]); await ctx.close(); }
  { const { p, ctx } = await page(OPEN, { quotes: Q, mc: { pricecurrent: '73100.50', priceprevclose: '73033.78', lastupd: '2026-10-12 15:30:00' } });
    await p.clock.fastForward(6000); const t = await tape(p); ok(!/73,100/.test(t), 'a Moneycontrol quote that is not from today is not shown as live', t.slice(0, 200)); await ctx.close(); }
  for (const bad of [{ pricecurrent: 'x', priceprevclose: '1', lastupd: '2026-10-13 11:00:00' }, { pricecurrent: '0', priceprevclose: '73033.78', lastupd: '2026-10-13 11:00:00' }, {}]) {
    const { p, ctx, errs } = await page(OPEN, { quotes: Q, mc: bad }); await p.clock.fastForward(6000); const t = await tape(p);
    ok(!errs.length && !/NaN|undefined/.test(t), 'bad Moneycontrol data ignored', errs[0] || t.slice(0, 100)); await ctx.close(); }
  // 3. Bitcoin in rupees from BTC-USD x INR=X, both ticking
  { const { p, ctx } = await page(OPEN, { quotes: Q, stream: ws => { ws.send(msg({ id: 'INR=X', price: 95 })); ws.send(msg({ id: 'BTC-USD', price: 84000, chgPct: 1.25 })); } });
    await p.clock.fastForward(2500); await p.waitForTimeout(400); const t = await tape(p);
    ok(/Bitcoin\s+₹79,80,000/.test(t) && /\+1\.25%/.test(t), 'Bitcoin in rupees (84,000 x 95)', t.slice(-260)); await ctx.close(); }
  { const { p, ctx } = await page(OPEN, { quotes: Q, stream: ws => ws.send(msg({ id: 'BTC-USD', price: 84000, chgPct: 1.25 })) });
    await p.clock.fastForward(2500); ok(!/Bitcoin/.test(await tape(p)), 'no Bitcoin without the rupee rate (never a wrong number)'); await ctx.close(); }
  { const { p, ctx } = await page(OPEN, { quotes: Q, stream: ws => { ws.send(msg({ id: 'INR=X', price: 5 })); ws.send(msg({ id: 'BTC-USD', price: 84000, chgPct: 1 })); } });
    await p.clock.fastForward(2500); ok(!/Bitcoin/.test(await tape(p)), 'an absurd exchange rate hides Bitcoin'); await ctx.close(); }
  // 4. an index Yahoo has no usable history for still works when the batch carries its official previous close
  { const { p, ctx } = await page(OPEN, { quotes: Q, patch: j => { j.market.forEach(m => { if (/LargeMidcap/.test(m.name)) { m.ticker = 'NIFTY_LARGEMID250.NS'; m.prev_close = 15755.2; } }); return j; },
      stream: ws => ws.send(msg({ id: 'NIFTY_LARGEMID250.NS', price: 15700 })) });
    await p.clock.fastForward(2500); await p.waitForTimeout(400); const t = await tape(p);
    ok(/LargeMidcap 250\s+15,700\.00/.test(t) && /−0\.35%/.test(t), 'LargeMidcap 250 ticks against its official previous close', t.slice(0, 500)); await ctx.close(); }
  // 5. ticks update the tape in place: the same elements stay (no rebuild, so no twitch)
  { const { p, ctx } = await page(OPEN, { quotes: Q, stream: ws => { ws.send(msg({ id: '^NSEI', price: 22600 })); setTimeout(() => ws.send(msg({ id: '^NSEI', price: 22650 })), 2500); } });
    await p.clock.fastForward(2000); await p.evaluate(() => { document.querySelector('#tkRun .tk-item').dataset.mark = 'same'; document.querySelectorAll('#tkRun .tk-item').forEach(e => { e.dataset.mark = 'same'; }); });
    await p.waitForTimeout(3000); await p.clock.fastForward(2500); await p.waitForTimeout(400);
    ok(await p.$$eval('#tkRun .tk-item', a => a.length > 0 && a.every(e => e.dataset.mark === 'same')), 'elements survive a tick (updated in place)'); ok(/22,650/.test(await tape(p)), 'and the new value is shown'); await ctx.close(); }
  // 6. junk is ignored
  for (const bad of ['not json', JSON.stringify({ type: 'heartbeat' }), JSON.stringify({ type: 'pricing', message: '!!!' }), JSON.stringify({ type: 'pricing', message: 'CgZeQlNF' }), JSON.stringify({ type: 'pricing' }),
                     msg({ id: '^NSEI', price: 0 }), msg({ id: '^NSEI', price: -5 }), msg({ id: '^NSEI', price: NaN }), msg({ id: '^NSEI' })]) {
    const { p, ctx, errs } = await page(OPEN, { quotes: Q, stream: ws => ws.send(bad) }); await p.clock.fastForward(2500); const t = await tape(p);
    ok(!errs.length && /Nifty 50\s+22,500/.test(t) && !/NaN|undefined/.test(t), 'junk ignored: ' + bad.slice(0, 30), errs[0] || t.slice(0, 120)); await ctx.close(); }
  // 4. the server drops the connection: nothing breaks and polling still gives the tape
  { const { p, ctx, errs } = await page(OPEN, { quotes: Q, stream: ws => ws.close() }); await p.clock.fastForward(5000); const t = await tape(p);
    ok(!errs.length && /Nifty 50\s+22,500/.test(t), 'dropped stream: polling carries on', errs[0] || t.slice(0, 120)); await ctx.close(); }
  // 5. weekend: indices ignore the stream, Brent does not
  { const { p, ctx } = await page('2026-10-10T05:30:00Z', { quotes: { 'BZ=F': { price: 100, prev: 99, t: '2026-10-10T05:25:00Z' } }, stream: ws => { ws.send(msg({ id: '^NSEI', price: 99999 })); ws.send(msg({ id: 'BZ=F', price: 101 })); } });
    await p.clock.fastForward(2500); await p.waitForTimeout(400); const t = await tape(p);
    ok(!/99,999/.test(t), 'closed market: index ticks ignored'); ok(/Brent crude\s+\$101/.test(t), 'Brent still streams', t.slice(0, 200)); await ctx.close(); }
  console.log(`${n - fail}/${n} passed`); await b.close(); process.exit(fail ? 1 : 0);
})();
