// 휴대폰 화면: 하차 기록
import * as NAV from '../nav.js';
import { hasPendingSecure, isUnlocked } from '../store.js';
import { addCrewEvent, backBtn, btn, commit, errMsg, go, h, isSolo, locking, pcStops, render, saveFailed, say, st, toast, todayKST } from '../core.js';
import { finalUnloadState, intermediateUnloadState, TOLL_GUIDANCE } from '../final-unload.js';
import { NO_LOC, navBtn, unloadLoc } from '../widgets.js';

// 지도에서 위치를 정하고 돌아와도 같은 초안을 쓴다. 잠금을 풀어 상태가 바뀌면 옛 초안을 재사용하지 않는다.
const drafts = new WeakMap();
const finalDrafts = new WeakMap();
const intermediateDrafts = new WeakMap();

const pendingWrite = (op) => !!op?.prepared && ['saving', 'failed'].includes(op.status);

function finalUnloadPending(source, key) {
  const draft = finalDrafts.get(source);
  return draft?.key === key && pendingWrite(draft.operation);
}

function intermediateUnload(source, today, stops = pcStops()) {
  const draft = intermediateDrafts.get(source);
  const generic = drafts.get(source);
  const pending = pendingWrite(draft?.operation) ? draft : pendingWrite(generic?.operation) ? generic : null;
  const mid = intermediateUnloadState(source, today, stops, pending?.operation.eventSeq);
  if (!mid) return null;
  const genericPending = !!pending && pending === generic;
  return { ...mid, pending: genericPending || pending?.key === mid.key,
    route: genericPending ? '#/unload' : `#/unload/mid-${mid.stop.request_id}` };
}

