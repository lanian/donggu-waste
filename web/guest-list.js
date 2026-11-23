// 조회용 QR은 발급마다 생성한 열쇠와 암호화한 배차만 운반한다.
import { concat, eq, parseFrame, toHex } from './proto.js';

const CLEAR_LIMIT = 1024 * 1024;
const MAX_FRAMES = 256;
const MAX_BODY = 700;
const FRAME_PREFIX = 'DGV1.';
const KEY_PREFIX = 'DGVIEW1:';
const keyPattern = /^DGVIEW1:([0-9a-f]{32}):([A-Za-z0-9_-]{42}[AEIMQUYcgkosw048])$/;

const positive = (n) => Number.isSafeInteger(n) && n > 0;
const text = (s) => typeof s === 'string';
const named = (s) => text(s) && s.trim().length > 0;
function fields(obj, names) {
  return !!obj && typeof obj === 'object' && !Array.isArray(obj) &&
    Object.keys(obj).length === names.length && names.every((n) => Object.hasOwn(obj, n));
}
function realDate(s) {
  if (!text(s) || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const value = new Date(s + 'T00:00:00Z');
  return Number.isFinite(value.getTime()) && value.toISOString().slice(0, 10) === s;
}
export function validateGuestList(list) {
  const bad = () => { throw new Error('조회용 배차 목록 형식이 맞지 않습니다. PC에서 새 QR을 발급해 주세요.'); };
  const work = list?.schema === 'dg.guest-work/1';
  const names = ['schema', 'work_date', 'crew', 'crew_id', 'assign_ver', 'issue_no', 'stops'];
  if (work) names.push('ticket', 'return_key');
  if (!fields(list, names) || (work ? (!named(list.ticket) || !text(list.return_key) || !/^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/.test(list.return_key)) :
      list.schema !== 'dg.guest-list/1') || !realDate(list.work_date) || !named(list.crew) || !named(list.crew_id) ||
      !positive(list.assign_ver) || !positive(list.issue_no) || !Array.isArray(list.stops)) bad();
  let previous = 0;
  for (const stop of list.stops) {
    if (!fields(stop, ['seq', 'dong', 'address', 'items']) || !positive(stop.seq) || stop.seq <= previous ||
        !text(stop.dong) || !text(stop.address) || !Array.isArray(stop.items)) bad();
    previous = stop.seq;
    for (const item of stop.items) {
      if (!fields(item, ['name', 'spec', 'qty', 'unit']) || !named(item.name) || !text(item.spec) || !text(item.unit) || !positive(item.qty)) bad();
    }
  }
  return list;
}

export function guestUpdate(current, next, today) {
  if (next.work_date < today) throw Object.assign(new Error('지난 날짜의 배차입니다. PC에서 오늘 목록을 받아 주세요.'), { code: 'GUEST_OLD' });
  if (!current || current.work_date < today) return 'save';
  if (current.work_date !== next.work_date || current.crew_id !== next.crew_id) return 'replace';
  if (next.assign_ver < current.assign_ver || next.assign_ver === current.assign_ver && next.issue_no < current.issue_no)
    throw Object.assign(new Error('이전 배차 QR입니다. 휴대폰에 저장한 목록이 더 최신입니다.'), { code: 'GUEST_OLD' });
  if (next.assign_ver === current.assign_ver && next.issue_no === current.issue_no) return 'duplicate';
  return 'save';
}

async function inflateList(bytes) {
  const reader = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate')).getReader();
  const parts = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > CLEAR_LIMIT) {
        await reader.cancel();
        throw new Error('조회용 목록의 크기 한도를 넘었습니다.');
      }
      parts.push(value);
    }
  } finally { reader.releaseLock(); }
  return validateGuestList(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(concat(...parts))));
}

export class GuestScan {
  constructor() {
    this.id = '';
    this.key = null;
    this.parts = new Map();
    this.total = 0;
  }
  clear() {
    this.id = '';
    this.key?.fill(0);
    this.key = null;
    this.parts.clear();
    this.total = 0;
  }
  add(value) {
    if (typeof value !== 'string') throw new Error('PC의 조회용 QR을 비춰 주세요.');
    if (value.startsWith(KEY_PREFIX)) {
      const match = keyPattern.exec(value);
      if (!match) throw new Error('조회용 열쇠 QR을 다시 비춰 주세요.');
      const raw = Uint8Array.from(atob(match[2].replace(/-/g, '+').replace(/_/g, '/') + '='), (c) => c.charCodeAt(0));
      if (this.id && (this.id !== match[1] || !eq(raw, this.key)))
        throw new Error('다른 열쇠 QR입니다. 지금 받던 목록을 계속 비추거나 처음부터 읽어 주세요.');
      if (!this.id) { this.id = match[1]; this.key = raw; }
      return { kind: 'key' };
    }
    if (!this.key) throw new Error('PC에 표시된 조회용 열쇠 QR부터 비춰 주세요.');
    if (!value.startsWith(FRAME_PREFIX)) throw new Error('PC의 조회용 목록 QR을 비춰 주세요.');
    if (value.length > FRAME_PREFIX.length + Math.ceil((MAX_BODY + 28) * 1.5)) throw new Error('조각 크기가 한도를 넘었습니다.');
    let f;
    try { f = parseFrame(value.slice(FRAME_PREFIX.length)); }
    catch { throw new Error('조회용 QR을 선명하게 다시 비춰 주세요.'); }
    if (toHex(f.bundleId) !== this.id) throw new Error('다른 목록의 QR입니다. 현재 열쇠에 맞는 목록을 비춰 주세요.');
    if (f.total > MAX_FRAMES || !f.body.length || f.body.length > MAX_BODY) throw new Error('QR 조각 개수나 크기가 한도를 넘었습니다.');
    if ((this.total && this.total !== f.total) || (this.parts.has(f.idx) && !eq(this.parts.get(f.idx), f.body)))
      throw new Error('목록 조각이 일치하지 않습니다. PC에서 발급한 QR을 다시 읽어 주세요.');
    this.total = f.total;
    this.parts.set(f.idx, f.body);
    return { kind: 'frame', got: this.parts.size, total: this.total };
  }
  async open() {
    if (!this.key || !this.total || this.parts.size !== this.total) throw new Error('목록 QR 조각을 모두 읽어 주세요.');
    const cipher = concat(...Array.from({ length: this.total }, (_, i) => this.parts.get(i)));
    const aad = new TextEncoder().encode(KEY_PREFIX + this.id);
    const aes = await crypto.subtle.importKey('raw', this.key, 'AES-GCM', false, ['decrypt']);
    let compressed;
    try {
      compressed = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: cipher.slice(0, 12), additionalData: aad }, aes, cipher.slice(12));
    } catch { throw new Error('목록과 열쇠가 맞지 않습니다. PC에서 두 QR을 다시 읽어 주세요.'); }
    return inflateList(compressed);
  }
}
