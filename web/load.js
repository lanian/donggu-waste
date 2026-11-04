// 품목별 적재 기준(추정치) — PC 노선 계산(pc/dgpc/routing.py)과 같은 표(data/item_handling.json)를 쓴다.
// 개인정보 없이 품목 이름·규격·수량만 본다.
import { ITEM_HANDLING } from './fees-data.js';

export const TAG_ORDER = ['EQUIP', 'TWO', 'BASE', 'UPRIGHT', 'DRAIN', 'FRAGILE'];
export const TAGS = ITEM_HANDLING.tags;  // { BASE: { label, hint }, ... }

const qtyOf = (i) => { const n = parseInt(i.qty ?? 1, 10); return Number.isFinite(n) ? Math.max(0, n) : 1; };

// 처음 맞는 규칙: 이름에 낱말이 있고, 규격 조건이 있으면 규격이나 이름에 그 말이 있을 때
export function handlingOf(item) {
  const name = String(item.name || ''), spec = String(item.spec || '');
  for (const [key, sk, m3, kg, min, tags] of ITEM_HANDLING.rules) {
    if (name.includes(key) && (!sk || spec.includes(sk) || name.includes(sk))) return { m3, kg, min, tags };
  }
  const [m3, kg, min, tags] = ITEM_HANDLING.default;
  return { m3, kg, min, tags };
}

export function tagsOf(items) {
  const got = new Set();
  for (const i of items || []) if (qtyOf(i)) for (const t of handlingOf(i).tags) got.add(t);
  if (got.has('EQUIP')) got.delete('TWO');
  return TAG_ORDER.filter((t) => got.has(t));
}

export function loadOf(items) {
  let m3 = 0, kg = 0;
  for (const i of items || []) { const x = handlingOf(i), q = qtyOf(i); m3 += x.m3 * q; kg += x.kg * q; }
  return { m3: Math.round(m3 * 10) / 10, kg: Math.round(kg), tags: tagsOf(items) };
}

// 방문 순서대로 놓인 곳들을 반입지 하차(unload_after) 기준으로 운행(회차)마다 나눈다.
// entries: [{ id, seq, items, unload_after }] → [{ no, from, to, m3, kg, base:[{seq,label}], fragile:[seq], heavy:[seq] }]
export function tripsOf(entries) {
  const trips = [];
  let cur = null;
  for (const e of entries) {
    if (!cur) { cur = { no: trips.length + 1, fromId: e.id, from: e.seq, to: e.seq, m3: 0, kg: 0, base: [], fragile: [], heavy: [], count: 0 }; trips.push(cur); }
    const l = loadOf(e.items);
    cur.to = e.seq; cur.count += 1; cur.m3 += l.m3; cur.kg += l.kg;
    if (l.tags.includes('BASE')) {
      const big = (e.items || []).filter((i) => qtyOf(i) && handlingOf(i).tags.includes('BASE')).map((i) => i.name.replace(/\s*\(.*\)$/, ''));
      cur.base.push({ seq: e.seq, label: [...new Set(big)].join('·') });
    }
    if (l.tags.includes('FRAGILE')) cur.fragile.push(e.seq);
    if (l.tags.includes('EQUIP') || l.tags.includes('TWO')) cur.heavy.push(e.seq);
    if (e.unload_after) cur = null;
  }
  for (const t of trips) { t.m3 = Math.round(t.m3 * 10) / 10; t.kg = Math.round(t.kg); }
  return trips;
}

// 운행 머리글 문구: 뒤 순서에 큰 짐이 있으면 앞쪽 바닥 자리를 남겨 두라고 알린다
export function tripAdvice(t) {
  const out = [];
  const few = (arr, f) => arr.slice(0, 3).map(f).join(', ') + (arr.length > 3 ? ` 외 ${arr.length - 3}곳` : '');
  const later = t.base.filter((b) => b.seq !== t.from);
  if (later.length) out.push(`큰 짐 ${few(later, (b) => `${b.seq}번 ${b.label}`)} → 앞쪽 바닥 자리 남겨 두기`);
  else if (t.base.length) out.push(`큰 짐 ${few(t.base, (b) => `${b.seq}번 ${b.label}`)} → 먼저 앞쪽 바닥에`);
  if (t.fragile.length) out.push(`유리·깨짐 ${few(t.fragile, (s) => s + '번')} → 맨 위나 따로`);
  return out;
}
