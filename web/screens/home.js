// 휴대폰 화면: 오늘 목록(홈)·배너·설정 단계
import * as NAV from '../nav.js';
import * as S from '../store.js';
import { deriveStop } from '../fees.js';
import { appLogo, btn, commit, go, h, isSolo, lockNow, mdOf, pcStops, pcWorkDate, render, st, tabBar, themeToggle, toast, todayKST, ui, unsent } from '../core.js';
import { navBtn, unloadLoc } from '../widgets.js';
import { screenMode } from './mode.js';
import { screenHomeSolo } from './solo.js';
import { daySummary, dongChips, filterDong, filterQ, homeBanners, routeItems, setupCard, stopCard, stopSearch, tripHeads, workerBar } from './stoplist.js';

async function screenHome() {
  if (!st.mode) return screenMode();
  if (isSolo()) return screenHomeSolo();
  const registered = !!(await S.getRaw('pc')) && !!(await S.getRaw('device'));
  const n = unsent().length;
  const dl = st.daylist;
  const top = h('header', { class: 'bar' }, appLogo(), h('h1', { class: 'bar-title' }, '동구 수거'), themeToggle(), btn('잠금', lockNow, 'ghost'));
  // 목록이 있으면 하루 한두 번 쓰는 버튼은 작게, 방문 순서가 먼저 보이게
  const stale = n && dl && dl.work_date < todayKST();
  // 접수 등록(현장·전화)은 기기 등록 전에도 받아 둘 수 있다(PC로 보내는 것만 등록 뒤에)
  const intakeBtn = btn('+ 접수 등록 (현장·전화)', () => { ui.form = null; go('#/intake'); }, 'big intake-btn');
  const actions = dl
    ? h('section', { class: 'actions' }, h('div', { class: 'grid2' },
      btn('오늘 목록 받기', () => go('#/scan'), 'big', { disabled: !registered }),
      btn(n ? `결과 보내기 ${n}건` : '결과 보내기', () => go('#/export'), 'big' + (stale ? ' warn' : ''))), intakeBtn)
    : h('section', { class: 'actions' },
      btn('오늘 목록 받기', () => go('#/scan'), 'primary huge', { disabled: !registered }),
      btn(n ? `결과 보내기 (미전송 ${n}건)` : '결과 보내기', () => go('#/export'), 'huge ' + (stale ? 'warn' : '')), intakeBtn);
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
  if (!registered) out.push(h('section', { class: 'card notice' }, h('p', {}, '먼저 관리자와 함께 기기 등록을 해 주세요.'), btn('기기 등록', () => go('#/register'), 'primary')));
  else out.push(setupCard());
  out.push(actions);
  const wd = pcWorkDate();
  const every = pcStops();
  const later = every.filter((s) => s.field && s.planned_date !== wd);  // 다른 날로 받은 현장 접수
  const today = every.filter((s) => !later.includes(s));
  const moreBtns = btn(st.events.some((e) => e.kind === 'UNLOAD') ? `하차 기록 (${st.events.filter((e) => e.kind === 'UNLOAD').length}회)` : '하차 기록', () => go('#/unload'), 'big');
  if (!today.length) { out.push(h('p', { class: 'empty' }, '받은 목록이 없습니다.')); out.push(moreBtns); }
  else {
    const stops = today.map((s) => ({ s, d: deriveStop(s, st.events) }));
    const remain = stops.filter((x) => x.d.progress !== 'DONE' && !x.s.removed);
    const hold = stops.filter((x) => ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.d.outcome) || x.d.contact === 'CALLBACK');
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
    if (!list.length) out.push(h('p', { class: 'empty' }, ui.stopQ ? `'${ui.stopQ}'에 맞는 곳이 없습니다.` : '해당하는 곳이 없습니다.'));
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
