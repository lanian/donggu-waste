// 휴대폰 화면: 목록 화면 부품(홈·단독 홈 공용): 접수 카드·검색·동 거르기·이동 순서·배너
import * as F from '../field.js';
import * as L from '../load.js';
import * as S from '../store.js';
import { LABEL } from '../fees.js';
import { deriveStop } from '../fees.js';
import { matchText } from '../fees.js';
import * as NAV from '../nav.js';
import { END_OF_DAY_H, ago, ask, btn, cancelAsked, commit, dial, dongOf, flushPhotos, go, h, isSolo, outbox, pendingPhotos, render, st, toast, ui, workerName } from '../core.js';
import { CHECKS, doInstall, installEvt, navBtn, runningAsApp } from '../widgets.js';

// 수량 입력만 끝난 상태와 방문 결과까지 저장한 완료를 구분한다.
const isStopDone = (s, d) => !d.resultNeedsConfirmation && (d.outcome ? d.outcome === 'DONE' && d.progress === 'DONE' : s.status === 'DONE');
// 방문 결과를 기록해야 할 곳: 취소·제외·개인정보 정리 아님, 완료 아님, 결과 없음 또는 재확인 필요.
// 홈·단독 홈의 '지금 할 일'과 상세 하단 바의 '다음 N번'이 같은 기준을 쓴다.
const needsResult = (s, d) => !s.removed && s.status !== 'CANCELLED' && !s.pii_purged && !isStopDone(s, d) && (!d.outcome || d.resultNeedsConfirmation);
// 방문 순서 목록에서 지금 지점 다음의 기록할 곳. 뒤쪽에 없으면 앞 번호로 돌아가 찾는다(지금 지점은 빼고).
function nextToVisit(list, current, events) {
  const i = list.indexOf(current);
  const order = i < 0 ? list : [...list.slice(i + 1), ...list.slice(0, i)];
  return order.find((x) => x !== current && needsResult(x, deriveStop(x, events))) || null;
}
// 실은 양 요약: 실은 수량 합/현장 수량 합. 단위가 섞이면 단위별로 "1/4개 · 0/2세트 실음".
function loadedSummary(lines) {
  const groups = new Map();
  for (const l of lines) {
    const label = l.unit_label || '', key = `${l.unit || ''}:${label}`;
    const g = groups.get(key) || { label, got: 0, all: 0 };
    g.got += Math.max(0, Number(l.qty_collected) || 0); g.all += Math.max(0, Number(l.qty_confirmed ?? l.qty) || 0);
    groups.set(key, g);
  }
  const list = [...groups.values()].filter((g) => g.all || g.got);
  if (!list.length) return '';
  return (list.length === 1 ? `${list[0].got}/${list[0].all}` : list.map((g) => `${g.got}/${g.all}${g.label}`).join(' · ')) + ' 실음';
}
// 접수 상태 판정 하나(목록 카드·찾기·단독 목록·상세 머리 칩 공용). 진행(입력한 수량)과 저장한 결과를 같은 글자로 보이지 않는다:
// 결과 없이 수량만 넣었으면(진행 PARTIAL·DONE) "결과 미저장"(경고색)과 실은 양, "일부 수거"는 저장한 결과(outcome PARTIAL)에만 쓴다.
function stopState(s, d) {
  const cancelled = s.status === 'CANCELLED', excluded = cancelled || !!s.removed;
  const review = !excluded && !!d.resultNeedsConfirmation, finished = !excluded && isStopDone(s, d);
  const unsaved = !excluded && !review && !finished && !d.outcome && ['PARTIAL', 'DONE'].includes(d.progress);
  const progress = excluded ? 'CANCELLED' : review || unsaved ? 'STARTED' : finished ? 'DONE' : d.outcome || d.progress;
  const label = s.removed ? '목록에서 제외됨' : cancelled ? '취소' : review ? '결과 재확인' : unsaved ? '결과 미저장'
    : finished ? LABEL.progress.DONE : d.outcome ? LABEL.outcome[d.outcome] : LABEL.progress[d.progress];
  return { label, progress, warn: review || unsaved, excluded, cancelled, review, unsaved, finished, loaded: unsaved ? loadedSummary(d.lines || []) : '' };
}

