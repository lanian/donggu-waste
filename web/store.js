// 휴대폰 안 저장소: IndexedDB + 레코드별 AES-GCM 암호화.
// 데이터 키는 PIN에서 만든 키(PBKDF2-SHA256)로 감싸 두고, 잠금 해제 때만 메모리에 푼다.
// 서버로 보내는 코드는 없다.

const subtle = crypto.subtle;
const DB_NAME = 'dg-proto';
const PBKDF2_ITER = 600000;
let dbp = null;
let dataKey = null; // 잠금 해제 동안만 존재
// 디스크 저장 실패분은 암호문만 잠시 보관한다. 잠금 때 키를 버려도 PIN 해제 뒤 다시 저장할 수 있다.
const pendingSecure = new Map(), secureWrites = new Map();
let storageEpoch = 0, secureWriteSeq = 0;
function resetRecovery() { storageEpoch++; pendingSecure.clear(); secureWrites.clear(); }
export const hasPendingSecure = (k) => pendingSecure.has(k);

function idb() {
  if (!dbp) dbp = new Promise((res, rej) => {
    const r = indexedDB.open(DB_NAME, 1);
    r.onupgradeneeded = () => r.result.createObjectStore('kv');
    r.onsuccess = () => {
      const db = r.result;
      // 다른 창에서 저장소를 지우거나 새 버전이 열리면 이 창의 연결을 바로 놓아 준다(안 그러면 그쪽이 기다림)
      db.onversionchange = () => { try { db.close(); } catch {} dbp = null; };
      res(db);
    };
    r.onerror = () => { dbp = null; rej(r.error); };
  });
  return dbp;
}
// 저장소가 열릴 때까지(다른 창이 붙잡고 있으면 놓을 때까지) 기다리는 약속. 화면은 이것을 보고 '기다리는 중'을 보여 준다.
export const whenReady = () => idb();
async function tx(mode, fn, epoch) {
  const db = await idb();
  if (epoch !== undefined && epoch !== storageEpoch) throw Object.assign(new Error('저장소가 바뀌었습니다'), { code: 'STORE_CHANGED' });
  return new Promise((res, rej) => {
    const t = db.transaction('kv', mode);
    const out = fn(t.objectStore('kv'));
    t.oncomplete = () => res(out && 'result' in out ? out.result : undefined);
    // 요청 오류(쿼터 초과 등)가 올라올 때 t.error 는 아직 null 이다(중단 뒤에 채워짐) → 요청의 오류를 먼저 본다.
    // null 로 거절하면 화면이 저장 공간 부족을 알아보지 못해 사진을 대기열에서 버린다.
    t.onerror = (ev) => rej((ev && ev.target && ev.target.error) || t.error || new Error('저장 실패'));
    t.onabort = () => rej(t.error || new Error('저장 실패'));
  });
}
export const getRaw = (k) => tx('readonly', (s) => s.get(k));
export const putRaw = (k, v) => tx('readwrite', (s) => s.put(v, k));
export const delRaw = (k) => tx('readwrite', (s) => s.delete(k));
export const keysRaw = () => tx('readonly', (s) => s.getAllKeys());

