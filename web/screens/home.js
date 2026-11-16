// 휴대폰 화면: 오늘 목록(홈)·배너·설정 단계
import * as NAV from '../nav.js';
import * as S from '../store.js';
import { deriveStop } from '../fees.js';
import { appLogo, btn, commit, flushSteps, go, h, isSolo, lockNow, mdOf, outbox, pcStops, pcWorkDate, pendingPhotos, render, saveFailed, st, tabBar, themeToggle, toast, todayKST, ui } from '../core.js';
import { navBtn, unloadLoc } from '../widgets.js';
import { screenMode } from './mode.js';
import { screenHomeSolo } from './solo.js';
import { openFinder } from './find.js';
import { daySummary, dongChips, emptyFilter, filterDong, filterQ, homeBanners, isStopDone, needsResult, nextQuick, routeItems, setupCard, stopCard, stopSearch, tripHeads, workerBar } from './stoplist.js';

// 화면 순서: 머리글 → 지금 할 일(화면에서 유일한 주 단추) → 경고 띠 → 자주 쓰는 기능(자리 고정) → 진행 요약 → 거르기 → 목록.
async function screenHome() {
  await flushSteps();
  if (!st.mode) return screenMode();
  if (isSolo()) return screenHomeSolo();
  const registered = !!(await S.getRaw('pc')) && !!(await S.getRaw('device'));
  const ob = outbox();  // 화면 표시용: 보낼 기록(어느 파일에도 없음) · 확인 대기(보냈지만 D-코드 전)
  const dl = st.daylist;
  const wd = pcWorkDate();
  const every = pcStops();
  const later = every.filter((s) => s.field && s.planned_date !== wd);  // 다른 날로 받은 현장 접수
  const today = every.filter((s) => !later.includes(s));
  const stops = today.map((s) => ({ s, d: deriveStop(s, st.events) }));
  const top = h('header', { class: 'bar' }, appLogo(), h('h1', { class: 'bar-title' }, '동구 수거'), themeToggle(), btn('잠금', lockNow, 'ghost'));
  ui.scanAck = false;
  const past = !!dl && dl.work_date < todayKST();
  const otherDay = !!dl && dl.work_date !== todayKST();
  const rcpt = st.lastReceipt;
  const showReceipt = rcpt && !rcpt.told && dl && rcpt.work_date === dl.work_date;
  // 안내는 현재 기록만 읽는다. 전송·반영·회수·정리는 각각의 기존 화면에서 사람이 확인한다.
  const waitingResult = st.exports.some((x) => !x.confirmed);
  const recovered = new Set(st.recoveredPhotos || []);
  const photos = [...st.events.filter((e) => e.kind === 'PHOTO_META').map((e) => e.body), ...(st.keptPhotos || [])];
  const left = photos.filter((p) => !recovered.has(p.photo_id));
  const sentPhotos = new Set((st.photoExports || []).flatMap((x) => x.ids));
  // 수량을 다 입력해도 방문 결과를 저장하지 않았으면 상세로 이어 간다.
  const unrecorded = stops.filter(({ s, d }) => needsResult(s, d));
  const reconfirm = unrecorded.find(({ d }) => d.resultNeedsConfirmation);
  const nextStop = unrecorded[0];
  const sendDetail = `보낼 기록 ${ob.toSend}건${ob.waiting ? ` · 확인 대기 ${ob.waiting}건` : ''}`;
  let next = { key: 'scan', title: '오늘 목록을 받으세요', detail: 'PC 담당자가 준비한 QR을 읽고, 받은 뒤 수신 코드를 알려 주세요.', label: '오늘 목록 받기', run: () => go('#/scan') };
  if (pendingPhotos.length) next = {
    key: 'photo', title: `사진 ${pendingPhotos.length}장이 아직 저장 대기 중입니다`, detail: '앱을 닫지 말고 아래의 [사진 다시 저장]으로 저장을 마쳐 주세요.', label: '대기 사진 저장 안내',
    run: () => { const el = document.querySelector('.pending-photo'); if (el) { el.scrollIntoView({ block: 'center' }); el.querySelector('button')?.focus({ preventScroll: true }); } },
  };
  else if (saveFailed || S.hasPendingSecure('state')) next = {
    key: registered ? 'send' : 'backup', title: '저장하지 못한 기록부터 보호하세요',
    detail: registered ? '앱을 닫지 말고 [결과 보내기]로 이 창의 기록을 먼저 보내 주세요. 저장 실패 알림은 실제 저장이 끝나야 사라집니다.'
      : '앱을 닫지 말고 이 창의 기록을 암호화 백업으로 보관하세요. 저장 실패 알림은 실제 저장이 끝나야 사라집니다.',
    label: registered ? '결과 보내기' : '기록 보호용 백업', run: () => go(registered ? '#/export' : '#/backup'),
  };
  else if (reconfirm) next = { key: 'stop', title: `${reconfirm.s.seq}번 수거 결과를 재확인하세요`, detail: '결과 저장 뒤 수량이 바뀌었습니다. 현재 수량에 맞는 결과를 다시 선택해 저장하세요.', label: `${reconfirm.s.seq}번 결과 재확인`, run: () => go(`#/stop/${reconfirm.s.request_id}`) };
  else if (!registered) next = { key: 'register', title: '관리자와 기기를 연결하세요', detail: '오늘 목록 받기와 결과 보내기에 필요한 등록입니다. 현장·전화 접수는 아래에서 먼저 받을 수 있습니다.', label: '기기 등록', run: () => go('#/register') };
  else if (past && (ob.toSend || ob.waiting || waitingResult)) next = {
    key: 'send', title: '지난 목록의 결과부터 마무리하세요', detail: `${mdOf(dl.work_date)} 목록의 기록이 남아 있습니다(${sendDetail}). 결과를 보내고 결과 반영 코드(D-)를 넣은 뒤 오늘 목록을 받으세요.`,
    label: ob.toSend ? '결과 보내기' : '결과 반영 코드 넣기', run: () => go('#/export'),
  };
  else if (otherDay) next.detail = `${mdOf(dl.work_date)} 목록을 보고 있습니다. 오늘 수거로 이어가기 전에 오늘 업무용 목록을 받으세요.`;
  else if (showReceipt) next = {
    key: 'receipt', title: '받은 목록의 수신 코드를 알려 주세요', detail: 'PC 담당자가 R-코드를 받아 적었는지 확인하세요. 알렸음 처리는 아래 수신 카드에서 직접 합니다.', label: '수신 코드 확인하기', run: () => { ui.receipt = null; go('#/received'); },
  };
  else if (nextStop) next = {
    key: 'stop', stop: nextStop.s, title: `${nextStop.s.seq}번 방문 결과를 이어서 기록하세요`, detail: `방문 결과 미기록 ${unrecorded.length}곳 · 휴대폰 방문 순서 기준`,
    label: `${nextStop.s.seq}번 수거 이어가기`, run: () => go(`#/stop/${nextStop.s.request_id}`),
  };
  else if (ob.toSend) next = {
    key: 'send', title: '기록한 결과를 PC로 보내세요', detail: `${sendDetail}. 결과 파일을 보낸 뒤 PC 담당자가 알려 주는 결과 반영 코드(D-)를 넣으세요. 사진은 따로 보냅니다.`,
    label: '결과 보내기', run: () => go('#/export'),
  };
  else if (ob.waiting || waitingResult) next = {
    key: 'send', title: 'PC의 결과 반영 코드를 넣으세요', detail: `${ob.waiting ? `확인 대기 ${ob.waiting}건 · ` : ''}PC 담당자가 결과를 가져온 뒤 알려 주는 D-코드를 넣으면 끝납니다. 파일이 PC에 가지 않았다면 결과 보내기 화면에서 다시 보낼 수 있습니다.`,
    label: '결과 반영 코드 넣기', run: () => go('#/export'),
  };
  else if (left.length) next = left.some((p) => !sentPhotos.has(p.photo_id))
    ? { key: 'photo-send', title: `사진 ${left.length}장을 보내세요`, detail: '사진 파일을 승인된 보안 매체로 PC에 옮기고 사진 회수 코드(P-)를 넣으세요. 파일을 만들기만 해서는 회수가 끝나지 않습니다.', label: '사진 보내기', run: () => go('#/export') }
    : { key: 'photo-send', title: `사진 ${left.length}장의 회수 코드가 남아 있습니다`, detail: 'PC가 사진을 가져온 뒤 알려 주는 사진 회수 코드(P-)를 넣으세요.', label: '사진 회수 코드 넣기', run: () => go('#/export') };
  else if (dl) next = { key: 'cleanup', title: '반영·회수를 확인한 자료를 정리할 수 있습니다', detail: '정리 화면에서 남은 기록을 확인하세요. [오늘 자료 정리]를 직접 누르기 전에는 목록과 기록을 지우지 않습니다.', label: '정리 단계 확인하기', run: () => go('#/export') };
  const out = [top, workerBar()];
  out.push(h('section', { class: 'card next-action', 'aria-label': '지금 할 일' }, h('h2', {}, '지금 할 일'),
    h('p', { class: 'next-title' }, next.title), h('p', { class: 'small next-detail' }, next.detail), btn(next.label, next.run, 'primary huge')));
  if (next.stop && !otherDay) out.push(nextQuick(next.stop));
  // 경고 띠: 지난(또는 다음) 날짜 목록, 수신 코드, 대기 사진·보낼 기록·배터리 등. 주 단추는 두지 않는다(지금 할 일이 맡음).
  if (otherDay) out.push(h('section', { class: 'card callout warn day-banner' + (past ? ' past' : ''), role: 'alert' },
    h('h2', {}, past ? `${mdOf(dl.work_date)} 목록입니다 — 결과를 보내고 오늘 목록을 받으세요` : `${mdOf(dl.work_date)} 목록입니다 — 그날 업무용 목록입니다`)));
  if (showReceipt) out.push(h('section', { class: 'card callout info receipt-card', role: 'status' },
    h('p', {}, h('strong', {}, '수신 코드 '), h('span', { class: 'code-kind' }, 'R-'), h('span', { class: 'rc-code' }, `${rcpt.code.slice(0, 4)} ${rcpt.code.slice(4)}`), ' — PC 담당자에게 알려 주세요'),
    h('div', { class: 'grid2' }, btn('수신 코드 다시 보기', () => { ui.receipt = null; go('#/received'); }, 'big'),
      btn('PC에 알렸음', async () => { st.lastReceipt = { ...rcpt, told: true }; await commit(); render(); }, 'ghost'))));
  out.push(...(await homeBanners()));
  // 자주 쓰는 기능: 늘 같은 자리·같은 단추. 접수 등록(현장·전화)은 기기 등록 전에도 받아 둘 수 있다(PC로 보내는 것만 등록 뒤에)
  const sendTool = ob.toSend ? `결과 보내기 · 보낼 기록 ${ob.toSend}건` : ob.waiting ? `결과 보내기 · 확인 대기 ${ob.waiting}건` : '결과 보내기';
  const stale = (ob.toSend || ob.waiting) && past;
  // 단독 방식과 같은 이름·같은 순서: [+ 접수 등록][날짜·접수 찾기][하차 기록][빠른 수수료 계산] (+ 지도를 켰으면 [지도])
  const unloads = st.events.filter((e) => e.kind === 'UNLOAD').length;
  out.push(h('section', { class: 'actions home-tools', 'aria-label': '자주 쓰는 기능' },
    h('h2', { class: 'section-label' }, '자주 쓰는 기능'), h('div', { class: 'grid2' },
      btn('오늘 목록 받기', () => go('#/scan'), 'big', { disabled: !registered }),
      btn(sendTool, () => go('#/export'), 'big' + (stale ? ' warn' : '')),
      btn('+ 접수 등록', () => { ui.form = null; go('#/intake'); }, 'big intake-btn'), btn('날짜·접수 찾기', openFinder, 'big'),
      btn(unloads ? `하차 기록 (${unloads}회)` : '하차 기록', () => go('#/unload'), 'big'),
      btn('빠른 수수료 계산', () => go('#/calculator'), 'big'),
      st.mapEnabled ? btn('지도', () => go('#/map'), 'big') : null)));
  if (registered) out.push(setupCard());
  out.push(h('h2', { id: 'collection-list', tabindex: '-1' }, '수거 목록'));
  if (!today.length) {
    // 지금 할 일이 같은 [오늘 목록 받기]면 빈 상태에는 문구만 둔다(같은 단추를 세 번 보이지 않게)
    out.push(h('p', { class: 'empty' }, '오늘 목록이 없습니다. PC 담당자의 QR을 받으세요.'),
      next.key === 'scan' ? null : h('div', { class: 'empty-actions' }, btn('오늘 목록 받기', () => go('#/scan'), 'big', { disabled: !registered })));
  } else {
    const active = stops.filter(({ s }) => !s.removed && s.status !== 'CANCELLED' && !s.pii_purged);
    const remain = active.filter(({ s, d }) => !isStopDone(s, d));
    const hold = active.filter(({ s, d }) => !isStopDone(s, d) && (d.resultNeedsConfirmation || ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(d.outcome) || d.contact === 'CALLBACK'));
    const changed = Object.keys(st.listChanges || {}).length;
    const reordered = !!(st.localOrder && st.localOrder.work_date === wd && st.localOrder.assign_ver === (dl ? dl.assign_ver : 0));
    const fieldN = today.filter((s) => s.field).length;
    out.push(h('p', { class: 'meta' }, (dl ? `${dl.work_date} ${dl.crew}` + (dl.assign_ver > 1 ? ` · 목록 ${dl.assign_ver}차` : '') : `${wd} · PC 목록 없음`) + (changed ? ` · 바뀐 곳 ${changed}` : '')
      + (fieldN ? ` · 현장 접수 ${fieldN}` : '') + (reordered ? ' · 순서 바꿈(이 휴대폰만)' : '')));
    if (!otherDay) out.push(daySummary(stops.filter((x) => !x.s.removed)));
    const TABS = [['ALL', '전체', stops.length], ['REMAIN', '남음', remain.length], ['HOLD', '재방문·보류', hold.length]];
    out.push(h('div', { class: 'tabs pc-list-filters', role: 'group', 'aria-label': '목록 거르기' },
      ...TABS.map(([k, t, c]) => btn(`${t} ${c}`, () => { ui.filter = k; render(); }, ui.filter === k ? 'tab on' : 'tab'))));
    out.push(dongChips(stops));
    out.push(stopSearch(stops.length));
    const list = filterDong(filterQ({ ALL: stops, REMAIN: remain, HOLD: hold }[ui.filter]));
    out.push(h('p', { class: 'small', role: 'status', 'aria-label': '목록 조회 결과' }, `${list.length}건 표시${ui.dong ? ` · ${ui.dong}` : ''}${ui.stopQ ? ' · 검색 적용' : ''}`));
    if (!list.length) {
      if (ui.stopQ) {
        out.push(h('p', { class: 'empty' }, `'${ui.stopQ}'에 맞는 곳이 없습니다.`));
        out.push(btn('다른 날짜에서도 찾기', () => openFinder({ q: ui.stopQ }), 'big'));
      } else out.push(...emptyFilter([ui.filter !== 'ALL' ? TABS.find(([k]) => k === ui.filter)[1] : '', ui.dong], '곳이',
        () => { ui.filter = 'ALL'; ui.dong = ''; render(); }));
    }
    const nowId = otherDay ? null : nextStop?.s.request_id;  // 지금 갈 곳 (오늘 목록만)
    // 방문 순서 바꾸기(이 휴대폰만): 골목 사정 등으로 순서를 바꿔 다닐 때. PC 노선·배정은 그대로이고, 새 목록을 받으면 PC 순서로 돌아간다.
    const canReorder = ui.filter === 'ALL' && !ui.stopQ && !ui.dong && stops.length > 1;
    if (canReorder) out.push(h('div', { class: 'row reorder-bar' },
      btn(ui.reorder ? '순서 정하기 끝' : '방문 순서 바꾸기', () => { ui.reorder = !ui.reorder; render(); }, 'ghost'),
      reordered && !ui.reorder ? btn('PC 순서로 되돌리기', async () => { st.localOrder = null; await commit(); toast('PC 순서로 되돌렸습니다'); render(); }, 'ghost') : null));
    const move = async (s, dir) => {
      const ids = stops.filter((x) => !x.s.removed).map((x) => x.s.request_id);
      const i = ids.indexOf(s.request_id), j = i + dir;
      if (i < 0 || j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      st.localOrder = { work_date: wd, assign_ver: dl ? dl.assign_ver : 0, ids };
      await commit(); render();
    };
    const one = ({ s, d }) => h('li', { class: ui.reorder && canReorder ? 'srow' : null },
      stopCard(s, d, { nowId }),
      ui.reorder && canReorder && !s.removed ? h('span', { class: 'mv' }, btn('▲', () => move(s, -1), 'step', { 'aria-label': '위로' }), btn('▼', () => move(s, 1), 'step', { 'aria-label': '아래로' })) : null,
      s.unload_after && !ui.stopQ && !(ui.reorder && canReorder) ? h('div', { class: 'unload-row' }, h('span', { class: 'seq u' }, '⇩'),
        h('span', { class: 'u-body' }, h('strong', {}, `${s.unload_after.label} 하차`), h('br'), h('span', { class: 'small' }, `도착 ${s.unload_after.eta || '-'} · 약 ${s.unload_after.volume_m3}m³ 비움 · ${s.unload_after.minutes}분`)),
        navBtn('길안내', null, unloadLoc(s.unload_after), NAV.navLabel('UNLOAD'), 'u-nav'),
        btn('하차 기록', () => go('#/unload'), 'u-log', { 'aria-label': `${s.unload_after.label} 하차 기록` })) : null);
    const showTrips = ui.filter === 'ALL' && !ui.stopQ && !ui.dong && !ui.reorder;
    out.push(h('ul', { class: 'stops route' }, ui.reorder && canReorder ? list.map(one) : routeItems(list, one, showTrips ? tripHeads(stops) : {})));
  }
  if (later.length) out.push(h('section', { class: 'card' }, h('h2', {}, `다른 날 현장 접수 ${later.length}건`),
    h('p', { class: 'small' }, '결과 보내기 때 PC로 갑니다. PC가 그날 목록에 넣으면 여기서 사라집니다.'),
    h('ul', { class: 'stops' }, ...later.map((s) => h('li', {}, stopCard(s, deriveStop(s, st.events), { showDate: true }))))));
  out.push(tabBar('list'));
  return h('main', { class: 'with-tabs' }, ...out);
}

export {
  screenHome,
};