// 오늘 진행 요약 띠: 완료/전체와 진행 막대 (0%여도 막대 바탕이 보이게)
function daySummary(items) {
  const active = items.filter((x) => !x.s.removed && x.s.status !== 'CANCELLED');
  const n = active.length, done = active.filter((x) => isStopDone(x.s, x.d)).length;
  // 재방문·보류 수는 거르기 탭에 있으므로 여기서는 되풀이하지 않는다
  const pend = active.filter((x) => x.d.fee.pendingCount).length;
  return h('section', { class: 'summary', 'aria-label': '오늘 진행' },
    h('div', { class: 'sum-top', 'aria-hidden': 'true' }, h('span', {}, '오늘 진행'), h('span', { class: 'sum-pct' }, `${n ? Math.round((done / n) * 100) : 0}%`)),
    h('div', { class: 'sum-main' }, h('b', {}, done), h('span', {}, ` / ${n}곳 완료`)),
    h('div', { class: 'progress track', role: 'progressbar', 'aria-label': `완료 ${done}/${n}`, 'aria-valuemin': 0, 'aria-valuemax': n, 'aria-valuenow': done }, h('div', { class: 'fill', style: `width:${n ? (done / n) * 100 : 0}%` })),
    h('div', { class: 'sum-sub' }, pend ? h('span', { class: 'chip warn' }, `금액 확인 대기 ${pend}`) : null));
}
// 지금 할 일이 다음 지점이면 큰 단추 아래 [N번 길안내][N번 전화]. 위치가 없으면 길안내를, 연락처가 없거나 정리됐으면 전화를 끈다.
function nextQuick(s) {
  const loc = deriveStop(s, st.events).loc;
  const nav = NAV.hasCoord(loc) ? navBtn(`${s.seq}번 길안내`, s, loc, NAV.navLabel(s.seq), 'quick-nav')
    : btn([`${s.seq}번 길안내`, h('small', {}, '위치 없음')], null, 'nav-btn no-loc quick-nav', { disabled: true, 'aria-label': `${s.seq}번 길안내, 위치 없음` });
  return h('div', { class: 'next-quick', role: 'group', 'aria-label': `${s.seq}번 바로 가기` }, nav,
    btn(`${s.seq}번 전화`, () => dial(s), 'quick-call', { disabled: !!s.pii_purged || !s.phone }));
}
// 거르기 결과가 없을 때: "'재방문·보류'에 해당하는 곳이 없습니다." + [전체 보기]
// subject: 조사까지 붙인 말('곳이'·'접수가')
function emptyFilter(names, subject, showAll, allLabel = '전체 보기') {
  const what = names.filter(Boolean).map((x) => `'${x}'`).join(' · ') || "'전체'";
  return [h('p', { class: 'empty' }, `${what}에 해당하는 ${subject} 없습니다.`), h('div', { class: 'empty-actions' }, btn(allLabel, showAll, 'big'))];
}
// 목록 찾기: 이름·연락처·주소·품목, 초성과 전화번호 뒷자리도 됨.
function stopSearch(total) {
  const source = st, mode = st.mode, route = location.hash;
  const q = h('input', { type: 'search', class: 'search', id: 'stop-list-search', placeholder: '접수번호 또는 이름', value: ui.stopQ || '', 'aria-label': '목록에서 찾기', 'aria-describedby': 'stop-search-help' });
  const owner = () => st === source && st.mode === mode && location.hash === route;
  q.addEventListener('input', () => {
    if (!owner() || !q.isConnected) return;
    const query = q.value;
    ui.stopQ = query; clearTimeout(stopSearch.t);
    stopSearch.t = setTimeout(async () => {
      if (!owner() || ui.stopQ !== query) return;
      await render();
      if (!owner() || ui.stopQ !== query) return;
      const el = document.querySelector('input[aria-label="목록에서 찾기"]');
      if (el) { el.focus({ preventScroll: true }); el.setSelectionRange(el.value.length, el.value.length); }
    }, 250);
  });
  return h('div', { class: 'stop-search' }, h('label', { for: 'stop-list-search' }, '목록에서 찾기'), q,
    h('p', { class: 'search-help', id: 'stop-search-help' }, '주소·품목·연락처 뒷자리·초성 지원'));
}
const stopHay = (s, d) => {
  const items = (d?.lines || s.items || []).map((i) => `${i.name || ''} ${i.spec || ''}`).join(' ');
  if (s.pii_purged) return `${s.receipt_no || ''} ${items}`;
  const phone = s.pii_purged ? '' : String(s.phone || ''), number = phone.replace(/\D/g, '');
  const formatted = /^02\d{7,8}$/.test(number) ? `${number.slice(0, 2)}-${number.slice(2, -4)}-${number.slice(-4)}`
    : /^\d{10,11}$/.test(number) ? `${number.slice(0, 3)}-${number.slice(3, -4)}-${number.slice(-4)}` : '';
  return `${s.name || ''} ${s.address || ''} ${s.dong || ''} ${s.spot_note || ''} ${s.receipt_no || ''} ${phone} ${number} ${formatted} ${items}`;
};
const filterQ = (list) => (ui.stopQ ? list.filter((x) => matchText(stopHay(x.s, x.d), ui.stopQ)) : list);

