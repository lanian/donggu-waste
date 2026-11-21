// 자동 배정과 실물 재고의 동별 현황. 읽기만으로 번호를 소모하지 않는다.
import { api, boot, btn, canWork, grid, h, postOnce, toast } from '../core.js';

const number = (v) => Number(v || 0).toLocaleString('ko-KR');
const REASONS = {
  MISSING_DONG: '접수의 행정동을 확인하세요.',
  MISSING_DATE: '수거 기록의 실제 수거일을 확인하세요.',
  NO_STOCK: '재고 부족 · 해당 동의 실물 재고를 확인하세요.',
  LATE_STOCK: '수거일 이후에 받은 재고입니다. 실물 수령일을 확인하세요.',
  CONFLICT: '기존 배정과 수거 기록이 일치하지 않습니다. 원장에서 재고 동과 일자를 확인하세요.',
  READY: '수거일에 사용 가능한 재고가 있습니다. 다시 배정해 보세요.',
};

function labelStock(parentCurrent, onSync, showLedger) {
  if (labelStock.owner !== boot.me.username) {
    labelStock.owner = boot.me.username;
    labelStock.state = { dong: '', page: 1 };
  }
  const state = labelStock.state, dongs = boot.dongs || [];
  if (state.dong && !dongs.some((d) => d.id === state.dong)) Object.assign(state, { dong: '', page: 1 });
  let panel, sequence = 0, busy = false, loaded = false;
  const current = () => panel?.isConnected && parentCurrent();
  const scope = h('select', { 'aria-label': '자동 배정 행정동' }, h('option', { value: '' }, '전체 행정동'),
    ...dongs.map((d) => h('option', { value: d.id, selected: state.dong === d.id }, d.name)));
  const summary = h('div', { 'aria-live': 'polite' }), message = h('p', { class: 'msg', role: 'status' });
  const contents = h('div'), inventory = h('div');
  const issues = h('section', { 'aria-label': '미배정 수거 내역' }, h('h3', {}, '미배정 수거 내역'), contents);

  const drawIssues = (data) => {
    state.page = data.page;
    if (!data.rows.length) {
      contents.replaceChildren(h('p', { class: 'empty' }, '미배정 수거분이 없습니다.'));
      return;
    }
    const table = grid(['접수번호', '행정동', '수거일', '미배정', '확인할 내용'], data.rows.map((row) => [
      h('a', { href: '#/request/' + encodeURIComponent(row.request_id) }, row.receipt_no),
      row.dong_name || '동 미지정', row.date || '일자 미확인', number(row.pending) + '장',
      h('div', {}, ...row.reasons.map((code) => h('p', { class: 'small' }, REASONS[code] || '접수와 재고 이력을 확인하세요.'))),
    ]), { num: [3] });
    contents.replaceChildren(table);
    if (data.pages > 1) contents.append(h('div', { class: 'pager' },
      btn('미배정 이전', () => { state.page--; load(); }, 'small', { disabled: data.page <= 1 }),
      h('span', { class: 'pg' }, `${data.page} / ${data.pages}쪽 · ${number(data.total)}건`),
      btn('미배정 다음', () => { state.page++; load(); }, 'small', { disabled: data.page >= data.pages })));
  };

  const load = async () => {
    if (!current()) return;
    const my = ++sequence, params = new URLSearchParams({ issues_page: state.page });
    if (state.dong) params.set('dong_id', state.dong);
    retry.textContent = state.dong ? scope.selectedOptions[0].textContent + ' 미배정 다시 배정' : '미배정 다시 배정';
    loaded = false; query.disabled = retry.disabled = true;
    contents.setAttribute('aria-busy', 'true');
    summary.replaceChildren(); contents.replaceChildren(); inventory.replaceChildren();
    message.textContent = '재고와 미배정 내역을 불러오는 중…';
    try {
      const d = await api('label-auto?' + params, { timeoutMs: 15000 });
      if (!current() || my !== sequence) return;
      summary.replaceChildren(
        h('div', { class: 'kpis compact' }, ...[['필요 장수', d.required], ['배정 장수', d.assigned], ['미배정 장수', d.pending]].map(([label, value]) =>
          h('div', { class: 'kpi' }, h('span', { class: 'k' }, label), h('b', { class: 'v', 'aria-label': label }, number(value) + '장')))),
        ...(d.manual_excess || d.conflicts ? [h('p', { class: 'notice bad' }, `수동 사용 초과 ${number(d.manual_excess)}장 · 동·일자 확인 필요 ${number(d.conflicts)}장. 번호 상세에서 연결과 재고 동을 확인해 주세요.`)] : []));
      inventory.replaceChildren(grid(['행정동', '배정 가능 재고', '사용', '무효'], d.dongs.map((d) => [
        showLedger ? btn(d.name, () => showLedger(d.id), 'small', { 'aria-label': d.name + ' 번호 원장 보기' }) : d.name,
        number(d.available) + '장', number(d.used) + '장', number(d.voided) + '장',
      ]), { num: [1, 2, 3], sort: { key: 'label-stock', cols: ['text', 'num', 'num', 'num'] } }));
      drawIssues(d.issues);
      message.textContent = '';
      loaded = true;
    } catch (error) {
      if (current() && my === sequence) message.textContent = error.message;
    } finally {
      if (current() && my === sequence) {
        contents.removeAttribute('aria-busy');
        query.disabled = busy; retry.disabled = busy || !loaded;
      }
    }
  };
  const query = btn('재고 현황 새로고침', load, 'small');
  const retry = btn('미배정 다시 배정', async () => {
    if (!current() || busy || !loaded) return;
    const dong = state.dong, body = dong ? { dong_id: dong } : {};
    busy = true; scope.disabled = query.disabled = retry.disabled = true;
    try {
      const d = await postOnce('label-auto:sync:' + dong, 'label-auto/sync', body);
      if (current()) {
        toast(`${number(d.new_assigned)}장 배정 · 미배정 ${number(d.pending)}장`);
        await Promise.all([load(), onSync()]);
      }
    } catch (error) { if (current()) message.textContent = error.message; }
    finally {
      busy = false;
      if (current()) { scope.disabled = query.disabled = false; retry.disabled = !loaded; }
    }
  }, 'small', { disabled: true });
  scope.addEventListener('change', () => { state.dong = scope.value; state.page = 1; load(); });
  panel = h('section', { class: 'card', 'aria-label': '동별 번호 자동 배정' },
    h('h2', {}, '동별 번호 자동 배정'),
    h('p', { class: 'muted' }, '확정 수거 1개당 해당 행정동 재고의 번호 1개를 배정합니다. 수거 결과 반영과 입고·배부 시 자동으로 맞춥니다.'),
    h('div', { class: 'row' }, h('label', { class: 'f' }, '자동 배정 행정동', scope), query, canWork() ? retry : null),
    message, summary, issues, h('h3', {}, '동별 번호 재고'), inventory);
  panel.refresh = load;
  setTimeout(() => { if (current()) load(); }, 0);
  return panel;
}
export { labelStock };
