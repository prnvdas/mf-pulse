// The service worker (docs/sw.js): after one visit the site opens from this browser's own copy when the connection is down or slow.
const { chromium } = require('playwright'), http = require('http');
const BASE = process.env.BASE.replace('/index.html', ''); let n = 0, fail = 0; const ok = (c, name, d) => { n++; if (!c) { fail++; console.log('FAIL', name, d || ''); } };
(async () => {
  const b = await chromium.launch();
  // a pass-through server in front of the fixture server that can be told to hold one file back for six seconds
  const slow = { on: false };
  const proxy = http.createServer((req, res) => {
    const go = () => http.get(BASE + req.url, r => { res.writeHead(r.statusCode, r.headers); r.pipe(res); }).on('error', () => { res.writeHead(502); res.end(); });
    if (slow.on && req.url.startsWith('/data/latest.json')) setTimeout(go, 6000); else go();
  });
  await new Promise(r => proxy.listen(8002, '127.0.0.1', r));
  const URL = 'http://127.0.0.1:8002/index.html?sw=1';
  const ctx = await b.newContext({ viewport: { width: 390, height: 800 } }), errs = [];
  const shown = p => p.evaluate(() => ({ cv: document.getElementById('cv').innerText, iv: document.getElementById('iv').innerText, text: document.body.innerText.length }));
  const open = async () => { const p = await ctx.newPage(); p.on('pageerror', e => errs.push(e.message)); await p.goto(URL, { waitUntil: 'networkidle' }); return p; };
  // visit 1 installs the worker, visit 2 is controlled by it (its requests are the ones that get saved)
  let p = await open(); await p.evaluate(() => navigator.serviceWorker.ready); await p.close();
  p = await open(); const controlled = await p.evaluate(() => !!navigator.serviceWorker.controller); await p.waitForTimeout(600);
  const normal = await shown(p); await p.close();
  ok(controlled, 'the service worker takes control on the second visit');
  ok(/₹/.test(normal.cv) && normal.text > 1500, 'normal visit still renders', JSON.stringify(normal));
  // the connection is down: the page and its data come from the saved copy
  await ctx.setOffline(true);
  p = await ctx.newPage(); p.on('pageerror', e => errs.push(e.message));
  await p.goto(URL, { waitUntil: 'domcontentloaded' }); await p.waitForFunction(() => /₹/.test(document.getElementById('cv').innerText), null, { timeout: 8000 }).catch(() => {});
  const off = await shown(p); await p.close(); await ctx.setOffline(false);
  ok(/₹/.test(off.cv) && /₹/.test(off.iv) && off.text > 1500, 'offline: the page opens from the saved copy with its numbers', JSON.stringify(off));
  // the connection is up but the latest data file stalls for six seconds: the saved copy fills in after about 2.5 s, not after the stall
  slow.on = true;
  p = await ctx.newPage(); p.on('pageerror', e => errs.push(e.message)); const t0 = Date.now();
  await p.goto(URL, { waitUntil: 'domcontentloaded' }); await p.waitForFunction(() => /₹/.test(document.getElementById('cv').innerText), null, { timeout: 15000 }).catch(() => {});
  const took = Date.now() - t0, st = await shown(p); await p.close(); slow.on = false;
  ok(/₹/.test(st.cv) && took < 5500, 'a stalled data file: numbers within about 2.5 s from the saved copy, not after the six-second stall', took + ' ms ' + JSON.stringify(st));
  ok(!errs.length, 'no page errors', errs[0]);
  proxy.close(); await b.close(); console.log(`\n${n - fail}/${n} passed`); process.exit(fail ? 1 : 0);
})();
