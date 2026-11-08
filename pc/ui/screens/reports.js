// PC 화면: 일일 작업일지·월보 (저장된 업무 기록을 개인정보 없이 집계)
import { won } from '/web/fees.js';
import { api, apiRaw, boot, btn, grid, h, pagehead, post, routeEpoch, searchbox, toast } from '../core.js';

const BASIS = [['planned', '수거 예정일'], ['received', '접수일'], ['completed', '완료일']];
const number = (value) => Number(value || 0).toLocaleString('ko-KR');

async function screenReports() {
  if (!['ADMIN', 'INTAKE', 'LEAD'].includes(boot.me.role)) return h('div', {}, pagehead('업무 보고서'), h('p', { class: 'notice' }, '보고서를 조회할 권한이 없습니다.'));
  if (screenReports.owner !== boot.me.username) {
    screenReports.owner = boot.me.username;
    screenReports.s = { kind: 'daily', date: boot.today, month: boot.today.slice(0, 7), basis: 'planned' };
  }
  const s = screenReports.s;
  const myRoute = routeEpoch, myHash = location.hash;
  let view, sequence = 0, result = null, resultKey = '', loadingKey = '', outputBusy = false;
  const current = () => view?.isConnected && routeEpoch === myRoute && location.hash === myHash;
  const kind = h('select', { 'aria-label': '보고서 종류', value: s.kind }, ...[['daily', '일일 작업일지'], ['monthly', '월보']].map(([v, t]) => h('option', { value: v, selected: v === s.kind }, t)));
  const date = h('input', { type: 'date', value: s.date, 'aria-label': '작업일' });
  const month = h('input', { type: 'month', value: s.month, 'aria-label': '보고 월' });
  const basis = h('select', { 'aria-label': '집계 기준' }, ...BASIS.map(([v, t]) => h('option', { value: v, selected: v === s.basis }, t)));
  const period = h('div', { class: 'row' });
  const message = h('p', { class: 'msg', role: 'status' });
  const content = h('div', { 'aria-live': 'polite' });
  const criteria = () => ({ kind: kind.value, ...(kind.value === 'daily' ? { date: date.value } : { month: month.value }), basis: basis.value });
  const key = () => JSON.stringify(criteria());
  const syncActions = () => {
    const ready = !!result && resultKey === key() && !loadingKey;
    [printButton, csvButton].forEach((button) => {
      button.disabled = !ready || outputBusy;
      if (outputBusy) button.setAttribute('aria-busy', 'true'); else button.removeAttribute('aria-busy');
    });
    query.disabled = loadingKey === key();
  };
  const changed = () => {
    Object.assign(s, { kind: kind.value, date: date.value, month: month.value, basis: basis.value });
    sequence++; loadingKey = '';
    period.replaceChildren(kind.value === 'daily' ? date : month);
    content.removeAttribute('aria-busy');
    message.textContent = result && resultKey !== key() ? '조회조건을 바꿨습니다. [조회]를 누르면 미리보기와 출력에 반영됩니다.' : '';
    syncActions();
  };
  const show = (d, c) => {
    const t = d.totals, st = t.by_status || {}, unload = d.unload_summary || {};
    const revenue = d.revenue || {}, revenueTotals = revenue.totals || {};
    const revenueRows = [...(revenue.methods || []), { ...revenueTotals, method: '합계' }].map((x) => [x.method, x.pay_n || 0, won(x.pay || 0), x.refund_n || 0, won(x.refund || 0), won(x.net || 0)]);
    const kpi = (label, value, detail, cls = '') => h('div', { class: 'kpi ' + cls }, h('span', { class: 'k' }, label), h('b', { class: 'v' }, value), h('span', { class: 's' }, detail));
    const card = (title, headers, rows, numeric = []) => h('section', { class: 'card', style: 'min-width:0' }, h('h2', {}, title), grid(headers, rows, { num: numeric, empty: '해당 기간에 기록이 없습니다.' }));
    content.replaceChildren(
      h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, `${c.kind === 'daily' ? '일일 작업일지' : '월보'} · ${c.date || c.month}`), h('span', { class: 'muted small' }, `${d.basis_label || BASIS.find(([v]) => v === c.basis)?.[1]} 기준 · 조회 ${d.generated_at || ''}`)),
        h('p', { class: 'muted small' }, d.source_note || '저장된 업무 기록의 현재 상태를 집계합니다.')),
      h('div', { class: 'kpis', 'data-report-summary': '1' },
        kpi('집계 접수', `${number(t.requests)}건`, `기간 중 접수 ${number(t.received)}건`),
        kpi('수거 완료', `${number(st.DONE)}건`, `취소 ${number(st.CANCELLED)}건`, 'ok'),
        kpi('재방문·보류', `${number((st.PARTIAL || 0) + (st.NOT_COLLECTED || 0) + (st.ON_HOLD || 0))}건`, `일부 ${number(st.PARTIAL)} · 미수거 ${number(st.NOT_COLLECTED)} · 보류 ${number(st.ON_HOLD)}`),
        kpi('부과 금액', won(t.levied || 0), `계산 ${won(t.calculated || 0)} · 감면 ${won(t.reduction || 0)}`),
        kpi('하차 기록', `${number(unload.count)}건`, `체적 기록 ${number(unload.volume_records)}건 · 중량 기록 ${number(unload.weight_records)}건`)),
      card('일자별 업무', ['날짜', '접수', '선택 기준 건수', '완료', '일부', '미수거', '취소', '부과 금액', '접수별 누적 납부 기록'], (d.days || []).map((x) => [x.date, x.received, x.planned, x.done, x.partial, x.not_collected, x.cancelled, won(x.levied || 0), won(x.paid || 0)]), [1, 2, 3, 4, 5, 6, 7, 8]),
      h('div', { class: 'grid2' },
        card('행정동별', ['행정동', '건수', '완료', '재방문·보류', '취소', '부과 금액'], (d.dongs || []).map((x) => [x.dong || '(동 미지정)', x.planned, x.done, x.revisit, x.cancelled, won(x.levied || 0)]), [1, 2, 3, 4, 5]),
        card('수거팀별', ['수거팀', '배정', '완료', '재방문·보류'], (d.crews || []).map((x) => [x.crew, x.assigned, x.done, x.partial_or_not]), [1, 2, 3])),
      card('품목별 수거', ['품목', '규격', '단위', '확인 수량', '누적 수거 수량'], (d.items || []).map((x) => [x.name, x.spec || '', x.unit_label, x.requested, x.collected]), [3, 4]),
      card('접수 경로별', ['접수 경로', '건수', '완료', '취소'], (d.channels || []).map((x) => [x.channel, x.planned, x.done, x.cancelled]), [1, 2, 3]),
      card('세입 · 납부일 기준', ['납부 방법', '납부 건수', '납부액', '환불 건수', '환불액', '순수입'], revenueRows, [1, 2, 3, 4, 5]),
      h('p', { class: 'notice info' }, `하차 ${number(t.unload_m3)}m³ · ${number(t.unload_kg)}kg. 하차 기록이 없는 경우 수거량이 0이라는 뜻은 아닙니다. 미리보기·인쇄·CSV에는 이름·연락처·주소를 넣지 않습니다.`));
  };
  const load = async () => {
    if (!current()) return;
    const c = criteria(), signature = JSON.stringify(c);
    if (loadingKey === signature) return;
    if (!(c.kind === 'daily' ? /^\d{4}-\d{2}-\d{2}$/.test(c.date) : /^\d{4}-\d{2}$/.test(c.month))) {
      message.textContent = c.kind === 'daily' ? '작업일을 골라 주세요.' : '보고 월을 골라 주세요.'; return;
    }
    const my = ++sequence;
    loadingKey = signature; message.textContent = '불러오는 중…'; content.setAttribute('aria-busy', 'true'); syncActions();
    try {
      const { kind: reportKind, ...params } = c;
      const d = await api(`reports/${reportKind}?${new URLSearchParams(params)}`, { timeoutMs: 15000 });
      if (!current() || my !== sequence) return;
      result = d; resultKey = signature; message.textContent = ''; show(d, c);
    } catch (e) {
      if (!current() || my !== sequence) return;
      result = null; resultKey = ''; content.replaceChildren(h('div', { class: 'notice bad', role: 'alert' }, e.message, ' ', btn('다시 시도', load, 'small')));
      message.textContent = '보고서를 불러오지 못했습니다. 조회조건은 유지됩니다.';
    } finally {
      if (current() && my === sequence) { loadingKey = ''; content.removeAttribute('aria-busy'); syncActions(); }
    }
  };
  const output = async (mode) => {
    if (!current() || outputBusy || !result || resultKey !== key() || loadingKey) return;
    const c = criteria(), signature = key(), startedSequence = sequence;
    const outputCurrent = () => current() && resultKey === signature && key() === signature && sequence === startedSequence;
    outputBusy = true; syncActions();
    try {
      if (mode === 'print') {
        const r = await post('reports/print', c);
        if (outputCurrent()) { window.open('/print/doc/' + r.token, 'dg-print'); toast('보고서 인쇄 화면을 열었습니다'); }
      } else {
        const { kind: reportKind, ...params } = c;
        const response = await apiRaw(`reports/${reportKind}.csv?${new URLSearchParams(params)}`);
        if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || '내보내기 실패');
        const blob = await response.blob();
        // 파일 저장은 응답·본문을 모두 받은 뒤에도 같은 화면과 조회조건일 때만 실행한다.
        if (!outputCurrent()) return;
        const url = URL.createObjectURL(blob);
        const link = h('a', { href: url, download: `동구-${reportKind === 'daily' ? '일일작업일지' : '월보'}-${c.date || c.month}.csv` });
        document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
        toast('보고서 CSV를 내려받았습니다');
      }
    } catch (e) { if (current()) toast(e.message, true); }
    finally { outputBusy = false; if (current()) syncActions(); }
  };
  const query = btn('조회', load, 'primary');
  const printButton = btn('인쇄 화면 열기', () => output('print'), 'small', { disabled: true });
  const csvButton = btn('CSV 내려받기', () => output('csv'), 'small', { disabled: true });
  [kind, date, month, basis].forEach((input) => input.addEventListener('change', changed));
  period.append(kind.value === 'daily' ? date : month);
  view = h('div', {}, pagehead('업무 보고서', printButton, csvButton),
    h('p', { class: 'muted' }, '업무 기록을 조회해 일일 작업일지와 월보를 만듭니다. 출력 서식은 추후 조정할 수 있습니다.'),
    searchbox([['보고서', kind], ['기간', period], ['집계 기준', basis]], query), message, content);
  setTimeout(() => { if (current()) load(); }, 0);
  return view;
}

export { screenReports };
