// PC 화면: 세입 일계표·월계표·대사
import { won } from '/web/fees.js';
import { RECON, api, boot, btn, canWork, cell, chip, confirmDlg, download, grid, h, kst, modal, pagehead, render, routeEpoch, searchbox, sessionOwner, toast, wrap } from '../core.js';

const option = (value, label, selected = false) => h('option', { value, selected }, label);
const money = (amount) => amount == null ? '—' : won(amount);
const verdict = (kind) => chip(RECON[kind] || kind, kind === 'MATCH' ? 'ok' : kind === 'ALREADY' ? 'neutral' : 'warn');

function reconReview(canEdit, owner, epoch) {
  const title = h('h2', { tabindex: '-1' }, '대사 불일치 검토');
  const results = h('div', {}, h('p', { class: 'muted', role: 'status' }, '불일치 목록을 불러오는 중…'));
  const status = h('select', {}, option('pending', '미해결·검토 중'), option('RESOLVED', '해결 완료'), option('all', '전체 상태'));
  const filter = h('div', { class: 'row' });
  const section = h('section', { class: 'card', 'aria-label': '대사 불일치 검토' }, title,
    h('div', { class: 'row' }, h('label', { class: 'f' }, '불일치 상태', status), filter), results);
  const current = () => section.isConnected && routeEpoch === epoch && sessionOwner() === owner;
  let importId = '', page = 1, seq = 0;

  function selectImport(id, at) {
    importId = id;
    status.value = id ? 'all' : 'pending';
    page = 1;
    filter.replaceChildren();
    if (id) filter.append(h('span', {}, `반입 ${kst(at)}`), btn('모든 반입 보기', () => selectImport(''), 'small'));
    if (id) title.focus();
    load();
  }

  async function open(id) {
    const path = `recon-exceptions/${encodeURIComponent(id)}`;
    const { case: r, history, statuses, bases } = await api(path, { _prefsOwner: owner });
    if (!current()) return;
    const state = h('select', {}, ...Object.entries(statuses).map(([k, label]) => option(k, label, k === r.status)));
    const basis = h('select', {}, option('', '근거 선택'), ...Object.entries(bases).map(([k, label]) => option(k, label)));
    let stale = false;
    const saved = await modal({ title: '대사 불일치 처리 이력', wide: true, ok: canEdit ? '저장' : '닫기', cancel: canEdit ? '닫기' : null,
      body: [h('p', {}, `반입 ${kst(r.import_at)} · 파일 ${r.file_line}행 · `, verdict(r.kind)),
        h('p', {}, `부과번호 ${r.levy_no || '—'} · 납부일 ${r.paid_on || '—'} · 파일 금액 ${money(r.amount)}`),
        h('p', {}, `반입 당시 부과액 ${money(r.levied)} · 기납부액 ${money(r.paid)}`,
          r.receipt_no ? [' · 접수 ', h('a', { href: `#/request/${encodeURIComponent(r.request_id)}` }, r.receipt_no)] : null),
        h('p', {}, `현재 ${statuses[r.status]}`, r.basis ? ` · ${bases[r.basis]}` : ''),
        grid(['처리 시각', '담당자', '상태 변경', '처리 근거'], history.map((x) => [kst(x.at), x.actor, `${statuses[x.from]} → ${statuses[x.to]}`, bases[x.basis]]),
          { empty: '아직 등록된 처리 이력이 없습니다.' }),
        canEdit ? h('div', { class: 'row' }, h('label', { class: 'f' }, '처리 상태', state), h('label', { class: 'f' }, '처리 근거', basis)) : null],
      onOk: canEdit ? async () => {
        if (!current()) throw new Error('세입·대사 화면을 다시 열어 주세요.');
        if (!basis.value) throw new Error('처리 근거를 골라 주세요.');
        try {
          return await api(path, { method: 'POST', body: { status: state.value, basis: basis.value, version: r.version }, _prefsOwner: owner });
        } catch (error) {
          stale = error.status === 409;
          throw error;
        }
      } : null });
    if (!current()) return;
    if (canEdit && saved) toast('불일치 검토를 저장했습니다');
    if ((canEdit && saved) || stale) {
      title.focus();
      load();
    }
  }

  async function load() {
    if (!current()) return;
    const mine = ++seq;
    const qs = new URLSearchParams({ import_id: importId, status: status.value, page });
    results.replaceChildren(h('p', { class: 'muted', role: 'status' }, '불일치 목록을 불러오는 중…'));
    try {
      const data = await api(`recon-exceptions?${qs}`, { _prefsOwner: owner, timeoutMs: 10_000 });
      if (!current() || mine !== seq) return;
      page = data.page;
      results.replaceChildren(h('p', { class: 'muted small' }, `조회 ${data.total}건`),
        grid(['반입 시각', '파일 행', '판정', '부과번호', '접수번호', '파일 금액', '납부일', '상태', '처리 이력'], data.rows.map((r) => [
          kst(r.import_at), r.file_line, verdict(r.kind), r.levy_no || '—', r.receipt_no || '—', money(r.amount), r.paid_on || '—',
          chip(data.statuses[r.status], r.status === 'RESOLVED' ? 'ok' : 'warn'),
          btn('열기', wrap(() => open(r.id)), 'small', { 'aria-label': `${r.file_line}행 처리 이력 열기` }),
        ]), { num: [1, 5], empty: '조건에 맞는 불일치 건이 없습니다.' }));
      if (data.pages > 1) results.append(h('div', { class: 'row' },
        btn('이전', () => { page--; load(); }, 'small', { disabled: page === 1 }),
        h('span', { class: 'small' }, `${page} / ${data.pages}쪽`),
        btn('다음', () => { page++; load(); }, 'small', { disabled: page === data.pages })));
    } catch (error) {
      if (current() && mine === seq) results.replaceChildren(h('p', { role: 'alert' }, error.message), btn('다시 조회', load, 'small'));
    }
  }

  status.addEventListener('change', () => { page = 1; load(); });
  setTimeout(load, 0);
  return { section, selectImport };
}

