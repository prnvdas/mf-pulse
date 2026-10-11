const { chromium } = require('playwright');
const URL=process.env.BASE||(process.env.BASE||'http://localhost:8000/index.html'); const BAD=/\bundefined\b|\bNaN\b|\[object|Infinity|\$\{/;
(async()=>{const b=await chromium.launch(); let n=0,fail=0; const out=(ok,name,d)=>{n++; if(!ok){fail++; console.log('FAIL',name,d||'');}};
 const clocks=[['Mon 08:00','2026-10-12T02:30:00Z'],['Mon 11:00 open','2026-10-12T05:30:00Z'],['Mon 15:40','2026-10-12T10:10:00Z'],['Mon 20:00','2026-10-12T14:30:00Z'],['Tue 02:00','2026-10-12T20:30:00Z'],['Sat 11:00','2026-10-10T05:30:00Z'],['Sun 20:00','2026-10-11T14:30:00Z'],['Muhurat Sun 18:00','2026-11-08T12:30:00Z'],['Gandhi Jayanti','2026-10-02T05:30:00Z'],['Dussehra eve','2026-10-19T14:30:00Z'],['5 Nov stale statement','2026-11-05T05:30:00Z'],['New Year','2027-01-01T05:30:00Z']];
 for (const [label,iso] of clocks) for (const [w,tag] of [[1280,'desktop'],[390,'mobile']]) {
  const ctx=await b.newContext({viewport:{width:w,height:900}}); const p=await ctx.newPage(); const errs=[]; p.on('pageerror',e=>errs.push(e.message));
  await p.clock.install({time:new Date(iso)}); await p.goto(URL,{waitUntil:'networkidle'}); await p.waitForTimeout(400); await p.clock.fastForward(4000); await p.waitForTimeout(250);
  const r=await p.evaluate(()=>({len:document.body.innerText.length,txt:document.body.innerText,over:document.documentElement.scrollWidth>innerWidth+2,rows:document.querySelectorAll('table tbody tr').length}));
  const probs=[]; if(errs.length) probs.push(errs[0]); if(BAD.test(r.txt)) probs.push('bad token: '+(r.txt.match(BAD)||[])[0]); if(r.over&&tag==='mobile') probs.push('overflow'); if(r.len<800) probs.push('nearly empty');
  out(!probs.length,`clock ${label} [${tag}]`,probs.join('; ')); await ctx.close(); }
 const files=['latest','config','history','analytics','news','outlook','movers','accuracy','risk','tax','holidays','mood','advisor','gift','flows','portfolio_history','fund_health','picks','crypto'];
 const modes={'404':r=>r.fulfill({status:404,body:''}),'500':r=>r.fulfill({status:500,body:'x'}),'garbage':r=>r.fulfill({status:200,contentType:'application/json',body:'{"a": <html>'}),'empty obj':r=>r.fulfill({status:200,contentType:'application/json',body:'{}'}),'empty list':r=>r.fulfill({status:200,contentType:'application/json',body:'[]'}),'null':r=>r.fulfill({status:200,contentType:'application/json',body:'null'}),'abort':r=>r.abort()};
 for (const f of files) for (const [m,fn] of Object.entries(modes)) {
  const ctx=await b.newContext({viewport:{width:1100,height:800}}); const p=await ctx.newPage(); const errs=[]; p.on('pageerror',e=>errs.push(e.message));
  await p.clock.install({time:new Date('2026-10-12T05:30:00Z')}); await p.route(new RegExp('/data/'+f+'\\.json'),fn);
  await p.goto(URL,{waitUntil:'networkidle'}); await p.waitForTimeout(450);
  const r=await p.evaluate(()=>({len:document.body.innerText.length,txt:document.body.innerText}));
  const probs=[]; if(errs.length) probs.push(errs[0].slice(0,90)); if(BAD.test(r.txt)) probs.push('bad token '+(r.txt.match(BAD)||[])[0]); if(r.len<300) probs.push('blank');
  out(!probs.length,`${f}.json ${m}`,probs.join('; ')); await ctx.close(); }
 console.log(`\n${n-fail}/${n} passed`); await b.close(); process.exit(fail?1:0);})();
