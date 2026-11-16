// 휴대폰 화면: 단독 모드 홈 — PC 연결 방식 홈과 같은 순서(머리글 → 지금 할 일 → 경고 띠 → 자주 쓰는 기능 → 진행 → 거르기 → 목록)
import { deriveStop } from '../fees.js';
import * as S from '../store.js';
import { BACKUP_WARN_MS, ago, appLogo, btn, commit, flushSteps, go, h, lockNow, pendingPhotos, render, saveFailed, st, tabBar, themeToggle, todayKST, ui } from '../core.js';
import { daySummary, dongChips, emptyFilter, filterDong, filterQ, homeBanners, isStopDone, needsResult, nextQuick, routeItems, setupCard, stopCard, stopSearch, workerBar } from './stoplist.js';
import { openFinder } from './find.js';

async function screenHomeSolo() {
  await flushSteps();
  const today = todayKST(), tomorrow = todayKST(1);
  const view = ui.soloView || 'TODAY';
  const all = st.requests.map((s) => ({ s, d: deriveStop(s, st.events) }));
  const live = all.filter((x) => x.s.status !== 'CANCELLED');
  const open = live.filter(({ s, d }) => !s.removed && !s.pii_purged && !isStopDone(s, d));
  const lists = {
    TODAY: live.filter((x) => x.s.planned_date === today),
    TOMORROW: live.filter((x) => x.s.planned_date === tomorrow),
    OPEN: open,
    CANCELLED: all.filter((x) => x.s.status === 'CANCELLED'),
  };
  const VIEWS = [['TODAY', '오늘'], ['TOMORROW', '내일'], ['OPEN', '남음'], ['CANCELLED', '취소']];  // '남음': PC 연결 방식 거르기와 같은 이름
  const dongs = dongChips(lists[view]);  // 유효하지 않은 동 조건을 먼저 정리한 뒤 목록을 거른다.
  const list = filterDong(filterQ(lists[view].slice().sort((a, b) => (a.s.planned_date || '').localeCompare(b.s.planned_date || '') || a.s.seq - b.s.seq)));
  const route = view === 'TODAY' || view === 'TOMORROW';
  const canReorder = route && !ui.stopQ && !ui.dong && list.length > 1;
  const needBackup = st.requests.length && (!st.lastBackupAt || Date.now() - Date.parse(st.lastBackupAt) > BACKUP_WARN_MS);
  const actionable = live.filter(({ s, d }) => needsResult(s, d));
  const reconfirm = actionable.find(({ d }) => d.resultNeedsConfirmation);
  const nextToday = actionable.filter(({ s }) => s.planned_date === today).sort((a, b) => a.s.seq - b.s.seq)[0];
  const overdue = actionable.filter(({ s }) => s.planned_date && s.planned_date < today)
    .sort((a, b) => a.s.planned_date.localeCompare(b.s.planned_date) || a.s.seq - b.s.seq)[0];
  let next = { key: 'intake', title: '새 전화 접수부터 시작하세요', detail: '오늘 예정된 수거가 없습니다. 접수 내용을 등록하면 이 휴대폰에서 수거 기록까지 이어집니다.', label: '새 전화 접수 시작', run: () => { ui.form = null; go('#/intake'); } };
  if (pendingPhotos.length) next = {
    key: 'photo', title: `사진 ${pendingPhotos.length}장이 아직 저장 대기 중입니다`, detail: '앱을 닫지 말고 아래의 [사진 다시 저장]으로 저장을 마쳐 주세요.', label: '대기 사진 저장 안내',
    run: () => { const el = document.querySelector('.pending-photo'); if (el) { el.scrollIntoView({ block: 'center' }); el.querySelector('button')?.focus({ preventScroll: true }); } },
  };
  else if (saveFailed || S.hasPendingSecure('state')) next = { key: 'backup', title: '저장하지 못한 기록부터 보호하세요', detail: '앱을 닫지 말고 이 창의 기록을 암호화 백업으로 보관하세요. 실제 저장이 끝나기 전에는 저장 실패 상태입니다.', label: '기록 보호용 백업', run: () => go('#/backup') };
  else if (reconfirm) next = { key: 'stop', title: `${reconfirm.s.seq}번 수거 결과를 재확인하세요`, detail: '결과 저장 뒤 수량이 바뀌었습니다. 현재 수량에 맞는 결과를 다시 선택해 저장하세요.', label: `${reconfirm.s.seq}번 결과 재확인`, run: () => go(`#/stop/${reconfirm.s.request_id}`) };
  else if (nextToday) next = { key: 'stop', stop: nextToday.s, title: `${nextToday.s.seq}번 방문 결과를 이어서 기록하세요`, detail: `오늘 방문 결과를 기록하지 않은 접수 ${actionable.filter(({ s }) => s.planned_date === today).length}건 · 취소 접수는 제외합니다.`, label: `${nextToday.s.seq}번 수거 이어가기`, run: () => go(`#/stop/${nextToday.s.request_id}`) };
  else if (overdue) next = { key: 'stop', stop: overdue.s, title: '지난 날짜의 미처리 접수가 남아 있습니다', detail: `${overdue.s.planned_date} 예정 접수입니다. 원래 접수에서 결과와 재방문 일정을 확인하세요.`, label: '미처리 접수 이어가기', run: () => go(`#/stop/${overdue.s.request_id}`) };
  else if (st.requests.length && (needBackup || lists.TODAY.length)) next = { key: 'backup', title: '접수와 현장 기록을 백업해 두세요', detail: '이 휴대폰이 원본입니다. 사진을 포함한 암호화 백업을 승인된 보안 매체에 보관하세요.', label: '오늘 기록 백업하기', run: () => go('#/backup') };
  const move = async (item, dir) => {
    const same = lists[view].map((x) => x.s).filter((r) => r.planned_date === item.planned_date).sort((a, b) => a.seq - b.seq);
    const i = same.indexOf(item), j = i + dir;
    if (j < 0 || j >= same.length) return;
    [same[i].seq, same[j].seq] = [same[j].seq, same[i].seq];
    await commit(); render();
  };
  const out = [
    h('header', { class: 'bar' }, appLogo(), h('h1', { class: 'bar-title' }, '동구 수거'), h('span', { class: 'chip mode-tag' }, '단독'), themeToggle(), btn('잠금', lockNow, 'ghost')),
    workerBar(),
    h('section', { class: 'card next-action', 'aria-label': '지금 할 일' }, h('h2', {}, '지금 할 일'),
      h('p', { class: 'next-title' }, next.title), h('p', { class: 'small next-detail' }, next.detail), btn(next.label, next.run, 'primary huge')),
    next.stop ? nextQuick(next.stop) : null,
    // 백업 경고 띠: 주 단추 없이 글 + 링크형 이동
    needBackup ? h('section', { class: 'callout warn band backup-warn', role: 'status' },
      h('p', {}, '⚠ ', st.lastBackupAt ? `마지막 백업: ${ago(st.lastBackupAt)}. 휴대폰을 잃어버리거나 고장 나면 접수 원본이 사라집니다.`
        : '아직 백업하지 않았습니다. 휴대폰을 잃어버리면 접수 원본이 사라집니다.'),
      h('a', { href: '#/backup', class: 'callout-link' }, '백업 ›')) : null,
    ...(await homeBanners()),
    // PC 연결 방식과 같은 이름·같은 순서: [+ 접수 등록][날짜·접수 찾기][하차 기록][빠른 수수료 계산] + [백업·복원] (+ [지도])
    h('section', { class: 'actions home-tools', 'aria-label': '자주 쓰는 기능' },
      h('h2', { class: 'section-label' }, '자주 쓰는 기능'), h('div', { class: 'grid2' },
        btn('+ 접수 등록', () => { ui.form = null; go('#/intake'); }, 'big intake-btn'),
        btn('날짜·접수 찾기', openFinder, 'big'),
        btn(`하차 기록${st.events.some((e) => e.kind === 'UNLOAD') ? ` (${st.events.filter((e) => e.kind === 'UNLOAD').length}회)` : ''}`, () => go('#/unload'), 'big'),
        btn('빠른 수수료 계산', () => go('#/calculator'), 'big'),
        btn('백업·복원', () => go('#/backup'), 'big'),
        st.mapEnabled ? btn('지도', () => go('#/map'), 'big') : null)),
    setupCard(),
    h('h2', { id: 'collection-list', tabindex: '-1' }, '수거 목록'),
    view === 'TODAY' && lists.TODAY.length ? daySummary(lists.TODAY) : null,
    h('div', { class: 'tabs', role: 'group', 'aria-label': '목록 거르기' }, ...VIEWS.map(([k, t]) => [k, `${t} ${(k === 'OPEN' ? open : lists[k]).length}`])
      .map(([k, t]) => btn(t, () => { ui.soloView = k; ui.reorder = false; render(); }, view === k ? 'tab on' : 'tab'))),
  ];
  out.push(dongs);
  out.push(stopSearch(lists[view].length));
  out.push(h('p', { class: 'small', role: 'status', 'aria-label': '목록 조회 결과' }, `${list.length}건 표시${ui.dong ? ` · ${ui.dong}` : ''}${ui.stopQ ? ' · 검색 적용' : ''}`));
  if (!list.length) {
    if (ui.stopQ) {
      out.push(h('p', { class: 'empty' }, `'${ui.stopQ}'에 맞는 접수가 없습니다.`));
      out.push(btn('다른 날짜에서도 찾기', () => openFinder({ q: ui.stopQ }), 'big'));
    } else if (view === 'TODAY' && !ui.dong) out.push(h('p', { class: 'empty' }, '오늘 수거 예정 접수가 없습니다. [+ 접수 등록]으로 시작하세요.'));
    else out.push(...emptyFilter([VIEWS.find(([k]) => k === view)[1], ui.dong], '접수가',
      () => { ui.soloView = 'TODAY'; ui.dong = ''; ui.reorder = false; render(); }, '오늘 보기'));
  }
  else {
    if (canReorder) out.push(btn(ui.reorder ? '순서 정하기 끝' : '방문 순서 바꾸기', () => { ui.reorder = !ui.reorder; render(); }, 'ghost'));
    const one = ({ s, d }) => h('li', { class: 'srow' }, stopCard(s, d, { solo: true, showDate: view === 'OPEN' }),
      ui.reorder && canReorder ? h('span', { class: 'mv' }, btn('▲', () => move(s, -1), 'step', { 'aria-label': '위로' }), btn('▼', () => move(s, 1), 'step', { 'aria-label': '아래로' })) : null);
    out.push(h('ul', { class: 'stops' + (route ? ' route' : '') }, route && !(ui.reorder && canReorder) ? routeItems(list, one) : list.map(one)));
  }
  out.push(tabBar('list'));
  return h('main', { class: 'with-tabs' }, ...out);
}

// ------------------------------------------------------------ 단독 모드 접수 등록·수정

export {
  screenHomeSolo,
};
