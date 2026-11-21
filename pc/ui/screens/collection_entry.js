// 접수 원본/수납과 별도로 실제 수거분을 입력한다. 화면 이탈·계정 전환 뒤 응답은 무시한다.
import { api, boot, btn, h, sessionOwner, toast, uuid, wrap } from '../core.js';
import { itemPicker } from '../widgets.js';
import { won } from '/web/fees.js';

const itemKey = (items) => JSON.stringify((items || []).map((i) => [i.line_id || '', i.fee_rate_id || '', i.qty]));
export function unchangedCollectionItems(items, original) {
  if (itemKey(items) !== itemKey(original)) throw new Error('품목 수정 내용을 먼저 저장한 뒤 수거 처리하세요.');
}

export function collectionButton(request, { path = 'requests', current = () => true, before, after } = {}) {
  const owner = sessionOwner();
  const alive = () => owner === sessionOwner() && !!boot?.me && button.isConnected && current();
  const panel = h('div', { id: `collection-${uuid()}`, hidden: true });
  const feedback = h('div', { class: 'collection-feedback', 'aria-live': 'polite' });
  let editor = null;
  const refresh = async (result) => {
    try {
      await after?.(result, owner);
      if (alive()) { feedback.replaceChildren(); button.disabled = result.status === 'DONE' || request.status === 'DONE'; }
    } catch (error) {
      if (!alive()) return;
      button.disabled = true;
      const retry = btn('최신 내용 다시 불러오기', async () => { retry.disabled = true; await refresh(result); }, 'small');
      feedback.replaceChildren(h('p', { class: 'notice bad', role: 'alert' }, `수거 결과는 저장됐지만 최신 접수를 불러오지 못했습니다. ${error.message}`), retry);
    }
  };
  const collapse = () => { panel.hidden = true; button.setAttribute('aria-expanded', 'false'); button.focus(); };
  const button = btn('수거 처리', async () => {
    if (!alive()) return;
    if (!panel.hidden) { collapse(); return; }
    button.disabled = true;
    try {
      if (!editor) {
        before?.();
        const fresh = await api(`${path}/${request.id}`, { _prefsOwner: owner });
        if (!alive()) return;
        before?.();
        if (fresh.req_ver !== request.req_ver) throw new Error('접수가 바뀌었습니다. 최신 접수를 다시 열어 확인하세요.');
        editor = collectionForm(fresh, path, alive, owner, before, collapse, async (result) => {
          panel.hidden = true; panel.replaceChildren(); editor = null; button.setAttribute('aria-expanded', 'false');
          button.disabled = true;
          toast(`수거 결과를 저장했습니다.${result.labels_pending ? ` 배출표 번호 미배정 ${result.labels_pending}장` : ''}`);
          await refresh(result);
        });
        panel.replaceChildren(editor);
      }
      panel.hidden = false; button.setAttribute('aria-expanded', 'true');
      editor.querySelector('h3').focus();
    } catch (error) { if (alive()) toast(error.message, true); }
    finally { if (alive()) button.disabled = request.status === 'DONE'; }
  }, 'primary small', { 'aria-expanded': 'false', 'aria-controls': panel.id,
    disabled: request.status === 'DONE' || !request.collection?.lines?.length });
  return h('div', { class: 'collection-workspace' }, button, panel, feedback);
}