function reconImport(hist, canEdit, selectImport, owner, epoch) {
  const file = h('input', { type: 'file', accept: '.csv,.txt', 'aria-label': '대사 CSV 파일' });
  const out = h('div'), msg = h('p', { class: 'msg', role: 'status' });
  let seq = 0;
  file.addEventListener('change', () => { seq++; msg.textContent = ''; out.replaceChildren(); });
  const preview = wrap(async () => {
    const selected = file.files[0];
    if (!selected) throw new Error('파일을 고르세요.');
    const mine = ++seq;
    const current = () => file.isConnected && mine === seq && file.files[0] === selected && routeEpoch === epoch && sessionOwner() === owner;
    const raw = new Uint8Array(await selected.arrayBuffer());
    if (!current()) return;
    out.replaceChildren();
    const r = await api('recon?preview=1', { method: 'POST', raw, _prefsOwner: owner });
    if (!current()) return;
    const matched = r.counts.MATCH || 0, exceptions = r.exception_count;
    const prior = r.already_imported;
    const apply = wrap(async () => {
      if (!(await confirmDlg('대사 반영', `일치 ${matched}건을 납부로 기록하고 불일치 ${exceptions}건을 검토 목록에 등록합니다.`, { ok: '기록' }))) return;
      if (!current()) return;
      const qs = new URLSearchParams({ preview: '0', preview_token: r.preview_token });
      let x;
      try {
        x = await api(`recon?${qs}`, { method: 'POST', raw, _prefsOwner: owner });
      } catch (error) {
        if (error.status === 409 && current()) out.replaceChildren();
        throw error;
      }
      if (!current()) return;
      toast([
        x.recorded && `${x.recorded}건을 납부로 기록했습니다`,
        x.exception_count && `불일치 ${x.exception_count}건을 검토 목록에 등록했습니다`,
      ].filter(Boolean).join(' · '));
      file.value = '';
      render();
    }, msg);
    out.replaceChildren(h('div', { class: 'notice info' }, `${r.total}행 · `, ...Object.entries(r.counts).map(([k, n]) => chip(`${RECON[k] || k} ${n}`, k === 'MATCH' ? 'ok' : k === 'ALREADY' ? 'neutral' : 'warn')),
        prior ? h('strong', {}, ` 이미 반영한 파일입니다(${kst(prior.at)}).`) : null),
      grid(['행', '부과번호', 'PC 접수번호', '금액', '납부일', '영수번호', '부과·납부', '판정', '설명'], r.rows.map((x) => [x.line, x.levy_no, x.receipt_no || x.input_receipt_no || '', x.amount != null ? won(x.amount) : '', x.paid_on, x.ref || '',
        x.levied != null ? `${won(x.levied)} / ${won(x.paid)}` : '', verdict(x.status), x.note || '']), { cls: 'recon', num: [0, 3], sort: { key: 'recon-preview', cols: ['num', 'text', 'text', 'num', 'date', 'text', null, 'text', 'text'] } }),
      h('div', { class: 'row' },
        prior?.exception_count ? btn('이 반입의 불일치 보기', () => selectImport(prior.id, prior.at), 'small') : null,
        btn(matched || !exceptions ? `일치 ${matched}건 납부로 기록` : `불일치 ${exceptions}건 검토 목록에 등록`, apply, 'primary',
          { disabled: (!matched && !exceptions) || !!prior }),
        exceptions ? h('span', { class: 'muted small' }, `불일치 ${exceptions}건은 대사 불일치 검토에서 이어서 처리합니다.`) : null));
  }, msg);
  return h('div', { class: 'card' }, h('h2', {}, '대사 (세외수입 납부 자료 맞추기)'),
    canEdit ? h('p', { class: 'muted' }, '납부 자료 CSV에 부과번호 또는 PC 접수번호와 금액, 납부일을 넣고 미리 보세요. 두 번호를 모두 입력하면 같은 접수의 번호여야 합니다. 같은 파일은 한 번만 반영됩니다.') : null,
    canEdit ? h('div', { class: 'row' }, btn('서식 내려받기', wrap(() => download('recon-template', '동구-대사-서식.csv')), 'small'), file, btn('미리보기', preview, 'small primary')) : null,
    msg, out,
    hist?.length ? h('details', { class: 'tableview' }, h('summary', {}, `반영 기록 ${hist.length}건`), grid(['시각', '작업자', '행', '납부 기록', '판정 집계', '불일치 검토'], hist.map((x) => [
      cell(kst(x.at), x.at), x.actor, x.rows, x.matched, Object.entries(x.counts).map(([k, n]) => `${RECON[k] || k} ${n}건`).join(' · '),
      x.exception_count ? btn(`불일치 ${x.exception_count}건 보기`, () => selectImport(x.id, x.at), 'small') : x.exception_count === 0 ? '0건' : '집계만 보관된 반입',
    ]), { num: [2, 3], sort: { key: 'recon-history', cols: ['date', 'text', 'num', 'num', null, null] } })) : h('p', { class: 'muted small', role: hist === null ? 'alert' : null },
      hist === null ? '반영 기록을 불러오지 못했습니다.' : '반영 기록이 없습니다.'));
}

