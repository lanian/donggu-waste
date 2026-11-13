import assert from 'node:assert/strict';
import * as F from '../web/fees.js';
import { BUILTIN_FEES as T } from '../web/fees-data.js';

assert.equal(typeof F.quoteFees, 'function', 'quoteFees calculates without creating an intake');
assert.equal(typeof F.feeQuoteText, 'function', 'feeQuoteText describes the same quote for copying');
let count = 0;
const test = (label, fn) => { fn(); count++; console.log('  ✓', label); };
const chair = T.rows.find((r) => r.name === '의자' && r.spec === '고정식 의자');
const wardrobe = T.rows.find((r) => r.name === '장롱' && r.spec === '120cm 이상 1쪽당');
const pending = T.rows.find((r) => r.status === 'REVIEW');
const item = (row, qty) => ({ fee_rate_id: row.id, qty });

test('multiple specifications retain their quantity units and confirmed sum', () => {
  const q = F.quoteFees(T, [item(chair, 3), item(wardrobe, 2)]);
  assert.equal(q.total, 36000);
  assert.equal(q.confirmedSubtotal, 36000);
  assert.deepEqual(q.qtyByUnit, { 개: 3, 쪽: 2 });
  assert.equal(q.lines[0].amount, 6000);
  assert.equal(q.lines[1].amount, 30000);
  assert.equal(q.version, T.version_id);
});
test('pending rows do not become free or a numeric final amount', () => {
  const q = F.quoteFees(T, [item(chair, 2), item(pending, 1)]);
  assert.equal(q.total, null);
  assert.equal(q.confirmedSubtotal, 4000);
  assert.equal(q.pendingCount, 1);
  assert.equal(q.lines[1].amount, null);
  assert.equal(q.lines[1].unit_price, null);
});
test('approved override prices are taken from the actual current table', () => {
  const revised = { ...T, version_id: 'test-revised', rows: T.rows.map((r) => r.id === pending.id ? { ...r, status: 'CONFIRMED', price: 12345 } : r) };
  assert.equal(F.quoteFees(revised, [item(pending, 2)]).total, 24690);
});
test('an explicit confirmed zero price remains distinct from unknown price', () => {
  const table = { ...T, rows: [{ ...chair, status: 'CONFIRMED', price: 0 }] };
  assert.equal(F.quoteFees(table, [item(chair, 2)]).total, 0);
});
test('bad or absent confirmed prices remain pending', () => {
  for (const price of [null, undefined, NaN, -1, Infinity, '2000', 1.5]) {
    const table = { ...T, rows: [{ ...chair, status: 'CONFIRMED', price }] };
    assert.equal(F.quoteFees(table, [item(chair, 1)]).total, null);
  }
});
test('quantity accepts integer input up to the intake limit', () => {
  assert.equal(F.quoteFees(T, [item(chair, '3')]).total, 6000);
  assert.equal(F.quoteFees(T, [item(chair, 9999)]).total, 19998000);
  for (const qty of ['', ' ', null, undefined, 0, -1, 0.5, 10000, Infinity, NaN, '2개', true]) {
    assert.throws(() => F.quoteFees(T, [item(chair, qty)]), /수량/);
  }
});
test('a missing rate cannot silently disappear from the total', () => {
  assert.throws(() => F.quoteFees(T, [{ fee_rate_id: 'missing', qty: 1 }]), /요금표/);
});
test('an empty calculator has a zero total and no pending or units', () => {
  const q = F.quoteFees(T, []);
  assert.equal(q.total, 0);
  assert.equal(q.pendingCount, 0);
  assert.deepEqual(q.qtyByUnit, {});
});
test('copy describes before-reduction amount, rate version and both unit counts', () => {
  const text = F.feeQuoteText(F.quoteFees(T, [item(chair, 3), item(wardrobe, 2)]));
  assert.ok(text.includes('감면 전') && text.includes('36,000원'));
  assert.ok(text.includes('3개') && text.includes('2쪽') && text.includes(T.version_id));
});
test('copy preserves uncertainty with a confirmed subtotal', () => {
  const text = F.feeQuoteText(F.quoteFees(T, [item(chair, 2), item(pending, 1)]));
  assert.ok(text.includes('금액 확인 대기') && text.includes('확정 소계 4,000원'));
  assert.ok(!text.includes('합계 4,000원'));
});
console.log(`통과 ${count}개`);
