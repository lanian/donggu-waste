// PC(Python) ↔ 휴대폰(WebCrypto) 상호 운용 테스트. 가상 자료만 사용한다.
// 실행: npm test   (Python 3 + cryptography + qrcode 필요)
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import * as P from '../web/proto.js';
import { BUILTIN_FEES } from '../web/fees-data.js';
import * as F from '../web/field.js';
import { deriveStop, needsFeeNotice, feeExtra, applyReduction } from '../web/fees.js';
import { tilesForBbox, isAllowedMapUrl, bboxAround, DONGGU_BBOX } from '../web/map.js';
import * as NAV from '../web/nav.js';
import * as J from '../web/juso.js';

const require = createRequire(import.meta.url);
const jsQR = require('jsqr');
const { PNG } = require('pngjs');

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const WORK = process.env.DG_TEST_DIR || mkdtempSync(join(tmpdir(), 'dg-'));
const env = { ...process.env, DG_STATE: join(WORK, 'state'), DG_OUT: join(WORK, 'out'), DG_DB_KEY: process.env.DG_DB_KEY || 'test-db-key-not-for-production' };
const py = (...args) => execFileSync('python3', [join(ROOT, 'pc', 'pc_tool.py'), ...args], { env, encoding: 'utf8' });
const pyExpectFail = (...args) => {
  try { py(...args); } catch (e) { return (e.stdout || '') + (e.stderr || ''); }
  throw new Error('실패해야 하는 명령이 성공함: ' + args.join(' '));
};
const pyEval = (code) => execFileSync('python3', ['-c', code], { env, cwd: join(ROOT, 'pc'), encoding: 'utf8' }).trim();

let passed = 0;
const ok = (cond, name) => { if (!cond) throw new Error('실패: ' + name); passed++; console.log('  ✓', name); };
const throwsCode = async (fn, code, name) => {
  try { await fn(); } catch (e) { return ok(e.code === code, `${name} → ${code} (받은 값 ${e.code})`); }
  throw new Error('실패: ' + name + ' 에서 오류가 나지 않음');
};

console.log('작업 폴더:', WORK);

// 1) PC 초기화와 기기 등록 --------------------------------------------------
py('init');
const reg = P.parseRegistration(pyEval('import pc_tool; print(pc_tool.registration_text())'));
const dev = await P.generateDeviceKeys();
const deviceId = P.randomBytes(16);
const kexRaw = await P.exportRaw(dev.kex.publicKey);
const signRaw = await P.exportRaw(dev.sign.publicKey);
const code = await P.deviceCode(deviceId, kexRaw, signRaw);
const regOut0 = py('register-device', code);
const fp = await P.fingerprint(reg.pcSignPub, reg.pcKexPub, deviceId, kexRaw, signRaw);
ok(regOut0.includes(fp) && fp.length === 9 && regOut0.includes('--confirm'), `기기 등록: 양쪽 9자리 지문 일치, 확인 전에는 사용 대기 (${fp})`);
const fakeKex = (await P.generateDeviceKeys()).kex;
ok((await P.fingerprint(reg.pcSignPub, await P.exportRaw(fakeKex.publicKey), deviceId, kexRaw, signRaw)) !== fp, '등록 QR의 키 합의 키만 바꿔치기해도 지문이 달라짐');
const regOut = py('register-device', code, '--confirm');
ok(regOut.includes('사용 시작'), '지문 확인 뒤 사용 시작');
const pcSignPub = await P.importEcdsaPub(reg.pcSignPub);
const pcKexPub = await P.importEcdhPub(reg.pcKexPub);

// 2) 목록 묶음 수신 ----------------------------------------------------------
const devShort = P.uuidStr(deviceId).slice(0, 8);
const mk = py('make-bundle', '--device', devShort, '--count', '20', '--date', '2026-11-03');
const short = mk.match(/묶음 ([0-9a-f]{8})/)[1];
const frames = JSON.parse(readFileSync(join(WORK, 'out', `bundle_${short}.frames.json`), 'utf8'));
console.log('  ·', mk.split('\n')[0]);

// 섞기 + 중복 + 다른 묶음 조각 혼입
const col = new P.Collector();
const foreign = P.splitFrames(P.randomBytes(900), P.randomBytes(16), 700);
const order = [...frames, frames[0], frames[1], foreign[0]].sort(() => Math.random() - 0.5);
let key;
for (const f of order) { const r = col.add(f); if (r.total === frames.length) key = r.key; }
ok(Object.keys(col.bundles).length === 2, '다른 묶음 조각은 별도 수집함으로 분리');
ok(col.complete(key), '중복 조각을 무시하고 전체 조각 수집 완료');

const envBytes = col.assemble(key);
const opened = await P.openEnvelope(envBytes, pcSignPub, dev.kex.privateKey, deviceId);
ok(opened.payload.schema === 'dg.daylist/2', '압축 형식 v2로 전송');
opened.payload = P.expandDaylist(opened.payload, BUILTIN_FEES);
const s0 = opened.payload.stops[0];
ok(opened.payload.stops.length === 20, '접수 20건 복원');
ok(/^010/.test(s0.phone) && s0.name.length >= 2 && s0.address.includes('동구'), `이름·연락처(앞자리 0)·주소 복원 (${s0.name}, ${s0.phone})`);
const longAddr = opened.payload.stops.find((s) => s.address.length > 40);
ok(!!longAddr, `긴 한글 주소 복원 (${longAddr.address.length}자)`);
py('confirm-receipt', short, opened.receipt);
ok(py('status').includes('수신 확인'), `수신 확인 코드 대조 성공 (${opened.receipt})`);
ok(pyExpectFail('confirm-receipt', short, 'AAAAAAAA').includes('맞지 않습니다'), '틀린 수신 확인 코드는 거부');

// 3) 실패 처리 --------------------------------------------------------------
const c2 = new P.Collector();
frames.slice(1).forEach((f) => c2.add(f));
const k2 = Object.keys(c2.bundles)[0];
ok(c2.missing(k2).join() === '0', '누락 조각 번호 표시 (0번)');
await throwsCode(() => c2.assemble(k2), 'QR_INCOMPLETE', '조각 누락 시 조합 거부');

const tampered = envBytes.slice(); tampered[200] ^= 1;
await throwsCode(() => P.openEnvelope(tampered, pcSignPub, dev.kex.privateKey, deviceId), 'AUTH_FAIL', '암호문 1바이트 변조');
const badHead = envBytes.slice(); badHead[40] ^= 1;
await throwsCode(() => P.openEnvelope(badHead, pcSignPub, dev.kex.privateKey, deviceId), 'AUTH_FAIL', '헤더(배정 버전) 변조');
await throwsCode(() => P.openEnvelope(envBytes, pcSignPub, dev.kex.privateKey, P.randomBytes(16)), 'WRONG_DEVICE', '다른 기기용 목록');
const other = await P.generateDeviceKeys();
const notPc = await P.importEcdsaPub(signRaw);
await throwsCode(() => P.openEnvelope(envBytes, notPc, dev.kex.privateKey, deviceId), 'AUTH_FAIL', 'PC가 아닌 서명자');
await throwsCode(() => P.openEnvelope(envBytes, pcSignPub, other.kex.privateKey, deviceId), 'AUTH_FAIL', '다른 기기 키로 복호화 시도');
const brokenFrame = frames[2].slice(0, 60) + (frames[2][60] === 'A' ? 'B' : 'A') + frames[2].slice(61);
let rejected = false; try { new P.Collector().add(brokenFrame); } catch (e) { rejected = ['QR_CORRUPT', 'QR_BAD_ENCODING'].includes(e.code); }
ok(rejected, '손상 조각 CRC로 즉시 폐기');

