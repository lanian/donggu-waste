// PC 화면: 시범 운영 비교
import { api, boot, btn, chip, download, grid, h, pagehead, post, render, toast, wrap } from '../core.js';

async function screenPilot() {
  const canEdit = ['ADMIN', 'INTAKE'].includes(boot.me.role);
  const d = await api('pilot');
  const cfg = d.settings, R = d.report;
  const inp = (v, attrs = {}) => h('input', { value: v ?? '', ...attrs });
  const F = { start: inp(cfg.start, { type: 'date', 'aria-label': '시범 운영 시작' }), end: inp(cfg.end, { type: 'date', 'aria-label': '시범 운영 끝' }),
    crew: h('select', { 'aria-label': '시범 운영 팀' }, h('option', { value: '' }, '전체 팀'), ...boot.crews.map((c) => h('option', { value: c.id, selected: c.id === cfg.crew_id }, c.name))),
    base_period: inp(cfg.base_period, { placeholder: '예: 2026년 9월(종이 대장)', 'aria-label': '기존 방식 기간' }),
    note: h('textarea', { rows: 2, 'aria-label': '메모' }, cfg.note || '') };
  const BK = d.base_keys;  // 기존 방식에서 적는 수치(서버가 정한 이름)
  const B = Object.fromEntries(Object.keys(BK).map((k) => [k, inp(cfg.base[k], { type: 'number', min: 0, step: 'any', style: 'width:110px', 'aria-label': BK[k] })]));
  if (!canEdit) [...Object.values(F), ...Object.values(B)].forEach((x) => { x.disabled = true; });
  const save = wrap(async () => {
    await post('pilot', { start: F.start.value, end: F.end.value, crew_id: F.crew.value, base_period: F.base_period.value, note: F.note.value,
      base: Object.fromEntries(Object.entries(B).map(([k, x]) => [k, x.value])) });
    toast('저장했습니다'); render(true);
  });
  const fmt = (v, unit) => (v == null ? h('span', { class: 'muted' }, '-') : `${Number(v).toLocaleString('ko-KR')}${unit === '건' ? '건' : unit === '%' ? '%' : unit === '일' ? '일' : unit === '분' ? '분' : ''}`);
  const verdict = (v) => (v ? chip(v, v === '좋아짐' ? 'ok' : v === '나빠짐' ? 'bad' : '') : h('span', { class: 'muted' }, '-'));
  const table = R ? grid(['지표', '단위', '기존 방식', '시범 운영', '차이', '판단', '근거'],
    R.rows.map((x) => [x.label, x.unit, fmt(x.base, x.unit), fmt(x.pilot, x.unit), x.diff == null ? h('span', { class: 'muted' }, '-') : (x.diff > 0 ? '+' : '') + x.diff,
      verdict(x.verdict), x.count != null ? `${x.count}건` : '']), { num: [2, 3, 4, 6], cls: 'pilot' }) : h('p', { class: 'empty' }, '시범 운영 기간을 정하고 저장하면 비교표가 나옵니다.');
  return h('div', {}, pagehead('시범 운영 비교', R ? btn('CSV', wrap(async () => { await download(`pilot.csv?start=${R.start}&end=${R.end}&crew=${R.crew_id}`, `동구-시범운영-${R.start}_${R.end}.csv`); toast('내려받았습니다'); }), 'small') : null),
    h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, R ? `${R.start} ~ ${R.end} (${R.days}일) · ${R.crew || '전체 팀'}` : '비교표'),
      h('span', { class: 'muted small' }, '건수는 기간·물량이 달라도 비교되게 100건당. 이름·연락처·주소는 쓰지 않음')),
      table,
      h('p', { class: 'muted small' }, '누락 = 마감 때 미배정·결과 없음으로 넘어간 건 + 예정일이 지났는데 결과가 없는 건. 접수·마감 시간은 이 PC 화면을 연 때부터 저장까지(5초~2시간만). 당일 결과 회수 = 수거 결과를 휴대폰에 남긴 날 PC에 들어온 비율.')),
    h('div', { class: 'card formtable' }, h('h2', {}, '시범 운영 기간과 기존 방식 수치'),
      h('div', { class: 'grid2' }, h('label', { class: 'f' }, '시범 운영 시작', F.start), h('label', { class: 'f' }, '끝', F.end)),
      h('label', { class: 'f' }, '팀', F.crew),
      h('label', { class: 'f' }, '기존 방식 기간', F.base_period),
      h('p', { class: 'muted small' }, '기존 방식(종이 대장·전화 등)으로 같은 길이의 기간에 실제로 센 값을 적습니다. 모르는 칸은 비워 두면 비교에서 빠집니다.'),
      h('div', { class: 'pilot-base' }, ...Object.entries(BK).map(([k, t]) => h('label', { class: 'f' }, t, B[k]))),
      h('label', { class: 'f' }, '메모', F.note),
      canEdit ? btn('저장', save, 'primary') : h('p', { class: 'muted small' }, '저장은 운영 관리자·접수 담당자만 할 수 있습니다.')));
}

// 정정 요청: 고칠 칸마다 지금 PC 원본 값 → 요청 값

export {
  screenPilot,
};
