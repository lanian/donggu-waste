// PC 화면: 오늘 현황(대시보드)·처리할 일 배지
import * as M from '/web/map.js';
import { STATUS, afterWork, api, boot, btn, chip, go, grid, h, kst, md, pagehead, render, workEnd } from '../core.js';
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
// 선택일의 업무 안내. 현재 배차의 최신 전송만 보며, 이동 이외의 처리는 하지 않는다.
function workflowCard(sum, batches, unassigned, pd, date) {
  const role = boot.me.role, office = ['ADMIN', 'INTAKE'].includes(role);
  const field = role === 'FIELD', resultsAllowed = office || field;
  const active = batches.filter((b) => b.status === 'ACTIVE');
  const unsent = active.filter((b) => !b.bundles[0]).length;
  const resend = active.filter((b) => b.resend_needed).length;
  const unreceived = active.filter((b) => b.bundles[0] && !b.bundles[0].receipt_confirmed_at && !b.resend_needed).length;
  const disconnected = active.filter((b) => !b.device_id || !(boot.devices || []).some((d) => d.id === b.device_id && d.status === 'ACTIVE' && d.confirmed_at)).length;
  const ended = afterWork(date), future = date > boot.today, closed = sum.closed;
  const noResult = (sum.no_result || []).length, photos = sum.photos_missing || 0;
  const closeHref = `#/close?date=${date}`, dispatchHref = `#/dispatch?date=${date}`, sendHref = `#/send?date=${date}`;
  const sendCounts = [resend && `재전송 ${resend}팀`, unsent && `미전송 ${unsent}팀`, unreceived && `수신 미확인 ${unreceived}팀`].filter(Boolean);
  const resultStatus = sum.conflicts ? `충돌 ${sum.conflicts}건 확인` : photos ? `사진 ${photos}장 미회수`
    : noResult ? (ended ? `결과 ${noResult}건 미회수` : '수거 진행 중') : (sum.by_status.DONE ? `수거 완료 ${sum.by_status.DONE}건 반영` : '수거 후 결과 확인');
  const closeStatus = closed ? (closed.status === 'APPROVED' ? '팀장 확인 완료' : '팀장 확인 대기') : future ? '예정일 도래 후 마감' : '마감 전';
  const steps = [
    { key: 'intake', title: '접수', status: sum.total ? `접수 ${sum.total}건` : '접수 없음', detail: '전화·현장 접수를 확인합니다.', href: office ? '#/request/new' : '#/requests', link: office ? '접수 등록' : '접수 보기' },
    { key: 'dispatch', title: '배정', status: office && unassigned === null ? '미배정 확인 필요' : office && unassigned ? `미배정 ${unassigned}건` : active.length ? `${active.length}팀 배정` : '확정 배정 없음', detail: office ? '미배정 사유·방문 순서를 확인합니다.' : '접수 담당자가 배정합니다.', href: office ? dispatchHref : null, link: '배정·노선 보기' },
    { key: 'send', title: '목록 전달', status: disconnected ? `휴대폰 연결 확인 ${disconnected}팀` : sendCounts.join(' · ') || (active.length ? `수신 확인 ${active.length}팀` : '배정 후 전달'), detail: disconnected ? (role === 'ADMIN' ? '지문 확인·팀 연결 후 전달하세요.' : '운영 관리자에게 휴대폰 연결을 요청하세요.') : '휴대폰의 R-수신 코드를 PC에 입력합니다.', href: office ? (disconnected && role === 'ADMIN' ? '#/settings/crews' : sendHref) : null, link: disconnected && role === 'ADMIN' ? '휴대폰 연결 확인' : '목록 전송 보기' },
    { key: 'results', title: '결과 확인', status: resultStatus, detail: '휴대폰 결과·사진을 가져오고 D/P 코드를 전달합니다.', href: resultsAllowed ? '#/results' : null, link: '결과 가져오기' },
    { key: 'close', title: '마감', status: closeStatus, detail: closed ? '기록된 마감과 변경 내역을 확인합니다.' : `금액 확인 ${(sum.pending_fee || []).length}건 · 수납 확인 ${(sum.payments || []).length}건`, href: closeHref, link: '일일 마감 보기' },
  ];
  let next;
  if (field) {
    next = { key: 'results', label: '결과 가져오기', href: '#/results', why: '이 휴대폰의 결과·사진을 PC에 가져온 뒤 D/P 확인 코드를 휴대폰에 전달하세요.' };
  } else if (role === 'LEAD') {
    const approval = (pd && pd.items || []).find((x) => x.key === 'close_approve' && x.count && x.oldest);
    const refund = (pd && pd.items || []).find((x) => x.key === 'refund_approve' && x.count);
    next = closed ? { key: 'close', label: closed.status === 'APPROVED' ? '마감 내역 보기' : '마감 확인하기', href: closeHref, why: `${date} ${closeStatus}. 기록을 검토한 뒤 확인하세요.` }
      : approval ? { key: 'close', label: '대기 중인 마감 검토', href: `#/close?date=${approval.oldest}`, why: `전체 기간 중 마감 확인 대기 ${approval.count}일 · 가장 오래된 ${approval.oldest}부터 확인하세요.` }
        : refund ? { key: 'close', label: '환불 결재 검토', href: '#/refunds?status=REQUESTED', why: `전체 기간의 환불 결재 대기 ${refund.count}건을 검토하세요.` }
          : { key: 'close', label: '일일 마감 현황 보기', href: closeHref, why: '접수·배정·목록 전달은 담당자가 처리하고, 팀장은 마감 기록을 검토합니다.' };
  } else if (closed) {
    next = { key: 'close', label: '마감 내역 보기', href: closeHref, why: `${closeStatus}. 마감 뒤 수정이 있었다면 변경 내역도 확인하세요.` };
  } else if (sum.conflicts) {
    next = { key: 'results', label: '충돌 검토하기', href: '#/results?focus=conflicts', why: `선택일의 충돌 ${sum.conflicts}건을 확인하고 반영할 결과를 결정하세요.` };
  } else if (unassigned === null) {
    next = { key: 'dispatch', label: '미배정 현황 확인', href: dispatchHref, why: '미배정 건수를 불러오지 못했습니다. 배정 화면에서 선택일의 접수를 확인하세요.' };
  } else if (unassigned) {
    next = { key: 'dispatch', label: `미배정 ${unassigned}건 배정하기`, href: dispatchHref, why: '노선을 계산하거나 현재 배정에 추가한 뒤 확정하세요. 미배정 사유도 확인할 수 있습니다.' };
  } else if (disconnected) {
    next = { key: 'send', label: role === 'ADMIN' ? '휴대폰 연결 확인' : '목록 전송 확인', href: role === 'ADMIN' ? '#/settings/crews' : sendHref, why: `${disconnected}팀의 휴대폰 연결·지문 확인이 필요합니다.${role === 'ADMIN' ? ' 설정에서 확인한 뒤 목록을 전달하세요.' : ' 운영 관리자에게 연결을 요청하세요.'}` };
  } else if (sendCounts.length) {
    next = { key: 'send', label: '목록 전송으로 이어가기', href: sendHref, why: `${sendCounts.join(' · ')}. 최신 목록을 전달하고 휴대폰의 R-수신 코드로 확인하세요.` };
  } else if (photos || (ended && noResult)) {
    next = { key: 'results', label: '결과 가져오기', href: '#/results', why: '휴대폰에서 결과·사진 보내기를 마친 뒤 파일 또는 QR을 가져오세요. D/P 확인 코드도 전달하세요.' };
  } else if (!sum.total && !active.length) {
    next = { key: 'intake', label: '전화 접수 등록', href: '#/request/new', why: '이 날짜의 접수가 없습니다. 등록 화면에서 수거 예정일을 확인하고 새 접수를 등록하세요.' };
  } else if ((!ended && noResult) || future) {
    next = { key: 'results', label: '결과 가져오기 안내', href: '#/results', why: future ? '수거 예정일의 배정·목록 전달을 확인했습니다. 결과는 수거를 마친 뒤 가져오세요.' : '수거 진행 중입니다. 휴대폰에만 있는 결과는 아직 이 현황에 반영되지 않습니다.' };
  } else {
    next = { key: 'close', label: '일일 마감 확인', href: closeHref, why: '수납·재방문·미처리 내역을 검토한 뒤 마감을 기록하세요. 마감은 해당 화면에서 직접 진행합니다.' };
  }
  return h('section', { class: 'card workflow', 'aria-labelledby': 'workflow-heading' },
    h('div', { class: 'workflow-head' }, h('h2', { id: 'workflow-heading' }, '업무 이어하기'), h('span', { class: 'workflow-date' }, `${date} 기준`)),
    h('div', { class: 'workflow-next' }, h('div', {}, h('strong', {}, '다음 처리'), h('p', {}, next.why)), h('a', { class: 'workflow-primary', href: next.href }, next.label, ' →')),
    h('ol', { class: 'workflow-steps', 'aria-label': 'PC·휴대폰 업무 순서' }, ...steps.map((step, i) => h('li', { class: 'workflow-step' + (step.key === next.key ? ' current' : ''), 'data-step': step.key, 'aria-current': step.key === next.key ? 'step' : null },
      h('span', { class: 'workflow-number', 'aria-hidden': 'true' }, i + 1), h('h3', {}, step.title), h('strong', { class: 'workflow-state' }, step.status), h('p', {}, step.detail),
      step.href ? h('a', { class: 'workflow-link', href: step.href }, step.link, ' →') : h('span', { class: 'workflow-owner' }, '담당자 처리')))));
}