// 4) 결과 파일 → PC 가져오기 -------------------------------------------------
const st = opened.payload.stops[0], line = st.items[0];
let seq = 0;
const ev = (kind, body) => ({ event_id: crypto.randomUUID(), seq: ++seq, request_id: st.request_id, assign_ver: opened.payload.assign_ver, kind, body });
const result = {
  schema: 'dg.result/1', work_date: opened.payload.work_date,
  events: [
    ev('CONTACT', { result: 'REACHED' }),
    ev('ITEM_RESULT', { line_id: line.line_id, line_kind: 'RECEIVED', qty_confirmed: line.qty, qty_collected: line.qty - 1, reason: 'NOT_PUT_OUT' }),
  ],
  photos: [],
};
const rhead = { btype: P.TYPE_RESULT, bundleId: P.randomBytes(16), deviceId, assignVer: opened.payload.assign_ver, dataVer: 1, signerKeyId: await P.keyId(signRaw) };
const sealed = await P.seal(result, rhead, pcKexPub, dev.sign.privateKey);
const file = join(WORK, 'result.dgr');
writeFileSync(file, sealed.bytes);
const imp1 = py('import-result', file);
ok(imp1.includes('"applied": 2') && imp1.includes(sealed.receipt), `결과 2건 반영, 보관 종료 확인 코드 일치 (${sealed.receipt})`);
ok(py('import-result', file).includes('이미 가져온 파일'), '같은 파일 재가져오기 → 변경 없음');
// 같은 이벤트를 새 파일로 다시 보낸 경우
const resend = await P.seal(result, { ...rhead, bundleId: P.randomBytes(16) }, pcKexPub, dev.sign.privateKey);
writeFileSync(file + '2', resend.bytes);
ok(py('import-result', file + '2').includes('"skipped_duplicate": 2'), '같은 이벤트 재전송 → 중복 건너뜀');
const badRes = sealed.bytes.slice(); badRes[120] ^= 1;
writeFileSync(file + '3', badRes);
ok(pyExpectFail('import-result', file + '3').includes('검증 실패'), '변조된 결과 파일 거부');

// 5) QR 이미지 인식 (Python qrcode → jsQR) ----------------------------------
const pngPath = join(WORK, 'f0.png');
pyEval(`import pc_tool,base64,json;u,_=pc_tool.qr_png_data_uri(json.load(open(${JSON.stringify(join(WORK, 'out', `bundle_${short}.frames.json`))}))[0],'M');open(${JSON.stringify(pngPath)},'wb').write(base64.b64decode(u.split(',')[1]))`);
const png = PNG.sync.read(readFileSync(pngPath));
const qr = jsQR(new Uint8ClampedArray(png.data), png.width, png.height);
ok(qr && qr.data === frames[0], `QR 이미지 인식 결과가 원래 조각과 일치 (${frames[0].length}자)`);

// 개정 수수료표 전달: PC가 승인한 확정 단가가 휴대폰 목록에 실려 온다
const marble = BUILTIN_FEES.rows.find((r) => r.status === 'REVIEW');
const ex = P.expandDaylist({ schema: 'dg.daylist/2', work_date: '2026-11-03', crew: 'x', assign_ver: 1, fee: BUILTIN_FEES.version_id + '-r1', fb: BUILTIN_FEES.version_id,
  fo: { [marble.id]: 15000 }, fe: '2026-11-02', s: [{ q: 1, i: 'AAAAAAAAAAAAAAAAAAAAAA', r: 'R', v: 1, n: 'n', p: '010', a: 'a', it: [['AAAAAAAAAAAAAAAAAAAAAA', marble.id, 1, { ps: 'C', pp: 15000 }]] }] }, BUILTIN_FEES);
ok(ex.fee_table.base_version === BUILTIN_FEES.version_id && ex.fee_table.overrides[marble.id] === 15000 && ex.stops[0].items[0].price_status === 'CONFIRMED' && ex.stops[0].items[0].amount === 15000,
  '개정 버전 목록: 내장 원문 표 + 확정 단가로 확장');


// 6) v0.10: 지점의 행정동 "d" (선택, 하위 호환) -----------------------------
const dongJson = pyEval(`import json, dgproto as P, sample_data as SD
from dgpc import service as S
p = SD.make_daylist(3, '2026-11-03', 1)
p['stops'][0]['dong'] = '충장동'; p['stops'][1]['dong'] = '계림동'; p['stops'][2].pop('dong', None)
c = P.compact_daylist(p, S.FEE_ROWS)
o = SD.make_daylist(2, '2026-11-03', 1)
for x in o['stops']: x.pop('dong', None)  # v0.9 이전 PC가 만든 목록(동 없음)
old = P.compact_daylist(o, S.FEE_ROWS)
print(json.dumps({'c': c, 'old': old, 'py': [s.get('dong') for s in P.expand_daylist(c, S.FEE_ROWS)['stops']], 'pyold': [s.get('dong') for s in P.expand_daylist(old, S.FEE_ROWS)['stops']]}, ensure_ascii=False))`);
const dj = JSON.parse(dongJson);
ok(dj.c.schema === 'dg.daylist/2' && dj.c.s[0].d === '충장동' && !('d' in dj.c.s[2]), 'PC 압축 형식 v2에 동 "d" 선택 항목 (없으면 생략, 형식 번호 그대로)');
const exD = P.expandDaylist(dj.c, BUILTIN_FEES), exOld = P.expandDaylist(dj.old, BUILTIN_FEES);
ok(exD.stops.map((x) => x.dong).join() === '충장동,계림동,' && exOld.stops.every((x) => x.dong === ''), '휴대폰: 동 있는 목록·이전 목록(동 없음) 모두 풀림');
ok(dj.py.join() === '충장동,계림동,' && dj.pyold.every((x) => x === ''), 'PC(Python) expand_daylist도 같은 규칙');

// 7) 하루 중 목록 갱신 비교 --------------------------------------------------
const base = exD.stops.map((x) => ({ ...x }));
const old7 = { work_date: '2026-11-03', stops: base };
const new7 = { work_date: '2026-11-03', stops: [{ ...base[1], seq: 1 }, { ...base[0], seq: 2 }, { ...base[2], seq: 3, memo: '바뀐 메모' }, { ...base[2], request_id: 'new-1', seq: 4 }] };
const df = F.diffDaylist(old7, new7);
ok(df.added === 1 && df.changes['new-1'][0] === 'NEW' && df.changes[base[2].request_id].includes('INFO'), '새 목록 비교: 새로 추가·정보 변경');
ok(df.changed === 2 && Object.values(df.changes).filter((f) => f.includes('ORDER')).length === 1, '두 곳 자리 바꿈 → 순서 변경은 1곳만(나머지는 그대로)');
const df2 = F.diffDaylist(old7, { work_date: '2026-11-03', stops: [{ ...base[0], seq: 1 }, { ...base[1], seq: 2 }] });
ok(df2.removed === 1 && df2.summary === '빠짐 1곳' && F.diffDaylist(old7, { ...new7, work_date: '2026-11-04' }).summary === '', "요약 '빠짐 1곳', 다른 날짜 목록은 비교하지 않음");
const purgedOld = { work_date: '2026-11-03', stops: [{ ...base[0] }] }; F.purgeStopPII(purgedOld.stops[0]);
ok(!F.diffDaylist(purgedOld, { work_date: '2026-11-03', stops: [{ ...base[0] }] }).changed, '개인정보를 지운 곳을 다시 받아도 "정보 변경"으로 보지 않음');

// 8) 문자 문구: 다른 주민 정보 금지, 자리표시는 그 주민 것만 ----------------
const st8 = [{ name: '가상하나', phone: '01000001234', address: '가상시 동구 예시로 12, 101동' }];
ok(F.SMS_DEFAULTS.every((t) => F.checkSmsTemplate(t.text, st8) === ''), '기본 문구 3종 통과');
ok(F.checkSmsTemplate('가상하나님 곧 갑니다', st8).includes('이름') && F.checkSmsTemplate('010-0000-1234로 연락', st8).includes('연락처')
  && F.checkSmsTemplate('가상시 동구 예시로 12 앞', st8).includes('주소') && F.checkSmsTemplate('{이름}님', st8).includes('자리표시'), '문구에 주민 이름·연락처·주소·모르는 자리표시 거부');
const filled = F.fillSms('접수 {접수번호} {시각} {재방문}', { receipt_no: 'DG-1', eta: '10:30' }, { date: '2026-11-05', start: '14:00' });
ok(filled === '접수 DG-1 10:30 2026-11-05 14:00' && F.smsHref('010-0000-1234', '가 나') === 'sms:01000001234?body=%EA%B0%80%20%EB%82%98', '자리표시 채우기·sms: 주소(번호·본문만)');

