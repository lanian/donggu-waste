// 휴대폰 화면: 접수 상세(현장 처리)·도착·문자·길안내·스티커·금액 고지·정정/취소 요청·재방문
import * as F from '../field.js';
import * as L from '../load.js';
import * as M from '../map.js';
import * as NAV from '../nav.js';
import * as S from '../store.js';
import { LABEL } from '../fees.js';
import { applyReduction } from '../fees.js';
import { deriveStop } from '../fees.js';
import { feeExtra } from '../fees.js';
import { needsFeeNotice } from '../fees.js';
import { won } from '../fees.js';
import { addEvent, ask, btn, camInput, cancelAsked, changeReqs, commit, dial, digits, dongOf, fieldEditable, findStop, flushSteps, fmtPhone, go, h, handoff, isSolo, nextSeq, openExternal, pcStops, pcWorkDate, pendingPhotos, photoUrls, reduceMotion, render, sheet, smsTemplates, st, stepper, toast, todayKST, ui } from '../core.js';
import { NO_LOC, navBtn } from '../widgets.js';

const hhmm = (iso) => new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(iso));
// [도착]: 시각과(허용했으면) 휴대폰 위치·정확도. 위치는 결과 파일로만 PC에 가고 지도 서버로는 보내지 않는다.
async function arrive(stop) {
  const geo = st.geoEvidence !== false ? await M.myFix({ ask: true, timeout: 8000 }) : null;
  await addEvent(stop, 'ARRIVE', geo ? { lat: geo.lat, lon: geo.lon, acc: geo.acc } : {});
  toast(geo ? `도착을 기록했습니다 (위치 오차 약 ${geo.acc}m)` : '도착을 기록했습니다 (위치 없음)');
  render();
}
// [문자]: 문구를 골라 문자 앱을 연다. 기록에는 문구 종류만 남기고 내용은 남기지 않는다.
async function smsSheet(stop, d) {
  const tpls = smsTemplates();
  const v = await sheet({ title: '문자 보내기', text: '문구를 고르면 문자 앱이 열립니다. 보내기 전에 내용을 확인하세요. 이 주민의 접수번호·시각만 들어갑니다.',
    choices: tpls.map((t, i) => ({ label: t.label, value: i, cls: i === 0 ? 'primary' : '' })) });
  if (v == null) return;
  const t = tpls[v];
  await addEvent(stop, 'SMS_INTENT', { template: t.id });
  handoff();
  openExternal(F.smsHref(stop.phone, F.fillSms(t.text, stop, d.appt)));
  render();
}
// 스티커·신고필증 확인 (품목별 3가지). 없음·불일치면 그 품목을 수거할지 바로 정한다.
async function stickerCheck(stop, l, result) {
  if (result === 'OK') { await addEvent(stop, 'STICKER_CHECK', { line_id: l.line_id, result }); return render(); }
  const v = await sheet({ title: `${l.name} — 스티커·필증 ${LABEL.sticker[result]}`,
    text: '이 품목을 어떻게 할까요? 미수거로 하면 "스티커·필증 없음·불일치" 사유로 남습니다.',
    input: result === 'MISMATCH' ? { label: '붙어 있는 번호 (선택)', placeholder: '예: 2611-0042' } : null,
    choices: [{ label: '그래도 수거', value: 'COLLECT', cls: 'primary' }, { label: '미수거 (사유 기록)', value: 'LEAVE' }] });
  if (v == null) return;
  const decision = typeof v === 'object' ? v.value : v;
  const number = typeof v === 'object' ? v.text.slice(0, 30) : '';
  await addEvent(stop, 'STICKER_CHECK', { line_id: l.line_id, result, ...(number ? { number } : {}), decision });
  if (decision === 'LEAVE') await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, qty_confirmed: l.qty_confirmed, qty_collected: 0, reason: 'NO_STICKER' });
  render();
}
// 현장 금액 변경 고지: 접수 때보다 금액이 늘면 주민 안내 결과를 기록한다. 거부하면 늘어난 품목은 미수거.
// 돌려주는 값: true = 계속 저장, false = 저장 멈춤
async function feeNotice(stop, d) {
  const x = feeExtra(d), f = d.fee;
  const v = await sheet({ title: `추가 금액 ${won(x)}`,
    text: `접수 당시 ${won(f.received)} → 현장 확인 ${won(f.confirmedSubtotal)}${f.pendingCount ? ` (금액 확인 대기 ${f.pendingCount}건 제외)` : ''}.`
      + (f.reductionRate ? ` 감면 ${f.reductionRate}% 적용 시 늘어나는 부과 금액은 ${won(applyReduction(x, f.reductionRate).levied)}입니다.` : '') + ' 주민에게 안내하고 결과를 고르세요.',
    choices: [{ label: LABEL.feeResponse.AGREED, value: 'AGREED', cls: 'primary' }, { label: LABEL.feeResponse.NOTIFIED, value: 'NOTIFIED' },
      { label: LABEL.feeResponse.REFUSED, value: 'REFUSED', cls: 'danger' }] });
  if (v == null) return false;
  const body = { received: f.received, total: f.confirmedSubtotal, extra: x, pending: f.pendingCount, response: v };
  if (v !== 'REFUSED') { await addEvent(stop, 'FEE_NOTICE', body); return true; }
  // 늘어난 부분: 현장 추가 품목 전체, 접수 품목은 접수 수량을 넘는 만큼
  const lines = [];
  for (const l of d.lines) {
    if (l.price == null) continue;
    if (l.kind === 'FIELD' && l.qty_confirmed > 0) { lines.push(l.line_id); await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, qty_confirmed: l.qty_confirmed, qty_collected: 0, reason: 'FEE_REFUSED' }); }
    else if (l.kind === 'RECEIVED' && l.qty_confirmed > l.qty_received) { lines.push(l.line_id); await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, qty_confirmed: l.qty_confirmed, qty_collected: Math.min(l.qty_collected, l.qty_received), reason: 'FEE_REFUSED' }); }
  }
  await addEvent(stop, 'FEE_NOTICE', { ...body, lines });
  ui.tried = stop.request_id; ui.outcomeMsg = '추가 금액을 거부한 품목을 미수거로 바꿨습니다. 결과를 다시 골라 주세요 (일부 수거·미수거).';
  render();
  return false;
}
// 부재·미배출로 미수거할 때는 사진 1장 이상, 또는 찍지 못한 이유가 있어야 한다.
async function evidence(stop, d) {
  const v = await sheet({ title: '증거 사진이 필요합니다', text: '부재·배출 안 됨으로 미수거하려면 현장 사진을 1장 이상 찍어 주세요. 찍을 수 없으면 이유를 고르세요.',
    choices: [{ label: '📷 사진 찍으러 가기', value: 'PHOTO', cls: 'primary' }, ...Object.entries(LABEL.noPhoto).map(([k, t]) => ({ label: `사진 불가: ${t}`, value: k }))] });
  if (v == null) return null;
  if (v === 'PHOTO') { const el = document.getElementById('st-photos'); if (el) el.scrollIntoView({ block: 'start' }); return null; }
  return v;
}