// 자동 잠금은 이 기기의 비밀이 아닌 설정이다. 업무 상태나 PIN/데이터 키는 바꾸지 않는다.
export const readAutoLock = async () => (await getRaw('auto-lock'))?.enabled !== false;
export async function saveAutoLock(enabled) {
  if (typeof enabled !== 'boolean') throw new Error('자동 잠금 설정값이 올바르지 않습니다.');
  if (!dataKey) throw Object.assign(new Error('잠금을 해제한 뒤 설정해 주세요.'), { code: 'LOCKED' });
  const epoch = storageEpoch, key = dataKey;
  await tx('readwrite', (s) => {
    if (dataKey !== key) throw Object.assign(new Error('잠금 상태가 바뀌었습니다.'), { code: 'LOCKED' });
    return s.put({ enabled }, 'auto-lock');
  }, epoch);
  if (epoch !== storageEpoch) throw Object.assign(new Error('저장소가 바뀌었습니다'), { code: 'STORE_CHANGED' });
}

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
  resetRecovery();  // 새 데이터 키로 바뀌면 이전 키의 미저장 암호문을 복원하지 않는다.
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
export async function enrollBio(pin, { credId, prfSalt, prfOut, transports = null }) {
  const L = await getRaw('lock');
  if (Date.now() < (L.until || 0)) throw Object.assign(new Error('잠시 후 다시 시도하세요'), { code: 'LOCKED_OUT', until: L.until });
  let k;
  try {
    // 감싸기 위해서만 잠깐 내보낼 수 있는 키로 푼다. 이 키는 저장하지 않고 이 함수가 끝나면 버린다.
    k = await subtle.unwrapKey('raw', L.wrapped, await pinKey(pin, L.salt), { name: 'AES-GCM', iv: L.iv }, { name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
  } catch { throw await countFail(L); }
  const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
  const wrapped = new Uint8Array(await subtle.wrapKey('raw', k, await prfKey(prfOut, salt), { name: 'AES-GCM', iv }));
  await putRaw('bio', { credId, prfSalt, salt, iv, wrapped, at: new Date().toISOString(), ...(transports && transports.length ? { transports } : {}) });
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
function sameRecord(a, b) {
  if (!a || !b) return a == null && b == null;
  return ['iv', 'ct'].every((k) => {
    const x = new Uint8Array(a[k]), y = new Uint8Array(b[k]);
    return x.length === y.length && x.every((v, i) => v === y[i]);
  });
}
async function writeSecureRecord(k, record, recovery, observed, epoch) {
  const db = await idb();
  if (epoch !== storageEpoch) throw Object.assign(new Error('저장소가 바뀌었습니다'), { code: 'STORE_CHANGED' });
  return new Promise((resolve, reject) => {
    const t = db.transaction('kv', 'readwrite'), store = t.objectStore('kv');
    let failure = null;
    t.oncomplete = () => resolve();
    t.onerror = (ev) => { failure ||= (ev && ev.target && ev.target.error) || t.error; };
    t.onabort = () => reject(failure || t.error || new Error('저장 실패'));
    const read = store.get('s:' + k);
    read.onsuccess = () => {
      observed.base = read.result; observed.known = true;
      // 복구 기준 대조와 쓰기를 한 트랜잭션에서 한다. 그 사이 다른 창의 쓰기가 끼어들 수 없다.
      if (recovery && (!recovery.known || !sameRecord(read.result, recovery.base))) {
        failure = Object.assign(new Error('다른 창에서 기록이 바뀌었습니다. 이 창의 기록을 먼저 보내거나 백업하세요.'), { code: 'STORE_CONFLICT' });
        t.abort(); return;
      }
      try { store.put(record, 's:' + k); } catch (e) { failure = e; t.abort(); }
    };
  });
}
export async function putSecure(k, obj) {
  const epoch = storageEpoch, seq = ++secureWriteSeq;
  const recovery = pendingSecure.get(k), observed = { known: false, base: undefined };
  secureWrites.set(k, seq);
  const record = await encryptBytes(enc.encode(JSON.stringify(obj)), k);
  // 암호화하는 동안 자료를 지우거나 PIN을 새로 만들었다면 옛 자료를 다시 쓰지 않는다.
  if (epoch !== storageEpoch) throw Object.assign(new Error('저장소가 바뀌었습니다'), { code: 'STORE_CHANGED' });
  try {
    await writeSecureRecord(k, record, recovery, observed, epoch);
    if (epoch === storageEpoch && secureWrites.get(k) === seq) pendingSecure.delete(k);
  } catch (e) {
    if (epoch === storageEpoch && secureWrites.get(k) === seq)
      pendingSecure.set(k, { record, base: recovery ? recovery.base : observed.base, known: recovery ? recovery.known : observed.known });
    throw e;  // 화면의 저장 실패 안내는 실제 디스크 저장 성공 때까지 유지한다.
  }
}
export async function getSecure(k) {
  const r = pendingSecure.get(k)?.record || await getRaw('s:' + k);
  return r ? JSON.parse(dec.decode(await decryptBytes(r, k))) : null;
}
export async function putSecureBytes(k, bytes) { await putRaw('b:' + k, await encryptBytes(bytes, k)); }
export async function getSecureBytes(k) { const r = await getRaw('b:' + k); return r ? decryptBytes(r, k) : null; }
export const delSecureBytes = (k) => delRaw('b:' + k);

export async function wipeAll() {
  // 저장소 파일을 지우지(deleteDatabase) 않고 안의 기록을 모두 비운다. 파일 삭제는 다른 창이 연결을 놓을 때까지
  // 끝나지 않아 다시 열 때 '불러오는 중'에 멈출 수 있었다. 비우기는 다른 창이 열려 있어도 바로 끝난다.
  resetRecovery(); dataKey = null;
  await tx('readwrite', (st) => st.clear());
  try { const bc = new BroadcastChannel('dg-store'); bc.postMessage('wiped'); bc.close(); } catch {}
}
