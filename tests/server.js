// Tiny static server for the page: docs/ as the site root; /data/* comes from tests/fixtures/data (frozen, so the
// tests don't change when the nightly jobs update the real data) unless `live` is true.
const http = require('http'), fs = require('fs'), path = require('path');
const DOCS = path.join(__dirname, '..', 'docs'), FIX = path.join(__dirname, 'fixtures');
const TYPES = { '.html': 'text/html; charset=utf-8', '.json': 'application/json', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.webmanifest': 'application/manifest+json' };
module.exports = function start(port, live) {
  return new Promise(resolve => {
    const srv = http.createServer((req, res) => {
      const url = decodeURIComponent(req.url.split('?')[0]);
      const rel = url === '/' ? '/index.html' : url;
      const file = path.normalize(path.join(!live && rel.startsWith('/data/') ? FIX : DOCS, rel));
      if (!file.startsWith(DOCS) && !file.startsWith(FIX)) { res.writeHead(403); return res.end(); }
      fs.readFile(file, (err, buf) => {
        if (err) { res.writeHead(404); return res.end('not found'); }
        res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
        res.end(buf);
      });
    });
    srv.listen(port, '127.0.0.1', () => resolve(srv));
  });
};