// 싣기 안내: 품목별 적재 기준표의 취급 표시(2인·세워서·유리 등)와 짐 양(추정)
function loadGuide(items) {
  const l = L.loadOf(items);
  if (!l.tags.length && l.m3 < 1) return null;
  return h('div', { class: 'load-guide' }, h('p', {}, h('strong', {}, '싣기'), ` 약 ${l.m3}m³ · ${l.kg.toLocaleString()}kg (추정)`),
    l.tags.length ? h('ul', {}, ...l.tags.map((t) => h('li', {}, h('em', { class: `chip load ld-${t}` }, L.TAGS[t].label), ' ', L.TAGS[t].hint))) : null);
}
async function screenStop(id) {
  const stop = findStop(id);
  if (!stop) { go('#/home'); return h('main'); }
  const d = deriveStop(stop, st.events);
  // 새 목록에서 바뀐 표시는 한 번 열어 보면 지운다
  const seen = (st.listChanges || {})[id];
  if (seen) { delete st.listChanges[id]; commit(); }
  const purged = !!stop.pii_purged;
  const setLine = async (l, patch) => {
    // 화면을 그린 뒤 다른 수량이 저장됐을 수 있으니 지금 기록에서 다시 계산한 값 위에 바꾼 칸만 덮는다
    const now = deriveStop(stop, st.events).lines.find((x) => x.line_id === l.line_id) || l;
    const v = { qty_confirmed: now.qty_confirmed, qty_collected: now.qty_collected, reason: now.reason, ...patch };
    if (v.qty_collected > v.qty_confirmed) v.qty_collected = v.qty_confirmed;
    await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, ...v });
    render();
  };

  const smsLog = st.events.filter((e) => e.request_id === id && e.kind === 'SMS_INTENT');
  const info = h('section', { class: 'card who' },
    seen ? h('p', { class: 'notice changed-note' }, '새 목록에서 바뀐 곳: ' + seen.map((c) => F.DIFF_LABEL[c] || c).join(', ')) : null,
    purged ? h('p', { class: 'name' }, '개인정보 정리됨') : h('p', { class: 'name' }, stop.name),
    purged ? h('p', { class: 'small' }, '결과 반영이 확인되어 이 휴대폰에서 이름·연락처·주소를 지웠습니다. 수거 기록은 남아 있습니다.')
      : h('p', { class: 'phone' }, h('a', { href: 'tel:' + stop.phone, onclick: async (e) => { e.preventDefault(); await dial(stop); } }, fmtPhone(stop.phone))),
    purged ? null : h('p', { class: 'addr' }, stop.address),
    dongOf(stop) ? h('p', { class: 'dong-line' }, h('em', { class: 'chip dong' }, dongOf(stop))) : null,
    stop.spot_note ? h('p', { class: 'spot' }, '배출 위치: ' + stop.spot_note) : null,
    stop.memo ? h('p', { class: 'memo' }, '요청: ' + stop.memo) : null,
    h('p', { class: 'small' }, stop.eta ? `${stop.receipt_no} · 도착 예정 ${stop.eta}` : stop.receipt_no),
    stop.levy_no ? h('p', { class: 'small levy-no' }, `부과번호 ${stop.levy_no}`) : null,
    stop.removed ? h('p', { class: 'notice' }, '새 목록에서 빠진 건입니다. PC 담당자 확인이 필요합니다.') : null,
    stop.field ? h('p', { class: 'notice' }, `이 휴대폰에서 받은 ${stop.channel || '현장'} 접수입니다(임시번호 ${stop.receipt_no}${stop.planned_date ? ` · 예정일 ${stop.planned_date}` : ''}). PC 접수번호는 PC 반영 뒤 목록에 나옵니다.`) : null,
    cancelAsked(stop.request_id) ? h('p', { class: 'notice' }, '취소를 요청했습니다. PC 담당자가 확인하기 전까지는 원래 접수 그대로입니다.') : null,
    stop.status === 'CANCELLED' ? h('p', { class: 'notice' }, '취소된 접수입니다. 사유: ' + (stop.cancel_reason || '-')) : null,
    stop.revisit ? h('p', { class: 'notice' }, '재방문 건입니다. 남은 품목만 표시하며, 이전에 수거한 수량에서 이어서 셉니다.') : null,
    stop.appt ? h('p', { class: 'memo' }, `방문 약속: ${stop.appt[0] || ''}~${stop.appt[1] || ''}`) : null,
    loadGuide(d.lines.map((l) => ({ name: l.name, spec: l.spec, qty: l.remaining ?? l.qty_confirmed }))),
    // 현장 동작: 도착 · 문자 · 길안내
    h('div', { class: 'grid2 field-acts' },
      btn(d.arrive ? `도착 ${hhmm(d.arrive.ts)} ✓` : '📍 도착', () => arrive(stop), 'opt arrive' + (d.arrive ? ' on' : ''), { 'aria-label': d.arrive ? `도착 기록됨 ${hhmm(d.arrive.ts)}, 다시 기록` : '도착' }),
      btn('✉ 문자', () => smsSheet(stop, d), 'opt sms', { disabled: purged || !stop.phone, 'aria-label': '문자' })),
    d.loc ? null : h('p', { class: 'small nav-none' }, `길안내: ${NO_LOC}` + (st.mapEnabled ? ' (아래 [위치 지정])' : '')),
    d.arrive ? h('p', { class: 'small' }, `도착 기록 ${hhmm(d.arrive.ts)}` + (d.arrive.acc != null ? ` · 위치 오차 약 ${d.arrive.acc}m` : ' · 위치 없음')) : null,
    smsLog.length ? h('p', { class: 'small sms-log' }, '문자 안내: ' + smsLog.map((e) => `${(smsTemplates().find((t) => t.id === e.body.template) || { label: e.body.template }).label} ${hhmm(e.ts)}`).join(', ')) : null,
    st.mapEnabled ? h('div', { class: 'grid2' },
      btn(d.loc ? '지도' : '위치 미지정', () => go(`#/map/${id}`), 'opt', { disabled: !d.loc }),
      btn(d.loc ? '위치 고치기' : '위치 지정', () => go(`#/pick/${id}`), 'opt')) : null);

  const contact = h('section', { class: 'card' + (ui.afterCall === id && d.contact === 'NOT_CONTACTED' ? ' ask' : '') },
    h('h2', {}, '연락 결과', ui.afterCall === id ? h('span', { class: 'hint' }, ' — 통화 결과를 골라 주세요') : null),
    h('div', { class: 'grid2' }, ...['REACHED', 'NO_ANSWER', 'CALLBACK', 'WRONG_NUMBER'].map((k) =>
      btn(LABEL.contact[k], async () => { ui.afterCall = null; await addEvent(stop, 'CONTACT', { result: k }); render(); }, d.contact === k ? 'opt on' : 'opt'))),
    d.appt ? h('p', { class: 'notice' }, `재방문 약속: ${d.appt.date} ${d.appt.start || ''}${d.appt.end ? '~' + d.appt.end : ''}`) : null,
    ['REACHED', 'CALLBACK'].includes(d.contact) && d.progress !== 'DONE' ? revisitForm(stop, d) : null);

  const touched = new Set(st.events.filter((e) => e.request_id === stop.request_id && e.kind === 'ITEM_RESULT').map((e) => e.body.line_id));
  const showLeft = (l) => touched.has(l.line_id) || !!d.outcome || ui.tried === id;
  const stickerRow = (l) => {
    const sk = d.stickers[l.line_id];
    return h('div', { class: 'chk' },
      h('span', { class: 'chk-label' }, '스티커·필증'),
      h('div', { class: 'chk-opts', role: 'group', 'aria-label': `${l.name} 스티커·신고필증` },
        ...['OK', 'MISSING', 'MISMATCH'].map((k) => btn(LABEL.sticker[k], () => stickerCheck(stop, l, k), 'opt sk' + (sk && sk.result === k ? ' on' : '') + (k !== 'OK' ? ' sk-bad' : '')))),
      sk && sk.result !== 'OK' ? h('p', { class: 'small sk-note' }, `${LABEL.sticker[sk.result]}${sk.number ? ` (${sk.number})` : ''} → ${sk.decision === 'LEAVE' ? '미수거' : sk.decision === 'COLLECT' ? '수거' : '결정 전'}`) : null);
  };
  const lines = h('section', { class: 'card' }, h('h2', {}, '품목'),
    ...d.lines.map((l) => h('div', { class: 'line ' + (l.kind === 'FIELD' ? 'field' : 'orig') },
      h('div', { class: 'lhead' },
        h('div', { class: 'lname' }, h('em', { class: 'tag' }, l.kind === 'FIELD' ? '현장 추가' : '처음 접수'), h('strong', {}, `${l.name} ${l.spec}`)),
        l.price == null ? h('span', { class: 'amt pending' }, '확인 대기') : h('span', { class: 'amt', 'aria-label': `금액 ${won(l.price * l.qty_confirmed)}` }, won(l.price * l.qty_confirmed))),
      h('div', { class: 'price' }, l.price == null ? '금액 확인 대기' : `${won(l.price)} / ${l.unit_label} × ${l.qty_confirmed}`),
      l.kind === 'RECEIVED' ? h('div', { class: 'row' }, h('span', {}, '접수 수량'), h('span', { class: 'val' }, l.qty_received)) : null,
      h('div', { class: 'row' }, h('span', {}, '현장 수량'), stepper(l.qty_confirmed, (n) => setLine(l, { qty_confirmed: n }), 0, 99, 350, `${l.name} 현장 수량`)),
      h('div', { class: 'row got' }, h('span', {}, '실은 수량'), stepper(l.qty_collected, (n) => setLine(l, { qty_collected: n }), 0, l.qty_confirmed, 350, `${l.name} 실은 수량`)),
      showLeft(l) ? h('div', { class: 'row' + (l.remaining ? ' left' : '') }, h('span', {}, '못 실은 수량'), h('span', { class: 'val' }, `${l.remaining} ${l.unit_label}`)) : null,
      l.remaining && showLeft(l) ? (() => {
        const sel = h('select', { class: 'reason' + (l.reason || ui.tried !== id ? '' : ' need'), 'aria-label': `${l.name} 못 실은 사유`, onchange: (e) => setLine(l, { reason: e.target.value }) },
          ...Object.entries(LABEL.reason).map(([k, t]) => h('option', { value: k, selected: k === l.reason }, t)));
        return sel;
      })() : null,
      stickerRow(l))),
    btn('+ 현장 품목 추가', () => { ui.add = { stop: id, q: '', cat: '', items: [] }; go(`#/add/${id}`); }, 'big'));

  const f = d.fee;
  const units = Object.entries(f.qtyByUnit).map(([u, n]) => `${n}${u}`).join(' · ');
  const extra = feeExtra(d);
  const noticeLine = d.notice ? h('p', { class: 'notice fee-notice' }, `추가 금액 안내: ${won(d.notice.extra)} · ${LABEL.feeResponse[d.notice.response] || d.notice.response} (${hhmm(d.notice.ts)})`) : null;
  const fee = h('section', { class: 'card fee' }, h('h2', {}, '계산 내역'),
    h('dl', {},
      h('dt', {}, '접수 당시 금액'), h('dd', {}, won(f.received)),
      h('dt', {}, '현장 추가분'), h('dd', {}, won(f.fieldAdded)),
      h('dt', {}, '확인된 소계'), h('dd', {}, won(f.confirmedSubtotal)),
      h('dt', {}, '접수 대비 증감'), h('dd', {}, (f.diff > 0 ? '+' : '') + won(f.diff)),
      h('dt', {}, '금액 확인 대기'), h('dd', {}, f.pendingCount + '건')),
    // 감면(PC 목록의 "rd"): 계산 금액 → 감면(율) → 부과 금액. 감면액은 원 단위 버림(PC와 같은 규칙), 현장 추가 품목에도 적용.
    f.reductionRate ? h('dl', { class: 'reduction', 'aria-label': '감면' },
      h('dt', {}, '계산 금액'), h('dd', {}, won(f.final)),
      h('dt', {}, `감면 (${f.reductionRate}%)`), h('dd', {}, f.reduction == null ? '미확정' : '−' + won(f.reduction))) : null,
    h('div', { class: 'total-box' + (f.final == null ? ' pending' : ''), role: 'group', 'aria-label': f.reductionRate ? '부과 금액' : '총 수수료' },
      h('span', { class: 'tb-label' }, f.reductionRate ? '부과 금액' : '총 수수료', h('small', {}, f.final == null ? '최종 금액 · 금액 확인 대기 있음' : f.reductionRate ? `감면 ${f.reductionRate}% 적용 · 주민이 낼 금액` : '최종 금액')),
      h('strong', { class: 'tb-val' }, f.final == null ? '미확정' : won(f.levied))),
    noticeLine,
    extra > 0 && needsFeeNotice(d) ? btn(`추가 금액 ${won(extra)} 안내 기록`, () => feeNotice(stop, d), 'big warn') : null,
    h('p', { class: 'small' }, `수량(단위별): ${units}. 금액은 현장 확인 수량 기준이며 수납 완료를 뜻하지 않습니다.`));

  // 사진: 카메라 입력은 화면 밖(몸통)에 하나씩 두고, 이 단추는 그것을 연다
  const photoIn = (phase) => {
    camInput(phase);
    return h('label', { class: 'btn big', for: 'cam-' + phase, onclick: () => { ui.photoTarget = { stopId: id, phase }; handoff(); } }, phase === 'BEFORE' ? '📷 수거 전' : '📷 수거 후');
  };
  const lineName = (lid) => { const l = d.lines.find((x) => x.line_id === lid); return l ? l.name : ''; };
  const photoFor = h('select', { 'aria-label': '사진을 묶을 품목', onchange: (e) => { ui.photoLine = { stop: id, line: e.target.value || null }; } },
    h('option', { value: '' }, '사진 대상: 접수 건 전체'),
    ...d.lines.map((l) => h('option', { value: l.line_id, selected: ui.photoLine && ui.photoLine.stop === id && ui.photoLine.line === l.line_id }, `사진 대상: ${l.name} ${l.spec || ''}`)));
  const thumbs = h('div', { class: 'thumbs' });
  for (const p of d.photos) {
    const img = h('img', { alt: p.phase === 'BEFORE' ? '수거 전' : '수거 후' });
    thumbs.append(h('figure', {}, img, h('figcaption', {}, (p.phase === 'BEFORE' ? '전' : '후') + (p.line_id ? ` · ${lineName(p.line_id)}` : '') + (p.taken_at ? ` · ${hhmm(p.taken_at)}` : ''))));
    S.getSecureBytes('ph:' + p.photo_id).then((b) => { if (b) { const u = URL.createObjectURL(new Blob([b], { type: 'image/jpeg' })); photoUrls.push(u); img.src = u; } });
  }
  const waiting = pendingPhotos.filter((p) => p.stopId === id).length;
  const photos = h('section', { class: 'card' }, h('h2', {}, `사진 ${d.photos.length}장`), d.lines.length > 1 ? photoFor : null, h('div', { class: 'grid2' }, photoIn('BEFORE'), photoIn('AFTER')),
    waiting ? h('p', { class: 'notice pending-photo' }, `사진 ${waiting}장 저장 중…`) : null, thumbs,
    h('p', { class: 'small' }, '사진은 앱 안에만 암호화 저장되며 갤러리에 남지 않습니다. 찍은 시각(과 허용했으면 위치)이 함께 기록됩니다.'));

  const msg = h('p', { class: 'msg' });
  // 추가 금액을 거부했거나 스티커가 없어 두고 가기로 한 품목은 [전부 실음]에서도 싣지 않은 채로 둔다
  const KEEP_OUT = ['FEE_REFUSED', 'NO_STICKER'];
  const fillAll = async () => {
    await flushSteps();
    const cur = deriveStop(stop, st.events);
    for (const l of cur.lines) if (!KEEP_OUT.includes(l.reason) && l.qty_collected !== l.qty_confirmed) await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, qty_confirmed: l.qty_confirmed, qty_collected: l.qty_confirmed, reason: '' });
    return deriveStop(stop, st.events);
  };
  const fail = (t) => { ui.tried = id; ui.outcomeMsg = t; render(); };
  const outMsg = h('p', { class: 'msg err', role: 'alert' }, ui.outcomeMsg && ui.tried === id ? ui.outcomeMsg : '');
  const outcome = h('section', { class: 'card' }, h('h2', {}, '수거 결과', d.outcome ? h('span', { class: 'hint' }, ' — 저장됨: ' + LABEL.outcome[d.outcome]) : null),
    // 가장 흔한 경우를 한 번에: 모든 품목을 실은 것으로 채우고 완료 저장
    btn('전부 실음 · 수거 완료', async () => {
      await flushSteps();
      const d = deriveStop(stop, st.events);
      if (needsFeeNotice(d) && !(await feeNotice(stop, d))) return;
      ui.tried = null; ui.outcomeMsg = '';
      const after = await fillAll();
      const left = after.lines.filter((l) => l.qty_collected < l.qty_confirmed);
      if (left.length) { await addEvent(stop, 'ATTEMPT', { outcome: 'PARTIAL' }); toast(`일부 수거로 저장했습니다 (안 싣기로 한 품목 ${left.length}개는 남김)`); render(); return; }
      await addEvent(stop, 'ATTEMPT', { outcome: 'DONE' }); toast('수거 완료로 저장했습니다'); render();
    }, d.outcome === 'DONE' ? 'primary huge on' : 'primary huge'),
    outMsg,
    h('div', { class: 'grid3' }, ...['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].map((k) => btn(LABEL.outcome[k], async () => {
      // 사유·수량을 고른 직후 누르면 화면이 아직 다시 그려지기 전일 수 있다: 모아 둔 수량을 먼저 저장하고 지금 기록으로 판단
      await flushSteps();
      const d = deriveStop(stop, st.events);
      const left = d.lines.filter((l) => l.remaining > 0);
      if (k !== 'ON_HOLD' && !left.length) return fail('못 실은 품목이 없습니다. 모두 실었으면 [전부 실음 · 수거 완료]를 누르세요.');
      if (k !== 'ON_HOLD' && left.some((l) => !l.reason)) return fail('못 실은 품목마다 사유를 골라 주세요 (위 품목 칸).');
      if (k === 'PARTIAL' && !d.lines.some((l) => l.qty_collected > 0)) return fail('실은 품목이 없으면 미수거로 저장하세요.');
      if (k === 'NOT_COLLECTED' && d.lines.some((l) => l.qty_collected > 0)) return fail('실은 품목이 있으면 일부 수거로 저장하세요.');
      const body = { outcome: k };
      // 부재·배출 안 됨으로 미수거: 사진 또는 "사진 불가" 이유가 있어야 한다
      if (k === 'NOT_COLLECTED' && (d.contact === 'NO_ANSWER' || left.some((l) => l.reason === 'NOT_PUT_OUT')) && !d.photos.length && !pendingPhotos.some((p) => p.stopId === id)) {
        const why = await evidence(stop, d);
        if (!why) return;
        body.no_photo_reason = why;
      }
      if (k === 'PARTIAL' && needsFeeNotice(d) && !(await feeNotice(stop, d))) return;
      ui.tried = null; ui.outcomeMsg = '';
      await addEvent(stop, 'ATTEMPT', body); toast('저장했습니다'); render();
    }, d.outcome === k ? 'opt on' : 'opt'))),
    d.outcome ? h('div', { class: 'result-sum' },
      h('p', {}, `저장된 결과: ${LABEL.outcome[d.outcome]}`),
      d.notice ? h('p', {}, `추가 금액 ${won(d.notice.extra)}: ${LABEL.feeResponse[d.notice.response] || d.notice.response}`) : null,
      d.noPhotoReason ? h('p', {}, `사진 불가 사유: ${LABEL.noPhoto[d.noPhotoReason] || d.noPhotoReason}`) : null,
      Object.values(d.stickers).some((x) => x.result !== 'OK') ? h('p', {}, '스티커·필증 문제 품목 있음') : null) : null,
    btn('품목 수량을 모두 실음으로 채우기', async () => { await fillAll(); render(); }, 'ghost link'),
    msg, fieldMemo(stop, d));

  const manage = isSolo() ? h('section', { class: 'card' }, h('h2', {}, '접수 관리'),
    h('p', { class: 'small' }, `원본 ${stop.req_ver}번째 버전 · 수정 ${(stop.versions || []).length}회`),
    h('div', { class: 'grid2' },
      btn('접수 수정', () => { ui.form = null; go(`#/intake/${id}`); }, 'opt', { disabled: stop.status === 'CANCELLED' || purged }),
      btn('접수 취소', async () => {
        const r = await sheet({ title: '접수 취소', text: '취소 사유를 적어 주세요. 기록은 지워지지 않습니다.', input: { label: '취소 사유', placeholder: '예: 주민이 직접 처리', required: '취소 사유를 적어 주세요.' },
          choices: [{ label: '취소로 기록', value: true, cls: 'danger', needInput: true }], cancel: '돌아가기' });
        if (!r || !r.text) return;
        stop.status = 'CANCELLED'; stop.cancel_reason = r.text; stop.cancelled_at = new Date().toISOString();
        await commit(); toast('취소로 기록했습니다'); render();
      }, 'opt', { disabled: stop.status === 'CANCELLED' })),
    (stop.versions || []).length ? h('ul', { class: 'small list' }, ...stop.versions.slice().reverse().map((v) => h('li', {}, `${v.at.slice(0, 16).replace('T', ' ')} · ${v.reason}`))) : null) : pcManage(stop, purged);
  // 아래 고정 영역: 장갑 낀 손으로도 누르기 쉬운 전화·다음 지점
  const seqList = (isSolo() ? st.requests.filter((r) => r.planned_date === stop.planned_date && r.status !== 'CANCELLED').sort((a, b) => a.seq - b.seq)
    : pcStops().filter((x) => !x.removed && !(x.field && x.planned_date !== pcWorkDate())));
  const next = seqList.slice(seqList.indexOf(stop) + 1).find((x) => deriveStop(x, st.events).progress !== 'DONE');
  // 결과를 저장하기 전에는 "결과 저장 ↓", 저장한 뒤에는 "다음 N번 ▶" — 기록 없이 건너뛰지 않게
  // 길안내는 전화 옆: 결과 저장 전에는 이 지점, 저장한 뒤에는 다음 미완료 지점
  const saved = d.outcome || stop.status === 'CANCELLED';
  const nextLoc = next ? deriveStop(next, st.events).loc : null;
  const dock = h('nav', { class: 'dock four', 'aria-label': '빠른 동작' },
    btn('←', () => go('#/home'), 'dock-back', { 'aria-label': '목록으로' }),
    btn('전화', () => dial(stop), 'dock-call', { disabled: purged || !stop.phone }),
    saved ? (next ? navBtn('다음 길안내', next, nextLoc, NAV.navLabel(next.seq), 'dock-nav') : h('span'))
      : navBtn('길안내', stop, d.loc, NAV.navLabel(stop.seq), 'dock-nav'),
    saved
      ? btn(next ? `다음 ${next.seq}번 ▶` : '목록으로', () => go(next ? `#/stop/${next.request_id}` : '#/home'), 'primary dock-next')
      : btn('결과 저장 ↓', () => document.getElementById('st-result').scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' }), 'primary dock-next'));
  // 단계 표시: 연락 → 품목 → 사진 → 결과. 누르면 그 단계로 이동, 끝난 단계는 ✓
  contact.id = 'st-contact'; lines.id = 'st-items'; photos.id = 'st-photos'; outcome.id = 'st-result';
  const itemsDone = d.lines.length > 0 && st.events.some((e) => e.request_id === stop.request_id && e.kind === 'ITEM_RESULT');
  const steps = [['st-contact', '연락', d.contact !== 'NOT_CONTACTED'], ['st-items', '품목', itemsDone], ['st-photos', '사진', d.photos.length > 0], ['st-result', '결과', !!d.outcome]];
  const nextStep = d.outcome ? null : steps.find(([, , done]) => !done);
  const stepNav = h('nav', { class: 'steps', 'aria-label': '처리 단계' }, ...steps.map(([sid, label, done], i) =>
    h('a', { href: '#', class: 'step' + (done ? ' done' : '') + (nextStep && nextStep[0] === sid ? ' next' : ''), 'aria-current': nextStep && nextStep[0] === sid ? 'step' : null,
      onclick: (e) => { e.preventDefault(); document.getElementById(sid).scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' }); } },
      h('span', { class: 'n' }, done ? '✓' : String(i + 1)), h('span', {}, label))));
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, `${stop.seq}번`),
      h('span', { class: `chip state p-${d.outcome || d.progress}` }, stop.status === 'CANCELLED' ? '취소' : d.outcome ? LABEL.outcome[d.outcome] : LABEL.progress[d.progress])),
    stepNav, info, contact, lines, fee, photos, outcome, manage, dock);
}

