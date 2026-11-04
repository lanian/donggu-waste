// PC 화면: 세입 일계표·월계표·대사
import { won } from '/web/fees.js';
import { RECON, api, boot, btn, canWork, chip, confirmDlg, download, grid, h, kst, pagehead, render, searchbox, toast, wrap } from '../core.js';

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
  // 대사: CSV(부과번호·금액·납부일) → 미리보기(일치·불일치) → 확인하면 일치 건만 '대사' 출처 납부로 기록
  const file = h('input', { type: 'file', accept: '.csv,.txt', 'aria-label': '대사 CSV 파일' });
  const out = h('div'), msg = h('p', { class: 'msg' });
  const raw = async () => { const f0 = file.files[0]; if (!f0) throw new Error('파일을 고르세요.'); return new Uint8Array(await f0.arrayBuffer()); };
  const canRecon = canWork();
  const preview = wrap(async () => {
    const r = await api('recon?preview=1', { method: 'POST', raw: await raw() });
    const bad = r.rows.filter((x) => x.status !== 'MATCH');
    out.replaceChildren(h('div', { class: 'notice info' }, `${r.total}행 · `, ...Object.entries(r.counts).map(([k, n]) => chip(`${RECON[k] || k} ${n}`, k === 'MATCH' ? 'ok' : k === 'ALREADY' ? 'neutral' : 'warn')),
        r.already_imported ? h('strong', {}, ` 이미 반영한 파일입니다(${kst(r.already_imported.at)}).`) : null),
      grid(['행', '부과번호', '접수번호', '금액', '납부일', '영수번호', '부과·납부', '판정', '설명'], r.rows.map((x) => [x.line, x.levy_no, x.receipt_no || '', x.amount != null ? won(x.amount) : '', x.paid_on, x.ref || '',
        x.levied != null ? `${won(x.levied)} / ${won(x.paid)}` : '', chip(RECON[x.status] || x.status, x.status === 'MATCH' ? 'ok' : x.status === 'ALREADY' ? 'neutral' : 'warn'), x.note || '']), { cls: 'recon', num: [0, 3] }),
      h('div', { class: 'row' }, btn(`일치 ${r.counts.MATCH || 0}건 납부로 기록`, wrap(async () => {
        if (!r.counts.MATCH) throw new Error('일치하는 행이 없습니다.');
        if (!(await confirmDlg('대사 반영', `일치 ${r.counts.MATCH}건을 납부(출처 '대사')로 기록합니다. 불일치 ${bad.length}건은 기록하지 않으니 따로 확인하세요.`, { ok: '기록' }))) return;
        const x = await api('recon?preview=0', { method: 'POST', raw: await raw() }); toast(`${x.recorded}건을 납부로 기록했습니다`); file.value = ''; render();
      }, msg), 'primary', { disabled: !r.counts.MATCH || !!r.already_imported }), h('span', { class: 'muted small' }, '불일치(부과번호 없음·금액 다름·중복)는 자동으로 고치지 않습니다. 접수 상세에서 확인해 수기로 기록하세요.')));
  }, msg);
  const hist = canRecon ? await api('recon-history').catch(() => []) : [];
  return h('div', {}, pagehead('세입·대사', dl('method', '방법별'), dl('dong', '동별'), dl('detail', '명세')),
    h('div', { class: 'notice warn' }, '세외수입시스템 연계 형식은 기관 확인 필요 — 이 화면의 CSV(일계표·월계표·명세)와 대사 서식은 담당자 확인용 기본 형식입니다. 실제 연계 파일 형식이 정해지면 맞춰야 합니다.'),
    searchbox([['기간', [start, '~', end]], ['집계 단위', unit]]),
    h('div', { class: 'kpis' }, ...[['납부', won(T.pay), `${T.pay_n}건`], ['환불', won(T.refund), `${T.refund_n}건`], ['순수입', won(T.net), `${d.count}건 기록`]].map(([k, v, sub]) => h('div', { class: 'kpi' }, h('span', { class: 'k' }, k), h('b', { class: 'v' }, v), h('span', { class: 's' }, sub)))),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('h2', {}, `납부 방법별 (${s.unit === 'month' ? '월계' : '일계'})`), grid([per, '납부 방법', '납부 건수', '납부액', '환불 건수', '환불액', '순수입'],
        d.by_method.map((x) => [x.period, x.method, x.pay_n, won(x.pay), x.refund_n, won(x.refund), won(x.net)]), { num: [2, 3, 4, 5, 6], empty: '이 기간 납부 기록이 없습니다.' })),
      h('div', { class: 'card' }, h('h2', {}, `행정동별 (${s.unit === 'month' ? '월계' : '일계'})`), grid([per, '행정동', '납부 건수', '납부액', '환불액', '순수입'],
        d.by_dong.map((x) => [x.period, x.dong || '(동 미지정)', x.pay_n, won(x.pay), won(x.refund), won(x.net)]), { num: [2, 3, 4, 5], empty: '이 기간 납부 기록이 없습니다.' }))),
    canRecon ? h('div', { class: 'card' }, h('h2', {}, '대사 (세외수입 납부 자료 맞추기)'),
      h('p', { class: 'muted' }, '세외수입 담당자에게 받은 납부 자료를 CSV(부과번호, 금액, 납부일, 영수번호 선택)로 넣으면 PC의 부과 금액과 맞춰 봅니다. 미리보기에서 일치·불일치를 확인한 뒤 [기록]을 눌러야 반영됩니다. 같은 파일은 한 번만 반영됩니다.'),
      h('div', { class: 'row' }, btn('서식 내려받기', wrap(() => download('recon-template', '동구-대사-서식.csv')), 'small'), file, btn('미리보기', preview, 'small primary')), msg, out,
      hist.length ? h('details', { class: 'tableview' }, h('summary', {}, `반영 기록 ${hist.length}건`), grid(['시각', '작업자', '행', '일치·기록'], hist.map((x) => [kst(x.at), x.actor, x.rows, x.matched]), { num: [2, 3] })) : null) : null);
}

// ------------------------------------------------------------ 환불 목록 (신청 → 결재 → 지급 기록)

export {
  screenRevenue,
};
