const { chromium } = require('playwright');
const URL=process.env.BASE||(process.env.BASE||'http://localhost:8000/index.html'); let n=0,fail=0; const ok=(c,name,d)=>{n++; if(!c){fail++; console.log('FAIL',name,d||'');}};
(async()=>{const b=await chromium.launch();
 async function page(iso,routes={},init){const ctx=await b.newContext({viewport:{width:1200,height:900}});const p=await ctx.newPage();const errs=[];p.on('pageerror',e=>errs.push(e.message));
  if(init) await p.addInitScript(init);
  await p.clock.install({time:new Date(iso)});
  for(const [f,fn] of Object.entries(routes)) await p.route(new RegExp('/data/'+f+'\\.json'),async r=>{const resp=await r.fetch();const j=await resp.json();r.fulfill({json:fn(j)});});
  await p.goto(URL,{waitUntil:'networkidle'});await p.waitForTimeout(400);await p.clock.fastForward(4000);await p.waitForTimeout(250);return {p,ctx,errs};}
 const txt=(p,id)=>p.$eval('#'+id,e=>e.hidden?'':e.innerText);
 // 1 pending
 {const {p,ctx,errs}=await page('2026-10-12T11:30:00Z',{latest:j=>{j.generated_at='2026-10-12T15:40:00+05:30';j.funds.forEach(f=>{f.nav_move_pct=0.5;f.projected_nav=f.last_nav*1.005});return j;}});
  const t=await txt(p,'scoreWrap'); ok(/waiting for the published NAVs/.test(t)&&/not published yet/.test(t),'pending card',t.slice(0,160)); ok(!errs.length,'pending no errors',errs[0]); await ctx.close();}
 // 1b before close: shows last graded
 {const {p,ctx}=await page('2026-10-12T08:30:00Z');const t=await txt(p,'scoreWrap');ok(/graded after the funds published/.test(t),'graded card before close',t.slice(0,120));await ctx.close();}
 // 2 SIP states
 for (const [iso,month,re,label,opt] of [['2026-10-10T08:30:00Z','2026-09',/Not counted/,'sip not counted'],['2026-10-02T08:30:00Z','2026-09',/Waiting/,'sip waiting'],['2026-10-02T08:30:00Z','2026-09',/Due/,'sip due','day20'],['2026-10-10T08:30:00Z','2026-10',/Counted for October/,'sip counted']]) {
  const {p,ctx,errs}=await page(iso,{config:j=>{j.funds.forEach(f=>f.sip_applied_month=month);if(opt==='day20')j.sips.forEach(x=>x.day_of_month=20);return j;}});
  const t=await txt(p,'sipWrap'); ok(re.test(t),label,t.slice(0,300)); ok(!errs.length,label+' errors',errs[0]); await ctx.close();}
 // 3 corrupt history
 for (const [name,fn] of [['strings',j=>{j.points.forEach(p=>p.v='abc');return j;}],['one point',j=>{j.points=j.points.slice(0,1);return j;}],['mixed',j=>{j.points[5]={d:'x'};j.points[6]=null;return j;}],['nulls',j=>{j.points.forEach(p=>{p.v=null;p.i=null});return j;}]]) {
  const {p,ctx,errs}=await page('2026-10-12T08:30:00Z',{portfolio_history:fn}); const body=await p.evaluate(()=>document.body.innerText); ok(!errs.length&&!/NaN|undefined|\[object/.test(body),'history '+name,errs[0]||''); await ctx.close();}
 // 3b corrupt health
 for (const [name,fn] of [['no windows',j=>{j.funds.forEach(f=>delete f.windows);return j;}],['nulls',j=>{j.funds.forEach(f=>{Object.values(f.windows).forEach(w=>{w.fund=null;w.rank=null});f.peers=null;});return j;}],['strings',j=>{j.funds.forEach(f=>{f.windows['1Y'].fund='x';f.verdict=5;f.label=null});return j;}]]) {
  const {p,ctx,errs}=await page('2026-10-12T08:30:00Z',{fund_health:fn}); const body=await p.evaluate(()=>document.body.innerText); ok(!errs.length&&!/NaN|undefined|\[object/.test(body),'health '+name,errs[0]||''); await ctx.close();}
 // 4 hide
 {const {p,ctx,errs}=await page('2026-10-12T08:30:00Z');
  await p.click('#hideAmt'); await p.waitForLoadState('networkidle'); await p.waitForTimeout(600);
  const body=await p.evaluate(()=>document.body.innerText);
  const leaks=(body.match(/[−+]?₹\s?[\d,]+(\.\d+)?( ?(lakh|crore|cr|L|Cr)\b)?/g)||[]).filter(m=>!/\bcr(ore)?$/.test(m)&&!(/^₹\d+\.\d\d$/.test(m))&&!/^[−+]?₹\s?\d{1,3}$/.test(m)&&!/^₹1$/.test(m));
  ok(leaks.length===0,'hide: no rupee amounts left',leaks.slice(0,8).join(' | '));
  ok(/Show/.test(await p.$eval('#hideAmt',e=>e.getAttribute('aria-label'))),'hide button label');
  const money=await txt(p,'moneyWrap'); ok(/₹ ••••/.test(money),'hide money masked');
  await p.click('#hideAmt'); await p.waitForLoadState('networkidle'); await p.waitForTimeout(500);
  ok(/₹\d/.test(await p.evaluate(()=>document.body.innerText)),'show again'); ok(!errs.length,'hide errors',errs[0]); await ctx.close();}
 // 5 interactions
 {const {p,ctx,errs}=await page('2026-10-12T08:30:00Z');
  await p.click('#moneySeg button[data-m=gain]'); ok(/Gain = what it is worth/.test(await p.$eval('#moneyLegend',e=>e.innerText)),'gain mode');
  await p.hover('#mhit',{position:{x:300,y:80}}); ok(await p.$eval('#mtip',e=>!e.hidden&&/worth/.test(e.innerText)),'chart tooltip');
  await p.click('#calPrev'); ok(/September 2026/.test(await p.$eval('#calMonth',e=>e.textContent)),'calendar prev');
  ok(await p.$('#calGrid .d.g'),'calendar has green days'); await p.click('#calNext'); ok(/October 2026/.test(await p.$eval('#calMonth',e=>e.textContent)),'calendar next');
  ok(!errs.length,'interaction errors',errs[0]); await ctx.close();}
 console.log(`${n-fail}/${n} passed`); await b.close(); process.exit(fail?1:0);})();
