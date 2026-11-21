// 오늘 현황과 동별 관리에서 같은 후속조치 목록·상세를 사용한다.
import { CONTACT, REASON, STATUS, api, boot, btn, chip, h, kst, modal, routeEpoch, sessionOwner, toast } from '../core.js';

const STATES = { NEEDS_ACTION: '조치 필요', OPEN: '대기', IN_PROGRESS: '처리 중', DONE: '조치 완료' };
const HISTORY = { CREATE: '등록', UPDATE: '수정', REOPEN: '다시 열기', COMPLETE: '완료' };
const option = (value, label, selected = false) => h('option', { value, selected }, label);
const field = (label, input) => h('label', { class: 'f' }, label, input);

function replaceChildren(target, ...children) {
  target.replaceChildren(...children.filter((child) => child != null));
}

export function followupQueue({ scoped = false } = {}) {
  const epoch = routeEpoch, owner = sessionOwner();
  const results = h('div', { 'aria-live': 'polite' });
  const dong = h('select', {}, option('all', '전체 행정동'));
  const assignee = h('select', {}, option('', '전체 담당자'));
  const state = h('select', {}, option('open', '처리할 건'), option('done', '완료한 건'));
  const overdue = h('input', { type: 'checkbox' });
  let page = 1, seq = 0;
  const section = h('details', { class: scoped ? 'card' : '' },
    h('summary', {}, '접수 후속조치'),
    h('div', { class: 'row', role: 'group', 'aria-label': '후속조치 조회 조건' },
      field('후속조치 행정동', dong), field('후속조치 담당자', assignee), field('후속조치 상태', state),
      h('label', {}, overdue, ' 기한 초과만')),
    results);
  const current = () => section.isConnected && routeEpoch === epoch && sessionOwner() === owner;
  async function load() {
    const mine = ++seq;
    const filters = new URLSearchParams({ dong: dong.value, assignee: assignee.value, status: state.value, overdue: overdue.checked ? '1' : '0', page });
    results.replaceChildren(h('p', { class: 'muted small' }, '후속조치를 불러오는 중…'));
    try {
      const data = await api(`dong-work/followups?${filters}`, { _prefsOwner: owner, timeoutMs: 10_000 });
      if (!current() || mine !== seq) return;
      replaceChildren(dong, option('all', '전체 행정동', filters.get('dong') === 'all'),
        ...data.dongs.map((d) => option(d.id, d.name, d.id === filters.get('dong'))),
        scoped && boot.me.role === 'DONG' ? null : option('-', '동 미지정', filters.get('dong') === '-'));
      assignee.replaceChildren(option('', '전체 담당자'), option('-', '담당 미지정', filters.get('assignee') === '-'),
        ...data.assignees.map((a) => option(a.id, a.name, a.id === filters.get('assignee'))));
      replaceChildren(results, data.rows.length ? h('ul', { class: 'todo' }, ...data.rows.map((r) => {
        const late = r.status !== 'DONE' && r.due_date && r.due_date < data.today;
        return h('li', {},
          h('a', { href: `#/${scoped ? 'dongs' : 'request'}/${encodeURIComponent(r.id)}?focus=followup` }, r.receipt_no),
          ' ', chip(STATES[r.status], r.status === 'DONE' ? 'ok' : 'warn'),
          h('div', { class: 'small' }, [r.dong || '동 미지정', r.assignee || '담당 미지정', r.due_date].filter(Boolean).join(' · '),
            late ? [' ', chip('기한 초과', 'bad')] : null),
          r.outcome || r.new_contact ? h('div', { class: 'muted small' },
            [STATUS[r.outcome], r.new_contact && '독촉·변경요청'].filter(Boolean).join(' · ')) : null);
      })) : h('p', { class: 'empty' }, '조건에 맞는 후속조치가 없습니다.'),
      page > 1 || data.has_next ? h('div', { class: 'row' },
        btn('이전', () => { page--; load(); }, 'small', { disabled: page === 1 }),
        h('span', { class: 'small' }, `${page}쪽`),
        btn('다음', () => { page++; load(); }, 'small', { disabled: !data.has_next })) : null);
    } catch (error) {
      if (current() && mine === seq) results.replaceChildren(h('p', { role: 'alert' }, error.message), btn('다시 조회', load, 'small'));
    }
  }
  dong.addEventListener('change', () => { assignee.value = ''; page = 1; load(); });
  state.addEventListener('change', () => { overdue.checked = false; overdue.disabled = state.value === 'done'; page = 1; load(); });
  for (const input of [assignee, overdue]) input.addEventListener('change', () => { page = 1; load(); });
  section.addEventListener('toggle', () => { if (section.open) load(); });
  return section;
}

