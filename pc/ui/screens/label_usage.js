// 수거 개수 기준 사용 장수. 번호 원장과 독립된 읽기 전용 누적 집계.
import { api, boot, btn, grid, h, kst, searchbox } from '../core.js';

const number = (value) => Number(value || 0).toLocaleString('ko-KR');

function collectionLabelUsage(parentCurrent) {
  if (collectionLabelUsage.owner !== boot.me.username) {
    collectionLabelUsage.owner = boot.me.username;
    collectionLabelUsage.state = { q: '', dong: '', page: 1 };
  }
  const s = collectionLabelUsage.state;
  let panel, sequence = 0, loadingKey = '';
  const current = () => panel?.isConnected && parentCurrent();
  const q = h('input', { type: 'search', maxlength: 60, value: s.q, 'aria-label': '집계 접수번호', placeholder: '접수번호 일부' });
  const dongs = [...new Set((boot.dongs || []).map((d) => d.name))];
  if (s.dong && !dongs.includes(s.dong)) dongs.push(s.dong);
  const dong = h('select', { 'aria-label': '집계 행정동' }, h('option', { value: '', selected: !s.dong }, '전체 행정동'),
    ...dongs.map((name) => h('option', { value: name, selected: s.dong === name }, name)));
  const summary = h('div', { class: 'kpis compact' });
  const rows = h('div', { 'aria-live': 'polite' });
  const pager = h('div', { class: 'pager' });
  const message = h('p', { class: 'msg', role: 'status' });
  const source = h('p', { class: 'muted small' }, '전체 기간 누적 · 마지막으로 확정된 수거 결과 기준');
  const remember = () => Object.assign(s, { q: q.value, dong: dong.value });
  const changed = () => {
    remember(); sequence++; loadingKey = ''; s.page = 1; query.disabled = false;
    summary.replaceChildren(); pager.replaceChildren(); rows.removeAttribute('aria-busy');
    rows.replaceChildren(h('p', { class: 'empty' }, '조회조건을 바꿨습니다. [집계 조회]를 눌러 주세요.'));
    message.textContent = '';
  };
  const load = async () => {
    if (!current()) return;
    remember();
    const params = { q: s.q, dong: s.dong, page: s.page }, signature = JSON.stringify(params);
    if (loadingKey === signature) return;
    const my = ++sequence;
    loadingKey = signature; query.disabled = true;
    summary.replaceChildren(); pager.replaceChildren(); rows.setAttribute('aria-busy', 'true');
    rows.replaceChildren(h('p', { class: 'empty' }, '사용 장수를 집계하는 중…'));
    message.textContent = '';
    try {
      const d = await api('label-usage?' + new URLSearchParams(params), { timeoutMs: 15000 });
      if (!current() || my !== sequence) return;
      s.page = d.page;
      summary.replaceChildren(
        h('div', { class: 'kpi ok' }, h('span', { class: 'k' }, '사용 장수'), h('b', { class: 'v', 'aria-label': '자동 집계 사용 장수' }, `${number(d.totals.used)}장`), h('span', { class: 's' }, '실제 수거 1개당 1장')),
        h('div', { class: 'kpi' }, h('span', { class: 'k' }, '수거 접수'), h('b', { class: 'v', 'aria-label': '자동 집계 수거 접수' }, `${number(d.totals.requests)}건`), h('span', { class: 's' }, '조회조건 전체 · 페이지 합계 아님')));
      source.textContent = d.source_note;
      rows.replaceChildren(grid(['접수번호', '행정동', '품목별 사용 장수', '사용 합계', '마지막 확정 일시'], d.rows.map((r) => [
        h('a', { href: `#/request/${encodeURIComponent(r.id)}` }, r.receipt_no), r.dong || '(동 미지정)',
        h('div', {}, ...r.lines.map((line) => h('p', { class: 'small' }, `${line.name || '(품목)'} ${line.spec || ''} · ${number(line.used)}장`))),
        `${number(r.used)}장`, kst(r.at)]), { num: [3], empty: '조건에 맞는 확정 수거 사용분이 없습니다.' }));
      pager.replaceChildren(btn('집계 이전', () => { s.page = d.page - 1; load(); }, 'small', { disabled: d.page <= 1 }),
        h('span', { class: 'pg' }, `${d.page} / ${d.pages}쪽 · ${number(d.total)}건 · 50건씩`),
        btn('집계 다음', () => { s.page = d.page + 1; load(); }, 'small', { disabled: d.page >= d.pages }));
    } catch (error) {
      if (!current() || my !== sequence) return;
      rows.replaceChildren(h('div', { class: 'notice bad', role: 'alert' }, error.message, ' ', btn('집계 다시 시도', load, 'small')));
      message.textContent = '집계하지 못했습니다. 조회조건은 유지됩니다.';
    } finally {
      if (current() && my === sequence) { loadingKey = ''; query.disabled = false; rows.removeAttribute('aria-busy'); }
    }
  };
  const query = btn('집계 조회', () => { s.page = 1; load(); }, 'primary');
  const reset = btn('집계 초기화', () => { q.value = ''; dong.value = ''; changed(); load(); }, 'small');
  q.addEventListener('input', changed);
  q.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); query.click(); } });
  dong.addEventListener('change', () => { changed(); load(); });
  const filters = searchbox([['접수번호', q], ['행정동', dong]], query, reset);
  filters.setAttribute('aria-label', '사용 장수 조회조건');
  panel = h('section', { class: 'card', 'aria-label': '사용 장수 자동 집계' },
    h('h2', {}, '사용 장수 자동 집계'),
    h('p', { class: 'muted' }, '휴대폰 수거 결과를 PC에 반영하면 사용 장수를 자동 계산합니다. 같은 결과의 재전송·재방문은 누적 수량으로 한 번만 셉니다.'),
    filters, message, summary, source, rows, pager);
  setTimeout(() => { if (current()) load(); }, 0);
  return panel;
}

export { collectionLabelUsage };
