// 서비스 워커: 앱 파일을 저장해 오프라인으로 실행하고, 바탕 지도 조각을 크기 한도 안에서 보관한다. 업무 자료는 다루지 않는다.
// 주소 검색(business.juso.go.kr) 응답은 저장하지 않는다(다른 출처 요청은 아래에서 그대로 통과).
// 새 버전은 자동 적용하지 않고, 앱 화면의 [업데이트]를 눌러야 바뀐다.
const VERSION = '0.12.12';
const CACHE = 'dg-app-' + VERSION;
const FILES = ['./', './index.html', './style.css', './app.js', './proto.js', './store.js', './fees.js', './fees-data.js', './backup.js', './map.js',
  './field.js', './bio.js', './nav.js', './juso.js',
  './vendor/maplibre/maplibre-gl.mjs', './vendor/maplibre/maplibre-gl-worker.mjs', './vendor/maplibre/maplibre-gl.css',
  './vendor/jsQR.js', './vendor/qrcode.mjs', './manifest.webmanifest', './icons/icon.svg', './icons/icon-180.png', './icons/icon-192.png', './icons/icon-512.png'];

// ---- 바탕 지도 조각 (tiles.openfreemap.org, 줌 14 이하만) ----
// 같은 이름을 map.js에서도 쓴다(설정 화면의 보관 크기 표시).
const TILE_HOST = 'https://tiles.openfreemap.org/';
const TILE_CACHE = 'dg-tiles-v1';
const TILE_INDEX = TILE_HOST + '__dg_tile_index__';
const TILE_CAP = 50 * 1024 * 1024; // 50MB 넘으면 오래 안 쓴 것부터 지움
const tileZoomOk = (url) => { const m = url.match(/\/(\d+)\/\d+\/\d+\.(?:pbf|png|mvt|webp|jpg)(?:\?|$)/); return !m || Number(m[1]) <= 14; };
const isTile = (url) => /\/\d+\/\d+\/\d+\.(?:pbf|png|mvt|webp|jpg)(?:\?|$)/.test(url);
let idx = null, saveTimer = null;
async function loadIdx() {
  if (idx) return idx;
  try { const r = await (await caches.open(TILE_CACHE)).match(TILE_INDEX); idx = r ? await r.json() : {}; } catch { idx = {}; }
  return idx;
}
function saveIdxSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const c = await caches.open(TILE_CACHE);
    let total = Object.values(idx).reduce((a, v) => a + v[0], 0);
    if (total > TILE_CAP) {
      for (const [u, v] of Object.entries(idx).sort((a, b) => a[1][1] - b[1][1])) {
        if (total <= TILE_CAP * 0.9) break;
        await c.delete(u); delete idx[u]; total -= v[0];
      }
    }
    await c.put(TILE_INDEX, new Response(JSON.stringify(idx), { headers: { 'content-type': 'application/json' } }));
  }, 1500);
}
async function tileFetch(req) {
  const c = await caches.open(TILE_CACHE);
  const key = req.url;
  const hit = await c.match(key, { ignoreVary: true, ignoreSearch: false });
  await loadIdx();
  if (hit && isTile(key)) { if (idx[key]) { idx[key][1] = Date.now(); saveIdxSoon(); } return hit; } // 조각은 저장본 먼저
  try {
    const res = await fetch(req);
    if (res.ok) {
      const buf = await res.clone().arrayBuffer();
      await c.put(key, res.clone());
      idx[key] = [buf.byteLength, Date.now()]; saveIdxSoon();
    }
    return res;
  } catch (err) {
    if (hit) return hit; // 양식·목록은 인터넷 먼저, 안 되면 저장본
    throw err;
  }
}

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES.map((f) => new Request(f, { cache: 'reload' })))));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith('dg-app-') && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('message', (e) => { if (e.data === 'SKIP_WAITING') self.skipWaiting(); });
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (e.request.url.startsWith(TILE_HOST)) {
    // 줌 15 이상(집 단위로 좁혀지는 조각)은 요청하지 않는다 — map.js transformRequest와 이중 차단
    if (!tileZoomOk(e.request.url) || e.request.url === TILE_INDEX) { e.respondWith(new Response('', { status: 403 })); return; }
    e.respondWith(tileFetch(e.request));
    return;
  }
  if (url.origin !== self.location.origin) return;
  e.respondWith(caches.match(e.request, { ignoreSearch: true }).then((hit) => hit ||
    (e.request.mode === 'navigate' ? caches.match('./index.html') : fetch(e.request))));
});