// 동(행정동): 2개 동 이상이면 거르기 단추, 노선 시간표는 동이 바뀌는 곳에 머리글
function dongChips(items) {
  const ds = [...new Set(items.map((x) => dongOf(x.s)).filter(Boolean))];
  if (ds.length < 2) { ui.dong = ''; return null; }
  if (ui.dong && !ds.includes(ui.dong)) ui.dong = '';
  return h('div', { class: 'tabs dongs', role: 'group', 'aria-label': '동 거르기' },
    btn('모든 동', () => { ui.dong = ''; render(); }, !ui.dong ? 'tab on' : 'tab'),
    ...ds.map((dg) => btn(`${dg} ${items.filter((x) => dongOf(x.s) === dg).length}`, () => { ui.dong = dg; render(); }, ui.dong === dg ? 'tab on' : 'tab')));
}
const filterDong = (list) => (ui.dong ? list.filter((x) => dongOf(x.s) === ui.dong) : list);

// 방문 카드 한 장 (PC 목록·단독 목록 공용). 개인정보를 지운 곳은 접수번호만 보인다.
function stopCard(s, d, { nowId = null, solo = false, showDate = false } = {}) {
  const purged = !!s.pii_purged;
  const { label: state, progress, warn, excluded, finished, loaded } = stopState(s, d);
  const lines = d.lines || s.items;
  const remaining = new Map();
  for (const line of lines) {
    const qty = Math.max(0, line.remaining ?? (line.qty_confirmed ?? line.qty) - (line.qty_collected || 0));
    if (!qty) continue;
    const label = line.unit_label || '', key = `${line.unit || ''}:${label}`;
    const previous = remaining.get(key);
    remaining.set(key, { label, qty: (previous?.qty || 0) + qty });
  }
  const left = [...remaining.values()].map((x) => `${x.qty}${x.label}`).join(' · ') || '없음';
  const changes = (st.listChanges || {})[s.request_id] || [];
  const isNow = !excluded && s.request_id === nowId;
  return h('a', { href: `#/stop/${s.request_id}`, class: `stop p-${progress} c-${d.contact}` + (finished || excluded ? ' done' : '') + (isNow ? ' now' : '') + (changes.length ? ' changed' : ''), 'aria-current': isNow ? 'step' : null },
    h('span', { class: 'seq' }, s.seq, isNow ? h('small', { class: 'now-tag' }, '지금') : null),
    h('span', { class: 'body' },
      h('span', { class: 'stop-heading' }, h('strong', {}, purged ? '개인정보 정리됨' : s.name),
        h('em', { class: 'chip state stop-state ' + (warn ? 'warn' : 'p-' + progress) }, state)),
      loaded ? h('span', { class: 'stop-loaded' }, loaded) : null,
      h('span', { class: 'stop-meta' }, h('span', { class: 'receipt' }, s.receipt_no ? `접수 ${s.receipt_no}` : '접수번호 없음'),
        s.eta ? h('span', { class: 'eta' }, `예정 ${s.eta}`) : null),
      !purged ? h('span', { class: 'addr' }, s.address) : null,
      h('span', { class: 'items' }, lines.map((i) => `${i.name} ${i.spec || ''} ${i.qty_confirmed ?? i.qty}${i.unit_label || ''}`.trim()).join(', ')),
      !excluded && !finished ? h('span', { class: 'stop-quantity' + (d.outcome === 'PARTIAL' || s.revisit ? ' warn' : '') }, `남음 ${left}`) : null,
      h('span', { class: 'chips' },
        ...changes.map((c) => h('em', { class: 'chip new' }, F.DIFF_LABEL[c] || c)),
        showDate ? h('em', { class: 'chip' }, s.planned_date) : null,
        !purged && dongOf(s) ? h('em', { class: 'chip dong' }, dongOf(s)) : null,
        ...(excluded || finished ? [] : L.tagsOf(lines.map((line) => ({ ...line, qty: line.qty_confirmed ?? line.qty }))).map((t) => h('em', { class: `chip load ld-${t}`, title: L.TAGS[t].hint }, L.TAGS[t].label))),
        !solo && s.revisit ? h('em', { class: 'chip warn' }, '재방문') : null,
        !solo && s.field ? h('em', { class: 'chip new' }, `${s.channel || '현장'} 접수`) : null,
        !solo && cancelAsked(s.request_id) ? h('em', { class: 'chip warn' }, '취소 요청') : null,
        h('em', { class: 'chip c-' + d.contact }, LABEL.contact[d.contact]),
        d.fee.pendingCount ? h('em', { class: 'chip warn' }, '금액 확인 대기') : null)));
}
// 노선 시간표: 동이 바뀌는 곳에 동 이름 머리글
function routeItems(list, renderOne, heads = {}) {
  const anyDong = list.some((x) => dongOf(x.s));
  const out = [];
  let prev = null;
  for (const x of list) {
    const dg = dongOf(x.s);
    if (heads[x.s.request_id]) out.push(heads[x.s.request_id]);
    if (anyDong && !ui.dong && dg !== prev) out.push(h('li', { class: 'dong-head' }, h('span', {}, dg || '동 미지정')));
    prev = dg;
    out.push(renderOne(x));
  }
  return out;
}
// 운행(반입지 하차 사이)마다 머리글: 실을 양(추정)과 큰 짐·유리 위치. 하차가 없고 알릴 것도 없으면 생략.
function tripHeads(stops) {
  const live = stops.filter((x) => !x.s.removed && x.s.status !== 'CANCELLED');
  const trips = L.tripsOf(live.map((x) => ({ seq: x.s.seq, id: x.s.request_id, items: x.s.items, unload_after: x.s.unload_after })));
  const out = {};
  const multi = trips.length > 1;
  trips.forEach((t) => {
    const adv = L.tripAdvice(t);
    if (!multi && !adv.length) return;
    out[t.fromId] = h('li', { class: 'trip-head' },
      h('strong', {}, multi ? `${t.no}회차 · ${t.from}~${t.to}번` : `싣기 안내 · ${t.from}~${t.to}번`),
      h('span', { class: 'small' }, ` 약 ${t.m3}m³ · ${t.kg.toLocaleString()}kg (추정)`),
      ...adv.map((a) => h('span', { class: 'small adv' }, a)));
  });
  return out;
}

