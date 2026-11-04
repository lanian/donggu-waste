// 동구 대형폐기물 시제품 - 공통 전송 규격 (웹 측 구현, pc/dgproto.py 와 같은 규격)
// 브라우저와 Node 22 모두에서 동작하도록 표준 Web API만 사용한다.

const subtle = globalThis.crypto.subtle;
export const VERSION = 1;
export const TYPE_DAYLIST = 1;
export const TYPE_RESULT = 3;
const FRAME_CHUNK = 0x10;
const HEADER_LEN = 52;
const enc = new TextEncoder();
const dec = new TextDecoder();

export class ProtocolError extends Error {
  constructor(code, msg = '') { super(`${code}: ${msg}`); this.code = code; }
}

// ------------------------------------------------------------ 바이트 유틸
export const concat = (...arrs) => {
  const out = new Uint8Array(arrs.reduce((n, a) => n + a.length, 0));
  let o = 0; for (const a of arrs) { out.set(a, o); o += a.length; }
  return out;
};
const u16 = (n) => new Uint8Array([n >> 8, n & 255]);
const u32 = (n) => new Uint8Array([(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]);
const rd16 = (b, o) => (b[o] << 8) | b[o + 1];
const rd32 = (b, o) => ((b[o] << 24) >>> 0) + (b[o + 1] << 16) + (b[o + 2] << 8) + b[o + 3];
export const eq = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
export const toHex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
export const uuidStr = (b) => { const h = toHex(b); return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`; };
export const uuidBytes = (s) => Uint8Array.from(s.replace(/-/g, '').match(/../g), (x) => parseInt(x, 16));
const b64url = (b) => btoa(String.fromCharCode(...b)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
export const randomBytes = (n) => globalThis.crypto.getRandomValues(new Uint8Array(n));

let CRC_TABLE;
export function crc32(b) {
  if (!CRC_TABLE) {
    CRC_TABLE = new Uint32Array(256);
    for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; CRC_TABLE[n] = c >>> 0; }
  }
  let c = 0xFFFFFFFF;
  for (const x of b) c = CRC_TABLE[(c ^ x) & 255] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

// ------------------------------------------------------------ Base45 (RFC 9285)
const B45 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:';
export function b45encode(data) {
  let s = '';
  for (let i = 0; i + 1 < data.length; i += 2) {
    let n = data[i] * 256 + data[i + 1];
    s += B45[n % 45]; n = Math.floor(n / 45); s += B45[n % 45] + B45[Math.floor(n / 45)];
  }
  if (data.length % 2) { const n = data[data.length - 1]; s += B45[n % 45] + B45[Math.floor(n / 45)]; }
  return s;
}
export function b45decode(s) {
  const v = Array.from(s, (c) => { const i = B45.indexOf(c); if (i < 0) throw new ProtocolError('QR_BAD_ENCODING'); return i; });
  const out = [];
  for (let i = 0; i < v.length; i += 3) {
    const rest = v.length - i;
    if (rest >= 3) { const n = v[i] + v[i + 1] * 45 + v[i + 2] * 2025; if (n > 0xFFFF) throw new ProtocolError('QR_BAD_ENCODING'); out.push(n >> 8, n & 255); }
    else if (rest === 2) { const n = v[i] + v[i + 1] * 45; if (n > 255) throw new ProtocolError('QR_BAD_ENCODING'); out.push(n); }
    else throw new ProtocolError('QR_BAD_ENCODING');
  }
  return new Uint8Array(out);
}

// ------------------------------------------------------------ 압축
async function streamBytes(input, stream) {
  const s = new Blob([input]).stream().pipeThrough(stream);
  return new Uint8Array(await new Response(s).arrayBuffer());
}
export const deflateRaw = (b) => streamBytes(b, new CompressionStream('deflate-raw'));
export const inflateRaw = (b) => streamBytes(b, new DecompressionStream('deflate-raw'));

// ------------------------------------------------------------ 키
const ECDH = { name: 'ECDH', namedCurve: 'P-256' };
const ECDSA = { name: 'ECDSA', namedCurve: 'P-256' };
export async function generateDeviceKeys() {
  // 개인키는 추출 불가(extractable=false). 공개키만 내보낼 수 있다.
  const kex = await subtle.generateKey(ECDH, false, ['deriveBits']);
  const sign = await subtle.generateKey(ECDSA, false, ['sign', 'verify']);
  return { kex, sign };
}
export const exportRaw = async (pub) => new Uint8Array(await subtle.exportKey('raw', pub));
export const importEcdhPub = (raw) => subtle.importKey('raw', raw, ECDH, false, []);
export const importEcdsaPub = (raw) => subtle.importKey('raw', raw, ECDSA, false, ['verify']);
export const sha256 = async (b) => new Uint8Array(await subtle.digest('SHA-256', b));
export const keyId = async (signPubRaw) => (await sha256(signPubRaw)).slice(0, 8);

// 9자리 지문: PC 서명·키 합의 공개키, 기기 번호·두 공개키를 모두 덮는다(pc/dgproto.py fingerprint와 같음)
export async function fingerprint(pcSign, pcKex, deviceId, devKex, devSign) {
  const h = await sha256(concat(new TextEncoder().encode('DGFP2'), pcSign, pcKex, deviceId, devKex, devSign));
  const n = h[0] * 2 ** 32 + ((h[1] << 24) >>> 0) + (h[2] << 16) + (h[3] << 8) + h[4];
  return String(n % 1e9).padStart(9, '0');
}

export async function deviceCode(deviceId, kexRaw, signRaw) {
  const data = concat(deviceId, kexRaw, signRaw);
  return 'DGD1.' + b64url(concat(data, (await sha256(data)).slice(0, 4)));
}

export function parseRegistration(text) {
  const raw = b45decode(text.trim());
  if (raw.length !== 4 + 65 + 65 || raw[0] !== 0x44 || raw[1] !== 0x47 || raw[3] !== 0x09) throw new ProtocolError('QR_NOT_REGISTRATION');
  if (raw[2] !== VERSION) throw new ProtocolError('BAD_VERSION');
  return { pcSignPub: raw.slice(4, 69), pcKexPub: raw.slice(69, 134) };
}

async function derive(sharedBits, bundleId, btype) {
  const ikm = await subtle.importKey('raw', sharedBits, 'HKDF', false, ['deriveBits']);
  const okm = new Uint8Array(await subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: bundleId, info: concat(enc.encode('DG-v1'), new Uint8Array([btype])) }, ikm, 512));
  const aes = await subtle.importKey('raw', okm.slice(0, 32), 'AES-GCM', false, ['encrypt', 'decrypt']);
  return { aes, ack: okm.slice(32) };
}

export async function receiptCode(ackKey, bundleId) {
  const k = await subtle.importKey('raw', ackKey, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = new Uint8Array(await subtle.sign('HMAC', k, concat(enc.encode('ack'), bundleId)));
  const alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
  let n = 0n; for (const x of mac.slice(0, 8)) n = (n << 8n) | BigInt(x);
  let s = ''; for (let i = 0; i < 8; i++) { s += alphabet[Number(n % 31n)]; n /= 31n; }
  return s;
}

// ------------------------------------------------------------ 봉투
export function packHeader(h) {
  return concat(enc.encode('DG'), new Uint8Array([VERSION, h.btype]), h.bundleId, h.deviceId, u32(h.assignVer), u32(h.dataVer), h.signerKeyId);
}
export function unpackHeader(b) {
  if (b.length < HEADER_LEN || b[0] !== 0x44 || b[1] !== 0x47) throw new ProtocolError('BAD_FORMAT', '헤더');
  if (b[2] !== VERSION) throw new ProtocolError('BAD_VERSION');
  return { btype: b[3], bundleId: b.slice(4, 20), deviceId: b.slice(20, 36), assignVer: rd32(b, 36), dataVer: rd32(b, 40), signerKeyId: b.slice(44, 52) };
}

export async function seal(payload, header, recipientKexPub, signerPriv) {
  const plain = await deflateRaw(enc.encode(JSON.stringify(payload)));
  const eph = await subtle.generateKey(ECDH, false, ['deriveBits']);
  const shared = new Uint8Array(await subtle.deriveBits({ name: 'ECDH', public: recipientKexPub }, eph.privateKey, 256));
  const { aes, ack } = await derive(shared, header.bundleId, header.btype);
  const nonce = randomBytes(12);
  const head = packHeader(header);
  const ct = new Uint8Array(await subtle.encrypt({ name: 'AES-GCM', iv: nonce, additionalData: head }, aes, plain));
  const body = concat(head, await exportRaw(eph.publicKey), nonce, u32(ct.length), ct);
  const sig = new Uint8Array(await subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, signerPriv, body));
  return { bytes: concat(body, sig), receipt: await receiptCode(ack, header.bundleId) };
}

export async function openEnvelope(data, signerPub, kexPriv, expectDeviceId) {
  const header = unpackHeader(data);
  if (expectDeviceId && !eq(header.deviceId, expectDeviceId)) throw new ProtocolError('WRONG_DEVICE');
  if (data.length < HEADER_LEN + 65 + 12 + 4 + 16 + 64) throw new ProtocolError('BAD_FORMAT', '길이');
  const body = data.slice(0, -64), sig = data.slice(-64);
  const ok = await subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, signerPub, sig, body);
  if (!ok) throw new ProtocolError('AUTH_FAIL', '서명 불일치');
  let o = HEADER_LEN;
  const ephRaw = body.slice(o, o + 65); o += 65;
  const nonce = body.slice(o, o + 12); o += 12;
  const ctLen = rd32(body, o); o += 4;
  const ct = body.slice(o);
  if (ct.length !== ctLen) throw new ProtocolError('BAD_FORMAT', '암호문 길이');
  const shared = new Uint8Array(await subtle.deriveBits({ name: 'ECDH', public: await importEcdhPub(ephRaw) }, kexPriv, 256));
  const { aes, ack } = await derive(shared, header.bundleId, header.btype);
  let plain;
  try { plain = new Uint8Array(await subtle.decrypt({ name: 'AES-GCM', iv: nonce, additionalData: body.slice(0, HEADER_LEN) }, aes, ct)); }
  catch { throw new ProtocolError('AUTH_FAIL', '인증 태그'); }
  const payload = JSON.parse(dec.decode(await inflateRaw(plain)));
  return { header, payload, receipt: await receiptCode(ack, header.bundleId) };
}

// ------------------------------------------------------------ 조각
export function splitFrames(envelope, bundleId, size = 700) {
  const total = Math.ceil(envelope.length / size), out = [];
  for (let i = 0; i < total; i++) {
    const body = envelope.slice(i * size, (i + 1) * size);
    out.push(b45encode(concat(enc.encode('DG'), new Uint8Array([VERSION, FRAME_CHUNK]), bundleId, u16(i), u16(total), u32(crc32(body)), body)));
  }
  return out;
}

export function parseFrame(text) {
  const raw = b45decode(text.trim());
  if (raw.length < 28 || raw[0] !== 0x44 || raw[1] !== 0x47 || raw[3] !== FRAME_CHUNK) throw new ProtocolError('QR_NOT_OURS');
  if (raw[2] !== VERSION) throw new ProtocolError('BAD_VERSION');
  const bundleId = raw.slice(4, 20), idx = rd16(raw, 20), total = rd16(raw, 22), crc = rd32(raw, 24), body = raw.slice(28);
  if (crc32(body) !== crc || idx >= total || total === 0) throw new ProtocolError('QR_CORRUPT');
  return { bundleId, idx, total, body };
}

// 조각 수집기: 중복 무시, 다른 묶음은 따로 보관, 전부 모였을 때만 조합한다.
export class Collector {
  constructor(state) { this.bundles = state || {}; }
  add(text) {
    const f = parseFrame(text);
    const key = toHex(f.bundleId);
    const b = this.bundles[key] ||= { total: f.total, parts: {}, firstSeen: Date.now() };
    if (b.total !== f.total) throw new ProtocolError('QR_CORRUPT', '전체 개수 불일치');
    const dup = b.parts[f.idx] !== undefined;
    if (!dup) b.parts[f.idx] = f.body;
    return { key, got: Object.keys(b.parts).length, total: b.total, dup };
  }
  missing(key) { const b = this.bundles[key]; return [...Array(b.total).keys()].filter((i) => b.parts[i] === undefined); }
  complete(key) { return this.missing(key).length === 0; }
  assemble(key) {
    if (!this.complete(key)) throw new ProtocolError('QR_INCOMPLETE');
    const b = this.bundles[key];
    return concat(...[...Array(b.total).keys()].map((i) => b.parts[i]));
  }
  drop(key) { delete this.bundles[key]; }
}

// ------------------------------------------------------------ 목록 압축 형식 v2 풀기 (pc/dgproto.py expand_daylist 와 같은 규칙)
// v0.10: 지점의 선택 항목 "d" = 행정동 이름. 없으면 ''. 형식 번호는 그대로 v2(이전 목록도 그대로 풀림).
const fromSid = (s) => {
  const b = Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '=='), (c) => c.charCodeAt(0));
  return uuidStr(b);
};
// v0.13: 목록 맨 위 선택 키 "rel" = 결과 반영 정보 {"seq": 정수, "ph": [사진 ID…]} (pc/dgproto.py norm_rel 과 같은 검사).
// 서명된 봉투 안에 있으므로 PC가 보낸 값이다. 형식이 틀리면 없는 것으로 본다(손으로 코드 입력하던 이전 동작).
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function normRel(rel) {
  if (!rel || typeof rel !== 'object' || Array.isArray(rel)) return null;
  if (!Number.isInteger(rel.seq) || rel.seq < 0) return null;
  const ph = rel.ph == null ? [] : rel.ph;
  if (!Array.isArray(ph) || !ph.every((x) => typeof x === 'string' && UUID_RE.test(x))) return null;
  return { seq: rel.seq, ph: [...new Set(ph.map((x) => x.toLowerCase()))] };
}
export function expandDaylist(p, feeTable) {
  if (!p || p.schema !== 'dg.daylist/2') return p;
  const rows = Object.fromEntries((feeTable ? feeTable.rows : []).map((r) => [r.id, r]));
  const stops = p.s.map((c) => ({
    seq: c.q, request_id: fromSid(c.i), receipt_no: c.r, req_ver: c.v, name: c.n, phone: c.p, address: c.a, dong: c.d || '',
    spot_note: c.o || '', memo: c.m || '', lat: c.y ? c.y[0] : null, lon: c.y ? c.y[1] : null, eta: c.e || '',
    appt: c.t || null, revisit: !!c.rv, assign_ver: p.assign_ver,
    // v0.11: 선택 키 "rd" 감면율(%), "lv" 부과번호. 없으면 감면 없음·부과번호 없음(이전 목록 그대로).
    levy_no: c.lv || '', reduction: c.rd ? { rate: Number(c.rd) } : null,
    // v0.10.1: "u"의 선택 5·6번째 값 = 반입지 위도·경도(길안내용). 없으면 null(이전 목록 그대로).
    unload_after: c.u ? { eta: c.u[0], label: c.u[1], minutes: c.u[2], volume_m3: c.u[3], lat: c.u[4] ?? null, lon: c.u[5] ?? null } : null,
    items: c.it.map(([sid, rid, qty, x = {}]) => {
      const row = rows[rid || ''];
      const status = x.ps === 'C' ? 'CONFIRMED' : x.ps === 'P' ? 'PENDING' : row && row.status === 'CONFIRMED' ? 'CONFIRMED' : 'PENDING';
      const price = x.pp ?? (row ? row.price : null);
      const kind = x.k === 'F' ? 'FIELD' : 'RECEIVED';
      return {
        line_id: fromSid(sid), line_kind: kind, fee_rate_id: rid, name: x.n ?? (row ? row.name : ''), spec: x.s ?? (row ? row.spec : ''),
        unit: x.u ?? (row ? row.unit : 'EA'), unit_label: x.ul ?? (row ? row.unit_label : '개'), qty, prior_collected: x.c || 0, unit_price: price,
        // qr: 접수 수량(현장에서 확인 수량이 바뀐 재방문 목록은 qty가 확인 수량이라 접수 당시 금액은 접수 수량으로 셈)
        qty_received: kind === 'FIELD' ? 0 : (x.qr ?? qty),
        amount: kind === 'FIELD' ? 0 : status === 'CONFIRMED' && price != null ? price * (x.qr ?? qty) : null, price_status: status,
      };
    }),
  }));
  const fee_table = { version_id: p.fee };
  if (p.fb) Object.assign(fee_table, { base_version: p.fb, overrides: p.fo || {}, effective_from: p.fe || null });
  const rel = normRel(p.rel);
  return { schema: 'dg.daylist/1', work_date: p.work_date, crew: p.crew, assign_ver: p.assign_ver, fee_table, stops, ...(rel ? { rel } : {}) };
}
