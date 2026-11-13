// 자동 배정과 실물 재고의 동별 현황. 읽기만으로 번호를 소모하지 않는다.
import { api, boot, btn, canWork, grid, h, postOnce, toast } from '../core.js';

const number = (v) => Number(v || 0).toLocaleString('ko-KR');
function labelStock(parentCurrent) {
  let panel, sequence = 0, busy = false;
  const current = () => panel?.isConnected && parentCurrent();
  const summary = h('div', { 'aria-live': 'polite' }), message = h('p', { class: 'msg', role: 'status' });
  const load = async () => {
    if (!current()) return;
    const my = ++sequence; query.disabled = true;
    try {
      const d = await api('label-auto', { timeoutMs: 15000 });
      if (!current() || my !== sequence) return;
      summary.replaceChildren(
        h('div', { class: 'kpis compact' }, ...[['필요 장수', d.required], ['배정 장수', d.assigned], ['미배정 장수', d.pending]].map(([label, value]) =>
          h('div', { class: 'kpi' }, h('span', { class: 'k' }, label), h('b', { class: 'v', 'aria-label': label }, number(value) + '장')))),
        d.manual_excess || d.conflicts ? h('p', { class: 'notice bad' }, `수동 사용 초과 ${number(d.manual_excess)}장 · 동·일자 확인 필요 ${number(d.conflicts)}장. 번호 상세에서 연결과 재고 동을 확인해 주세요.`) : null,
        grid(['행정동', '배정 가능 재고', '사용', '무효'], d.dongs.map((dong) => [dong.name, number(dong.available) + '장', number(dong.used) + '장', number(dong.voided) + '장']), { num: [1, 2, 3] }));
      message.textContent = d.pending ? '미배정 수거분이 있습니다. 해당 동의 재고와 실제 수령일을 등록하면 자동 배정합니다.' : '';
    } catch (error) { if (current() && my === sequence) message.textContent = error.message; }
    finally { if (current() && my === sequence) query.disabled = false; }
  };
  const query = btn('재고 현황 새로고침', load, 'small');
  const retry = btn('미배정 다시 배정', async () => {
    if (!current() || busy) return;
    busy = true; retry.disabled = true;
    try {
      const d = await postOnce('label-auto:sync', 'label-auto/sync', {});
      if (current()) { toast(`${number(d.new_assigned)}장 배정 · 미배정 ${number(d.pending)}장`); load(); }
    } catch (error) { if (current()) message.textContent = error.message; }
    finally { busy = false; if (current()) retry.disabled = false; }
  }, 'small');
  panel = h('section', { class: 'card', 'aria-label': '동별 번호 자동 배정' }, h('div', { class: 'panel-h' }, h('h2', {}, '동별 번호 자동 배정'), h('div', { class: 'row' }, query, canWork() ? retry : null)),
    h('p', { class: 'muted' }, '확정 수거 1개당 해당 행정동 재고의 번호 1개를 배정합니다. 수거 결과 반영과 입고·배부 시 자동으로 맞춥니다.'), message, summary);
  panel.refresh = load;
  setTimeout(() => { if (current()) load(); }, 0);
  return panel;
}
export { labelStock };
