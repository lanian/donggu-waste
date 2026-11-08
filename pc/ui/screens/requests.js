// PC 화면: 접수 목록·일괄 처리·CSV 일괄 등록
import { won } from '/web/fees.js';
import { CONTACT, PAY, STATUS, api, boot, btn, canWork, chip, confirmDlg, dongOpts, download, fmtPhone, go, grid, gridbar, h, modal, pagehead, post, reasonDlg, routeEpoch, searchbox, statusChip, toast, wrap } from '../core.js';
async function screenRequests() {
  const myRoute = routeEpoch, myHash = location.hash;
  let view, loadSeq = 0, ready = false;
  const current = () => view?.isConnected && myRoute === routeEpoch && location.hash === myHash;
  const f = (screenRequests.f ||= { q: '', date_field: 'planned', date_from: '', date_to: '', status: 'OPEN', crew: '', dong: '', pay: '', channel: '', quick: 'ALL', sort: '', dir: '', page: 1, size: 50 });
  const sel = (screenRequests.sel ||= new Set());
  const q = h('input', { type: 'search', id: 'rq-q', placeholder: '이름·연락처·주소·접수번호', value: f.q });
  const dfield = h('select', { id: 'rq-df', 'aria-label': '기간 기준' }, h('option', { value: 'planned', selected: f.date_field === 'planned' }, '예정일'), h('option', { value: 'received', selected: f.date_field === 'received' }, '접수일'));
  const dfrom = h('input', { type: 'date', id: 'rq-from', 'aria-label': '기간 시작', value: f.date_from }), dto = h('input', { type: 'date', id: 'rq-to', 'aria-label': '기간 끝', value: f.date_to });
  const status = h('select', { id: 'rq-status' }, ...[['OPEN', '미완료 전체'], ['', '전체'], ...Object.entries(STATUS)].map(([v, t]) => h('option', { value: v, selected: v === f.status }, t)));
  const crew = h('select', { id: 'rq-crew' }, h('option', { value: '' }, '전체'), ...boot.crews.map((c) => h('option', { value: c.id, selected: c.id === f.crew }, c.name)));
  const dong = h('select', { id: 'rq-dong' }, ...dongOpts(f.dong, { all: true }));
  const pay = h('select', { id: 'rq-pay' }, h('option', { value: '' }, '전체'), ...Object.entries(PAY).map(([k, t]) => h('option', { value: k, selected: k === f.pay }, t)));
  const channel = h('select', { id: 'rq-channel' }, h('option', { value: '' }, '전체'), ...boot.channels.map((c) => h('option', { value: c, selected: c === f.channel }, c)));
  const count = h('b', {}, '0');
  const tbody = h('tbody');
  const thead = h('thead');
  const quickBar = h('div', { class: 'quick' });
  const pager = h('div', { class: 'pager' });
  const selBar = h('div', { class: 'selbar' });
  const QUICK = [['ALL', '전체'], ['TODAY', '오늘 예정'], ['OVERDUE', '지난 예정일 미처리'], ['NODATE', '예정일 없음'], ['NOLOC', '위치 없음'], ['FEE', '금액 확인 대기'], ['UNASSIGNED', '미배정'], ['REFUND', '환불 필요'], ['FIELD', '현장 접수 확인']];
  const COLS = [['', null], ['번호', null], ['접수번호', 'receipt_no'], ['예정일', 'planned_date'], ['동', 'dong'], ['이름', 'name'], ['연락처', null], ['배출 주소', 'address'],
    ['품목', null], ['상태', 'status'], ['금액', null], ['위치', null], ['배정', null]];
  let res = { rows: [], total: 0, page: 1, pages: 1, counts: {} };
  const work = canWork();
  const drawHead = () => {
    const pageIds = res.rows.map((r) => r.id);
    const all = h('input', { type: 'checkbox', 'aria-label': '이 쪽 모두 고르기', disabled: !ready, checked: pageIds.length && pageIds.every((i) => sel.has(i)),
      onchange: (e) => { if (!ready || !current() || !e.currentTarget.isConnected) return; pageIds.forEach((i) => (e.target.checked ? sel.add(i) : sel.delete(i))); draw(); } });
    thead.replaceChildren(h('tr', {}, ...COLS.map(([t, key], i) => {
      if (i === 0) return h('th', { class: 'chk', scope: 'col' }, work ? all : null);
      if (!key) return h('th', { class: i === 1 ? 'num' : null, scope: 'col' }, t);
      const on = f.sort === key;
      return h('th', { scope: 'col', 'aria-sort': on ? (f.dir === 'desc' ? 'descending' : 'ascending') : 'none' },
        h('button', { type: 'button', class: 'sort' + (on ? ' on' : ''), onclick: () => { if (on) f.dir = f.dir === 'desc' ? '' : 'desc'; else { f.sort = key; f.dir = ''; } f.page = 1; reload(); } },
          t, h('span', { class: 'arr', 'aria-hidden': 'true' }, on ? (f.dir === 'desc' ? '▼' : '▲') : '↕')));
    })));
  };
  const draw = () => {
    if (!ready || !current()) return;
    quickBar.replaceChildren(...QUICK.map(([k, t]) => h('button', { type: 'button', class: 'qchip' + (f.quick === k ? ' on' : ''), 'aria-pressed': f.quick === k ? 'true' : 'false',
      onclick: () => { f.quick = k; f.page = 1; reload(); } }, t, h('b', {}, (k === 'ALL' ? res.counts.ALL : res.counts[k]) ?? 0))));
    count.textContent = res.total.toLocaleString();
    drawHead();
    const shortDate = (d) => (!d ? '-' : d === boot.today ? '오늘' : d.slice(5));
    const base = (res.page - 1) * res.size;
    tbody.replaceChildren(...(res.rows.length ? res.rows.map((r, ri) => h('tr', { class: 'click' + (sel.has(r.id) ? ' sel' : ''), onclick: (e) => { if (!['A', 'INPUT', 'LABEL'].includes(e.target.tagName)) go(`#/request/${r.id}`); } },
      h('td', { class: 'chk' }, work ? h('input', { type: 'checkbox', 'aria-label': `${r.receipt_no} 고르기`, checked: sel.has(r.id), onchange: (e) => { if (!ready || !current() || !e.currentTarget.isConnected) return; e.target.checked ? sel.add(r.id) : sel.delete(r.id); draw(); } }) : null),
      h('td', { class: 'num rn' }, base + ri + 1), h('td', { class: 'code' }, h('a', { href: `#/request/${r.id}`, title: r.receipt_no }, r.receipt_no.replace(/^DG-\d{4}/, ''))), h('td', { class: 'code' }, shortDate(r.planned_date)),
      h('td', { class: 'nowrap' }, r.dong || h('span', { class: 'muted' }, '-')), h('td', { class: 'nowrap' }, r.name), h('td', { class: 'code' }, fmtPhone(r.phone)),
      h('td', {}, h('span', { class: 'clamp' }, r.address)),
      h('td', {}, h('span', { class: 'clamp' }, r.items.map((i) => `${i.name} ${i.spec} ${i.qty}${i.unit_label}`).join(', '))),
      h('td', {}, r.status !== 'RECEIVED' ? statusChip(r.status) : null, r.state.contact !== 'NOT_CONTACTED' ? chip(CONTACT[r.state.contact]) : null, r.must_today ? chip('오늘 필수', 'warn') : null,
        (r.warnings || []).length ? h('span', { class: 'chip bad', title: r.warnings.join(' ') }, '관할 확인') : null, r.channel && r.channel !== '전화' ? chip(r.channel) : null),
      h('td', {}, r.status === 'CANCELLED' ? null : r.state.fee.final == null ? chip('금액 확인 대기', 'warn') : won(r.state.fee.final)),
      h('td', { class: 'c' }, r.lat == null ? chip('위치 없음', 'warn') : '✓'),
      h('td', {}, r.assigned_to ? `${r.assigned_to.name} ${r.assigned_to.work_date}` : ''))) : [h('tr', {}, h('td', { colspan: COLS.length, class: 'muted' }, f.q ? `'${f.q}'에 맞는 접수가 없습니다. 초성(예: ㄱㅅ)이나 전화번호 뒷자리로도 찾을 수 있습니다.` : '해당 접수가 없습니다. ', !f.q && work ? h('a', { href: '#/request/new' }, '전화 접수 등록 →') : null))]));
    const sizeSel = h('select', { 'aria-label': '쪽당 건수', onchange: (e) => { f.size = +e.target.value; f.page = 1; reload(); } }, ...[20, 50, 100, 200].map((n) => h('option', { value: n, selected: n === f.size }, `${n}건씩`)));
    pager.replaceChildren(btn('‹ 이전', () => { f.page = res.page - 1; reload(); }, 'small', { disabled: res.page <= 1 }), h('span', { class: 'pg' }, `${res.page} / ${res.pages}쪽`),
      btn('다음 ›', () => { f.page = res.page + 1; reload(); }, 'small', { disabled: res.page >= res.pages }), sizeSel);
    selBar.replaceChildren(...(work && sel.size ? [h('b', {}, `${sel.size}건 선택`), btn('선택 해제', () => { sel.clear(); draw(); }, 'small'),
      btn('예정일 변경', bulkDate, 'small'), btn('동 지정', bulkDong, 'small'), btn('취소', bulkCancel, 'small danger'), btn('목록 인쇄', bulkPrint, 'small'), btn('배출 표지', bulkLabels, 'small'), btn('CSV', bulkCsv, 'small')] : []));
  };
  const load = wrap(async () => {
    if (!current()) return;
    const my = ++loadSeq;
    Object.assign(f, { q: q.value, date_field: dfield.value, date_from: dfrom.value, date_to: dto.value, status: status.value, crew: crew.value, dong: dong.value, pay: pay.value, channel: channel.value });
    const qs = new URLSearchParams(Object.entries(f).map(([k, v]) => [k, String(v ?? '')]));
    markLoading();
    let next;
    try { next = await api('requests?' + qs); }
    catch (e) {
      if (my !== loadSeq || !current()) return;
      tbody.removeAttribute('aria-busy');
      tbody.replaceChildren(h('tr', {}, h('td', { colspan: COLS.length, class: 'muted' }, '목록을 불러오지 못했습니다. ', btn('다시 시도', () => load(), 'small'))));
      throw e;
    }
    if (my !== loadSeq || !current()) return;
    res = next;
    f.page = res.page;
    ready = true; tbody.removeAttribute('aria-busy');
    draw();
  });
  // 조건을 바꾸면 이전 결과를 지우고 불러오는 중 표시 (옛 목록을 새 결과로 오인하지 않게)
  const markLoading = () => {
    ready = false; tbody.setAttribute('aria-busy', 'true');
    thead.querySelectorAll('input').forEach((x) => { x.disabled = true; });
    pager.querySelectorAll('button').forEach((x) => { x.disabled = true; });
    selBar.querySelectorAll('button').forEach((x) => { x.disabled = true; });
    tbody.replaceChildren(h('tr', {}, h('td', { colspan: COLS.length, class: 'muted' }, '불러오는 중…')));
  };
  const reload = () => { if (current()) load(); };
  let t = null;
  q.addEventListener('input', () => { clearTimeout(t); loadSeq++; markLoading(); t = setTimeout(() => { if (current()) { f.page = 1; load(); } }, 300); });
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter') { clearTimeout(t); f.page = 1; load(); } });
  [dfield, dfrom, dto, status, crew, dong, pay, channel].forEach((x) => x.addEventListener('change', () => { f.page = 1; reload(); }));
  // 일괄 처리 (고른 건, 한 번에 한 트랜잭션·건별 이력)
  const ids = () => [...sel];
  const done = (m) => { toast(m); sel.clear(); load(); };
  const bulkDate = wrap(async () => {
    const d = h('input', { type: 'date', value: boot.today });
    const r = await reasonDlg(`예정일 변경 · ${sel.size}건`, boot.postpone_codes, { ok: '변경', before: [h('label', { class: 'f' }, '새 수거 예정일', d)],
      submit: async (v) => { if (!d.value) throw new Error('새 예정일을 고르세요.'); return post('requests/bulk', { action: 'reschedule', ids: ids(), date: d.value, ...v }); } });
    if (!r) return;
    (r.warnings || []).forEach((w) => toast(w, true));
    done(`${r.changed}건 예정일을 바꿨습니다`);
  });
  const bulkDong = wrap(async () => {
    const ds = h('select', {}, ...dongOpts(''));
    const why = h('input', { placeholder: '마감한 날의 접수가 있으면 필수', class: 'fill' });
    const r = await modal({ title: `동 지정 · ${sel.size}건`, ok: '지정', body: [h('label', { class: 'f' }, '행정동', ds), h('label', { class: 'f' }, '사유', why)],
      onOk: () => post('requests/bulk', { action: 'dong', ids: ids(), dong: ds.value, reason: why.value }) });
    if (r) done(`${r.changed}건 동을 지정했습니다`);
  });
  const bulkCancel = wrap(async () => {
    const r = await reasonDlg(`접수 취소 · ${sel.size}건`, boot.cancel_codes, { ok: '취소 기록', danger: true, label: '취소 사유',
      lead: [h('p', {}, '기록은 지워지지 않습니다. 이미 휴대폰에 보낸 건은 수거팀에 전화로 알리세요.')], submit: (v) => post('requests/bulk', { action: 'cancel', ids: ids(), ...v }) });
    if (!r) return;
    if (r.refund_needed.length) toast(`납부 기록이 있는 ${r.refund_needed.length}건은 '환불 필요'입니다: ${r.refund_needed.join(', ')}`, true);
    if (r.field_notice_needed) toast(`휴대폰에 보낸 ${r.field_notice_needed}건은 수거팀에 전화로 알리세요.`, true);
    done(`${r.cancelled}건 취소했습니다` + (r.skipped ? ` (이미 취소 ${r.skipped}건 제외)` : ''));
  });
  const bulkPrint = wrap(async () => {
    if (!(await confirmDlg('목록 인쇄', `고른 ${sel.size}건을 이름·연락처·주소와 함께 인쇄합니다. 출력 기록이 건마다 남고, 업무가 끝나면 파쇄해야 합니다.`, { ok: '인쇄 화면 열기' }))) return;
    const r = await post('requests/print', { ids: ids() });
    window.open('/print/requests/' + r.token, 'dg-print');
  });
  const bulkLabels = wrap(async () => {
    const r = await post('requests/labels', { ids: ids() });
    window.open('/print/doc/' + r.token, 'dg-print'); toast('배출 표지 인쇄 화면을 열었습니다 (A4 한 장에 6개, 품목 수량만큼)');
  });
  const bulkCsv = wrap(async () => {
    const pii = h('input', { type: 'checkbox' });
    const ok = await modal({ title: `고른 ${sel.size}건 CSV`, ok: '내려받기', body: [h('p', {}, '기본은 통계 CSV와 같이 이름·연락처·주소 없이 내보냅니다.'),
      h('label', { class: 'row' }, pii, '이름·연락처·주소 포함 (개인정보 — 승인된 용도만, 건마다 내보낸 기록이 남음)')],
      onOk: async () => { if (pii.checked && !(await confirmDlg('개인정보 포함 내보내기', '이름·연락처·주소가 든 파일입니다. 승인된 보안 매체에만 저장하고 쓴 뒤 지우세요. 계속할까요?', { ok: '개인정보 포함 내보내기', danger: true }))) throw new Error('취소했습니다.'); return { pii: pii.checked }; } });
    if (!ok) return;
    await download('requests/export', `동구-선택-${sel.size}건${ok.pii ? '-개인정보' : ''}.csv`, { method: 'POST', body: { ids: ids(), include_pii: ok.pii } });
    toast('내려받았습니다');
  });
  // mount한 화면만 조회한다. 더 늦은 render가 만든 뒤 버린 화면은 공유 조건에도 손대지 않는다.
  setTimeout(() => { if (current()) load(); }, 0);
  setTimeout(() => { if (current() && (!document.activeElement || document.activeElement === document.body)) q.focus(); }, 30);
  // 조회조건 상자(회색 이름 칸 + 값 칸) → 목록 머리줄(총 N건 · 단추) → 목록 표
  const cl = (t, forId) => h('label', { class: 'cond-l', for: forId }, t);
  view = h('div', {}, h('div', { class: 'pagehead requests-heading' },
    h('div', {}, h('h1', {}, '접수'), h('p', { class: 'muted small' }, '단축키: ', h('kbd', {}, '/'), ' 찾기 · ', h('kbd', {}, 'N'), ' 새 접수')),
    work ? btn('+ 전화 접수 등록', () => go('#/request/new'), 'primary requests-create') : null),
    h('div', { class: 'card filters searchbox', role: 'search', 'aria-label': '조회조건' },
      h('div', { class: 'cond' }, cl('검색어', 'rq-q'), h('div', { class: 'cond-v' }, q), cl('기간', 'rq-from'), h('div', { class: 'cond-v' }, dfield, dfrom, '~', dto), cl('처리 상태', 'rq-status'), h('div', { class: 'cond-v' }, status),
        cl('수거팀', 'rq-crew'), h('div', { class: 'cond-v' }, crew), cl('행정동', 'rq-dong'), h('div', { class: 'cond-v' }, dong), cl('수납 상태', 'rq-pay'), h('div', { class: 'cond-v' }, pay),
        cl('접수 경로', 'rq-channel'), h('div', { class: 'cond-v' }, channel),
        h('span', { class: 'cond-l' }, '빠른 조회'), h('div', { class: 'cond-v wide2' }, quickBar)),
      h('div', { class: 'cond-btns' }, btn('검색', () => { f.page = 1; load(); }, 'dark'))),
    h('div', { class: 'gridbar' }, h('span', { class: 'cnt' }, '총 ', count, '건'), selBar, h('span', { class: 'grow' }),
      work ? btn('일괄 등록(CSV)', () => importDlg().then((n) => { if (n) load(); }), '') : null),
    h('div', { class: 'card tablecard' }, h('table', { class: 'grid' }, thead, tbody)), pager);
  markLoading(); drawHead();
  return view;
}

