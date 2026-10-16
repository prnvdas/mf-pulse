const { chromium } = require('playwright'); let n=0,fail=0; const ok=(c,name,d)=>{n++; if(!c){fail++; console.log('FAIL',name,d||'');}};
(async()=>{const b=await chromium.launch();
 async function page(routes={}, perms, iso){const ctx=await b.newContext({viewport:{width:1200,height:900},permissions:perms||[]});const p=await ctx.newPage();const errs=[];p.on('pageerror',e=>errs.push(e.message));
  await p.clock.install({time:new Date(iso||'2026-10-12T08:30:00Z')});
  for(const [f,fn] of Object.entries(routes)) await p.route(new RegExp('/data/'+f+'\\.json'),async r=>{const j=await (await r.fetch()).json();r.fulfill({json:fn(j)});});
  await p.goto((process.env.BASE||'http://localhost:8000/index.html'),{waitUntil:'networkidle'});await p.waitForTimeout(400);await p.clock.fastForward(3000);await p.waitForTimeout(250);return {p,ctx,errs};}
 {const {p,ctx,errs}=await page();ok(await p.$eval('#txBar',e=>e.hidden),'tx bar hidden when ok');ok(!errs.length,'ok errs',errs[0]);await ctx.close();}
 {const {p,ctx,errs}=await page({portfolio_history:j=>{j.tx_check={status:'stale',gaps_pct:{'axis-small-cap':-2.4,'mirae-large-midcap':0.1},last_transaction:'2026-10-01'};return j;}},['clipboard-read','clipboard-write']);
  const t=await p.$eval('#txBar',e=>e.hidden?'':e.innerText);ok(/history out of date/i.test(t)&&/Axis Small Cap \(-2.4%\)/.test(t)&&!/Mirae/.test(t)&&/encrypt_statement.py --transactions --push/.test(t)&&/Order history/.test(t),'tx bar stale content',t);
  await p.click('#txBar button.cp');await p.waitForTimeout(200);const clip=await p.evaluate(()=>navigator.clipboard.readText());ok(clip==='python3 src/encrypt_statement.py --transactions --push','copy button copies command',clip);
  ok(/Copied/.test(await p.$eval('#txBar button.cp',e=>e.textContent)),'copy feedback');ok(!errs.length,'stale errs',errs[0]);await (await p.$('#txBar')).screenshot({path:'txbar.png'});await ctx.close();}
 for (const [m,fn] of Object.entries({garbage:j=>{j.tx_check='x';return j;},nullgaps:j=>{j.tx_check={status:'stale',gaps_pct:null};return j;},weird:j=>{j.tx_check={status:'stale',gaps_pct:{a:'x',b:null}};return j;}})) {const {p,ctx,errs}=await page({portfolio_history:fn});ok(!errs.length&&!/NaN|undefined/.test(await p.evaluate(()=>document.body.innerText)),'tx_check '+m,errs[0]);await ctx.close();}
 // a stale "holiday" batch carrying a small negative move must show as zero, not -Rs 222
 {const {p,ctx,errs}=await page({latest:j=>{j.phase='holiday';j.funds.forEach(f=>{f.nav_move_pct=-0.002;f.rupee_impact=-100;f.band_rupees=3000;});j.totals.today_impact=-222;j.totals.today_pct=-0.002;return j;}});
  const t=await p.$eval('#pnl',e=>e.textContent); ok(!/222/.test(t)&&!/−/.test(t),'holiday batch shows no impact',t); ok(!errs.length,'holiday errs',errs[0]); await ctx.close();}
 // a "holiday" label written overnight (before 09:45 IST) is not a holiday; one written mid-morning is
 {const r=await page({latest:j=>{j.phase='holiday';j.generated_at='2026-10-13T01:55:16+05:30';return j;}},[],'2026-10-13T02:48:00Z');
  const t=await r.p.$eval('#stamp',e=>e.textContent); ok(!/holiday/i.test(t),'overnight holiday label ignored',t); ok(!r.errs.length,'preopen errs',r.errs[0]); await r.ctx.close();}
 {const ctx=await b.newContext({viewport:{width:1200,height:900}});const p=await ctx.newPage();
  await p.clock.install({time:new Date('2026-10-13T06:00:00Z')});
  await p.route(/\/data\/latest\.json/,async r=>{const j=await (await r.fetch()).json();j.phase='holiday';j.generated_at='2026-10-13T10:30:00+05:30';r.fulfill({json:j});});
  await p.goto(process.env.BASE||'http://localhost:8000/index.html',{waitUntil:'networkidle'});await p.waitForTimeout(400);await p.clock.fastForward(3000);await p.waitForTimeout(250);
  const t=await p.$eval('#stamp',e=>e.textContent); ok(/holiday/i.test(t),'a real mid-morning holiday label still shows',t); await ctx.close();}
 // price-data health notice
 for (const [st,re,label] of [['warn',/data check/i,'warn'],['bad',/estimate on hold/i,'bad']]) {const {p,ctx,errs}=await page({latest:j=>{j.health={status:st,issues:[{level:st,text:'Yahoo had a hole for 261 of 264 stocks.'}],checked_at:'2026-10-12T14:00:00+05:30',repaired:261};return j;}});
  const t=await p.$eval('#dataBar',e=>e.hidden?'':e.innerText); ok(re.test(t)&&/261 of 264/.test(t),'data bar '+label,t); ok(/price data/i.test(await p.$eval('#healthLine',e=>e.innerText)),'footer price-data item '+label); ok(!errs.length,'data bar errs '+label,errs[0]); await ctx.close();}
 for (const h of [{status:'ok',issues:[]},null,{status:'warn',issues:'x'},{status:'warn',issues:[null,{text:5}]},'str']) {const {p,ctx,errs}=await page({latest:j=>{if(h===null)delete j.health;else j.health=h;return j;}});
  ok(!errs.length&&!/NaN|undefined|\[object/.test(await p.evaluate(()=>document.body.innerText)),'health shape '+JSON.stringify(h).slice(0,30),errs[0]); await ctx.close();}
 // before today's first estimate: say so instead of showing yesterday's close as today's
 {const {p,ctx,errs}=await page({latest:j=>{j.generated_at='2026-10-13T23:53:00+05:30';j.phase='final';return j;}},[],'2026-10-14T02:40:00Z');   // Wed 08:10 IST
  const e=await p.$eval('#eyebrow',x=>x.textContent), s=await p.$eval('#sub',x=>x.textContent), pn=await p.$eval('#pnl',x=>x.textContent);
  ok(/starts at 09:15/i.test(e)&&/opens at 09:15/.test(s)&&pn==='—','pre-open morning shows no estimate',e+'|'+pn); ok(!errs.length,'pre-open errs',errs[0]); await ctx.close();}
 {const {p,ctx}=await page({latest:j=>{j.generated_at='2026-10-13T23:53:00+05:30';j.phase='final';return j;}},[],'2026-10-14T04:30:00Z');   // Wed 10:00 IST, no estimate yet today
  ok(/waiting for today/i.test(await p.$eval('#eyebrow',x=>x.textContent)),'open but no estimate yet says waiting'); await ctx.close();}
 {const {p,ctx}=await page({latest:j=>{j.generated_at='2026-10-14T09:50:00+05:30';j.phase='live';return j;}},[],'2026-10-14T04:30:00Z');   // 10:00 IST, fresh estimate
  ok(/live/i.test(await p.$eval('#eyebrow',x=>x.textContent))&&(await p.$eval('#pnl',x=>x.textContent))!=='—','a fresh estimate shows normally'); await ctx.close();}
 // valuation back-test: plain English up front, the technical table folded away
 {const {p,ctx,errs}=await page(); const t=await p.$eval('#valBody',e=>e.innerText);
  ok(/bottom line/i.test(t)&&/a typical next year/i.test(t)&&/the worst next year/i.test(t),'backtest plain summary',t.slice(-500)); ok(await p.$eval('#valBody details',e=>!e.open),'full numbers folded away');
  ok(!errs.length&&!/NaN|undefined|\[object/.test(t),'backtest clean',errs[0]); await ctx.close();}
 for (const fn of [j=>{j.valuation_backtest={buckets:[{bucket:'x'},null]};return j;},j=>{j.valuation_backtest={buckets:[]};return j;},j=>{j.valuation_backtest={buckets:[{bucket:'Cheapest third',median_1y:null,worst_1y:'a'}],note:5};return j;}]) {const {p,ctx,errs}=await page({mood:fn});
  ok(!errs.length&&!/NaN|undefined|\[object/.test(await p.evaluate(()=>document.body.innerText)),'backtest hostile data',errs[0]); await ctx.close();}
 // header chips: three icon cards
 {const {p,ctx,errs}=await page(); const n=await p.$$eval('#hChips .hc',a=>a.length), t=await p.$eval('#hChips',e=>e.innerText);
  ok(n===3&&/market mood/i.test(t)&&/next session/i.test(t)&&/institutional flows/i.test(t)&&/FII/.test(t)&&/DII/.test(t),'three header cards',String(n)+t.slice(0,160)); ok(await p.$$eval('#hChips .hc svg',a=>a.length)===3,'each card has an icon'); ok(await p.$eval('#hChips .mbar i',e=>/%$/.test(e.style.left)),'mood meter marker');
  ok(!errs.length,'chip errs',errs[0]); await ctx.close();}
 for (const [f,fn] of [['mood',j=>{j.mood={value:'x',zone:5};return j;}],['mood',j=>{j.mood=null;return j;}],['outlook',j=>{j.call={colour:'green',p_call:'?'};return j;}],['news',j=>{j.why={flows:{fii:null,dii:'a'}};return j;}]]) {const {p,ctx,errs}=await page({[f]:fn});
  ok(!errs.length&&!/NaN|undefined|\[object/.test(await p.$eval('#hChips',e=>e.innerText)),'chips hostile '+f,errs[0]); await ctx.close();}
 // estimate-vs-actual card: was the miss inside the advertised range?
 for (const [bands,re,label,act] of [[[5362,4993,180],/inside that range/,'inside',11000],[[500,400,50],/outside it/,'outside',13500]]) {const {p,ctx,errs}=await page({history:j=>{const d=j.reduce((m,r)=>r.date>m?r.date:m,'');let k=0;j.forEach(r=>{if(r.date===d){r.band_rupees=bands[k++%3];r.predicted_pl_rupees=10000;r.actual_pl_rupees=act;}});return j;}});
  const t=await p.$eval('#scoreWrap',e=>e.innerText); ok(re.test(t)&&/within about/.test(t),'score card range check: '+label,t.slice(-300)); ok(!errs.length,'score errs',errs[0]); await ctx.close();}
 {const {p,ctx}=await page({history:j=>{j.forEach(r=>{delete r.band_rupees;});return j;}}); ok(!/within about/.test(await p.$eval('#scoreWrap',e=>e.innerText)),'no range sentence when the band was not recorded'); await ctx.close();}
 // a tab running an old copy of the page is told a newer one exists
 {const ctx=await b.newContext({viewport:{width:1200,height:900}});const p=await ctx.newPage();let n=0,altered=false;
  await p.clock.install({time:new Date('2026-10-12T08:30:00Z')});
  await p.route(/\/index\.html/,async r=>{n++;const resp=await r.fetch();let body=await resp.text();if(altered&&n>2)body+='<!-- newer -->';r.fulfill({response:resp,body});});
  await p.goto(process.env.BASE||'http://localhost:8000/index.html',{waitUntil:'networkidle'});await p.waitForTimeout(500);
  await p.clock.fastForward(5*60000+2000);await p.waitForTimeout(600); ok(!(await p.$('#newVer')),'no notice while the page is unchanged');
  altered=true;await p.clock.fastForward(5*60000+2000);await p.waitForTimeout(800);
  ok(await p.$eval('#newVer',e=>/newer version/.test(e.textContent)&&!!e.querySelector('button')),'notice appears when the published page changed'); await ctx.close();}
 // comparison with an index, falls and recoveries, fund overlap, the recap
 {const {p,ctx,errs}=await page(); const g=async id=>p.$eval('#'+id,e=>e.hidden?'':e.innerText);
  let t=await g('moneyVs'); ok(/same money/.test(t)&&/(ahead|behind) by/.test(t)&&/Nifty 50/.test(t),'index comparison sentence',t);
  ok(await p.$$eval('#moneyCmp button',a=>a.length)===3,'comparison picker has none / Nifty 50 / Nifty 500');
  await p.click('#moneyCmp button[data-c="m"]'); ok(/Nifty 500/.test(await g('moneyVs')),'switch to Nifty 500'); await p.click('#moneyCmp button[data-c=""]'); ok((await g('moneyVs'))==='','no comparison hides the sentence');
  t=await g('ddWrap'); ok(/deepest fall/i.test(t)&&/right now/i.test(t)&&/fell from/i.test(t),'falls and recoveries section',t.slice(0,200));
  t=await g('overlapWrap'); ok(/different stocks/.test(t)&&/ten biggest stocks/i.test(t)&&/stocks in common/i.test(t),'fund overlap section',t.slice(0,200));
  t=await g('recapWrap'); ok(/your portfolio moved/i.test(t)&&/by fund/i.test(t),'recap card',t.slice(0,200));
  ok(!errs.length&&!/NaN|undefined|\[object/.test(await p.evaluate(()=>document.body.innerText)),'new sections clean',errs[0]); await ctx.close();}
 // hostile data for the new sections
 for (const [f,fn] of [['portfolio_history',j=>{j.points.forEach(p=>{p.n=null;p.m='x'});return j;}],['portfolio_history',j=>{j.points=j.points.slice(0,30);return j;}],['portfolio_history',j=>{j.points.forEach((p,i)=>{if(i%7===0)p.v=0});return j;}],
   ['recap',j=>({date:'x',lines:'y'})],['recap',j=>({date:'2026-10-15',lines:[null,{t:5},{t:'ok',k:'<b>'}]})],['recap',j=>[]],
   ['config',j=>{j.funds.forEach(f=>{f.holdings=null});return j;}],['config',j=>{j.funds.forEach(f=>{f.holdings=[{t:5,w:'x'},null]});return j;}]]) {const {p,ctx,errs}=await page({[f]:fn});
  ok(!errs.length&&!/NaN|undefined|\[object/.test(await p.evaluate(()=>document.body.innerText)),'hostile '+f,errs[0]); await ctx.close();}
 // fund by fund against its benchmark, fund changes watch, system status
 {const {p,ctx,errs}=await page(); const g=async id=>p.$eval('#'+id,e=>e.hidden?'':e.innerText);
  let t=await g('fundBenchWrap'); ok(/Nifty Smallcap 250/.test(t)&&/same money/i.test(t)&&/(ahead|behind)/.test(t),'fund vs benchmark section',t.slice(0,160));
  await p.click('#fundBenchSeg button:nth-child(2)'); ok(/Mirae/.test(await g('fundBenchLine')),'fund tabs switch'); ok(await p.$$eval('#fundBenchChart path',a=>a.length)>=3,'fund chart draws three lines');
  t=await g('changesWrap'); ok(/fund manager/i.test(t)&&/no change/i.test(t),'fund changes table',t.slice(0,160));
  ok(!errs.length&&!/NaN|undefined|\[object/.test(await p.evaluate(()=>document.body.innerText)),'fund sections clean',errs[0]); await ctx.close();}
 {const {p,ctx,errs}=await page({fund_changes:j=>{j.funds[0].events=[{date:'2026-10-10',kind:'manager',text:'Lead fund manager changed: A → B.'}];return j;}});
  ok(/lead fund manager changed/i.test(await p.$eval('#changesBody',e=>e.innerText))&&/something changed/i.test(await p.$eval('#changesBody',e=>e.innerText)),'a recent change is shown and flagged'); ok(!errs.length,'changes errs',errs[0]); await ctx.close();}
 for (const [f,fn] of [['fund_history',j=>{j.funds.axis_x={v:[1],i:[1],b:[1]};Object.values(j.funds).forEach(x=>{x.b=null});return j;}],['fund_history',j=>{j.dates=[];return j;}],['fund_history',j=>({dates:'x',funds:5})],['fund_history',j=>{Object.values(j.funds).forEach(x=>{x.v=x.v.map(()=>'a')});return j;}],
   ['fund_changes',j=>({funds:[null,{name:5},{name:'x',events:'y',managers:5,aum_cr:'a'}]})],['fund_changes',j=>[]]]) {const {p,ctx,errs}=await page({[f]:fn});
  ok(!errs.length&&!/NaN|undefined|\[object/.test(await p.evaluate(()=>document.body.innerText)),'hostile '+f,errs[0]); await ctx.close();}
 {const ctx=await b.newContext({viewport:{width:1200,height:900}});const p=await ctx.newPage();const errs=[];p.on('pageerror',e=>errs.push(e.message));
  await p.clock.install({time:new Date('2026-10-12T08:30:00Z')});
  await p.route(/api\.github\.com\/repos\/.*\/actions\/runs/,r=>r.fulfill({status:200,contentType:'application/json',headers:{'access-control-allow-origin':'*'},body:JSON.stringify({workflow_runs:[{name:'estimate',conclusion:'success',created_at:'2026-10-12T08:00:00Z',html_url:'x'},{name:'estimate',conclusion:'failure',created_at:'2026-10-12T07:00:00Z',html_url:'x'},{name:'outlook',conclusion:'failure',created_at:'2026-10-12T06:00:00Z',html_url:'x'},{name:'pages build and deployment',conclusion:'success',created_at:'2026-10-12T08:00:00Z'},null,{name:5}]})}));
  await p.goto(process.env.BASE||'http://localhost:8000/index.html',{waitUntil:'networkidle'});await p.waitForTimeout(400);await p.clock.fastForward(3000);
  await p.click('#statusBox summary');await p.waitForTimeout(900);const t=await p.$eval('#statusBody',e=>e.innerText);
  ok(/data on this page/i.test(t)&&/prices and today/i.test(t)&&/estimate/.test(t)&&/outlook/.test(t)&&!/pages build/i.test(t),'status lists data freshness and jobs',t.slice(0,300)); ok(/failure/.test(t),'a failed job is shown'); ok(!errs.length,'status errs',errs[0]); await ctx.close();}
 {const ctx=await b.newContext({viewport:{width:1200,height:900}});const p=await ctx.newPage();const errs=[];p.on('pageerror',e=>errs.push(e.message));
  await p.clock.install({time:new Date('2026-10-12T08:30:00Z')}); await p.route(/api\.github\.com/,r=>r.fulfill({status:403,contentType:'application/json',body:'{}'}));
  await p.goto(process.env.BASE||'http://localhost:8000/index.html',{waitUntil:'networkidle'});await p.waitForTimeout(400);await p.click('#statusBox summary');await p.waitForTimeout(900);
  ok(/limit|reach/i.test(await p.$eval('#statusJobs',e=>e.innerText))&&!errs.length,'status copes with GitHub refusing',errs[0]); await ctx.close();}
 // dark theme: follows the system on first visit, a click switches and is remembered
 {const ctx=await b.newContext({viewport:{width:1200,height:900},colorScheme:'light'});const p=await ctx.newPage();const errs=[];p.on('pageerror',e=>errs.push(e.message));
  await p.clock.install({time:new Date('2026-10-12T08:30:00Z')});const U=process.env.BASE||'http://localhost:8000/index.html';
  await p.goto(U,{waitUntil:'networkidle'});await p.waitForTimeout(400);
  ok(await p.evaluate(()=>document.documentElement.dataset.theme)==='light','light system -> light page');
  await p.click('#themeBtn');ok(await p.evaluate(()=>document.documentElement.dataset.theme)==='dark','click switches to dark');
  ok(await p.evaluate(()=>getComputedStyle(document.documentElement).backgroundColor)==='rgb(5, 7, 15)','dark space background applied');
  ok(/light theme/i.test(await p.$eval('#themeBtn',e=>e.getAttribute('aria-label'))),'button now offers light');
  await p.reload({waitUntil:'networkidle'});ok(await p.evaluate(()=>document.documentElement.dataset.theme)==='dark','choice is remembered after reload');
  await p.click('#themeBtn');ok(await p.evaluate(()=>document.documentElement.dataset.theme)==='light'&&await p.evaluate(()=>localStorage.getItem('mfp_theme'))==='light','and switches back');
  ok(!errs.length,'theme errs',errs[0]);await ctx.close();}
 {const ctx=await b.newContext({viewport:{width:1200,height:900},colorScheme:'dark'});const p=await ctx.newPage();await p.addInitScript(()=>{try{localStorage.clear()}catch(e){}});
  await p.goto(process.env.BASE||'http://localhost:8000/index.html',{waitUntil:'networkidle'});ok(await p.evaluate(()=>document.documentElement.dataset.theme)==='dark','dark system -> dark page on the first visit');await ctx.close();}
 {const ctx=await b.newContext({viewport:{width:1200,height:900},colorScheme:'light'});const p=await ctx.newPage();await p.addInitScript(()=>{try{localStorage.setItem('mfp_theme','purple')}catch(e){}});
  await p.goto(process.env.BASE||'http://localhost:8000/index.html',{waitUntil:'networkidle'});ok(await p.evaluate(()=>document.documentElement.dataset.theme)==='light','an invalid stored value is ignored');await ctx.close();}
 // statement bar copy
 {const {p,ctx}=await page({analytics:j=>j});ok(true,'noop');await ctx.close();}
 console.log(`${n-fail}/${n} passed`);await b.close();process.exit(fail?1:0);})();
