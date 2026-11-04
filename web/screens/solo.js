// 휴대폰 화면: 단독 모드 홈
import { deriveStop } from '../fees.js';
import { BACKUP_WARN_MS, ago, appLogo, btn, commit, go, h, lockNow, render, st, tabBar, themeToggle, todayKST, ui } from '../core.js';
import { daySummary, dongChips, filterDong, filterQ, homeBanners, routeItems, setupCard, stopCard, stopSearch, workerBar } from './stoplist.js';

async function screenHomeSolo() {
  const today = todayKST(), tomorrow = todayKST(1);
  const view = ui.soloView || 'TODAY';
  const all = st.requests.map((s) => ({ s, d: deriveStop(s, st.events) }));
  const live = all.filter((x) => x.s.status !== 'CANCELLED');
  const open = live.filter((x) => x.d.progress !== 'DONE');
  const lists = {
    TODAY: live.filter((x) => x.s.planned_date === today),
    TOMORROW: live.filter((x) => x.s.planned_date === tomorrow),
    OPEN: open,
    CANCELLED: all.filter((x) => x.s.status === 'CANCELLED'),
  };
  const list = filterDong(filterQ(lists[view].slice().sort((a, b) => (a.s.planned_date || '').localeCompare(b.s.planned_date || '') || a.s.seq - b.s.seq)));
  const needBackup = st.requests.length && (!st.lastBackupAt || Date.now() - Date.parse(st.lastBackupAt) > BACKUP_WARN_MS);
  const move = async (item, dir) => {
    const same = lists[view].map((x) => x.s).filter((r) => r.planned_date === item.planned_date).sort((a, b) => a.seq - b.seq);
    const i = same.indexOf(item), j = i + dir;
    if (j < 0 || j >= same.length) return;
    [same[i].seq, same[j].seq] = [same[j].seq, same[i].seq];
    await commit(); render();
  };
  const out = [
    h('header', { class: 'bar' }, appLogo(), h('h1', { class: 'bar-title' }, '동구 수거'), h('span', { class: 'mode-tag' }, '단독'), themeToggle(), btn('잠금', lockNow, 'ghost')),
    workerBar(), ...(await homeBanners()), setupCard(),
    needBackup ? h('section', { class: 'card notice' },
      h('p', {}, `마지막 백업: ${ago(st.lastBackupAt)}. 휴대폰을 잃어버리거나 고장 나면 접수 원본이 사라집니다.`),
      btn('지금 백업', () => go('#/backup'), 'primary')) : null,
    h('section', { class: 'actions' },
      btn('+ 전화 접수 등록', () => { ui.form = null; go('#/intake'); }, 'primary huge'),
      h('div', { class: 'grid2' }, btn('백업·복원', () => go('#/backup'), 'big'), btn(`하차 기록${st.events.some((e) => e.kind === 'UNLOAD') ? ` (${st.events.filter((e) => e.kind === 'UNLOAD').length}회)` : ''}`, () => go('#/unload'), 'big'))),
    h('div', { class: 'tabs', role: 'group', 'aria-label': '목록 거르기' }, ...[['TODAY', `오늘 ${lists.TODAY.length}`], ['TOMORROW', `내일 ${lists.TOMORROW.length}`], ['OPEN', `미처리 ${open.length}`], ['CANCELLED', `취소 ${lists.CANCELLED.length}`]]
      .map(([k, t]) => btn(t, () => { ui.soloView = k; ui.reorder = false; render(); }, view === k ? 'tab on' : 'tab'))),
  ];
  if (view === 'TODAY' && lists.TODAY.length) out.splice(out.indexOf(out.find((x) => x && x.classList && x.classList.contains('actions'))), 0, daySummary(lists.TODAY));
  out.push(dongChips(lists[view]));
  out.push(stopSearch(lists[view].length));
  if (!list.length) out.push(h('p', { class: 'empty' }, ui.stopQ ? `'${ui.stopQ}'에 맞는 접수가 없습니다.` : view === 'TODAY' ? '오늘 수거 예정 접수가 없습니다. [+ 전화 접수 등록]으로 시작하세요.' : '해당 접수가 없습니다.'));
  else {
    if (view === 'TODAY' || view === 'TOMORROW') out.push(btn(ui.reorder ? '순서 정하기 끝' : '방문 순서 바꾸기', () => { ui.reorder = !ui.reorder; render(); }, 'ghost'));
    const route = view === 'TODAY' || view === 'TOMORROW';
    const one = ({ s, d }) => h('li', { class: 'srow' }, stopCard(s, d, { solo: true, showDate: view === 'OPEN' }),
      ui.reorder ? h('span', { class: 'mv' }, btn('▲', () => move(s, -1), 'step', { 'aria-label': '위로' }), btn('▼', () => move(s, 1), 'step', { 'aria-label': '아래로' })) : null);
    out.push(h('ul', { class: 'stops' + (route ? ' route' : '') }, route && !ui.reorder ? routeItems(list, one) : list.map(one)));
  }
  out.push(tabBar('list'));
  return h('main', { class: 'with-tabs' }, ...out);
}

// ------------------------------------------------------------ 단독 모드 접수 등록·수정

export {
  screenHomeSolo,
};
