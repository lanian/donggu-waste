// 품목 검색과 수수료·잔량 계산 (순수 함수, 화면과 분리해 테스트 가능)

const norm = (s) => (s || '').toLowerCase().replace(/\s+/g, '');

// 검색: 단어별 AND, 동의어는 공식 명칭으로 바꿔서 비교. 검색어는 어디에도 전송·저장하지 않는다.
// 초성 검색: "ㅈㄹ" → 장롱, "ㅅㅍ" → 소파. 초성만으로 된 낱말은 대상의 초성 문자열과 비교한다.
const CHO = 'ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ';
export const isChosung = (t) => /[ㄱ-ㅎ]/.test(t) && /^[ㄱ-ㅎ0-9a-z.]+$/i.test(t);
export function chosungOf(s) {
  let out = '';
  for (const ch of s || '') {
    const c = ch.charCodeAt(0) - 0xac00;
    out += c >= 0 && c < 11172 ? CHO[Math.floor(c / 588)] : ch;
  }
  return out;
}
export function matchText(hay, query) {
  // 띄어쓰기로 나눈 낱말이 모두 들어 있어야 한다. 초성 낱말은 초성끼리 비교.
  const h = norm(hay), hc = chosungOf(h);
  return (query || '').trim().split(/\s+/).filter(Boolean).every((t) => { const n = norm(t); return isChosung(n) ? hc.includes(n) : h.includes(n); });
}

export function searchFees(table, query) {
  const q = (query || '').trim();
  if (!q || !table) return [];
  const aliases = table.aliases || {};
  let tokens = [];
  for (const t of q.split(/\s+/)) {
    const alias = aliases[t] || aliases[norm(t)];
    tokens = tokens.concat(alias ? alias.split(/\s+/) : [t]);
  }
  tokens = tokens.map(norm).filter(Boolean);
  const out = [];
  table.rows.forEach((r, order) => {
    const name = norm(r.name), hay = norm(`${r.name} ${r.spec} ${r.category}`), hayC = chosungOf(hay), nameC = chosungOf(name);
    if (!tokens.every((t) => (isChosung(t) ? hayC.includes(t) : hay.includes(t)))) return;
    const rank = tokens.some((t) => t === name || t === nameC) ? 0 : tokens.some((t) => (isChosung(t) ? nameC.startsWith(t) : name.includes(t))) ? 1 : 2;
    out.push({ row: r, rank, order });
  });
  return out.sort((a, b) => a.rank - b.rank || a.order - b.order).map((x) => x.row);
}

export const won = (n) => (n == null ? '미확정' : n.toLocaleString('ko-KR') + '원');

// 한 접수 건의 현재 상태를 이벤트에서 계산한다 (이벤트는 추가만, 최신 값이 현재 값).
export function deriveStop(stop, events) {
  const mine = events.filter((e) => e.request_id === stop.request_id);
  // 재방문 목록은 남은 품목만 오며, 이전 방문에서 이미 수거한 수량(prior_collected)에서 이어서 센다.
  const lines = stop.items.map((it) => ({
    line_id: it.line_id, kind: it.line_kind || 'RECEIVED', name: it.name, spec: it.spec, unit: it.unit, unit_label: it.unit_label,
    fee_rate_id: it.fee_rate_id, price: it.price_status === 'CONFIRMED' ? it.unit_price : null,
    qty_received: (it.line_kind || 'RECEIVED') === 'RECEIVED' ? it.qty : 0,
    amount_received: (it.line_kind || 'RECEIVED') === 'RECEIVED' && it.price_status === 'CONFIRMED' ? it.amount : 0,
    qty_confirmed: it.qty, qty_collected: it.prior_collected || 0, reason: '',
  }));
  const byId = Object.fromEntries(lines.map((l) => [l.line_id, l]));
  let contact = 'NOT_CONTACTED', outcome = null, photos = [], loc = null, appt = null;
  const memos = [], stickers = {}, notices = [];
  let arrive = null, noPhotoReason = null;
  for (const e of mine) {
    const b = e.body || {};
    if (e.kind === 'CONTACT') contact = b.result;
    else if (e.kind === 'FIELD_ITEM_ADD') {
      const l = { line_id: b.line_id, kind: 'FIELD', name: b.name, spec: b.spec || '', unit: b.unit, unit_label: b.unit_label,
        fee_rate_id: b.fee_rate_id || null, price: b.price_status === 'CONFIRMED' ? b.unit_price : null,
        qty_received: 0, amount_received: 0, qty_confirmed: b.qty, qty_collected: 0, reason: '' };
      lines.push(l); byId[l.line_id] = l;
    } else if (e.kind === 'ITEM_RESULT' && byId[b.line_id]) {
      Object.assign(byId[b.line_id], { qty_confirmed: b.qty_confirmed, qty_collected: b.qty_collected, reason: b.reason || '' });
    } else if (e.kind === 'ATTEMPT') { outcome = b.outcome; noPhotoReason = b.no_photo_reason || null; }
    else if (e.kind === 'STICKER_CHECK') stickers[b.line_id] = { result: b.result, number: b.number || '', decision: b.decision || null };
    else if (e.kind === 'FEE_NOTICE') notices.push({ ...b, ts: e.ts });
    else if (e.kind === 'ARRIVE') arrive = { ts: e.ts, lat: b.lat ?? null, lon: b.lon ?? null, acc: b.acc ?? null };
    else if (e.kind === 'PHOTO_META') photos.push(b);
    else if (e.kind === 'FIELD_MEMO') memos.push({ text: b.text, ts: e.ts });
    else if (e.kind === 'LOCATION_FIX') loc = { lat: b.lat, lon: b.lon };
    else if (e.kind === 'REVISIT_APPT') appt = { date: b.date, start: b.start, end: b.end };
  }
  for (const l of lines) l.remaining = Math.max(0, l.qty_confirmed - l.qty_collected);
  // 위치: 현장에서 고친 위치(LOCATION_FIX) 우선, 없으면 접수 자료의 좌표
  if (!loc && stop.lat != null && stop.lon != null) loc = { lat: stop.lat, lon: stop.lon };
  const fee = calcFee(lines, stop.reduction ? stop.reduction.rate : 0);
  const notice = notices.length ? notices[notices.length - 1] : null;
  return { lines, contact, outcome, photos, loc, appt, fee, memos, stickers, notices, notice, arrive, noPhotoReason,
    progress: progressOf(lines, mine.filter((e) => !NON_PROGRESS.includes(e.kind)).length) };
}
// 수거 진행으로 세지 않는 기록 (연락·안내·위치 같은 것)
export const NON_PROGRESS = ['LOCATION_FIX', 'REVISIT_APPT', 'CONTACT', 'DIAL_INTENT', 'FIELD_MEMO', 'SMS_INTENT', 'NAV_INTENT'];

