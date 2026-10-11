const { chromium } = require('playwright');
const URL=process.env.BASE||(process.env.BASE||'http://localhost:8000/index.html'); let n=0,fail=0; const ok=(c,name,d)=>{n++; if(!c){fail++; console.log('FAIL',name,d||'');}};
(async()=>{const b=await chromium.launch();
 async function page(iso,routes={},w=1200){const ctx=await b.newContext({viewport:{width:w,height:900}});const p=await ctx.newPage();const errs=[];p.on('pageerror',e=>errs.push(e.message));
  await p.clock.install({time:new Date(iso)});
  for(const [f,fn] of Object.entries(routes)) await p.route(new RegExp('/data/'+f+'\\.json'),async r=>{const resp=await r.fetch();const j=await resp.json();r.fulfill({json:fn(j)});});
  await p.goto(URL,{waitUntil:'networkidle'});await p.waitForTimeout(400);await p.clock.fastForward(4000);await p.waitForTimeout(250);return {p,ctx,errs};}
 const longHist=j=>{const pts=[];let v=40000,i=40000;const d0=Date.parse('2022-12-12');for(let k=0;k<1000;k++){const d=new Date(d0+k*86400000*1.4);if(d.getDay()%6===0)continue;if(k%21===0)i+=50000;v=(v+(k%21===0?50000:0))*(1+Math.sin(k/9)*0.006+0.0006);pts.push({d:d.toISOString().slice(0,10),v:Math.round(v),i});}j.points=pts;j.basis='your transaction report';return j;};
 const settle=async p=>{let last=-1,same=0;for(let i=0;i<60&&same<4;i++){await p.waitForTimeout(100);const y=await p.evaluate(()=>Math.round(scrollY));same=y===last?same+1:0;last=y;}};
 // nav
 {const {p,ctx,errs}=await page('2026-10-12T08:30:00Z');
  ok(await p.$eval('#snav',e=>!e.hidden),'nav visible'); const labels=await p.$$eval('#snavIn button',b=>b.map(x=>x.textContent)); ok(labels.length===7,'nav buttons',labels.join());
  await p.click('#snavIn button:nth-child(6)'); await settle(p); const y=await p.$eval('#retWrap',e=>Math.round(e.getBoundingClientRect().top)); ok(y<120&&y>=0,'nav jumps to returns',String(y));
  ok((await p.$eval('#snavIn button:nth-child(6)',e=>e.getAttribute('aria-current')))==='true','scroll-spy marks Returns');
  await p.click('#snavIn button:nth-child(1)'); await settle(p); ok(await p.evaluate(()=>scrollY<50),'nav back to top');
  ok(!errs.length,'nav errors',errs[0]); await ctx.close();}
 // gift in ticker
 {const {p,ctx}=await page('2026-10-10T11:00:00Z',{gift:j=>{j.quote_time='10-Oct-2026 16:00:00';j.last=22575.5;j.change=-27;j.change_pct=-0.119;return j;}});
  const t=await p.$eval('#tkRun',e=>e.innerText); ok(/GIFT Nifty/.test(t)&&/22,575.5/.test(t),'gift in ticker',t.slice(0,200)); ok(t.indexOf('Brent')<t.indexOf('GIFT')||t.indexOf('Brent')<0,'brent before gift'); await ctx.close();}
 {const {p,ctx,errs}=await page('2026-10-20T11:00:00Z');const t=await p.$eval('#tkRun',e=>e.innerText);ok(!/GIFT/.test(t),'old gift quote not shown'); ok(!errs.length,'stale gift errors',errs[0]); await ctx.close();}
 for (const [m,fn] of Object.entries({nulls:j=>({last:null}),garbage:j=>({last:'x',change:'y'}),zero:j=>({last:0,change:0,change_pct:0})})) {const {p,ctx,errs}=await page('2026-10-10T11:00:00Z',{gift:fn});ok(!errs.length&&!/NaN|undefined/.test(await p.$eval('#tkRun',e=>e.innerText)),'gift '+m,errs[0]);await ctx.close();}
 // health line
 {const {p,ctx,errs}=await page('2026-10-12T08:30:00Z',{config:j=>{j.profile.github_token_expires='2026-10-30';return j;}});
  const t=await p.$eval('#healthLine',e=>e.hidden?'':e.innerText); ok(/Prices/.test(t)&&/NAVs graded/.test(t)&&/Statement of/.test(t)&&/token valid to 30 Oct/.test(t),'health line items',t); ok(!errs.length,'health errors',errs[0]); await ctx.close();}
 {const {p,ctx}=await page('2026-12-10T08:30:00Z',{config:j=>{j.profile.github_token_expires='2026-11-30';return j;}});const t=await p.$eval('#healthLine',e=>e.innerText);ok(/token expired/.test(t),'token expired shown',t);ok(/NAV grading behind/.test(t)||/Prices from/.test(t),'stale warnings',t);await ctx.close();}
 // long history + ranges
 for (const w of [1200,390]) {const {p,ctx,errs}=await page('2026-10-12T08:30:00Z',{portfolio_history:longHist},w);
  ok(await p.$$eval('#moneyRange button',b=>b.length===4),'range tabs '+w); await p.click('#moneyRange button[data-r="1Y"]'); ok(/1 trading days|trading days/.test(await p.$eval('#moneyNote',e=>e.textContent)),'1Y range');
  const n1=parseInt((await p.$eval('#moneyNote',e=>e.textContent)).match(/(\d+) trading/)[1]); await p.click('#moneyRange button[data-r="All"]'); const n2=parseInt((await p.$eval('#moneyNote',e=>e.textContent)).match(/(\d+) trading/)[1]); ok(n1<n2,'range filters',n1+' vs '+n2);
  ok(/transaction report/.test(await p.$eval('#moneyFoot',e=>e.textContent)),'tx footnote');
  await p.hover('#mhit',{position:{x:200,y:60}}); ok(await p.$eval('#mtip',e=>!e.hidden),'tooltip long'); await p.click('#calPrev'); ok(!errs.length,'long history errors '+w,errs[0]);
  await (await p.$('#moneyWrap')).screenshot({path:`w_long_${w}.png`}); await ctx.close();}
 console.log(`${n-fail}/${n} passed`); await b.close(); process.exit(fail?1:0);})();
