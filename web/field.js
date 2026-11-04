// 현장 보조 규칙 (순수 함수: 화면과 분리해 Node에서도 테스트한다)
// - 하루 중 새 목록을 받았을 때 바뀐 곳 찾기
// - 문자 안내 문구(다른 주민 정보가 들어가지 않게 검사)
// - 보관 기간이 지난 개인정보 지우기
// - 미전송 기록 나이

// ------------------------------------------------------------ 하루 중 목록 갱신
export const DIFF_LABEL = { NEW: '새로 추가', ORDER: '순서·시간 변경', INFO: '정보 변경' };

const PII = ['name', 'phone', 'address', 'spot_note', 'memo'];
const itemKey = (s) => (s.items || []).map((i) => [i.line_id, i.qty, i.fee_rate_id || null, i.unit_price ?? null, i.price_status || '', i.prior_collected || 0]);
// 개인정보를 지운 지점은 이름·연락처·주소가 비어 있으므로, 그 칸은 비교하지 않는다(다시 받은 것은 변경이 아님)
const infoKey = (s, skipPii) => JSON.stringify([skipPii ? null : [...PII.map((k) => s[k] || ''), s.lat ?? null, s.lon ?? null],
  s.dong || '', s.appt || null, !!s.revisit, itemKey(s), ...(s.reduction && s.reduction.rate ? [s.reduction.rate] : []), ...(s.reduction && s.reduction.round_unit > 1 ? [s.reduction.round_unit] : [])]);

// 가장 긴 증가 부분 수열에 들지 않는 위치 = 순서가 바뀐 지점 (하나 끼워 넣어도 나머지는 "변경"이 아님)
function movedIndexes(seq) {
  const n = seq.length, len = new Array(n).fill(1), prev = new Array(n).fill(-1);
  let best = -1;
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < i; j++) if (seq[j] < seq[i] && len[j] + 1 > len[i]) { len[i] = len[j] + 1; prev[i] = j; }
    if (best < 0 || len[i] > len[best]) best = i;
  }
  const keep = new Set();
  for (let i = best; i >= 0; i = prev[i]) keep.add(i);
  return new Set([...Array(n).keys()].filter((i) => !keep.has(i)));
}

/** 같은 날짜의 이전 목록과 새 목록을 비교한다.
 *  돌려주는 값: { changes: {request_id: ['NEW'|'ORDER'|'INFO', ...]}, added, changed, removed, summary } */
export function diffDaylist(oldList, newList) {
  const out = { changes: {}, added: 0, changed: 0, removed: 0, summary: '' };
  if (!oldList || !newList || oldList.work_date !== newList.work_date) return out;
  const prev = new Map(oldList.stops.filter((s) => !s.removed).map((s) => [s.request_id, s]));
  const prevOrder = new Map(oldList.stops.filter((s) => !s.removed).slice().sort((a, b) => a.seq - b.seq).map((s, i) => [s.request_id, i]));
  const live = newList.stops.filter((s) => !s.removed).slice().sort((a, b) => a.seq - b.seq);
  const common = live.filter((s) => prev.has(s.request_id));
  const moved = movedIndexes(common.map((s) => prevOrder.get(s.request_id)));
  const movedIds = new Set(common.filter((_, i) => moved.has(i)).map((s) => s.request_id));
  for (const s of live) {
    const o = prev.get(s.request_id);
    if (!o) { out.changes[s.request_id] = ['NEW']; out.added++; continue; }
    const f = [];
    if (movedIds.has(s.request_id) || (o.eta || '') !== (s.eta || '')) f.push('ORDER');
    if (infoKey(o, !!o.pii_purged) !== infoKey(s, !!o.pii_purged)) f.push('INFO');
    if (f.length) { out.changes[s.request_id] = f; out.changed++; }
  }
  const ids = new Set(live.map((s) => s.request_id));
  for (const id of prev.keys()) if (!ids.has(id)) out.removed++;
  out.summary = [out.added && `새 ${out.added}곳`, out.changed && `변경 ${out.changed}곳`, out.removed && `빠짐 ${out.removed}곳`].filter(Boolean).join(', ');
  return out;
}

