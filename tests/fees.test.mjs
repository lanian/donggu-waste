// 공식 수수료표([별표 1]) 검색·계산 규칙 테스트
import { readFileSync } from 'node:fs';
import { searchFees, deriveStop, chosungOf, matchText, applyReduction, LABEL } from '../web/fees.js';
import { LABELS } from '../web/labels-data.js';
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
// 초성 검색
ok(first('ㅈㄹ').name === '장롱', "초성 'ㅈㄹ' → 장롱");
ok(first('ㅅㅍㄱ').name === '선풍기' && searchFees(T, '3ㅇㅇ ㅅㅍ').some((r) => r.spec === '3인용 소파'), "초성 'ㅅㅍㄱ' → 선풍기, '3ㅇㅇ ㅅㅍ' → 3인용 소파");
ok(searchFees(T, 'ㅁㅌㄹㅅ 2인').length >= 1, "초성과 일반 낱말 섞어 검색 'ㅁㅌㄹㅅ 2인'");
ok(chosungOf('가상하나 동구') === 'ㄱㅅㅎㄴ ㄷㄱ' && matchText('가상하나 동구 시험로', 'ㄱㅅㅎ 시험'), '초성 변환·혼합 일치');
// 재방문: 목록을 받은 시점(since_seq) 전 기록은 오늘 방문 상태에 섞이지 않음
const rv = { request_id: 'r2', req_ver: 1, since_seq: 10, items: [{ line_id: 'x', name: '의자', spec: '', unit: 'EA', unit_label: '개', qty: 2, prior_collected: 1, unit_price: 2000, amount: 4000, price_status: 'CONFIRMED' }] };
const old = [{ seq: 3, request_id: 'r2', kind: 'ATTEMPT', body: { outcome: 'PARTIAL' } }, { seq: 4, request_id: 'r2', kind: 'CONTACT', body: { result: 'REACHED' } }];
const dr = deriveStop(rv, old);
ok(dr.outcome === null && dr.contact === 'NOT_CONTACTED' && dr.lines[0].qty_collected === 1, '재방문: 전날 결과·연락 기록은 오늘 상태로 안 셈(이전 수거분은 prior_collected로만)');
// 같은 날 다시 받은 목록에 이미 있는 현장 추가 줄은 두 번 넣지 않음
const rs = { request_id: 'r3', req_ver: 1, items: [{ line_id: 'F1', line_kind: 'FIELD', name: '식탁', spec: '', unit: 'EA', unit_label: '개', qty: 1, unit_price: 6000, amount: 0, price_status: 'CONFIRMED' }] };
const dup = deriveStop(rs, [{ seq: 1, request_id: 'r3', kind: 'FIELD_ITEM_ADD', body: { line_id: 'F1', name: '식탁', unit: 'EA', unit_label: '개', unit_price: 6000, price_status: 'CONFIRMED', qty: 1 } }]);
ok(dup.lines.length === 1, '다시 받은 목록의 현장 추가 줄 + 현장 추가 기록 → 한 줄(금액 두 배 안 됨)');
// 현장 접수·정정·취소 요청 기록만으로는 '처리 중'이 아님
ok(deriveStop({ request_id: 'r4', req_ver: 1, items: [] }, [{ seq: 1, request_id: 'r4', kind: 'FIELD_INTAKE', body: {} }]).progress !== 'STARTED', '현장 접수 기록만 있으면 처리 중으로 세지 않음');
// PC·휴대폰 공통 계산 규칙: data/derive_vectors.json (tests/test_pc.py 도 같은 파일로 PC derive_pure·fee_summary 를 확인)
const VEC = JSON.parse(readFileSync(new URL('../data/derive_vectors.json', import.meta.url), 'utf8')).vectors;
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
for (const v of VEC) {
  const stop = { request_id: 'v', req_ver: 1, items: v.items, ...(v.reduction_rate ? { reduction: { rate: v.reduction_rate } } : {}) };
  const d = deriveStop(stop, v.events.map((e, i) => ({ request_id: 'v', seq: i + 1, kind: e.kind, body: e.body, ts: `2026-11-12T00:00:0${i}Z` })));
  const x = v.expect, f = d.fee;
  const lines = d.lines.map((l) => ({ line_id: l.line_id, kind: l.kind, price: l.price, qty_confirmed: l.qty_confirmed, qty_collected: l.qty_collected, remaining: l.remaining }));
  const fee = { received: f.received, confirmed_subtotal: f.confirmedSubtotal, field_added: f.fieldAdded, diff: f.diff, pending_count: f.pendingCount, final: f.final, qty_by_unit: f.qtyByUnit };
  const red = { rate: f.reductionRate, reduction: f.reduction, levied: f.levied };
  const ar = applyReduction(f.final, v.reduction_rate);
  ok(same(lines, x.lines) && same(fee, x.fee) && same(red, x.reduction) && same(ar, x.reduction) && d.contact === x.contact && d.outcome === x.outcome,
    `공통 벡터: ${v.name}`);
}
// 표시 이름: data/labels.json → web/labels-data.js (tools/build_labels.py). 휴대폰 LABEL 은 공통 표에서 온다
ok(same(LABEL.contact, LABELS.contact) && same(LABEL.sticker, LABELS.sticker) && LABEL.reason[''] === '사유 선택'
  && same(Object.keys(LABEL.reason).slice(1), Object.keys(LABELS.reason)) && Object.keys(LABEL.outcome).every((k) => LABEL.outcome[k] === LABELS.status[k]),
  '휴대폰 표시 이름(연락·사유·스티커·결과) = data/labels.json');
console.log(`\n통과 ${n}개`);
