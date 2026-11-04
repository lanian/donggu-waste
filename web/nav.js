// 길안내: 휴대폰에 깔린 지도 앱(카카오맵·T맵·네이버지도·기본 지도 앱)을 목적지 좌표로 연다. (순수 함수: Node에서도 시험)
//
// 개인정보 원칙 (v0.10.1)
// - 지도 앱에 넘기는 것은 목적지 좌표(위도·경도 소수 6자리)와 표시 이름 "N번 수거지" 또는 "반입지"뿐이다.
//   주민 이름·연락처·주소·접수번호는 넘기지 않는다. 표시 이름은 순번에서만 만들고(navLabel), 다른 글자는 거부한다(safeLabel).
// - 앱이 없을 때를 대비한 웹 주소(카카오맵·네이버지도)도 같은 좌표·표시 이름만 담는다. T맵은 웹 길안내가 없어 기본 지도 앱(geo:)으로 넘긴다.
// - 지도 앱이 어떤 경로를 안내했는지, 실제로 갔는지는 이 앱이 알 수 없다(앱을 연 기록 NAV_INTENT만 남김).

export const NAV_APPS = { kakao: '카카오맵', tmap: 'T맵', naver: '네이버지도', geo: '기본 지도 앱' };
export const DEFAULT_NAV_APP = 'kakao';
export const NAV_FALLBACK_MS = 1500; // 이 시간 안에 화면이 다른 앱으로 넘어가지 않으면 앱이 없는 것으로 보고 웹 주소를 연다

/** 지점 표시 이름: 순번만 쓴다. 반입지는 "반입지". */
export const navLabel = (seq) => (seq === 'UNLOAD' ? '반입지' : `${Number(seq) || 0}번 수거지`);
const LABEL_RE = /^(?:\d{1,4}번 수거지|반입지)$/;
export const safeLabel = (label) => (LABEL_RE.test(label || '') ? label : '수거지');

const coord = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) throw new Error('BAD_COORD');
  return n.toFixed(6);
};
const ok = (lat, lon) => Number.isFinite(Number(lat)) && Number.isFinite(Number(lon)) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 && !(Number(lat) === 0 && Number(lon) === 0);
export const hasCoord = (p) => !!p && p.lat != null && p.lon != null && ok(p.lat, p.lon);

/** 기본 지도 앱: Android는 geo: (표시 이름은 q의 괄호), iOS는 Apple 지도 maps:// (자동차 길안내). */
export function geoUrl(lat, lon, label, ios = false) {
  const y = coord(lat), x = coord(lon);
  return ios ? `maps://?daddr=${y},${x}&dirflg=d` : `geo:${y},${x}?q=${y},${x}(${encodeURIComponent(safeLabel(label))})`;
}

/** 앱 주소와(있으면) 앱이 없을 때 열 웹 주소.
 *  - 카카오맵: kakaomap://route?ep=위도,경도&by=CAR  (출발지 sp를 빼면 현재 위치에서 출발). 웹: https://map.kakao.com/link/to/이름,위도,경도
 *  - T맵: tmap://route?goalx=경도&goaly=위도&goalname=이름  (x=경도, y=위도). 웹 길안내 없음 → 기본 지도 앱
 *  - 네이버지도: nmap://route/car?dlat=위도&dlng=경도&dname=이름&appname=호출한 앱 식별자. 웹: https://m.map.naver.com/route.nhn?menu=route&ex=경도&ey=위도&ename=이름&pathType=0
 *  - 기본 지도 앱: geoUrl
 *  돌려주는 값: { app: 'kakao'|'tmap'|'naver'|'geo'|'apple-maps', url, fallback: string|null } */
export function navUrls(app, { lat, lon, label }, { ios = false, appname = 'donggu-waste' } = {}) {
  if (!ok(lat, lon)) throw new Error('NO_COORD');
  const y = coord(lat), x = coord(lon), name = encodeURIComponent(safeLabel(label));
  const geo = geoUrl(lat, lon, label, ios);
  switch (app in NAV_APPS ? app : DEFAULT_NAV_APP) {
    case 'kakao':
      return { app: 'kakao', url: `kakaomap://route?ep=${y},${x}&by=CAR`, fallback: `https://map.kakao.com/link/to/${name},${y},${x}` };
    case 'tmap':
      return { app: 'tmap', url: `tmap://route?goalx=${x}&goaly=${y}&goalname=${name}`, fallback: geo };
    case 'naver':
      return { app: 'naver', url: `nmap://route/car?dlat=${y}&dlng=${x}&dname=${name}&appname=${encodeURIComponent(appname)}`,
        fallback: `https://m.map.naver.com/route.nhn?menu=route&ex=${x}&ey=${y}&ename=${name}&pathType=0&showMap=true` };
    default:
      return { app: ios ? 'apple-maps' : 'geo', url: geo, fallback: null };
  }
}
