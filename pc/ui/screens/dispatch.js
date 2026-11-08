// PC 화면: 배정·노선
import * as M from '/web/map.js';
import * as NAV from '/web/nav.js';
import { api, boot, btn, chip, clearMaps, confirmDlg, go, grid, h, isStale, md, post, reasonDlg, routeEpoch, searchbox, statusChip, toast, wrap } from '../core.js';
import { mapIn } from '../widgets.js';

// 품목별 취급 표시(적재 기준표): 사람·장비가 더 필요한 것은 주의, 싣는 방법은 안내
const LOAD_TAG_CLS = { EQUIP: 'warn', TWO: 'warn', FRAGILE: 'warn', BASE: 'info', UPRIGHT: 'info', DRAIN: 'info' };
// 하루 물량 점검: 고른 접수 합(적재·무게·작업 시간) ↔ 고른 팀이 하루에 감당할 양, 취급 표시별 곳 수, 경고
function daySummaryCard(res) {
  const m = res.summary;
  if (!m) return null;
  const TL = res.tag_labels || {};
  return h('div', { class: 'card day-load' }, h('h2', {}, '하루 물량 점검'),
    h('p', {}, `${m.requests}건 · 적재 추정 ${m.m3}m³ / 고른 팀 ${m.cap_m3}m³${m.trips_per_crew > 1 ? ` (팀마다 ${m.trips_per_crew}회 싣기, 하차 포함)` : ''}`
      + ` · 무게 추정 ${m.kg.toLocaleString()}kg${m.cap_kg ? ` / ${m.cap_kg.toLocaleString()}kg` : ''} · 현장 작업 약 ${m.work_min}분 / 근무 ${m.shift_min}분`),
    Object.keys(m.tags || {}).length ? h('p', { class: 'row' }, ...Object.entries(m.tags).map(([t, n]) => chip(`${TL[t] || t} ${n}곳`, LOAD_TAG_CLS[t] || ''))) : null,
    res.must_relaxed ? h('div', { class: 'notice warn small' }, '오늘 필수 건이 모두 들어가지 않아, 넣을 수 있는 만큼 넣고 나머지를 미배정에 "오늘 필수였지만 넣지 못함"으로 표시했습니다. 팀을 더하거나 다른 건을 미루고 다시 계산하세요.') : null,
    ...(m.warnings || []).map((w) => h('div', { class: 'notice warn small' }, w)),
    h('p', { class: 'muted small' }, '부피·무게·작업 시간은 품목별 적재 기준표의 추정치입니다(설정 → 수거팀·차량 → 적재량 보정에서 실측과 비교).'));
}
// 카카오맵 웹 길찾기 링크(지점마다): 좌표와 "N번 수거지"만 담는다. 이름·주소는 넣지 않는다. (휴대폰 web/nav.js와 같은 형식)
function kakaoLink(q, n) {
  if (!q || !NAV.hasCoord(q)) return null;
  const label = NAV.navLabel(n);
  return h('a', { class: 'small maplink', href: NAV.navUrls('kakao', { lat: q.lat, lon: q.lon, label }).fallback, target: '_blank', rel: 'noopener noreferrer',
    title: `카카오맵에서 ${label}까지 길찾기 (좌표만 전달)`, 'aria-label': `${label} 카카오맵 길찾기` }, ' 카카오맵');
}
let dispatchViewSeq = 0;
async function screenDispatch() {
  const myView = ++dispatchViewSeq, myRoute = routeEpoch, myHash = location.hash;
  let view, requestSeq = 0;
  const s = (screenDispatch.s ||= { date: boot.today, crews: new Set(), seen: new Set(), plan: null });
  const date = h('input', { type: 'date', id: 'dp-date', value: s.date });
  const crews = boot.crews.filter((c) => c.active);
  crews.forEach((c) => { if (!s.seen.has(c.id)) { s.seen.add(c.id); s.crews.add(c.id); } });  // 새로 만든 팀은 자동으로 포함
  const selectedCrews = () => [...s.crews].filter((id) => crews.some((c) => c.id === id));
  const crewKey = (ids = []) => JSON.stringify([...ids].sort());
  const current = () => view?.isConnected && myView === dispatchViewSeq && myRoute === routeEpoch && location.hash === myHash;
  const beginRequest = () => ({ seq: ++requestSeq, date: date.value, crewIds: selectedCrews() });
  const requestCurrent = (request) => current() && request.seq === requestSeq && request.date === date.value && crewKey(request.crewIds) === crewKey(selectedCrews());
  const calcBtn = btn('노선 계산', null, 'primary');
  const crewHint = h('span', { class: 'muted small' });
  const syncCalc = () => { const none = ![...s.crews].some((id) => crews.some((c) => c.id === id)); calcBtn.disabled = none; crewHint.textContent = none ? '팀을 하나 이상 고르세요.' : ''; };
  const crewBox = h('div', { class: 'row crewpick' }, ...crews.map((c) => h('label', { class: 'row' }, h('input', { type: 'checkbox', checked: s.crews.has(c.id), onchange: (e) => { e.target.checked ? s.crews.add(c.id) : s.crews.delete(c.id); requestSeq++; s.edit = null; syncCalc(); draw(); } }),
    `${c.name} (${c.capacity_m3}m³${c.capacity_kg ? ` · ${Number(c.capacity_kg).toLocaleString()}kg` : ''}, ${c.shift_start}~${c.shift_end}${c.device_id ? '' : ', 휴대폰 미연결'})`)), crewHint);
  const msg = h('p', { class: 'msg' });
  const planMsg = h('p', { class: 'msg' });
  const out = h('div');
  const calc = wrap(async () => {
    if (!current()) return;
    const request = beginRequest();
    s.date = request.date; calcBtn.disabled = true;
    out.replaceChildren(h('div', { class: 'card loading', 'aria-busy': 'true' }, '노선을 계산하는 중입니다 (몇 초 걸립니다)'));
    let res;
    try { res = await post('route', { date: request.date, crew_ids: request.crewIds }); }
    catch (error) { if (requestCurrent(request)) throw error; return; }
    finally { if (current()) syncCalc(); }
    if (!requestCurrent(request)) return;
    s.edit = null;
    s.plan = { date: request.date, crewIds: request.crewIds, res, routes: res.routes.map((r) => ({ crew_id: r.crew_id, crew_name: r.crew_name, stops: r.stops.map((x) => ({ ...x, unload_after: x.unload_after ? { ...x.unload_after } : undefined })), end: r.end, unloads: r.unloads,
      lunch: r.lunch, capacity_m3: r.capacity_m3, capacity_kg: r.capacity_kg, end_load_m3: r.end_load_m3, end_load_kg: r.end_load_kg })),
      unassigned: res.unassigned.map((u) => ({ ...u })) };
    draw();
  }, msg);
  calcBtn.addEventListener('click', async (event) => { await calc(event); if (current()) syncCalc(); }); syncCalc();
  // 날짜를 바꾸면 이전 계산 결과는 그 날짜 것이므로 확정하지 못하게(다시 계산하거나 현재 배정을 불러오게)
  date.addEventListener('change', () => { requestSeq++; s.date = date.value; if (s.edit) { s.edit = null; } draw(); });
  // ---- 확정된 배정 부분 수정 (다시 계산하지 않음): 넣기·빼기·순서·팀 옮기기. 고친 팀마다 새 갱신본 → 그 팀 목록 다시 보내기
  const loadCurrent = wrap(async () => {
    if (!current()) return;
    const request = beginRequest();
    s.date = request.date; s.plan = null;
    out.replaceChildren(h('div', { class: 'card loading', 'aria-busy': 'true' }, '확정된 배정을 불러오는 중입니다'));
    let cur, bs, cand;
    try {
      [cur, bs, cand] = await Promise.all([api('assign/current?date=' + request.date), api('batches?date=' + request.date),
        api('requests?' + new URLSearchParams({ date_field: 'planned', date_from: request.date, date_to: request.date, status: '', quick: 'UNASSIGNED', size: 200, page: 1 }))]);
    } catch (error) { if (requestCurrent(request)) throw error; return; }
    if (!requestCurrent(request)) return;
    const names = {}, received = {}, sent = {};
    bs.filter((b) => b.status === 'ACTIVE').forEach((b) => { b.stops.forEach((x) => { names[x.request_id] = x; }); sent[b.crew_id] = !!b.bundles[0]; received[b.crew_id] = !!(b.bundles[0] && b.bundles[0].receipt_confirmed_at); });
    s.edit = { date: request.date, cur, names, received, sent, cand: cand.rows, reason: (s.edit && s.edit.reason) || '' };
    drawEdit();
  }, msg);
  const drawEdit = () => {
    clearMaps();
    const E = s.edit;
    const editSeq = requestSeq;
    const editCurrent = () => current() && s.edit === E && E.date === date.value && editSeq === requestSeq;
    const vers = Object.fromEntries(E.cur.map((b) => [b.crew_id, b.assign_ver]));
    const crewName = (cid) => (crews.find((c) => c.id === cid) || boot.crews.find((c) => c.id === cid) || { name: cid }).name;
    const editMsg = h('p', { class: 'msg', 'aria-live': 'polite' });
    const staleBox = h('div');
    const why = h('input', { placeholder: '수정 사유 (선택, 예: 주민 요청으로 추가)', value: E.reason, class: 'w-l', 'aria-label': '배정 수정 사유', oninput: (e) => { E.reason = e.target.value; } });
    const op = (body, touched) => wrap(async () => {
      if (!editCurrent()) return;
      const assign_ver = Object.fromEntries(touched.map((c) => [c, vers[c] ?? 0]));
      let r;
      try { r = await post('assign/edit', { date: E.date, assign_ver, reason: why.value, ...body }); }
      catch (e) {
        if (!editCurrent()) return;
        if (isStale(e)) {
          staleBox.replaceChildren(h('div', { class: 'notice bad', role: 'alert' }, h('strong', {}, '배정이 다른 곳에서 바뀌었습니다. '), `${e.data && e.data.crew_id ? crewName(e.data.crew_id) + ' 배정이 ' : ''}다른 화면이나 다른 담당자가 먼저 고쳤습니다. 고치지 않았습니다. `,
            btn('다시 불러오기', loadCurrent, 'small primary')));
          throw new Error('다른 곳에서 바뀌어 고치지 않았습니다. [다시 불러오기]를 누르세요.');
        }
        throw e;
      }
      if (!editCurrent()) return;
      toast(`고쳤습니다. ${r.resend_needed.map(crewName).join(', ')} 목록을 휴대폰에 다시 보내야 합니다 ([목록 전송]에서 QR 표시).`);
      await loadCurrent();
    }, editMsg);
    const R = E.names;
    const label = (x) => { const n = R[x.request_id]; return n ? `${n.name} · ${n.address}` : x.receipt_no; };
    const cols = crews.map((c, ci) => {
      const b = E.cur.find((x) => x.crew_id === c.id);
      const stops = b ? b.stops : [];
      const order = stops.map((x) => x.request_id);
      const swap = (i, d) => { const o = [...order]; [o[i], o[i + d]] = [o[i + d], o[i]]; return op({ op: 'reorder', crew_id: c.id, order: o }, [c.id]); };
      const pick = h('select', { class: 'small', 'aria-label': `${c.name}에 넣을 접수` }, h('option', { value: '' }, E.cand.length ? `넣을 접수 고르기 (${E.cand.length}건)` : '이날 미배정 접수 없음'),
        ...E.cand.map((q) => h('option', { value: q.id }, `${q.receipt_no.replace(/^DG-\d{4}/, '')} ${q.name} · ${q.address}${q.lat == null ? ' (위치 없음)' : ''}`)));
      const pos = h('input', { type: 'number', min: 1, placeholder: '끝', class: 'w-xs', 'aria-label': `${c.name} 넣을 위치(비우면 끝)` });
      return h('div', { class: `card route c${(ci % 4) + 1}` }, h('h2', {}, h('span', { class: 'swatch', 'aria-hidden': 'true' }), `${c.name} · ${stops.length}곳 `,
          b ? chip(`갱신본 ${b.assign_ver}`) : chip('배정 없음', 'neutral'), E.received[c.id] ? chip('휴대폰 수신함', 'info') : E.sent[c.id] ? chip('QR 표시함') : null),
        E.received[c.id] ? h('div', { class: 'notice warn small' }, '이미 휴대폰이 받은 목록입니다. 고치면 수거팀이 QR을 다시 받아야 합니다. 급하면 전화로 먼저 알리세요.') : null,
        ...stops.map((x, i) => h('div', { class: 'stopline' }, h('span', { class: 's' }, i + 1),
          h('span', { class: 't' }, label(x), h('br'), h('span', { class: 'muted' }, `${x.receipt_no}${x.eta ? ' · 도착 ' + x.eta : ''}${x.dong ? ' · ' + x.dong : ''}`), ' ', statusChip(x.status), x.unload_after ? chip('뒤에 하차') : null),
          h('span', { class: 'ctl' }, btn('▲', swap(i, -1), 'small', { 'aria-label': `${c.name} ${i + 1}번 위로`, disabled: i === 0 }), btn('▼', swap(i, 1), 'small', { 'aria-label': `${c.name} ${i + 1}번 아래로`, disabled: i === stops.length - 1 }),
            crews.length > 1 ? h('select', { class: 'small', 'aria-label': `${i + 1}번 다른 팀으로 옮기기`, onchange: (e) => { if (e.target.value) op({ op: 'move', request_id: x.request_id, to_crew_id: e.target.value }, [c.id, e.target.value])(); } },
              h('option', { value: '' }, '옮기기…'), ...crews.filter((o) => o.id !== c.id).map((o) => h('option', { value: o.id }, o.name))) : null,
            btn('빼기', wrap(async () => {
              if (!(await confirmDlg('배정에서 빼기', `${label(x)}을(를) ${c.name} 배정에서 뺍니다. 접수는 남고 미배정이 됩니다.`, { ok: '빼기', danger: true }))) return;
              await op({ op: 'remove', request_id: x.request_id }, [c.id])();
            }), 'small danger', { 'aria-label': `${c.name} ${i + 1}번 빼기` })))),
        h('div', { class: 'row insert-row' }, pick, pos, btn('넣기', (e) => { if (!pick.value) { toast('넣을 접수를 고르세요', true); return; } op({ op: 'insert', crew_id: c.id, request_id: pick.value, position: pos.value || null }, [c.id])(e); }, 'small primary', { 'aria-label': `${c.name}에 넣기` })));
    });
    out.replaceChildren(h('div', { class: 'notice info edit-mode' }, h('strong', {}, `${md(E.date)} 확정 배정 부분 수정 `), '— 다시 계산하지 않고 넣기·빼기·순서·팀 옮기기만 합니다. 도착 예상 시각은 다시 계산되지 않으며, 고친 팀은 목록을 다시 보내야 합니다. ',
        btn('수정 끝내기', () => { s.edit = null; draw(); }, 'small')),
      staleBox, h('div', { class: 'row' }, why, editMsg),
      E.cur.length || E.cand.length ? h('div', { class: 'cols edit-cols' }, ...cols) : h('div', { class: 'card empty-state' }, h('p', { class: 'muted' }, `${md(E.date)}에 확정된 배정이 없습니다. [노선 계산]으로 먼저 확정하세요.`)));
  };
  const draw = () => {
    if (s.edit && s.edit.date === date.value) return drawEdit();
    clearMaps();
    const p = s.plan;
    if (!p) { out.replaceChildren(h('div', { class: 'card empty-state' }, h('p', { class: 'muted' }, '날짜와 팀을 고르고 [노선 계산]을 누르세요. 결과는 추천안이며 순서를 바꿔 확정할 수 있습니다.'))); return; }
    const R = p.res.requests;
    const TL = p.res.tag_labels || {};
    const tagChips = (x) => (x.tags || []).map((t) => chip(TL[t] || t, LOAD_TAG_CLS[t] || ''));
    const loadTxt = (x) => x.load_m3 == null ? '' : ` · 누적 적재(추정) ${x.load_m3}m³${p.res.uses_weight && x.load_kg != null ? ` · ${x.load_kg.toLocaleString()}kg` : ''}`;
    // 누적 적재(추정)는 화면에서 다시 셈: 손으로 순서·팀을 바꾸거나 미배정에서 넣어도 바로 맞게, 차량 용량을 넘는 곳 표시
    p.routes.forEach((rt) => {
      let m = 0, kg = 0; rt.overAt = null;
      rt.stops.forEach((x, i) => {
        const q = R[x.request_id] || {};
        m += Math.round((q.m3 || 0) * 100); kg += q.kg || 0;
        x.load_m3 = m / 100; x.load_kg = kg; if (!x.tags) x.tags = q.tags || [];
        x.over = m > Math.round((rt.capacity_m3 || 8) * 100) || (!!rt.capacity_kg && kg > rt.capacity_kg);
        if (x.over && rt.overAt == null) rt.overAt = i;
        if (x.unload_after) { m = 0; kg = 0; }
      });
      rt.end_load_m3 = m / 100; rt.end_load_kg = kg;
    });
    const clearUnload = (ri) => { if (p.routes[ri].stops.some((x) => x.unload_after)) { p.routes[ri].stops.forEach((x) => delete x.unload_after); p.routes[ri].unloadStale = true; } };
    const move = (ri, i, d) => { const a = p.routes[ri].stops; const j = i + d; if (j < 0 || j >= a.length) return; [a[i], a[j]] = [a[j], a[i]]; clearUnload(ri); p.adjusted = true; draw(); };
    const toCrew = (ri, i, target) => { p.adjusted = true; clearUnload(ri); if (target !== 'OUT') clearUnload(+target); const [x] = p.routes[ri].stops.splice(i, 1); if (target === 'OUT') p.unassigned.push({ request_id: x.request_id, reason: '담당자가 제외' }); else p.routes[+target].stops.push({ request_id: x.request_id, eta: '' }); draw(); };
    const cols = p.routes.map((rt, ri) => h('div', { class: `card route c${(ri % 4) + 1}` }, h('h2', {}, h('span', { class: 'swatch', 'aria-hidden': 'true' }), `${rt.crew_name} · ${rt.stops.length}곳`, rt.end ? h('span', { class: 'muted' }, ` 예상 종료 ${rt.end}`) : null, rt.unloads ? chip(`반입지 경유 ${rt.unloads}회`) : null,
        rt.lunch ? chip(`쉬는 시간 ${rt.lunch.start}부터 ${rt.lunch.minutes}분`) : null),
      rt.unloadStale ? h('div', { class: 'notice warn small' }, '순서를 바꿔 반입지 경유 위치가 지워졌습니다. 적재가 넘치지 않는지 확인하거나 [노선 계산]을 다시 하세요.') : null,
      rt.overAt != null ? h('div', { class: 'notice warn small over-note' }, `${rt.overAt + 1}번부터 누적 적재(추정)가 차량 용량(${rt.capacity_m3 || 8}m³${rt.capacity_kg ? ` · ${rt.capacity_kg.toLocaleString()}kg` : ''})을 넘습니다. 반입지에 들러야 하면 [노선 계산]을 다시 하거나 몇 곳을 다른 팀·다른 날로 옮기세요.`) : null,
      ...rt.stops.flatMap((x, i) => [h('div', { class: 'stopline' }, h('span', { class: 's' }, i + 1),
        h('span', { class: 't' }, `${R[x.request_id].name} · ${R[x.request_id].address}`, h('br'), h('span', { class: 'muted' }, `${x.eta ? '도착 ' + x.eta : '시간 재계산 필요'}${loadTxt(x)}${R[x.request_id].appt[0] ? ` · 약속 ${R[x.request_id].appt[0]}~${R[x.request_id].appt[1] || ''}` : ''}`),
          (R[x.request_id].items || []).length ? h('span', { class: 'stop-items small' }, h('br'), R[x.request_id].items.join(', ')) : null, h('br'),
          ...tagChips(x), x.over ? chip('적재 초과', 'bad') : null,
          R[x.request_id].must_today ? chip('오늘 필수', 'warn') : null, x.same_place > 1 ? chip(`같은 장소 ${x.same_place}건`) : null,
          R[x.request_id].dong ? chip(R[x.request_id].dong) : null,
          R[x.request_id].pref_crew_id && R[x.request_id].pref_crew_id !== rt.crew_id ? chip('동 담당 팀 아님', 'info') : null,
          kakaoLink(R[x.request_id], i + 1)),
        h('span', { class: 'ctl' }, btn('▲', () => move(ri, i, -1), 'small', { 'aria-label': `${i + 1}번 위로`, disabled: i === 0 }), btn('▼', () => move(ri, i, 1), 'small', { 'aria-label': `${i + 1}번 아래로`, disabled: i === rt.stops.length - 1 }),
          h('select', { class: 'small', 'aria-label': '다른 팀으로 이동', onchange: (e) => toCrew(ri, i, e.target.value) }, h('option', { value: '' }, '이동…'), ...p.routes.map((o, oi) => oi !== ri ? h('option', { value: oi }, o.crew_name) : null), h('option', { value: 'OUT' }, '이번 배정에서 빼기')))),
        x.unload_after ? h('div', { class: 'stopline unload' }, h('span', { class: 's' }, '⇩'), h('span', { class: 't' }, `${x.unload_after.label} 하차`, h('br'),
          h('span', { class: 'muted' }, `도착 ${x.unload_after.eta} · 약 ${x.unload_after.volume_m3}m³${p.res.uses_weight && x.unload_kg ? ` · ${x.unload_kg.toLocaleString()}kg` : ''} 비움 · ${x.unload_after.minutes}분`))) : null]),
      rt.stops.length && rt.end_load_m3 ? h('p', { class: 'muted small' }, `마지막에 실려 있는 양(추정) ${rt.end_load_m3}m³${p.res.uses_weight ? ` · ${(rt.end_load_kg || 0).toLocaleString()}kg` : ''}`) : null));
    const noLoc = p.unassigned.filter((u) => R[u.request_id] && R[u.request_id].lat == null).length;
    const un = h('div', { class: 'card' }, h('h2', {}, `미배정 ${p.unassigned.length}건`),
      noLoc ? h('div', { class: 'row' }, btn(`위치 없는 ${noLoc}건 주소 자료로 찾기`, wrap(async () => { const g = await post('geocode/missing', { date: s.date }); toast(`찾음 ${g.found}건 · 못 찾음 ${g.missing}건. 노선을 다시 계산합니다.`); calc(); }, msg), 'small'),
        h('span', { class: 'muted' }, '찾은 위치는 건물 출입구 기준입니다.')) : null, ...p.unassigned.map((u, i) => h('div', { class: 'stopline' },
      h('span', { class: 't' }, R[u.request_id] ? `${R[u.request_id].name} · ${R[u.request_id].address}` : u.request_id, h('br'), h('span', { class: 'muted' }, u.reason)),
      R[u.request_id] && R[u.request_id].lat == null ? btn('위치 지정', () => go(`#/request/${u.request_id}`), 'small') : null,
      R[u.request_id] ? btn('다른 날로 미루기', wrap(async () => {
        const r = await postponeDlg([u.request_id], R[u.request_id].dong, s.date, `${R[u.request_id].name} · ${R[u.request_id].address}`);
        if (!r) return;
        p.unassigned.splice(p.unassigned.indexOf(u), 1); draw();
      }, msg), 'small') : null,
      h('select', { class: 'small', 'aria-label': '팀에 넣기', onchange: (e) => { if (e.target.value === '') return; const [x] = p.unassigned.splice(i, 1); p.routes[+e.target.value].stops.push({ request_id: x.request_id, eta: '' }); p.adjusted = true; draw(); } },
        h('option', { value: '' }, '팀에 넣기…'), ...p.routes.map((o, oi) => h('option', { value: oi }, o.crew_name))))));
    const mapBox = h('div', { class: 'mapbox' });
    const adjReason = h('input', { placeholder: '조정 사유 (예: 골목 진입 불가로 순서 변경)', value: p.adjustReason || '', style: 'flex:1;min-width:280px', oninput: (e) => { p.adjustReason = e.target.value; } });
    const crewStale = crewKey(p.crewIds) !== crewKey(selectedCrews());
    const planStale = p.date !== date.value || crewStale;
    const confirmBtn = btn('배정 확정', wrap(async () => {
      if (!current() || s.plan !== p) return;
      const confirmSeq = requestSeq;
      const planCurrent = () => current() && confirmSeq === requestSeq && s.plan === p && p.date === date.value && crewKey(p.crewIds) === crewKey(selectedCrews());
      if (p.date !== date.value) throw new Error(`이 결과는 ${md(p.date)} 계산 결과입니다. 날짜를 바꿨으면 [노선 계산]을 다시 하세요.`);
      if (crewKey(p.crewIds) !== crewKey(selectedCrews())) throw new Error('수거팀 선택이 바뀌었습니다. [노선 계산]을 다시 하세요.');
      if (p.adjusted && !adjReason.value.trim()) { adjReason.focus(); throw new Error('추천에서 순서·팀을 바꿨습니다. 조정 사유를 적어 주세요.'); }
      // 이미 휴대폰에 보낸(받은) 배정을 다시 확정하면 새 갱신본 → 수거팀이 QR을 다시 받아야 한다
      const bs = await api('batches?date=' + p.date);
      if (!planCurrent()) return;
      const got = bs.filter((b) => b.status === 'ACTIVE' && b.bundles[0] && b.bundles[0].receipt_confirmed_at), shown = bs.filter((b) => b.status === 'ACTIVE' && b.bundles[0] && !b.bundles[0].receipt_confirmed_at);
      if ((got.length || shown.length) && !(await confirmDlg('수거팀이 QR을 다시 받아야 합니다',
        `${[...got, ...shown].map((b) => b.crew_name).join(', ')}에 ${md(p.date)} 목록을 이미 ${got.length ? '보냈고 휴대폰이 받았습니다' : 'QR로 표시했습니다'}. 다시 확정하면 새 갱신본이 생겨 [목록 전송]에서 QR을 다시 보여 줘야 하고, 수거팀은 새 목록을 받아야 합니다. 한두 건만 바꾸려면 [현재 배정 불러오기]가 낫습니다.`, { ok: '다시 확정' }))) return;
      if (!planCurrent()) return;
      const overs = p.routes.filter((rt) => rt.overAt != null);
      if (overs.length && !(await confirmDlg('적재 초과', `${overs.map((rt) => `${rt.crew_name} ${rt.overAt + 1}번부터`).join(', ')} 누적 적재(추정)가 차량 용량을 넘습니다. 그대로 확정할까요? (추정치이므로 현장에서 반입지를 더 들를 수 있습니다)`, { ok: '그대로 확정' }))) return;
      if (!planCurrent()) return;
      const plan = {}; p.routes.forEach((rt) => { plan[rt.crew_id] = rt.stops.map((x) => ({ request_id: x.request_id, eta: x.eta || '', unload_after: x.unload_after || null })); });
      const res = await post('assign', { date: p.date, plan, adjusted: !!p.adjusted, adjust_reason: adjReason.value });
      if (!planCurrent()) return;
      toast(`배정 확정: ${res.map((b) => `${b.count}곳(갱신본 ${b.assign_ver})`).join(', ')}`); s.plan = null; go(`#/send?date=${p.date}`);
    }, planMsg), 'primary', { disabled: planStale });
    const nStops = p.routes.reduce((a, rt) => a + rt.stops.length, 0);
    const lastEnd = p.routes.map((rt) => rt.end).filter(Boolean).sort().pop();
    const summary = p.routes.map((rt) => `${rt.crew_name} ${rt.stops.length}곳`).join(' · ') + (p.unassigned.length ? ` · 미배정 ${p.unassigned.length}건` : '');
    const note = p.res.matrix_source === 'KAKAO' ? p.res.matrix_note : `이동시간: ${(p.res.matrix_note || '').replace('로컬 OSRM', 'PC 안의 도로 자료') || '계산할 지점 없음'}`;
    out.replaceChildren(planStale ? h('div', { class: 'notice bad plan-stale', role: 'alert' }, h('strong', {}, `${md(p.date)} 계산 결과입니다. `), crewStale ? '수거팀 선택을 바꿨으므로 이 결과로는 확정할 수 없습니다. [노선 계산]을 다시 하세요.' : `수거일을 ${md(date.value)}로 바꿨으므로 이 결과로는 확정할 수 없습니다. [노선 계산]을 다시 하거나 날짜를 되돌리세요.`) : null,
      h('div', { class: 'notice matrix-note' }, note, p.res.unload_site ? ` · 적재가 차면 ${p.res.unload_site.label || '반입지'}에 들러 비움` : ' · 반입지 좌표가 없어 차량 1회 적재 안에서만 계산 (설정에서 지정)', p.res.end_site ? ` · 마지막에 ${p.res.end_site.label || '종료지'}로` : '', p.res.infeasible ? h('strong', {}, ' · 해를 찾지 못했습니다. 차량·근무시간·필수 포함 조건을 조정하세요.') : null),
      daySummaryCard(p.res),
      h('div', { class: 'kpis compact' }, ...[['배정 팀', `${p.routes.length}팀`, ''], ['방문 지점', `${nStops}곳`, ''], ['미배정', `${p.unassigned.length}건`, p.unassigned.length ? 'warn' : 'ok'],
        ['반입지 경유', `${p.routes.reduce((a, rt) => a + (rt.unloads || 0), 0)}회`, ''], ['마지막 예상 종료', lastEnd || '—', '']].map(([k, v, c]) => h('div', { class: 'kpi ' + c }, h('span', { class: 'k' }, k), h('b', { class: 'v' }, v)))),
      h('div', { class: 'dispatch-grid' }, h('div', { class: 'mapcol' }, h('div', { class: 'card mapcard' }, h('div', { class: 'panel-h' }, h('h2', {}, '지도'), h('span', { class: 'legend small' }, ...p.routes.map((rt, ri) => h('span', { class: `lg route c${(ri % 4) + 1}` }, rt.crew_name)))), mapBox,
        h('p', { class: 'muted small' }, '선은 방문 순서를 잇는 직선이며 실제 도로가 아닙니다. 핀 위치와 이름은 지도 서버로 보내지 않습니다.'))),
        h('div', { class: 'routecol' }, h('div', { class: 'routecol-h' }, h('b', {}, '팀별 방문 순서'), h('span', { class: 'muted small' }, '▲▼로 순서 변경, [이동…]으로 팀 변경')), h('div', { class: 'cols' }, ...cols), un)),
      h('div', { class: 'actionbar' }, h('b', {}, summary), p.adjusted ? adjReason : h('span', { class: 'muted' }, '추천 순서 그대로 확정합니다.'), h('span', { class: 'grow' }), planMsg, confirmBtn));
    setTimeout(async () => {
      try {
        const { map, ml } = await mapIn(mapBox, { center: [boot.settings.depot.lon, boot.settings.depot.lat], zoom: 13 });
        const b = new ml.LngLatBounds(); b.extend([boot.settings.depot.lon, boot.settings.depot.lat]);
        M.pin(ml, map, { lng: boot.settings.depot.lon, lat: boot.settings.depot.lat, label: '출발', cls: 'me' });
        const lines = [];
        p.routes.forEach((rt, ri) => {
          const coords = [[boot.settings.depot.lon, boot.settings.depot.lat]];
          rt.stops.forEach((x, i) => { const q = R[x.request_id]; if (q.lat == null) return; coords.push([q.lon, q.lat]); b.extend([q.lon, q.lat]); M.pin(ml, map, { lng: q.lon, lat: q.lat, label: `${ri + 1}-${i + 1}`, cls: 'c' + ((ri % 4) + 1) }); });
          lines.push({ type: 'Feature', properties: { c: ri % 4 }, geometry: { type: 'LineString', coordinates: coords } });
        });
        p.unassigned.forEach((u) => { const q = R[u.request_id]; if (q && q.lat != null) { b.extend([q.lon, q.lat]); M.pin(ml, map, { lng: q.lon, lat: q.lat, label: '미', cls: 'un' }); } });
        map.fitBounds(b, { padding: 50, maxZoom: 16, duration: 0 });  // 바탕 지도가 늦어도 핀은 모두 보이게
        const whenStyle = (f) => (map.isStyleLoaded() ? f() : map.once('style.load', f));
        whenStyle(() => {
          map.addSource('routes', { type: 'geojson', data: { type: 'FeatureCollection', features: lines } });
          map.addLayer({ id: 'routes', type: 'line', source: 'routes', paint: { 'line-width': 3, 'line-dasharray': [2, 1], 'line-color': ['match', ['get', 'c'], 0, '#2557a7', 1, '#a5560d', 2, '#6c3fb5', '#0e7490'] } });
        });
      } catch { mapBox.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다.')); }
    }, 0);
  };
  draw();
  view = h('div', { class: 'dispatch' }, h('h1', {}, '배정·노선'),
    h('div', { class: 'card searchbox', role: 'group', 'aria-label': '조회조건' },
      h('div', { class: 'cond' }, h('label', { class: 'cond-l', for: 'dp-date' }, '수거일'), h('div', { class: 'cond-v' }, date),
        h('span', { class: 'cond-l' }, '수거팀'), h('div', { class: 'cond-v wide' }, crewBox)),
      h('div', { class: 'cond-btns' }, calcBtn, ['ADMIN', 'INTAKE'].includes(boot.me.role) ? btn('현재 배정 불러오기', loadCurrent, '', { title: '이미 확정한 배정에서 한두 건만 넣기·빼기·순서·팀 옮기기 (다시 계산하지 않음)' }) : null),
      crews.length ? null : h('p', { class: 'err' }, '설정에서 수거팀을 먼저 만드세요.'), msg),
    out);
  return view;
}

// 다른 날로 미루기: 날짜 직접 또는 가장 빠른 가능일(동 수거 요일·한도·휴무일 반영) + 사유 코드
async function postponeDlg(ids, dong, fromDate, label) {
  const d = h('input', { type: 'date' });
  const hint = h('span', { class: 'muted small' });
  const next = new Date(fromDate + 'T00:00:00'); next.setDate(next.getDate() + 1);
  const start = next.toISOString().slice(0, 10) > boot.today ? next.toISOString().slice(0, 10) : boot.today;
  const pick = wrap(async () => {
    const c = await api(`calendar?${new URLSearchParams({ start, days: 1, dong: dong || '' })}`);
    if (!c.earliest) throw new Error('120일 안에 여유 있는 날이 없습니다.');
    d.value = c.earliest; hint.textContent = `${dong || '전체'} 기준 가장 빠른 가능일`;
  });
  pick();
  const r = await reasonDlg('다른 날로 미루기', boot.postpone_codes, { ok: '미루기', code: 'OVER_CAPACITY', label: '미루는 사유',
    lead: [h('p', {}, label)], before: [h('label', { class: 'f' }, '새 수거 예정일', d), h('div', { class: 'row' }, btn('가장 빠른 가능일', pick, 'small'), hint)],
    submit: (v) => { if (!d.value) throw new Error('날짜를 고르세요.'); return post('requests/bulk', { action: 'reschedule', ids, date: d.value, ...v }); } });
  if (r) { (r.warnings || []).forEach((w) => toast(w, true)); toast(`${d.value}로 미뤘습니다`); }
  return r;
}

// ------------------------------------------------------------ 전송

export {
  LOAD_TAG_CLS, daySummaryCard, kakaoLink, postponeDlg, screenDispatch,
};
