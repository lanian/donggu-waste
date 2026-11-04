// PC 화면: 통계·보고서
import { won } from '/web/fees.js';
import { api, boot, btn, download, grid, h, pagehead, post, render, searchbox, toast, wrap } from '../core.js';

const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attrs = {}, ...kids) => { const el = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); kids.forEach((c) => el.append(c)); return el; };
function dayChart(days) {
  // 날짜별 수거 예정 대비 완료·재방문(일부 수거·미수거) 막대. 남은 칸은 예정 중 아직 완료 아님.
  const W = Math.max(560, days.length * 28), H = 200, padL = 34, padB = 24, padT = 10;
  const max = Math.max(1, ...days.map((d) => d.planned));
  const step = (W - padL - 8) / Math.max(1, days.length), bw = Math.min(18, step - 4);
  const y = (v) => padT + (H - padT - padB) * (1 - v / max);
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': '날짜별 수거 예정·완료·재방문' });
  for (const t of [0, Math.ceil(max / 2), max]) {
    svg.append(sv('line', { x1: padL, x2: W - 4, y1: y(t), y2: y(t), class: 'grid' }), sv('text', { x: padL - 6, y: y(t) + 4, class: 'tick', 'text-anchor': 'end' }, document.createTextNode(t)));
  }
  const tip = h('div', { class: 'tip', hidden: true });
  days.forEach((d, i) => {
    const x = padL + i * step + (step - bw) / 2;
    const warn = d.partial + d.not_collected;
    const g = sv('g', { class: 'bar', tabindex: 0 });
    g.append(sv('rect', { x: padL + i * step, y: padT, width: step, height: H - padT - padB, class: 'hit' }));
    g.append(sv('rect', { x, y: y(d.planned), width: bw, height: Math.max(0, y(0) - y(d.planned)), rx: 3, class: 'b-track' }));
    if (d.done) g.append(sv('rect', { x, y: y(d.done), width: bw, height: y(0) - y(d.done), rx: 3, class: 'b-ok' }));
    if (warn) g.append(sv('rect', { x, y: y(d.done + warn), width: bw, height: Math.max(0, y(d.done) - y(d.done + warn) - 2), rx: 3, class: 'b-warn' }));
    if (days.length <= 16 || i % Math.ceil(days.length / 16) === 0) svg.append(sv('text', { x: x + bw / 2, y: H - 6, class: 'tick', 'text-anchor': 'middle' }, document.createTextNode(d.date.slice(5))));
    const show = () => { tip.hidden = false; tip.textContent = `${d.date} · 예정 ${d.planned} · 완료 ${d.done} · 재방문 ${warn} · 취소 ${d.cancelled}`; tip.style.left = `${Math.min(x, W - 260)}px`; };
    g.addEventListener('mouseenter', show); g.addEventListener('focus', show);
    g.addEventListener('mouseleave', () => { tip.hidden = true; }); g.addEventListener('blur', () => { tip.hidden = true; });
    svg.append(g);
  });
  return h('div', { class: 'chartbox' }, h('div', { class: 'legend' }, h('span', { class: 'lg ok' }, '완료'), h('span', { class: 'lg warn' }, '재방문(일부 수거·미수거)'), h('span', { class: 'lg track' }, '예정 중 남음')), h('div', { class: 'chartscroll' }, svg, tip));
}
async function screenStats() {
  const s = (screenStats.s ||= (() => { const t = boot.today; return { start: t.slice(0, 8) + '01', end: t, basis: 'planned', itemPage: 1 }; })());
  const start = h('input', { type: 'date', value: s.start, 'aria-label': '기간 시작' }), end = h('input', { type: 'date', value: s.end, 'aria-label': '기간 끝' });
  const basis = h('select', { 'aria-label': '집계 기준' }, ...[['planned', '수거 예정일'], ['received', '접수일'], ['completed', '완료일']].map(([v, t]) => h('option', { value: v, selected: v === s.basis }, t)));
  const preset = (label, fn) => btn(label, () => { const [a, b] = fn(new Date(boot.today + 'T00:00:00')); s.start = a; s.end = b; render(true); }, 'small');
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const shift = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const presets = h('div', { class: 'seg-btns' },
    preset('오늘', (t) => [iso(t), iso(t)]), preset('최근 7일', (t) => [iso(shift(t, -6)), iso(t)]),
    preset('이번 달', (t) => [boot.today.slice(0, 8) + '01', iso(t)]),
    preset('지난달', (t) => [iso(new Date(t.getFullYear(), t.getMonth() - 1, 1)), iso(new Date(t.getFullYear(), t.getMonth(), 0))]),
    preset('올해', (t) => [`${t.getFullYear()}-01-01`, iso(t)]));
  [start, end, basis].forEach((x) => x.addEventListener('change', () => { s.start = start.value; s.end = end.value; s.basis = basis.value; s.itemPage = 1; render(true); }));
  const d = await api(`stats?start=${s.start}&end=${s.end}&basis=${s.basis}`);
  const T = d.totals, bs = T.by_status;
  const active = T.requests - (bs.CANCELLED || 0);
  const doneRate = active ? Math.round(((bs.DONE || 0) / active) * 100) : 0;
  const kpi = (k, v, sub, cls = '') => h('div', { class: 'kpi ' + cls }, h('span', { class: 'k' }, k), h('b', { class: 'v' }, v), h('span', { class: 's' }, sub || ' '));
  const q = () => `start=${s.start}&end=${s.end}&basis=${s.basis}`;
  const csv = (kind, label) => wrap(async () => { await download(`stats.csv?${q()}&kind=${kind}`, `동구-${label}-${s.start}_${s.end}.csv`); toast('내려받았습니다 (내보낸 기록이 이력에 남습니다)'); });
  const csvBtn = (kind, label) => btn('CSV', csv(kind, label), 'small', { 'aria-label': `${label} CSV` });
  const card = (title, kind, label, body) => h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, title), kind ? csvBtn(kind, label) : null), body);
  // 많이 나온 품목: 전체를 쪽으로 나눠 보기
  const IP = 20, ipages = Math.max(1, Math.ceil(d.items.length / IP));
  s.itemPage = Math.min(s.itemPage, ipages);
  const itemBody = h('div');
  const drawItems = () => {
    const part = d.items.slice((s.itemPage - 1) * IP, s.itemPage * IP);
    itemBody.replaceChildren(grid(['순위', '품목', '규격', '확인 수량', '수거 수량'], part.map((x, i) => [(s.itemPage - 1) * IP + i + 1, x.name, x.spec, `${x.requested}${x.unit_label}`, `${x.collected}${x.unit_label}`]), { num: [0, 3, 4], empty: '없음' }),
      ipages > 1 ? h('div', { class: 'pager' }, btn('‹ 이전', () => { s.itemPage -= 1; drawItems(); }, 'small', { disabled: s.itemPage <= 1 }), h('span', { class: 'pg' }, `${s.itemPage} / ${ipages}쪽 · ${d.items.length}종`), btn('다음 ›', () => { s.itemPage += 1; drawItems(); }, 'small', { disabled: s.itemPage >= ipages })) : null);
  };
  drawItems();
  const printMonthly = wrap(async () => { const r = await post('stats/print', { start: s.start, end: s.end, basis: s.basis }); window.open('/print/doc/' + r.token, 'dg-print'); });
  return h('div', {}, pagehead('통계·보고서', btn('월보 출력', printMonthly, 'small'), btn('접수별 CSV', csv('requests', '접수별'), 'primary small')),
    searchbox([['기간', [start, '~', end]], ['집계 기준', basis], ['빠른 기간', presets, 'wide2']]),
    h('div', { class: 'kpis' },
      kpi('접수', `${T.received}건`, `${d.basis_label} 기준 ${T.requests}건 · 취소 제외 ${active}건`),
      kpi('수거 완료', `${bs.DONE || 0}건`, `완료율 ${doneRate}%`, 'ok'),
      kpi('재방문·보류', `${(bs.PARTIAL || 0) + (bs.NOT_COLLECTED || 0) + (bs.ON_HOLD || 0)}건`, bs.CANCELLED ? `취소 ${bs.CANCELLED}건 별도` : ' ', (bs.PARTIAL || bs.NOT_COLLECTED) ? 'warn' : ''),
      kpi('부과 금액', won(T.levied), `계산 ${won(T.calculated)} · 감면 ${won(T.reduction)} (${T.reduction_count}건) · 납부 ${won(T.paid)}`),
      kpi('확인 필요', `${T.amount_pending + T.unpaid}건`, `금액 대기 ${T.amount_pending} · 미납·부족 ${T.unpaid}`, (T.amount_pending + T.unpaid) ? 'warn' : '')),
    h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, `날짜별 수거 (${d.basis_label} 기준)`), h('span', { class: 'row' },
        h('span', { class: 'muted small' }, `하차 ${T.unload_m3}m³ · ${T.unload_kg.toLocaleString()}kg · 현장 추가 품목 ${T.field_added_lines}건${T.lead_days_avg != null ? ` · 접수→수거 평균 ${T.lead_days_avg}일` : ''}`), btn('날짜별 CSV', csv('days', '날짜별'), 'small'))),
      d.days.length ? dayChart(d.days) : h('p', { class: 'empty' }, '이 기간에 자료가 없습니다. 기간이나 집계 기준을 바꿔 보세요.'),
      d.days.length ? h('details', { class: 'tableview' }, h('summary', {}, '표로 보기'),
        grid(['날짜', '접수', '예정', '완료', '일부', '미수거', '취소', '계산 금액', '부과 금액', '납부 기록', '하차'], d.days.map((x) => [x.date, x.received, x.planned, x.done, x.partial, x.not_collected, x.cancelled, won(x.calculated), won(x.levied), won(x.paid), x.unload_m3 ? `${x.unload_m3}m³` : '']), { num: [1, 2, 3, 4, 5, 6, 7, 8, 9] })) : null),
    card('월별 추이', 'months', '월별', grid(['월', '건수', '완료', '재방문·보류', '취소', '계산 금액', '감면액', '부과 금액', '납부 기록'], d.months.map((m) => [m.month, m.count, m.done, m.revisit, m.cancelled, won(m.calculated), won(m.reduction), won(m.levied), won(m.paid)]), { num: [1, 2, 3, 4, 5, 6, 7, 8], empty: '없음' })),
    h('div', { class: 'grid2' },
      card('행정동별', 'dongs', '동별', grid(['행정동', '건수', '완료', '재방문·보류', '취소', '완료율', '부과 금액'], d.dongs.map((x) => { const act = x.planned - x.cancelled; return [x.dong || '(동 미지정)', x.planned, x.done, x.revisit, x.cancelled, `${act ? Math.round((x.done / act) * 100) : 0}%`, won(x.levied)]; }), { num: [1, 2, 3, 4, 5, 6], empty: '없음' })),
      card('품목 분류별', 'categories', '분류별', grid(['분류', '품목 행', '확인 수량', '수거 수량', '계산 금액'], d.categories.map((x) => [x.category, x.lines, x.requested, x.collected, won(x.amount)]), { num: [1, 2, 3, 4], empty: '없음' }))),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '세입 (납부 방법별, 납부일 기준)'), h('a', { href: '#/revenue', class: 'small' }, '세입·대사 →')),
        grid(['납부 방법', '납부 건수', '납부액', '환불액', '순수입'], d.revenue.methods.map((x) => [x.method, x.pay_n, won(x.pay), won(x.refund), won(x.net)]), { num: [1, 2, 3, 4], empty: '이 기간 납부 기록 없음' })),
      card('감면', 'reductions', '감면', grid(['감면 사유', '감면율', '건수', '감면액'], d.reductions.map((x) => [x.label, `${x.rate}%`, x.count, won(x.amount)]), { num: [1, 2, 3], empty: '감면 없음' }))),
    h('div', { class: 'grid2' },
      card('많이 나온 품목 (전체)', 'items', '품목별', itemBody),
      h('div', {}, card('접수 경로별', 'channels', '경로별', grid(['접수 경로', '건수', '완료', '취소'], d.channels.map((x) => [x.channel, x.planned, x.done, x.cancelled]), { num: [1, 2, 3], empty: '없음' })),
        card('팀별', null, null, grid(['팀', '배정', '완료', '재방문·보류', '완료율'], d.crews.map((c) => [c.crew, c.assigned, c.done, c.partial_or_not, `${c.assigned ? Math.round((c.done / c.assigned) * 100) : 0}%`]), { num: [1, 2, 3, 4], empty: '배정 기록 없음' })))),
    h('p', { class: 'muted' }, 'CSV는 엑셀에서 바로 열립니다. 이름·연락처·주소·메모는 넣지 않고, 상태는 한글 이름과 코드를 함께 담습니다. 월보 제목은 설정 → 기본에서 바꿉니다. 내보낸 기록은 변경 이력에 남습니다.'));
}

// ------------------------------------------------------------ 세입 일계표·월계표 + 대사 (세외수입 담당자용)

export {
  SVGNS, dayChart, screenStats, sv,
};