// 현장 금액 변경 고지: 확인된 소계가 접수 당시 금액보다 커졌는데, 그 금액 이상으로 안내한 기록이 없으면 안내가 필요하다.
export function feeExtra(d) { return Math.max(0, d.fee.confirmedSubtotal - d.fee.received); }
export function needsFeeNotice(d) {
  const x = feeExtra(d);
  // 거부(해당 품목 미수거)도 안내가 끝난 것으로 본다 — 같은 금액으로 다시 묻지 않음
  return x > 0 && !d.notices.some((n) => n.extra >= x);
}

// 감면(v0.11): PC finance.fee_summary 와 같은 규칙 — 감면액 = floor(계산 금액 × 감면율 / 100) (원 단위 버림), 부과 금액 = 계산 금액 − 감면액.
// 현장 추가 품목도 계산 금액에 들어가므로 같은 감면율이 적용된다.
export function applyReduction(amount, rate) {
  const r = Math.max(0, Math.min(100, Math.trunc(Number(rate) || 0)));
  if (amount == null) return { rate: r, reduction: null, levied: null };
  const reduction = Math.floor((amount * r) / 100);
  return { rate: r, reduction, levied: amount - reduction };
}

// 수수료: 산정 수량 = 현장 확인 수량. 실제 수거 수량은 금액에 쓰지 않는다. 단위가 다른 행은 합산하지 않는다.
export function calcFee(lines, reductionRate = 0) {
  const received = lines.filter((l) => l.kind === 'RECEIVED').reduce((s, l) => s + (l.amount_received || 0), 0);
  const pending = lines.filter((l) => l.price == null && l.qty_confirmed > 0);
  const confirmedSubtotal = lines.filter((l) => l.price != null).reduce((s, l) => s + l.price * l.qty_confirmed, 0);
  const fieldAdded = lines.filter((l) => l.kind === 'FIELD' && l.price != null).reduce((s, l) => s + l.price * l.qty_confirmed, 0);
  const qtyByUnit = {};
  for (const l of lines) qtyByUnit[l.unit_label] = (qtyByUnit[l.unit_label] || 0) + l.qty_confirmed;
  const final = pending.length ? null : confirmedSubtotal;
  const red = applyReduction(final, reductionRate);
  return {
    received, confirmedSubtotal, fieldAdded,
    diff: confirmedSubtotal - received,
    pendingCount: pending.length,
    final,
    qtyByUnit,
    // 감면: reductionRate(%), reduction(감면액), levied(부과 금액 = 주민이 낼 금액). 감면이 없으면 levied = final. 금액 확인 대기가 있으면 null.
    reductionRate: red.rate, reduction: red.reduction, levied: red.levied,
  };
}

export function progressOf(lines, eventCount) {
  if (!eventCount) return 'TODO';
  const anyCollected = lines.some((l) => l.qty_collected > 0);
  const allDone = lines.every((l) => l.remaining === 0);
  if (allDone && anyCollected) return 'DONE';
  if (anyCollected) return 'PARTIAL';
  return 'STARTED';
}

export const LABEL = {
  contact: { NOT_CONTACTED: '연락 전', REACHED: '통화 완료', NO_ANSWER: '부재', CALLBACK: '재연락 예정', WRONG_NUMBER: '번호 오류' },
  progress: { TODO: '방문 전', STARTED: '처리 중', PARTIAL: '일부 수거', DONE: '수거 완료' },
  outcome: { DONE: '수거 완료', PARTIAL: '일부 수거', NOT_COLLECTED: '미수거', ON_HOLD: '보류' },
  reason: { '': '사유 선택', NOT_PUT_OUT: '배출 안 됨', DIFFERENT: '접수와 다름', CANNOT_LOAD: '적재 불가', POSTPONED: '주민 요청 연기',
    NO_STICKER: '스티커·필증 없음·불일치', FEE_REFUSED: '추가 금액 거부', OTHER: '기타' },
  sticker: { OK: '확인', MISSING: '없음', MISMATCH: '번호 불일치' },
  feeResponse: { NOTIFIED: '주민에게 안내함', AGREED: '동의', REFUSED: '거부(해당 품목 미수거)' },
  noPhoto: { DARK: '어두워 촬영 불가', REFUSED: '주민이 촬영 거부', CAMERA: '카메라 고장', OTHER: '기타' },
};