// 접수 일괄 등록: 서식 내려받기 → 파일 고르기 → 미리보기(오류·중복 후보) → 등록
async function importDlg() {
  const file = h('input', { type: 'file', accept: '.csv,.txt', 'aria-label': '접수 CSV 파일' });
  const skip = h('input', { type: 'checkbox' });
  const out = h('div', { class: 'imp-out' });
  let previewed = false;
  const raw = async () => { const f0 = file.files[0]; if (!f0) throw new Error('파일을 고르세요.'); return new Uint8Array(await f0.arrayBuffer()); };
  const prev = async () => {
    const r = await api('requests/import?preview=1', { method: 'POST', raw: await raw() });
    previewed = true;
    out.replaceChildren(h('p', {}, `${r.rows}행 · 등록 가능 ${r.ok}행 · 오류 ${r.errors}행 · 중복 후보 ${r.duplicates}행`),
      h('div', { class: 'imp-scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ...['행', '이름', '연락처', '주소', '예정일', '동', '경로', '품목', '확인'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...r.lines.map((x) => h('tr', { class: x.errors.length ? 'bad' : '' }, h('td', {}, x.line), h('td', {}, x.summary.name), h('td', {}, fmtPhone(x.summary.phone)), h('td', {}, x.summary.address),
          h('td', {}, x.summary.planned_date || '-'), h('td', {}, x.summary.dong || '-'), h('td', {}, x.summary.channel), h('td', {}, x.summary.items),
          h('td', {}, ...x.errors.map((e) => chip(e, 'bad')), x.duplicates.length ? chip('중복 후보: ' + x.duplicates.join(', '), 'warn') : null, ...x.warnings.map((w) => h('div', { class: 'small muted' }, w)))))))));
  };
  file.addEventListener('change', () => { previewed = false; out.replaceChildren(); });
  return modal({ title: '접수 일괄 등록 (CSV)', ok: '등록', wide: true,
    body: [h('p', { class: 'muted' }, '서식에 맞춘 CSV(엑셀에서 "CSV UTF-8"로 저장)를 넣습니다. 미리보기에서 오류 행은 등록되지 않습니다. 같은 연락처·주소는 중복 후보로 표시하며 자동으로 합치지 않습니다.'),
      h('div', { class: 'row' }, btn('서식 내려받기', wrap(() => download('requests/import-template', '동구-접수-일괄등록-서식.csv')), 'small'), file, btn('미리보기', wrap(prev), 'small')),
      h('label', { class: 'row' }, skip, '중복 후보 행은 건너뛰기'), out],
    onOk: async () => {
      if (!previewed) { await prev(); throw new Error('미리보기를 확인한 뒤 [등록]을 누르세요.'); }
      const r = await api(`requests/import?preview=0&skip_dup=${skip.checked ? 1 : 0}`, { method: 'POST', raw: await raw() });
      toast(`${r.created}건 등록했습니다` + (r.skipped_lines.length ? ` (건너뜀: ${r.skipped_lines.join(', ')}행)` : ''));
      return r.created || true;
    } });
}

// ------------------------------------------------------------ 접수 등록·수정

export {
  importDlg, screenRequests,
};
