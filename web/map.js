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
      link.rel = 'stylesheet'; link.href = 'vendor/maplibre/maplibre-gl.css'; link.dataset.maplibre = '1';
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

// 휴대폰 위치 (기기 안에서만 사용, 서버로 보내지 않음)
export function myPosition() {
  return new Promise((res, rej) => {
    if (!navigator.geolocation) return rej(new Error('이 기기는 위치 기능을 지원하지 않습니다'));
    navigator.geolocation.getCurrentPosition((p) => res([p.coords.longitude, p.coords.latitude]),
      () => rej(new Error('위치를 가져오지 못했습니다. 위치 권한을 확인해 주세요')), { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
  });
}
