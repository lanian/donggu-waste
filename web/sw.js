// 서비스 워커: 앱 파일만 저장해 오프라인으로 실행한다. 업무 자료는 다루지 않는다.
// 새 버전은 자동 적용하지 않고, 앱 화면의 [업데이트]를 눌러야 바뀐다.
const VERSION = '0.3.0';
const CACHE = 'dg-app-' + VERSION;
const FILES = ['./', './index.html', './style.css', './app.js', './proto.js', './store.js', './fees.js', './fees-data.js', './backup.js', './map.js',
  './vendor/maplibre/maplibre-gl.mjs', './vendor/maplibre/maplibre-gl-worker.mjs', './vendor/maplibre/maplibre-gl.css',
  './vendor/jsQR.js', './manifest.webmanifest', './icons/icon.svg', './icons/icon-180.png', './icons/icon-192.png', './icons/icon-512.png'];

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
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  e.respondWith(caches.match(e.request, { ignoreSearch: true }).then((hit) => hit ||
    (e.request.mode === 'navigate' ? caches.match('./index.html') : fetch(e.request))));
});