// 9) 개인정보 지우기·미전송 나이·보관 기간 -----------------------------------
const pst = { name: 'n', phone: 'p', address: 'a', spot_note: 's', memo: 'm', lat: 1, lon: 2, receipt_no: 'R', items: [], versions: [{ req_ver: 1, at: 't', reason: 'r', snapshot: { name: 'n' } }] };
F.purgeStopPII(pst, '2026-11-03T00:00:00Z');
ok(!pst.name && !pst.phone && !pst.address && pst.lat === null && pst.receipt_no === 'R' && pst.versions[0].snapshot === null && pst.pii_purged, '지점 개인정보 지우기 (접수번호·기록 유지, 이전 버전 사본도)');
const ev9 = { body: { text: '메모', lat: 1, lon: 2, acc: 5, qty: 3 } }; F.purgeEventPII(ev9);
ok(ev9.body.qty === 3 && !('text' in ev9.body) && !('lat' in ev9.body), '기록에서 메모 글·좌표만 지움');
const fi = { kind: 'FIELD_INTAKE', body: { temp_no: '현장-1104-1', name: '가상', phone: '01000000000', address: '가상로 1', spot_note: '옆', memo: 'm', dong: '충장동', planned_date: '2026-11-04', items: [{ qty: 1 }] } };
F.purgeEventPII(fi);
const fc = { kind: 'CORRECTION', body: { fields: { phone: '01011112222', planned_date: '2026-11-05' }, reason: '번호 바뀜' } };
F.purgeEventPII(fc);
ok(!fi.body.name && !fi.body.phone && !fi.body.address && !fi.body.dong && fi.body.items.length === 1 && fi.body.temp_no && JSON.stringify(fc.body.fields) === '["phone","planned_date"]' && !JSON.stringify(fc).includes('01011112222'),
  '현장 접수·정정 요청 기록: 이름·연락처·주소 값은 지우고 품목·칸 이름만 남김');
const now = Date.parse('2026-11-03T12:00:00Z');
const u9 = F.unsentInfo([{ seq: 1, ts: '2026-11-03T08:00:00Z' }, { seq: 2, ts: '2026-11-03T11:00:00Z' }, { seq: 3, ts: '2026-11-03T11:30:00Z' }], 1, now);
ok(u9.count === 2 && Math.abs(u9.ageH - 1) < 1e-9, '미전송 기록 수·가장 오래된 나이(시간)');
const reqs = [{ request_id: 'a', status: 'ACTIVE', updated_at: '2026-07-01T00:00:00Z' }, { request_id: 'b', status: 'ACTIVE', updated_at: '2026-10-30T00:00:00Z' }];
const rc = F.retentionCandidates(reqs, [{ request_id: 'a', ts: '2026-07-02T00:00:00Z' }], 90, () => true, now);
ok(rc.length === 1 && rc[0].r.request_id === 'a', '단독 보관 기간 90일 지난 처리 끝난 접수만 후보');

// 10) 현장 기록: 도착·스티커·금액 고지·문자는 진행으로 세지 않음 ------------
const s10 = { request_id: 'r', items: [{ line_id: 'l1', name: '의자', spec: '', unit: 'EA', unit_label: '개', qty: 1, unit_price: 2000, amount: 2000, price_status: 'CONFIRMED' }] };
const e10 = [{ request_id: 'r', kind: 'SMS_INTENT', body: { template: 'SOON' } }];
ok(deriveStop(s10, e10).progress === 'TODO', '문자 안내만으로는 "처리 중"이 되지 않음');
e10.push({ request_id: 'r', kind: 'ITEM_RESULT', body: { line_id: 'l1', qty_confirmed: 3, qty_collected: 0, reason: '' } },
  { request_id: 'r', kind: 'STICKER_CHECK', body: { line_id: 'l1', result: 'MISMATCH', number: '12', decision: 'COLLECT' } },
  { request_id: 'r', kind: 'ARRIVE', ts: '2026-11-03T01:00:00Z', body: { lat: 1, lon: 2, acc: 9 } });
const d10 = deriveStop(s10, e10);
ok(feeExtra(d10) === 4000 && needsFeeNotice(d10) && d10.stickers.l1.decision === 'COLLECT' && d10.arrive.acc === 9, '수량 증가 → 추가 금액 4,000원 고지 필요, 스티커·도착 기록 반영');
e10.push({ request_id: 'r', kind: 'FEE_NOTICE', ts: 't', body: { extra: 4000, response: 'AGREED' } });
ok(!needsFeeNotice(deriveStop(s10, e10)), '같은 금액 이상 안내했으면 다시 묻지 않음');

// 11) 지도 미리 받기: 줌 14 이하 조각만, 동구 범위 ---------------------------
const tl = tilesForBbox(DONGGU_BBOX);
ok(tl.length > 0 && tl.length < 200 && Math.max(...tl.map((t) => t.z)) === 14 && tilesForBbox(DONGGU_BBOX, 15, 16).length === 0, `동구 범위 조각 ${tl.length}개 (줌 10~14, 15 이상 없음)`);
const bb = bboxAround([{ lat: 35.15, lon: 126.92 }]);
ok(bb[0] < 126.92 && bb[2] > 126.92 && isAllowedMapUrl('https://tiles.openfreemap.org/planet/x/14/1/1.pbf') && !isAllowedMapUrl('https://tiles.openfreemap.org/planet/x/15/1/1.pbf'), '범위 여유·줌 15 이상 차단');

// 12) 길안내 주소: 좌표와 "N번 수거지"만 ------------------------------------
const P12 = { lat: 35.146213, lon: 126.923301, label: NAV.navLabel(3) };
const PII12 = ['가상하나', '01000001234', '예시로', 'DG-2026'];
const u12 = Object.fromEntries(Object.keys(NAV.NAV_APPS).map((a) => [a, NAV.navUrls(a, P12, { appname: 'https://app.example' })]));
ok(u12.kakao.url === 'kakaomap://route?ep=35.146213,126.923301&by=CAR' && u12.kakao.fallback === `https://map.kakao.com/link/to/${encodeURIComponent('3번 수거지')},35.146213,126.923301`, '카카오맵: kakaomap://route?ep=위도,경도&by=CAR, 없으면 map.kakao.com/link/to');
ok(u12.tmap.url === `tmap://route?goalx=126.923301&goaly=35.146213&goalname=${encodeURIComponent('3번 수거지')}` && u12.tmap.fallback.startsWith('geo:35.146213,126.923301'), 'T맵: goalx=경도·goaly=위도, 웹 길안내가 없어 geo:로');
ok(u12.naver.url.startsWith('nmap://route/car?dlat=35.146213&dlng=126.923301&dname=') && u12.naver.url.includes('appname=https%3A%2F%2Fapp.example') && u12.naver.fallback.startsWith('https://m.map.naver.com/route.nhn?'), '네이버지도: nmap://route/car?dlat·dlng·dname·appname, 없으면 모바일 웹');
ok(u12.geo.url === `geo:35.146213,126.923301?q=35.146213,126.923301(${encodeURIComponent('3번 수거지')})` && u12.geo.fallback === null
  && NAV.navUrls('geo', P12, { ios: true }).url === 'maps://?daddr=35.146213,126.923301&dirflg=d' && NAV.navUrls('geo', P12, { ios: true }).app === 'apple-maps', '기본 지도 앱: Android geo:, iOS Apple 지도');
const all12 = Object.values(u12).flatMap((x) => [x.url, x.fallback]).filter(Boolean).map(decodeURIComponent);
ok(all12.every((u) => !PII12.some((x) => u.includes(x)) && (u.match(/[가-힣]+/g) || []).every((w) => ['번', '수거지'].includes(w))), '모든 길안내 주소에 좌표·"3번 수거지" 외 글자(이름·연락처·주소·접수번호) 없음');
ok(NAV.safeLabel('가상하나 집') === '수거지' && NAV.navLabel('UNLOAD') === '반입지' && NAV.navUrls('kakao', { lat: 35.1, lon: 126.9, label: '가상하나' }).fallback.includes(encodeURIComponent('수거지')), '정해진 표시 이름 외에는 "수거지"로 바꿈 (이름이 들어갈 수 없음)');
let threw = false; try { NAV.navUrls('kakao', { lat: null, lon: null, label: '1번 수거지' }); } catch { threw = true; }
ok(threw && !NAV.hasCoord({ lat: null, lon: 1 }) && NAV.hasCoord({ lat: 35.1, lon: 126.9 }), '좌표 없는 곳은 길안내 주소를 만들지 않음');

