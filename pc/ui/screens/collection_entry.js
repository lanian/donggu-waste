// 접수 원본/수납과 별도로 실제 수거분을 입력한다. 화면 이탈·계정 전환 뒤 응답은 무시한다.
import { api, boot, btn, h, sessionOwner, toast, uuid, wrap } from '../core.js';

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
          toast(`수거 결과를 저장했습니다. 이번 ${result.added}개 · 누적 ${result.total}개${result.labels_pending ? ` · 배출표 번호 미배정 ${result.labels_pending}장` : ''}`);
          await refresh(result);
        });
        panel.replaceChildren(editor);
      }
      panel.hidden = false; button.setAttribute('aria-expanded', 'true');
      editor.querySelector('h3').focus();
    } catch (error) { if (alive()) toast(error.message, true); }
    finally { if (alive()) button.disabled = request.status === 'DONE'; }
  }, 'primary small', { 'aria-expanded': 'false', 'aria-controls': panel.id,
    disabled: request.status === 'DONE' || !request.collection?.lines?.some((line) => line.remaining > 0) });
  return h('div', { class: 'collection-workspace' }, button, panel, feedback);
}

function collectionForm(request, path, parentAlive, owner, before, collapse, after) {
  const info = request.collection;
  const day = h('input', { type: 'date', value: boot.today, max: boot.today, 'aria-label': '실제 수거일' });
  const reason = h('input', { maxlength: 200, 'aria-label': '일부 수거·미수거 사유', placeholder: '예: 의자 1개 수거, 나머지는 미배출' });
  const summary = h('p', { class: 'notice info', role: 'status', 'aria-label': '수거 입력 요약' });
  const quantities = info.lines.map((line, i) => h('input', { type: 'number', min: 0, max: line.remaining, step: 1, value: 0,
    inputmode: 'numeric', class: 'w-xs', 'aria-label': `${i + 1}번 이번 수거 개수` }));
  const left = info.lines.map(() => h('span'));
  let submitted = null, id = uuid();
  const setQuantity = (i, value) => {
    if (submitted) return;
    quantities[i].value = value; draw();
  };
  const stepQuantity = (i, delta) => {
    const input = quantities[i], n = Number(input.value);
    if (submitted || input.value === '' || !Number.isInteger(n) || n < 0 || n > info.lines[i].remaining) return;
    if (n + delta >= 0 && n + delta <= info.lines[i].remaining) setQuantity(i, n + delta);
  };
  const quick = info.lines.map((line, i) => ({
    minus: btn('−', () => stepQuantity(i, -1), 'small', { 'aria-label': `${i + 1}번 수거 수량 줄이기` }),
    plus: btn('+', () => stepQuantity(i, 1), 'small', { 'aria-label': `${i + 1}번 수거 수량 늘리기` }),
    remaining: btn('남은 수량', () => setQuantity(i, line.remaining), 'small', { 'aria-label': `${i + 1}번 남은 수량 채우기` }),
  }));
  const rows = info.lines.map((line, i) => h('tr', {}, h('th', { scope: 'row' }, `${i + 1}. ${line.name} ${line.spec || ''}`, h('small', { class: 'block muted' }, `확인 수량 ${line.confirmed}${line.unit_label}`)),
    h('td', {}, `${line.previous}${line.unit_label}`), h('td', {}, h('div', { class: 'row' }, quick[i].minus, quantities[i], quick[i].plus, h('span', {}, line.unit_label), quick[i].remaining)), h('td', {}, left[i])));
  const draw = () => {
    let added = 0, invalid = false;
    info.lines.forEach((line, i) => {
      const n = Number(quantities[i].value), bad = quantities[i].value === '' || !Number.isInteger(n) || n < 0 || n > line.remaining;
      invalid ||= bad; quantities[i].setAttribute('aria-invalid', String(bad));
      quick[i].minus.disabled = !!submitted || bad || n === 0;
      quick[i].plus.disabled = !!submitted || bad || n === line.remaining;
      quick[i].remaining.disabled = !!submitted || (!bad && n === line.remaining);
      added += n; left[i].textContent = bad ? '확인 필요' : `${line.remaining - n}${line.unit_label}`;
    });
    const rest = info.lines.reduce((sum, line) => sum + line.remaining, 0) - added;
    summary.textContent = invalid ? '수량을 확인해 주세요.' : `이전 수거 ${info.previous_total}개 · 이번 수거 ${added}개 · 남음 ${rest}개 → ${rest === 0 ? '수거 완료' : info.previous_total + added ? '일부 수거' : '미수거'}`;
  };
  quantities.forEach((input) => input.addEventListener('input', draw));
  const all = btn('남은 수량 전부', () => { quantities.forEach((input, i) => { input.value = info.lines[i].remaining; }); draw(); }, 'small');
  const zero = btn('이번 수거 0개', () => { quantities.forEach((input) => { input.value = 0; }); draw(); }, 'small');
  const controls = [day, reason, ...quantities, all, zero, ...quick.flatMap((row) => [row.minus, row.plus, row.remaining])];
  const freeze = (value) => { controls.forEach((el) => { el.disabled = value; }); if (!value) draw(); };
  const close = btn('입력 접기', collapse, 'small');
  const message = h('p', { role: 'alert' });
  const heading = h('h3', { tabindex: -1 }, 'PC 수거 처리');
  const box = h('section', { class: 'collection-entry', role: 'region', 'aria-label': 'PC 수거 처리', 'data-ux-dirty': 'true' },
    h('div', { class: 'collection-heading' }, heading, close),
    h('p', {}, h('strong', {}, request.receipt_no), ` · ${request.dong || '동 미지정'}`),
    h('p', { class: 'muted' }, '휴대폰의 [PC 입력 도움]에서 같은 접수번호와 품목을 확인하세요. 이미 수거한 수량을 빼고 이번에 추가로 수거한 개수만 입력합니다.'),
    h('label', { class: 'f' }, '실제 수거일', day), h('div', { class: 'row' }, all, zero),
    h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['품목', '이전 수거', '이번 수거 개수', '남음'].map((v) => h('th', {}, v)))), h('tbody', {}, ...rows))),
    summary, h('label', { class: 'f' }, '일부 수거·미수거 사유', reason),
    h('p', { class: 'muted small' }, '실제 수거일과 개수로 배출표를 자동 배정하고 월보·대장에 반영합니다. 수납은 별도로 기록합니다.'));
  draw();
  const save = btn('수거 결과 저장', wrap(async () => {
    if (!parentAlive() || !box.isConnected) throw new Error('화면이나 접속 계정이 바뀌었습니다. 접수를 다시 열어 주세요.');
    if (!submitted) {
      before?.();
      const items = info.lines.map((line, i) => {
        const qty = Number(quantities[i].value);
        if (quantities[i].value === '' || !Number.isInteger(qty) || qty < 0) throw new Error('이번 수거 개수는 0 이상의 정수로 입력하세요.');
        if (qty > line.remaining) throw new Error('이번 수거 개수가 남은 수량을 넘습니다.');
        return { line_id: line.line_id, qty };
      });
      if (items.reduce((sum, item) => sum + item.qty, 0) < info.lines.reduce((sum, line) => sum + line.remaining, 0) && !reason.value.trim()) throw new Error('일부 수거·미수거 사유를 입력하세요.');
      submitted = { req_ver: request.req_ver, basis: info.basis, collected_on: day.value, items, reason: reason.value.trim(), client_req_id: id };
    }
    freeze(true); close.disabled = true; box.setAttribute('aria-busy', 'true');
    let result;
    try { result = await api(`${path}/${request.id}/collection`, { method: 'POST', body: submitted, _prefsOwner: owner }); }
    catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) { submitted = null; id = uuid(); freeze(false); save.textContent = '수거 결과 저장'; }
      else { save.textContent = '저장 여부 다시 확인'; error.message += ' 같은 저장 버튼을 누르면 동일한 요청의 반영 여부를 확인합니다.'; }
      throw error;
    }
    finally { close.disabled = false; box.removeAttribute('aria-busy'); }
    if (parentAlive() && box.isConnected) await after(result);
  }, message), 'primary', { 'data-collection-save': '1' });
  box.append(h('div', { class: 'collection-footer' }, h('p', { class: 'muted small' }, '입력을 접어도 이 화면에서는 작성한 내용이 유지됩니다.'), message, save));
  return box;
}
