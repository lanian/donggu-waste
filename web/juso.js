// 주소 검색: 행정안전부 도로명주소 검색 API + 좌표제공 API (주소정보누리집, business.juso.go.kr)
//
// 개인정보 원칙 (v0.10.1)
// - 보내는 것: 검색칸에 직접 적은 검색어(주소 낱말)와 기관 승인키뿐이다. 주민 이름·연락처·메모·접수번호는 보내지 않는다.
//   좌표를 받을 때는 검색 결과에 들어 있던 건물 식별 번호(행정구역코드·도로명코드·지하여부·건물 본번·부번)만 보낸다.
// - 쿠키·Referer를 보내지 않는다(credentials: 'omit', referrerPolicy: 'no-referrer'). 요청 주소는 index.html CSP connect-src로 이 서버만 허용.
// - 브라우저가 CORS로 막으면(서버가 허용 머리글을 주지 않는 경우) 검색이 실패한다 → 화면은 직접 입력·지도에서 위치 지정으로 넘어간다.
//   이 경우 주소 작업은 PC(PC 연결 방식)에서 한다. JSONP(외부 스크립트 실행)는 CSP(script-src 'self')를 지키기 위해 쓰지 않는다.
// - 좌표는 UTM-K(EPSG:5179, GRS80)로 오므로 이 휴대폰 안에서 WGS84 위도·경도로 바꾼다(pc/dgpc/geo.py와 같은 식).

export const JUSO_HOST = 'https://business.juso.go.kr';
export const SEARCH_URL = JUSO_HOST + '/addrlink/addrLinkApi.do';
export const COORD_URL = JUSO_HOST + '/addrlink/addrCoordApi.do';

// ------------------------------------------------------------ UTM-K (EPSG:5179) ↔ WGS84
// GRS80 타원체, 원점 위도 38°·경도 127.5°, 축척 0.9996, 가산 1,000,000(동)·2,000,000(북). 횡메르카토르(Snyder 식).
const A = 6378137.0, F = 1 / 298.257222101, E2 = F * (2 - F), EP2 = E2 / (1 - E2);
const LAT0 = (38 * Math.PI) / 180, LON0 = (127.5 * Math.PI) / 180, K0 = 0.9996, FE = 1000000, FN = 2000000;
const mArc = (phi) => {
  const e4 = E2 * E2, e6 = e4 * E2;
  return A * ((1 - E2 / 4 - (3 * e4) / 64 - (5 * e6) / 256) * phi - ((3 * E2) / 8 + (3 * e4) / 32 + (45 * e6) / 1024) * Math.sin(2 * phi)
    + ((15 * e4) / 256 + (45 * e6) / 1024) * Math.sin(4 * phi) - ((35 * e6) / 3072) * Math.sin(6 * phi));
};
const M0 = mArc(LAT0);
const deg = (r) => (r * 180) / Math.PI, rad = (d) => (d * Math.PI) / 180;

export function wgs84ToUtmk(lat, lon) {
  const phi = rad(lat), lam = rad(lon);
  const n = A / Math.sqrt(1 - E2 * Math.sin(phi) ** 2);
  const t = Math.tan(phi) ** 2, c = EP2 * Math.cos(phi) ** 2, a = (lam - LON0) * Math.cos(phi);
  const x = FE + K0 * n * (a + ((1 - t + c) * a ** 3) / 6 + ((5 - 18 * t + t * t + 72 * c - 58 * EP2) * a ** 5) / 120);
  const y = FN + K0 * (mArc(phi) - M0 + n * Math.tan(phi) * ((a * a) / 2 + ((5 - t + 9 * c + 4 * c * c) * a ** 4) / 24
    + ((61 - 58 * t + t * t + 600 * c - 330 * EP2) * a ** 6) / 720));
  return [x, y];
}

export function utmkToWgs84(x, y) {
  const m = M0 + (y - FN) / K0;
  const mu = m / (A * (1 - E2 / 4 - (3 * E2 * E2) / 64 - (5 * E2 ** 3) / 256));
  const e1 = (1 - Math.sqrt(1 - E2)) / (1 + Math.sqrt(1 - E2));
  const phi1 = mu + ((3 * e1) / 2 - (27 * e1 ** 3) / 32) * Math.sin(2 * mu) + ((21 * e1 * e1) / 16 - (55 * e1 ** 4) / 32) * Math.sin(4 * mu)
    + ((151 * e1 ** 3) / 96) * Math.sin(6 * mu) + ((1097 * e1 ** 4) / 512) * Math.sin(8 * mu);
  const n1 = A / Math.sqrt(1 - E2 * Math.sin(phi1) ** 2);
  const t1 = Math.tan(phi1) ** 2, c1 = EP2 * Math.cos(phi1) ** 2;
  const r1 = (A * (1 - E2)) / (1 - E2 * Math.sin(phi1) ** 2) ** 1.5;
  const d = (x - FE) / (n1 * K0);
  const lat = phi1 - ((n1 * Math.tan(phi1)) / r1) * ((d * d) / 2 - ((5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * EP2) * d ** 4) / 24
    + ((61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * EP2 - 3 * c1 * c1) * d ** 6) / 720);
  const lon = LON0 + (d - ((1 + 2 * t1 + c1) * d ** 3) / 6 + ((5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * EP2 + 24 * t1 * t1) * d ** 5) / 120) / Math.cos(phi1);
  return [deg(lat), deg(lon)];
}

