// PC 화면: 오늘 현황(대시보드)·처리할 일 배지
import * as M from '/web/map.js';
import { STATUS, afterWork, api, boot, chip, grid, h, kst, md, pagehead, render, workEnd } from '../core.js';
import { mapIn } from '../widgets.js';
import { screenRequests } from './requests.js';

// 처리할 일(전체 기간, GET /api/pending) → 화면 링크. 결과 미회수 팀은 근무가 끝난 팀만 센다.
function pendingTodos(pd) {
  const by = Object.fromEntries((pd ? pd.items : []).map((x) => [x.key, x]));
  const nr = ((by.no_result_crews || {}).crews || []).filter((c) => afterWork(c.work_date, c.crew_id));
  const old = (x) => (x && x.oldest ? ` · 가장 오래된 ${md(x.oldest)}` : '');
  const t = [];
  const add = (key, n, text, href, cls, roles) => { if (n && roles.split(' ').includes(boot.me.role)) t.push({ key, n, text, href, cls }); };
  const x = (k) => by[k] || { count: 0 };
  add('close_approve', x('close_approve').count, `마감 확인 대기 ${x('close_approve').count}일${old(by.close_approve)}`, `#/close?date=${x('close_approve').oldest}`, 'warn', 'ADMIN LEAD');
  add('refund_approve', x('refund_approve').count, `환불 결재 대기 ${x('refund_approve').count}건${old(by.refund_approve)}`, '#/refunds?status=REQUESTED', 'warn', 'ADMIN LEAD');
  add('unclosed_days', x('unclosed_days').count, `마감 안 한 지난 날짜 ${x('unclosed_days').count}일${old(by.unclosed_days)}`, `#/close?date=${x('unclosed_days').oldest}`, 'warn', 'ADMIN INTAKE LEAD');
  add('overdue', x('overdue').count, `지난 예정일 미처리 ${x('overdue').count}건${old(by.overdue)}`, '#/requests?quick=OVERDUE', 'bad', 'ADMIN INTAKE LEAD');
  add('nodate', x('nodate').count, `예정일 없음 ${x('nodate').count}건`, '#/requests?quick=NODATE', 'warn', 'ADMIN INTAKE LEAD');
  add('no_result_crews', nr.length, `결과 미회수 팀 ${nr.length}곳 (${[...new Set(nr.map((c) => `${c.crew} ${md(c.work_date)}`))].slice(0, 3).join(', ')})`, '#/results', 'bad', 'ADMIN INTAKE LEAD');
  add('conflicts', x('conflicts').count, `충돌함 ${x('conflicts').count}건${old(by.conflicts)}`, '#/results?focus=conflicts', 'bad', 'ADMIN INTAKE');
  return t;
}
// 메뉴 숫자(일일 마감·환불·결과 가져오기): 화면을 옮길 때 20초에 한 번 불러온다(작업 뒤에는 바로)
const badges = { at: 0, counts: {} };
async function refreshBadges(force) {
  if (!boot || boot.me.role === 'FIELD' || boot.me.must_change) return;
  if (!force && Date.now() - badges.at < 20000) return paintBadges();
  badges.at = Date.now();
  try {
    const t = pendingTodos(await api('pending'));
    const n = (...ks) => t.filter((x) => ks.includes(x.key)).reduce((a, x) => a + x.n, 0);
    badges.counts = { close: n('close_approve', 'unclosed_days'), refunds: n('refund_approve'), results: n('conflicts', 'no_result_crews') };
  } catch { /* 숫자는 보조 정보 */ }
  paintBadges();
}
function paintBadges() {
  document.querySelectorAll('nav.side a[data-k]').forEach((a) => {
    const n = badges.counts[a.dataset.k] || 0;
    let b = a.querySelector('.nav-badge');
    if (!n) { if (b) b.remove(); return; }
    if (!b) { b = h('span', { class: 'nav-badge' }); a.append(b); }
    b.textContent = n > 99 ? '99+' : String(n); b.setAttribute('aria-label', `처리할 일 ${n}건`);
  });
}
// 화면 키(서버 readiness의 fix) → 이 화면의 주소
const FIX_HREF = { 'settings/crews': '#/settings/crews', 'settings/devices': '#/settings/devices', 'settings/route': '#/settings/basic', 'settings/dongs': '#/settings/dongs',
  'settings/address': '#/settings/addr', 'settings/backup': '#/settings/backup', fee: '#/fees', 'settings/notice': '#/settings/basic', users: '#/users' };
