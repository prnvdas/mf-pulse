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
    const code = await new Promise(res => spawn(process.execPath, [path.join(dir, name + '.js')], { stdio: 'inherit', env: { ...process.env, BASE: `http://127.0.0.1:${live ? 8001 : 8000}/index.html` }, cwd: __dirname }).on('exit', res));
    if (code !== 0) { failed++; console.log(`*** ${name} FAILED`); }
  }
  s1.close(); s2.close();
  console.log(failed ? `\n${failed} suite(s) failed` : '\nall suites passed');
  process.exit(failed ? 1 : 0);
})();