// ------------------------------------------------------------ 문자 안내
// 문구에는 그 주민 자신의 접수번호·도착 시각·재방문 일정만 넣을 수 있다. 이름·주소는 넣지 않는다.
export const SMS_VARS = { '{시각}': '도착 예정 시각', '{접수번호}': '접수번호', '{재방문}': '재방문 약속 날짜·시간' };
export const SMS_DEFAULTS = [
  { id: 'SOON', label: '곧 도착', text: '[동구 대형폐기물] 수거팀입니다. {시각}쯤 도착 예정입니다. 배출 장소에 내어 놓아 주세요.' },
  { id: 'ABSENT', label: '부재 안내', text: '[동구 대형폐기물] 수거팀이 방문했으나 배출된 물건을 찾지 못했습니다(접수번호 {접수번호}). 이 번호로 연락 주시면 다시 안내해 드립니다.' },
  { id: 'REVISIT', label: '재방문 안내', text: '[동구 대형폐기물] 재방문 일정을 알려 드립니다: {재방문}. 접수번호 {접수번호}.' },
];
const digitsOf = (s) => (s || '').replace(/\D/g, '');
const squash = (s) => (s || '').replace(/\s+/g, '');

/** 문구 검사. 문제가 있으면 이유(한국어)를, 없으면 ''를 돌려준다.
 *  stops: 이 휴대폰에 있는 모든 지점 — 어떤 주민의 이름·연락처·주소도 문구에 들어 있으면 거부한다. */
export function checkSmsTemplate(text, stops = []) {
  const t = (text || '').trim();
  if (!t) return '문구를 적어 주세요.';
  if (t.length > 300) return '문구는 300자 이내로 적어 주세요.';
  const unknown = (t.match(/\{[^}]*\}/g) || []).filter((v) => !(v in SMS_VARS));
  if (unknown.length) return `쓸 수 없는 자리표시: ${unknown.join(', ')} (쓸 수 있는 것: ${Object.keys(SMS_VARS).join(' ')})`;
  const d = digitsOf(t), sq = squash(t);
  for (const s of stops) {
    if (s.phone && digitsOf(s.phone).length >= 8 && d.includes(digitsOf(s.phone))) return '주민 연락처가 들어 있습니다. 문구에는 주민 정보를 넣지 마세요.';
    if (s.name && squash(s.name).length >= 2 && sq.includes(squash(s.name))) return '주민 이름이 들어 있습니다. 문구에는 주민 정보를 넣지 마세요.';
    const core = squash(s.address).split(/[,(]/)[0]; // 동·호수 앞까지(도로명·번지)
    if (core.length >= 6 && sq.includes(core.slice(0, 12))) return '주민 주소가 들어 있습니다. 문구에는 주민 정보를 넣지 마세요.';
  }
  return '';
}

/** 그 지점 자신의 값으로 자리표시를 채운다. */
export function fillSms(text, stop, appt) {
  const when = appt && appt.date ? `${appt.date}${appt.start ? ' ' + appt.start : ''}${appt.end ? '~' + appt.end : ''}` : '(일정 미정)';
  return (text || '').replace(/\{시각\}/g, stop.eta || '곧').replace(/\{접수번호\}/g, stop.receipt_no || '').replace(/\{재방문\}/g, when);
}

/** sms: 링크. 번호와 본문만 넣는다. */
export const smsHref = (phone, body) => `sms:${digitsOf(phone)}?body=${encodeURIComponent(body)}`;

// ------------------------------------------------------------ 개인정보 지우기
/** 지점(또는 단독 방식 접수)의 이름·연락처·주소·상세 위치·메모·좌표를 지운다. 접수번호·품목·기록은 남긴다. */
export function purgeStopPII(s, at = new Date().toISOString()) {
  if (s.pii_purged) return false;
  for (const k of PII) s[k] = '';
  s.lat = null; s.lon = null;
  if (Array.isArray(s.versions)) s.versions = s.versions.map((v) => ({ req_ver: v.req_ver, at: v.at, reason: v.reason, snapshot: null }));
  s.pii_purged = at;
  return true;
}
/** 지운 지점의 현장 기록에서 개인정보가 들어갈 수 있는 칸(메모 글, 좌표)을 지운다. 나머지 기록(수량·결과)은 남긴다. */
export function purgeEventPII(e) {
  const b = e.body || {};
  let hit = false;
  for (const k of ['text', 'lat', 'lon', 'acc', 'geo']) if (k in b) { delete b[k]; hit = true; }
  // 현장 접수·정정 요청(PC 연결 방식)에는 이름·연락처·주소가 들어 있다: 칸 이름만 남기고 값은 지운다
  if (e.kind === 'FIELD_INTAKE') for (const k of [...PII, 'dong']) if (k in b) { delete b[k]; hit = true; }
  if (e.kind === 'CORRECTION' && b.fields && typeof b.fields === 'object' && !Array.isArray(b.fields)) { b.fields = Object.keys(b.fields); hit = true; }
  if (e.kind === 'CORRECTION' && b.reason) { delete b.reason; hit = true; }  // 사유 글에 새 번호·이름을 적는 일이 있다
  if (hit) b.pii_purged = true;
  return hit;
}