// 13) UTM-K(EPSG:5179) → WGS84: 원점·Python(pc/dgpc/geo.py)과 비교 -----------
const [ox, oy] = J.wgs84ToUtmk(38, 127.5);
ok(Math.abs(ox - 1e6) < 1e-6 && Math.abs(oy - 2e6) < 1e-6, 'UTM-K 원점 (38°N, 127.5°E) = (1,000,000, 2,000,000)');
const pts13 = [[35.1462, 126.9233], [37.5665, 126.978], [33.25, 126.56], [38.6, 128.4]];
const pyPts = JSON.parse(pyEval(`import json; from dgpc import geo; print(json.dumps([geo.wgs84_to_utmk(a, b) for a, b in ${JSON.stringify(pts13)}]))`));
const pyBack = JSON.parse(pyEval(`import json; from dgpc import geo; print(json.dumps([geo.utmk_to_wgs84(x, y) for x, y in ${JSON.stringify(pyPts)}]))`));
ok(pts13.every(([a, b], i) => { const [x, y] = J.wgs84ToUtmk(a, b); return Math.abs(x - pyPts[i][0]) < 1e-3 && Math.abs(y - pyPts[i][1]) < 1e-3; }), 'WGS84→UTM-K: 휴대폰(JS)과 PC(Python) 1mm 이내 일치 (광주 동구청·서울시청·제주·강원)');
ok(pyPts.every(([x, y], i) => { const [a, b] = J.utmkToWgs84(x, y); return Math.abs(a - pyBack[i][0]) < 1e-9 && Math.abs(b - pyBack[i][1]) < 1e-9 && Math.abs(a - pts13[i][0]) < 1e-8 && Math.abs(b - pts13[i][1]) < 1e-8; }), 'UTM-K→WGS84: Python과 일치, 왕복 오차 1mm 미만');
const [sx, sy] = J.wgs84ToUtmk(37.5665, 126.978);
ok(Math.abs(sx - 953901) < 50 && Math.abs(sy - 1952032) < 50, `서울시청 부근 UTM-K (${sx.toFixed(0)}, ${sy.toFixed(0)}) 알려진 값 범위`);

// 14) 도로명주소 API: 요청에는 검색어·승인키만, 좌표 변환·동 제안 -----------
const jusoCalls = [];
const fakeFetch = async (url, o) => {
  jusoCalls.push({ url, o });
  const u = new URL(url);
  const body = u.pathname.endsWith('addrLinkApi.do')
    ? { results: { common: { errorCode: '0', errorMessage: '정상', totalCount: '1', currentPage: '1', countPerPage: '10' }, juso: [{ roadAddr: '광주광역시 동구 서남로 1 (계림동)', roadAddrPart1: '광주광역시 동구 서남로 1', jibunAddr: '광주광역시 동구 계림동 1-1', bdNm: '가상빌딩', zipNo: '61400', emdNm: '계림동', hemdNm: '', admCd: '2911010800', rnMgtSn: '291103166047', udrtYn: '0', buldMnnm: '1', buldSlno: '0' }] } }
    : { results: { common: { errorCode: '0', errorMessage: '정상', totalCount: '1' }, juso: [{ entX: String(pyPts[0][0]), entY: String(pyPts[0][1]) }] } };
  return { ok: true, status: 200, json: async () => body };
};
const sr = await J.searchAddress({ key: 'TESTKEY', keyword: '서남로 1', fetchFn: fakeFetch });
const q14 = new URL(jusoCalls[0].url);
ok(q14.origin === 'https://business.juso.go.kr' && q14.pathname === '/addrlink/addrLinkApi.do' && [...q14.searchParams.keys()].sort().join() === 'confmKey,countPerPage,currentPage,keyword,resultType'
  && q14.searchParams.get('keyword') === '서남로 1' && q14.searchParams.get('resultType') === 'json' && jusoCalls[0].o.credentials === 'omit' && jusoCalls[0].o.referrerPolicy === 'no-referrer', '검색 요청: business.juso.go.kr/addrlink/addrLinkApi.do, 검색어·승인키·쪽수·json만, 쿠키·Referer 없음');
ok(sr.total === 1 && sr.items[0].zip === '61400' && sr.items[0].bdNm === '가상빌딩', '검색 결과: 도로명·지번·건물명·우편번호');
const co = await J.coordOf({ key: 'COORDKEY', item: sr.items[0], fetchFn: fakeFetch });
const q14b = new URL(jusoCalls[1].url);
ok(q14b.pathname === '/addrlink/addrCoordApi.do' && [...q14b.searchParams.keys()].sort().join() === 'admCd,buldMnnm,buldSlno,confmKey,resultType,rnMgtSn,udrtYn' && q14b.searchParams.get('confmKey') === 'COORDKEY', '좌표 요청: addrCoordApi.do, 건물 식별 번호와 좌표 승인키만');
ok(Math.abs(co.lat - 35.1462) < 1e-6 && Math.abs(co.lon - 126.9233) < 1e-6, `좌표(UTM-K 출입구) → 위도·경도 ${co.lat}, ${co.lon}`);
const dg = J.suggestDong(sr.items[0], []);
ok(dg.dong === '' && dg.candidates.join() === '계림1동,계림2동' && J.suggestDong({ hemdNm: '계림2동', emdNm: '계림동' }).dong === '계림2동' && J.suggestDong({ emdNm: '가상1동' }, ['가상1동']).dong === '가상1동', '동 제안: 행정동이 있으면 그대로, 법정동 "계림동" → 계림1동·계림2동 후보, 접수에 쓴 동과도 맞춤');
await throwsCode(() => J.searchAddress({ key: 'BAD', keyword: '서남로', fetchFn: async () => ({ ok: true, json: async () => ({ results: { common: { errorCode: 'E0001', errorMessage: '승인되지 않은 KEY 입니다.' } } }) }) }), 'E0001', '틀린 승인키');
await throwsCode(() => J.searchAddress({ key: 'K', keyword: '서남로', fetchFn: async () => { throw new TypeError('Failed to fetch'); } }), 'NETWORK', 'CORS 차단·인터넷 끊김');
ok(J.cleanKeyword(" 서남로 1 <script>% = [x]") === '서남로 1 script x', '검색어의 특수문자(서버가 거부하는 것) 제거');

// 15) 반입지 좌표 (목록 묶음 "u"의 선택 5·6번째 값) -----------------------------
const u15 = { ...dj.c, s: dj.c.s.map((x, i) => (i === 0 ? { ...x, u: ['11:00', '가상 반입지', 20, 5.5, 35.13, 126.95] } : i === 1 ? { ...x, u: ['12:00', '반입지', 20, 3] } : x)) };
const e15 = P.expandDaylist(u15, BUILTIN_FEES);
ok(e15.stops[0].unload_after.lat === 35.13 && e15.stops[0].unload_after.lon === 126.95 && e15.stops[1].unload_after.lat === null, '반입지 좌표가 있으면 받고(길안내), 없는 이전 형식도 그대로');

// 16) PC가 만든 목록 묶음에 반입지 좌표가 실리고(5·6번째 값), 휴대폰이 읽는다 -----------------------
const pc16 = JSON.parse(pyEval(`import json, dgproto as P
from dgpc import service as S
p = {"schema": "dg.daylist/1", "work_date": "2026-11-05", "crew": "가상", "assign_ver": 1, "fee_table": {"version_id": S.FEE_TABLE["version_id"]},
     "stops": [{"seq": 1, "request_id": "00000000-0000-0000-0000-000000000001", "receipt_no": "DG-1", "req_ver": 1, "name": "가", "phone": "01000000000", "address": "가상로 1",
                "items": [], "unload_after": {"eta": "11:00", "label": "가상 반입지", "minutes": 20, "volume_m3": 5.5, "lat": 35.13, "lon": 126.95}},
               {"seq": 2, "request_id": "00000000-0000-0000-0000-000000000002", "receipt_no": "DG-2", "req_ver": 1, "name": "나", "phone": "01000000001", "address": "가상로 2",
                "items": [], "unload_after": {"eta": "12:00", "label": "반입지", "minutes": 20, "volume_m3": 3}}]}
print(json.dumps(P.compact_daylist(p, S.FEE_ROWS), ensure_ascii=False))`));
const e16 = P.expandDaylist(pc16, BUILTIN_FEES);
ok(pc16.s[0].u.length === 6 && pc16.s[1].u.length === 4 && e16.stops[0].unload_after.lat === 35.13 && e16.stops[0].unload_after.lon === 126.95 && e16.stops[1].unload_after.lat === null,
  'PC 목록 묶음: 반입지 좌표가 있으면 "u" 5·6번째 값으로 싣고 휴대폰이 읽음(없으면 이전 형식)');

