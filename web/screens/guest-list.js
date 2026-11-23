import { GuestScan, guestUpdate } from '../guest-list.js';
import * as S from '../store.js';
import { backBtn, btn, h, msgBox, onLock, render, say, sheet, st, startCamera, stopCamera, toast, todayKST } from '../core.js';

const STORE = 'guestList';
const write = (value) => S.putSecure(STORE, value, { recoverOnFailure: false });
let pending = Promise.resolve();

// 같은 창에서 보호 기간 정리·삭제·수신이 겹치지 않게 한다. 다른 창과는 putSecure가 원본 암호문을 대조한다.
function withStore(fn) {
  const operation = pending.then(fn);
  pending = operation.then(() => {}, () => {});
  return operation;
}
async function readCurrent(today) {
  const list = await S.getSecure(STORE);
  if (list && list.work_date < today) {
    await write(null);
    return null;
  }
  return list;
}
export function loadGuestList(today = todayKST()) {
  return withStore(() => readCurrent(today));
}
function sameIssue(a, b) {
  return !a || !b ? a === b : a.work_date === b.work_date && a.crew_id === b.crew_id &&
    a.assign_ver === b.assign_ver && a.issue_no === b.issue_no;
}
const titleOf = (list) => list.crew + ' · ' + list.work_date;
const changed = () => new Error('다른 창에서 조회 목록이 바뀌었습니다. 저장된 목록을 다시 열어 확인하세요.');
function readableError(error) {
  if (error?.code === 'STORE_CONFLICT') return changed().message;
  if (error?.name === 'QuotaExceededError') return '저장 공간이 부족합니다. 공간을 확보한 뒤 다시 저장하세요.';
  if (error instanceof SyntaxError || error instanceof TypeError) return '조회용 목록을 열지 못했습니다. PC에서 새 QR을 받아 주세요.';
  return error?.message || '목록을 저장하지 못했습니다. 다시 시도해 주세요.';
}

let leaveView = null;
function leaveGuest() { leaveView?.(); leaveView = null; }
onLock(leaveGuest);
window.addEventListener('hashchange', () => { if (location.hash !== '#/guest-list') leaveGuest(); });
window.addEventListener('pagehide', leaveGuest);
window.addEventListener('pageshow', (event) => {
  if (event.persisted && location.hash === '#/guest-list') render();
});

