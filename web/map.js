// 온라인 지도 (MapLibre + OpenFreeMap 바탕 지도)
//
// 개인정보 반출 방지 원칙
// - 지도 서비스로 나가는 것은 "지도 조각(타일)·글꼴·아이콘 요청"뿐이다. 요청 주소는 지도 서비스가 정한 형식이며
//   이름·연락처·주소·접수번호를 넣지 않는다. 주소로 위치를 찾는 검색(지오코딩)·길찾기 API는 쓰지 않는다.
// - 방문 지점 핀은 이 휴대폰 화면에서만 그린다. 좌표를 서버로 보내지 않는다.
// - 허용된 지도 서버 외의 주소는 요청 단계에서 막는다(아래 transformRequest + index.html CSP 이중 차단).
// - 쿠키·Referer를 보내지 않는다.

export const MAP_HOST = 'https://tiles.openfreemap.org/';
const STYLE_URL = MAP_HOST + 'styles/liberty';
export const DEFAULT_CENTER = [126.9233, 35.1462]; // 광주 동구청 부근 (경도, 위도)
const KO_FONTS = "'Apple SD Gothic Neo', 'Noto Sans KR', 'Malgun Gothic', sans-serif";

let libPromise = null;
export function loadLib() {
  if (!libPromise) {
    if (!document.querySelector('link[data-maplibre]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet'; link.href = new URL('./vendor/maplibre/maplibre-gl.css', import.meta.url).href; link.dataset.maplibre = '1';
      document.head.append(link);
    }
    libPromise = import('./vendor/maplibre/maplibre-gl.mjs');
  }
  return libPromise;
}

// 허용 주소인지 검사 (테스트에서도 사용)
// 지도 조각은 줌 14(약 2km 단위)까지만 요청한다. 더 확대한 화면은 휴대폰이 그 조각을 키워서 그리므로,
// 지도 서버는 수거팀이 어느 집 앞을 보고 있는지 알 수 없다.
const MAX_TILE_ZOOM = 14;
export const isAllowedMapUrl = (url) => {
  if (typeof url !== 'string' || !url.startsWith(MAP_HOST)) return false;
  const m = url.match(/\/(\d+)\/\d+\/\d+\.(?:pbf|png|mvt|webp|jpg)(?:\?|$)/);
  return !m || Number(m[1]) <= MAX_TILE_ZOOM;
};

export async function createMap(container, { center = DEFAULT_CENTER, zoom = 14 } = {}) {
  const ml = await loadLib();
  const map = new ml.Map({
    container, style: STYLE_URL, center, zoom,
    attributionControl: { compact: true },
    localIdeographFontFamily: KO_FONTS, // 한글은 휴대폰 글꼴로 그려 글꼴 요청을 줄인다
    cooperativeGestures: false,
    transformRequest: (url) => (isAllowedMapUrl(url) ? { url, credentials: 'omit' } : { url: 'data:,' }),
  });
  map.addControl(new ml.NavigationControl({ showCompass: false }), 'top-right');
  map.once('styledata', () => {
    // 지명은 한국어 이름 우선
    for (const layer of map.getStyle().layers) {
      if (layer.type === 'symbol' && layer.layout && layer.layout['text-field']) {
        try { map.setLayoutProperty(layer.id, 'text-field', ['coalesce', ['get', 'name:ko'], ['get', 'name']]); } catch {}
      }
    }
  });
  return { map, ml };
}

// 번호 핀 (휴대폰 화면에서만 그림)
export function pin(ml, map, { lng, lat, label, cls = '', onClick }) {
  const el = document.createElement('button');
  el.type = 'button'; el.className = 'pin ' + cls; el.textContent = label;
  el.setAttribute('aria-label', `${label}번 위치`);
  if (onClick) el.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
  return new ml.Marker({ element: el, anchor: 'bottom' }).setLngLat([lng, lat]).addTo(map);
}

// ------------------------------------------------------------ 지도 미리 받기 (오프라인용)
// 미리 받는 것도 같은 지도 서버의 같은 조각(줌 14 이하)뿐이다. 받은 조각은 서비스 워커가 크기 한도(50MB, 오래 안 쓴 것부터 지움) 안에서 보관한다.
// 요청 주소에는 조각 번호(약 2km 단위 격자)만 들어가며, 어느 지점을 도는지는 범위를 넓혀(여유 1km 이상) 드러나지 않게 한다.
export const TILE_CACHE = 'dg-tiles-v1';
export const TILE_INDEX = 'https://tiles.openfreemap.org/__dg_tile_index__';
export const DONGGU_BBOX = [126.905, 35.06, 127.02, 35.175]; // 광주 동구 대략 범위 [서, 남, 동, 북]
export function lonLatToTile(lon, lat, z) {
  const n = 2 ** z, r = (Math.max(-85, Math.min(85, lat)) * Math.PI) / 180;
  const x = Math.floor(((lon + 180) / 360) * n), y = Math.floor(((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * n);
  return [Math.max(0, Math.min(n - 1, x)), Math.max(0, Math.min(n - 1, y))];
}
export function tilesForBbox([w, s, e, n], zmin = 10, zmax = MAX_TILE_ZOOM) {
  const out = [];
  for (let z = zmin; z <= Math.min(zmax, MAX_TILE_ZOOM); z++) {
    const [x0, y0] = lonLatToTile(w, n, z), [x1, y1] = lonLatToTile(e, s, z);
    for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) out.push({ z, x, y });
  }
  return out;
}
/** 지점 좌표들을 감싸는 범위(여유 pad도). 지점이 없으면 동구 범위. */
export function bboxAround(points, pad = 0.012) {
  const ok = points.filter((p) => p && p.lat != null && p.lon != null);
  if (!ok.length) return DONGGU_BBOX.slice();
  const lons = ok.map((p) => p.lon), lats = ok.map((p) => p.lat);
  return [Math.min(...lons) - pad, Math.min(...lats) - pad, Math.max(...lons) + pad, Math.max(...lats) + pad];
}
const getJson = async (u) => { const r = await fetch(u, { credentials: 'omit', referrerPolicy: 'no-referrer' }); if (!r.ok) throw new Error(String(r.status)); return r.json(); };
/** 바탕 지도 양식·조각 목록·조각을 받아 둔다(서비스 워커가 저장). onProgress(done, total) */
export async function prefetchTiles(bbox, onProgress = () => {}, { zmin = 10, limit = 1500 } = {}) {
  const style = await getJson(STYLE_URL);
  const templates = [];
  for (const src of Object.values(style.sources || {})) {
    if (src.tiles) templates.push(...src.tiles);
    else if (src.url && isAllowedMapUrl(src.url)) { const tj = await getJson(src.url); templates.push(...(tj.tiles || [])); }
  }
  const extra = [];
  const sprites = typeof style.sprite === 'string' ? [style.sprite] : (style.sprite || []).map((x) => x.url);
  for (const sp of sprites) extra.push(sp + '.json', sp + '.png', sp + '@2x.json', sp + '@2x.png');
  if (style.glyphs) {
    const stacks = new Set();
    for (const l of style.layers || []) { const f = l.layout && l.layout['text-font']; if (Array.isArray(f) && f.every((x) => typeof x === 'string')) stacks.add(f.join(',')); }
    for (const fs of stacks) extra.push(style.glyphs.replace('{fontstack}', encodeURIComponent(fs)).replace('{range}', '0-255'));
  }
  const tiles = tilesForBbox(bbox, zmin).slice(0, limit);
  const urls = [...extra, ...templates.flatMap((t) => tiles.map(({ z, x, y }) => t.replace('{z}', z).replace('{x}', x).replace('{y}', y)))].filter(isAllowedMapUrl);
  let done = 0, fail = 0, i = 0;
  const worker = async () => {
    while (i < urls.length) {
      const u = urls[i++];
      try { const r = await fetch(u, { credentials: 'omit', referrerPolicy: 'no-referrer' }); if (!r.ok) fail++; else await r.arrayBuffer(); } catch { fail++; }
      onProgress(++done, urls.length);
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker));
  return { total: urls.length, fail, tiles: tiles.length };
}
/** 저장된 지도 조각 수·크기 (서비스 워커가 남긴 목록) */
export async function tileCacheInfo() {
  try {
    const c = await caches.open(TILE_CACHE);
    const r = await c.match(TILE_INDEX);
    const idx = r ? await r.json() : {};
    const vals = Object.values(idx);
    return { count: vals.length, bytes: vals.reduce((a, v) => a + (v[0] || 0), 0) };
  } catch { return { count: 0, bytes: 0 }; }
}

// 휴대폰 위치 (기기 안에서만 사용, 서버로 보내지 않음)
export function myPosition() {
  return new Promise((res, rej) => {
    if (!navigator.geolocation) return rej(new Error('이 기기는 위치 기능을 지원하지 않습니다'));
    navigator.geolocation.getCurrentPosition((p) => res([p.coords.longitude, p.coords.latitude]),
      () => rej(new Error('위치를 가져오지 못했습니다. 위치 권한을 확인해 주세요')), { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
  });
}

/** 위치와 정확도. ask=false면 이미 허용된 경우에만(권한 창을 띄우지 않음) 읽는다. 실패하면 null. */
export async function myFix({ ask = true, timeout = 8000 } = {}) {
  try {
    if (!navigator.geolocation) return null;
    if (!ask && navigator.permissions) {
      const p = await navigator.permissions.query({ name: 'geolocation' });
      if (p.state !== 'granted') return null;
    }
    return await new Promise((res) => navigator.geolocation.getCurrentPosition(
      (p) => res({ lat: Math.round(p.coords.latitude * 1e6) / 1e6, lon: Math.round(p.coords.longitude * 1e6) / 1e6, acc: Math.round(p.coords.accuracy || 0) }),
      () => res(null), { enableHighAccuracy: true, timeout, maximumAge: 30000 }));
  } catch { return null; }
}