// 17) 감면율 "rd"·부과번호 "lv" (v0.11 선택 키) — PC → 휴대폰 ---------------------------------
ok(/^\d{4}-/.test(s0.levy_no) && s0.reduction === null, `실제 PC 목록 묶음: 부과번호 "lv" 받음(${s0.levy_no}), 감면 없는 건은 reduction 없음`);
const pc17 = JSON.parse(pyEval(`import json, dgproto as P
from dgpc import service as S
row = next(r for r in S.FEE_TABLE["rows"] if r["status"] == "CONFIRMED" and r["unit"] == "EA")
it = {"line_id": "00000000-0000-0000-0000-0000000000a1", "fee_rate_id": row["id"], "name": row["name"], "spec": row["spec"], "unit": row["unit"], "unit_label": row["unit_label"],
      "qty": 3, "unit_price": row["price"], "amount": row["price"] * 3, "price_status": "CONFIRMED"}
base = {"req_ver": 1, "name": "가", "phone": "01000000000", "address": "가상로 1", "items": [it]}
p = {"schema": "dg.daylist/1", "work_date": "2026-11-05", "crew": "가상", "assign_ver": 1, "fee_table": {"version_id": S.FEE_TABLE["version_id"]},
     "stops": [{**base, "seq": 1, "request_id": "00000000-0000-0000-0000-000000000011", "receipt_no": "DG-1", "levy_no": "2026-01-00007", "reduction": {"rate": 50, "label": "차상위계층"}},
               {**base, "seq": 2, "request_id": "00000000-0000-0000-0000-000000000012", "receipt_no": "DG-2", "levy_no": "2026-01-00008"},
               {**base, "seq": 3, "request_id": "00000000-0000-0000-0000-000000000013", "receipt_no": "DG-3"}]}
print(json.dumps({"c": P.compact_daylist(p, S.FEE_ROWS), "price": row["price"]}, ensure_ascii=False))`));
const c17 = pc17.c, e17 = P.expandDaylist(c17, BUILTIN_FEES);
ok(c17.s[0].rd === 50 && c17.s[0].lv === '2026-01-00007' && !('rd' in c17.s[1]) && c17.s[1].lv === '2026-01-00008' && !('lv' in c17.s[2]) && !JSON.stringify(c17).includes('차상위'),
  'PC 압축: 감면 건만 "rd"(정수 %), 부과번호 있으면 "lv", 감면 사유 이름은 싣지 않음');
ok(e17.stops[0].reduction.rate === 50 && e17.stops[0].levy_no === '2026-01-00007' && e17.stops[1].reduction === null && e17.stops[2].levy_no === '', '휴대폰이 "rd"·"lv"를 읽음 (없으면 감면 없음·부과번호 빈 값)');
const old17 = { ...c17, s: c17.s.map(({ rd, lv, ...x }) => x) };
ok(P.expandDaylist(old17, BUILTIN_FEES).stops.every((x) => x.reduction === null && x.levy_no === ''), '이전 형식(rd·lv 없음)도 그대로 읽음');
// 휴대폰 쪽 v2 자료(rd·lv 포함)를 PC(Python) expand_daylist 가 같은 뜻으로 읽음 — 반대 방향
const jsSide = { ...c17, s: c17.s.map((x, i) => (i === 2 ? { ...x, rd: 30, lv: '2026-01-00009' } : x)) };
const py17 = JSON.parse(pyEval(`import json, dgproto as P
from dgpc import service as S
c = json.loads(${JSON.stringify(JSON.stringify(jsSide))})
o = json.loads(${JSON.stringify(JSON.stringify(old17))})
e, eo = P.expand_daylist(c, S.FEE_ROWS), P.expand_daylist(o, S.FEE_ROWS)
print(json.dumps({"red": [s.get("reduction") for s in e["stops"]], "lv": [s["levy_no"] for s in e["stops"]], "oldred": [s.get("reduction") for s in eo["stops"]], "oldlv": [s["levy_no"] for s in eo["stops"]],
                  "rt": P.compact_daylist(e | {"fee_table": {"version_id": e["fee_table"]["version_id"]}}, S.FEE_ROWS)["s"][2]}))`));
ok(JSON.stringify(py17.red) === JSON.stringify([{ rate: 50 }, null, { rate: 30 }]) && py17.lv.join() === '2026-01-00007,2026-01-00008,2026-01-00009'
  && py17.oldred.every((x) => x === null) && py17.oldlv.every((x) => x === ''), 'PC(Python) expand_daylist 도 "rd"·"lv"를 같은 뜻으로 읽음 (이전 형식 포함)');
ok(py17.rt.rd === 30 && py17.rt.lv === '2026-01-00009', 'PC 풀기 → 다시 압축해도 "rd"·"lv" 유지 (왕복)');
// 금액: 계산 금액 → 감면(원 단위 버림) → 부과 금액, PC finance.fee_summary 와 같은 결과
const pairs = [[1001, 50], [3333, 30], [12345, 100], [999, 0], [7, 33], [45670, 50]];
const pyRed = JSON.parse(pyEval(`import json
from dgpc import finance as FN
print(json.dumps([[FN.fee_summary({"reduction_code": "X", "reduction_rate": r}, a)["reduction"], FN.fee_summary({"reduction_code": "X", "reduction_rate": r}, a)["levied"]] for a, r in ${JSON.stringify(pairs)}]))`));
ok(pairs.every(([a, r], i) => { const x = applyReduction(a, r); return x.reduction === pyRed[i][0] && x.levied === pyRed[i][1]; }), `감면 계산이 PC와 같음 (1,001원 50% → 감면 ${applyReduction(1001, 50).reduction}원, 부과 ${applyReduction(1001, 50).levied}원)`);
const st17 = e17.stops[0];
const d17 = deriveStop(st17, []);
const price = pc17.price;
ok(d17.fee.final === price * 3 && d17.fee.reductionRate === 50 && d17.fee.reduction === Math.floor((price * 3) / 2) && d17.fee.levied === price * 3 - Math.floor((price * 3) / 2),
  `휴대폰 금액: 계산 ${price * 3} → 감면 50% → 부과 ${d17.fee.levied}`);
const fieldRow = BUILTIN_FEES.rows.find((r) => r.status === 'CONFIRMED' && r.unit === 'EA' && r.price % 200 === 100) || BUILTIN_FEES.rows.find((r) => r.status === 'CONFIRMED');
const d17b = deriveStop(st17, [{ request_id: st17.request_id, kind: 'FIELD_ITEM_ADD', body: { line_id: 'f1', name: fieldRow.name, spec: fieldRow.spec, unit: fieldRow.unit, unit_label: fieldRow.unit_label, fee_rate_id: fieldRow.id, qty: 1, unit_price: fieldRow.price, price_status: 'CONFIRMED' } }]);
const tot = price * 3 + fieldRow.price;
ok(d17b.fee.final === tot && d17b.fee.reduction === Math.floor((tot * 50) / 100) && d17b.fee.levied === tot - Math.floor((tot * 50) / 100) && d17b.fee.fieldAdded === fieldRow.price,
  `현장 추가 품목에도 감면 적용: 계산 ${tot} → 부과 ${d17b.fee.levied}`);
ok(deriveStop(e17.stops[1], []).fee.levied === price * 3 && deriveStop(e17.stops[1], []).fee.reductionRate === 0, '감면 없는 건: 부과 금액 = 계산 금액');
const pend = deriveStop({ ...st17, items: [{ ...st17.items[0], price_status: 'PENDING', unit_price: null, amount: null }] }, []);
ok(pend.fee.final === null && pend.fee.levied === null && pend.fee.reduction === null, '금액 확인 대기가 있으면 감면·부과 금액도 미확정');
// 17b) 감면액 끝수 단위 "ru" (v0.13 선택 키) — PC → 휴대폰, 휴대폰 → PC, 이전 형식 -----------------------
const pc17b = JSON.parse(pyEval(`import json, dgproto as P
from dgpc import service as S
row = next(r for r in S.FEE_TABLE["rows"] if r["status"] == "CONFIRMED" and r["unit"] == "EA")
it = {"line_id": "00000000-0000-0000-0000-0000000000b1", "fee_rate_id": None, "name": "가상", "spec": "", "unit": "EA", "unit_label": "개",
      "qty": 1, "unit_price": 3015, "amount": 3015, "price_status": "CONFIRMED"}
base = {"req_ver": 1, "name": "가", "phone": "01000000000", "address": "가상로 1", "items": [it]}
p = {"schema": "dg.daylist/1", "work_date": "2026-11-05", "crew": "가상", "assign_ver": 1, "fee_table": {"version_id": S.FEE_TABLE["version_id"]},
     "stops": [{**base, "seq": 1, "request_id": "00000000-0000-0000-0000-000000000021", "receipt_no": "DG-1", "reduction": {"rate": 50, "label": "가상", "round_unit": 10}},
               {**base, "seq": 2, "request_id": "00000000-0000-0000-0000-000000000022", "receipt_no": "DG-2", "reduction": {"rate": 50, "label": "가상"}},
               {**base, "seq": 3, "request_id": "00000000-0000-0000-0000-000000000023", "receipt_no": "DG-3"}]}
print(json.dumps({"c": P.compact_daylist(p, S.FEE_ROWS)}, ensure_ascii=False))`));
const c17b = pc17b.c, e17b = P.expandDaylist(c17b, BUILTIN_FEES);
ok(c17b.s[0].ru === 10 && !('ru' in c17b.s[1]) && !('ru' in c17b.s[2]), 'PC 압축: 끝수 10원 감면 건만 "ru": 10 (1원·감면 없음은 싣지 않음 — 이전 형식 그대로)');
const f17b = e17b.stops.map((x) => deriveStop(x, []).fee);
ok(e17b.stops[0].reduction.round_unit === 10 && !('round_unit' in e17b.stops[1].reduction) && f17b[0].reduction === 1500 && f17b[0].levied === 1515
  && f17b[1].reduction === 1507 && f17b[1].levied === 1508 && f17b[2].levied === 3015,
  '휴대폰이 "ru"를 읽어 10원 미만 버림(3,015원 50% → 감면 1,500·부과 1,515), 없으면 1원(감면 1,507)');