export function followupCard(id, { afterRevisit } = {}) {
  const epoch = routeEpoch, owner = sessionOwner();
  const path = `dong-work/followups/${encodeURIComponent(id)}`;
  const title = h('h2', { tabindex: -1 }, '접수 후속조치');
  const body = h('div', {}, h('p', { class: 'muted small' }, '후속조치를 불러오는 중…'));
  const card = h('section', { class: 'card', id: 'followup', 'aria-label': '접수 후속조치' }, title, body);
  const current = () => card.isConnected && routeEpoch === epoch && sessionOwner() === owner;
  let seq = 0;
  const verifyView = () => { if (!current()) throw new Error('접수 화면이 바뀌었습니다. 다시 열어 주세요.'); };
  const save = (url, body) => api(url, { method: 'POST', body, _prefsOwner: owner });

  async function edit(data) {
    const assignee = h('select', {}, option('', '담당자 선택'),
      ...data.assignees.map((a) => option(a.id, a.name, a.id === data.assignee_id)));
    const due = h('input', { type: 'date', value: data.due_date || '' });
    const action = h('textarea', { maxlength: 500 }, data.next_action || '');
    const state = h('select', {}, ...['OPEN', 'IN_PROGRESS', 'DONE'].map((s) => option(s, STATES[s], s === data.status)));
    let stale = false;
    const saved = await modal({ title: '후속조치 저장', ok: '저장', enterSubmit: false,
      body: [field('담당자', assignee), field('처리기한', due), field('다음 조치', action), field('처리 상태', state)],
      onOk: async () => {
        verifyView();
        try {
          return await save(path, { assignee_id: assignee.value, due_date: due.value, next_action: action.value,
            status: state.value, version: data.version, signal: data.signal });
        } catch (error) {
          if (error.status === 409) stale = true;
          throw error;
        }
      } });
    if (current() && (saved || stale)) await load();
    if (current() && saved) toast(state.value === 'DONE' ? '후속조치를 완료했습니다' : '후속조치를 저장했습니다');
  }

  async function revisit(data) {
    const a = data.appointment;
    const date = h('input', { type: 'date', value: a.date || '' });
    const start = h('input', { type: 'time', value: a.start || '' });
    const end = h('input', { type: 'time', value: a.end || '' });
    let stale = false;
    const saved = await modal({ title: '재방문 약속', ok: '약속 확정', enterSubmit: false,
      body: [field('재방문 날짜', date), field('방문 시작 (선택)', start), field('방문 종료 (선택)', end)],
      onOk: async () => {
        verifyView();
        try {
          return await save(path + '/revisit', { req_ver: data.req_ver, date: date.value, start: start.value, end: end.value });
        } catch (error) {
          if (error.status === 409) stale = true;
          throw error;
        }
      } });
    if (!current()) return;
    if (saved) {
      toast('재방문 약속을 확정했습니다');
      await afterRevisit?.(saved, data.req_ver);
    }
    if (current() && (saved || stale)) await load();
  }

  function draw(d) {
    card.hidden = !d.status;
    if (!d.status) return;
    const a = d.appointment;
    const set = d.editable && d.status !== 'DONE';
    const hadFocus = body.contains(document.activeElement);
    const historyOpen = body.querySelector('details[data-history]')?.open;
    const editButton = set ? btn('후속조치 수정', () => edit(d), 'small primary') : null;
    const contactLine = (c) => `${c.kind} · ${kst(c.at)} · ${c.content}`;
    const metaText = (m) => [m.assignee, m.due_date, STATES[m.status]].filter(Boolean).join(' · ');
    replaceChildren(body,
      h('div', { class: 'row' }, chip(STATES[d.status], d.status === 'DONE' ? 'ok' : 'warn'),
        d.assignee || '담당 미지정', d.due_date ? `처리기한 ${d.due_date}` : null,
        editButton),
      d.next_action ? h('p', {}, '다음 조치 · ', d.next_action) : null,
      d.contacts.length ? h('p', {}, contactLine(d.contacts[0])) : null,
      d.contacts.length > 1 ? h('details', {}, h('summary', {}, '이전 독촉·변경요청'),
        h('ul', {}, ...d.contacts.slice(1).map((c) => h('li', {}, contactLine(c))))) : null,
      h('p', {}, `수거 상태 · ${STATUS[d.request_status]}`,
        d.outcome && d.outcome !== d.request_status ? ` · 최근 수거 결과 ${STATUS[d.outcome]}` : null,
        d.field_contact && d.field_contact !== 'NOT_CONTACTED' ? ` · 연락 ${CONTACT[d.field_contact] || d.field_contact}` : null),
      d.remaining.length ? h('ul', { 'aria-label': '남은 품목과 사유' }, ...d.remaining.map((l) =>
        h('li', {}, `${[l.name, l.spec].filter(Boolean).join(' ')} · 남은 ${l.remaining}${l.unit_label || ''}`,
          l.reason ? ` · ${REASON[l.reason] || l.reason}` : ''))) : null,
      d.collection_note ? h('p', {}, d.collection_note) : null,
      h('div', { class: 'row' },
        h('span', {}, a.confirmed ? '재방문 약속 확정' : '현재 수거 일정',
          a.date ? ` · ${a.date}${a.start ? ' ' + a.start : ''}${a.end ? ' ~ ' + a.end : ''}` : ' · 미정'),
        set && !['DONE', 'CANCELLED'].includes(d.request_status) ? btn('재방문 약속', () => revisit(d), 'small') : null),
      d.history.length ? h('details', { 'data-history': true, open: historyOpen }, h('summary', {}, '후속조치 이력'),
        h('ul', {}, ...d.history.map((e) => h('li', {},
          `${kst(e.at)} · ${e.actor || ''} · ${HISTORY[e.action]} · `,
          e.before ? `${metaText(e.before)} → ` : '', metaText(e.after))))) : null);
    if (hadFocus) (editButton || title).focus({ preventScroll: true });
  }
  async function load() {
    const mine = ++seq;
    try {
      const d = await api(path, { _prefsOwner: owner, timeoutMs: 10_000 });
      if (current() && mine === seq) draw(d);
    } catch (error) {
      if (current() && mine === seq) {
        card.hidden = false;
        body.replaceChildren(h('p', { role: 'alert' }, error.message), btn('다시 조회', load, 'small'));
      }
    }
  }
  setTimeout(() => { if (current()) load(); }, 0);
  return card;
}