// ------------------------------------------------------------ 미전송 기록
/** 보내지 않은 기록 수와 가장 오래된 기록의 나이(시간). */
export function unsentInfo(events, releasedSeq, now = Date.now()) {
  const list = events.filter((e) => e.seq > releasedSeq);
  const oldest = list.reduce((m, e) => Math.min(m, Date.parse(e.ts) || now), now);
  return { count: list.length, oldestTs: list.length ? new Date(oldest).toISOString() : null, ageH: list.length ? (now - oldest) / 3600000 : 0 };
}

/** 단독 방식 보관 기간: 처리가 끝난(isDone) 지 days일이 지난 접수. doneAt = 그 접수의 마지막 기록·수정·취소 시각.
 *  돌려주는 값: [{ r, doneAt }] — 실제로 지울지는 화면에서 백업 여부를 보고 사람이 정한다. */
export function retentionCandidates(requests, events, days, isDone, now = Date.now()) {
  const last = {};
  for (const e of events) if (e.request_id) last[e.request_id] = Math.max(last[e.request_id] || 0, Date.parse(e.ts) || 0);
  const out = [];
  for (const r of requests) {
    if (r.pii_purged || !isDone(r)) continue;
    const doneAt = Math.max(last[r.request_id] || 0, Date.parse(r.cancelled_at || '') || 0, Date.parse(r.updated_at || '') || 0, Date.parse(r.received_at || '') || 0);
    if (now - doneAt > days * 864e5) out.push({ r, doneAt });
  }
  return out;
}

// ------------------------------------------------------------ PC와 주고받는 확인 코드 (v0.13 이름 통일)
// 수신 코드(휴대폰 → PC, 목록을 받았다는 확인), 결과 반영 코드(PC → 휴대폰, 이전 이름 '보관 종료 확인 코드'),
// 사진 회수 코드(PC → 휴대폰). 코드 자체(8자)는 PC가 만들며 바뀌지 않았다. 앞 글자(R·D·P)는 화면 표시·입력 확인용으로만 쓴다.
export const CODE_KINDS = {
  RECEIPT: { prefix: 'R', label: '수신 코드', dir: '휴대폰 → PC' },
  RESULT: { prefix: 'D', label: '결과 반영 코드', dir: 'PC → 휴대폰' },
  PHOTO: { prefix: 'P', label: '사진 회수 코드', dir: 'PC → 휴대폰' },
};
const CODE_ALPHABET = /^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{8}$/;
/** 입력한 코드를 정리한다: 공백·하이픈 제거, 대문자, 앞 글자(R·D·P + '-')가 있으면 떼어 prefix로 돌려준다. */
export function parseCode(input) {
  let s = String(input || '').toUpperCase().replace(/\s+/g, '');
  let prefix = '';
  const m = s.match(/^([RDP])-(.*)$/);
  if (m) { prefix = m[1]; s = m[2]; }
  s = s.replace(/-/g, '');
  if (!prefix && s.length === 9 && /^[RDP]/.test(s) && CODE_ALPHABET.test(s.slice(1))) { prefix = s[0]; s = s.slice(1); }
  return { prefix, code: s };
}
/** 코드 입력칸 확인. field: 'RESULT' | 'PHOTO'. known: { RESULT: [코드…], PHOTO: [코드…], RECEIPT: [코드…] } (이 휴대폰이 아는 코드)
 *  돌려주는 값: { code, kind } (맞는 칸) 또는 { error } (다른 칸 코드·형식 오류·모르는 코드). */
