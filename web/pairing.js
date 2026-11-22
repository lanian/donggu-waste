// 관리자 PC의 일회용 QR에서 읽은 비밀은 암호화 키와 중계 방 주소를 유도할 때만 쓴다.
import { concat, parseRegistration, randomBytes, sha256, toHex } from './proto.js';

const text = new TextEncoder();
const base64url = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

export function readPairRegistration(scan) {
  const usb = scan.startsWith('DGUSB1.');
  if (!usb && !scan.startsWith('DGPAIR1.')) return { pc: parseRegistration(scan) };
  let data;
  try {
    const raw = scan.slice(usb ? 7 : 8);
    if (raw.length > 3000 || !/^[\w-]+$/.test(raw)) throw new Error();
    data = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(atob(raw.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0))));
    if (usb && typeof data.secret !== 'string') throw new Error();
    if (!/^[A-Za-z0-9_-]{43}$/.test(data.secret) || typeof data.registration !== 'string' || typeof data.relay !== 'string') throw new Error();
  } catch { throw new Error('간편 등록 QR을 읽을 수 없습니다. PC에서 새 QR을 열어 주세요.'); }
  let relay;
  try { relay = new URL(data.relay); } catch { throw new Error('중계 주소를 확인하세요.'); }
  if (usb) {
    const [a, b] = relay.hostname.split('.').map(Number);
    const privateIPv4 = /^\d+\.\d+\.\d+\.\d+$/.test(relay.hostname) &&
      (a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168));
    if (!privateIPv4 || relay.protocol !== 'http:' || !Number(relay.port) || relay.origin !== data.relay)
      throw new Error('USB 등록 주소가 잘못되었습니다. PC에서 새 USB 등록 QR을 열어 주세요.');
    if (!Object.hasOwn(data, 'expires_at')) throw new Error('USB 등록 QR에 만료 시각이 없습니다.');
  } else {
    const local = relay.protocol === 'http:' && relay.hostname === '127.0.0.1' && /^http:\/\/127\.0\.0\.1(?=[:/]|$)/.test(data.relay);
    if ((relay.protocol !== 'https:' && !local) || relay.username || relay.password || relay.search || relay.hash)
      throw new Error('간편 등록에는 HTTPS 중계 주소가 필요합니다.');
  }
  const pc = parseRegistration(data.registration);
  if (Object.hasOwn(data, 'expires_at') && (!Number.isSafeInteger(data.expires_at) || data.expires_at <= 0))
    throw new Error('만료 시각이 잘못되었습니다. PC에서 새 QR을 열어 주세요.');
  const expires = Math.min(Date.now() + 300000, (data.expires_at ?? Infinity) * 1000);
  if (expires <= Date.now()) throw new Error('등록 QR이 만료되었습니다. PC에서 새 QR을 열고 다시 읽으세요.');
  const session = { pc, relay: relay.href.replace(/\/+$/, ''), secret: data.secret, expires };
  if (usb) session.usb = true;
  return { pc, session };
}

export async function makePairSubmission(session, code, deviceSigningKey) {
  const room = toHex(await sha256(text.encode('dg-pair-room-v1:' + session.secret)));
  const key = await crypto.subtle.importKey('raw', await sha256(text.encode('dg-pair-data-v1:' + session.secret)), 'AES-GCM', false, ['encrypt']);
  const totpKey = randomBytes(20), nonce = randomBytes(12), encodedKey = base64url(totpKey);
  const proof = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, deviceSigningKey,
    text.encode(`DGPAIR1-proof:${room}:${code}:${encodedKey}`));
  const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce, additionalData: text.encode('DGPAIR1:' + room) }, key,
    text.encode(JSON.stringify({ code, totp_key: encodedKey, proof: base64url(new Uint8Array(proof)) })));
  const payload = base64url(concat(nonce, new Uint8Array(ciphertext)));
  const target = session.usb ? { handoff: session.relay + '/register/' + room + '#' + payload }
    : { endpoint: session.relay + '/v1/rooms/' + room + '/submit' };
  return { ...target, payload,
    signingKey: await crypto.subtle.importKey('raw', totpKey, { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']) };
}

export async function registrationNumber(key, step) {
  const counter = new DataView(new ArrayBuffer(8));
  counter.setBigUint64(0, BigInt(step));
  const mac = new Uint8Array(await crypto.subtle.sign('HMAC', key, counter));
  const offset = mac[mac.length - 1] & 15;
  const value = new DataView(mac.buffer).getUint32(offset) & 0x7fffffff;
  return String(value % 1000000).padStart(6, '0');
}

export function submitPairPacket({ endpoint, payload }, signal) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement('iframe'), channel = new MessageChannel();
    frame.hidden = true;
    frame.title = '등록 암호문 전송';
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.referrerPolicy = 'no-referrer';
    function finish(status) {
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      channel.port1.close(); channel.port2.close(); frame.remove();
      if (status >= 200 && status < 300) resolve();
      else if ([404, 409, 410].includes(status)) reject(new Error('등록 QR이 만료되었거나 이미 사용 중입니다. PC에서 새 QR을 열고 다시 읽으세요.'));
      else reject(new Error('중계에 연결하지 못했습니다. 연결을 확인한 뒤 다시 시도하세요.'));
    }
    const abort = () => finish(0), timer = setTimeout(abort, 15000);
    if (signal.aborted) { abort(); return; }
    signal.addEventListener('abort', abort, { once: true });
    channel.port1.onmessage = ({ data }) => finish(data);
    frame.onload = () => frame.contentWindow?.postMessage({ endpoint, payload }, '*', [channel.port2]);
    frame.onerror = abort;
    frame.src = new URL('./pair-submit.html', import.meta.url).href;
    document.body.append(frame);
  });
}
