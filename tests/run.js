// Runs every suite against the page: node tests/run.js [suite-name ...]
// 1. frozen fixtures (tests/fixtures/data) on port 8000: behaviour, layout, hostile data files, clock states
// 2. the real docs/data on port 8001 with the real clock: "does the live site render what the nightly jobs produced?"
// Suites run side by side (TEST_JOBS, default 3) and a suite that fails is run once more: a one-off network timeout then
// shows up as a warning instead of failing the run, while a real failure fails twice. Each suite's output is printed in one piece.
const { spawn } = require('child_process'), fs = require('fs'), path = require('path'), start = require('./server');
const JOBS = Math.max(1, Number(process.env.TEST_JOBS) || 3), RETRIES = process.env.TEST_RETRIES === undefined ? 1 : Number(process.env.TEST_RETRIES);
const dir = path.join(__dirname, 'suites');

// asynchronous on purpose: the servers started in main() live in this process and must keep answering while a suite runs
function runOnce(name) {
  const live = name === 'live';
  return new Promise(res => {
    const c = spawn(process.execPath, [path.join(dir, name + '.js')], { stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, NODE_OPTIONS: `--require ${path.join(__dirname, 'preload.js')}`, BASE: `http://127.0.0.1:${live ? 8001 : 8000}/index.html` }, cwd: __dirname });
    let out = '';
    c.stdout.on('data', d => { out += d; }); c.stderr.on('data', d => { out += d; });
    c.on('exit', code => res({ code, out }));
  });
}

async function runSuite(name) {
  let r = await runOnce(name), tries = 0;
  const first = r;
  while (r.code !== 0 && tries < RETRIES) { tries++; r = await runOnce(name); }
  return { name, ok: r.code === 0, out: r.out, retried: tries && r.code === 0 ? first.out : '' };
}

(async () => {
  const s1 = await start(8000, false), s2 = await start(8001, true);
  const want = process.argv.slice(2);
  const suites = fs.readdirSync(dir).filter(f => f.endsWith('.js')).map(f => f.slice(0, -3)).filter(n => !want.length || want.includes(n)).sort();
  const queue = [...suites], results = [];
  const ga = !!process.env.GITHUB_ACTIONS, note = (kind, name, l) => console.log(`::${kind} title=${name}::${l.trim().slice(0, 300).replace(/[\r\n%]/g, ' ')}`);
  const worker = async () => {
    while (queue.length) {
      const r = await runSuite(queue.shift());
      results.push(r);
      console.log(`\n=== ${r.name} ===\n${r.out.trimEnd()}`);
      if (r.retried) {
        console.log(`*** ${r.name} failed once and passed on the second run. First run:\n${r.retried.trimEnd()}`);
        if (ga) note('warning', r.name, 'failed once, passed on retry (a one-off timeout?)');
      }
      if (!r.ok) {
        console.log(`*** ${r.name} FAILED`);
        if (ga) { let seen = 0; for (const l of r.out.split('\n')) if (/^FAIL|Error|failed|Timeout/.test(l.trim()) && seen++ < 25) note('error', r.name, l); }
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(JOBS, suites.length) }, worker));
  s1.close(); s2.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(failed ? `\n${failed} suite(s) failed` : '\nall suites passed');
  process.exit(failed ? 1 : 0);
})();
