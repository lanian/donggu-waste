// 휴대폰 안 저장소: IndexedDB + 레코드별 AES-GCM 암호화.
// 데이터 키는 PIN에서 만든 키(PBKDF2-SHA256)로 감싸 두고, 잠금 해제 때만 메모리에 푼다.
// 서버로 보내는 코드는 없다.

const subtle = crypto.subtle;
const DB_NAME = 'dg-proto';
const PBKDF2_ITER = 600000;
let dbp = null;
let dataKey = null; // 잠금 해제 동안만 존재

function idb() {
  if (!dbp) dbp = new Promise((res, rej) => {
    const r = indexedDB.open(DB_NAME, 1);
    r.onupgradeneeded = () => r.result.createObjectStore('kv');
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
  return dbp;
}
async function tx(mode, fn) {
  const db = await idb();
  return new Promise((res, rej) => {
    const t = db.transaction('kv', mode);
    const out = fn(t.objectStore('kv'));
    t.oncomplete = () => res(out && 'result' in out ? out.result : undefined);
    t.onerror = () => rej(t.error);
    t.onabort = () => rej(t.error || new Error('저장 실패'));
  });
}
export const getRaw = (k) => tx('readonly', (s) => s.get(k));
export const putRaw = (k, v) => tx('readwrite', (s) => s.put(v, k));
export const delRaw = (k) => tx('readwrite', (s) => s.delete(k));
export const keysRaw = () => tx('readonly', (s) => s.getAllKeys());

export async function persist() {
  try { return navigator.storage && navigator.storage.persist ? await navigator.storage.persist() : false; } catch { return false; }
}

// ------------------------------------------------------------ PIN과 데이터 키
async function pinKey(pin, salt) {
  const base = await subtle.importKey('raw', new TextEncoder().encode(pin), 'PBKDF2', false, ['deriveKey']);
  return subtle.deriveKey({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: PBKDF2_ITER }, base,
    { name: 'AES-GCM', length: 256 }, false, ['wrapKey', 'unwrapKey']);
}

export const hasPin = async () => !!(await getRaw('lock'));
export const isUnlocked = () => !!dataKey;
export const lock = () => { dataKey = null; };

export async function setupPin(pin) {
  const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
  const k = await subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
  const wrapped = new Uint8Array(await subtle.wrapKey('raw', k, await pinKey(pin, salt), { name: 'AES-GCM', iv }));
  await putRaw('lock', { salt, iv, wrapped, iter: PBKDF2_ITER, fails: 0, until: 0 });
  return unlock(pin);
}

export async function unlock(pin) {
  const L = await getRaw('lock');
  if (Date.now() < (L.until || 0)) throw Object.assign(new Error('잠시 후 다시 시도하세요'), { code: 'LOCKED_OUT', until: L.until });
  try {
    dataKey = await subtle.unwrapKey('raw', L.wrapped, await pinKey(pin, L.salt), { name: 'AES-GCM', iv: L.iv },
      { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
    if (L.fails) await putRaw('lock', { ...L, fails: 0, until: 0 });
    return true;
  } catch { throw await countFail(L); }
}

// ------------------------------------------------------------ 생체 인증 빠른 잠금 해제 (WebAuthn PRF)
// 지문·얼굴 인증 뒤 인증기가 돌려주는 PRF 값(기기 밖으로 나가지 않는 비밀 + 소금으로 계산)에서 HKDF로 키를 만들어
// 데이터 키를 한 번 더 감싸 둔다. 감싼 결과만 저장하고, 데이터 키를 풀린 채로 저장하지 않는다.
// PRF를 지원하지 않는 기기에서는 이 기능을 켜지 않는다(감쌀 비밀이 없으므로).
async function prfKey(prfOut, salt) {
  const base = await subtle.importKey('raw', prfOut, 'HKDF', false, ['deriveKey']);
  return subtle.deriveKey({ name: 'HKDF', hash: 'SHA-256', salt, info: new TextEncoder().encode('DG-bio-v1') }, base,
    { name: 'AES-GCM', length: 256 }, false, ['wrapKey', 'unwrapKey']);
}
async function countFail(L) {
  const fails = (L.fails || 0) + 1;
  const until = fails >= 5 ? Date.now() + Math.min(30000 * 2 ** (fails - 5), 30 * 60000) : 0;
  await putRaw('lock', { ...L, fails, until });
  return Object.assign(new Error('PIN이 맞지 않습니다'), { code: 'BAD_PIN', fails, until });
}
/** PIN을 다시 확인한 뒤(틀리면 실패 횟수에 셈) 데이터 키를 PRF 키로 감싸 저장한다. */
export async function enrollBio(pin, { credId, prfSalt, prfOut }) {
  const L = await getRaw('lock');
  if (Date.now() < (L.until || 0)) throw Object.assign(new Error('잠시 후 다시 시도하세요'), { code: 'LOCKED_OUT', until: L.until });
  let k;
  try {
    // 감싸기 위해서만 잠깐 내보낼 수 있는 키로 푼다. 이 키는 저장하지 않고 이 함수가 끝나면 버린다.
    k = await subtle.unwrapKey('raw', L.wrapped, await pinKey(pin, L.salt), { name: 'AES-GCM', iv: L.iv }, { name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
  } catch { throw await countFail(L); }
  const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
  const wrapped = new Uint8Array(await subtle.wrapKey('raw', k, await prfKey(prfOut, salt), { name: 'AES-GCM', iv }));
  await putRaw('bio', { credId, prfSalt, salt, iv, wrapped, at: new Date().toISOString() });
}
export const getBio = () => getRaw('bio');
export const removeBio = () => delRaw('bio');
export async function unlockBio(prfOut) {
  const B = await getRaw('bio');
  if (!B) throw Object.assign(new Error('생체 인증이 등록되어 있지 않습니다'), { code: 'NO_BIO' });
  try {
    dataKey = await subtle.unwrapKey('raw', B.wrapped, await prfKey(prfOut, B.salt), { name: 'AES-GCM', iv: B.iv },
      { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
  } catch { throw Object.assign(new Error('생체 인증 키가 맞지 않습니다. PIN으로 여세요.'), { code: 'BIO_FAIL' }); }
  return true;
}

// ------------------------------------------------------------ 암호화 레코드
const enc = new TextEncoder(), dec = new TextDecoder();
async function encryptBytes(bytes, aad) {
  if (!dataKey) throw Object.assign(new Error('잠겨 있습니다'), { code: 'LOCKED' });
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await subtle.encrypt({ name: 'AES-GCM', iv, additionalData: enc.encode(aad) }, dataKey, bytes));
  return { iv, ct };
}
async function decryptBytes(rec, aad) {
  if (!dataKey) throw Object.assign(new Error('잠겨 있습니다'), { code: 'LOCKED' });
  return new Uint8Array(await subtle.decrypt({ name: 'AES-GCM', iv: rec.iv, additionalData: enc.encode(aad) }, dataKey, rec.ct));
}
export async function putSecure(k, obj) { await putRaw('s:' + k, await encryptBytes(enc.encode(JSON.stringify(obj)), k)); }
export async function getSecure(k) { const r = await getRaw('s:' + k); return r ? JSON.parse(dec.decode(await decryptBytes(r, k))) : null; }
export async function putSecureBytes(k, bytes) { await putRaw('b:' + k, await encryptBytes(bytes, k)); }
export async function getSecureBytes(k) { const r = await getRaw('b:' + k); return r ? decryptBytes(r, k) : null; }
export const delSecureBytes = (k) => delRaw('b:' + k);

export async function wipeAll() {
  dataKey = null;
  const db = await idb(); db.close(); dbp = null;
  await new Promise((res, rej) => { const r = indexedDB.deleteDatabase(DB_NAME); r.onsuccess = res; r.onerror = rej; r.onblocked = res; });
}
