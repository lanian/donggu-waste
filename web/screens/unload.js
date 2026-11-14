// 휴대폰 화면: 하차 기록
import * as NAV from '../nav.js';
import { isUnlocked } from '../store.js';
import { addCrewEvent, btn, commit, errMsg, go, h, isSolo, locking, render, st, toast, todayKST } from '../core.js';
import { NO_LOC, navBtn, unloadLoc } from '../widgets.js';

// 지도에서 위치를 정하고 돌아와도 같은 초안을 쓴다. 잠금을 풀어 상태가 바뀌면 옛 초안을 재사용하지 않는다.
const drafts = new WeakMap();
function screenUnload() {
  const source = st;
  const list = source.events.filter((e) => e.kind === 'UNLOAD');
  const plannedLabel = source.daylist && (source.daylist.stops.find((x) => x.unload_after) || {}).unload_after;
  const uLoc = unloadLoc(plannedLabel);
  if (!drafts.has(source)) drafts.set(source, {
    place: (list.length ? list[list.length - 1].body.place : plannedLabel?.label) || '',
    time: new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date()),
    volume: '', weight: '', note: '', operation: null,
  });
  const draft = drafts.get(source);
  let view;
  const current = () => st === source && isUnlocked() && !locking && view?.isConnected && location.hash === '#/unload';
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
    locationButton.disabled = true;
    save.disabled = op.status === 'saving';
    save.textContent = op.status === 'saved' ? '새 하차 기록' : op.status === 'saving' ? '저장 중…' : '저장 다시 시도';
    save.setAttribute('aria-busy', String(op.status === 'saving'));
    msg.textContent = op.status === 'saved' ? '하차 저장이 끝났습니다. 새 하차를 기록하려면 [새 하차 기록]을 누르세요.'
      : op.status === 'saving' ? '하차를 저장하고 있습니다. 같은 기록의 저장 결과를 기다려 주세요.'
      : `하차를 저장하지 못했습니다. 입력은 남아 있으며 같은 기록을 다시 저장합니다.${op.error ? ' ' + op.error : ''}`;
    msg.classList.toggle('err', op.status === 'failed');
  };
  const persist = async (op) => {
    if (!current() || op.status === 'saving') return;
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
        const write = addCrewEvent('UNLOAD', op.body);
        op.prepared = source.events.length === before + 1;
        saved = await write;
        if (!op.prepared) throw new Error('하차 기록을 준비하지 못했습니다.');
      }
      op.status = saved ? 'saved' : 'failed';
    } catch (error) { op.status = 'failed'; op.error = errMsg(error); }
    finally { window.removeEventListener('hashchange', leave); }
    if (st !== source || !isUnlocked() || draft.operation !== op) return;
    op.notify?.();
    if (!left && current() && op.status === 'saved') {
      drafts.delete(source); toast('하차를 기록했습니다'); go('#/home');
    }
  };
  const save = btn('하차 기록', async () => {
    if (!current()) return;
    const existing = draft.operation;
    if (existing) {
      if (existing.status === 'saved') { drafts.delete(source); render(); }
      else if (existing.status === 'failed') await persist(existing);
      return;
    }
    const volume = vol.value === '' ? null : Number(vol.value), weight = kg.value === '' ? null : Number(kg.value);
    if (vol.validity.badInput || kg.validity.badInput || (volume != null && (!Number.isFinite(volume) || volume < 0)) || (weight != null && (!Number.isFinite(weight) || weight < 0))
      || !(volume > 0 || weight > 0)) { msg.textContent = '부피·중량은 0 이상이고 하나는 0보다 커야 합니다.'; msg.classList.add('err'); return; }
    if (!place.value.trim()) { msg.textContent = '반입지 이름을 적어 주세요.'; return; }
    const op = { status: 'ready', prepared: false, body: {
      place: place.value.trim(), at_time: time.value, volume_m3: volume, weight_kg: weight, note: note.value.trim(),
      work_date: source.daylist ? source.daylist.work_date : todayKST(),
    }, notify: () => { if (current()) sync(); } };
    draft.operation = op;
    await persist(op);
  }, 'primary dock-next');
  const locationButton = btn(uLoc ? '반입지 위치 고치기' : '반입지 위치 지정', () => { if (current() && !draft.operation) go('#/pick/unload'); }, 'opt', { disabled: !source.mapEnabled });
  view = h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '하차 기록')),
    h('section', { class: 'card' }, fld('반입지', place), h('div', { class: 'grid2' }, fld('시각', time), fld('부피 (m³, 추정)', vol)), fld('중량 (kg)', kg), fld('메모', note),
      h('p', { class: 'small' }, isSolo() ? '이 휴대폰에 저장되고 백업 파일에 함께 들어갑니다. 부피나 중량 중 하나는 적어 주세요.' : '결과 보내기 때 PC로 함께 갑니다. 부피나 중량 중 하나는 적어 주세요.'), msg),
    h('section', { class: 'card' }, h('h2', {}, '반입지 길안내'),
      h('div', { class: 'grid2' }, navBtn('🧭 길안내', null, uLoc, NAV.navLabel('UNLOAD'), 'big'), locationButton),
      uLoc ? h('p', { class: 'small' }, (plannedLabel && plannedLabel.lat != null ? 'PC 목록의 반입지 위치' : '이 휴대폰에 지정한 반입지 위치') + '로 안내합니다. 지도 앱에는 좌표와 "반입지"만 넘깁니다.')
        : h('p', { class: 'small nav-none' }, `길안내: ${NO_LOC}`)),
    list.length ? h('section', { class: 'card' }, h('h2', {}, `하차 내역 ${list.length}회`),
      h('ul', { class: 'list' }, ...list.map((e) => h('li', {}, `${e.body.at_time} ${e.body.place} · ${e.body.volume_m3 ? e.body.volume_m3 + 'm³' : ''} ${e.body.weight_kg ? e.body.weight_kg + 'kg' : ''} ${e.seq > source.releasedSeq ? '(미전송)' : ''}`)))) : null,
    h('nav', { class: 'dock' }, h('span', {}), h('span', {}), save));
  if (draft.operation) draft.operation.notify = () => { if (current()) sync(); };
  sync();
  return view;
}

export { screenUnload };
