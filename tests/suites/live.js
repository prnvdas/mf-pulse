// The real, current docs/data with the real clock. Catches what only real data can: a file the page can't parse,
// a section that silently disappeared, a NaN that reached the screen.
const { chromium } = require('playwright');
const BASE = process.env.BASE; let n = 0, fail = 0;
const ok = (c, name, d) => { n++; if (!c) { fail++; console.log('FAIL', name, d || ''); } };
(async () => {
  const b = await chromium.launch();
  for (const [w, tag] of [[1280, 'desktop'], [390, 'mobile']]) {
    const ctx = await b.newContext({ viewport: { width: w, height: 900 } }), p = await ctx.newPage(), errs = [];
    p.on('pageerror', e => errs.push(e.message));
    await p.goto(BASE, { waitUntil: 'networkidle' }); await p.waitForTimeout(800);
    const r = await p.evaluate(async () => {
      const files = ['latest', 'config', 'history', 'analytics', 'news', 'outlook', 'movers', 'accuracy', 'tax', 'holidays', 'mood', 'advisor', 'gift', 'flows', 'portfolio_history', 'fund_health'];
      const bad = [], docs = {};
      for (const f of files) { try { const t = await (await fetch('data/' + f + '.json', { cache: 'no-store' })).text(); docs[f] = JSON.parse(t); } catch (e) { bad.push(f + ': ' + e.message.slice(0, 60)); } }
      const vis = id => { const e = document.getElementById(id); return !!e && !e.hidden; };
      const ph = docs.portfolio_history, fh = docs.fund_health, an = docs.analytics, cfg = docs.config;
      return { bad, txt: document.body.innerText, over: document.documentElement.scrollWidth > innerWidth + 2,
        money: !!(ph && ph.points && ph.points.length > 1) === vis('moneyWrap'), health: !!(fh && fh.funds && fh.funds.length) === vis('healthWrap'),
        sector: !!(an && an.sector_exposure) === vis('sectorWrap'), sip: !!(cfg && cfg.sips && cfg.sips.length) === vis('sipWrap'), title: document.title, nav: vis('snav') };
    });
    ok(!r.bad.length, `${tag}: every data file is valid JSON`, r.bad.join('; '));
    ok(!errs.length, `${tag}: no page errors`, errs[0]);
    ok(!/\bundefined\b|\bNaN\b|\[object|Infinity/.test(r.txt), `${tag}: no undefined/NaN on the page`, (r.txt.match(/.{20}(undefined|NaN|\[object|Infinity).{20}/) || [])[0]);
    ok(r.money, `${tag}: money-over-time chart shown exactly when there is history`);
    ok(r.health, `${tag}: fund health shown exactly when the file has funds`);
    ok(r.sector, `${tag}: sector exposure shown exactly when the file has it`);
    ok(r.sip, `${tag}: SIP tracker shown exactly when SIPs are configured`);
    ok(r.nav && /MF Pulse/.test(r.title), `${tag}: menu and title`);
    ok(r.txt.length > 1500, `${tag}: page has substance`);
    if (tag === 'mobile') ok(!r.over, 'no horizontal overflow on mobile');
    await ctx.close();
  }
  console.log(`${n - fail}/${n} passed`); await b.close(); process.exit(fail ? 1 : 0);
})();