const old17b = { ...c17b, s: c17b.s.map(({ ru, ...x }) => x) };
ok(P.expandDaylist(old17b, BUILTIN_FEES).stops.every((x) => !x.reduction || !('round_unit' in x.reduction)) && deriveStop(P.expandDaylist(old17b, BUILTIN_FEES).stops[0], []).fee.reduction === 1507,
  '이전 형식("ru" 없음)은 1원 단위 그대로');
const js17b = { ...c17b, s: c17b.s.map((x, i) => (i === 1 ? { ...x, ru: 10 } : i === 2 ? { ...x, ru: 10 } : x)) };
const py17b = JSON.parse(pyEval(`import json, dgproto as P
from dgpc import service as S
c = json.loads(${JSON.stringify(JSON.stringify(js17b))})
o = json.loads(${JSON.stringify(JSON.stringify(old17b))})
e, eo = P.expand_daylist(c, S.FEE_ROWS), P.expand_daylist(o, S.FEE_ROWS)
print(json.dumps({"red": [s.get("reduction") for s in e["stops"]], "old": [s.get("reduction") for s in eo["stops"]],
                  "rt": [x.get("ru") for x in P.compact_daylist(e | {"fee_table": {"version_id": e["fee_table"]["version_id"]}}, S.FEE_ROWS)["s"]]}))`));
ok(JSON.stringify(py17b.red) === JSON.stringify([{ rate: 50, round_unit: 10 }, { rate: 50, round_unit: 10 }, null]) && JSON.stringify(py17b.old) === JSON.stringify([{ rate: 50 }, { rate: 50 }, null])
  && JSON.stringify(py17b.rt) === JSON.stringify([10, 10, null]),
  'PC(Python) expand_daylist 도 "ru"를 같은 뜻으로 읽음(감면 없는 지점의 "ru"는 무시), 이전 형식 포함, 다시 압축해도 유지(왕복)');
const pyRu = JSON.parse(pyEval(`import json
from dgpc import finance as FN
print(json.dumps([FN.fee_summary({"reduction_code": "X", "reduction_rate": r, "reduction_round_unit": 10}, a)["levied"] for a, r in ${JSON.stringify(pairs)}]))`));
ok(pairs.every(([a, r], i) => applyReduction(a, r, 10).levied === pyRu[i]), '끝수 10원 감면 계산이 PC와 같음');

// 18) 지문·얼굴 등록: 등록 응답에 PRF가 없어도(Samsung Pass) 다음 인증에서 PRF가 나오면 등록, 끝내 없으면 거부 ---------
{
  const BIO = await import('../web/bio.js');
  const prfVal = new Uint8Array(32).fill(7);
  const mock = (createExt, getExt) => ({
    create: async () => ({ rawId: new Uint8Array([1, 2, 3]).buffer, getClientExtensionResults: () => createExt }),
    get: async () => ({ getClientExtensionResults: () => getExt }),
  });
  const setCred = (c) => Object.defineProperty(globalThis, 'navigator', { value: { credentials: c }, configurable: true, writable: true });
  setCred(mock({}, { prf: { results: { first: prfVal.buffer } } }));  // Samsung Pass: 등록 응답에 PRF 없음, 인증 때 줌
  const r1 = await BIO.bioRegister();
  setCred(mock({ prf: { enabled: true, results: { first: prfVal.buffer } } }, {}));  // Google 비밀번호 관리자·iCloud: 등록 때 바로 줌
  const r2 = await BIO.bioRegister();
  setCred(mock({ prf: { enabled: false } }, {}));  // 지원 안 함
  let code = null;
  try { await BIO.bioRegister(); } catch (e) { code = e.code; }
  ok(r1.prfOut.length === 32 && r1.prfOut[0] === 7 && r2.prfOut[0] === 7 && code === 'NO_PRF',
    '지문·얼굴 등록: 등록 응답에 PRF가 없어도(Samsung Pass) 인증에서 나오면 등록, 끝내 없으면 PIN 안내');
}

// 19) 배출 주소 확인·행정동 추천(휴대폰 안 자료로만) ---------------------------------------------
{
  const J = await import('../web/juso.js');
  const w = (a) => J.checkAddress(a).warnings.join('|');
  ok(w('광주 동구 예시로 12') === '' && w('동구 대인동 34-5') === '' && /건물번호/.test(w('예시아파트 앞')) && /형식/.test(w('동구 12번지 근처'))
     && /관할/.test(w('광주 서구 상무대로 1')) && /서울특별시/.test(w('서울특별시 강남구 테헤란로 1')) && /전화번호/.test(w('충장로 5 010-1234-5678'))
     && J.checkAddress('충장로1가 12').kind === 'JIBUN' && J.jibunDongOf('금남로5가 4') === '금남로5가' && J.roadOf('충장로1가 12') === '',
     '주소 확인: 건물번호 없음·형식·관할(구) 밖·다른 시도·전화번호 섞임');
  const hist = [{ address: '광주 동구 예시로 3', dong: '충장동' }, { address: '예시로 9', dong: '충장동' }, { address: '대인동 1', dong: '충장동' }];
  ok(J.guessDong('동구 계림1동 12').dong === '계림1동' && J.guessDong('광주 동구 학동로 12').dong === '' && J.guessDong('예시로 15', hist).dong === '충장동'
     && J.guessDong('대인동 7', hist).dong === '충장동' && J.guessDong('계림동 123').candidates.join() === '계림1동,계림2동' && J.guessDong('계림동 123').dong === '',
     '동 추천: 주소의 행정동 이름(낱말 단위), 같은 도로·법정동의 이전 접수, 법정동 줄기 후보(계림동 → 계림1·2동)');
  ok(J.isKnownDong('충장동') && !J.isKnownDong('충정동') && J.isKnownDong('가상1동', ['가상1동']) && J.isKnownDong(''), '동 이름 오타 확인(동구 행정동·PC 목록의 동)');
}

