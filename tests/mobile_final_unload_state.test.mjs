import assert from 'node:assert/strict';
import { finalUnloadState } from '../web/final-unload.js';
import { expandDaylist } from '../web/proto.js';

const day = '2026-11-20';
const depot = { lat: 35.155915, lon: 126.92974 };
const received = (id, seq) => ({
  request_id: id, seq, items: [{ line_id: id + '-line', qty: 1, prior_collected: 0 }],
});
const one = received('first', 2);
const two = { ...received('last', 4), unload_after: {
  final: true, lat: 35.154308, lon: 126.926742, depot, stop_count: 2,
} };
const list = { work_date: day, assign_ver: 4, stops: [one, two] };
const state = (events = [], patch = {}) => ({ mode: 'PC', daylist: list, events, ...patch });
const loaded = (stop, seq) => ({ seq, request_id: stop.request_id, kind: 'ITEM_RESULT',
  body: { line_id: stop.items[0].line_id, qty_confirmed: 1, qty_collected: 1 } });
const result = (stop, seq, outcome = 'DONE') => ({ seq, request_id: stop.request_id, kind: 'ATTEMPT', body: { outcome } });
const unloaded = (seq, work_date = day, assign_ver = 4) => ({ seq, request_id: '', kind: 'UNLOAD', assign_ver, body: { work_date } });

assert.equal(finalUnloadState(state(), '2026-11-21'), null);
assert.equal(finalUnloadState(state([], { mode: 'SOLO' }), day), null);
assert.equal(finalUnloadState(state([], { daylist: { ...list, stops: [one] } }), day), null);
const untouched = finalUnloadState(state(), day);
assert.equal(untouched.ready, false);
assert.equal(untouched.hasLoad, false);
assert.deepEqual(untouched.depot, depot);

const afterMid = [loaded(one, 1), result(one, 2), unloaded(3), loaded(two, 4), result(two, 5)];
const plan = finalUnloadState(state(afterMid), day);
assert.equal(plan.ready, true);
assert.equal(plan.hasLoad, true);
assert.equal(plan.recorded, false);
const returned = finalUnloadState(state([...afterMid, unloaded(6)], { finalUnloadReceipt: plan.key }), day);
assert.equal(returned.recorded, true);
assert.equal(returned.hasLoad, false);
assert.equal(finalUnloadState(state(afterMid, { finalUnloadReceipt: plan.key, daylist: { ...list, assign_ver: 5 } }), day).recorded, false);
assert.equal(finalUnloadState(state([...afterMid, unloaded(6, '2026-11-19')]), day).hasLoad, true);
const empty = finalUnloadState(state([result(one, 1, 'NOT_COLLECTED'), result(two, 2, 'ON_HOLD')]), day);
assert.equal(empty.ready, true);
assert.equal(empty.hasLoad, false);

const printed = state(afterMid, { daylist: { ...list, printed_since_seq: 0, stops: [two] } });
assert.equal(finalUnloadState(printed, day).missingStops, 1);
assert.equal(finalUnloadState(printed, day).ready, false);
const corrected = { ...one, removed: true };
assert.equal(finalUnloadState(state(afterMid, { daylist: { ...list, stops: [corrected, two] } }), day).ready, true);
const local = { ...received('field', 5), field: true, planned_date: day };
assert.equal(finalUnloadState(state(afterMid, { fieldStops: [local] }), day).ready, false);
assert.equal(finalUnloadState(state(afterMid, { fieldStops: [{ ...local, planned_date: '2026-11-21' }] }), day).ready, true);
const newLoad = finalUnloadState(state([...afterMid, unloaded(6), loaded(local, 7), result(local, 8)],
  { fieldStops: [local], finalUnloadReceipt: plan.key }), day);
assert.equal(newLoad.ready, true);
assert.equal(newLoad.hasLoad, true);
assert.equal(newLoad.recorded, false);

const carried = { ...list, carry_load: { volume_m3: 0.5, weight_kg: 0 }, carry_since_seq: 6 };
const noNewPickup = [unloaded(5), result(one, 7, 'NOT_COLLECTED'), result(two, 8, 'ON_HOLD')];
const withoutNewPickup = finalUnloadState(state(noNewPickup, { daylist: carried }), day);
assert.equal(withoutNewPickup.ready, true);
assert.equal(withoutNewPickup.hasLoad, true, '남은 방문에 새 수거가 없어도 PC가 보낸 기존 적재가 남음');
assert.equal(finalUnloadState(state([...noNewPickup, unloaded(9)],
  { daylist: carried, finalUnloadReceipt: withoutNewPickup.key }), day).recorded, true);

const returnOnly = { ...carried, stops: [], final_unload: { ...two.unload_after, stop_count: 0 } };
const standalone = finalUnloadState(state([unloaded(6)], { daylist: returnOnly }), day);
assert.equal(standalone.ready, true);
assert.equal(standalone.hasLoad, true, '방문 없는 새 갱신본의 적재는 예전 하차로 지워지지 않음');
assert.equal(finalUnloadState(state([unloaded(9, day, 3)], { daylist: returnOnly }), day).hasLoad, true);
assert.equal(finalUnloadState(state([unloaded(7)],
  { daylist: returnOnly, finalUnloadReceipt: standalone.key }), day).recorded, true);

const packed = {schema: 'dg.daylist/3', work_date: day, crew: '가상 수거팀', assign_ver: 4, fee: 'fixture',
  s: [], cl: [0.5, 0], fu: ['15:00', '가상 반입지', 20, 0.5, 35.154308, 126.926742, depot.lat, depot.lon]};
const expanded = expandDaylist(packed);
assert.deepEqual(expanded.carry_load, returnOnly.carry_load);
assert.deepEqual(expanded.final_unload.depot, depot);
assert.equal(expanded.final_unload.stop_count, 0);
assert.throws(() => expandDaylist({...packed, cl: [0, 0]}), {code: 'BAD_FORMAT'});
assert.throws(() => expandDaylist({...packed, fu: undefined}), {code: 'BAD_FORMAT'});
console.log('최종 하차 상태 검증 통과');