export async function screenGuestList() {
  leaveGuest();
  const owner = st, route = location.hash;
  const view = h('main'), body = h('section', { class: 'card' });
  const controller = new AbortController();
  const scan = new GuestScan();
  const alive = () => !controller.signal.aborted && st === owner && S.isUnlocked() && location.hash === route;
  const shown = () => alive() && view.isConnected;
  let dayTimer, saved = null, decoded = null;
  leaveView = () => {
    controller.abort(); clearTimeout(dayTimer);
    if (view.isConnected && body.querySelector('video')) stopCamera();
    scan.clear(); saved = null; decoded = null;
    body.replaceChildren(); view.replaceChildren();
  };
  const stored = await loadGuestList();
  if (!alive()) return view;
  saved = stored;
  view.append(h('header', { class: 'bar' }, backBtn(), h('h1', { class: 'bar-title' }, '배차 목록 조회')), body);
  const onNewDay = () => {
    if (shown() && saved && saved.work_date < todayKST()) { view.replaceChildren(); render(); }
    else if (alive() && saved) scheduleDayCheck();
  };
  function scheduleDayCheck() {
    clearTimeout(dayTimer);
    const midnight = Date.parse(todayKST(1) + 'T00:00:00+09:00');
    dayTimer = setTimeout(onNewDay, Math.max(1, midnight - Date.now()));
  }
  if (saved) scheduleDayCheck();
  const confirm = async (title, text, label, cls) => shown() && (await sheet({
    title, text, choices: [{ label, value: true, cls }], signal: controller.signal,
  })) === true && shown();

  function showList() {
    scan.clear(); decoded = null;
    const message = msgBox();
    body.replaceChildren(...(saved ? [
      h('h2', {}, titleOf(saved)),
      h('p', { class: 'small' }, '조회 전용 · ' + saved.assign_ver + '차 배차 · 방문 ' + saved.stops.length + '곳'),
      saved.stops.length ? h('ol', { class: 'stops', 'aria-label': '방문 순서' }, ...saved.stops.map((stop) =>
        h('li', { class: 'card', value: stop.seq },
          h('h3', {}, stop.seq + '번 · ' + (stop.address || stop.dong || '주소 미입력')),
          stop.dong && stop.address ? h('p', { class: 'small' }, stop.dong) : null,
          ...stop.items.map((item) => h('p', {}, [item.name, item.spec, item.qty + item.unit].filter(Boolean).join(' · ')))))) :
        h('p', {}, '이번 배차에는 방문할 수거지가 없습니다.'),
    ] : [h('p', {}, '받은 배차 목록이 없습니다. PC의 목록 전송 화면에서 조회용 QR을 여세요.')]),
    btn('조회용 QR 읽기', showScanner, 'primary big'),
    ...(saved ? [btn('목록 지우기', async () => {
      if (!await confirm('조회 목록을 지울까요?', titleOf(saved) + ' 배차를 이 휴대폰에서 지웁니다.', '지우기', 'danger')) return;
      try {
        await withStore(async () => {
          const current = await S.getSecure(STORE);
          if (!shown()) return;
          if (!sameIssue(current, saved)) throw changed();
          await write(null);
        });
        if (shown()) { toast('조회 목록을 지웠습니다'); render(); }
      } catch (e) { if (shown()) say(message, readableError(e), true); }
    }, 'ghost big')] : []),
    message);
  }
  function showScanner() {
    scan.clear(); decoded = null;
    const video = h('video', { playsinline: true, muted: true, class: 'cam' });
    const progress = h('p', { class: 'count', role: 'status', tabindex: '-1' }, '열쇠 QR을 비추세요');
    const message = msgBox(), retry = btn('카메라 다시 열기', () => decoded ? save() : camera(), 'big', { hidden: true });
    const active = () => shown() && body.contains(video);
    let busy = false;
    const restart = btn('열쇠부터 다시 읽기', () => {
      if (!active() || busy) return;
      scan.clear(); decoded = null; restart.hidden = true;
      progress.textContent = '열쇠 QR을 비추세요'; say(message, '');
      retry.textContent = '카메라 다시 열기';
      progress.focus(); camera();
    }, 'big', { hidden: true });
    const updateProgress = (text) => {
      if (progress.textContent !== text) progress.textContent = text;
    };
    const save = async () => {
      if (!active() || busy || !decoded) return;
      busy = true; retry.hidden = true;
      try {
        const current = await loadGuestList();
        if (!active()) return;
        const choice = guestUpdate(current, decoded, todayKST());
        if (choice === 'replace' && !await confirm('조회 목록을 바꿀까요?',
          titleOf(current) + ' 목록을 지우고 ' + titleOf(decoded) + ' 목록을 저장합니다.', '바꿔서 저장', 'primary')) {
          if (active()) render();
          return;
        }
        await withStore(async () => {
          const latest = await S.getSecure(STORE);
          if (!active()) return;
          if (!sameIssue(latest, current)) throw changed();
          if (guestUpdate(latest, decoded, todayKST()) !== 'duplicate') await write(decoded);
        });
        if (active()) { toast(choice === 'duplicate' ? '이미 받은 조회 목록입니다' : '조회 목록을 저장했습니다'); render(); }
      } catch (e) {
        if (active()) {
          say(message, readableError(e), true);
          if (e.code === 'GUEST_OLD') { decoded = null; updateProgress('최신 조회용 QR을 받아 주세요'); }
          else { retry.textContent = '조회 목록 저장 다시 시도'; retry.hidden = false; }
        }
      } finally { busy = false; }
    };
    const receive = async (text) => {
      if (!active() || busy || decoded) return;
      try {
        const step = scan.add(text);
        if (step.kind === 'key') {
          restart.hidden = false;
          if (!scan.total) updateProgress('목록 QR을 비추세요');
          if (!message.textContent) say(message, 'PC에서 목록 QR을 표시하세요.');
          return;
        }
        updateProgress('목록 QR ' + step.got + ' / ' + step.total);
        say(message, '');
        if (step.got < step.total) return;
        busy = true; stopCamera();
        try {
          const opened = await scan.open();
          if (!active()) return;
          decoded = opened;
        } catch (e) {
          if (active()) {
            scan.clear(); progress.textContent = '열쇠 QR을 다시 비추세요';
            retry.textContent = 'QR 다시 읽기'; retry.hidden = false;
          }
          throw e;
        } finally { busy = false; }
        if (active()) { scan.clear(); await save(); }
      } catch (e) { if (active()) say(message, readableError(e), true); }
    };
    const camera = async () => {
      if (!active() || busy) return;
      retry.hidden = true;
      try { await startCamera(video, receive); }
      catch (e) { if (active()) { say(message, readableError(e), true); retry.hidden = false; } }
    };
    body.replaceChildren(progress, h('p', { class: 'small' }, 'PC에서 열쇠 QR을 먼저 읽고, 이어서 목록 QR을 모두 비추세요.'),
      video, message, retry, restart, btn('목록으로 돌아가기', () => { stopCamera(); render(); }, 'ghost big'));
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && active() && !busy && !decoded) retry.hidden = false;
    }, { signal: controller.signal });
    progress.focus();
    camera();
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden || !shown()) return;
    if (saved && saved.work_date < todayKST()) onNewDay();
  }, { signal: controller.signal });
  showList();
  return view;
}
