// 휴대폰 화면 표시용 '보낼 기록'·'확인 대기' 계산 (web/field.js outboxInfo). 가상 자료만 사용한다.
// 실행: node tests/outbox.test.mjs
import * as F from '../web/field.js';

let passed = 0;
const ok = (cond, msg) => {
  if (!cond) { console.error('  ✗', msg); process.exit(1); }
  passed++; console.log('  ✓', msg);
};

const now = Date.parse('2026-11-16T12:00:00Z');
const ev = (seq, ts) => ({ seq, ts, kind: 'CONTACT', request_id: 'r' + seq });
const events = [ev(1, '2026-11-16T01:00:00Z'), ev(2, '2026-11-16T02:00:00Z'), ev(3, '2026-11-16T03:00:00Z'), ev(4, '2026-11-16T08:00:00Z'), ev(5, '2026-11-16T10:00:00Z')];

ok(typeof F.outboxInfo === 'function', 'field.js 에 순수 함수 outboxInfo 가 있음');

// 아무 파일도 만들지 않음: releasedSeq 뒤 기록은 모두 보낼 기록
let o = F.outboxInfo(events, 1, [], now);
ok(o.toSend === 4 && o.waiting === 0, `파일 없음: 보낼 기록 4·확인 대기 0 (${JSON.stringify(o)})`);
ok(o.oldestTs === '2026-11-16T02:00:00.000Z' && Math.abs(o.ageH - 10) < 1e-9, '가장 오래된 보낼 기록은 2번(10시간 전)');

// 2~3번을 결과 파일로 만들었지만 D-코드 전: 확인 대기 2, 보낼 기록 2(4·5번), 나이는 보낼 기록 기준
const exp = [{ from: 2, to: 3, code: 'AAAAAAAA', confirmed: false }];
o = F.outboxInfo(events, 1, exp, now);
ok(o.toSend === 2 && o.waiting === 2, `파일 2~3: 보낼 기록 2·확인 대기 2 (${JSON.stringify(o)})`);
ok(o.oldestTs === '2026-11-16T08:00:00.000Z' && Math.abs(o.ageH - 4) < 1e-9, '나이는 보낼 기록(4번, 4시간 전) 기준 — 파일에 들어간 옛 기록은 세지 않음');

// 모두 파일에 들어감: 보낼 기록 0, 확인 대기 4, 나이 없음
o = F.outboxInfo(events, 1, [...exp, { from: 4, to: 5, code: 'BBBBBBBB', confirmed: false }], now);
ok(o.toSend === 0 && o.waiting === 4 && o.oldestTs === null && o.ageH === 0, `모두 파일에 들어감: 보낼 기록 0·확인 대기 4 (${JSON.stringify(o)})`);

// D-코드로 3번까지 반영: releasedSeq 이하는 어느 쪽에도 세지 않음
o = F.outboxInfo(events, 3, [{ ...exp[0], confirmed: true }], now);
ok(o.toSend === 2 && o.waiting === 0, `D-코드로 3번까지 반영: 보낼 기록 2(4·5번)·확인 대기 0 (${JSON.stringify(o)})`);

// 확인된 파일 범위가 releasedSeq 보다 뒤까지 있어도(복원 병합 등) 화면 표시로는 '파일에 들어감'
o = F.outboxInfo(events, 1, [{ from: 2, to: 5, code: 'CCCCCCCC', confirmed: true }], now);
ok(o.toSend === 0 && o.waiting === 4, '확인 표시와 무관하게 결과 파일 범위 안이면 확인 대기');

// 기존 unsentInfo 의미는 그대로(seq > releasedSeq 전부)
const u = F.unsentInfo(events, 1, now);
ok(u.count === 4 && Math.abs(u.ageH - 10) < 1e-9, 'unsentInfo 는 그대로 releasedSeq 뒤 전부를 셈');

// exports 가 없거나 비어 있어도 됨, 입력은 바꾸지 않음
const before = JSON.stringify(events);
o = F.outboxInfo(events, 0, undefined, now);
ok(o.toSend === 5 && o.waiting === 0 && JSON.stringify(events) === before, 'exports 없음 → 모두 보낼 기록, 기록 배열은 바꾸지 않음');
ok(F.outboxInfo([], 0, [], now).toSend === 0, '기록 없음 → 0');

console.log(`\n통과 ${passed}개`);
