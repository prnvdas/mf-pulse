const { chromium } = require('playwright'); let n=0,fail=0; const ok=(c,name,d)=>{n++; if(!c){fail++; console.log('FAIL',name,d||'');}};
(async()=>{const b=await chromium.launch();
 async function page(routes={}, perms){const ctx=await b.newContext({viewport:{width:1200,height:900},permissions:perms||[]});const p=await ctx.newPage();const errs=[];p.on('pageerror',e=>errs.push(e.message));
  await p.clock.install({time:new Date('2026-10-12T08:30:00Z')});
  for(const [f,fn] of Object.entries(routes)) await p.route(new RegExp('/data/'+f+'\\.json'),async r=>{const j=await (await r.fetch()).json();r.fulfill({json:fn(j)});});
  await p.goto((process.env.BASE||'http://localhost:8000/index.html'),{waitUntil:'networkidle'});await p.waitForTimeout(400);await p.clock.fastForward(3000);await p.waitForTimeout(250);return {p,ctx,errs};}
 {const {p,ctx,errs}=await page();ok(await p.$eval('#txBar',e=>e.hidden),'tx bar hidden when ok');ok(!errs.length,'ok errs',errs[0]);await ctx.close();}
 {const {p,ctx,errs}=await page({portfolio_history:j=>{j.tx_check={status:'stale',gaps_pct:{'axis-small-cap':-2.4,'mirae-large-midcap':0.1},last_transaction:'2026-10-01'};return j;}},['clipboard-read','clipboard-write']);
  const t=await p.$eval('#txBar',e=>e.hidden?'':e.innerText);ok(/history out of date/i.test(t)&&/Axis Small Cap \(-2.4%\)/.test(t)&&!/Mirae/.test(t)&&/encrypt_statement.py --transactions --push/.test(t)&&/Order history/.test(t),'tx bar stale content',t);
  await p.click('#txBar button.cp');await p.waitForTimeout(200);const clip=await p.evaluate(()=>navigator.clipboard.readText());ok(clip==='python3 src/encrypt_statement.py --transactions --push','copy button copies command',clip);
  ok(/Copied/.test(await p.$eval('#txBar button.cp',e=>e.textContent)),'copy feedback');ok(!errs.length,'stale errs',errs[0]);await (await p.$('#txBar')).screenshot({path:'txbar.png'});await ctx.close();}
 for (const [m,fn] of Object.entries({garbage:j=>{j.tx_check='x';return j;},nullgaps:j=>{j.tx_check={status:'stale',gaps_pct:null};return j;},weird:j=>{j.tx_check={status:'stale',gaps_pct:{a:'x',b:null}};return j;}})) {const {p,ctx,errs}=await page({portfolio_history:fn});ok(!errs.length&&!/NaN|undefined/.test(await p.evaluate(()=>document.body.innerText)),'tx_check '+m,errs[0]);await ctx.close();}
 // statement bar copy
 {const {p,ctx}=await page({analytics:j=>j});ok(true,'noop');await ctx.close();}
 ok(false,'DELIBERATE failure to test the automatic undo');console.log(`${n-fail}/${n} passed`);await b.close();process.exit(fail?1:0);})();
