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
  s.dong || '', s.appt || null, !!s.revisit, itemKey(s), ...(s.reduction && s.reduction.rate ? [s.reduction.rate] : [])]);

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