// 20) v0.13 목록 묶음의 결과 반영 정보 "rel" (선택 키) — 양방향·이전 형식·서명된 봉투 ------------------------
{
  const PH1 = '0a0a0a0a-0000-4000-8000-000000000001', PH2 = '0a0a0a0a-0000-4000-8000-000000000002';
  const pc20 = JSON.parse(pyEval(`import json, dgproto as P
from dgpc import service as S
row = next(r for r in S.FEE_TABLE["rows"] if r["status"] == "CONFIRMED" and r["unit"] == "EA")
it = {"line_id": "00000000-0000-0000-0000-0000000000b1", "fee_rate_id": row["id"], "name": row["name"], "spec": row["spec"], "unit": row["unit"], "unit_label": row["unit_label"],
      "qty": 1, "unit_price": row["price"], "amount": row["price"], "price_status": "CONFIRMED"}
p = {"schema": "dg.daylist/1", "work_date": "2026-11-05", "crew": "가상", "assign_ver": 1, "fee_table": {"version_id": S.FEE_TABLE["version_id"]},
     "stops": [{"seq": 1, "request_id": "00000000-0000-0000-0000-000000000021", "receipt_no": "DG-1", "req_ver": 1, "name": "가", "phone": "01000000000", "address": "가상로 1", "items": [it]}]}
out = {"with": P.compact_daylist({**p, "rel": {"seq": 12, "ph": ["${PH1}", "${PH2.toUpperCase()}", "${PH1}"]}}, S.FEE_ROWS),
       "without": P.compact_daylist(p, S.FEE_ROWS),
       "bad": [("rel" in P.compact_daylist({**p, "rel": r}, S.FEE_ROWS)) for r in ({"seq": -1}, {"seq": "3"}, {"seq": True}, {"seq": 2, "ph": ["x"]}, {"seq": 2, "ph": "abc"}, None, [])],
       "zero": P.compact_daylist({**p, "rel": {"seq": 0}}, S.FEE_ROWS).get("rel")}
print(json.dumps(out, ensure_ascii=False))`));
  ok(JSON.stringify(pc20.with.rel) === JSON.stringify({ seq: 12, ph: [PH1, PH2] }) && !('rel' in pc20.without) && pc20.bad.every((x) => x === false)
     && JSON.stringify(pc20.zero) === JSON.stringify({ seq: 0, ph: [] }),
     'PC 압축: "rel" {seq, ph[]} 실음(사진 ID 소문자·중복 제거), 없거나 형식이 틀리면 키 없음(이전 형식)');
  const e20 = P.expandDaylist(pc20.with, BUILTIN_FEES), e20old = P.expandDaylist(pc20.without, BUILTIN_FEES);
  ok(e20.rel.seq === 12 && e20.rel.ph.join() === `${PH1},${PH2}` && !('rel' in e20old) && e20old.stops.length === 1,
     '휴대폰이 "rel"을 읽음, 이전 형식(rel 없음)은 rel 없이 그대로');
  ok([{ seq: -1 }, { seq: 1.5 }, { seq: '3' }, { seq: 2, ph: ['x'] }, { seq: 2, ph: 'a' }, [], null].every((r) => !('rel' in P.expandDaylist({ ...pc20.without, rel: r }, BUILTIN_FEES))),
     '휴대폰: 형식이 틀린 rel은 없는 것으로 봄(손으로 코드 입력하던 이전 동작)');
  // 반대 방향: 휴대폰 쪽 v2(rel 포함)를 PC(Python)가 같은 뜻으로 읽고, 다시 압축해도 유지
  const js20 = { ...pc20.without, rel: { seq: 7, ph: [PH2] } };
  const py20 = JSON.parse(pyEval(`import json, dgproto as P
from dgpc import service as S
c = json.loads(${JSON.stringify(JSON.stringify(js20))})
o = json.loads(${JSON.stringify(JSON.stringify(pc20.without))})
e, eo = P.expand_daylist(c, S.FEE_ROWS), P.expand_daylist(o, S.FEE_ROWS)
print(json.dumps({"rel": e.get("rel"), "old": "rel" in eo, "rt": P.compact_daylist(e, S.FEE_ROWS).get("rel")}))`));
  ok(py20.rel.seq === 7 && py20.rel.ph.join() === PH2 && py20.old === false && py20.rt.seq === 7 && py20.rt.ph.join() === PH2,
     'PC(Python) expand_daylist 도 "rel"을 같은 뜻으로 읽음, 왕복 유지, 이전 형식은 rel 없음');
  // 실제 봉투: PC가 rel을 넣어 서명·암호화한 목록을 휴대폰이 열면 rel이 그대로 (서명 안에 있음)
  const frames20 = JSON.parse(pyEval(`import json, dgproto as P
from dgpc import store, service as S
c = store.connect()
d = [x for x in S.list_devices(c) if x["id"].startswith("${devShort}")][0]["id"]
b = [x for x in S.list_batches(c, "2026-11-03") if x["status"] == "ACTIVE"][0]
pl, _ = S.build_daylist(c, b["id"])
pl["rel"] = {"seq": 2, "ph": ["${PH1}"]}
print(json.dumps(S.make_bundle(c, payload=pl, device_id=d)["frames"]))`));
  const col20 = new P.Collector();
  let k20; for (const f of frames20) k20 = col20.add(f).key;
  const env20 = col20.assemble(k20);
  const o20 = await P.openEnvelope(env20, pcSignPub, dev.kex.privateKey, deviceId);
  ok(o20.payload.rel && o20.payload.rel.seq === 2 && P.expandDaylist(o20.payload, BUILTIN_FEES).rel.ph[0] === PH1, 'PC가 서명한 목록 봉투 안의 rel을 휴대폰이 그대로 받음');
  const bad20 = env20.slice(); bad20[bad20.length - 70] ^= 1;
  await throwsCode(() => P.openEnvelope(bad20, pcSignPub, dev.kex.privateKey, deviceId), 'AUTH_FAIL', 'rel이 든 봉투를 바꾸면 서명 검증 실패');
}

// 21) rel 적용: 결과 반영 코드·사진 회수 코드를 넣은 것과 같은 효과, rel이 말한 번호를 넘지 않음 ---------------------
{
  const evs = [1, 2, 3, 4, 5].map((n) => ({ event_id: 'e' + n, seq: n, request_id: 'r', kind: n === 2 ? 'PHOTO_META' : 'CONTACT', body: n === 2 ? { photo_id: 'p-known' } : { result: 'REACHED' } }));
  const st21 = { seq: 5, releasedSeq: 0, events: evs, exports: [{ from: 1, to: 3, code: 'AAAA2222', confirmed: false }, { from: 4, to: 5, code: 'BBBB3333', confirmed: false }], recoveredPhotos: [], keptPhotos: [{ photo_id: 'p-kept' }] };
  const r1 = F.applyRelease(st21, { seq: 3, ph: ['p-known', 'p-unknown'] });
  ok(r1.events === 3 && r1.photos === 1 && st21.releasedSeq === 3 && st21.exports[0].confirmed && !st21.exports[1].confirmed && F.unsentInfo(st21.events, st21.releasedSeq).count === 2
     && st21.recoveredPhotos.join() === 'p-known', 'rel.seq=3: 1~3번 반영됨(결과 파일 1 확인), 4·5번은 미전송 그대로, 아는 사진만 회수 확인');
  F.applyRelease(st21, { seq: 2, ph: [] });
  ok(st21.releasedSeq === 3 && F.applyRelease(st21, null) === null, '더 작은 rel은 되돌리지 않음, rel 없으면 아무것도 안 함');
  const r3 = F.applyRelease(st21, { seq: 9, ph: ['p-kept'] });
  ok(st21.releasedSeq === 9 && st21.seq === 9 && r3.events === 2 && st21.exports[1].confirmed && st21.recoveredPhotos.includes('p-kept'),
     'PC가 아는 번호가 휴대폰 번호보다 크면(오래된 백업 복원 등) 다음 기록 번호도 그 뒤로 — PC에 있는 번호를 다시 쓰지 않음');
}

// 22) 백업 복원: seq = max(지금, 백업), PC 연결 방식은 반영 상태 유지·기록 합침 → 복원 뒤 새 기록을 PC가 충돌 없이 받음 ---------
{
  const sid = opened.payload.stops[1].request_id;
  const mkEv = (n) => ({ event_id: crypto.randomUUID(), seq: n, request_id: sid, assign_ver: opened.payload.assign_ver, base_req_ver: 1, kind: 'CONTACT', body: { result: 'REACHED' }, ts: new Date().toISOString() });
  const send = async (events, tag) => {
    const head = { btype: P.TYPE_RESULT, bundleId: P.randomBytes(16), deviceId, assignVer: opened.payload.assign_ver, dataVer: 1, signerKeyId: await P.keyId(signRaw) };
    const s = await P.seal({ schema: 'dg.result/1', work_date: opened.payload.work_date, events: events.map(({ ts, ...e }) => ({ ...e, device_ts: ts })), photos: [] }, head, pcKexPub, dev.sign.privateKey);
    const f = join(WORK, `restore-${tag}.dgr`); writeFileSync(f, s.bytes); return f;
  };
  // 이 기기는 4) 에서 1·2번을 보냈다. 백업은 2번까지(모두 반영), 그 뒤 3·4번을 보내 PC가 가져감(반영 코드는 아직)
  const e1 = { ...mkEv(1), event_id: result.events[0].event_id }, e2 = { ...mkEv(2), event_id: result.events[1].event_id };
  const bak = { mode: 'PC', seq: 2, releasedSeq: 2, events: [e1, e2], exports: [{ from: 1, to: 2, code: sealed.receipt, confirmed: true }] };
  const e3 = mkEv(3), e4 = mkEv(4);
  const cur = { mode: 'PC', seq: 4, releasedSeq: 2, events: [e1, e2, e3, e4], exports: [...bak.exports, { from: 3, to: 4, code: 'CCCC4444', confirmed: false }] };
  ok(py('import-result', await send([e3, e4], 'a')).includes('"applied": 2'), '복원 전: 3·4번 PC로 감');
  // 예전 복원(백업으로 덮어씀)이면 다음 기록이 3번 → PC에 이미 있는 번호라 가져오기 실패
  const naiveNext = { ...mkEv(bak.seq + 1) };
  const naiveFile = await send([naiveNext], 'naive');
  let naiveOut; try { naiveOut = py('import-result', naiveFile); } catch (e) { naiveOut = 'FAIL ' + (e.stdout || '') + (e.stderr || ''); }
  const warnOf = (id) => pyEval(`from dgpc import store, service as S
c = store.connect()
r = S.one(c, "SELECT * FROM result_event WHERE event_id=?", ("${id}",))
print((r or {}).get("warning") or "")`);
  ok(/^FAIL/.test(naiveOut) || /순번/.test(warnOf(naiveNext.event_id)), '(비교) 백업으로 덮어쓰면 다음 기록이 PC에 있는 번호(3번)를 다시 씀 → PC가 순번 재사용 경고(이전 PC는 가져오기 실패)');
  const m = F.mergeRestore(cur, bak);
  ok(m.seq === 4 && m.releasedSeq === 2 && m.events.length === 4 && m.exports.length === 2, `합친 복원: seq ${m.seq}(= max), 반영 상태·기록 4건·결과 파일 2개 유지`);
  const e5 = mkEv(m.seq + 1); m.seq += 1; m.events.push(e5);
  const unsentNow = m.events.filter((e) => e.seq > m.releasedSeq);
  const out22 = py('import-result', await send(unsentNow, 'b'));
  ok(out22.includes('"applied": 1') && out22.includes('"skipped_duplicate": 2') && out22.includes('"conflict": 0') && !warnOf(e5.event_id),
     '복원 → 새 기록(5번) → PC 가져오기: 충돌 0·순번 재사용 없음, 새 기록 1건 반영, 이미 받은 3·4번은 중복으로 건너뜀');
  // 단독 방식: 백업으로 바꾸되 seq만 지킴. 같은 번호의 다른 기록은 뒤로 옮김
  const solo = F.mergeRestore({ mode: 'SOLO', seq: 9, events: [] }, { mode: 'SOLO', seq: 3, events: [mkEv(1)], requests: [{ request_id: 'x' }] });
  ok(solo.seq === 9 && solo.requests.length === 1 && solo.events.length === 1, '단독 방식 복원: 접수·기록은 백업 것, 기록 번호는 max(지금, 백업)');
  const a = mkEv(5), b = { ...mkEv(5), event_id: 'other' };
  const clash = F.mergeRestore({ mode: 'PC', seq: 5, releasedSeq: 4, events: [b] }, { mode: 'PC', seq: 5, releasedSeq: 4, events: [a] });
  ok(clash.events.find((e) => e.event_id === a.event_id).seq === 5 && clash.events.find((e) => e.event_id === 'other').seq === 6 && clash.seq === 6,
     '서로 다른 기록이 같은 번호면 백업 쪽을 그 번호에 두고 나머지는 뒤 번호로');
}