export function checkCode(input, field, known = {}) {
  const { prefix, code } = parseCode(input);
  if (!code) return { error: `${CODE_KINDS[field].label}를 넣어 주세요.` };
  const kindOfPrefix = Object.keys(CODE_KINDS).find((k) => CODE_KINDS[k].prefix === prefix);
  if (prefix && kindOfPrefix !== field) return { error: wrongField(kindOfPrefix, field) };
  if ((known[field] || []).includes(code)) return { code, kind: field };
  const other = Object.keys(CODE_KINDS).find((k) => k !== field && (known[k] || []).includes(code));
  if (other) return { error: wrongField(other, field) };
  if (!CODE_ALPHABET.test(code)) return { error: `${CODE_KINDS[field].label}는 8자리입니다(숫자 0·1, 글자 I·L·O는 쓰지 않음). 다시 확인해 주세요.` };
  return { error: `맞는 ${CODE_KINDS[field].label}가 없습니다. PC 담당자에게 코드를 다시 확인해 주세요.` };
}
function wrongField(kind, field) {
  if (kind === 'RECEIPT') return '이것은 수신 코드(휴대폰 → PC)입니다. PC 담당자에게 알려 주는 코드이며, 여기에 넣는 코드가 아닙니다.';
  return `이것은 ${CODE_KINDS[kind].label}입니다. ${kind === 'PHOTO' ? '아래 사진 칸' : '위 결과 칸'}에 넣어 주세요. (여기는 ${CODE_KINDS[field].label} 칸)`;
}
/** 화면 표시: 'D-ABCD EFGH' */
export const showCode = (kind, code) => `${CODE_KINDS[kind].prefix}-${String(code).slice(0, 4)} ${String(code).slice(4)}`;

// ------------------------------------------------------------ 결과 반영 정보 (목록 묶음의 "rel", v0.13)
/** 결과 반영 코드를 넣은 것과 같은 처리: 보낸 범위의 결과 파일을 반영됨으로, releasedSeq를 올린다.
 *  seq를 넘는 기록은 반영됨으로 하지 않는다. 휴대폰 번호(st.seq)가 PC가 아는 번호보다 작으면(오래된 백업 복원 등)
 *  다음 기록이 PC에 이미 있는 번호를 다시 쓰지 않게 st.seq를 올린다. 돌려주는 값: 새로 반영된 기록 수. */
export function releaseUpTo(st, seq) {
  const before = st.releasedSeq || 0;
  st.seq = Math.max(st.seq || 0, seq);
  st.releasedSeq = Math.max(before, seq);
  for (const x of st.exports || []) if (!x.confirmed && x.to <= st.releasedSeq) { x.confirmed = true; x.via = 'list'; }
  return (st.events || []).filter((e) => e.seq > before && e.seq <= st.releasedSeq).length;
}
/** 목록에 실려 온 rel을 적용한다. 돌려주는 값: { events: 새로 반영된 기록 수, photos: 새로 회수 확인된 사진 수 } 또는 null(rel 없음). */
export function applyRelease(st, rel) {
  if (!rel || !Number.isInteger(rel.seq) || rel.seq < 0) return null;
  const events = releaseUpTo(st, rel.seq);
  const known = new Set([...(st.events || []).filter((e) => e.kind === 'PHOTO_META').map((e) => e.body && e.body.photo_id), ...(st.keptPhotos || []).map((p) => p.photo_id)]);
  const had = new Set(st.recoveredPhotos || []);
  const add = (rel.ph || []).filter((id) => known.has(id) && !had.has(id));
  if (add.length) st.recoveredPhotos = [...had, ...add];
  return { events, photos: add.length };
}

