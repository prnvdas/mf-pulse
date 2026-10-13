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
  async function page(iso, { stream, quotes = {} } = {}) {
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
    ok(subs.includes('^NSEI') && subs.includes('^BSESN') && subs.includes('BZ=F') && !subs.includes('BTC-USD'), 'subscribes to the indices and Brent only', subs.join());
    ok(/LIVE/.test(await label(p)), 'tape says LIVE', await label(p)); ok(!errs.length, 'stream errors', errs[0]); await ctx.close(); }
  // 3. junk is ignored
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