// 개인폰은 작업자를 고르지 않고 현재 이름만 확인한다. 변경은 설정에서 한다.
function workerBar() {
  const name = workerName();
  return name ? h('p', { class: 'small', role: 'group', 'aria-label': '현재 작업자' }, '작업자 ', h('strong', {}, name), ' · ', h('a', { href: '#/settings' }, '이름 변경')) : null;
}
const kstHour = () => Number(new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', hour: '2-digit', hour12: false }).format(new Date()));
async function batteryInfo() {
  try {
    if (!navigator.getBattery) return null;
    return await Promise.race([navigator.getBattery(), new Promise((r) => setTimeout(() => r(null), 300))]);
  } catch { return null; }
}
// 처음 설정(기기 등록 뒤 또는 단독 방식): 작업자 · 길안내 앱 · 지도 받기 · 현장 점검. 끝난 것 ✓, 모두 끝나면(또는 [닫기]) 숨김.
function setupSteps() {
  const chk = st.deviceCheck || {};
  return [
    ['worker', '작업자', '이 휴대폰을 쓰는 사람 이름', !!workerName(), '#/settings'],
    ['nav', '길안내 앱', '카카오맵·T맵·네이버지도·기본 지도 중 고르기', !!st.navAppSet, '#/settings'],
    ['map', '지도 받기', '인터넷이 약한 곳에 대비해 동구 지도 미리 받기', !st.mapEnabled || !!st.mapPrefetchedAt, '#/settings'],
    ['check', '현장 점검', '길안내·카메라·전화가 이 휴대폰에서 되는지', CHECKS.every(([k]) => chk[k] && chk[k].result), '#/check'],
  ];
}
function setupCard() {
  if (st.setupHidden) return null;
  const steps = setupSteps();
  const done = steps.filter((x) => x[3]).length;
  if (done === steps.length) return null;
  return h('section', { class: 'card setup-card', 'aria-label': '처음 설정' },
    h('h2', {}, `처음 설정 ${done}/${steps.length}`),
    h('ul', { class: 'setup-list' }, ...steps.map(([k, t, hint, ok, href]) => h('li', { class: 'setup-' + k + (ok ? ' done' : '') },
      h('span', { class: 'tick', 'aria-hidden': 'true' }, ok ? '✓' : '○'),
      h('a', { href, 'aria-label': `${t}${ok ? ' (끝남)' : ''}` }, h('strong', {}, t), h('span', { class: 'small' }, ' ' + hint))))),
    btn('나중에 (닫기)', async () => { st.setupHidden = true; await commit(); render(); }, 'ghost link'));
}
// 홈 위 알림(지금 할 일 아래): 저장 대기 사진, 자료 보존 거절, 설치 안내, 보낼 기록(PC), 배터리, 보관 기간(단독).
// 주 단추(primary)는 두지 않는다 — 화면의 주 행동은 '지금 할 일' 하나다.
async function homeBanners() {
  const out = [];
  if (pendingPhotos.length) out.push(h('section', { class: 'card callout warn pending-photo', role: 'alert' },
    h('p', {}, h('strong', {}, `저장 대기 사진 ${pendingPhotos.length}장`), ' — 아직 암호화 저장되지 않았습니다. 앱을 닫지 마세요.'),
    btn('사진 다시 저장', async () => { await flushPhotos(); render(); }, 'big')));
  if (st.storagePersisted === false && !st.persistHintOff) out.push(h('section', { class: 'card callout warn persist-banner', role: 'status' },
    h('p', {}, h('strong', {}, '자료 보존이 허락되지 않았습니다.'), ' 휴대폰 저장 공간이 모자라면 브라우저가 이 앱의 자료(보낼 기록·사진)를 지울 수 있습니다.'),
    h('p', { class: 'small' }, '앱을 홈 화면에 설치하고, 결과를 자주 보내 주세요. 설치 뒤 [다시 요청]을 누르면 허락될 수 있습니다.'),
    h('div', { class: 'grid2' }, btn('다시 요청', async () => { const ok = await S.persist(); st.storagePersisted = !!ok; await commit(); toast(ok ? '자료 보존이 허락되었습니다' : '아직 허락되지 않았습니다. 앱을 설치한 뒤 다시 눌러 보세요.', ok ? '' : 'bad'); render(); }, 'big'),
      btn('알겠습니다', async () => { st.persistHintOff = true; await commit(); render(); }, 'ghost'))));
  if (!runningAsApp() && installEvt && !st.installHintOff) out.push(h('section', { class: 'card callout info install-hint' },
    h('p', {}, '지금은 브라우저에서 실행 중입니다. 앱으로 설치하면 홈 화면 아이콘으로 바로 엽니다(자료는 그대로).'),
    h('div', { class: 'grid2' }, btn('앱 설치', doInstall, 'big'), btn('다음에', async () => { st.installHintOff = true; await commit(); render(); }, 'ghost'))));
  // 보낼 기록 경고는 '보낼 기록'(어느 파일에도 없음) 중 가장 오래된 것으로 잰다. 다 보냈고 D-코드만 남았으면 조용한 안내로.
  const u = F.unsentInfo(st.events, st.releasedSeq), ob = outbox();
  const late = kstHour() >= END_OF_DAY_H, limit = st.unsentWarnH || 3;
  if (!isSolo() && ob.toSend && (ob.ageH >= limit || late)) out.push(h('section', { class: 'callout warn band unsent-banner', role: 'status' },
    h('p', {}, '⚠ ', h('strong', {}, `${late ? '퇴근 전 ' : ''}보낼 기록 ${ob.toSend}건`), ` · 가장 오래된 기록 ${ago(ob.oldestTs)}`),
    h('a', { href: '#/export', class: 'callout-link' }, '보내기 ›')));
  else if (!isSolo() && !ob.toSend && ob.waiting && (u.ageH >= limit || late)) out.push(h('section', { class: 'callout info band waiting-note', role: 'status' },
    h('p', {}, `확인 대기 ${ob.waiting}건 — PC의 D-코드를 넣으세요`),
    h('a', { href: '#/export', class: 'callout-link' }, '코드 넣기 ›')));
  const bat = await batteryInfo();
  if (bat && bat.level < 0.2 && !bat.charging && u.count) {
    out.push(h('section', { class: 'card callout warn battery', role: 'status' },
      h('p', {}, h('strong', {}, `배터리 ${Math.round(bat.level * 100)}%`), isSolo() ? ' — 지금 백업하세요' : ' — 결과를 지금 보내세요'),
      btn(isSolo() ? '지금 백업' : '결과 보내기', () => go(isSolo() ? '#/backup' : '#/export'), 'big')));
  }
  if (isSolo()) {
    const cand = F.retentionCandidates(st.requests, st.events, st.retentionDays || 90, (r) => {
      const d = deriveStop(r, st.events);
      return r.status === 'CANCELLED' || d.progress === 'DONE' && !d.resultNeedsConfirmation;
    });
    if (cand.length) {
      const ready = cand.filter((c) => st.lastBackupAt && Date.parse(st.lastBackupAt) > c.doneAt);
      out.push(h('section', { class: 'card callout warn retention', role: 'status' },
        h('p', {}, h('strong', {}, `보관 기간(${st.retentionDays || 90}일)이 지난 처리 끝난 접수 ${cand.length}건`)),
        ready.length ? h('p', { class: 'small' }, `백업이 끝난 ${ready.length}건의 이름·연락처·주소를 지울 수 있습니다. 접수번호·품목·수거 기록은 남습니다.`)
          : h('p', { class: 'small' }, '백업을 먼저 만들어야 개인정보를 지울 수 있습니다.'),
        ready.length ? btn(`${ready.length}건 개인정보 지우기`, async () => {
          if (!(await ask(`${ready.length}건의 이름·연락처·주소·메모를 이 휴대폰에서 지웁니다. 되돌릴 수 없습니다. 마지막 백업(${ago(st.lastBackupAt)})에는 남아 있습니다.`, '지우기', { danger: true }))) return;
          const at = new Date().toISOString();
          const ids = new Set(ready.map((c) => c.r.request_id));
          for (const c of ready) F.purgeStopPII(c.r, at);
          for (const e of st.events) if (ids.has(e.request_id)) F.purgeEventPII(e);
          await commit(); toast(`${ready.length}건의 개인정보를 지웠습니다`); render();
        }, 'big') : btn('지금 백업', () => go('#/backup'), 'big')));
    }
  }
  return out;
}

export {
  batteryInfo, daySummary, dongChips, emptyFilter, filterDong, filterQ, homeBanners, isStopDone, kstHour, loadedSummary, needsResult, nextQuick, nextToVisit, routeItems,
  setupCard, setupSteps, stopCard, stopHay, stopSearch, stopState, tripHeads, workerBar,
};
