// 날짜와 상태에 가려진 접수 찾기. 현재 휴대폰에 보관 중인 자료만 읽는다.
import { deriveStop, matchText } from '../fees.js';
import { allStops, appLogo, btn, go, h, isSolo, render, st, tabBar } from '../core.js';
import { isStopDone, stopCard, stopHay } from './stoplist.js';

const filtersByState = new WeakMap();
const defaults = () => ({ q: '', range: 'ALL', day: '', status: 'ALL' });
function filters() {
  if (!filtersByState.has(st)) filtersByState.set(st, {});
  const modes = filtersByState.get(st), mode = isSolo() ? 'SOLO' : 'PC';
  return modes[mode] ||= defaults();
}
export function openFinder(options = {}) {
  Object.assign(filters(), defaults());
  if (typeof options.q === 'string') filters().q = options.q;
  go('#/find');
}

export function screenFind() {
  const source = st, f = filters();
  let view, timer;
  const current = () => st === source && view?.isConnected && location.hash === '#/find';
  const solo = isSolo(), received = new Set(solo ? [] : (source.daylist?.stops || []).map((s) => s.request_id));
  const every = allStops().map((s) => ({ s, d: deriveStop(s, source.events), received: received.has(s.request_id),
    date: received.has(s.request_id) ? source.daylist.work_date || '' : s.planned_date || '' }));
  const q = h('input', { type: 'search', class: 'search', value: f.q, placeholder: '접수번호·이름·연락처·주소·품목', 'aria-label': '찾을 접수' });
  q.addEventListener('input', () => {
    f.q = q.value; clearTimeout(timer);
    timer = setTimeout(async () => { if (!current()) return; await render();
      if (st !== source || location.hash !== '#/find') return;
      const input = document.querySelector('input[aria-label="찾을 접수"]');
      input?.focus(); input?.setSelectionRange(input.value.length, input.value.length);
    }, 200);
  });
  const change = (key, value) => { if (!current()) return; f[key] = value; render(); };
  const range = h('select', { 'aria-label': '찾기 날짜 범위', onchange: (e) => change('range', e.target.value) },
    ...[['ALL', '전체 기간'], ['DAY', '예정일 선택'], ['UNDATED', '예정일 없음']].map(([value, text]) => h('option', { value, selected: f.range === value }, text)));
  const date = h('input', { type: 'date', value: f.day, 'aria-label': '찾기 예정일', onchange: (e) => change('day', e.target.value), disabled: f.range !== 'DAY' });
  const status = h('select', { 'aria-label': '찾기 처리 상태', onchange: (e) => change('status', e.target.value) },
    ...[['ALL', '모든 상태'], ['OPEN', '미완료'], ['DONE', '완료'], ['CANCELLED', '취소']].map(([value, text]) => h('option', { value, selected: f.status === value }, text)));
  const found = every.filter(({ s, d, date }) => {
    const cancelled = s.status === 'CANCELLED', done = isStopDone(s, d);
    if (f.range === 'UNDATED' && date || f.range === 'DAY' && (!f.day || date !== f.day)) return false;
    if (f.status === 'DONE' && (!done || cancelled) || f.status === 'OPEN' && (done || cancelled) || f.status === 'CANCELLED' && !cancelled) return false;
    const hay = stopHay(s, d);
    return !f.q.trim() || matchText(hay, f.q.trim());
  }).sort((a, b) => (a.date || '9999').localeCompare(b.date || '9999') || a.s.seq - b.s.seq);
  const period = f.range === 'ALL' ? '전체 기간' : f.range === 'UNDATED' ? '예정일 없음' : f.day ? `예정일 ${f.day}` : '예정일을 선택하세요';
  view = h('main', { class: 'with-tabs' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), appLogo(), h('h1', { class: 'bar-title' }, '날짜·접수 찾기')),
    h('section', { class: 'card' },
      h('p', { class: 'small', 'aria-label': '찾기 자료 범위' }, solo ? '이 휴대폰에 보관 중인 전체 접수입니다.' : '이 휴대폰의 받은 목록과 현장 접수에서 찾습니다. 받은 목록은 목록일, 현장 접수는 예정일 기준입니다. PC 전체 과거 내역은 PC에서 조회하세요.'),
      h('label', { class: 'fld' }, h('span', {}, '접수번호·이름·연락처·주소·품목'), q),
      h('div', { class: 'grid2' }, h('label', { class: 'fld' }, h('span', {}, solo ? '예정일 기준' : '목록일·예정일 기준'), range), h('label', { class: 'fld' }, h('span', {}, '날짜'), date)),
      h('label', { class: 'fld' }, h('span', {}, '처리 상태'), status),
      btn('조회조건 초기화', () => { Object.assign(f, defaults()); render(); }, 'big'),
      h('p', { class: 'small', role: 'status', 'aria-label': '찾기 조회 범위' }, `${period} · ${status.selectedOptions[0]?.textContent || '모든 상태'} · ${found.length}/${every.length}건`)),
    ...(found.length ? [h('ul', { class: 'stops' }, ...found.map(({ s, d, date, received }) => h('li', {},
      h('p', { class: 'small' }, `${received ? '목록일' : '예정일'} ${date || '없음'}`), stopCard(s, d, { solo }))))]
      : [h('p', { class: 'empty' }, f.range === 'DAY' && !f.day ? '예정일을 선택하거나 전체 기간으로 바꾸세요.' : '이 조건에 맞는 접수가 없습니다. 조회조건 초기화로 보관 중인 전체 자료를 확인하세요.')]),
    tabBar('list'));
  return view;
}
