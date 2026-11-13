import assert from 'node:assert/strict';
import { carryOver } from '../web/field.js';
import { deriveStop } from '../web/fees.js';
import { expandDaylist } from '../web/proto.js';

const line = {line_id: 'line', name: '가상 의자', spec: '', unit: 'EA', unit_label: '개', qty: 3, qty_received: 3, unit_price: 1000, amount: 3000, price_status: 'CONFIRMED'};
const oldStop = {request_id: 'req', req_ver: 1, items: [line]};
const oldList = {work_date: '2026-11-12', stops: [oldStop]};
const next = {work_date: oldList.work_date, stops: [{...oldStop, req_ver: 2, pc_collection_ver: 2, revisit: true, items: [{...line, prior_collected: 2}]}]};
const events = [
  {request_id: 'req', seq: 1, base_req_ver: 1, kind: 'ITEM_RESULT', body: {line_id: 'line', qty_confirmed: 3, qty_collected: 1}},
  {request_id: 'req', seq: 2, base_req_ver: 1, kind: 'ATTEMPT', body: {outcome: 'PARTIAL'}},
  {request_id: 'req', seq: 3, base_req_ver: 1, kind: 'CONTACT', body: {result: 'REACHED'}}
];
const original = JSON.stringify(events);
carryOver(oldList, next, events);
let state = deriveStop(next.stops[0], events);
assert.equal(state.lines[0].qty_collected, 2, 'PC 추가 수거 기준을 옛 휴대폰 이벤트로 되돌리면 안 됨');
assert.equal(state.contact, 'REACHED');
assert.equal(JSON.stringify(events), original, '미전송 원본 이벤트는 보존');
assert.equal(state.outcome, null, '새 PC 기준 전의 최종 결과를 재생하면 안 됨');
const unaware = [...events, {request_id:'req',seq:4,base_req_ver:2,kind:'ITEM_RESULT',body:{line_id:'line',qty_confirmed:3,qty_collected:0}}];
assert.equal(deriveStop(next.stops[0], unaware).lines[0].qty_collected, 2, '옛 앱이 새 요청 버전만 붙인 로컬 기록도 PC 수량 기준으로 재생하면 안 됨');
events.push({request_id: 'req', seq: 4, base_req_ver: 2, kind: 'ITEM_RESULT', body: {line_id: 'line', qty_confirmed: 3, qty_collected: 3, pc_collection_ver: 2}});
events.push({request_id: 'req', seq: 5, base_req_ver: 2, kind: 'ATTEMPT', body: {outcome: 'DONE', pc_collection_ver: 2}});
state = deriveStop(next.stops[0], events);
assert.equal(state.lines[0].qty_collected, 3);
const compact = {schema: 'dg.daylist/2', assign_ver: 1, s: [{q:1,i:'AAAAAAAAAAAAAAAAAAAAAA',r:'DG-1',v:2,n:'',p:'',a:'',pcv:2,it:[]}]};
assert.equal(expandDaylist(compact, {rows:[]}).stops[0].pc_collection_ver, 2);
console.log('PC 수거 이후 같은 날 목록 갱신·누적 기준·이벤트 보존·선택 프로토콜 필드 통과');