// ------------------------------------------------------------ 요청 주소
/** 검색어 다듬기: 서버가 거부하는 특수문자(% = < > [ ] 등)를 빼고 공백을 줄인다. 한글 40자(영문·숫자 80자) 제한에 맞춰 자른다. */
export function cleanKeyword(q) {
  return String(q || '').replace(/[%=<>\[\]'"`;\\{}|^]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 40);
}
export function searchUrl(key, keyword, page = 1, per = 10) {
  const p = new URLSearchParams({ confmKey: key, currentPage: String(page), countPerPage: String(per), keyword: cleanKeyword(keyword), resultType: 'json' });
  return `${SEARCH_URL}?${p}`;
}
export function coordUrl(key, j) {
  const p = new URLSearchParams({ confmKey: key, admCd: j.admCd, rnMgtSn: j.rnMgtSn, udrtYn: j.udrtYn ?? '0', buldMnnm: String(j.buldMnnm), buldSlno: String(j.buldSlno ?? 0), resultType: 'json' });
  return `${COORD_URL}?${p}`;
}

export class JusoError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const ERR_TEXT = {
  E0001: '승인키가 맞지 않습니다. 설정에서 승인키를 확인하세요.', E0014: '승인키 사용 기간이 끝났습니다. 설정에서 새 승인키를 넣으세요.',
  E0006: '주소를 조금 더 자세히 적어 주세요 (예: 도로명 + 건물번호).', E0008: '두 글자 이상 적어 주세요.',
  E0013: '특수문자는 빼고 적어 주세요.', E0015: '검색 결과가 너무 많습니다. 더 자세히 적어 주세요.',
};
const opts = { method: 'GET', credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store', mode: 'cors' };
async function getJson(url, fetchFn) {
  let r;
  try { r = await fetchFn(url, opts); }
  catch { throw new JusoError('NETWORK', '주소 검색 서버에 연결하지 못했습니다(인터넷 또는 브라우저 차단). 주소를 직접 적고 지도에서 위치를 지정하세요.'); }
  if (!r.ok) throw new JusoError('HTTP_' + r.status, `주소 검색 서버 오류(${r.status}). 잠시 뒤 다시 하거나 직접 적어 주세요.`);
  const j = await r.json();
  const common = (j && j.results && j.results.common) || {};
  if (String(common.errorCode) !== '0') throw new JusoError(common.errorCode || 'UNKNOWN', ERR_TEXT[common.errorCode] || common.errorMessage || '주소 검색에 실패했습니다.');
  return j.results;
}

/** 도로명주소 검색. 돌려주는 값: { total, items: [{ road, roadPart1, jibun, bdNm, zip, emdNm, hemdNm, admCd, rnMgtSn, udrtYn, buldMnnm, buldSlno }] } */
export async function searchAddress({ key, keyword, page = 1, per = 10, fetchFn = fetch }) {
  const res = await getJson(searchUrl(key, keyword, page, per), fetchFn);
  const items = (res.juso || []).map((x) => ({
    road: x.roadAddr || '', roadPart1: x.roadAddrPart1 || x.roadAddr || '', jibun: x.jibunAddr || '', bdNm: x.bdNm || '', zip: x.zipNo || '',
    siNm: x.siNm || '', sggNm: x.sggNm || '', emdNm: x.emdNm || '', hemdNm: x.hemdNm || '',
    admCd: x.admCd || '', rnMgtSn: x.rnMgtSn || '', udrtYn: x.udrtYn ?? '0', buldMnnm: x.buldMnnm ?? '', buldSlno: x.buldSlno ?? '0',
  }));
  return { total: Number(res.common.totalCount || items.length), items };
}

/** 좌표제공 API → WGS84 { lat, lon } (소수 6자리). 건물 출입구 좌표이므로 실제 배출 위치와 다를 수 있다. */
export async function coordOf({ key, item, fetchFn = fetch }) {
  const res = await getJson(coordUrl(key, item), fetchFn);
  const j = (res.juso || [])[0];
  const x = j && Number(j.entX), y = j && Number(j.entY);
  if (!j || !Number.isFinite(x) || !Number.isFinite(y) || !x || !y) throw new JusoError('NO_COORD', '이 주소의 좌표가 없습니다. 지도에서 위치를 지정하세요.');
  const [lat, lon] = utmkToWgs84(x, y);
  return { lat: Math.round(lat * 1e6) / 1e6, lon: Math.round(lon * 1e6) / 1e6 };
}

// ------------------------------------------------------------ 행정동 제안
// 광주광역시 동구 행정동 13곳 (2026년 기준). 접수에 쓴 동 이름도 함께 비교한다.
export const DONGGU_DONGS = ['충장동', '동명동', '계림1동', '계림2동', '산수1동', '산수2동', '지산1동', '지산2동', '서남동', '학동', '학운동', '지원1동', '지원2동'];
const sq = (s) => String(s || '').replace(/\s+/g, '');
const stem = (s) => sq(s).replace(/(\d+)?(동|읍|면|가)$/, '').replace(/\d+$/, '');
/** 검색 결과의 행정동(hemdNm)·법정동(emdNm)을 아는 동 이름과 맞춘다.
 *  돌려주는 값: { dong: '정확히 하나면 그 이름' | '', candidates: [같은 줄기의 동들] } */
export function suggestDong(item, known = []) {
  const all = [...new Set([...known.map(sq).filter(Boolean), ...DONGGU_DONGS])];
  for (const name of [item.hemdNm, item.emdNm].map(sq).filter(Boolean)) if (all.includes(name)) return { dong: name, candidates: [name] };
  const st = stem(item.hemdNm) || stem(item.emdNm);
  if (!st) return { dong: '', candidates: [] };
  const cands = all.filter((d) => stem(d) === st);
  return { dong: cands.length === 1 ? cands[0] : '', candidates: cands };
}