// ------------------------------------------------------------ PC 연결 방식: 접수 변경 요청 (정정·취소). PC가 원본이라 바로 바꾸지 않고 사무실 확인을 거친다.
const FIX_LABEL = { name: '이름', phone: '연락처', address: '주소', spot_note: '상세 위치', planned_date: '예정일' };
const CANCEL_REQ = [['RESIDENT', '주민 취소'], ['DUPLICATE', '중복 접수'], ['ITEM_REFUSED', '품목 불가'], ['OUT_OF_AREA', '관할 밖'], ['OTHER', '기타']];
function pcManage(stop, purged) {
  const id = stop.request_id;
  const reqs = changeReqs(id);
  const state = (e) => (e.seq > st.releasedSeq ? '보내기 전 (결과 보내기 때 함께)' : 'PC로 보냄 · 사무실 확인 뒤 새 목록에 반영');
  const text = (e) => {
    if (e.kind === 'CANCEL_REQUEST') return `취소 요청 · ${(CANCEL_REQ.find(([k]) => k === e.body.code) || [, e.body.code])[1]}${e.body.text ? ` (${e.body.text})` : ''}`;
    const f = e.body.fields || {};
    const keys = Array.isArray(f) ? f : Object.keys(f);
    return '정정 요청 · ' + keys.map((k) => (Array.isArray(f) ? FIX_LABEL[k] || k : `${FIX_LABEL[k] || k} → ${k === 'phone' ? fmtPhone(f[k]) : f[k] || '(비움)'}`)).join(', ');
  };
  const cancelReq = async () => {
    const r = await sheet({ title: '취소 요청', text: '주민이 취소했거나 수거할 수 없는 건이면 사유를 골라 PC로 요청합니다. PC 담당자가 확인한 뒤 취소되며, 기록은 지워지지 않습니다.',
      input: { label: '내용 (기타는 필수)', placeholder: '예: 주민이 직접 처리함', required: '기타 사유를 적어 주세요.' },
      choices: CANCEL_REQ.map(([v, l]) => ({ label: l, value: v, cls: v === 'RESIDENT' ? 'danger' : '', needInput: v === 'OTHER' })), cancel: '돌아가기' });
    if (!r || !r.value) return;
    await addEvent(stop, 'CANCEL_REQUEST', { code: r.value, text: (r.text || '').slice(0, 200) });
    toast('취소 요청을 남겼습니다. 결과 보내기 때 PC로 갑니다'); render();
  };
  return h('section', { class: 'card' }, h('h2', {}, '접수 변경 요청'),
    h('p', { class: 'small' }, 'PC가 원본입니다. 주민에게 들은 바뀐 연락처·주소·날짜나 취소를 요청으로 보내면, 사무실이 확인해 반영하고 다음 목록에 나옵니다.'),
    fieldEditable(id) ? h('div', {}, h('p', { class: 'small' }, '아직 PC로 보내지 않은 접수라 바로 고칠 수 있습니다.'),
      btn('접수 고치기 (보내기 전)', () => { ui.form = null; go(`#/intake/${id}`); }, 'primary big')) : null,
    h('div', { class: 'grid2' },
      btn('정정 요청', () => { ui.fix = null; go(`#/fix/${id}`); }, 'opt', { disabled: purged || stop.status === 'CANCELLED' || !!fieldEditable(id) }),
      btn('취소 요청', cancelReq, 'opt', { disabled: stop.status === 'CANCELLED' || cancelAsked(id) })),
    reqs.length ? h('ul', { class: 'small list' }, ...reqs.map((e) => {
      const np = e.kind === 'CORRECTION' && e.body.fields && !Array.isArray(e.body.fields) ? e.body.fields.phone : null;
      return h('li', {}, text(e), h('br'), h('span', { class: 'muted' }, state(e)),
        np ? btn(`새 번호로 전화 ${fmtPhone(np)}`, async () => { await addEvent(stop, 'DIAL_INTENT', { to: 'CORRECTION' }); ui.afterCall = id; handoff(); openExternal('tel:' + np); }, 'opt call-new') : null);
    })) : null);
}
function screenFix(id) {
  const stop = findStop(id);
  if (!stop || stop.pii_purged) { go('#/home'); return h('main'); }
  const cur = { name: stop.name || '', phone: digits(stop.phone), address: stop.address || '', spot_note: stop.spot_note || '', planned_date: stop.planned_date || pcWorkDate() };
  if (!ui.fix || ui.fix.id !== id) ui.fix = { id, ...cur, phone: fmtPhone(cur.phone), reason: '' };
  const f = ui.fix;
  const fld = (key, label, attrs = {}) => {
    const input = h('input', { class: 'search', autocomplete: 'off', value: f[key], ...attrs });
    input.addEventListener('input', () => { f[key] = input.value; });
    return h('label', { class: 'fld' }, h('span', {}, label), input);
  };
  const msg = h('p', { class: 'msg' });
  const save = btn('정정 요청 남기기', async () => {
    const nv = { name: f.name.trim(), phone: digits(f.phone), address: f.address.trim(), spot_note: f.spot_note.trim(), planned_date: f.planned_date };
    if (!nv.name) return (msg.textContent = '이름을 적어 주세요.');
    if (!/^0\d{8,10}$/.test(nv.phone)) return (msg.textContent = '연락처를 확인해 주세요. (0으로 시작하는 9~11자리)');
    if (!nv.address) return (msg.textContent = '주소를 적어 주세요.');
    if (!nv.planned_date) return (msg.textContent = '예정일을 골라 주세요.');
    const fields = Object.fromEntries(Object.entries(nv).filter(([k, v]) => v !== cur[k]));
    if (!Object.keys(fields).length) return (msg.textContent = '바뀐 내용이 없습니다.');
    if (!f.reason.trim()) return (msg.textContent = '요청 사유를 적어 주세요. (예: 주민이 번호가 바뀌었다고 알려 줌)');
    await addEvent(stop, 'CORRECTION', { fields, reason: f.reason.trim().slice(0, 200) });
    ui.fix = null; toast('정정 요청을 남겼습니다. 결과 보내기 때 PC로 갑니다'); go(`#/stop/${id}`);
  }, 'primary huge');
  return h('main', {},
    h('header', { class: 'bar' }, btn('← 취소', () => { ui.fix = null; go(`#/stop/${id}`); }, 'ghost'), h('h1', { class: 'bar-title' }, `정정 요청 ${stop.seq}번`)),
    h('section', { class: 'card notice' }, h('p', {}, '바뀐 칸만 고치세요. 이 휴대폰의 목록은 그대로이고, PC 담당자가 확인해 원본을 고치면(이전 내용은 이력으로 남음) 다음 목록에 반영됩니다.')),
    h('section', { class: 'card' },
      fld('name', '이름'), fld('phone', '연락처', { type: 'tel', inputmode: 'tel', placeholder: '010-0000-0000' }), fld('address', '주소'),
      fld('spot_note', '상세 배출 위치', { placeholder: '예: 101동 분리수거장 옆' }), fld('planned_date', '수거 예정일', { type: 'date' })),
    h('section', { class: 'card' }, fld('reason', '요청 사유 (필수)', { placeholder: '예: 주민이 다음 주로 미뤄 달라고 함' })),
    h('div', { class: 'cta' }, msg, save));
}