async function screenRevenue() {
  const s = (screenRevenue.s ||= { start: boot.today.slice(0, 8) + '01', end: boot.today, unit: 'day' });
  const start = h('input', { type: 'date', value: s.start, 'aria-label': '기간 시작' }), end = h('input', { type: 'date', value: s.end, 'aria-label': '기간 끝' });
  const unit = h('select', { 'aria-label': '집계 단위' }, h('option', { value: 'day', selected: s.unit === 'day' }, '일계표(날짜별)'), h('option', { value: 'month', selected: s.unit === 'month' }, '월계표(월별)'));
  [start, end, unit].forEach((x) => x.addEventListener('change', () => { Object.assign(s, { start: start.value, end: end.value, unit: unit.value }); render(true); }));
  const d = await api(`revenue?start=${s.start}&end=${s.end}&unit=${s.unit}`);
  const qs = `start=${s.start}&end=${s.end}&unit=${s.unit}`;
  const dl = (kind, label) => btn(label, wrap(async () => { await download(`revenue.csv?${qs}&kind=${kind}`, `동구-세입-${label}-${s.start}_${s.end}.csv`); toast('내려받았습니다'); }), 'small');
  const per = s.unit === 'month' ? '월' : '납부일';
  const T = d.totals;
  const owner = sessionOwner(), epoch = routeEpoch, canEdit = canWork();
  const hist = await api('recon-history', { _prefsOwner: owner }).catch(() => null);
  const review = reconReview(canEdit, owner, epoch);
  return h('div', {}, pagehead('세입·대사', dl('method', '방법별'), dl('dong', '동별'), dl('detail', '명세')),
    h('div', { class: 'notice warn' }, '세외수입시스템 연계 형식은 기관 확인 필요 — 이 화면의 CSV(일계표·월계표·명세)와 대사 서식은 담당자 확인용 기본 형식입니다. 실제 연계 파일 형식이 정해지면 맞춰야 합니다.'),
    searchbox([['기간', [start, '~', end]], ['집계 단위', unit]]),
    h('div', { class: 'kpis' }, ...[['납부', won(T.pay), `${T.pay_n}건`], ['환불', won(T.refund), `${T.refund_n}건`], ['순수입', won(T.net), `${d.count}건 기록`]].map(([k, v, sub]) => h('div', { class: 'kpi' }, h('span', { class: 'k' }, k), h('b', { class: 'v' }, v), h('span', { class: 's' }, sub)))),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('h2', {}, `납부 방법별 (${s.unit === 'month' ? '월계' : '일계'})`), grid([per, '납부 방법', '납부 건수', '납부액', '환불 건수', '환불액', '순수입'],
        d.by_method.map((x) => [x.period, x.method, x.pay_n, won(x.pay), x.refund_n, won(x.refund), won(x.net)]), { num: [2, 3, 4, 5, 6], empty: '이 기간 납부 기록이 없습니다.',
          sort: { key: 'revenue-method', cols: ['date', 'text', 'num', 'num', 'num', 'num', 'num'] } })),
      h('div', { class: 'card' }, h('h2', {}, `행정동별 (${s.unit === 'month' ? '월계' : '일계'})`), grid([per, '행정동', '납부 건수', '납부액', '환불액', '순수입'],
        d.by_dong.map((x) => [x.period, x.dong || '(동 미지정)', x.pay_n, won(x.pay), won(x.refund), won(x.net)]), { num: [2, 3, 4, 5], empty: '이 기간 납부 기록이 없습니다.',
          sort: { key: 'revenue-dong', cols: ['date', 'text', 'num', 'num', 'num', 'num'] } }))),
    reconImport(hist, canEdit, review.selectImport, owner, epoch),
    review.section);
}

// ------------------------------------------------------------ 환불 목록 (신청 → 결재 → 지급 기록)

export {
  screenRevenue,
};
