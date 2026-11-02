// 단독 모드 백업 파일: 백업 비밀번호(PBKDF2-SHA256 60만 회)로 만든 키로 AES-256-GCM 암호화.
// 형식: 'DGB1'(4) | salt(16) | iv(12) | 암호문+태그   평문 = deflate-raw(JSON)
import { deflateRaw, inflateRaw, concat } from './proto.js';

const subtle = globalThis.crypto.subtle;
const MAGIC = new TextEncoder().encode('DGB1');
const ITER = 600000;

async function keyFrom(password, salt) {
  const base = await subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  return subtle.deriveKey({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations: ITER }, base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
}

export async function makeBackup(obj, password) {
  const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
  const plain = await deflateRaw(new TextEncoder().encode(JSON.stringify(obj)));
  const ct = new Uint8Array(await subtle.encrypt({ name: 'AES-GCM', iv, additionalData: MAGIC }, await keyFrom(password, salt), plain));
  return concat(MAGIC, salt, iv, ct);
}

export async function openBackup(bytes, password) {
  if (bytes.length < 48 || !MAGIC.every((b, i) => bytes[i] === b)) throw Object.assign(new Error('백업 파일이 아닙니다'), { code: 'NOT_BACKUP' });
  const salt = bytes.slice(4, 20), iv = bytes.slice(20, 32), ct = bytes.slice(32);
  let plain;
  try { plain = new Uint8Array(await subtle.decrypt({ name: 'AES-GCM', iv, additionalData: MAGIC }, await keyFrom(password, salt), ct)); }
  catch { throw Object.assign(new Error('비밀번호가 틀렸거나 파일이 손상되었습니다'), { code: 'BAD_BACKUP_PASSWORD' }); }
  const obj = JSON.parse(new TextDecoder().decode(await inflateRaw(plain)));
  if (obj.format !== 'dg.backup/1') throw Object.assign(new Error('지원하지 않는 백업 형식'), { code: 'NOT_BACKUP' });
  return obj;
}

export const b64 = (u8) => { let s = ''; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode(...u8.subarray(i, i + 0x8000)); return btoa(s); };
export const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
