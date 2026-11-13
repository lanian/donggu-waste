import { feeCalculator } from '/web/fee-calculator.js';
import { FEES, boot, btn, go, h, pagehead, sessionOwner } from '../core.js';

const drafts = new WeakMap();
export function screenCalculator() {
  const owner = sessionOwner();
  if (!drafts.has(owner)) drafts.set(owner, {});
  return h('div', {}, pagehead('빠른 수수료 계산', btn('업무 화면으로', () => go(boot.me.role === 'DONG' ? '#/dongs' : '#/home'), 'small')),
    h('p', { class: 'muted' }, '품목·규격을 골라 담고 수량을 조절하세요. 감면 전 수수료를 계산합니다.'),
    feeCalculator({ table: FEES(), state: drafts.get(owner), source: 'PC 적용 요금표 기준', isActive: () => sessionOwner() === owner }));
}
