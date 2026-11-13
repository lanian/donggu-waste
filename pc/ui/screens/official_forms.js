// 선택한 동의 실제 수거분을 원본 월보·일일 수불대장으로 내려받는다.
import { won } from '/web/fees.js';
import { api, apiRaw, boot, btn, canWork, grid, h, searchbox, sessionOwner, toast } from '../core.js';

const number = (v) => Number(v || 0).toLocaleString('ko-KR');

function officialForms(parentCurrent) {
  if (officialForms.owner !== boot.me.username) {
    officialForms.owner = boot.me.username;
    officialForms.state = { dong_id: boot.dongs?.[0]?.id || '', date: boot.today };
  }
  const s = officialForms.state;
  const owner = sessionOwner();
  let panel, sequence = 0, loading = false, outputBusy = false, result = null, resultKey = '';
  const current = () => panel?.isConnected && parentCurrent() && !!boot?.me && sessionOwner() === owner;
  const dong = h('select', { 'aria-label': '서식 행정동' }, h('option', { value: '', selected: !s.dong_id }, '행정동 선택'),
    ...(boot.dongs || []).map((d) => h('option', { value: d.id, selected: s.dong_id === d.id }, d.name)));
  const date = h('input', { type: 'date', value: s.date, 'aria-label': '서식 처리일' });
  const message = h('p', { class: 'msg', role: 'status' });
  const content = h('div', { 'aria-live': 'polite' });
  const criteria = () => ({ dong_id: dong.value, date: date.value });
  const key = () => JSON.stringify(criteria());
  const buttons = [];
  const sync = () => {
    query.disabled = loading;
    for (const [kind, button] of buttons) {
      const ready = result && resultKey === key() && !loading;
      button.disabled = !ready || outputBusy || !(kind === 'xlsx' ? result?.monthly.ready : kind === 'odt' ? result?.daily.ready : result?.monthly.ready && result?.daily.ready);
      if (outputBusy) button.setAttribute('aria-busy', 'true'); else button.removeAttribute('aria-busy');
    }
  };
  const changed = () => {
    Object.assign(s, criteria()); sequence++; loading = false;
    content.removeAttribute('aria-busy');
    message.textContent = '서식 조건을 바꿨습니다. [서식 조회]를 눌러 주세요.';
    sync();
  };
  const load = async () => {
    if (!current() || loading) return;
    const c = criteria(), signature = key();
    Object.assign(s, c);
    if (!c.dong_id || !/^\d{4}-\d{2}-\d{2}$/.test(c.date)) { message.textContent = '행정동과 처리일을 골라 주세요.'; return; }
    const my = ++sequence;
    loading = true; result = null; message.textContent = '실제 수거분과 재고를 조회하는 중…'; content.setAttribute('aria-busy', 'true'); sync();
    try {
      const d = await api('reports/forms?' + new URLSearchParams(c), { timeoutMs: 15000, _prefsOwner: owner });
      if (!current() || my !== sequence) return;
      result = d; resultKey = signature;
      const rows = [['월보 · ' + d.month, d.monthly, '월보 수거 수량'], ['일일 대장 · ' + d.date, d.daily, '일일 대장 수거 수량']];
      content.replaceChildren(
        h('p', { class: 'muted small' }, d.source_note),
        grid(['서식 기간', '수거 수량', '수거분 수수료', '이월', '수령', '불출', '잔량'], rows.map(([label, t, aria]) => [label,
          h('b', { 'aria-label': aria }, number(t.qty) + '개'), won(t.fee), number(t.opening) + '장', number(t.received) + '장', number(t.issued) + '장', number(t.closing) + '장']), { num: [1, 2, 3, 4, 5, 6] }),
        ...rows.flatMap(([label, t]) => (t.issues || []).map((issue) => h('p', { class: 'notice bad' }, label + ': ' + issue))));
      message.textContent = d.monthly.ready && d.daily.ready ? '서식 작성 준비가 됐습니다.' : '표시된 항목을 보완한 뒤 다시 조회해 주세요.';
    } catch (error) {
      if (!current() || my !== sequence) return;
      result = null; content.replaceChildren(btn('서식 다시 시도', load, 'small'));
      message.textContent = error.message;
    } finally { if (current() && my === sequence) { loading = false; content.removeAttribute('aria-busy'); sync(); } }
  };
  const download = async (format) => {
    if (!current() || !canWork() || outputBusy || loading || resultKey !== key()) return;
    const c = criteria(), signature = key(), my = sequence;
    outputBusy = true; sync();
    try {
      const response = await apiRaw('reports/forms/download', { method: 'POST', body: { ...c, format }, _prefsOwner: owner });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || '서식을 만들지 못했습니다.');
      const blob = await response.blob();
      if (!current() || sequence !== my || signature !== key()) return;
      const disposition = response.headers.get('Content-Disposition') || '';
      const filename = decodeURIComponent(disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1] || `동구-업무서식.${format}`);
      const url = URL.createObjectURL(blob), link = h('a', { href: url, download: filename });
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast('작성한 서식을 내려받았습니다');
    } catch (error) { if (current()) { message.textContent = error.message; toast(error.message, true); } }
    finally { outputBusy = false; if (current()) sync(); }
  };
  const query = btn('서식 조회', load, 'primary');
  if (canWork()) for (const [kind, label] of [['xlsx', '월보 XLSX'], ['odt', '일일 대장 ODT'], ['zip', '두 서식 ZIP']]) buttons.push([kind, btn(label, () => download(kind), kind === 'zip' ? 'primary small' : 'small', { disabled: true })]);
  [dong, date].forEach((input) => input.addEventListener('change', changed));
  panel = h('section', { class: 'card', 'aria-label': '원본 서식 자동 작성' }, h('h2', {}, '원본 서식 자동 작성'),
    h('p', { class: 'muted' }, '월보는 처리일이 속한 달 전체, 일일 대장은 선택한 처리일의 실제 수거분으로 작성합니다.'),
    searchbox([['행정동', dong], ['처리일', date]], query), message, content,
    h('div', { class: 'row' }, ...buttons.map(([, button]) => button)),
    h('p', { class: 'muted small' }, canWork() ? '내려받는 파일에는 성명·주소 등 접수 정보가 포함됩니다. 파일을 다시 받아도 번호와 재고는 바뀌지 않습니다.' : '원본 파일은 접수 담당자와 운영 관리자가 내려받을 수 있습니다.'));
  setTimeout(() => { if (current() && dong.value) load(); }, 0);
  return panel;
}

export { officialForms };
