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
import { deriveStop, needsFeeNotice, feeExtra } from '../web/fees.js';
import { tilesForBbox, isAllowedMapUrl, bboxAround, DONGGU_BBOX } from '../web/map.js';

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
const regOut = py('register-device', code);
const fp = await P.fingerprint(reg.pcSignPub, kexRaw, signRaw);
ok(regOut.includes(fp), `기기 등록 후 양쪽 지문 일치 (${fp})`);
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

console.log(`\n통과 ${passed}개`);
