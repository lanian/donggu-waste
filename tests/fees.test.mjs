// 공식 수수료표([별표 1]) 검색·계산 규칙 테스트
import { searchFees, deriveStop } from '../web/fees.js';
import { BUILTIN_FEES as T } from '../web/fees-data.js';

let n = 0;
const ok = (c, m) => { if (!c) throw new Error('실패: ' + m); n++; console.log('  ✓', m); };
const first = (q) => searchFees(T, q)[0];
const row = (name, spec) => T.rows.find((r) => r.name === name && r.spec === spec);

ok(T.rows.length === 169, `공식 표 169행 (원문 168행 + 각주 20ℓ당 1행)`);
ok(row('의자', '고정식 의자').price === 2000 && row('의자', '회전식 의자').price === 3000, '의자: 고정식 2,000원, 회전식 3,000원');
ok(row('침대', '1인용 매트리스').price === 8000 && row('침대', '2인용 매트리스').price === 11000, '매트리스: 1인용 8,000원, 2인용 11,000원');
ok(first('3인용 소파').spec === '3인용 소파' && first('3인용 소파').price === 10000, "'3인용 소파' → 응접세트 3인용 소파 10,000원");
ok(first('쇼파').spec.includes('소파'), "현장 표현 '쇼파' → 소파");
ok(first('장농').name === '장롱' && first('장농').unit_label === '쪽', "'장농' → 장롱, 부과단위 '쪽'");
ok(first('회전 의자').spec === '회전식 의자', "'회전 의자' → 회전식 의자");
ok(searchFees(T, '매트리스').length >= 5, "'매트리스' 검색 시 1인용·2인용·2단·틀 등 여러 규격 표시");
const marble = searchFees(T, '대리석');
ok(marble.length === 2 && marble.every((r) => r.status === 'REVIEW'), '대리석 식탁 6인용 경계 겹침 → 두 행 모두 규격 확인 필요');
ok(row('물탱크, 정화조(FRP)', '용적 1.0 ㎡ 당').status === 'REVIEW', '물탱크 용적 단위(㎡) 불명확 → 규격 확인 필요');

// 계산: 확인 필요 행은 금액 확인 대기, 단위가 다른 수량은 합산하지 않음
const stop = { request_id: 'r1', req_ver: 1, items: [
  { line_id: 'a', fee_rate_id: row('장롱', '120cm 이상 1쪽당').id, name: '장롱', spec: '120cm 이상 1쪽당', unit: 'PIECE', unit_label: '쪽', qty: 2, unit_price: 15000, amount: 30000, price_status: 'CONFIRMED' },
  { line_id: 'b', name: '의자', spec: '고정식 의자', unit: 'EA', unit_label: '개', qty: 3, unit_price: 2000, amount: 6000, price_status: 'CONFIRMED' },
] };
const ev = [{ request_id: 'r1', kind: 'FIELD_ITEM_ADD', body: { line_id: 'c', name: '식탁', spec: '6인용 이상(대리석)', unit: 'EA', unit_label: '개', unit_price: 15000, price_status: 'PENDING', qty: 1 } }];
const d = deriveStop(stop, ev);
ok(d.fee.received === 36000 && d.fee.confirmedSubtotal === 36000, '장롱 2쪽 30,000 + 의자 3개 6,000 = 36,000원');
ok(d.fee.final === null && d.fee.pendingCount === 1, '규격 확인 필요 행 추가 → 최종 금액 미확정 (최저가 자동 선택 없음)');
ok(d.fee.qtyByUnit['쪽'] === 2 && d.fee.qtyByUnit['개'] === 4, '쪽과 개를 하나의 개수로 합산하지 않음');
console.log(`\n통과 ${n}개`);