// 23) 같은 날 갱신본: 이 휴대폰 기록이 있는 지점을 PC가 재방문처럼 돌려보내도 기록이 그대로 보임 ------------------------------
{
  const it = (id, q) => ({ line_id: id, line_kind: 'RECEIVED', name: '의자', spec: '고정식', unit: 'EA', unit_label: '개', qty: q, qty_received: q, prior_collected: 0, unit_price: 2000, amount: 2000 * q, price_status: 'CONFIRMED', fee_rate_id: 'x' });
  const old = { work_date: '2026-11-05', stops: [{ request_id: 'A', seq: 1, items: [it('a1', 2), it('a2', 1)] }, { request_id: 'B', seq: 2, items: [it('b1', 1)] }] };
  const evs = [{ seq: 1, request_id: 'A', kind: 'ITEM_RESULT', body: { line_id: 'a1', qty_confirmed: 2, qty_collected: 2 } },
    { seq: 2, request_id: 'A', kind: 'ITEM_RESULT', body: { line_id: 'a2', qty_confirmed: 1, qty_collected: 0, reason: 'NOT_PUT_OUT' } },
    { seq: 3, request_id: 'A', kind: 'FIELD_ITEM_ADD', body: { line_id: 'f1', name: '안마기', spec: '', unit: 'EA', unit_label: '개', qty: 1, unit_price: null, price_status: 'PENDING' } },
    { seq: 4, request_id: 'A', kind: 'ITEM_RESULT', body: { line_id: 'a1', qty_confirmed: 3, qty_collected: 2, reason: 'CANNOT_LOAD' } },
    { seq: 5, request_id: 'A', kind: 'ATTEMPT', body: { outcome: 'PARTIAL' } }];
  const before = deriveStop(old.stops[0], evs);
  // PC 중간 반영 뒤 같은 날 목록: A는 재방문, 다 실은 a1 줄은 빠지고 a2는 prior_collected 0, B는 PC에서 재방문(이 휴대폰 기록 없음)
  const pl = { work_date: '2026-11-05', stops: [{ request_id: 'A', seq: 1, revisit: true, items: [{ ...it('a1', 3), qty_received: 2, prior_collected: 2 }, { ...it('a2', 1) },
    { ...it('f1', 1), line_kind: 'FIELD', qty_received: 0, amount: 0, unit_price: null, price_status: 'PENDING' }] }, { request_id: 'B', seq: 2, revisit: true, items: [it('b1', 1)] }] };
  const n = F.carryOver(old, pl, evs);
  const after = deriveStop(pl.stops[0], evs);
  const diff = F.diffDaylist(old, pl);
  ok(n === 1 && !pl.stops[0].revisit && pl.stops[0].items.length === 2 && after.progress === before.progress && after.outcome === 'PARTIAL' && after.lines.length === 3
     && JSON.stringify(after.lines) === JSON.stringify(before.lines) && JSON.stringify(after.fee) === JSON.stringify(before.fee)
     && !(diff.changes.A || []).includes('INFO') && pl.stops[1].revisit === true,
     '같은 날 갱신본: 이 휴대폰이 기록한 지점은 재방문·줄 빠짐으로 바뀌지 않음(기록 그대로, 정보 변경 표시 없음), 기록 없는 지점은 PC 값 그대로');
  // 이 휴대폰이 남긴 재방문 약속을 PC가 방문 약속·도착 예정으로 돌려보낸 것도 '변경'이 아님
  const o2 = { work_date: 'd', stops: [{ request_id: 'C', seq: 1, eta: '09:00', appt: null, items: [it('c1', 1)] }] };
  const p2 = { work_date: 'd', stops: [{ request_id: 'C', seq: 1, eta: '14:00', appt: ['14:00', '16:00'], items: [it('c1', 1)] }] };
  F.carryOver(o2, p2, [{ seq: 9, request_id: 'C', kind: 'REVISIT_APPT', body: { date: '2026-11-06', start: '14:00', end: '16:00' } }]);
  ok(p2.stops[0].appt === null && p2.stops[0].eta === '09:00' && !F.diffDaylist(o2, p2).changes.C, '이 휴대폰의 재방문 약속을 PC가 돌려보낸 방문 약속·시간은 변경으로 표시하지 않음');
  const other = { work_date: '2026-11-06', stops: pl.stops.map((s) => ({ ...s })) };
  ok(F.carryOver(old, other, evs) === 0, '다른 날짜 목록에는 적용하지 않음(재방문은 since_seq로 전날 기록과 나눔)');
}

// 24) 코드 세 가지: 수신 코드 R- · 결과 반영 코드 D- · 사진 회수 코드 P- — 칸이 맞는지 확인 ---------------------------------------------
{
  const known = { RESULT: ['ABCD2345'], PHOTO: ['PQRS6789'], RECEIPT: ['WXYZ2345'] };
  ok(F.parseCode(' d-abcd 2345 ').prefix === 'D' && F.parseCode(' d-abcd 2345 ').code === 'ABCD2345' && F.parseCode('ABCD 2345').prefix === '' && F.parseCode('PPQRS6789').prefix === 'P',
     '코드 정리: 공백·대소문자·앞 글자(D-/P-/R-) 떼기');
  ok(F.checkCode('abcd2345', 'RESULT', known).code === 'ABCD2345' && F.checkCode('D-ABCD2345', 'RESULT', known).code === 'ABCD2345' && F.checkCode('P-PQRS6789', 'PHOTO', known).code === 'PQRS6789',
     '맞는 칸에 넣은 코드는 받음 (앞 글자 있어도·없어도)');
  ok(/사진 회수 코드/.test(F.checkCode('PQRS6789', 'RESULT', known).error) && /결과 반영 코드/.test(F.checkCode('ABCD2345', 'PHOTO', known).error)
     && /수신 코드/.test(F.checkCode('WXYZ2345', 'RESULT', known).error) && /사진 회수 코드/.test(F.checkCode('P-ABCD2345', 'RESULT', known).error)
     && /맞는 결과 반영 코드가 없습니다/.test(F.checkCode('ZZZZ2345', 'RESULT', known).error) && /8자리/.test(F.checkCode('ABC', 'RESULT', known).error),
     '다른 칸 코드(사진↔결과)·수신 코드·다른 앞 글자·모르는 코드·형식 오류는 이유와 함께 거절');
  ok(F.showCode('RESULT', 'ABCD2345') === 'D-ABCD 2345', '표시: D-ABCD 2345');
}

console.log(`\n통과 ${passed}개`);