function collectionForm(request, path, parentAlive, owner, before, collapse, after) {
  const info = request.collection;
  const day = h('input', { type: 'date', value: boot.today, max: boot.today, 'aria-label': '실제 수거일' });
  const reason = h('input', { maxlength: 200, placeholder: '예: 의자 1개 추가 확인, 다른 1개는 미배출' });
  const reasonLabel = h('span');
  const summary = h('p', { class: 'notice info', role: 'status', 'aria-label': '수거 입력 요약' });
  const fees = h('p', { class: 'muted small', hidden: true, 'aria-label': '변경 후 예상 수수료' });
  const tbody = h('tbody');
  const rows = [];
  let submitted = null, id = uuid();
  const editors = h('fieldset', { style: 'margin:0;padding:0;border:0;min-width:0', 'aria-label': '수거 입력' });
  const pickerDetails = h('details', {}, h('summary', {}, '현장 품목 추가'));
  pickerDetails.addEventListener('toggle', () => {
    if (!pickerDetails.open || pickerDetails.querySelector('.waste-picker')) return;
    pickerDetails.append(itemPicker((item) => {
      if (submitted || !parentAlive() || !pickerDetails.isConnected) return;
      const rate = item._row;
      addRow({ name: rate?.name || item.name, spec: rate?.spec || '', unit_label: rate?.unit_label || item.unit_label,
        confirmed: item.qty, previous: 0, received: 0, kind: 'FIELD', unit_price: rate?.status === 'CONFIRMED' ? rate.price : null },
        rate ? { fee_rate_id: rate.id } : { name: item.name, unit: item.unit });
      draw();
    }, { usagePath: path === 'requests' ? 'fee-usage' : null }));
  });

  function addRow(line, fieldItem) {
    const unit = line.unit_label || '(단위 미확인)';
    const num = h('span');
    const confirmed = h('input', { type: 'number', min: fieldItem ? 1 : line.previous, max: 9999, step: 1, inputmode: 'numeric', class: 'w-xs', value: line.confirmed });
    const quantity = h('input', { id: `collection-quantity-${uuid()}`, type: 'number', min: 0, step: 1, inputmode: 'numeric', class: 'w-xs', value: 0 });
    const projectedText = h('span', { id: `collection-projection-${uuid()}` });
    const projected = h('output', { for: quantity.id, class: 'collection-projection', 'aria-live': 'off' }, projectedText);
    quantity.setAttribute('aria-describedby', projectedText.id);
    const left = h('span');
    const set = (n) => { quantity.value = n; draw(); };
    const minus = btn('−', () => set(quantity.valueAsNumber - 1), 'small');
    const plus = btn('+', () => set(quantity.valueAsNumber + 1), 'small');
    const remaining = btn('남은 수량', () => set(confirmed.valueAsNumber - line.previous), 'small');
    const remove = fieldItem && btn('추가 취소', () => {
      const index = rows.indexOf(row);
      rows.splice(index, 1); tr.remove(); rows.forEach(numberRow); draw();
      (rows[index]?.confirmed || pickerDetails.querySelector('summary')).focus();
    }, 'small');
    const source = line.kind === 'FIELD' ? '현장 추가' : `접수 ${line.received ?? line.confirmed}${unit}`;
    const tr = h('tr', {}, h('th', { scope: 'row' }, num, line.name, line.spec ? ' ' + line.spec : '',
      h('small', { class: 'block muted' }, source + (line.unit_price === null ? ' · 금액 확인 대기' : '')),
      h('label', { class: 'row small' }, '현장 확인', confirmed, unit), remove),
    h('td', {}, `${line.previous}${unit}`),
    h('td', {}, h('div', { class: 'row' }, minus, quantity, plus, h('span', {}, unit), remaining), projected),
    h('td', {}, left));
    const row = { line, fieldItem, unit, num, confirmed, quantity, minus, plus, remaining, projected, projectedText, left, remove };
    rows.push(row); tbody.append(tr); numberRow(row, rows.length - 1);
    confirmed.addEventListener('input', draw);
    quantity.addEventListener('input', draw);
  }

  function numberRow(row, i) {
    row.num.textContent = `${i + 1}. `;
    for (const [control, label] of [[row.confirmed, '현장 확인 수량'], [row.quantity, '이번 수거 개수'], [row.minus, '수거 수량 줄이기'],
      [row.plus, '수거 수량 늘리기'], [row.remaining, '남은 수량 채우기'], [row.projected, '입력 후 누적'], [row.remove, '현장 품목 추가 취소']]) {
      if (control) control.setAttribute('aria-label', `${i + 1}번 ${label}`);
    }
  }

  function byUnit(values, valueOf) {
    const totals = new Map();
    for (const value of values) totals.set(value.unit, (totals.get(value.unit) || 0) + valueOf(value));
    return [...totals].map(([unit, qty]) => `${qty}${unit}`).join(' · ');
  }
  function readRow(row) {
    const { line, unit, fieldItem } = row;
    const confirmed = row.confirmed.valueAsNumber, qty = row.quantity.valueAsNumber, previous = line.previous;
    const room = confirmed - previous;
    const badConfirmed = !Number.isInteger(confirmed) || confirmed < (fieldItem ? 1 : previous) || confirmed > 9999;
    const badQty = !Number.isInteger(qty) || qty < 0 || qty > room;
    return { confirmed, previous, qty, room, unit, changed: !!fieldItem || confirmed !== line.confirmed, badConfirmed, badQty };
  }

  const initialSubtotal = info.lines.reduce((n, line) => n + (line.unit_price ?? 0) * line.confirmed, 0);
  const initialPending = info.lines.some((line) => line.unit_price == null && line.confirmed > 0);
  const feeText = (subtotal, pending) => pending ? `확정 소계 ${won(subtotal)} + 금액 확인 대기` : won(subtotal);
  function draw() {
    const values = rows.map(readRow);
    rows.forEach((r, i) => {
      const v = values[i], invalid = v.badConfirmed || v.badQty;
      r.confirmed.setAttribute('aria-invalid', String(v.badConfirmed)); r.quantity.setAttribute('aria-invalid', String(invalid));
      r.quantity.max = v.badConfirmed ? 0 : v.room;
      r.minus.disabled = invalid || v.qty === 0;
      r.plus.disabled = invalid || v.qty === v.room;
      r.remaining.disabled = v.badConfirmed || v.qty === v.room;
      r.left.textContent = invalid ? '확인 필요' : `${v.room - v.qty}${v.unit}`;
      r.projectedText.textContent = invalid ? '입력 후 누적 확인 필요' : `입력 후 누적 ${v.previous + v.qty}${v.unit}`;
    });
    const changed = values.some((v) => v.changed);
    reasonLabel.textContent = changed ? '품목·수량 변경 사유' : '일부 수거·미수거 사유';
    reason.setAttribute('aria-label', reasonLabel.textContent);
    const invalid = values.some((v) => v.badConfirmed || v.badQty);
    const total = values.reduce((sum, v) => sum + v.previous + v.qty, 0);
    const rest = values.reduce((sum, v) => sum + v.room - v.qty, 0);
    summary.textContent = invalid ? '수량을 확인해 주세요.' : [
      `이전 수거 ${byUnit(values, (v) => v.previous)}`, `이번 수거 ${byUnit(values, (v) => v.qty)}`,
      `입력 후 누적 ${byUnit(values, (v) => v.previous + v.qty)}`, `남음 ${byUnit(values, (v) => v.room - v.qty)}`,
    ].join(' / ') + ` → ${total ? (rest ? '일부 수거' : '수거 완료') : '미수거'}`;
    fees.hidden = !changed || values.some((v) => v.badConfirmed);
    if (!fees.hidden) {
      const pending = rows.some((r, i) => r.line.unit_price == null && values[i].confirmed > 0);
      const subtotal = rows.reduce((n, r, i) => n + (r.line.unit_price ?? 0) * values[i].confirmed, 0);
      fees.textContent = `예상 수수료(감면 전) · 변경 전 ${feeText(initialSubtotal, initialPending)} → 변경 후 ${feeText(subtotal, pending)}. 수납은 별도로 기록합니다.`;
    }
  }
  for (const line of info.lines) addRow(line);
  const all = btn('남은 수량 전부', () => {
    for (const row of rows) { const v = readRow(row); if (!v.badConfirmed) row.quantity.value = v.room; }
    draw();
  }, 'small');
  const zero = btn('이번 수거 모두 0', () => { for (const row of rows) row.quantity.value = 0; draw(); }, 'small');
  const close = btn('입력 접기', collapse, 'small');
  const message = h('p', { role: 'alert' });
  const heading = h('h3', { tabindex: -1 }, 'PC 수거 처리');
  const box = h('section', { class: 'collection-entry', role: 'region', 'aria-label': 'PC 수거 처리', 'data-ux-dirty': 'true' },
    h('div', { class: 'collection-heading' }, heading, close),
    h('p', {}, h('strong', {}, request.receipt_no), ` · ${request.dong || '동 미지정'}`),
    h('p', { class: 'muted' }, '휴대폰과 PC의 행 번호는 다를 수 있습니다. 같은 접수번호에서 품목명·규격·단위를 맞추고, 입력 후 누적을 휴대폰의 목표 누적과 대조하세요.'),
    h('p', { class: 'notice info collection-comparison' }, '휴대폰 기준 이전 수거와 PC의 이전 수거가 같은지 확인하세요. 이미 입력했거나 결과를 가져온 수량은 다시 추가하지 마세요. 기준이 다르면 수거 이력을 먼저 대조하세요.'),
    editors);
  editors.append(h('label', { class: 'f' }, '실제 수거일', day), h('div', { class: 'row' }, all, zero),
    h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['품목', '이전 수거', '이번 수거 개수', '남음'].map((v) => h('th', {}, v)))), tbody)),
    h('p', { class: 'muted small' }, '현장에서 품목이 바뀌면 기존 품목의 현장 확인 수량을 줄이고 실제 품목을 추가하세요. 이번 수거에는 해당 수거일에 실은 수량만 입력합니다.'),
    pickerDetails, summary, fees, h('label', { class: 'f' }, reasonLabel, reason),
    h('p', { class: 'muted small' }, '실제 수거일과 개수로 배출표를 자동 배정하고 월보·대장에 반영합니다. 수납은 별도로 기록합니다.'));
  draw();
  const save = btn('수거 결과 저장', wrap(async () => {
    if (!parentAlive() || !box.isConnected) throw new Error('화면이나 접속 계정이 바뀌었습니다. 접수를 다시 열어 주세요.');
    if (!submitted) {
      before?.();
      const items = [], fieldItems = [];
      let changed = false, collected = 0, remaining = 0;
      for (const row of rows) {
        const v = readRow(row);
        if (v.badConfirmed) throw new Error(row.fieldItem ? '추가 품목의 현장 확인 수량은 1~9999의 정수로 입력하세요.'
          : '현장 확인 수량은 이전 수거 수량 이상, 9999 이하의 정수로 입력하세요.');
        if (!Number.isInteger(v.qty) || v.qty < 0) throw new Error('이번 수거 개수는 0 이상의 정수로 입력하세요.');
        if (v.badQty) throw new Error('이번 수거 개수가 남은 수량을 넘습니다.');
        if (row.fieldItem) fieldItems.push({ ...row.fieldItem, confirmed_qty: v.confirmed, qty: v.qty });
        else items.push({ line_id: row.line.line_id, qty: v.qty, ...(v.changed ? { confirmed_qty: v.confirmed } : {}) });
        changed ||= v.changed; collected += v.previous + v.qty; remaining += v.room - v.qty;
      }
      if (!reason.value.trim() && (changed || remaining || collected === 0)) throw new Error(reasonLabel.textContent + '를 입력하세요.');
      submitted = { req_ver: request.req_ver, basis: info.basis, collected_on: day.value, items, reason: reason.value.trim(), client_req_id: id };
      if (fieldItems.length) submitted.field_items = fieldItems;
    }
    editors.disabled = true; box.setAttribute('data-collection-submitted', ''); close.disabled = true; box.setAttribute('aria-busy', 'true');
    let result;
    try { result = await api(`${path}/${request.id}/collection`, { method: 'POST', body: submitted, _prefsOwner: owner }); }
    catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) {
        submitted = null; id = uuid(); editors.disabled = false; box.removeAttribute('data-collection-submitted'); draw(); save.textContent = '수거 결과 저장';
      }
      else { save.textContent = '저장 여부 다시 확인'; error.message += ' 같은 저장 버튼을 누르면 동일한 요청의 반영 여부를 확인합니다.'; }
      throw error;
    }
    finally { close.disabled = false; box.removeAttribute('aria-busy'); }
    if (parentAlive() && box.isConnected) await after(result);
  }, message), 'primary', { 'data-collection-save': '1' });
  box.append(h('div', { class: 'collection-footer' }, h('p', { class: 'muted small' }, '입력을 접어도 이 화면에서는 작성한 내용이 유지됩니다.'), message, save));
  return box;
}
