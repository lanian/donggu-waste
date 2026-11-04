// 휴대폰 화면: 현장 품목 추가
import { deriveStop } from '../fees.js';
import { ERR, addEvent, ask, btn, feeTable, findStop, go, h, sheet, st, toast, ui } from '../core.js';
import { pickScreen } from '../widgets.js';

function screenAdd(id) {
  const stop = findStop(id);
  if (!stop) { go('#/home'); return h('main'); }
  const a = ui.add && ui.add.stop === id && ui.add.items ? ui.add : (ui.add = { stop: id, q: '', cat: '', items: [] });
  const table = feeTable();
  if (!table) return h('main', {}, h('header', { class: 'bar' }, btn('← 취소', () => go(`#/stop/${id}`), 'ghost')), h('p', { class: 'msg' }, ERR.FEE_VERSION_MISSING));
  const leave = () => { ui.add = null; go(`#/stop/${id}`); };
  // 담은 품목을 한 번에 저장: 같은 품목을 이미 현장 추가했다면 기존 행에 더할지 따로 둘지 한 번 묻는다(앞선 기록은 지워지지 않음)
  const save = async () => {
    if (!a.items.length) return leave();
    const d = deriveStop(stop, st.events);
    const prevOf = (it) => (it.fee_rate_id ? d.lines.find((l) => l.kind === 'FIELD' && l.fee_rate_id === it.fee_rate_id) : null);
    const dups = a.items.filter(prevOf);
    let merge = false;
    if (dups.length) {
      const r = await sheet({ title: '같은 품목이 있습니다', text: '이미 현장에서 추가한 품목입니다: ' + dups.map((it) => { const p0 = prevOf(it); return `${it.name} ${it.spec} ${p0.qty_confirmed} → ${p0.qty_confirmed + it.qty}${it.unit_label}`; }).join(', '),
        choices: [{ label: '기존 행에 더하기', value: 'merge', cls: 'primary' }, { label: '따로 한 줄 더 추가', value: 'sep' }], cancel: '돌아가기' });
      if (!r) return;
      merge = r === 'merge';
    }
    let n = 0;
    for (const it of a.items) {
      const prev = merge ? prevOf(it) : null;
      if (prev) await addEvent(stop, 'ITEM_RESULT', { line_id: prev.line_id, line_kind: 'FIELD', qty_confirmed: prev.qty_confirmed + it.qty, qty_collected: prev.qty_collected, reason: prev.reason || '' });
      else await addEvent(stop, 'FIELD_ITEM_ADD', { line_id: it.line_id, fee_rate_id: it.fee_rate_id, fee_version_id: it.fee_rate_id ? table.version_id : null, name: it.name, spec: it.spec || '', unit: it.unit, unit_label: it.unit_label,
        unit_price: it.unit_price, price_status: it.price_status, qty: it.qty });
      n += it.qty;
    }
    toast(merge && dups.length ? `현장 품목 ${n}개를 추가했습니다 (같은 품목은 기존 행에 더함, 변경 기록 남음)` : `현장 품목 ${n}개를 추가했습니다`);
    leave();
  };
  return pickScreen({ items: a.items, state: a, table, title: '현장 품목 추가', onDone: save,
    doneLabel: (items) => (items.length ? `추가 저장 (${items.reduce((x, it) => x + it.qty, 0)})` : '닫기'),
    back: { label: '← 취소', fn: async () => { if (a.items.length && !(await ask(`담은 품목 ${a.items.length}종을 저장하지 않고 나갈까요?`, '저장 안 하고 나가기', { danger: true }))) return; leave(); } },
    intro: h('p', { class: 'small pick-intro' }, '현장에서 더 나온 품목을 담고 [추가 저장]을 누르세요. 접수 때 품목의 수량은 지점 화면에서 고칩니다.') });
}

// ------------------------------------------------------------ 결과 보내기

export {
  screenAdd,
};
