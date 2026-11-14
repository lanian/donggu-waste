// 휴대폰 화면: 현장 품목 추가
import { deriveStop } from '../fees.js';
import { isUnlocked } from '../store.js';
import { ERR, addEvent, ask, btn, commit, errMsg, feeTable, findStop, go, h, sheet, st, toast, ui } from '../core.js';
import { pickScreen } from '../widgets.js';

const operations = new WeakMap(), draftOwners = new WeakMap();
let navigationEpoch = 0;
addEventListener('hashchange', () => { navigationEpoch += 1; });
const operationsFor = (state) => {
  if (!operations.has(state)) operations.set(state, new Map());
  return operations.get(state);
};

function screenAdd(id) {
  const stop = findStop(id);
  if (!stop) { go('#/home'); return h('main'); }
  const source = st, writes = operationsFor(source);
  const epoch = navigationEpoch, historySize = history.length;
  const previous = writes.get(id);
  const a = previous?.form || (ui.add && ui.add.stop === id && ui.add.items && (!draftOwners.has(ui.add) || draftOwners.get(ui.add) === source)
    ? ui.add : { stop: id, q: '', cat: '', items: [] });
  ui.add = a; draftOwners.set(a, source);
  const table = feeTable();
  if (!table) return h('main', {}, h('header', { class: 'bar' }, btn('← 취소', () => go(`#/stop/${id}`), 'ghost')), h('p', { class: 'msg' }, ERR.FEE_VERSION_MISSING));
  let view, frozen = null;
  // 같은 hash로 즉시 돌아와 옛 DOM이 잠시 남아 있어도 이전 화면의 완료 응답은 적용하지 않는다.
  const current = () => st === source && isUnlocked() && view?.isConnected && ui.add === a && navigationEpoch === epoch
    && history.length === historySize && location.hash === `#/add/${id}`;
  const leave = () => { if (ui.add === a) ui.add = null; go(`#/stop/${id}`); };
  const message = h('p', { class: 'notice small', role: 'status', hidden: true, 'aria-label': '현장 품목 저장 상태' });
  const sync = () => {
    const op = writes.get(id), active = !!op;
    if (!view) return;
    const done = view.querySelector('.pick-foot button'), back = view.querySelector('.pick-top button');
    if (active && !frozen) frozen = [...view.querySelectorAll('button,input,select,textarea')].map((el) => [el, el.disabled]);
    if (active) frozen.forEach(([el]) => { el.disabled = true; });
    else if (frozen) { frozen.forEach(([el, disabled]) => { el.disabled = disabled; }); frozen = null; }
    if (!op) {
      message.hidden = true;
      done.textContent = a.items.length ? `추가 저장 (${a.items.reduce((n, it) => n + it.qty, 0)})` : '닫기';
      done.removeAttribute('aria-busy'); back.textContent = '← 취소';
      return;
    }
    message.hidden = false;
    message.textContent = op.status === 'choosing' ? '같은 품목을 더할 방법을 확인하고 있습니다.'
      : op.status === 'saving' ? '현장 품목을 저장하고 있습니다. 같은 작업의 저장 결과를 기다려 주세요.'
      : op.status === 'saved' ? '현장 품목 저장이 끝났습니다. 저장된 품목을 확인하세요.'
      : `현장 품목을 저장하지 못했습니다. 담은 내용은 남아 있으며 같은 기록을 다시 저장합니다.${op.error ? ' ' + op.error : ''}`;
    done.textContent = op.status === 'saved' ? '저장된 품목 보기' : op.status === 'failed' ? '저장 다시 시도' : '저장 중…';
    done.disabled = !['saved', 'failed'].includes(op.status);
    done.setAttribute('aria-busy', String(op.status === 'saving' || op.status === 'choosing'));
    back.textContent = op.prepared ? '← 접수 상세로' : '← 취소';
    back.disabled = op.status === 'choosing';
  };
  const finish = (op) => {
    if (!current() || writes.get(id) !== op || op.status !== 'saved') return;
    writes.delete(id);
    toast(op.message); leave();
  };
  const persist = async (op) => {
    if (!current() || op.status === 'saving' || op.status === 'choosing') return;
    op.status = 'saving'; op.error = ''; sync();
    const pending = (async () => {
      try {
        let saved;
        if (op.prepared === op.plans.length) saved = await commit();
        else {
          const pendingRows = [];
          // 첫 쓰기를 기다리기 전에 모든 행을 한 번 준비해 잠금 중 새 행·ID를 만들지 않는다.
          for (let i = op.prepared; i < op.plans.length; i++) {
            if (st !== source || !isUnlocked()) throw new Error('잠금 상태가 바뀌어 품목 준비를 중단했습니다.');
            const plan = op.plans[i], before = source.events.length;
            const row = addEvent(stop, plan.kind, plan.body);
            pendingRows.push(row);
            if (source.events.length !== before + 1) { await Promise.all(pendingRows); throw new Error('품목 기록을 준비하지 못했습니다.'); }
            op.prepared += 1;
          }
          saved = (await Promise.all(pendingRows)).at(-1);
        }
        op.status = saved ? 'saved' : 'failed';
      } catch (error) { op.status = 'failed'; op.error = errMsg(error); }
      if (st !== source || !isUnlocked() || writes.get(id) !== op) return;
      op.notify?.();
      if (current() && op.status === 'saved') finish(op);
    })();
    await pending;
  };
  // 저장 실패 뒤에는 준비한 이벤트/행을 유지한다. 다시 시도는 새 품목 추가가 아닌 같은 상태의 저장이다.
  const save = async () => {
    if (!current()) return;
    let op = writes.get(id);
    if (op) {
      if (op.status === 'saved') finish(op);
      else if (op.status === 'failed') await persist(op);
      return;
    }
    if (!a.items.length) return leave();
    if (a.items.some((it) => !Number.isInteger(it.qty) || it.qty < 1 || it.qty > 9999)) { message.hidden = false; message.textContent = '품목 수량은 1~9999 사이의 정수로 입력해 주세요.'; return; }
    op = { form: a, status: 'choosing', prepared: 0, plans: [], notify: () => { if (current()) sync(); } };
    writes.set(id, op); sync();
    try {
      const d = deriveStop(stop, st.events);
      const prevOf = (it) => (it.fee_rate_id ? d.lines.find((l) => l.kind === 'FIELD' && l.fee_rate_id === it.fee_rate_id) : null);
      const dups = a.items.filter(prevOf);
      let merge = false;
      if (dups.length) {
        const controller = new AbortController(), leaveChoice = () => controller.abort();
        addEventListener('hashchange', leaveChoice, { once: true });
        let r;
        try {
          r = await sheet({ title: '같은 품목이 있습니다', text: '이미 현장에서 추가한 품목입니다: ' + dups.map((it) => { const p0 = prevOf(it); return `${it.name} ${it.spec} ${p0.qty_confirmed} → ${p0.qty_confirmed + it.qty}${it.unit_label}`; }).join(', '),
            choices: [{ label: '기존 행에 더하기', value: 'merge', cls: 'primary' }, { label: '따로 한 줄 더 추가', value: 'sep' }], cancel: '돌아가기', signal: controller.signal });
        } finally { removeEventListener('hashchange', leaveChoice); }
        if (!r || !current()) { writes.delete(id); op.notify?.(); return; }
        merge = r === 'merge';
      }
      if (!current()) { writes.delete(id); op.notify?.(); return; }
      let n = 0;
      for (const it of a.items) {
        const prev = merge ? prevOf(it) : null;
        const existing = prev && op.plans.find((plan) => plan.kind === 'ITEM_RESULT' && plan.body.line_id === prev.line_id);
        if (existing) existing.body.qty_confirmed += it.qty;
        else if (prev) op.plans.push({ kind: 'ITEM_RESULT', body: { line_id: prev.line_id, line_kind: 'FIELD', qty_confirmed: prev.qty_confirmed + it.qty, qty_collected: prev.qty_collected, reason: prev.reason || '' } });
        else op.plans.push({ kind: 'FIELD_ITEM_ADD', body: { line_id: it.line_id, fee_rate_id: it.fee_rate_id, fee_version_id: it.fee_rate_id ? table.version_id : null, name: it.name, spec: it.spec || '', unit: it.unit, unit_label: it.unit_label,
          unit_price: it.unit_price, price_status: it.price_status, qty: it.qty } });
        n += it.qty;
      }
      if (op.plans.some((plan) => plan.kind === 'ITEM_RESULT' && (!Number.isInteger(plan.body.qty_confirmed) || plan.body.qty_confirmed > 9999))) {
        throw new Error('같은 행의 수량은 9999를 넘을 수 없습니다. 따로 한 줄 더 추가해 주세요.');
      }
      op.message = merge && dups.length ? `현장 품목 ${n}개를 추가했습니다 (같은 품목은 기존 행에 더함, 변경 기록 남음)` : `현장 품목 ${n}개를 추가했습니다`;
      op.status = 'ready'; await persist(op);
    } catch (error) {
      if (writes.get(id) === op) {
        writes.delete(id); op.notify?.();
        if (current()) { message.hidden = false; message.textContent = errMsg(error); }
      }
    }
  };
  view = pickScreen({ items: a.items, state: a, table, title: '현장 품목 추가', onDone: save,
    doneLabel: (items) => (items.length ? `추가 저장 (${items.reduce((x, it) => x + it.qty, 0)})` : '닫기'),
    back: { label: '← 취소', fn: async () => {
      if (!current()) return;
      const pending = writes.get(id);
      if (pending) { if (pending.status !== 'choosing') go(`#/stop/${id}`); return; }
      if (a.items.length && (!(await ask(`담은 품목 ${a.items.length}종을 저장하지 않고 나갈까요?`, '저장 안 하고 나가기', { danger: true })) || !current())) return;
      leave();
    } },
    intro: h('p', { class: 'small pick-intro' }, '현장에서 더 나온 품목을 담고 [추가 저장]을 누르세요. 접수 때 품목의 수량은 지점 화면에서 고칩니다.') });
  view.querySelector('.pick-intro')?.after(message);
  if (previous) previous.notify = () => { if (current()) sync(); };
  sync();
  return view;
}

// ------------------------------------------------------------ 결과 보내기

export {
  screenAdd,
};
