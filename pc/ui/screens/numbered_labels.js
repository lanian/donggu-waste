// PC 화면: 숫자 배출표 원장. 번호는 문자열로 다루며 인쇄와 사용 기록을 구분한다.
import { api, boot, btn, canWork, chip, grid, h, kst, modal, pagehead, postOnce, routeEpoch, searchbox, toast, wrap, STATUS } from '../core.js';

const STATES = { AVAILABLE: '보관 중', DISTRIBUTED: '배부', USED: '사용', VOID: '무효' };
const ACTIONS = { RECEIVE: '입고', DISTRIBUTE: '배부', USE: '사용', VOID: '무효' };
const stateChip = (status) => chip(STATES[status] || status, status === 'USED' ? 'ok' : status === 'DISTRIBUTED' ? 'info' : status === 'VOID' ? 'neutral' : '');
const numberInput = (label, value) => h('input', { type: 'text', inputmode: 'numeric', pattern: '[0-9]{1,30}', maxlength: 30, value, 'aria-label': label, autocomplete: 'off', spellcheck: 'false' });
function rangeValues(start, end) {
  const a = start.trim(), b = end.trim();
  if (!/^[0-9]{1,30}$/.test(a) || !/^[0-9]{1,30}$/.test(b)) throw new Error('시작·끝 번호를 1~30자리 숫자로 적어 주세요.');
  if (a.length !== b.length) throw new Error('시작·끝 번호의 자릿수를 같게 적어 주세요. 앞의 0도 포함합니다.');
  const count = BigInt(b) - BigInt(a) + 1n;
  if (count < 1n) throw new Error('끝 번호는 시작 번호보다 작을 수 없습니다.');
  if (count > 5000n) throw new Error('한 번에 5,000개까지 기록할 수 있습니다. 구간을 나누어 주세요.');
  return { start: a, end: b, count: Number(count) };
}

