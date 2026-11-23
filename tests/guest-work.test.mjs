import assert from 'node:assert/strict';
import test from 'node:test';
import { validateGuestList } from '../web/guest-list.js';
import { collectEntry, createWorkFile, workFileRanges } from '../web/guest-work.js';

const ticket = 'opaque-pc-ticket';
const key = crypto.getRandomValues(new Uint8Array(32));
const visit = {seq: 3, address: '가상로 5', dong: '시험동', items: [{name:'의자', spec:'', qty:3, unit:'개'}]};
const work = {schema:'dg.guest-work/1', crew:'시험팀', crew_id:'test', assign_ver:1, issue_no:9,
  work_date:'2030-01-01', stops:[visit], ticket, return_key:Buffer.from(key).toString('base64url')};

test('업무 QR만 결과 열쇠와 발급표를 받는다', () => {
  assert.equal(validateGuestList(work), work);
  for (const bad of [{...work, return_key:'a'.repeat(43)}, {...work, return_key:[work.return_key]}, {...work, ticket:''}, {...work, schema:'dg.guest-list/1'}])
    assert.throws(() => validateGuestList(bad));
});

test('결과 파일을 건수와 UTF-8 암호문 크기 한도 안에서 나눈다', () => {
  assert.deepEqual(workFileRanges(Array(501).fill({kind:'unload'})),[[0,500],[500,501]]);
  const large=Array(200).fill({items:Array(100).fill({name:'의'.repeat(60),qty:9999})});
  const ranges=workFileRanges(large);
  assert(ranges.length>1);
  for (const [start,end] of ranges) assert(Buffer.byteLength(JSON.stringify({entries:large.slice(start,end)})) < 1500000);
  assert.equal(ranges.at(-1)[1],200);
});

test('품목 수량을 조정하고 추가 품목과 미수거 사유를 기록한다', () => {
  const id = crypto.randomUUID();
  const form = {qty:['2'], confirmed:['2'], additions:[{name:'상자', qty:'2', collected:'1'}], reason:'상자 1개 미배출'};
  assert.deepEqual(collectEntry(id, visit, form), {id,kind:'collection',seq:3,qty:[2],confirmed:[2],
    additions:[{name:'상자',qty:2,collected:1}],reason:form.reason});
  for (const change of [{qty:['3']},{qty:['']},{qty:['2.5']},{reason:''},{additions:[{name:'',qty:1,collected:1}]}])
    assert.throws(() => collectEntry(id,visit,{...form,...change}));
});

test('파일에는 PC로 보낼 암호문만 있고 재전송해도 기록 ID가 같다', async () => {
  const entries = [{id:crypto.randomUUID(),kind:'unload',site:'가상 하차장',weight_kg:12.5}];
  const make = () => createWorkFile({...work,entries});
  const a = await make(), b = await make();
  assert.deepEqual(Object.keys(a).sort(), ['schema','ticket','iv','ciphertext'].sort());
  assert.equal(a.schema, 'dg.guest-return/1');
  assert.notEqual(a.iv,b.iv);
  const aes = await crypto.subtle.importKey('raw',key,'AES-GCM',false,['decrypt']);
  const open = (x, aad) => crypto.subtle.decrypt({name:'AES-GCM',iv:Buffer.from(x.iv,'base64url'),
    additionalData:new TextEncoder().encode(aad)}, aes, Buffer.from(x.ciphertext,'base64url'));
  for (const x of [a,b]) assert.deepEqual(JSON.parse(new TextDecoder().decode(await open(x,ticket))), {entries});
  await assert.rejects(open(a, ticket+'x'));
});
