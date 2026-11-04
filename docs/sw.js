// MF Pulse service worker: keeps the site usable when the connection to GitHub is slow or down.
// Pages and data: ask the network, but wait at most 2.5 seconds, then show the copy from the last visit (the request carries on in the
// background and refreshes that copy). Fonts and icons: from the copy once there is one. Other websites' requests are left alone.
const CACHE = "mfp-v1", WAIT_MS = 2500;
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k);
  await self.clients.claim();
})()));
self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  const key = url.origin + url.pathname;                                  // ignore ?t=… cache-busters
  const still = /\/fonts\/|\.(png|svg|webmanifest)$/.test(url.pathname);   // fonts and icons hardly ever change
  e.respondWith((async () => {
    const cache = await caches.open(CACHE), hit = await cache.match(key);
    if (still && hit) return hit;
    const net = fetch(req).then(r => { if (r.ok) cache.put(key, r.clone()); return r; });
    if (!hit) return net;
    e.waitUntil(net.catch(() => {}));
    return Promise.race([net.catch(() => hit), new Promise(res => setTimeout(() => res(hit), WAIT_MS))]);
  })());
});
