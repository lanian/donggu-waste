import assert from 'node:assert/strict';
import { navLabel, navUrls, safeLabel } from '../web/nav.js';

const location = { lat: 35.15123456, lon: 126.95123456 };
const depot = navLabel('DEPOT');
assert.equal(depot, '차고지');
assert.equal(safeLabel(depot), depot);
assert.equal(navLabel('UNLOAD'), '반입지');
assert.equal(navLabel(6), '6번 수거지');

const destination = { ...location, label: depot };
const tmap = navUrls('tmap', destination);
const naver = navUrls('naver', destination);
const kakao = navUrls('kakao', destination);
assert.match(decodeURIComponent(tmap.url), /goalx=126\.951235&goaly=35\.151235&goalname=차고지$/);
assert.match(decodeURIComponent(tmap.fallback), /35\.151235,126\.951235\(차고지\)$/);
assert.match(decodeURIComponent(naver.url), /dlat=35\.151235&dlng=126\.951235&dname=차고지&appname=/);
assert.match(decodeURIComponent(naver.fallback), /ename=차고지&/);
assert.match(decodeURIComponent(kakao.fallback), /\/차고지,35\.151235,126\.951235$/);
assert.equal(navUrls('geo', destination, { ios: true }).url, 'maps://?daddr=35.151235,126.951235&dirflg=d');

const untrusted = '가상 주민 차고지&추가정보';
assert.equal(safeLabel(untrusted), '수거지');
assert.match(decodeURIComponent(navUrls('tmap', { ...location, label: untrusted }).url), /goalname=수거지$/);
console.log('모바일 길안내 차고지 라벨 통과');