// ------------------------------------------------------------ 백업 복원 (v0.13)
const byKey = (arr, key) => { const m = new Map(); for (const x of arr || []) if (x && x[key] != null && !m.has(x[key])) m.set(x[key], x); return m; };
const newerList = (a, b) => {
  if (!a) return b; if (!b) return a;
  if (a.work_date !== b.work_date) return a.work_date > b.work_date ? a : b;
  return (a.assign_ver || 0) >= (b.assign_ver || 0) ? a : b;
};
/** 백업(bak)을 지금 상태(cur)에 복원한다. 새 상태를 돌려준다(cur·bak은 바꾸지 않음).
 *  - 기록 번호 seq = max(지금, 백업): 이미 PC로 보낸 번호를 다시 쓰지 않는다(PC는 기기·번호가 같으면 거부).
 *  - PC 연결 방식: 지금 휴대폰의 반영 상태(releasedSeq·결과 파일·사진 파일·회수 확인)를 지키고 백업 것과 합친다.
 *    기록은 event_id로 합친다(지금 휴대폰에만 있는 기록도 남김). 번호가 겹치는 다른 기록은 뒤 번호로 옮긴다.
 *    목록은 더 새 것(날짜·갱신본)을 쓴다.
 *  - 단독 방식: 백업으로 바꾸되 seq만 지킨다(새 휴대폰으로 옮기는 용도). */
export function mergeRestore(cur, bak) {
  const out = { ...bak };
  const seq = Math.max(cur.seq || 0, bak.seq || 0);
  out.seq = seq;
  if ((bak.mode || cur.mode) !== 'PC') return out;
  out.releasedSeq = Math.max(cur.releasedSeq || 0, bak.releasedSeq || 0);
  const evs = byKey(bak.events, 'event_id');
  for (const e of cur.events || []) if (!evs.has(e.event_id)) evs.set(e.event_id, e);
  // 서로 다른 기록이 같은 번호(이전에 잘못 복원한 휴대폰): 백업 쪽 기록을 그 번호에 두고 나머지는 맨 뒤 번호로 옮긴다.
  // 옮긴 기록을 PC가 이미 받았다면 PC는 event_id로 중복을 알아 건너뛴다(번호 충돌 없음).
  const fromBak = new Set((bak.events || []).map((e) => e.event_id));
  const events = [...evs.values()].sort((a, b) => a.seq - b.seq || (fromBak.has(b.event_id) ? 1 : 0) - (fromBak.has(a.event_id) ? 1 : 0));
  const taken = new Set();
  let next = seq;
  for (const e of events) {
    if (taken.has(e.seq)) { next += 1; e.seq = next; }
    taken.add(e.seq);
  }
  out.seq = Math.max(seq, next);
  out.events = events.sort((a, b) => a.seq - b.seq);
  const ex = byKey(cur.exports, 'code');
  for (const x of bak.exports || []) { const c = ex.get(x.code); if (!c) ex.set(x.code, x); else if (x.confirmed && !c.confirmed) ex.set(x.code, x); }
  out.exports = [...ex.values()].sort((a, b) => a.from - b.from || a.to - b.to);
  for (const x of out.exports) if (!x.confirmed && x.to <= out.releasedSeq) x.confirmed = true;
  out.photoExports = [...byKey([...(cur.photoExports || []), ...(bak.photoExports || [])], 'code').values()];
  out.recoveredPhotos = [...new Set([...(cur.recoveredPhotos || []), ...(bak.recoveredPhotos || [])])];
  out.keptPhotos = [...byKey([...(cur.keptPhotos || []), ...(bak.keptPhotos || [])], 'photo_id').values()];
  const dl = newerList(cur.daylist, bak.daylist);
  if (dl && dl === cur.daylist) { out.daylist = cur.daylist; out.lastBundleHash = cur.lastBundleHash; out.listChanges = cur.listChanges || {}; out.localOrder = cur.localOrder || null; }
  out.fieldStops = [...byKey([...(bak.fieldStops || []), ...(cur.fieldStops || [])], 'request_id').values()];
  return out;
}

// ------------------------------------------------------------ 하루 중 같은 날 목록 갱신 (v0.13)
/** 같은 날짜의 새 목록(pl)에서, 이 휴대폰이 그날 이미 기록한 지점을 PC가 '재방문'처럼 돌려보낸 것(중간 결과 반영 뒤 PC가
 *  수거한 품목을 빼고 prior_collected로 보냄)을 이 휴대폰 기록 기준으로 되돌린다. 그렇게 하지 않으면 같은 날 기록이
 *  재방문으로 표시되고, 다 실은 품목 줄이 사라져 그 기록이 화면 계산에서 빠진다(기록이 '오래된 것'처럼 보임).
 *  - 이전 목록에도 있던 지점(같은 팀 목록)만 본다. 그 지점의 since_seq(어느 기록부터 볼지)는 이전 값을 그대로.
 *  - 이전 목록에 없던 재방문이 아니면 revisit 표시를 이전 값으로, 새 목록에 빠진 품목 줄 중 이 휴대폰 기록이 있는 줄은 이전 줄을 다시 넣는다.
 *  pl.stops를 바꾸고, 되돌린 지점 수를 돌려준다. */
