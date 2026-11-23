// 업무 QR을 받은 휴대폰이 담당 PC에 보낼 수거 기록.
function number(value, label, max = 9999, min = 0) {
  if (!/^\d+$/.test(String(value)) || Number(value) < min || Number(value) > max)
    throw new Error(label + '에 ' + min + '~' + max + ' 사이의 정수를 입력하세요.');
  return Number(value);
}
function text(value, label, limit, required = true) {
  const trimmed = String(value ?? '').trim();
  if ((required && !trimmed) || trimmed.length > limit) throw new Error(label + '을(를) ' + limit + '자 이내로 입력하세요.');
  return trimmed;
}
function workItems(rows, additions) {
  if ((!additions && !rows.length) || rows.length > 100) throw new Error('품목은 한 기록에 100개까지 입력할 수 있습니다.');
  return rows.map((row) => {
    const name = text(row.name, '품목명', 60), qty = number(row.qty, '품목 수량', 9999, 1);
    return additions ? {name, qty, collected:number(row.collected, '추가 품목 수거 수량', qty)} : {name, qty};
  });
}
export function collectEntry(id, stop, form) {
  if (form.confirmed.length !== stop.items.length || form.qty.length !== stop.items.length)
    throw new Error('배차 품목을 다시 확인하세요.');
  const confirmed = form.confirmed.map((n) => number(n, '현장 확인 수량'));
  const qty = form.qty.map((n,i) => number(n, '수거 수량', confirmed[i]));
  const additions = workItems(form.additions, true);
  const reasonNeeded = additions.length > 0 || qty.every(n => n === 0) ||
    stop.items.some((it,i) => it.qty !== confirmed[i] || qty[i] !== confirmed[i]);
  const reason = text(form.reason, '변동·미수거 사유', 200, reasonNeeded);
  return {id, kind:'collection', seq:stop.seq, qty, confirmed, additions, reason};
}
export function intakeEntry(id, form) {
  const phone = form.phone.trim().replace(/[- ()]/g, '');
  if (!/^0[0-9]{8,10}$/.test(phone)) throw new Error('연락처에 지역번호 또는 010을 포함한 전화번호를 입력하세요.');
  return {id, kind:'intake', name:text(form.name,'신청인',60), phone, address:text(form.address,'배출 주소',300),
    dong:text(form.dong,'행정동',20,false), items:workItems(form.items,false)};
}
export function unloadEntry(id, form) {
  const site = text(form.site, '하차지', 120), kg = Number(form.weight_kg);
  if (!Number.isFinite(kg) || kg <= 0 || kg > 1000000) throw new Error('하차 중량은 0보다 크고 1,000,000kg 이하여야 합니다.');
  return {id, kind:'unload', site, weight_kg:kg};
}
export function outcome(entry) {
  const got = entry.qty.reduce((s,n) => s+n, 0) + entry.additions.reduce((s,it) => s+it.collected, 0);
  if (!got) return '미수거';
  return entry.qty.every((n,i) => n === entry.confirmed[i]) && entry.additions.every(it => it.qty === it.collected) ? '수거 완료' : '부분 수거';
}
function encode(bytes) {
  let str = '';
  for (let i = 0; i < bytes.length; i += 0x4000) str += String.fromCharCode(...bytes.subarray(i, i + 0x4000));
  return btoa(str).replaceAll('+','-').replaceAll('/','_').replace(/=+$/, '');
}
// PC의 base64url 암호문 한도(2 MiB) 안에서 한글의 실제 UTF-8 길이까지 센다.
const PLAIN_LIMIT = 1024 * 1024;
export function workFileRanges(entries) {
  const result = [], encoder = new TextEncoder();
  const overhead = encoder.encode('{"entries":[]}').length;
  let start = 0, total = overhead;
  for (let i=0; i<entries.length; i++) {
    const size = encoder.encode(JSON.stringify(entries[i])).length;
    if (size + overhead > PLAIN_LIMIT) throw new Error('한 기록이 결과 파일 크기 한도를 넘었습니다.');
    if (i-start >= 500 || total + size + 1 > PLAIN_LIMIT) {
      result.push([start,i]); start = i; total = overhead;
    }
    total += size + (i===start ? 0 : 1);
  }
  if (start < entries.length) result.push([start,entries.length]);
  return result;
}
export async function createWorkFile({ticket, return_key, entries}) {
  if (!entries.length || entries.length > 500) throw new Error('결과 파일당 1~500건의 기록이 필요합니다.');
  const raw = Uint8Array.from(atob(return_key.replaceAll('-','+').replaceAll('_','/')), c => c.charCodeAt(0));
  const key = await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt']);
  raw.fill(0);
  const iv = crypto.getRandomValues(new Uint8Array(12)), enc = new TextEncoder();
  const plain = enc.encode(JSON.stringify({entries}));
  if (plain.length > PLAIN_LIMIT) throw new Error('결과 파일 크기 한도를 넘었습니다.');
  const cipher = await crypto.subtle.encrypt({name:'AES-GCM', iv, additionalData:enc.encode(ticket)}, key, plain);
  return {schema:'dg.guest-return/1', ticket, iv:encode(iv), ciphertext:encode(new Uint8Array(cipher))};
}
