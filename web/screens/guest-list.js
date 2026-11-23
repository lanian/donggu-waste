import { GuestScan, guestUpdate } from '../guest-list.js';
import * as S from '../store.js';
import { guestWorkControls } from './guest-work.js';
import { backBtn, btn, go, h, msgBox, onLock, render, say, sheet, st, startCamera, stopCamera, toast, todayKST } from '../core.js';

const STORE = 'guestList';
const WORK = 'guestWorkJournal';
export async function pendingGuestRecords() {
  const groups = await S.getSecure(WORK) || [];
  return groups.reduce((count, group) => count + (group.closed ? 0 : group.entries.length + Number(!!group.draft)), 0);
}
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
let scanOnEntry = false;
export function startGuestDispatch() {
  scanOnEntry = true;
  go('#/guest-list');
}
function leaveGuest() { scanOnEntry = false; leaveView?.(); leaveView = null; }
onLock(leaveGuest);
window.addEventListener('hashchange', () => { if (location.hash !== '#/guest-list') leaveGuest(); });
window.addEventListener('pagehide', leaveGuest);
window.addEventListener('pageshow', (event) => {
  if (event.persisted && location.hash === '#/guest-list') render();
});

export async function screenGuestList() {
  const startFromHome = scanOnEntry;
  leaveGuest();
  const owner = st, route = location.hash;
  const view = h('main'), body = h('section', { class: 'card' });
  const controller = new AbortController();
  const scan = new GuestScan();
  const sameSession = () => st === owner && S.isUnlocked();
  const alive = () => !controller.signal.aborted && sameSession() && location.hash === route;
  const shown = () => alive() && view.isConnected;
  let dayTimer, saved = null, decoded = null;
  leaveView = () => {
    controller.abort(); clearTimeout(dayTimer);
    if (view.isConnected && body.querySelector('video')) stopCamera();
    scan.clear(); saved = null; decoded = null;
    body.replaceChildren(); view.replaceChildren();
  };
  const [stored, groups] = await withStore(async () => {
    const today = todayKST();
    const list = await readCurrent(today);
    const all = await S.getSecure(WORK) || [];
    const kept = all.filter(g => !g.closed || g.work_date >= today);
    if (kept.length !== all.length) await S.putSecure(WORK, kept, {recoverOnFailure:false});
    return [list, kept];
  });
  if (!alive()) return view;
  saved = stored;
  const heading = h('h1', { class: 'bar-title' }, '배차와 현장 기록');
  view.append(h('header', { class: 'bar' }, backBtn(), heading), body);
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

  const updateWork = (ticket, change, transfer = '') => withStore(async () => {
    if (!sameSession()) throw new Error('화면을 다시 열어 기록을 확인하세요.');
    const disk = await S.getSecure(WORK) || [];
    const index = disk.findIndex(g=>g.ticket === ticket);
    const observed = groups.find(g=>g.ticket === ticket);
    const target = disk[index];
    // 전달 완료/취소는 시도 ID로 자기 예약만 처리한다. 다른 창에서 먼저 끝낼 수도 있다.
    if ((transfer !== 'settle' && (target?.revision || 0) !== (observed?.revision || 0)) || target?.closed || (observed && !target))
      throw new Error('다른 창에서 현장 기록이 바뀌었습니다. 배차 화면을 다시 열어 확인하세요.');
    if (!transfer && target?.delivery) throw new Error('파일 전달이 진행 중입니다. 같은 결과 파일을 저장한 뒤 계속하세요.');
    let group = target;
    if (!group) {
      const current = await S.getSecure(STORE);
      if (!current || current.ticket !== ticket || current.work_date < todayKST()) throw changed();
      group = {ticket, return_key:current.return_key, crew:current.crew, work_date:current.work_date,
        issue_no:current.issue_no, entries:[], draft:null, exported:0, revision:0};
    }
    if (change(group) === false) return;
    group.revision++;
    if (index < 0) disk.push(group);
    if (!sameSession()) throw new Error('화면을 다시 열어 기록을 확인하세요.');
    await S.putSecure(WORK,disk,{recoverOnFailure:false});
    if (observed) groups.splice(groups.indexOf(observed),1,group);
    else groups.push(group);
  });

  function showList() {
    scan.clear(); decoded = null;
    const message = msgBox();
    const work = saved?.schema === 'dg.guest-work/1';
    const controls = guestWorkControls({list:saved,groups,update:updateWork,body,active:shown,finish:showList,confirm});
    body.replaceChildren(...(saved ? [
      h('h2', {}, titleOf(saved)),
      h('p', { class: 'small' }, (work ? '현장 업무 · ' : '조회 전용 · ') + saved.assign_ver + '차 배차 · 방문 ' + saved.stops.length + '곳'),
      controls.actions,
      saved.stops.length ? h('ol', { class: 'stops', 'aria-label': '방문 순서' }, ...saved.stops.map((stop) =>
        h('li', { class: 'card', value: stop.seq },
          h('h3', {}, stop.seq + '번 · ' + (stop.address || stop.dong || '주소 미입력')),
          stop.dong && stop.address ? h('p', { class: 'small' }, stop.dong) : null,
          ...stop.items.map((item) => h('p', {}, [item.name, item.spec, item.qty + item.unit].filter(Boolean).join(' · '))),
          controls.stopAction(stop)))) :
        h('p', {}, '이번 배차에는 방문할 수거지가 없습니다.'),
    ].filter(Boolean) : [h('p', {}, '받은 배차 목록이 없습니다. PC의 목록 전송 화면에서 배차 QR을 여세요.')]),
    ...controls.logs,
    btn('배차 QR 읽기', () => showScanner(), 'primary big'),
    ...(saved ? [btn('목록 지우기', async () => {
      if (!await confirm('조회 목록을 지울까요?', titleOf(saved) + ' 배차를 이 휴대폰에서 지웁니다.' + (work ? ' 저장한 현장 기록과 초안은 별도로 보관됩니다.' : ''), '지우기', 'danger')) return;
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
  function showScanner(fromHome = false) {
    scan.clear(); decoded = null;
    heading.textContent = '배차 QR 읽기';
    const video = h('video', { playsinline: true, muted: true, class: 'cam' });
    const preview = h('div', { class: 'guest-camera-view' }, video);
    const keyStep = h('li', {}, '1단계 · 열쇠 QR'), listStep = h('li', {}, '2단계 · 목록 QR');
    const steps = h('ol', { class: 'guest-scan-steps', 'aria-label': '배차 QR 읽는 순서' }, keyStep, listStep);
    const headline = h('strong'), hint = h('span');
    const stage = h('div', { class: 'guest-scan-stage', role: 'status', 'aria-atomic': 'true', tabindex: '-1' }, headline, hint);
    const progress = h('p', { class: 'count', role: 'status', hidden: true });
    function showStage(hasKey) {
      stage.classList.toggle('key-read', hasKey);
      (hasKey ? keyStep : listStep).removeAttribute('aria-current');
      (hasKey ? listStep : keyStep).setAttribute('aria-current', 'step');
      keyStep.classList.toggle('complete', hasKey);
      headline.textContent = hasKey ? '열쇠 읽음' : '열쇠 QR을 비추세요';
      hint.textContent = hasKey ? 'PC에서 [목록 QR 표시]를 누르세요.' : 'PC에 표시된 열쇠 QR을 카메라 가운데에 맞추세요.';
    }
    const message = msgBox(), retry = btn('카메라 다시 열기', () => decoded ? save() : camera(), 'big', { hidden: true });
    const active = () => shown() && body.contains(video);
    let busy = false;
    const restart = btn('열쇠부터 다시 읽기', () => {
      if (!active() || busy) return;
      scan.clear(); decoded = null; restart.hidden = true;
      showStage(false); updateProgress(''); say(message, '');
      retry.textContent = '카메라 다시 열기';
      stage.focus(); camera();
    }, 'big', { hidden: true });
    const updateProgress = (text) => {
      if (progress.textContent !== text) {
        progress.hidden = !text;
        progress.textContent = text;
      }
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
        if (active()) { toast(choice === 'duplicate' ? '이미 받은 배차 목록입니다' : '배차 목록을 저장했습니다'); render(); }
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
          if (!stage.classList.contains('key-read')) { showStage(true); say(message, ''); }
          if (!scan.total) updateProgress('목록 QR을 비추세요');
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
            scan.clear(); showStage(false); updateProgress(''); restart.hidden = true;
            retry.textContent = 'QR 다시 읽기'; retry.hidden = false;
          }
          throw e;
        } finally { busy = false; }
        if (active()) { scan.clear(); await save(); }
      } catch (e) { if (active()) say(message, readableError(e), true); }
    };
    const camera = async () => {
      if (!active() || busy) return;
      retry.hidden = true; preview.hidden = false; say(message, '');
      try { await startCamera(video, receive); }
      catch (e) {
        if (active()) {
          preview.hidden = true; say(message, readableError(e), true); retry.hidden = false;
        }
      }
    };
    showStage(false);
    body.classList.add('guest-scanner');
    const cancel = btn(fromHome ? '촬영 취소' : '목록으로 돌아가기', () => {
      stopCamera();
      if (fromHome) go('#/home'); else render();
    }, 'ghost big');
    body.replaceChildren(steps, stage, progress, message, retry, preview,
      h('div', { class: 'guest-scan-actions' }, restart, cancel));
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && active() && !busy && !decoded) retry.hidden = false;
    }, { signal: controller.signal });
    // 라우터가 새 화면을 붙인 뒤에만 카메라를 요청한다. 그 사이 떠난 화면은 촬영하지 않는다.
    requestAnimationFrame(() => {
      if (active()) { stage.focus(); camera(); }
    });
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden || !shown()) return;
    if (saved && saved.work_date < todayKST()) onNewDay();
  }, { signal: controller.signal });
  if (startFromHome && !saved && !groups.some(g => !g.closed && (g.entries.length || g.draft))) showScanner(true);
  else showList();
  return view;
}