export function carryOver(oldList, pl, events) {
  if (!oldList || !pl || oldList.work_date !== pl.work_date) return 0;
  const prev = new Map(oldList.stops.map((s) => [s.request_id, s]));
  let n = 0;
  for (const s of pl.stops) {
    const o = prev.get(s.request_id);
    if (!o) continue;
    if (o.since_seq != null) s.since_seq = o.since_seq;
    const mine = events.filter((e) => e.request_id === s.request_id && !(e.seq <= (o.since_seq || 0)));
    if (!mine.length) continue;
    let touched = false;
    if (s.revisit && !o.revisit) { s.revisit = false; touched = true; }
    // 이 휴대폰이 남긴 재방문 약속(REVISIT_APPT)을 PC가 방문 약속·도착 예정으로 돌려보낸 것: 이전 값으로(기록이 약속을 정함)
    const appt = [...mine].reverse().find((e) => e.kind === 'REVISIT_APPT');
    const b = appt && appt.body;
    if (b && b.start && Array.isArray(s.appt) && s.appt[0] === b.start && (s.appt[1] || null) === (b.end || null) && JSON.stringify(s.appt) !== JSON.stringify(o.appt || null)) {
      s.appt = o.appt || null;
      if (appt.body.start && s.eta === appt.body.start) s.eta = o.eta || '';
      touched = true;
    }
    const have = new Set((s.items || []).map((i) => i.line_id));
    const used = new Set(mine.map((e) => e.body && e.body.line_id).filter(Boolean));
    for (const it of o.items || []) {
      if (!have.has(it.line_id) && used.has(it.line_id)) { s.items.push({ ...it }); touched = true; }
    }
    // 줄 순서는 이전 목록 순서대로(되살린 줄이 끝으로 가서 '정보 변경'으로 보이지 않게), 새 줄은 뒤에
    const pos = new Map((o.items || []).map((x, i) => [x.line_id, i]));
    s.items = s.items.map((x, i) => [x, pos.has(x.line_id) ? pos.get(x.line_id) : 1e6 + i]).sort((a, b) => a[1] - b[1]).map((x) => x[0]);
    // 이 휴대폰이 현장에서 더한 줄(FIELD_ITEM_ADD)을 PC가 목록에 넣어 돌려보낸 것은 빼 둔다 — 기록에서 다시 만들어진다(같은 줄 ID)
    const addedHere = new Set(mine.filter((e) => e.kind === 'FIELD_ITEM_ADD').map((e) => e.body && e.body.line_id));
    const before = s.items.length;
    s.items = s.items.filter((it) => !(addedHere.has(it.line_id) && !(o.items || []).some((x) => x.line_id === it.line_id)));
    if (s.items.length !== before) touched = true;
    // 이 휴대폰 기록이 있는 줄은 PC가 그 기록으로 바꾼 수량(확인 수량·prior_collected)을 쓰지 않는다 — 기록이 그 값을 정함.
    // 단가·금액 상태(사무실이 확정한 단가 등)는 새 목록 것을 쓴다.
    for (const it of s.items) {
      const o2 = (o.items || []).find((x) => x.line_id === it.line_id);
      if (!o2 || !used.has(it.line_id)) continue;
      const q = { qty: o2.qty, qty_received: o2.qty_received, prior_collected: o2.prior_collected || 0 };
      if (it.qty !== q.qty || it.qty_received !== q.qty_received || (it.prior_collected || 0) !== q.prior_collected) {
        Object.assign(it, q);
        const recv = (it.line_kind || 'RECEIVED') === 'RECEIVED';
        it.amount = !recv ? 0 : it.price_status === 'CONFIRMED' && it.unit_price != null ? it.unit_price * (it.qty_received ?? it.qty) : null;
        touched = true;
      }
    }
    if (touched) n++;
  }
  return n;
}
