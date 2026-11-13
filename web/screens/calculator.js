import { BUILTIN_FEES } from '../fees-data.js';
import { feeCalculator } from '../fee-calculator.js';
import { btn, feeTable, go, h, isSolo, st, themeToggle, ui } from '../core.js';

export function screenCalculator() {
  const current = feeTable(), table = current || BUILTIN_FEES;
  const header = h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '빠른 수수료 계산'), themeToggle());
  if (!isSolo() && st.daylist?.fee_table && !current) {
    return h('main', {}, header, h('section', { class: 'card notice', role: 'alert' }, h('h2', {}, '요금표 업데이트 필요'),
      h('p', {}, '받은 PC 요금표를 이 앱에서 읽을 수 없습니다. 앱을 업데이트하고 PC에서 새 목록을 받은 뒤 다시 계산하세요.'),
      h('p', { class: 'small' }, `받은 요금표 ${st.daylist.fee_table.version_id || '미확인'}`)));
  }
  const source = isSolo() ? '이 휴대폰의 요금표 기준' : current
    ? `받은 PC 요금표 기준 · 목록 ${st.daylist?.work_date || ''}` : '내장 요금표 기준 · 받은 PC 요금표 없음';
  ui.feeCalculator ||= {};
  const owner = st;
  return h('main', {}, header,
    h('p', { class: 'small' }, '품목·규격을 골라 담고 수량을 조절하세요. 감면 전 수수료를 계산합니다.'),
    feeCalculator({ table, state: ui.feeCalculator, source, isActive: () => st === owner }));
}