function screenUnload(arg) {
  const source = st;
  const finalMode = arg === 'final';
  const intermediateMode = arg?.startsWith('mid-');
  const final = finalMode ? finalUnloadState(source, todayKST()) : null;
  const mid = !finalMode ? intermediateUnload(source, todayKST()) : null;
  const planned = finalMode || intermediateMode;
  const route = planned ? `#/unload/${arg}` : '#/unload';
  const plan = final || (intermediateMode && mid?.route === route ? mid : null);
  const title = finalMode ? '최종 하차 기록' : intermediateMode ? '중간 하차 기록' : '하차 기록';
  const draftStore = finalMode ? finalDrafts : intermediateMode ? intermediateDrafts : drafts;
  const pending = final ? finalUnloadPending(source, final.key) : !!plan?.pending;
  const page = (heading, ...content) => h('main', {}, h('header', { class: 'bar' }, backBtn('#/home'),
    h('h1', { class: 'bar-title' }, heading)), h('section', { class: 'card' }, ...content));
  if (intermediateMode && (!plan || (!pending && (saveFailed || hasPendingSecure('state'))))) {
    return page(title, h('p', { role: 'status' }, '수거 목록에서 저장 상태와 지금 할 일을 확인하세요.'),
      btn('수거 목록으로', () => go('#/home'), 'primary big'));
  }
  if (!planned && mid?.pending && mid.route !== route) {
    return page(title, h('p', { role: 'status' }, '준비한 중간 하차 기록의 저장 상태를 확인하세요.'),
      btn('중간 하차 저장 확인', () => go(mid.route), 'primary big'));
  }
  if (finalMode && (!final?.ready || (final.recorded && !pending && (saveFailed || hasPendingSecure('state'))))) {
    const reason = !final ? '오늘 배차 목록에 최종 하차 정보가 없습니다.'
      : final.missingStops ? '아직 읽지 않은 수거 건이 있습니다. 배차표의 남은 QR을 읽어 주세요.'
        : !final.ready ? '오늘 수거 목록의 방문 결과를 먼저 저장해 주세요.'
          : '저장 상태를 확인한 뒤 복귀 길안내를 열어 주세요.';
    return page(title, h('p', { role: 'status' }, reason), btn('수거 목록으로', () => go('#/home'), 'primary big'));
  }
  if (final?.recorded && !pending) {
    return page('차고지 복귀',
      h('p', {}, '최종 하차를 저장했습니다.'),
      navBtn('차고지 복귀 길안내', null, final.depot, NAV.navLabel('DEPOT'), 'big'),
      h('p', { class: 'small' }, TOLL_GUIDANCE));
  }
  const list = source.events.filter((e) => e.kind === 'UNLOAD');
  const plannedLabel = plan?.site || source.daylist?.stops.find((x) => x.unload_after)?.unload_after;
  const uLoc = plan ? plan.site : unloadLoc(plannedLabel);
  const previousDraft = draftStore.get(source);
  const newLoad = final && final.hasLoad && !final.recorded && previousDraft?.operation?.status === 'saved';
  if (!previousDraft || (plan && previousDraft.key !== plan.key) || newLoad) draftStore.set(source, {
    key: plan?.key,
    place: (plan ? plan.site.label : list.length ? list[list.length - 1].body.place : plannedLabel?.label) || '',
    time: new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date()),
    volume: '', weight: '', note: '', operation: null,
  });
  const draft = draftStore.get(source);
  let view;
  const current = () => st === source && isUnlocked() && !locking && view?.isConnected && location.hash === route;
  const field = (key, attrs) => {
    const el = h('input', { value: draft[key], ...attrs });
    el.addEventListener('input', () => { if (current() && !draft.operation) draft[key] = el.value; });
    return el;
  };
  const place = field('place', { type: 'text', class: 'search', placeholder: '반입지 이름' });
  const time = field('time', { type: 'time' });
  const vol = field('volume', { type: 'number', inputmode: 'decimal', step: '0.1', min: 0, placeholder: '예: 5.5' });
  const kg = field('weight', { type: 'number', inputmode: 'decimal', step: '1', min: 0, placeholder: '계근표 중량' });
  const note = field('note', { type: 'text', placeholder: '메모 (선택, 개인정보 적지 않기)' });
  const msg = h('p', { class: 'msg', role: 'status', 'aria-live': 'polite' });
  const fld = (t, el) => h('label', { class: 'fld' }, h('span', {}, t), el);
  const sync = () => {
    const op = draft.operation;
    if (!op) return;
    [place, time, vol, kg, note].forEach((el) => { el.disabled = true; });
    if (locationButton) locationButton.disabled = true;
    save.disabled = op.status === 'saving';
    save.textContent = op.status === 'saved' ? (final ? '차고지 복귀 안내' : plan ? '수거 목록으로' : '새 하차 기록')
      : op.status === 'saving' ? '저장 중…' : '저장 다시 시도';
    save.setAttribute('aria-busy', String(op.status === 'saving'));
    say(msg, op.status === 'saved' ? (plan ? `${final ? '최종' : '중간'} 하차를 저장했습니다.` : '하차 저장이 끝났습니다. 새 하차를 기록하려면 [새 하차 기록]을 누르세요.')
      : op.status === 'saving' ? '하차를 저장하고 있습니다. 같은 기록의 저장 결과를 기다려 주세요.'
      : `하차를 저장하지 못했습니다. 입력은 남아 있으며 같은 기록을 다시 저장합니다.${op.error ? ' ' + op.error : ''}`, op.status === 'failed');
  };
  const persist = async (op) => {
    if (!current() || op.status === 'saving') return;
    if (plan && !op.prepared) {
      const latest = final ? finalUnloadState(source, todayKST()) : intermediateUnload(source, todayKST());
      if (latest?.key !== plan.key || (final ? !latest.ready || latest.recorded : latest.pending || saveFailed || hasPendingSecure('state'))) {
        draft.operation = null;
        render();
        return;
      }
    }
    const affectsIntermediate = intermediateMode || (!planned && !!intermediateUnload(source, todayKST()));
    op.status = 'saving'; op.error = ''; sync();
    // 저장 중 다른 화면을 다녀왔으면 같은 주소로 돌아와도 이전 화면의 완료 이동은 하지 않는다.
    let left = false;
    const leave = () => { left = true; };
    window.addEventListener('hashchange', leave);
    try {
      let saved;
      if (op.prepared) saved = await commit();
      else {
        const before = source.events.length;
        const oldReceipt = source.finalUnloadReceipt;
        if (final) source.finalUnloadReceipt = final.key;
        const write = addCrewEvent('UNLOAD', op.body);
        op.prepared = source.events.length === before + 1;
        if (op.prepared) op.eventSeq = source.events[before].seq;
        if (final && !op.prepared) source.finalUnloadReceipt = oldReceipt;
        saved = await write;
        if (!op.prepared) throw new Error('하차 기록을 준비하지 못했습니다.');
      }
      op.status = saved ? 'saved' : 'failed';
    } catch (error) { op.status = 'failed'; op.error = errMsg(error); }
    finally { window.removeEventListener('hashchange', leave); }
    if (st !== source || !isUnlocked() || draft.operation !== op) return;
    op.notify?.();
    if (!left && current() && op.status === 'saved') {
      draftStore.delete(source); toast(plan ? `${final ? '최종' : '중간'} 하차를 기록했습니다` : '하차를 기록했습니다'); go('#/home');
    } else if (affectsIntermediate && op.status === 'saved' && (location.hash === '#/home' || location.hash.startsWith('#/stop/'))) {
      if (intermediateMode) draftStore.delete(source);
      render();
    }
  };
  const save = btn(title, async () => {
    if (!current()) return;
    const existing = draft.operation;
    if (existing) {
      if (existing.status === 'saved') { draftStore.delete(source); if (intermediateMode) go('#/home'); else render(); }
      else if (existing.status === 'failed') await persist(existing);
      return;
    }
    const volume = vol.value === '' ? null : Number(vol.value), weight = kg.value === '' ? null : Number(kg.value);
    if (vol.validity.badInput || kg.validity.badInput || (volume != null && (!Number.isFinite(volume) || volume < 0)) || (weight != null && (!Number.isFinite(weight) || weight < 0))
      || !(volume > 0 || weight > 0)) { say(msg, '부피·중량은 0 이상이고 하나는 0보다 커야 합니다.', true); return; }
    if (!place.value.trim()) { say(msg, '반입지 이름을 적어 주세요.', true); return; }
    const op = { status: 'ready', prepared: false, body: {
      place: place.value.trim(), at_time: time.value, volume_m3: volume, weight_kg: weight, note: note.value.trim(),
      work_date: source.daylist ? source.daylist.work_date : todayKST(),
    }, notify: () => { if (current()) sync(); } };
    draft.operation = op;
    await persist(op);
  }, 'primary dock-next');
  const locationButton = planned ? null : btn(uLoc ? '반입지 위치 고치기' : '반입지 위치 지정', () => { if (current() && !draft.operation) go('#/pick/unload'); }, 'opt', { disabled: !source.mapEnabled });
  view = h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, backBtn(intermediateMode ? '#/home' : undefined), h('h1', { class: 'bar-title' }, title)),
    h('section', { class: 'card' }, fld('반입지', place), h('div', { class: 'grid2' }, fld('시각', time), fld('부피 (m³, 추정)', vol)), fld('중량 (kg)', kg), fld('메모', note),
      h('p', { class: 'small' }, isSolo() ? '이 휴대폰에 저장되고 백업 파일에 함께 들어갑니다. 부피나 중량 중 하나는 적어 주세요.' : '결과 보내기 때 PC로 함께 갑니다. 부피나 중량 중 하나는 적어 주세요.'), msg),
    h('section', { class: 'card' }, h('h2', {}, '반입지 길안내'),
      planned ? (NAV.hasCoord(uLoc) ? navBtn('반입지 길안내', null, uLoc, NAV.navLabel('UNLOAD'), 'big') : null)
        : h('div', { class: 'grid2' }, navBtn('🧭 길안내', null, uLoc, NAV.navLabel('UNLOAD'), 'big'), locationButton),
      NAV.hasCoord(uLoc) ? h('p', { class: 'small' }, (plannedLabel && plannedLabel.lat != null ? 'PC 목록의 반입지 위치' : '이 휴대폰에 지정한 반입지 위치') + '로 안내합니다. 지도 앱에는 좌표와 "반입지"만 넘깁니다.')
        : h('p', { class: 'small nav-none' }, `길안내: ${NO_LOC}`),
      planned && NAV.hasCoord(uLoc) ? h('p', { class: 'small' }, TOLL_GUIDANCE) : null),
    list.length ? h('section', { class: 'card' }, h('h2', {}, `하차 내역 ${list.length}회`),
      h('ul', { class: 'list' }, ...list.map((e) => h('li', {}, `${e.body.at_time} ${e.body.place} · ${e.body.volume_m3 ? e.body.volume_m3 + 'm³' : ''} ${e.body.weight_kg ? e.body.weight_kg + 'kg' : ''} ${e.seq <= source.releasedSeq ? '' : (source.exports || []).some((x) => e.seq >= x.from && e.seq <= x.to) ? '(확인 대기)' : '(보낼 기록)'}`)))) : null,
    h('nav', { class: 'dock', 'aria-label': '주 동작' }, save));  // 단추 하나뿐인 하단 바는 한 칸 전체(style.css .dock:has(> .btn:only-child))
  if (draft.operation) draft.operation.notify = () => { if (current()) sync(); };
  sync();
  return view;
}

export { finalUnloadPending, intermediateUnload, screenUnload };
