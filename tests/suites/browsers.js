// The page in Firefox and in WebKit (Safari's engine), at desktop and phone width: it renders, nothing throws, nothing is wider than the
// screen, and the parts people touch work (switches, calendar, the hover box). The other suites run in Chromium.
// A browser that is not installed is skipped locally (npx playwright install firefox webkit) but fails in CI, where it must be there.
const { firefox, webkit } = require('playwright'); let n = 0, fail = 0; const ok = (c, name, d) => { n++; if (!c) { fail++; console.log('FAIL', name, d || ''); } };
(async () => {
  for (const [name, type] of [['firefox', firefox], ['webkit', webkit]]) {
    let b; try { b = await type.launch(); } catch (e) { if (process.env.GITHUB_ACTIONS) { ok(false, name + ' launches', String(e.message).split('\n')[0]); } else console.log('SKIP', name, '(not installed)'); continue; }
    for (const w of [1200, 390]) {
      const ctx = await b.newContext({ viewport: { width: w, height: 900 } }), p = await ctx.newPage(), errs = []; const tag = name + ' ' + w + 'px: ';
      p.on('pageerror', e => errs.push(e.message));
      await p.route(/allorigins\.win|query1\.finance\.yahoo|priceapi\.moneycontrol/, r => r.abort());   // the suite runs on frozen data: no live third-party prices (the page copes with them failing)
      await p.clock.install({ time: new Date('2026-10-12T08:30:00Z') });
      await p.goto(process.env.BASE || 'http://localhost:8000/index.html', { waitUntil: 'networkidle' }); await p.waitForTimeout(500); await p.clock.fastForward(3000); await p.waitForTimeout(500);
      const t = await p.evaluate(() => ({ cv: document.getElementById('cv').innerText, greet: document.getElementById('bGreet').innerText, doc: document.documentElement.scrollWidth, iw: innerWidth, cal: document.querySelectorAll('.cal .d').length, seg: document.querySelectorAll('.seg button').length, body: document.body.innerText }));
      ok(/₹/.test(t.cv) && !/NaN|undefined/.test(t.body), tag + 'numbers render, no NaN', t.cv);
      ok(/Good (morning|afternoon|evening)/.test(t.greet), tag + 'greeting renders', t.greet);
      ok(t.doc <= t.iw + 1, tag + 'page is not wider than the screen', JSON.stringify({ doc: t.doc, iw: t.iw }));
      ok(t.cal >= 20 && t.seg >= 4, tag + 'calendar and switches are drawn', JSON.stringify({ cal: t.cal, seg: t.seg }));
      // a switch: the second option of the money chart becomes the selected one
      const btn = p.locator('#moneySeg button').nth(1); await btn.scrollIntoViewIfNeeded(); await btn.click(); await p.waitForTimeout(200);
      ok((await p.locator('#moneySeg button').nth(1).getAttribute('aria-selected')) === 'true', tag + 'a switch can be pressed');
      // the hover box: shows, stays inside the chart
      await p.locator('#mhit').scrollIntoViewIfNeeded(); await p.waitForTimeout(200);
      const box = await p.$eval('#mhit', e => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; });
      await p.mouse.move(box.x + box.w * .7, box.y + box.h * .5); await p.waitForTimeout(150);
      const tip = await p.evaluate(() => { const t = document.getElementById('mtip'), c = document.querySelector('#moneyChart svg').getBoundingClientRect(), r = t.getBoundingClientRect(); return { shown: !t.hidden && r.width > 0, inside: r.left >= c.left - 1 && r.right <= c.right + 1 }; });
      ok(tip.shown && tip.inside, tag + 'hover box shows inside the chart', JSON.stringify(tip));
      ok(!errs.length, tag + 'no page errors', errs[0]);
      await ctx.close();
    }
    await b.close();
  }
  console.log(`${n - fail}/${n} passed`); process.exit(fail ? 1 : 0);
})();
