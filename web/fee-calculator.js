// PC·모바일 공용 계산 전용 도구. 요금표와 화면 메모리만 사용한다.
import { feeQuoteText, quoteFees, searchFees, won } from './fees.js';

function node(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key === 'value') el.value = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) if (child != null) el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return el;
}

export function feeCalculator({ table, state = {}, source = '', isActive = () => true }) {
  state.items ||= []; state.q ||= ''; state.category ||= ''; state.visible ||= 12;
  const rows = new Map(table.rows.map((row) => [row.id, row]));
  const button = (text, onclick, cls = '', extra = {}) => node('button', { type: 'button', class: 'fc-button ' + cls, onclick, ...extra }, text);
  const search = node('input', { type: 'search', value: state.q, placeholder: '예: 의자, 매트리스, ㅈㄹ', autocomplete: 'off', 'aria-label': '수수료 계산 검색' });
  const category = node('select', { 'aria-label': '수수료 계산 분류' }, node('option', { value: '' }, '모든 분류'),
    ...[...new Set(table.rows.map((row) => row.category))].filter(Boolean).map((name) => node('option', { value: name }, name)));
  category.value = state.category;
  const addQty = node('input', { type: 'number', min: 1, max: 9999, step: 1, inputmode: 'numeric', value: state.addQty || '1', 'aria-label': '담을 수량' });
  const results = node('div', { class: 'fc-results', 'aria-label': '계산할 규격 선택' });
  const resultCount = node('p', { class: 'fc-small', role: 'status' });
  const actionMessage = node('p', { class: 'fc-small', role: 'status', 'aria-live': 'polite' });
  const more = button('규격 더 보기', () => { state.visible += 24; drawResults(); }, 'fc-more');
  const lines = node('ul', { class: 'fc-lines', 'aria-label': '담은 품목' });
  const amount = node('strong', { class: 'fc-amount', 'aria-label': '계산 합계' });
  const units = node('p', { class: 'fc-small' });
  const pending = node('p', { class: 'fc-pending' });
  const error = node('p', { class: 'fc-error', role: 'alert' });
  const copied = node('p', { class: 'fc-small', role: 'status' });
  const fallback = node('textarea', { class: 'fc-copy-text', readonly: true, rows: 7, hidden: true, 'aria-label': '계산 내용' });
  const live = () => root.isConnected && isActive();
  let quote, revision = 0;
  const copy = button('계산 내용 복사', async () => {
    if (!live() || !quote || !state.items.length) return;
    const text = feeQuoteText(quote, source), sentRevision = revision;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(text);
      if (live() && revision === sentRevision) copied.textContent = '계산 내용을 복사했습니다.';
    } catch {
      if (!live() || revision !== sentRevision) return;
      copied.textContent = '아래 내용을 선택해 복사하세요.';
      fallback.value = text; fallback.hidden = false; fallback.focus(); fallback.select();
    }
  }, 'fc-primary fc-copy');
  const clear = button('계산 비우기', () => {
    if (!live()) return;
    state.items.splice(0); actionMessage.textContent = '계산할 품목을 비웠습니다.'; drawCart();
  }, 'fc-clear');
  function drawSummary() {
    revision++;
    fallback.hidden = true; copied.textContent = ''; error.textContent = '';
    try {
      quote = quoteFees(table, state.items);
      amount.textContent = quote.total == null ? '금액 확인 대기' : won(quote.total);
      amount.classList.toggle('fc-waiting', quote.total == null);
      pending.textContent = quote.pendingCount ? `확정 소계 ${won(quote.confirmedSubtotal)}` : '';
      pending.hidden = !quote.pendingCount;
      units.textContent = state.items.length ? `${quote.lines.length}종 · ${Object.entries(quote.qtyByUnit).map(([unit, qty]) => `${qty}${unit}`).join(' · ')}` : '품목을 담으면 바로 계산합니다.';
    } catch (e) {
      quote = null; amount.textContent = '—'; units.textContent = '수량과 선택한 규격을 확인하세요.';
      pending.hidden = true; error.textContent = e.message;
    }
    copy.disabled = !quote || !state.items.length;
    clear.disabled = !state.items.length;
    lines.querySelectorAll('[data-fee-id]').forEach((li) => {
      const item = state.items.find((entry) => entry.fee_rate_id === li.dataset.feeId);
      const row = quote?.lines.find((entry) => entry.fee_rate_id === li.dataset.feeId);
      li.querySelector('.fc-line-amount').textContent = row ? row.amount == null ? '금액 확인 대기' : won(row.amount) : '—';
      const invalid = !Number.isInteger(Number(item.qty)) || Number(item.qty) < 1 || Number(item.qty) > 9999;
      li.querySelector('input').setAttribute('aria-invalid', String(invalid));
      li.querySelector('.fc-minus').disabled = invalid || Number(item.qty) <= 1;
      li.querySelector('.fc-plus').disabled = invalid || Number(item.qty) >= 9999;
    });
  }
  function drawCart() {
    lines.replaceChildren(...state.items.map((item) => {
      const row = rows.get(item.fee_rate_id) || item;
      const qty = node('input', { type: 'number', min: 1, max: 9999, step: 1, inputmode: 'numeric', value: item.qty, 'aria-label': '수량' });
      qty.addEventListener('input', () => { if (live()) { item.qty = qty.value; drawSummary(); } });
      const step = (delta) => {
        if (!live()) return;
        const next = Number(item.qty) + delta;
        if (!Number.isInteger(next) || next < 1 || next > 9999) return;
        item.qty = next; qty.value = next; drawSummary();
      };
      const remove = button('삭제', () => {
        if (!live()) return;
        const at = state.items.indexOf(item); state.items.splice(at, 1); drawCart();
        (lines.querySelector('input') || search).focus();
      }, 'fc-remove', { 'aria-label': '품목 삭제' });
      return node('li', { 'data-fee-id': item.fee_rate_id },
        node('div', { class: 'fc-line-title' }, node('div', {}, node('strong', {}, row.name || '선택한 품목'), node('p', { class: 'fc-small' }, row.spec || '')), remove),
        node('div', { class: 'fc-line-controls' }, node('div', { class: 'fc-stepper' },
          button('−', () => step(-1), 'fc-minus', { 'aria-label': '수량 줄이기' }), qty,
          button('+', () => step(1), 'fc-plus', { 'aria-label': '수량 늘리기' }), node('span', {}, row.unit_label || '개')),
        node('strong', { class: 'fc-line-amount' })));
    }));
    if (!state.items.length) lines.append(node('li', { class: 'fc-empty' }, '품목·규격을 골라 담으세요.'));
    drawSummary();
  }
  function add(row) {
    if (!live()) return;
    try {
      const count = Number(addQty.value);
      quoteFees(table, [{ fee_rate_id: row.id, qty: count }]);
      const item = state.items.find((entry) => entry.fee_rate_id === row.id);
      if (item) {
        const next = Number(item.qty) + count;
        quoteFees(table, [{ fee_rate_id: row.id, qty: next }]);
        item.qty = next;
      } else state.items.push({ fee_rate_id: row.id, qty: count, name: row.name, spec: row.spec, unit_label: row.unit_label });
      actionMessage.textContent = `${row.name} ${row.spec} ${count}${row.unit_label || '개'} 담았습니다.`;
      addQty.removeAttribute('aria-invalid'); drawCart();
    } catch (e) {
      actionMessage.textContent = e.message; addQty.setAttribute('aria-invalid', 'true'); addQty.focus();
    }
  }
  function drawResults() {
    const matched = (state.q.trim() ? searchFees(table, state.q) : table.rows).filter((row) => !state.category || row.category === state.category);
    resultCount.textContent = `${matched.length}개 규격${matched.length > state.visible ? ` · ${Math.min(state.visible, matched.length)}개 표시` : ''}`;
    results.replaceChildren(...matched.slice(0, state.visible).map((row) => button('', () => add(row), 'fc-result', { 'aria-label': `계산에 추가: ${row.name} ${row.spec}` })));
    [...results.children].forEach((el, i) => {
      const row = matched[i], line = quoteFees(table, [{ fee_rate_id: row.id, qty: 1 }]).lines[0];
      el.append(node('span', { class: 'fc-result-copy' }, node('strong', {}, row.name), node('span', {}, row.spec),
        node('small', {}, line.unit_price == null ? '금액 확인 대기' : `${won(line.unit_price)} / ${line.unit_label}`)), node('span', { class: 'fc-add', 'aria-hidden': 'true' }, '+ 담기'));
    });
    if (!matched.length) results.append(node('p', { class: 'fc-empty' }, '일치하는 품목이 없습니다. 다른 이름이나 초성으로 찾아보세요.'));
    more.hidden = matched.length <= state.visible;
  }
  const filter = () => { state.q = search.value; state.category = category.value; state.visible = 12; drawResults(); };
  search.addEventListener('input', filter); category.addEventListener('change', filter);
  search.addEventListener('keydown', (e) => {
    if (!e.isComposing && !e.ctrlKey && !e.altKey && !e.metaKey && ['Enter', 'ArrowDown'].includes(e.key)) {
      const first = results.querySelector('button');
      if (first) { e.preventDefault(); first.focus(); }
    }
  });
  addQty.addEventListener('input', () => { state.addQty = addQty.value; addQty.removeAttribute('aria-invalid'); });
  const root = node('div', { class: 'fee-calculator' },
    node('section', { class: 'fc-panel fc-search' }, node('h2', {}, '품목 고르기'), search,
      node('div', { class: 'fc-quick' }, ...['의자', '매트리스', '소파', '장롱', '책상', '냉장고'].map((name) => button(name, () => { search.value = name; filter(); }))),
      node('div', { class: 'fc-filter' }, category, node('label', {}, '담을 수량', addQty)), resultCount, results, more, actionMessage),
    node('section', { class: 'fc-panel fc-total', 'aria-live': 'polite', 'aria-atomic': 'true' },
      node('p', { class: 'fc-kicker' }, '수수료 합계 · 감면 전'), amount, units, pending, error),
    node('section', { class: 'fc-panel fc-cart' }, node('div', { class: 'fc-cart-head' }, node('h2', {}, '계산할 품목'), clear), lines, copy, copied, fallback,
      node('div', { class: 'fc-source' }, node('p', {}, `요금표 ${table.version_id || '미확인'}`), node('p', {}, source))));
  drawResults(); drawCart();
  return root;
}
