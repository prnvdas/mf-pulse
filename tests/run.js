// Runs every suite against the page: node tests/run.js [suite-name ...]
// 1. frozen fixtures (tests/fixtures/data) on port 8000: behaviour, layout, hostile data files, clock states
// 2. the real docs/data on port 8001 with the real clock: "does the live site render what the nightly jobs produced?"
const { spawn } = require('child_process'), fs = require('fs'), path = require('path'), start = require('./server');
(async () => {
  const s1 = await start(8000, false), s2 = await start(8001, true);
  const want = process.argv.slice(2), dir = path.join(__dirname, 'suites');
  const suites = fs.readdirSync(dir).filter(f => f.endsWith('.js')).map(f => f.slice(0, -3)).filter(n => !want.length || want.includes(n)).sort();
  let failed = 0;
  for (const name of suites) {
    const live = name === 'live';
    console.log(`\n=== ${name} ===`);
    // asynchronous on purpose: the servers above live in this process and must keep answering while the suite runs
    // output is passed through, and in GitHub Actions each failure also becomes an annotation (visible without the raw log)
    const code = await new Promise(res => {
      const c = spawn(process.execPath, [path.join(dir, name + '.js')], { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, BASE: `http://127.0.0.1:${live ? 8001 : 8000}/index.html` }, cwd: __dirname });
      let seen = 0;
      const tee = stream => d => { const t = d.toString(); stream.write(t);
        if (process.env.GITHUB_ACTIONS) for (const l of t.split('\n')) if (/^FAIL|Error|failed|Timeout/.test(l.trim()) && seen++ < 25) console.log(`::error title=${name}::${l.trim().slice(0, 300).replace(/[\r\n%]/g, ' ')}`); };
      c.stdout.on('data', tee(process.stdout)); c.stderr.on('data', tee(process.stderr)); c.on('exit', res);
    });
    if (code !== 0) { failed++; console.log(`*** ${name} FAILED`); }
  }
  s1.close(); s2.close();
  console.log(failed ? `\n${failed} suite(s) failed` : '\nall suites passed');
  process.exit(failed ? 1 : 0);
})();