async function screenHome() {
  const s = (screenHome.s ||= { date: boot.today });
  const date = h('input', { type: 'date', value: s.date, onchange: (e) => { s.date = e.target.value; render(); } });
  const role0 = boot.me.role, overseer = ['ADMIN', 'LEAD'].includes(role0);
  const notField = role0 !== 'FIELD';
  const read = (path) => api(path, { timeoutMs: 10_000 });
  const [sum, batches, accSt, bkSt, pd, rd, ops, unassigned] = await Promise.all([read('close?date=' + s.date), read('batches?date=' + s.date),
    overseer ? read('access/status').catch(() => null) : null, overseer ? read('backup-status').catch(() => null) : null,
    notField ? read('pending').catch(() => null) : null, role0 === 'ADMIN' ? read('readiness').catch(() => null) : null, overseer ? read('ops-status').catch(() => null) : null,
    ['ADMIN', 'INTAKE'].includes(role0) ? read('requests?' + new URLSearchParams({ quick: 'UNASSIGNED', date_field: 'planned', date_from: s.date, date_to: s.date, size: 1, page: 1 })).then((r) => r.total).catch(() => null) : null]);
  // 결과 미회수: 지난 날짜이거나 근무가 끝난 뒤에만(근무 중에는 아직 수거 중)
  const showNoResult = afterWork(s.date);
  const bs = sum.by_status || {};
  const done = bs.DONE || 0, open = (bs.RECEIVED || 0) + (bs.ASSIGNED || 0) + (bs.IN_PROGRESS || 0), revisit = (bs.PARTIAL || 0) + (bs.NOT_COLLECTED || 0) + (bs.ON_HOLD || 0);
  const active = sum.total - (bs.CANCELLED || 0);
  const pct = active ? Math.round((done / active) * 100) : 0;
  const role = boot.me.role;
  const kpi = (label, value, sub, cls = '', href, unit = '') => h(href ? 'a' : 'div', { class: 'kpi ' + cls, href }, h('span', { class: 'k' }, label),
    h('b', { class: 'v' }, h('span', {}, value), unit ? h('small', { class: 'today-kpi-unit' }, unit) : null), sub ? h('span', { class: 's' }, sub) : null);
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
  // 주소 위치 자료가 45일 넘음(행안부 매월 갱신): 새 주소를 못 찾으니 새 자료로 바꾸라는 안내
  const addrWarn = ops && ops.addr_warning ? h('div', { class: 'notice warn addr-warn', role: 'status' }, h('strong', {}, '주소 위치 자료 갱신: '), ops.addr_warning, ' ',
    role0 === 'ADMIN' ? h('a', { href: '#/settings/addr' }, '설정 → 주소 위치 자료 →') : '운영 관리자에게 알리세요.') : null;
  const backlogCard = notField ? h('div', { class: 'card backlog dash-backlog' }, h('div', { class: 'panel-h' }, h('h2', {}, '밀린 일 (전체 기간)'), backlog.length ? h('span', { class: 'muted small' }, `${backlog.length}종`) : null),
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
    } catch { mapBox.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다(인터넷 연결 확인). 팀별 진행은 이 화면 아래에서 볼 수 있습니다.')); }
  }, 30);
  if (!act.length) mapBox.append(h('div', { class: 'map-empty' }, h('b', {}, '확정된 배정이 없습니다'), h('span', {}, role0 === 'FIELD' ? '배정이 확정되면 방문 순서가 표시됩니다.' : '[배정·노선]에서 확정하면 팀별 핀과 방문 순서가 지도에 표시됩니다.')));
  // 관리자·팀장: 접속기록 월 점검, 최근 백업
  const overseeCard = overseer ? h('div', { class: 'card dash-oversee' }, h('div', { class: 'panel-h' }, h('h2', {}, '점검'), null),
    h('dl', { class: 'kv compact' },
      h('dt', {}, '이번 달 접속기록 점검'), h('dd', {}, accSt ? (accSt.done ? [chip('점검 완료', accSt.done.result && accSt.done.result.ok ? 'ok' : 'bad'), ` ${kst(accSt.done.at)} · ${accSt.done.actor}`]
        : [chip('점검 전', 'warn'), ' ', h('a', { href: '#/access' }, '접속기록에서 점검 →')]) : '—'),
      h('dt', {}, '최근 백업'), h('dd', {}, bkSt ? (bkSt.last_at ? [chip(bkSt.stale ? `${Math.floor(bkSt.age_hours)}시간 전 · 확인 필요` : '정상', bkSt.stale ? 'bad' : 'ok'), ` ${kst(bkSt.last_at)}`] : chip('없음', 'bad')) : '—',
        bkSt && bkSt.auto && bkSt.auto.at && !bkSt.auto.ok ? h('div', { class: 'small err' }, `자동 백업 실패: ${bkSt.auto.error || ''}`) : null,
        bkSt && !bkSt.key ? h('div', { class: 'small muted' }, '백업 키가 없어 자동 백업을 하지 않습니다.') : null))) : null;
  const nodeCls = (st) => (st === 'DONE' ? 'ok' : ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(st) ? 'warn' : st === 'CANCELLED' ? 'neutral' : st === 'IN_PROGRESS' ? 'info' : '');
  const dongCard = (sum.dongs || []).length ? h('div', { class: 'card today-dongs' }, h('div', { class: 'panel-h' }, h('h2', {}, '동별 현황'), sum.capacity && sum.capacity.limit ? h('span', { class: 'muted small' }, `전체 ${sum.capacity.booked}/${sum.capacity.limit}건`) : null),
    h('div', { class: 'tablewrap' }, h('table', { class: 'dongtbl' }, h('thead', {}, h('tr', {}, ...['동', '담당 팀', '예정', '완료', '미처리', '한도 대비'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, ...sum.dongs.map((x) => h('tr', {}, h('td', {}, x.dong ? h('a', { href: '#/requests', onclick: () => { screenRequests.f = { ...(screenRequests.f || {}), size: 50, dong: x.dong, status: '', date_from: s.date, date_to: s.date, date_field: 'planned', page: 1, quick: 'ALL' }; } }, x.dong) : h('span', { class: 'muted' }, '(동 미지정)'),
        x.working ? null : chip('수거일 아님', 'neutral')),
        h('td', {}, x.crew || '-'), h('td', { class: 'num' }, x.total), h('td', { class: 'num' }, x.done), h('td', { class: 'num' }, x.open),
        h('td', {}, x.limit ? chip(`${x.total}/${x.limit}건`, x.total > x.limit ? 'bad' : x.total === x.limit ? 'warn' : 'ok') : '-', x.limit_m3 ? chip(`${x.m3}/${x.limit_m3}m³`, x.m3 > x.limit_m3 ? 'bad' : '') : null))))))) : null;
  const crewsCard = h('div', { class: 'card today-crews' }, h('div', { class: 'panel-h' }, h('h2', {}, '팀별 진행'), act.length ? h('span', { class: 'legend small' }, h('span', { class: 'lg ok' }, '완료'), h('span', { class: 'lg warn' }, '재방문'), h('span', { class: 'lg track' }, '남음')) : null),
    act.length ? h('div', { class: 'crews' }, ...act.map((b, bi) => {
      const n = b.stops.length, d = b.stops.filter((x) => x.status === 'DONE').length, w = b.stops.filter((x) => ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.status)).length;
      const last = b.bundles[0];
      return h('div', { class: `crew route c${(bi % 4) + 1}` }, h('div', { class: 'row crew-h' }, h('span', { class: 'swatch', 'aria-hidden': 'true' }), h('b', {}, b.crew_name), h('span', { class: 'muted small' }, `갱신본 ${b.assign_ver}`), h('span', { class: 'grow' }),
          !last ? chip('미전송', 'warn') : last.receipt_confirmed_at ? chip('수신 확인', 'ok') : chip('수신 미확인', 'warn'), b.resend_needed ? chip('재전송 필요', 'bad') : null),
        h('div', { class: 'seg' }, h('i', { class: 'ok', style: `flex:${d}` }), h('i', { class: 'warn', style: `flex:${w}` }), h('i', { class: 'rest', style: `flex:${Math.max(0, n - d - w)}` })),
        h('div', { class: 'row small' }, `${n}곳 중 완료 ${d} · 재방문 ${w}`),
        n ? h('ol', { class: 'tline', 'aria-label': `${b.crew_name} 방문 순서` }, ...b.stops.map((x, i) => h('li', { class: nodeCls(x.status), title: `${i + 1}. ${x.name} · ${STATUS[x.status] || x.status}` },
          h('b', {}, i + 1), h('small', {}, x.eta || '—'), h('span', { class: 'sr-only' }, ` ${x.name} ${STATUS[x.status] || ''}`)))) : null);
    })) : h('p', { class: 'empty' }, '확정된 배정이 없습니다.', role !== 'FIELD' ? h('a', { href: '#/dispatch' }, ' 배정·노선으로 →') : null));
  const tasksCard = h('div', { class: 'card dash-tasks' }, h('div', { class: 'panel-h' }, h('h2', {}, '처리할 일'), todo.length ? h('span', { class: 'muted small' }, `${todo.length}종`) : null),
    todo.length ? h('ul', { class: 'todo' }, ...todo.map(([, t, href, cls, , pre]) => h('li', {}, h('a', { href, class: 'todo-a ' + cls, onclick: pre || null }, h('span', {}, t), h('span', { class: 'arr' }, '→')))))
      : h('p', { class: 'empty' }, sum.total ? (s.date === boot.today && !showNoResult && sum.no_result.length ? `지금 처리할 일이 없습니다. 결과 미회수는 근무가 끝난 뒤(${workEnd()}) 표시합니다.` : '지금 처리할 일이 없습니다. 결과를 가져오면 미회수·충돌·수납 확인이 여기에 모입니다.') : '이 날짜의 접수가 없습니다. 다른 날짜를 고르거나 접수를 등록하세요.'));
  return h('div', { class: 'dash today-dashboard' }, h('div', { class: 'pagehead dash-heading' },
    h('div', { class: 'dash-heading-copy' }, h('h1', {}, '오늘 현황'), h('p', { class: 'muted small' }, '선택한 날짜의 수거 진행과 처리할 일을 확인하세요.')),
    h('div', { class: 'row dash-controls' }, btn('빠른 수수료 계산', () => go('#/calculator'), 'primary'), h('label', { class: 'f inline dash-date-control' }, h('span', {}, '기준일'), date), sum.closed ? chip('마감됨', 'ok') : chip('마감 전'))),
    workflowCard(sum, batches, unassigned, pd, s.date),
    h('div', { class: 'kpis dash-kpis' },
      kpi('수거 예정', active, (bs.CANCELLED ? `취소 ${bs.CANCELLED}건 제외` : '선택일 기준'), '', '#/requests', '건'),
      kpi('수거 완료', done, h('span', { class: 'today-completion' }, h('span', {}, `${pct}% 완료`), h('span', { class: 'bar', role: 'progressbar', 'aria-label': '수거 완료율', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': pct }, h('i', { style: `width:${pct}%` }))), 'ok', null, '건'),
      kpi('진행·대기', open, bs.IN_PROGRESS ? `처리 중 ${bs.IN_PROGRESS}` : ' ', '', null, '건'),
      kpi('재방문·보류', revisit, bs.NOT_COLLECTED ? `미수거 ${bs.NOT_COLLECTED}` : ' ', revisit ? 'warn' : '', null, '건'),
      kpi('하차 기록', sum.unload_total.volume_m3, sum.unloads.length ? `${sum.unloads.length}회 · ${sum.unload_total.weight_kg}kg` : '기록 없음', '', null, 'm³')),
    opsWarn, addrWarn,
    h('div', { class: 'dash-grid today-activity-grid' },
      h('div', { class: 'card mapcard dash-map-card' }, h('div', { class: 'panel-h' }, h('h2', {}, '배차 지도'), h('span', { class: 'muted small' }, act.length ? `확정 배정 ${act.length}팀 · ${act.reduce((a, b) => a + b.stops.length, 0)}곳` : '확정된 배정 없음')), mapBox,
        h('p', { class: 'muted small' }, '핀 번호는 팀-방문 순서, 녹색 핀은 수거 완료. 선은 방문 순서를 잇는 직선이며 실제 도로가 아닙니다.')),
      h('div', { class: 'dash-side' }, tasksCard, backlogCard)),
    h('div', { class: 'today-detail-grid' }, dongCard, crewsCard),
    h('div', { class: 'today-operations-grid' }, readinessCard(rd), overseeCard),
    h('p', { class: 'muted small today-footnote' }, '현황은 이 PC에 가져온 결과 기준입니다. 휴대폰에서 아직 보내지 않은 결과는 반영되지 않습니다.'));
}

export {
  FIX_HREF, badges, paintBadges, pendingTodos, readinessCard, refreshBadges, screenHome,
};
