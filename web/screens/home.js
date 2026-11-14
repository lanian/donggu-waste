// 휴대폰 화면: 오늘 목록(홈)·배너·설정 단계
import * as NAV from '../nav.js';
import * as S from '../store.js';
import { deriveStop } from '../fees.js';
import { appLogo, btn, commit, flushSteps, go, h, isSolo, lockNow, mdOf, pcStops, pcWorkDate, pendingPhotos, render, saveFailed, st, tabBar, themeToggle, toast, todayKST, ui, unsent } from '../core.js';
import { navBtn, unloadLoc } from '../widgets.js';
import { screenMode } from './mode.js';
import { screenHomeSolo } from './solo.js';
import { openFinder } from './find.js';
import { daySummary, dongChips, filterDong, filterQ, homeBanners, routeItems, setupCard, stopCard, stopSearch, tripHeads, workerBar } from './stoplist.js';

async function screenHome() {
  await flushSteps();
  if (!st.mode) return screenMode();
  if (isSolo()) return screenHomeSolo();
  const registered = !!(await S.getRaw('pc')) && !!(await S.getRaw('device'));
  const n = unsent().length;
  const dl = st.daylist;
  const wd = pcWorkDate();
  const every = pcStops();
  const later = every.filter((s) => s.field && s.planned_date !== wd);  // 다른 날로 받은 현장 접수
  const today = every.filter((s) => !later.includes(s));
  const stops = today.map((s) => ({ s, d: deriveStop(s, st.events) }));
  const top = h('header', { class: 'bar' }, appLogo(), h('h1', { class: 'bar-title' }, '동구 수거'), themeToggle(), btn('잠금', lockNow, 'ghost'));
  // 주 동작은 '지금 할 일'에 두고, 자주 쓰는 도구는 두 열로 묶는다.
  const stale = n && dl && dl.work_date < todayKST();
  // 접수 등록(현장·전화)은 기기 등록 전에도 받아 둘 수 있다(PC로 보내는 것만 등록 뒤에)
  const intakeBtn = btn('+ 접수 등록 (현장·전화)', () => { ui.form = null; go('#/intake'); }, 'big intake-btn');
  const actions = h('section', { class: 'actions home-tools', 'aria-label': '자주 쓰는 기능' },
    h('h2', {}, '자주 쓰는 기능'), h('div', { class: 'grid2' },
      btn('오늘 목록 받기', () => go('#/scan'), 'big', { disabled: !registered }),
      btn(n ? (dl ? `결과 보내기 ${n}건` : `결과 보내기 (미전송 ${n}건)`) : '결과 보내기', () => go('#/export'), 'big' + (stale ? ' warn' : '')),
      intakeBtn, btn('날짜·접수 찾기', openFinder, 'big'),
      btn('빠른 수수료 계산', () => go('#/calculator'), 'big')));
  ui.scanAck = false;
  const otherDay = !!dl && dl.work_date !== todayKST();
  const out = [top, workerBar()];
  // 지난(또는 다음) 날짜 목록: 오늘 할 일로 보이지 않게 맨 위 큰 띠, '지금 갈 곳'·진행 띠는 숨김
  if (otherDay) out.push(h('section', { class: 'card day-banner' + (dl.work_date < todayKST() ? ' past' : ''), role: 'alert' },
    h('h2', {}, dl.work_date < todayKST() ? `${mdOf(dl.work_date)} 목록입니다 — 결과를 보내고 오늘 목록을 받으세요` : `${mdOf(dl.work_date)} 목록입니다 — 그날 업무용 목록입니다`),
    dl.work_date < todayKST() ? h('div', { class: 'grid2' }, btn(n ? `결과 보내기 ${n}건` : '결과 보내기', () => go('#/export'), 'primary big'), btn('오늘 목록 받기', () => go('#/scan'), 'big', { disabled: !registered })) : null));
  out.push(...(await homeBanners()));
  const rcpt = st.lastReceipt;
  if (rcpt && !rcpt.told && dl && rcpt.work_date === dl.work_date) out.push(h('section', { class: 'card notice receipt-card', role: 'status' },
    h('p', {}, h('strong', {}, '수신 코드 '), h('span', { class: 'code-kind' }, 'R-'), h('span', { class: 'rc-code' }, `${rcpt.code.slice(0, 4)} ${rcpt.code.slice(4)}`), ' — PC 담당자에게 알려 주세요'),
    h('div', { class: 'grid2' }, btn('수신 코드 다시 보기', () => { ui.receipt = null; go('#/received'); }, 'big'),
      btn('PC에 알렸음', async () => { st.lastReceipt = { ...rcpt, told: true }; await commit(); render(); }, 'ghost'))));
  // 안내는 현재 기록만 읽는다. 전송·반영·회수·정리는 각각의 기존 화면에서 사람이 확인한다.
  const waitingResult = st.exports.some((x) => !x.confirmed);
  const recovered = new Set(st.recoveredPhotos || []);
  const photos = [...st.events.filter((e) => e.kind === 'PHOTO_META').map((e) => e.body), ...(st.keptPhotos || [])];
  const photoN = photos.filter((p) => !recovered.has(p.photo_id)).length;
  // 수량을 다 입력해도 방문 결과를 저장하지 않았으면 상세로 이어 간다.
  const unrecorded = stops.filter(({ s, d }) => !s.removed && s.status !== 'CANCELLED' && !s.pii_purged && (!d.outcome || d.resultNeedsConfirmation));
  const reconfirm = unrecorded.find(({ d }) => d.resultNeedsConfirmation);
  const nextStop = unrecorded[0];
  let next = { title: '오늘 배정 목록을 받으세요', detail: 'PC 담당자가 준비한 QR을 읽고, 받은 뒤 수신 코드를 알려 주세요.', label: '배정 목록 수신', run: () => go('#/scan') };
  if (pendingPhotos.length) next = {
    title: `사진 ${pendingPhotos.length}장이 아직 저장 대기 중입니다`, detail: '앱을 닫지 말고 위의 [사진 다시 저장]으로 저장을 마쳐 주세요.', label: '대기 사진 저장 안내',
    run: () => { const el = document.querySelector('.pending-photo'); if (el) { el.scrollIntoView({ block: 'center' }); el.querySelector('button')?.focus({ preventScroll: true }); } },
  };
  else if (saveFailed || S.hasPendingSecure('state')) next = {
    title: '저장하지 못한 기록부터 보호하세요', detail: '앱을 닫지 말고 이 창의 기록을 먼저 내보내 주세요. 저장 실패 알림은 실제 저장이 끝나야 사라집니다.',
    label: registered ? '기록 보호용 내보내기' : '기록 보호용 백업', run: () => go(registered ? '#/export' : '#/backup'),
  };
  else if (reconfirm) next = { title: `${reconfirm.s.seq}번 수거 결과를 재확인하세요`, detail: '결과 저장 뒤 수량이 바뀌었습니다. 현재 수량에 맞는 결과를 다시 선택해 저장하세요.', label: `${reconfirm.s.seq}번 결과 재확인`, run: () => go(`#/stop/${reconfirm.s.request_id}`) };
  else if (!registered) next = { title: '관리자와 기기를 연결하세요', detail: '목록 수신과 결과 전달에 필요한 등록입니다. 현장·전화 접수는 아래에서 먼저 받을 수 있습니다.', label: 'PC 연결 등록하기', run: () => go('#/register') };
  else if (dl && dl.work_date < todayKST() && (n || waitingResult)) next = {
    title: '지난 목록의 PC 반영을 먼저 확인하세요', detail: `${mdOf(dl.work_date)} 목록의 기록이 남아 있습니다. 결과를 전달하고 반영을 확인한 뒤 오늘 목록을 받으세요.`, label: '결과 전달·반영 확인', run: () => go('#/export'),
  };
  else if (otherDay) next.detail = `${mdOf(dl.work_date)} 목록을 보고 있습니다. 오늘 수거로 이어가기 전에 오늘 업무용 목록을 받으세요.`;
  else if (rcpt && !rcpt.told && dl && rcpt.work_date === dl.work_date) next = {
    title: '받은 목록의 수신 코드를 알려 주세요', detail: 'PC 담당자가 R-코드를 받아 적었는지 확인하세요. 알렸음 처리는 기존 수신 카드에서 직접 합니다.', label: '수신 코드 확인하기', run: () => { ui.receipt = null; go('#/received'); },
  };
  else if (nextStop) next = {
    title: `${nextStop.s.seq}번 방문 결과를 이어서 기록하세요`, detail: `아직 방문 결과를 기록하지 않은 곳 ${unrecorded.length}곳 · 휴대폰의 방문 순서를 따릅니다.`,
    label: `${nextStop.s.seq}번 수거 이어가기`, run: () => go(`#/stop/${nextStop.s.request_id}`),
  };
  else if (n || waitingResult) next = {
    title: waitingResult ? '결과의 PC 반영 확인이 남아 있습니다' : '기록한 결과를 PC로 전달하세요',
    detail: waitingResult ? '결과 파일을 아직 보내지 않았다면 먼저 전달하세요. PC 담당자가 가져온 뒤 알려 주는 D-코드로 반영을 확인합니다.' : `미반영 기록 ${n}건이 있습니다. 결과 파일을 전달한 뒤 PC 담당자에게 반영을 확인하세요. 사진은 따로 전달합니다.`,
    label: '결과 전달·반영 확인', run: () => go('#/export'),
  };
  else if (photoN) next = { title: `사진 ${photoN}장의 회수 확인이 남아 있습니다`, detail: '사진 파일을 승인된 보안 매체로 전달하고 P-코드를 확인하세요. 파일을 만들기만 해서는 회수가 끝나지 않습니다.', label: '사진 전달·회수 확인', run: () => go('#/export') };
  else if (dl) next = { title: '반영·회수를 확인한 자료를 정리할 수 있습니다', detail: '정리 화면에서 남은 기록을 확인하세요. [오늘 자료 정리]를 직접 누르기 전에는 목록과 기록을 지우지 않습니다.', label: '정리 단계 확인하기', run: () => go('#/export') };
  out.push(h('section', { class: 'card next-action', 'aria-label': '지금 할 일' }, h('h2', {}, '지금 할 일'),
    h('p', { class: 'next-title' }, next.title), h('p', { class: 'small next-detail' }, next.detail), btn(next.label, next.run, 'primary huge')));
  if (!registered) out.push(h('section', { class: 'card notice' }, h('p', {}, '먼저 관리자와 함께 기기 등록을 해 주세요.'), btn('기기 등록', () => go('#/register'), 'primary')));
  else out.push(setupCard());
  out.push(actions);
  const moreBtns = btn(st.events.some((e) => e.kind === 'UNLOAD') ? `하차 기록 (${st.events.filter((e) => e.kind === 'UNLOAD').length}회)` : '하차 기록', () => go('#/unload'), 'big');
  if (!today.length) { out.push(h('p', { class: 'empty' }, '받은 목록이 없습니다.')); out.push(moreBtns); }
  else {
    const remain = stops.filter((x) => (x.d.progress !== 'DONE' || x.d.resultNeedsConfirmation) && !x.s.removed);
    const hold = stops.filter((x) => x.d.resultNeedsConfirmation || ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.d.outcome) || x.d.contact === 'CALLBACK');
    const changed = Object.keys(st.listChanges || {}).length;
    const reordered = !!(st.localOrder && st.localOrder.work_date === wd && st.localOrder.assign_ver === (dl ? dl.assign_ver : 0));
    const fieldN = today.filter((s) => s.field).length;
    out.push(h('p', { class: 'meta' }, (dl ? `${dl.work_date} ${dl.crew}` + (dl.assign_ver > 1 ? ` · 목록 ${dl.assign_ver}차` : '') : `${wd} · PC 목록 없음`) + (changed ? ` · 바뀐 곳 ${changed}` : '')
      + (fieldN ? ` · 현장 접수 ${fieldN}` : '') + (reordered ? ' · 순서 바꿈(이 휴대폰만)' : '')));
    if (!otherDay) out.push(daySummary(stops.filter((x) => !x.s.removed)));
    out.push(h('div', { class: 'tabs', role: 'group', 'aria-label': '목록 거르기' },
      ...[['ALL', `전체 ${stops.length}`], ['REMAIN', `남음 ${remain.length}`], ['HOLD', `재방문·보류 ${hold.length}`]].map(([k, t]) =>
        btn(t, () => { ui.filter = k; render(); }, ui.filter === k ? 'tab on' : 'tab'))));
    out.push(dongChips(stops));
    out.push(stopSearch(stops.length));
    const list = filterDong(filterQ({ ALL: stops, REMAIN: remain, HOLD: hold }[ui.filter]));
    if (!list.length) {
      out.push(h('p', { class: 'empty' }, ui.stopQ ? `'${ui.stopQ}'에 맞는 곳이 없습니다.` : '해당하는 곳이 없습니다.'));
      if (ui.stopQ) out.push(btn('다른 날짜에서도 찾기', () => openFinder({ q: ui.stopQ }), 'big'));
    }
    const nowId = otherDay ? null : (stops.find((x) => x.d.progress !== 'DONE' && !x.d.outcome && !x.s.removed) || {}).s?.request_id;  // 지금 갈 곳 (오늘 목록만)
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
        navBtn('길안내', null, unloadLoc(s.unload_after), NAV.navLabel('UNLOAD'), 'u-nav')) : null);
    const showTrips = ui.filter === 'ALL' && !ui.stopQ && !ui.dong && !ui.reorder;
    out.push(h('ul', { class: 'stops route' }, ui.reorder && canReorder ? list.map(one) : routeItems(list, one, showTrips ? tripHeads(stops) : {})));
    out.push(moreBtns);
  }
  if (later.length) out.push(h('section', { class: 'card' }, h('h2', {}, `다른 날 현장 접수 ${later.length}건`),
    h('p', { class: 'small' }, '결과 보내기 때 PC로 갑니다. PC가 그날 목록에 넣으면 여기서 사라집니다.'),
    h('ul', { class: 'stops' }, ...later.map((s) => h('li', {}, stopCard(s, deriveStop(s, st.events), { showDate: true }))))));
  out.push(tabBar('list'));
  return h('main', { class: 'with-tabs' }, ...out);
}

// ------------------------------------------------------------ 하차 기록 (반입지에서 비운 양)

export {
  screenHome,
};