async function screenNumberedLabels() {
  if (!['ADMIN', 'INTAKE', 'LEAD'].includes(boot.me.role)) return h('div', {}, pagehead('숫자 배출표'), h('p', { class: 'notice' }, '숫자 배출표 원장을 조회할 권한이 없습니다.'));
  if (screenNumberedLabels.owner !== boot.me.username) {
    screenNumberedLabels.owner = boot.me.username;
    screenNumberedLabels.s = { q: '', status: '', destination: '', page: 1, receive: { start: '', end: '' }, distribute: { start: '', end: '', destination: '' }, details: {} };
  }
  const s = screenNumberedLabels.s, work = canWork();
  const myRoute = routeEpoch, myHash = location.hash;
  let view, loadSeq = 0, detailSeq = 0, loadingKey = '', result = null;
  const current = () => view?.isConnected && myRoute === routeEpoch && location.hash === myHash;
  const q = numberInput('번호 찾기', s.q);
  q.placeholder = '번호 일부 · 앞 0 포함';
  const status = h('select', { 'aria-label': '번호 상태' }, h('option', { value: '', selected: !s.status }, '전체'), ...Object.entries(STATES).map(([v, t]) => h('option', { value: v, selected: v === s.status }, t)));
  const destination = h('input', { type: 'search', value: s.destination, 'aria-label': '배부처 찾기', placeholder: '배부처 일부' });
  const summary = h('div', { class: 'kpis compact', 'aria-label': '전체 원장 현황' });
  const rows = h('div'), pager = h('div', { class: 'pager' }), count = h('b', {}, '0');
  const message = h('p', { class: 'msg', role: 'status' });
  const saveFilters = () => Object.assign(s, { q: q.value, status: status.value, destination: destination.value });
  const invalidate = () => {
    saveFilters(); loadSeq++; loadingKey = ''; s.page = 1; query.disabled = false;
    rows.setAttribute('aria-busy', 'true');
    rows.replaceChildren(h('p', { class: 'empty' }, '조회조건을 바꿨습니다. [조회]를 눌러 주세요.'));
    pager.replaceChildren();
  };
  const load = async (force = false) => {
    if (!current()) return;
    saveFilters();
    if (s.q && !/^[0-9]{1,30}$/.test(s.q)) { message.textContent = '번호는 1~30자리 숫자로 찾아 주세요.'; return; }
    const params = { q: s.q, status: s.status, destination: s.destination, page: s.page };
    const signature = JSON.stringify(params);
    if (force !== true && loadingKey === signature) return;
    const my = ++loadSeq;
    loadingKey = signature; query.disabled = true;
    message.textContent = '불러오는 중…'; rows.setAttribute('aria-busy', 'true');
    rows.replaceChildren(h('p', { class: 'empty' }, '원장을 불러오는 중…')); pager.replaceChildren();
    try {
      const d = await api('numbered-labels?' + new URLSearchParams(params), { timeoutMs: 15000 });
      if (!current() || my !== loadSeq) return;
      result = d; s.page = d.page;
      count.textContent = Number(d.total).toLocaleString('ko-KR');
      summary.replaceChildren(...[['ALL', '전체'], ...Object.entries(STATES)].map(([code, label]) => h('div', { class: 'kpi' }, h('span', { class: 'k' }, label), h('b', { class: 'v' }, `${Number(d.counts?.[code] || 0).toLocaleString('ko-KR')}개`), h('span', { class: 's' }, '전체 원장 기준'))));
      rows.replaceChildren(grid(['번호', '상태', '배부처', '접수 연결', '입고 일시', '마지막 변경'], d.rows.map((x) => [
        btn(x.number, () => openDetail(x.number), 'small code', { 'aria-label': x.number }), stateChip(x.status), x.destination || '-',
        x.request_id ? h('a', { href: `#/request/${encodeURIComponent(x.request_id)}` }, '연결된 접수 보기') : '-', kst(x.received_at), kst(x.updated_at)]), { empty: '조건에 맞는 번호가 없습니다.' }));
      pager.replaceChildren(btn('‹ 이전', () => { s.page = result.page - 1; load(); }, 'small', { disabled: d.page <= 1 }), h('span', { class: 'pg' }, `${d.page} / ${d.pages}쪽 · 50개씩`), btn('다음 ›', () => { s.page = result.page + 1; load(); }, 'small', { disabled: d.page >= d.pages }));
      message.textContent = '';
    } catch (e) {
      if (!current() || my !== loadSeq) return;
      rows.replaceChildren(h('div', { class: 'notice bad', role: 'alert' }, e.message, ' ', btn('다시 시도', load, 'small')));
      message.textContent = '원장을 불러오지 못했습니다. 조회조건은 유지됩니다.';
    } finally {
      if (current() && my === loadSeq) { loadingKey = ''; query.disabled = false; rows.removeAttribute('aria-busy'); }
    }
  };
  const rangeDialog = async (type) => {
    if (!current() || !work) return;
    const distribute = type === 'distribute', draft = s[type];
    const start = numberInput('시작 번호', draft.start), end = numberInput('끝 번호', draft.end);
    const dest = h('input', { value: draft.destination || '', maxlength: 100, 'aria-label': '배부처', placeholder: '예: 가상1동 행정복지센터' });
    const rangeMessage = h('p', { class: 'muted small', 'aria-live': 'polite' });
    const remember = () => {
      draft.start = start.value; draft.end = end.value;
      if (distribute) draft.destination = dest.value;
      try { rangeMessage.textContent = `번호 ${rangeValues(start.value, end.value).count.toLocaleString('ko-KR')}개`; }
      catch (e) { rangeMessage.textContent = start.value || end.value ? e.message : '같은 자릿수의 시작·끝 번호를 적어 주세요. 한 번에 최대 5,000개입니다.'; }
    };
    [start, end, dest].forEach((input) => input.addEventListener('input', remember));
    remember();
    await modal({ title: distribute ? '번호 구간 배부' : '번호 구간 입고', ok: distribute ? '배부 기록' : '입고 기록', body: [
      h('p', { class: 'muted' }, distribute ? '보관 중인 번호 구간을 배부처에 넘긴 기록을 남깁니다.' : '실물로 받은 번호 구간을 원장에 등록합니다. 앞의 0을 포함해 적어 주세요.'),
      h('div', { class: 'grid2' }, h('label', { class: 'f' }, '시작 번호', start), h('label', { class: 'f' }, '끝 번호', end)),
      distribute ? h('label', { class: 'f' }, '배부처', dest) : null, rangeMessage,
      btn('입력 초기화', () => { start.value = ''; end.value = ''; dest.value = ''; remember(); start.focus(); }, 'small')],
      onOk: async () => {
        if (!current()) throw new Error('화면을 다시 열어 기록해 주세요.');
        const { start: a, end: b } = rangeValues(start.value, end.value);
        const body = { start: a, end: b };
        if (distribute) { if (!dest.value.trim()) throw new Error('배부처를 적어 주세요.'); body.destination = dest.value.trim(); }
        const r = await postOnce(`numbered-labels:${type}`, `numbered-labels/${type}`, body);
        if (current()) { toast(`${r.count.toLocaleString('ko-KR')}개 ${distribute ? '배부' : '입고'}를 기록했습니다`); load(true); }
        return true;
      } });
  };
  const openDetail = async (number) => {
    if (!current()) return;
    const my = ++detailSeq;
    let history;
    try { history = await api(`numbered-labels/${encodeURIComponent(number)}/history`, { timeoutMs: 15000 }); }
    catch (e) { if (current() && my === detailSeq) toast(e.message, true); return; }
    if (!current() || my !== detailSeq) return;
    const draft = (s.details[number] ||= { q: '', selected: null, line: '', reason: '' });
    const panel = h('div');
    let searchSeq = 0, historySeq = 0, mutating = false;
    const detailCurrent = () => current() && panel.isConnected && my === detailSeq;
    const redraw = () => {
      const label = history.label;
      const info = h('div', { class: 'notice info' }, stateChip(label.status), ` · 배부처 ${label.destination || '없음'} · 입고 ${kst(label.received_at)}`,
        label.request_id ? [' · ', h('a', { href: `#/request/${encodeURIComponent(label.request_id)}` }, '연결된 접수 보기')] : null);
      const localMessage = h('p', { class: 'msg', role: 'alert' });
      const printing = btn('인쇄 화면 열기', wrap(async () => {
        if (!detailCurrent() || mutating) return;
        const r = await postOnce(`numbered-labels:print:${number}`, `numbered-labels/${encodeURIComponent(number)}/print`, {});
        if (detailCurrent()) { window.open('/print/doc/' + r.token, 'dg-print'); toast('배출표 인쇄 화면을 열었습니다. 사용 처리는 별도로 기록합니다.'); }
      }, localMessage), 'small');
      const refresh = btn('상태 새로고침', wrap(async () => {
        if (!detailCurrent() || mutating) return;
        const seq = ++historySeq;
        const next = await api(`numbered-labels/${encodeURIComponent(number)}/history`, { timeoutMs: 15000 });
        if (detailCurrent() && seq === historySeq) { searchSeq++; history = next; redraw(); }
      }, localMessage), 'small');
      const fields = [];
      const mutationButtons = [];
      const save = async (action, body) => {
        if (!detailCurrent() || mutating) return;
        const seq = ++historySeq;
        let saved;
        mutating = true; mutationButtons.forEach((button) => { button.disabled = true; }); printing.disabled = refresh.disabled = true;
        try {
          saved = await postOnce(`numbered-labels:${action}:${number}`, `numbered-labels/${encodeURIComponent(number)}/${action}`, body);
        } catch (e) { if (detailCurrent()) { localMessage.className = 'err'; localMessage.textContent = e.message; } }
        finally {
          mutating = false;
          if (detailCurrent()) { mutationButtons.forEach((button) => { button.disabled = false; }); printing.disabled = refresh.disabled = false; }
        }
        if (!saved) return;
        // 상세창을 닫아도 같은 목록 화면이면 저장 결과를 반영한다. 다른 화면은 변경하지 않는다.
        if (current()) {
          load(true);
          toast(`${number}: ` + (action === 'use' ? (saved.unchanged ? '같은 접수에 이미 연결된 사용 기록입니다' : '접수에 연결하고 사용을 기록했습니다') : '무효를 기록했습니다'));
        }
        if (!detailCurrent() || seq !== historySeq) return;
        history = { label: saved.label, rows: history.rows };
        searchSeq++; redraw();
        try {
          const next = await api(`numbered-labels/${encodeURIComponent(number)}/history`, { timeoutMs: 15000 });
          if (detailCurrent() && seq === historySeq) { history = next; redraw(); }
        } catch (e) { if (detailCurrent() && seq === historySeq) toast('기록은 저장했습니다. 최신 내역을 불러오지 못했습니다. [상태 새로고침]을 눌러 주세요.', true); }
      };
      if (work && ['AVAILABLE', 'DISTRIBUTED'].includes(label.status)) {
        const search = h('input', { type: 'search', value: draft.q, 'aria-label': '접수 찾기', placeholder: '접수번호 또는 전화번호 뒷자리', maxlength: 100, 'data-label-search': '1' });
        const candidates = h('div', { 'aria-live': 'polite' });
        const selected = h('p', { class: 'muted small' });
        const line = h('select', { 'aria-label': '연결 품목' });
        const drawSelection = () => {
          selected.textContent = draft.selected ? `선택한 접수: ${draft.selected.receipt_no}` : '연결할 접수를 검색해 선택해 주세요.';
          line.replaceChildren(h('option', { value: '' }, '접수 전체에 연결 (품목 지정 안 함)'), ...(draft.selected?.items || []).map((x) => h('option', { value: x.line_id, selected: x.line_id === draft.line }, `${x.name} ${x.spec || ''} · ${x.qty}${x.unit_label || ''}`)));
          line.disabled = !draft.selected;
        };
        drawSelection();
        line.addEventListener('change', () => { draft.line = line.value; });
        const searchButton = btn('접수 검색', async () => {
          if (!detailCurrent() || mutating) return;
          if (!search.value.trim()) { localMessage.textContent = '접수번호 또는 전화번호 뒷자리를 적어 주세요.'; search.focus(); return; }
          const seq = ++searchSeq, value = search.value.trim();
          searchButton.disabled = true; candidates.textContent = '접수를 찾는 중…';
          try {
            const d = await api(`requests?${new URLSearchParams({ q: value, size: 10, page: 1 })}`, { timeoutMs: 15000 });
            if (!detailCurrent() || seq !== searchSeq) return;
            candidates.replaceChildren(...(d.rows.length ? d.rows.map((r) => h('div', { class: 'row' },
              btn(`${r.receipt_no} 선택`, () => {
                if (!detailCurrent() || mutating) return;
                draft.selected = { id: r.id, receipt_no: r.receipt_no, items: (r.items || []).filter((x) => x.line_id).map((x) => ({ line_id: x.line_id, name: x.name, spec: x.spec, qty: x.qty, unit_label: x.unit_label })) };
                draft.line = ''; drawSelection(); candidates.replaceChildren();
              }, 'small'), chip(STATUS[r.status] || r.status))) : [h('p', { class: 'empty' }, '맞는 접수가 없습니다. 검색어를 바꾸어 주세요.')]),
              d.total > d.rows.length ? h('p', { class: 'muted small' }, '후보가 많습니다. 접수번호나 검색어를 더 적어 범위를 좁혀 주세요.') : null);
          } catch (e) { if (detailCurrent() && seq === searchSeq) candidates.replaceChildren(h('p', { class: 'err', role: 'alert' }, e.message)); }
          finally { if (detailCurrent() && seq === searchSeq) searchButton.disabled = false; }
        }, 'small', { 'data-label-search-button': '1' });
        search.addEventListener('input', () => { draft.q = search.value; draft.selected = null; draft.line = ''; searchSeq++; searchButton.disabled = false; candidates.replaceChildren(); drawSelection(); });
        const use = btn('접수에 연결하고 사용 기록', () => {
          if (!draft.selected) { localMessage.textContent = '연결할 접수를 검색해 선택해 주세요.'; return; }
          save('use', { request_id: draft.selected.id, ...(draft.line ? { line_id: draft.line } : {}) });
        }, 'primary');
        mutationButtons.push(use);
        fields.push(h('section', { class: 'card' }, h('h2', {}, '접수 연결 · 사용'), h('label', { class: 'f' }, '접수 찾기', search), h('div', { class: 'row' }, searchButton), candidates, selected, h('label', { class: 'f' }, '연결 품목', line), use));
      }
      if (work && label.status !== 'VOID') {
        const reason = h('input', { value: draft.reason, maxlength: 300, 'aria-label': '무효 사유', placeholder: '분실·훼손 등 무효 사유' });
        reason.addEventListener('input', () => { draft.reason = reason.value; });
        const voidButton = btn('무효 기록', () => {
          if (!reason.value.trim()) { localMessage.textContent = '무효 사유를 적어 주세요.'; reason.focus(); return; }
          save('void', { reason: reason.value.trim() });
        }, 'small danger');
        mutationButtons.push(voidButton);
        fields.push(h('section', { class: 'card' }, h('h2', {}, '무효 처리'), h('p', { class: 'muted small' }, '원장과 접수 연결 기록은 남습니다. 무효 처리 후에는 다시 사용할 수 없습니다.'), h('label', { class: 'f' }, '무효 사유', reason), voidButton));
      }
      panel.replaceChildren(info, h('div', { class: 'row' }, printing, refresh), h('p', { class: 'muted small' }, '인쇄는 사용 기록과 별개입니다. 실제 접수에 사용할 때 [접수에 연결하고 사용 기록]을 누르세요.'),
        ...fields, localMessage, h('h3', {}, '최근 변경 내역 (최대 20건)'),
        grid(['일시', '작업', '상태', '작업자', '사유'], history.rows.map((x) => [kst(x.at), ACTIONS[x.action] || x.action, `${STATES[x.from_status] || '등록 전'} → ${STATES[x.to_status] || x.to_status}`, x.actor || '-', x.reason || '']), { empty: '변경 내역이 없습니다.' }));
    };
    redraw();
    // 공통 modal은 입력칸 Enter를 확인으로 처리한다. 이 창의 접수 찾기는 검색을 먼저 실행한다.
    const searchEnter = (event) => {
      if (event.key === 'Enter' && detailCurrent() && event.target.matches('[data-label-search]')) {
        event.preventDefault(); event.stopImmediatePropagation(); panel.querySelector('[data-label-search-button]')?.click();
      }
    };
    document.addEventListener('keydown', searchEnter, true);
    try { await modal({ title: `숫자 배출표 ${history.label.number}`, wide: true, ok: '닫기', cancel: null, body: [panel] }); }
    finally { searchSeq++; document.removeEventListener('keydown', searchEnter, true); }
  };
  const query = btn('조회', () => { s.page = 1; load(); }, 'primary');
  const reset = btn('조회 초기화', () => { q.value = ''; status.value = ''; destination.value = ''; invalidate(); load(); }, 'small');
  [q, destination].forEach((input) => { input.addEventListener('input', invalidate); input.addEventListener('keydown', (event) => { if (event.key === 'Enter') query.click(); }); });
  status.addEventListener('change', () => { invalidate(); load(); });
  view = h('div', {}, pagehead('숫자 배출표', ...(work ? [btn('번호 구간 배부', () => rangeDialog('distribute'), 'small'), btn('번호 구간 입고', () => rangeDialog('receive'), 'primary small')] : [])),
    h('p', { class: 'muted' }, '실물 배출표의 입고·배부·사용·무효를 번호별로 관리합니다. 번호를 누르면 접수 연결과 변경 내역을 볼 수 있습니다.'), summary,
    searchbox([['번호', q], ['상태', status], ['배부처', destination]], query, reset), message,
    h('div', { class: 'gridbar' }, h('span', { class: 'cnt' }, '조회 ', count, '개'), h('span', { class: 'grow' }), h('span', { class: 'muted small' }, '번호를 눌러 상세·인쇄')), rows, pager);
  setTimeout(() => { if (current()) load(); }, 0);
  return view;
}

export { screenNumberedLabels };
