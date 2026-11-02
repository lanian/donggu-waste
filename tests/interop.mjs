// PC(Python) ↔ 휴대폰(WebCrypto) 상호 운용 테스트. 가상 자료만 사용한다.
// 실행: npm test   (Python 3 + cryptography + qrcode 필요)
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import * as P from '../web/proto.js';

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

console.log(`\n통과 ${passed}개`);