function readinessCard(rd) {
  if (!rd || rd.ok) return null;
  return h('div', { class: 'card readiness' }, h('div', { class: 'panel-h' }, h('h2', {}, '운영 준비 점검'), chip(`${rd.done} / ${rd.total} 완료`, rd.done === rd.total ? 'ok' : 'warn')),
    h('ul', { class: 'ready-list' }, ...rd.items.map((x) => h('li', { class: x.ok ? 'ok' : 'no' }, h('span', { class: 'mark', 'aria-hidden': 'true' }, x.ok ? '✓' : '✗'),
      h('span', { class: 'sr-only' }, x.ok ? '완료: ' : '안 됨: '), h('span', { class: 'lbl' }, x.label), x.note ? h('span', { class: 'muted small' }, ` ${x.note}`) : null,
      !x.ok && FIX_HREF[x.fix] ? h('a', { href: FIX_HREF[x.fix], class: 'small fix' }, '고치기 →') : null))),
    h('p', { class: 'muted small' }, '모두 끝나면 이 상자는 사라집니다. 운영 매뉴얼 9장(운영 전 확인)과 같은 목록입니다.'));
}
async function screenHome() {
  const s = (screenHome.s ||= { date: boot.today });
  const date = h('input', { type: 'date', value: s.date, onchange: (e) => { s.date = e.target.value; render(); } });
  const role0 = boot.me.role, overseer = ['ADMIN', 'LEAD'].includes(role0);
  const notField = role0 !== 'FIELD';
  const [sum, batches, accSt, bkSt, pd, rd, ops] = await Promise.all([api('close?date=' + s.date), api('batches?date=' + s.date),
    overseer ? api('access/status').catch(() => null) : null, overseer ? api('backup-status').catch(() => null) : null,
    notField ? api('pending').catch(() => null) : null, role0 === 'ADMIN' ? api('readiness').catch(() => null) : null, overseer ? api('ops-status').catch(() => null) : null]);
  // 결과 미회수: 지난 날짜이거나 근무가 끝난 뒤에만(근무 중에는 아직 수거 중)
  const showNoResult = afterWork(s.date);
  const bs = sum.by_status || {};
  const done = bs.DONE || 0, open = (bs.RECEIVED || 0) + (bs.ASSIGNED || 0) + (bs.IN_PROGRESS || 0), revisit = (bs.PARTIAL || 0) + (bs.NOT_COLLECTED || 0) + (bs.ON_HOLD || 0);
  const active = sum.total - (bs.CANCELLED || 0);
  const pct = active ? Math.round((done / active) * 100) : 0;
  const role = boot.me.role;
  const kpi = (label, value, sub, cls = '', href) => h(href ? 'a' : 'div', { class: 'kpi ' + cls, href }, h('span', { class: 'k' }, label), h('b', { class: 'v' }, value), sub ? h('span', { class: 's' }, sub) : null);
  // 해야 할 일: 담당자가 바로 눌러 처리할 수 있게
  const todo = [
    [sum.unconfirmed_bundles, `수신 미확인 전송 ${sum.unconfirmed_bundles}건`, '#/send', 'warn', 'ADMIN INTAKE'],
    [showNoResult ? sum.no_result.length : 0, `결과 미회수 ${sum.no_result.length}건`, '#/results', 'warn', 'ADMIN INTAKE FIELD'],
    [sum.conflicts, `충돌·현장 요청 검토 ${sum.conflicts}건`, '#/results', 'bad', 'ADMIN INTAKE'],
    [sum.field_intakes, `현장 접수 확인 ${sum.field_intakes}건`, '#/requests', 'warn', 'ADMIN INTAKE', () => { screenRequests.f = { ...(screenRequests.f || {}), status: '', date_from: '', date_to: '', quick: 'FIELD', page: 1 }; }],
    [sum.pending_fee.length, `금액 확인 대기 ${sum.pending_fee.length}건`, '#/requests?quick=FEE', 'warn', 'ADMIN INTAKE'],
    [sum.payments.length, `수납 확인 필요 ${sum.payments.length}건`, '#/close', 'warn', 'ADMIN INTAKE'],
    [sum.photos_missing, `사진 미회수 ${sum.photos_missing}장`, '#/results', '', 'ADMIN INTAKE FIELD'],
    [revisit, `재방문 후보 ${revisit}건`, '#/close', 'warn', 'ADMIN INTAKE'],
    [(sum.sticker_issues || []).length, `스티커 없음·불일치 ${(sum.sticker_issues || []).length}건`, '#/close', 'warn', 'ADMIN INTAKE LEAD'],
    [(sum.fee_refused || []).length, `추가 금액 고지 거부 ${(sum.fee_refused || []).length}건`, '#/close', 'warn', 'ADMIN INTAKE LEAD'],
    [accSt && !accSt.done ? 1 : 0, `이번 달 접속기록 점검 (${accSt ? accSt.month : ''})`, '#/access', 'warn', 'ADMIN LEAD'],
    [bkSt && bkSt.stale ? 1 : 0, bkSt && bkSt.last_at ? `백업이 ${Math.floor(bkSt.age_hours)}시간 지났습니다` : '백업 기록이 없습니다', '#/settings/backup', 'bad', 'ADMIN LEAD'],
  ].filter(([n, , , , r]) => n && r.split(' ').includes(role));
  const backlog = pendingTodos(pd);
  badges.counts = { close: backlog.filter((x) => ['close_approve', 'unclosed_days'].includes(x.key)).reduce((a, x) => a + x.n, 0),
    refunds: backlog.filter((x) => x.key === 'refund_approve').reduce((a, x) => a + x.n, 0), results: backlog.filter((x) => ['conflicts', 'no_result_crews'].includes(x.key)).reduce((a, x) => a + x.n, 0) };
  badges.at = Date.now();
  // 예약 작업(자동 파기·사진 정리·무결성 검사·백업)이 이틀 넘게 안 돌았으면 경고 띠
  const opsWarn = ops && ops.warnings && ops.warnings.length ? h('div', { class: 'notice warn ops-warn', role: 'status' }, h('strong', {}, '예약 작업 확인 필요: '), ops.warnings.join(' · '),
    ' — 서버가 꺼져 있었거나 작업이 실패했을 수 있습니다. ', role0 === 'ADMIN' ? h('a', { href: '#/users?focus=diag' }, '진단 보기 →') : '운영 관리자에게 알리세요.') : null;
  const backlogCard = notField ? h('div', { class: 'card backlog' }, h('div', { class: 'panel-h' }, h('h2', {}, '밀린 일 (전체 기간)'), backlog.length ? h('span', { class: 'muted small' }, `${backlog.length}종`) : null),
    backlog.length ? h('ul', { class: 'todo' }, ...backlog.map((x) => h('li', {}, h('a', { href: x.href, class: 'todo-a ' + x.cls, 'data-key': x.key }, h('span', {}, x.text), h('span', { class: 'arr' }, '→')))))
      : h('p', { class: 'empty' }, '지난 날짜에 밀린 일이 없습니다.')) : null;
  const act = batches.filter((b) => b.status === 'ACTIVE');
  // 배차 지도: 팀 색 핀 + 순서 선 (핀은 이 화면에서만 그림, 지도 서버로 보내지 않음)
  const mapBox = h('div', { class: 'mapbox dash-map' });
  const dep = boot.settings.depot;
  setTimeout(async () => {
    try {
      const { map, ml } = await mapIn(mapBox, { center: [dep.lon, dep.lat], zoom: 13 });
      const bnd = new ml.LngLatBounds(); bnd.extend([dep.lon, dep.lat]);
      M.pin(ml, map, { lng: dep.lon, lat: dep.lat, label: '출발', cls: 'me' });
      const lines = [];
      act.forEach((b, bi) => {
        const coords = [[dep.lon, dep.lat]];
        b.stops.forEach((x, i) => { if (x.lat == null) return; coords.push([x.lon, x.lat]); bnd.extend([x.lon, x.lat]); M.pin(ml, map, { lng: x.lon, lat: x.lat, label: `${bi + 1}-${i + 1}`, cls: x.status === 'DONE' ? 'done' : 'c' + ((bi % 4) + 1) }); });
        lines.push({ type: 'Feature', properties: { c: bi % 4 }, geometry: { type: 'LineString', coordinates: coords } });
      });
      // 핀이 모두 보이게 먼저 맞춘다 (바탕 지도가 늦거나 못 와도 핀은 보이게)
      if (lines.length) map.fitBounds(bnd, { padding: 50, maxZoom: 15, duration: 0 });
      const whenStyle = (f) => (map.isStyleLoaded() ? f() : map.once('style.load', f));
      whenStyle(() => {
        map.addSource('routes', { type: 'geojson', data: { type: 'FeatureCollection', features: lines } });
        map.addLayer({ id: 'routes', type: 'line', source: 'routes', paint: { 'line-width': 3, 'line-dasharray': [2, 1], 'line-color': ['match', ['get', 'c'], 0, '#2557a7', 1, '#a5560d', 2, '#6c3fb5', '#0e7490'] } });
      });
    } catch { mapBox.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다(인터넷 연결 확인). 팀별 진행은 오른쪽 목록에서 볼 수 있습니다.')); }
  }, 30);
  if (!act.length) mapBox.append(h('div', { class: 'map-empty' }, h('b', {}, '확정된 배정이 없습니다'), h('span', {}, role0 === 'FIELD' ? '배정이 확정되면 방문 순서가 표시됩니다.' : '[배정·노선]에서 확정하면 팀별 핀과 방문 순서가 지도에 표시됩니다.')));
  // 관리자·팀장: 접속기록 월 점검, 최근 백업
  const overseeCard = overseer ? h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '점검'), null),
    h('dl', { class: 'kv compact' },
      h('dt', {}, '이번 달 접속기록 점검'), h('dd', {}, accSt ? (accSt.done ? [chip('점검 완료', accSt.done.result && accSt.done.result.ok ? 'ok' : 'bad'), ` ${kst(accSt.done.at)} · ${accSt.done.actor}`]
        : [chip('점검 전', 'warn'), ' ', h('a', { href: '#/access' }, '접속기록에서 점검 →')]) : '—'),
      h('dt', {}, '최근 백업'), h('dd', {}, bkSt ? (bkSt.last_at ? [chip(bkSt.stale ? `${Math.floor(bkSt.age_hours)}시간 전 · 확인 필요` : '정상', bkSt.stale ? 'bad' : 'ok'), ` ${kst(bkSt.last_at)}`] : chip('없음', 'bad')) : '—',
        bkSt && bkSt.auto && bkSt.auto.at && !bkSt.auto.ok ? h('div', { class: 'small err' }, `자동 백업 실패: ${bkSt.auto.error || ''}`) : null,
        bkSt && !bkSt.key ? h('div', { class: 'small muted' }, '백업 키가 없어 자동 백업을 하지 않습니다.') : null))) : null;
  const nodeCls = (st) => (st === 'DONE' ? 'ok' : ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(st) ? 'warn' : st === 'CANCELLED' ? 'neutral' : st === 'IN_PROGRESS' ? 'info' : '');
  return h('div', { class: 'dash' }, readinessCard(rd), opsWarn, h('div', { class: 'pagehead' }, h('h1', {}, '오늘 현황'), h('div', { class: 'row' }, h('label', { class: 'f inline' }, h('span', { class: 'sr-only' }, '기준일'), date), sum.closed ? chip('마감됨', 'ok') : chip('마감 전'))),
    h('div', { class: 'kpis' },
      kpi('수거 예정', `${active}건`, (bs.CANCELLED ? `취소 ${bs.CANCELLED}건 제외` : ' '), '', '#/requests'),
      kpi('수거 완료', `${done}건`, h('span', { class: 'bar' }, h('i', { style: `width:${pct}%` })), 'ok'),
      kpi('진행·대기', `${open}건`, bs.IN_PROGRESS ? `처리 중 ${bs.IN_PROGRESS}` : ' ', ''),
      kpi('재방문·보류', `${revisit}건`, bs.NOT_COLLECTED ? `미수거 ${bs.NOT_COLLECTED}` : ' ', revisit ? 'warn' : ''),
      kpi('하차 기록', `${sum.unload_total.volume_m3}m³`, sum.unloads.length ? `${sum.unloads.length}회 · ${sum.unload_total.weight_kg}kg` : '기록 없음', '')),
    h('div', { class: 'dash-grid' },
      h('div', { class: 'card mapcard' }, h('div', { class: 'panel-h' }, h('h2', {}, '배차 지도'), h('span', { class: 'muted small' }, act.length ? `확정 배정 ${act.length}팀 · ${act.reduce((a, b) => a + b.stops.length, 0)}곳` : '확정된 배정 없음')), mapBox,
        h('p', { class: 'muted small' }, '핀 번호는 팀-방문 순서, 녹색 핀은 수거 완료. 선은 방문 순서를 잇는 직선이며 실제 도로가 아닙니다.')),
      h('div', { class: 'dash-side' },
      backlogCard,
      overseeCard,
      h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '처리할 일'), todo.length ? h('span', { class: 'muted small' }, `${todo.length}종`) : null),
        todo.length ? h('ul', { class: 'todo' }, ...todo.map(([, t, href, cls, , pre]) => h('li', {}, h('a', { href, class: 'todo-a ' + cls, onclick: pre || null }, h('span', {}, t), h('span', { class: 'arr' }, '→')))))
          : h('p', { class: 'empty' }, sum.total ? (s.date === boot.today && !showNoResult && sum.no_result.length ? `지금 처리할 일이 없습니다. 결과 미회수는 근무가 끝난 뒤(${workEnd()}) 표시합니다.` : '지금 처리할 일이 없습니다. 결과를 가져오면 미회수·충돌·수납 확인이 여기에 모입니다.') : '이 날짜의 접수가 없습니다. 다른 날짜를 고르거나 접수를 등록하세요.')),
      (sum.dongs || []).length ? h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '동별 현황'), sum.capacity && sum.capacity.limit ? h('span', { class: 'muted small' }, `전체 ${sum.capacity.booked}/${sum.capacity.limit}건`) : null),
        h('div', { class: 'tablewrap' }, h('table', { class: 'dongtbl' }, h('thead', {}, h('tr', {}, ...['동', '담당 팀', '예정', '완료', '미처리', '한도 대비'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, ...sum.dongs.map((x) => h('tr', {}, h('td', {}, x.dong ? h('a', { href: '#/requests', onclick: () => { screenRequests.f = { ...(screenRequests.f || {}), size: 50, dong: x.dong, status: '', date_from: s.date, date_to: s.date, date_field: 'planned', page: 1, quick: 'ALL' }; } }, x.dong) : h('span', { class: 'muted' }, '(동 미지정)'),
            x.working ? null : chip('수거일 아님', 'neutral')),
            h('td', {}, x.crew || '-'), h('td', { class: 'num' }, x.total), h('td', { class: 'num' }, x.done), h('td', { class: 'num' }, x.open),
            h('td', {}, x.limit ? chip(`${x.total}/${x.limit}건`, x.total > x.limit ? 'bad' : x.total === x.limit ? 'warn' : 'ok') : '-', x.limit_m3 ? chip(`${x.m3}/${x.limit_m3}m³`, x.m3 > x.limit_m3 ? 'bad' : '') : null))))))) : null,
      h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '팀별 진행'), act.length ? h('span', { class: 'legend small' }, h('span', { class: 'lg ok' }, '완료'), h('span', { class: 'lg warn' }, '재방문'), h('span', { class: 'lg track' }, '남음')) : null),
        act.length ? h('div', { class: 'crews' }, ...act.map((b, bi) => {
          const n = b.stops.length, d = b.stops.filter((x) => x.status === 'DONE').length, w = b.stops.filter((x) => ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.status)).length;
          const last = b.bundles[0];
          return h('div', { class: `crew route c${(bi % 4) + 1}` }, h('div', { class: 'row crew-h' }, h('span', { class: 'swatch', 'aria-hidden': 'true' }), h('b', {}, b.crew_name), h('span', { class: 'muted small' }, `갱신본 ${b.assign_ver}`), h('span', { class: 'grow' }),
              !last ? chip('미전송', 'warn') : last.receipt_confirmed_at ? chip('수신 확인', 'ok') : chip('수신 미확인', 'warn'), b.resend_needed ? chip('재전송 필요', 'bad') : null),
            h('div', { class: 'seg' }, h('i', { class: 'ok', style: `flex:${d}` }), h('i', { class: 'warn', style: `flex:${w}` }), h('i', { class: 'rest', style: `flex:${Math.max(0, n - d - w)}` })),
            h('div', { class: 'row small' }, `${n}곳 중 완료 ${d} · 재방문 ${w}`),
            n ? h('ol', { class: 'tline', 'aria-label': `${b.crew_name} 방문 순서` }, ...b.stops.map((x, i) => h('li', { class: nodeCls(x.status), title: `${i + 1}. ${x.name} · ${STATUS[x.status] || x.status}` },
              h('b', {}, i + 1), h('small', {}, x.eta || '—'), h('span', { class: 'sr-only' }, ` ${x.name} ${STATUS[x.status] || ''}`)))) : null);
        })) : h('p', { class: 'empty' }, '확정된 배정이 없습니다.', role !== 'FIELD' ? h('a', { href: '#/dispatch' }, ' 배정·노선으로 →') : null)))),
    h('p', { class: 'muted small' }, '현황은 이 PC에 가져온 결과 기준입니다. 휴대폰에서 아직 보내지 않은 결과는 반영되지 않습니다.'));
}

export {
  FIX_HREF, badges, paintBadges, pendingTodos, readinessCard, refreshBadges, screenHome,
};
