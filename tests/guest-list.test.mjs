import test from 'node:test';
import assert from 'node:assert/strict';
import { deflateSync } from 'node:zlib';
import { concat, splitFrames, uuidBytes } from '../web/proto.js';
import { GuestScan, guestUpdate, validateGuestList } from '../web/guest-list.js';

const id = '342dbc8e79894133b74f8ef9d28fc4d1';
const otherId = 'ed5ec5c0a2f541589ade97dda01e6206';
const key = crypto.getRandomValues(new Uint8Array(32));
const keyText = (bundleId = id, secret = key) => 'DGVIEW1:' + bundleId + ':' + Buffer.from(secret).toString('base64url');
const stop = { seq: 2, dong: '가상동', address: '시험길 1', items: [{ name: '가상 의자', spec: '', qty: 2, unit: '개' }] };
const list = { schema: 'dg.guest-list/1', crew_id: 'mock-crew', crew: '가상 수거팀', work_date: '2028-02-29', assign_ver: 3, issue_no: 8, stops: [stop] };

async function frames(text = JSON.stringify(list), chunk = 35) {
  const aes = await crypto.subtle.importKey('raw', key, 'AES-GCM', false, ['encrypt']);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: new TextEncoder().encode('DGVIEW1:' + id) }, aes, deflateSync(text));
  return splitFrames(concat(iv, new Uint8Array(ct)), uuidBytes(id), chunk).map((f) => 'DGV1.' + f);
}

test('키, 순서가 뒤바뀐 조각과 중복, 중간에 섞인 다른 팀 QR', async () => {
  const scan = new GuestScan(), f = (await frames()).reverse();
  assert.throws(() => scan.add(f[0]), /열쇠/);
  assert.throws(() => scan.add(keyText() + '='), /열쇠/);
  assert.equal(scan.add(keyText()).kind, 'key');
  assert.equal(scan.add(keyText()).kind, 'key');
  assert.throws(() => scan.add(keyText(otherId)), /다른/);
  assert.equal(scan.add(f[0]).got, 1);
  assert.equal(scan.add(f[0]).got, 1);
  assert.throws(() => scan.add('DGV1.' + splitFrames(new Uint8Array([42]), uuidBytes(otherId))[0]), /다른/);
  await assert.rejects(scan.open(), /조각/);
  for (const frame of f.slice(1)) scan.add(frame);
  assert.deepEqual(await scan.open(), list);
});

test('조각 개수와 크기, 충돌하는 중복, 바뀐 열쇠, 압축을 푼 크기를 검증', async () => {
  const scan = new GuestScan();
  scan.add(keyText());
  const frame = (b, n) => 'DGV1.' + splitFrames(b, uuidBytes(id), n)[0];
  assert.throws(() => scan.add(frame(new Uint8Array(257), 1)), /크기|개수/);
  assert.throws(() => scan.add(frame(new Uint8Array(701), 701)), /크기|개수/);
  scan.add(frame(new Uint8Array([1, 2]), 1));
  assert.throws(() => scan.add(frame(new Uint8Array([3, 2]), 1)), /일치|바뀌/);
  const wrong = new GuestScan();
  wrong.add(keyText(id, crypto.getRandomValues(new Uint8Array(32))));
  for (const f of await frames()) wrong.add(f);
  await assert.rejects(wrong.open(), /열쇠|복호화/);
  const oversized = new GuestScan();
  oversized.add(keyText());
  for (const f of await frames(' '.repeat(1024 * 1024 + 1))) oversized.add(f);
  await assert.rejects(oversized.open(), /크기/);
});

test('허용 필드, 실제 날짜와 수량, 방문 순서를 검사하며 빈 배차도 받는다', () => {
  assert.deepEqual(validateGuestList({ ...list, stops: [] }), { ...list, stops: [] });
  for (const bad of [
    { ...list, work_date: '2027-02-29' }, { ...list, work_date: '2028-13-01' }, { ...list, work_date: '2028-3-1' },
    { ...list, issue_no: 0 }, { ...list, assign_ver: 1.5 }, { ...list, rel: {} },
    { ...list, stops: [{ ...stop, phone: '000' }] }, { ...list, stops: [stop, stop] },
    { ...list, stops: [{ ...stop, items: [{ ...stop.items[0], qty: -3 }] }] },
    { ...list, stops: [{ ...stop, items: [{ ...stop.items[0], amount: 5000 }] }] },
  ]) assert.throws(() => validateGuestList(bad), /형식/);
});

test('날짜 만료, 같은 배정의 재발급 역순, 다른 팀과 날짜 교체', () => {
  const today = list.work_date;
  assert.equal(guestUpdate(null, list, today), 'save');
  assert.equal(guestUpdate(list, list, today), 'duplicate');
  assert.equal(guestUpdate(list, { ...list, issue_no: 9 }, today), 'save');
  assert.throws(() => guestUpdate(list, { ...list, issue_no: 7 }, today), /이전/);
  assert.throws(() => guestUpdate(list, { ...list, assign_ver: 2, issue_no: 9 }, today), /이전/);
  assert.throws(() => guestUpdate(null, list, '2028-03-01'), /지난/);
  assert.equal(guestUpdate(list, { ...list, crew_id: 'other' }, today), 'replace');
  assert.equal(guestUpdate(list, { ...list, work_date: '2028-03-01' }, today), 'replace');
});