// 현장 메모: 짧게 남기고 고치지 않는다(새 메모를 더함). 결과 파일로 PC에 전달.
// 적는 중인 글은 화면을 다시 그려도(사진 저장·통화 뒤 등) 남는다: ui.memo[지점]에 두고, 남기면 비운다(메모리에만, 잠그면 사라짐).
function fieldMemo(stop, d) {
  const id = stop.request_id;
  ui.memo = ui.memo || {};
  const ta = h('textarea', { rows: 2, placeholder: '현장 메모 (예: 현관 앞 아님, 지하주차장 입구에 배출)', 'aria-label': '현장 메모' });
  ta.value = ui.memo[id] || '';
  ta.addEventListener('input', () => { if (ta.value) ui.memo[id] = ta.value; else delete ui.memo[id]; });
  return h('div', { class: 'memo-box' },
    (d.memos || []).length ? h('ul', { class: 'small list' }, ...d.memos.map((m) => h('li', {}, m.text))) : null,
    ta, btn('메모 남기기', async () => { const t = ta.value.trim(); if (!t) return; await addEvent(stop, 'FIELD_MEMO', { text: t.slice(0, 300) }); delete ui.memo[id]; toast('메모를 남겼습니다'); render(); }, 'opt'));
}

// 통화로 약속한 재방문 날짜·시간: PC에서는 그 날짜 노선 후보와 방문 시간창이 된다
function revisitForm(stop, d) {
  const id = stop.request_id;
  ui.revisit = ui.revisit || {};
  const draft = ui.revisit[id] || {};  // 고르는 중인 날짜·시간(다시 그려도 유지)
  const date = h('input', { type: 'date', value: draft.date ?? (d.appt ? d.appt.date : todayKST(1)) });
  const start = h('input', { type: 'time', value: draft.start ?? (d.appt ? d.appt.start || '' : '') });
  const end = h('input', { type: 'time', value: draft.end ?? (d.appt ? d.appt.end || '' : '') });
  for (const [k, el] of [['date', date], ['start', start], ['end', end]]) for (const t of ['input', 'change']) el.addEventListener(t, () => { ui.revisit[id] = { ...(ui.revisit[id] || {}), [k]: el.value }; });
  // 다시 그려도(사진 저장 등) 펼친 상태를 유지
  return h('details', { class: 'revisit', open: ui.openAppt === stop.request_id, ontoggle: (e) => { ui.openAppt = e.target.open ? stop.request_id : false; } },
    h('summary', {}, d.appt ? '재방문 약속 바꾸기' : '재방문 약속 기록'),
    h('div', { class: 'grid2' }, h('label', { class: 'fld' }, h('span', {}, '날짜'), date), h('div', {})),
    h('div', { class: 'grid2' }, h('label', { class: 'fld' }, h('span', {}, '부터'), start), h('label', { class: 'fld' }, h('span', {}, '까지'), end)),
    btn('약속 저장', async () => {
      if (!date.value) return toast('날짜를 골라 주세요', 'bad');
      if (start.value && end.value && start.value >= end.value) return toast('시간 범위를 확인해 주세요', 'bad');
      await addEvent(stop, 'REVISIT_APPT', { date: date.value, start: start.value || null, end: end.value || null });
      if (isSolo()) { // 단독 방식: 원본 일정도 바꾸고 이전 일정은 버전으로 남김
        const { versions = [], ...prev } = stop;
        versions.push({ req_ver: stop.req_ver, at: new Date().toISOString(), reason: '통화로 재방문 약속', snapshot: prev });
        Object.assign(stop, { versions, req_ver: stop.req_ver + 1, planned_date: date.value, eta: start.value || '', appt: start.value ? [start.value, end.value || null] : null, seq: nextSeq(date.value, stop.request_id) });
        await commit();
      }
      ui.openAppt = false; delete ui.revisit[id]; toast('재방문 약속을 저장했습니다'); render();
    }, 'primary big'));
}

// 사진 저장(거의 한 덩어리로): 줄인 사진 바이트·해시·사진 ID를 대기 항목(meta)에 먼저 만들어 두고, 다시 시도해도 같은 ID를 쓴다.

export {
  CANCEL_REQ, FIX_LABEL, arrive, evidence, feeNotice, fieldMemo, hhmm, loadGuide,
  pcManage, revisitForm, screenFix, screenStop, smsSheet, stickerCheck,
};
