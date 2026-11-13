// PC 화면: 설정(기본·수거팀·기기·행정동·한도·휴무일·수수료/감면·주소·백업·개인정보)
import * as M from '/web/map.js';
import { api, boot, btn, chip, confirmDlg, download, grid, h, isPlaceholderContact, kst, modal, pagehead, post, postOnce, render, runJob, sessionOwner, setBoot, toast, wrap } from '../core.js';
import { mapIn, purgeLogTable, webcamReadOnce } from '../widgets.js';

const WD = '월화수목금토일';
let dongOrderGeneration = 0;
const dongRowDrafts = new WeakMap();
const dongRefreshGenerations = new WeakMap();
const dongCenterResults = new WeakMap();
const dongCenterStates = new WeakMap();
function dayChecks(cur, label) {
  const boxes = [...WD].map((t, i) => h('input', { type: 'checkbox', checked: cur.includes(i), 'aria-label': `${label} ${t}요일` }));
  return { el: h('span', { class: 'wdays' }, ...boxes.map((b, i) => h('label', {}, b, WD[i]))), get: () => boxes.map((b, i) => (b.checked ? i : null)).filter((x) => x != null) };
}
function dongCard() {
  const msg = h('p', { class: 'msg' });
  const owner = `${boot.me?.username || ''}|${boot.me?.role || ''}`;
  const authOwner = sessionOwner();
  if (!dongRowDrafts.has(authOwner)) dongRowDrafts.set(authOwner, new Map());
  const drafts = dongRowDrafts.get(authOwner);
  if (!dongRefreshGenerations.has(authOwner)) dongRefreshGenerations.set(authOwner, { value: 0 });
  const refreshGeneration = dongRefreshGenerations.get(authOwner);
  if (!dongCenterStates.has(authOwner)) dongCenterStates.set(authOwner, { phase: 'idle', rowWrites: 0, views: new Set() });
  const centerState = dongCenterStates.get(authOwner);
  const alive = () => card?.isConnected && !!boot?.me && authOwner === sessionOwner();
  const tbody = h('tbody'), models = new Map(), rowButtons = [];
  const frozenInputs = new Map();
  const centerStatus = h('p', { class: 'small', role: 'status', 'aria-live': 'polite' });
  let order = boot.dongs.map((d) => d.id), savedOrder = [...order], saving = false, rowBusy = false;
  let card, newRow, centerBulk;
  const centerBlocked = () => centerState.phase !== 'idle';
  function notifyCenterViews() {
    for (const view of centerState.views) {
      if (view.alive()) view.updateControls(); else centerState.views.delete(view);
    }
  }
  const dirty = () => order.some((id, i) => id !== savedOrder[i]);
  const orderHint = h('p', { class: 'muted small' });
  const orderMsg = h('p', { class: 'msg', role: 'status', 'aria-live': 'polite' });
  const orderSave = btn('순서 저장', saveOrder, 'small primary');
  const orderReset = btn('순서 되돌리기', () => {
    if (saving || rowBusy || centerBlocked()) return;
    order = [...savedOrder]; clearOrderMessage(); draw();
    models.get(order[0])?.down.focus();
  }, 'small');
  function clearOrderMessage() {
    orderMsg.textContent = ''; orderMsg.className = 'msg'; orderMsg.setAttribute('role', 'status');
  }
  function updateControls() {
    const blocked = saving || rowBusy || centerBlocked();
    orderSave.disabled = blocked || !dirty(); orderReset.disabled = blocked || !dirty();
    orderHint.textContent = saving ? '동 순서를 저장하고 있습니다.' : dirty()
      ? '변경한 순서는 아직 저장하지 않았습니다. 순서를 저장하거나 되돌린 뒤 다른 항목을 저장하세요.'
      : '위·아래 버튼으로 직제순을 맞춘 뒤 순서 저장을 누르세요. 동별 관리·접수·담당 동 선택에 함께 적용됩니다.';
    order.forEach((id, i) => {
      const model = models.get(id);
      model.position.textContent = String(i + 1);
      model.up.disabled = blocked || i === 0; model.down.disabled = blocked || i === order.length - 1;
    });
    rowButtons.forEach((button) => { button.disabled = blocked || dirty() || !!button.dataset.busy; });
    if (centerBlocked()) {
      tbody.querySelectorAll('input,select,textarea').forEach((control) => {
        if (!frozenInputs.has(control)) frozenInputs.set(control, control.disabled);
        control.disabled = true;
      });
    } else {
      frozenInputs.forEach((disabled, control) => { control.disabled = disabled; });
      frozenInputs.clear();
    }
    if (centerBulk) {
      centerBulk.textContent = centerState.phase === 'uncertain' ? '적용 결과 다시 확인' : '행정복지센터 차고지 일괄 적용';
      centerBulk.disabled = saving || rowBusy || !!centerState.rowWrites || dirty() || ['saving', 'refresh'].includes(centerState.phase);
    }
    centerStatus.textContent = centerState.phase === 'saving' ? '차고지를 적용하고 있습니다. 결과 확인 전까지 동 설정을 변경할 수 없습니다.'
      : centerState.phase === 'refresh' ? '적용한 차고지를 다시 불러오고 있습니다.'
      : centerState.phase === 'uncertain' ? `적용 결과를 확인하지 못했습니다. [적용 결과 다시 확인]을 누른 뒤 동 설정을 변경하세요. ${centerState.error || ''}` : '';
  }
  function draw() {
    tbody.append(...order.map((id) => models.get(id).row), newRow);
    updateControls();
  }
  function move(id, delta) {
    if (saving || rowBusy || centerBlocked()) return;
    const at = order.indexOf(id), next = at + delta;
    if (at < 0 || next < 0 || next >= order.length) return;
    [order[at], order[next]] = [order[next], order[at]];
    clearOrderMessage(); draw();
    const model = models.get(id), preferred = delta < 0 ? model.up : model.down;
    (preferred.disabled ? (delta < 0 ? model.down : model.up) : preferred).focus();
  }
  async function saveOrder() {
    if (!alive() || saving || rowBusy || centerBlocked() || !dirty()) return;
    const generation = ++dongOrderGeneration, requestBoot = boot;
    refreshGeneration.value++;
    saving = true; centerState.rowWrites++; clearOrderMessage(); orderSave.setAttribute('aria-busy', 'true'); updateControls(); notifyCenterViews();
    try {
      const result = await api('dongs/order', { method: 'POST', body: { ids: [...order], base_ids: [...savedOrder] }, _prefsOwner: authOwner });
      if (authOwner !== sessionOwner() || `${boot?.me?.username || ''}|${boot?.me?.role || ''}` !== owner || generation !== dongOrderGeneration) return;
      if (!card.isConnected) {
        if (boot !== requestBoot) return;
        const positions = new Map(result.dongs.map((d, i) => [d.id, i]));
        if (boot.dongs.length === positions.size && boot.dongs.every((d) => positions.has(d.id))) {
          setBoot({ ...boot, dongs: boot.dongs.map((d) => ({ ...d, sort: positions.get(d.id) })).sort((a, b) => a.sort - b.sort) });
        }
        return;
      }
      setBoot({ ...boot, dongs: result.dongs });
      savedOrder = result.dongs.map((d) => d.id);
      orderMsg.textContent = '동 순서를 저장했습니다.';
      orderMsg.className = 'notice ok-note';
    } catch (error) {
      if (card.isConnected) { orderMsg.textContent = error.message; orderMsg.className = 'err'; orderMsg.setAttribute('role', 'alert'); }
    } finally {
      saving = false; centerState.rowWrites--; orderSave.removeAttribute('aria-busy'); updateControls(); notifyCenterViews();
    }
  }
  const rowAction = (fn) => wrap(async () => {
    if (!alive() || saving || rowBusy || centerBlocked() || dirty()) return;
    rowBusy = true; centerState.rowWrites++; updateControls(); notifyCenterViews();
    try { await fn(); } finally { rowBusy = false; centerState.rowWrites--; updateControls(); notifyCenterViews(); }
  }, msg);
  const refreshDongs = async ({ center = false } = {}) => {
    if (!boot?.me || authOwner !== sessionOwner()) return false;
    const generation = ++refreshGeneration.value, requestDongs = boot.dongs;
    const dongs = await api('dongs', { timeoutMs: 15000, _prefsOwner: authOwner });
    if (!boot?.me || authOwner !== sessionOwner() || generation !== refreshGeneration.value || boot.dongs !== requestDongs) return false;
    setBoot({ ...boot, dongs });
    // 다른 hash에서 시작한 작업의 자동 렌더 생략 규칙은 이 동 카드의 재조회에 적용하지 않는다.
    if (center) {
      if ([...centerState.views].some((view) => view.alive())) await render(true);
    } else if (card?.isConnected) await render();
    return true;
  };
  const refreshSaved = async () => {
    try { await refreshDongs(); }
    catch (error) {
      if (alive()) msg.replaceChildren('저장은 완료했습니다. 목록을 다시 불러오지 못했습니다. ', btn('저장 목록 다시 불러오기', wrap(refreshDongs, msg), 'small'));
    }
  };
  [...boot.dongs, { id: '', name: '', active: true, crew_id: '', weekdays: [], daily_limit: null, daily_limit_m3: null, keywords: [] }].forEach((d) => {
    const lbl = d.name || '새 동';
    const draftKey = d.id || '__new__';
    const baseline = { name: d.name, code: d.code || '', active: !!d.active, crew_id: d.crew_id || '', weekdays: d.weekdays || [],
      daily_limit: String(d.daily_limit ?? ''), daily_limit_m3: String(d.daily_limit_m3 ?? ''), keywords: (d.keywords || []).join(', '), depot: d.depot || null };
    const initial = drafts.get(draftKey) || baseline;
    const f = { name: h('input', { value: initial.name, placeholder: '새 동 이름 (예: ○○1동)', 'aria-label': `${lbl} 이름`, class: 'w-s' }),
      code: h('input', { value: initial.code, placeholder: '코드', 'aria-label': `${lbl} 부과번호 코드`, class: 'w-xs', maxlength: 4 }), active: h('input', { type: 'checkbox', checked: initial.active, 'aria-label': `${lbl} 사용` }),
      crew: h('select', { 'aria-label': `${lbl} 기본 담당 팀` }, h('option', { value: '' }, '(없음)'), ...boot.crews.map((c) => h('option', { value: c.id, selected: c.id === initial.crew_id }, c.name))),
      days: dayChecks(initial.weekdays, lbl), limit: h('input', { type: 'number', min: 1, value: initial.daily_limit, placeholder: '건', 'aria-label': `${lbl} 일일 한도 건수`, class: 'w-xs' }),
      m3: h('input', { type: 'number', step: '0.5', min: 0, value: initial.daily_limit_m3, placeholder: 'm³', 'aria-label': `${lbl} 일일 한도 m³`, class: 'w-xs' }),
      kw: h('input', { value: initial.keywords, placeholder: '법정동·도로명 (쉼표로)', 'aria-label': `${lbl} 주소 판별 낱말`, class: 'w-l' }) };
    f.depot = initial.depot ? { ...initial.depot } : null;
    f.depotDirty = JSON.stringify(f.depot) !== JSON.stringify(baseline.depot);
    const values = () => ({ name: f.name.value, code: f.code.value, active: f.active.checked, crew_id: f.crew.value, weekdays: f.days.get(),
      daily_limit: f.limit.value, daily_limit_m3: f.m3.value, keywords: f.kw.value, depot: f.depot });
    const remember = () => {
      // 제출 후 원래 값/공통 차고지로 되돌린 입력도 사용자의 새 초안이다.
      drafts.set(draftKey, values());
    };
    [f.name, f.code, f.active, f.crew, f.limit, f.m3, f.kw, ...f.days.el.querySelectorAll('input')].forEach((el) => {
      el.addEventListener('input', remember); el.addEventListener('change', remember);
    });
    const depotStatus = h('span', { class: 'muted small', 'aria-label': `${lbl} 차고지 현황` });
    const drawDepot = () => { depotStatus.textContent = f.depot ? `${f.depot.label} · ${f.depot.lat}, ${f.depot.lon}${f.depotDirty ? ' · 저장 전' : ''}` : `공통 차고지 사용${f.depotDirty ? ' · 저장 전' : ''}`; };
    const depotButton = btn('차고지 설정', async () => {
      if (saving || rowBusy || centerBlocked() || dirty()) return;
      const alive = () => card?.isConnected && !!boot?.me && authOwner === sessionOwner();
      if (!alive()) return;
      const label = h('input', { value: f.depot?.label || '', maxlength: 80, placeholder: `${f.name.value || lbl} 차고지`, 'aria-label': '차고지 이름' });
      const lat = h('input', { value: f.depot?.lat ?? '', inputmode: 'decimal', 'aria-label': '차고지 위도' });
      const lon = h('input', { value: f.depot?.lon ?? '', inputmode: 'decimal', 'aria-label': '차고지 경도' });
      const center = d.center_depot;
      const result = await modal({ title: `${lbl} 차고지 설정`, ok: '행에 적용', body: [
        h('p', { class: 'muted' }, '이 동의 담당팀이 출발하고 돌아올 실제 위치를 지정하세요. 적용한 뒤 행의 [저장]을 눌러야 반영됩니다.'),
        center ? h('div', { class: 'notice info' }, h('strong', {}, center.label), h('p', { class: 'small' }, center.address),
          btn('행정복지센터 위치 채우기', () => { lat.value = center.lat; lon.value = center.lon; label.value = center.label; }, 'small primary'),
          h('p', { class: 'small' }, `공식 지도 표식 · 확인일 ${center.checked_on} · 차량 출입 위치는 필요하면 조정하세요. `, h('a', { href: center.source, target: '_blank', rel: 'noopener noreferrer' }, '공식 안내'))) : null,
        h('label', { class: 'f' }, '차고지 이름', label),
        h('div', { class: 'grid2' }, h('label', { class: 'f' }, '차고지 위도', lat), h('label', { class: 'f' }, '차고지 경도', lon)),
        coordTools(lat, lon, `${lbl} 차고지`, label),
        h('p', { class: 'muted small' }, '동별 차고지는 기본 담당팀을 고정해 계산합니다. 별도 종료지가 설정되어 있으면 그곳으로 종료합니다.'),
        btn('공통 차고지 사용', () => { lat.value = ''; lon.value = ''; label.value = ''; lat.focus(); }, 'small'),
        h('p', { class: 'muted small' }, '위도·경도를 모두 비우면 기존 공통 차고지를 사용합니다.')],
        onOk: () => {
          if (!alive() || centerBlocked()) throw new Error('화면이나 설정 처리 상태가 바뀌었습니다. 다시 열어 주세요.');
          if (!lat.value.trim() && !lon.value.trim()) return { depot: null };
          if (!lat.value.trim() || !lon.value.trim()) throw new Error('차고지 위도와 경도를 모두 입력해 주세요.');
          const y = Number(lat.value), x = Number(lon.value);
          if (!Number.isFinite(y) || !Number.isFinite(x) || y < 33 || y > 39 || x < 124 || x > 132) throw new Error('차고지 좌표가 국내 범위를 벗어났습니다.');
          return { depot: { lat: y, lon: x, label: label.value.trim() || `${f.name.value || lbl} 차고지` } };
        } });
      if (result && alive()) { f.depot = result.depot; f.depotDirty = true; remember(); drawDepot(); }
    }, 'small', { 'aria-label': `${lbl} 차고지 설정` });
    drawDepot(); rowButtons.push(depotButton);
    const save = btn(d.id ? '저장' : '추가', rowAction(async () => {
        const sent = values(), signature = JSON.stringify(sent);
        await api('dongs', { method: 'POST', body: { id: d.id || null, ...sent }, _prefsOwner: authOwner });
        if (!boot?.me || authOwner !== sessionOwner()) return;
        // 다른 화면에서 다시 편집한 같은 행의 초안은 옛 제출 응답으로 지우지 않는다.
        if (JSON.stringify(drafts.get(draftKey)) === signature) drafts.delete(draftKey);
        if (alive()) toast('저장했습니다');
        await refreshSaved();
      }), 'small primary');
    const remove = d.id ? btn('삭제', rowAction(async () => {
        if (!(await confirmDlg('동 삭제', `${d.name}을(를) 지웁니다. 이 동으로 접수된 건이 있으면 지울 수 없고 '사용'을 끄면 됩니다.`, { ok: '삭제', danger: true }))) return;
        if (!alive()) return;
        await api(`dongs/${d.id}/delete`, { method: 'POST', _prefsOwner: authOwner });
        if (!boot?.me || authOwner !== sessionOwner()) return;
        drafts.delete(draftKey);
        if (alive()) toast('지웠습니다');
        await refreshSaved();
      }), 'small danger') : null;
    rowButtons.push(save, ...(remove ? [remove] : []));
    const position = h('span', { class: 'code', 'aria-label': `${lbl} 현재 순서` });
    const up = btn('↑', () => move(d.id, -1), 'small', { 'aria-label': `${lbl} 위로 이동`, title: '위로 이동' });
    const down = btn('↓', () => move(d.id, 1), 'small', { 'aria-label': `${lbl} 아래로 이동`, title: '아래로 이동' });
    const row = h('tr', { 'data-dong-id': d.id || null }, h('td', { class: 'nowrap' }, d.id ? [position, ' ', up, ' ', down] : h('span', { class: 'muted' }, '새 동')),
      h('td', {}, f.name), h('td', {}, f.code), h('td', { class: 'c' }, f.active), h('td', {}, f.crew), h('td', {}, depotButton, h('br'), depotStatus), h('td', {}, f.days.el),
      h('td', {}, f.limit), h('td', {}, f.m3), h('td', {}, f.kw), h('td', { class: 'nowrap' }, save, remove));
    if (d.id) models.set(d.id, { row, position, up, down }); else newRow = row;
  });
  draw();
  const centerResult = dongCenterResults.get(authOwner);
  const centerFeedback = centerResult ? h('div', { class: 'notice info', role: 'status' }, `행정복지센터 차고지 ${centerResult.applied}개 적용 · 기존 설정 유지 또는 확인 필요 ${centerResult.skipped.length}개`,
    centerResult.skipped.length ? h('details', {}, h('summary', {}, '미적용 내역 확인'), h('ul', {}, ...centerResult.skipped.map((row) => h('li', {}, `${row.name}: ${row.reason}`)))) : null) : null;
  centerBulk = btn('행정복지센터 차고지 일괄 적용', wrap(async () => {
    if (!alive() || saving || rowBusy || centerState.rowWrites || dirty() || ['saving', 'refresh'].includes(centerState.phase)) return;
    if (centerState.phase === 'idle') {
      if ([...drafts.keys()].some((key) => key === '__new__' || models.has(key))) throw new Error('작성 중인 동 설정을 먼저 저장한 뒤 일괄 적용하세요.');
      centerState.requestId = crypto.randomUUID(); centerState.sentAt = Date.now(); centerState.ack = false;
    }
    centerState.phase = centerState.ack ? 'refresh' : 'saving'; centerState.error = ''; notifyCenterViews();
    try {
      if (!centerState.ack) {
        if (Date.now() - centerState.sentAt >= 24 * 60 * 60 * 1000) throw new Error('이전 요청의 확인 기간(24시간)이 지났습니다. 기존 설정을 관리자에게 확인해 주세요.');
        const result = await api('dongs/centers', { method: 'POST', body: { client_req_id: centerState.requestId }, _prefsOwner: authOwner });
        if (!boot?.me || sessionOwner() !== authOwner) return;
        centerState.ack = true; dongCenterResults.set(authOwner, result);
        centerState.phase = 'refresh'; notifyCenterViews();
      }
      if (!(await refreshDongs({ center: true }))) throw new Error('동 목록이 바뀌었습니다. 다시 확인해 주세요.');
      centerState.phase = 'idle';
      if ([...centerState.views].some((view) => view.alive())) toast(`행정복지센터 차고지 ${dongCenterResults.get(authOwner).applied}개 적용 · 기존 차고지는 보존했습니다.`);
    } catch (error) {
      centerState.phase = 'uncertain'; centerState.error = error.message;
    } finally { notifyCenterViews(); }
  }, msg), 'small primary');
  card = h('div', { class: 'card' }, h('h2', {}, '행정동 관리'),
    h('p', { class: 'muted' }, '동구 기본 행정동은 자동 등록됩니다. 동별 담당 팀·차고지·수거 요일·한도를 확인하세요. 차고지를 지정하면 해당 팀으로 고정 배정하며, 미지정 동은 공통 차고지를 사용합니다.'),
    h('div', { class: 'notice info' }, h('strong', {}, '동마다 담당팀 1개 · 행정복지센터 차고지'),
      h('p', { class: 'small' }, '공식 센터 13곳의 위치를 제공합니다. 저장된 담당팀이 있는 동의 비어 있는 차고지만 채웁니다. 기존 차고지와 담당팀은 유지하며, 팀 미연결·중복 연결·사용 중지 동은 미적용 내역에서 확인하세요.'), centerBulk, centerStatus), centerFeedback,
    h('div', { class: 'row' }, orderSave, orderReset), orderHint, orderMsg,
    h('p', { class: 'muted' }, '코드는 부과번호(연도-동코드-일련번호)에 쓰는 숫자·영문 1~4자입니다(비우면 00). 접수 화면은 주소에 판별 낱말(법정동 이름·도로명 등)이 있거나 주소 위치 자료의 관할 행정동이 같으면 동을 자동으로 고릅니다. 수거 요일을 비우면 쉬는 요일 말고 매일입니다. 차고지 미지정 동의 기본 담당 팀은 노선 계산에서 우선(약한 선호)합니다. 바꾼 내용은 변경 이력에 남습니다.'),
    h('div', { class: 'tablewrap' }, h('table', { class: 'dongedit grid' }, h('thead', {}, h('tr', {}, ...['순서', '동', '코드', '사용', '기본 담당 팀', '차고지', '수거 요일', '한도(건)', '한도(m³)', '주소 판별 낱말', ''].map((t) => h('th', {}, t)))), tbody)), msg);
  const centerView = { alive, updateControls };
  centerState.views.add(centerView);
  updateControls();
  setTimeout(() => {
    if (alive()) { centerState.views.add(centerView); notifyCenterViews(); }
    else centerState.views.delete(centerView);
  }, 0);
  return card;
}
// 날짜별 수거 한도·쉬는 요일·휴무일
function capacityCard() {
  const c = boot.settings.capacity;
  const msg = h('p', { class: 'msg' });
  const lim = h('input', { type: 'number', min: 1, value: c.daily_count ?? '', placeholder: '없음', class: 'w-xs', 'aria-label': '전체 일일 한도 건수' });
  const m3 = h('input', { type: 'number', step: '0.5', min: 0, value: c.daily_m3 ?? '', placeholder: '없음', class: 'w-xs', 'aria-label': '전체 일일 한도 m³' });
  const closed = dayChecks(c.closed_weekdays, '쉬는 요일');
  const pref = h('input', { type: 'checkbox', checked: c.prefer_dong_crew });
  return h('div', { class: 'card' }, h('h2', {}, '수거 한도'),
    h('div', { class: 'row' }, '전체 일일 한도', lim, '건 ·', m3, 'm³', h('span', { class: 'muted small' }, '비우면 한도 없음. 넘어도 막지 않고 접수 화면에서 경고합니다. 동별 한도는 [행정동] 탭에서 정합니다.')),
    h('div', { class: 'row' }, '쉬는 요일', closed.el),
    h('label', { class: 'row' }, pref, '차고지 미지정 동의 기본 담당 팀 우선 (약한 선호)'),
    btn('저장', wrap(async () => { await post('settings', { capacity: { daily_count: lim.value, daily_m3: m3.value, closed_weekdays: closed.get(), prefer_dong_crew: pref.checked } }); toast('저장했습니다'); setBoot(null); render(); }, msg), 'primary'), msg);
}
function holidayCard() {
  const msg = h('p', { class: 'msg' });
  const list = h('tbody');
  const hd = h('input', { type: 'date', 'aria-label': '휴무일' }), hl = h('input', { placeholder: '이름 (예: 개교기념일, 시설 점검)', class: 'w-m', 'aria-label': '휴무일 이름' });
  const load = wrap(async () => {
    const hs = await api(`holidays?start=${boot.today.slice(0, 4)}-01-01`);
    list.replaceChildren(...(hs.length ? hs.map((x) => h('tr', {}, h('td', { class: 'code' }, x.day), h('td', {}, x.label), h('td', {}, btn('지우기', wrap(async () => { await post(`holidays/${x.day}/delete`); load(); }, msg), 'small'))))
      : [h('tr', {}, h('td', { colspan: 3, class: 'muted' }, '등록한 휴무일이 없습니다. 공휴일은 미리 들어 있지 않으니 담당자가 넣으세요.'))]));
  }, msg);
  load();
  return h('div', { class: 'card' }, h('h2', {}, '휴무일·공휴일'), h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['날짜', '이름', ''].map((t) => h('th', {}, t)))), list)),
    h('div', { class: 'row' }, hd, hl, btn('휴무일 추가', wrap(async () => { await post('holidays', { day: hd.value, label: hl.value }); hd.value = ''; hl.value = ''; toast('추가했습니다'); load(); }, msg), 'small primary')), msg);
}
// 접수 시 안내: 품목 낱말이 맞으면 접수 화면에 안내문 (예: 폐가전 무상수거)
function noticeCard() {
  const msg = h('p', { class: 'msg' });
  const rowsF = [...(boot.settings.intake_notices || []), { keywords: [], text: '' }].map((n) => ({ kw: h('input', { value: n.keywords.join(', '), placeholder: '품목 낱말 (예: 냉장고, 세탁기)', class: 'w-l', 'aria-label': '안내 품목 낱말' }),
    text: h('input', { value: n.text, placeholder: '안내문 (비우면 지움)', class: 'fill', 'aria-label': '안내문' }) }));
  return h('div', { class: 'card' }, h('h2', {}, '접수 시 안내'),
    h('p', { class: 'muted' }, '접수 화면에서 고른 품목 이름에 낱말이 들어 있으면 안내문을 보여 줍니다. 예: "냉장고, 세탁기, 에어컨" → "폐가전 무상 방문수거 서비스를 먼저 안내하세요."'),
    ...rowsF.map((f) => h('div', { class: 'row' }, f.kw, f.text)),
    btn('저장', wrap(async () => { await post('settings', { intake_notices: rowsF.map((f) => ({ keywords: f.kw.value, text: f.text.value })) }); toast('저장했습니다'); setBoot(null); render(); }, msg), 'primary'), msg);
}

// 적재량 보정: 품목별 추정 부피와 담당자 보정값을 따로 관리 (노선 계산의 적재 제약에 사용)
function volumeCard() {
  const tbody = h('tbody');
  const msg = h('span', { class: 'msg' });
  api('volumes').then((list) => tbody.replaceChildren(...(list.length ? list.map((v) => {
    const inp = h('input', { type: 'number', step: '0.1', min: 0, value: v.override ?? '', placeholder: '보정값', style: 'width:90px' });
    return h('tr', {}, h('td', {}, `${v.name} ${v.spec}`), h('td', {}, `${v.estimate} m³/${v.unit_label}`), h('td', {}, v.kg != null ? `${v.kg} kg` : ''),
      h('td', {}, ...(v.tags || []).map((t) => chip(t))), h('td', {}, inp, ' m³'),
      h('td', {}, btn('저장', wrap(async () => { await post('volumes', { fee_rate_id: v.fee_rate_id, m3: inp.value === '' ? null : +inp.value }); toast(inp.value === '' ? '보정값을 지웠습니다(추정치 사용)' : '보정값을 저장했습니다'); }, msg), 'small')));
  }) : [h('tr', {}, h('td', { colspan: 6, class: 'muted' }, '접수에 쓰인 품목이 생기면 여기에 나타납니다.'))]))).catch(() => {});
  const chk = h('div', { class: 'load-check' });
  api('load-check').then((r) => {
    if (!r.days.length) { chk.replaceChildren(h('p', { class: 'muted small' }, '최근 30일 하차 기록이 없어 추정치와 비교할 수 없습니다. 반입지 계근표 중량·부피를 하차 기록에 남기면 여기서 비교합니다.')); return; }
    const pct = (v) => (v == null ? '—' : `${v}%`);
    chk.replaceChildren(h('h3', {}, `추정치 ↔ 하차 기록 (${r.start} ~ ${r.end})`),
      h('p', {}, `추정 ${r.est_m3}m³ · ${r.est_kg.toLocaleString()}kg ↔ 하차 기록 ${r.rec_m3}m³ · ${r.rec_kg.toLocaleString()}kg  (부피 ${pct(r.m3_pct)} · 중량 ${pct(r.kg_pct)})`),
      h('p', { class: 'muted small' }, '100%에서 많이 벗어나면(예: 70% 미만, 130% 넘음) 자주 쓰는 품목의 보정값을 고치세요. 재방문 건이 섞이면 근사치입니다.'),
      h('table', {}, h('thead', {}, h('tr', {}, ...['날짜', '하차', '추정 m³', '기록 m³', '추정 kg', '기록 kg'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...r.days.map((d) => h('tr', {}, ...[d.date, `${d.unloads}회`, d.est_m3, d.rec_m3 ?? '—', d.est_kg.toLocaleString(), d.rec_kg == null ? '—' : d.rec_kg.toLocaleString()].map((v) => h('td', {}, String(v))))))));
  }).catch(() => {});
  return h('div', { class: 'card' }, h('h2', {}, '적재량 보정'), h('p', { class: 'muted' }, '자주 쓰는 품목의 1단위 부피·무게와 취급 표시(품목별 적재 기준표의 추정치)입니다. 추정치는 그대로 두고, 실측한 부피가 있으면 보정값에 적으세요(비우면 추정치 사용).'),
    h('table', {}, h('thead', {}, h('tr', {}, ...['품목', '부피 추정', '무게 추정', '취급', '부피 보정값', ''].map((t) => h('th', {}, t)))), tbody), msg, chk);
}

// 하차 기록: 반입지에서 내린 양(부피·중량)을 팀별로 기록
function unloadCard(d) {
  const msg = h('span', { class: 'msg' });
  const crew = h('select', {}, ...boot.crews.map((c) => h('option', { value: c.id }, c.name)));
  const at = h('input', { type: 'time' }), place = h('input', { placeholder: '반입지' });
  const vol = h('input', { type: 'number', step: '0.1', placeholder: 'm³', style: 'width:90px' }), kg = h('input', { type: 'number', placeholder: 'kg', style: 'width:100px' });
  const note = h('input', { placeholder: '메모', style: 'flex:1' });
  return h('div', { class: 'card' }, h('h2', {}, `하차 기록 · 합계 ${d.unload_total.volume_m3}m³ · ${d.unload_total.weight_kg}kg`),
    d.unloads.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['팀', '시각', '반입지', '부피', '중량', '메모'].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...d.unloads.map((u) => h('tr', {}, h('td', {}, u.crew_name), h('td', {}, u.at_time || ''), h('td', {}, u.place), h('td', {}, u.volume_m3 ?? ''), h('td', {}, u.weight_kg ?? ''), h('td', {}, u.note || ''))))) : h('p', { class: 'muted' }, '기록 없음'),
    boot.crews.length ? h('div', { class: 'row' }, crew, at, place, vol, kg, note, btn('하차 기록', wrap(async () => {
      await postOnce(`unload:${d.work_date}`, 'unloads', { crew_id: crew.value, work_date: d.work_date, at_time: at.value, place: place.value, volume_m3: vol.value, weight_kg: kg.value, note: note.value });
      toast('하차를 기록했습니다'); render();
    }, msg), 'primary'), msg) : null);
}

// ------------------------------------------------------------ QR 실측 (가상 자료)
const SETTING_TABS = [['basic', '기본'], ['crews', '수거팀'], ['devices', '기기'], ['dongs', '행정동'], ['capacity', '한도'], ['holidays', '휴무일'], ['fees', '수수료·감면'], ['addr', '주소·외부 서비스'], ['backup', '백업'], ['privacy', '개인정보']];
async function screenSettings(tab) {
  tab = SETTING_TABS.some(([k]) => k === tab) ? tab : 'basic';
  const tabs = h('nav', { class: 'tabs', 'aria-label': '설정 묶음' }, ...SETTING_TABS.map(([k, t]) => h('a', { href: '#/settings/' + k, class: k === tab ? 'on' : '', 'aria-current': k === tab ? 'page' : null }, t)));
  const body = await ({ basic: settingsBasic, crews: settingsCrews, devices: settingsDevices, dongs: () => [dongCard()], capacity: () => [capacityCard()], holidays: () => [holidayCard()],
    fees: settingsFees, addr: () => [addrCard(), externalCard()], backup: () => [autoBackupCard(), backupCard()], privacy: privacyCard }[tab])();
  return h('div', {}, pagehead('설정'), tabs, ...[].concat(body));
}
const fp9 = (f) => (f.length === 9 ? `${f.slice(0, 3)} ${f.slice(3, 6)} ${f.slice(6)}` : f);
const regState = { fp: '' };  // 방금 등록한 기기의 지문: 목록을 다시 그려도 보이게 화면 밖에 둔다
// 지문 확인 뒤 "어느 팀에 연결할까요?": 있는 팀 고르기 또는 새 팀 만들기 (나중에 해도 됨)
async function crewChooser(dev) {
  const crews = boot.crews.filter((c) => c.active);
  const pick = h('select', { 'aria-label': '연결할 팀' }, ...crews.map((c) => h('option', { value: c.id }, `${c.name}${c.device_id && c.device_id !== dev.id ? ' (다른 휴대폰 연결됨 → 바꿈)' : ''}`)),
    h('option', { value: '__new', selected: !crews.length }, '+ 새 팀 만들기'));
  const nm = h('input', { placeholder: '새 팀 이름 (예: 1팀)', 'aria-label': '새 팀 이름' });
  const sync = () => { nm.closest('label').hidden = pick.value !== '__new'; };
  const body = [h('p', {}, `${dev.label || dev.id.slice(0, 8)} 휴대폰을 사용하기 시작했습니다. 이 휴대폰으로 목록을 받을 팀을 고르세요.`), h('label', { class: 'f' }, '팀', pick), h('label', { class: 'f' }, '새 팀 이름', nm)];
  pick.addEventListener('change', sync); setTimeout(sync, 0);
  return modal({ title: '어느 팀에 연결할까요?', ok: '연결', cancel: '나중에', body,
    onOk: async () => {
      if (pick.value === '__new') {
        if (!nm.value.trim()) throw new Error('새 팀 이름을 적어 주세요.');
        await post('crews', { id: null, name: nm.value.trim(), device_id: dev.id, capacity_m3: 8, capacity_kg: 0, shift_start: '09:00', shift_end: '17:00', active: true });
        return nm.value.trim();
      }
      const c = crews.find((x) => x.id === pick.value);
      await post('crews', { id: c.id, name: c.name, device_id: dev.id, capacity_m3: c.capacity_m3, capacity_kg: c.capacity_kg || 0, shift_start: c.shift_start, shift_end: c.shift_end, active: !!c.active });
      return c.name;
    } });
}
function settingsDevices() {
  const devMsg = h('p', { class: 'msg' }), regMsg = h('p', { class: 'msg' });
  const regImg = h('img', { alt: '기기 등록 QR', style: 'width:260px;image-rendering:pixelated' });
  api('registration-qr').then((r) => { regImg.src = r.image; }).catch(() => {});
  const code = h('textarea', { placeholder: '휴대폰 화면의 기기 코드 (DGD1.…) — 웹캠이 없을 때만 붙여 넣기', rows: 3, style: 'width:100%' });
  const label = h('input', { placeholder: '기기 이름 (예: 1팀 업무폰)' });
  const fp = h('p', { class: 'big' }, regState.fp);
  // 등록하면 바로 목록을 새로 그린다(지문은 regState에 남겨 다시 그린 화면에도 보임)
  const register = wrap(async () => {
    const r = await post('devices/register', { code: code.value, label: label.value });
    regState.fp = `지문 ${fp9(r.fingerprint)}`; fp.textContent = regState.fp;
    toast(r.confirmed ? '이미 등록된 기기입니다.' : '등록했습니다. 지문이 휴대폰과 같으면 아래 목록에서 [지문 같음 · 사용 시작]을 누르세요.');
    setBoot(await api('bootstrap'));
    if (location.hash.startsWith('#/settings/devices')) render(true);
  }, regMsg);
  // 휴대폰 화면의 기기 코드 QR을 이 PC 웹캠으로 읽어 바로 등록 (코드를 옮겨 적거나 보낼 필요 없음)
  const camVideo = h('video', { playsinline: true, muted: true, class: 'regcam', hidden: true });
  const camBtn = btn('휴대폰 QR 웹캠으로 읽기', wrap(async () => {
    if (camVideo._stop) { camVideo._stop(); return; }
    camBtn.textContent = '읽기 멈추기'; regMsg.textContent = '휴대폰 [기기 등록] 2단계의 QR을 웹캠에 비추세요.'; regMsg.className = 'msg';
    try {
      const text = await webcamReadOnce(camVideo, (t) => /^DGD1\./.test(t.trim()));
      code.value = text.trim(); regMsg.textContent = '기기 코드를 읽었습니다. 등록합니다…';
      await register();
    } finally { camBtn.textContent = '휴대폰 QR 웹캠으로 읽기'; }
  }, regMsg), 'primary');
  const pending = (d) => d.status === 'ACTIVE' && !d.confirmed_at;
  const devs = grid(['기기', '이름', '상태', '등록', ''], boot.devices.map((d) => [d.id.slice(0, 8), d.label || '',
    pending(d) ? h('span', {}, chip('지문 확인 대기', 'warn'), h('br'), h('b', { class: 'fp-inline' }, fp9(d.fingerprint || ''))) : d.status === 'ACTIVE' ? chip('사용', 'ok') : chip(d.status === 'LOST' ? '분실' : '중지', 'bad'),
    d.registered_at.slice(0, 10), h('span', { class: 'row' },
      pending(d) ? btn('지문 같음 · 사용 시작', wrap(async () => {
        await post(`devices/${d.id}/confirm`); toast('사용을 시작했습니다'); regState.fp = '';
        setBoot(await api('bootstrap'));
        const crewName = await crewChooser(d);
        if (crewName) toast(`${crewName}에 연결했습니다`);
        setBoot(null); render(true);
      }, devMsg), 'small primary') : null,
      ...[['ACTIVE', '사용'], ['LOST', '분실 처리'], ['RETIRED', '사용 중지']].filter(([k]) => k !== d.status).map(([k, t]) =>
      btn(t, wrap(async () => { if (k !== 'ACTIVE' && !(await confirmDlg(`휴대폰 ${t}`, `${d.id.slice(0, 8)} 기기를 '${t}'합니다. 이 기기로는 목록을 보낼 수 없고 결과도 받지 않습니다. 계속할까요?`, { ok: t, danger: true }))) return; await post(`devices/${d.id}/status`, { status: k }); setBoot(null); render(); }, devMsg), 'small')))]), { empty: '등록된 휴대폰이 없습니다.' });
  return [h('div', { class: 'grid2' },
    h('div', { class: 'card' }, h('h2', {}, '휴대폰 등록'),
      h('ol', { class: 'steps-list' }, h('li', {}, '휴대폰 앱 [기기 등록]으로 아래 QR을 비춥니다 (공개키만 들어 있음).'),
        h('li', {}, '휴대폰에 나온 기기 코드 QR을 [휴대폰 QR 웹캠으로 읽기]로 이 PC 웹캠에 비춥니다. 읽으면 바로 등록됩니다.'),
        h('li', {}, '양쪽 9자리 지문이 같은지 확인하고, 아래 목록에서 [지문 같음 · 사용 시작]을 누릅니다. 그 전에는 이 휴대폰으로 목록을 보낼 수 없습니다.')),
      regImg, label, h('div', { class: 'row' }, camBtn), camVideo, fp, regMsg,
      h('details', { class: 'free' }, h('summary', {}, '웹캠이 없을 때: 기기 코드 붙여 넣기'),
        h('p', { class: 'small muted' }, '휴대폰의 [코드 복사]로 복사한 코드를 업무 메일 등 승인된 경로로 이 PC에 옮겨 붙여 넣습니다(공개키만 들어 있어 비밀은 아니지만, 지문 대조로 바꿔치기를 막습니다).'),
        code, btn('기기 등록', register, 'small'))),
    h('div', { class: 'card' }, h('h2', {}, '등록된 휴대폰'), devs, h('p', { class: 'muted' }, '분실 시 바로 "분실 처리"하세요. 오프라인 휴대폰의 자료를 원격으로 지울 수는 없지만, 이후 그 기기로는 목록을 보내지 않고 결과도 받지 않습니다.'), devMsg))];
}
function settingsCrews() {
  const crewMsg = h('p', { class: 'msg' });
  const crewRows = [...boot.crews, { id: '', name: '', device_id: '', capacity_m3: 8, capacity_kg: 0, shift_start: '09:00', shift_end: '17:00', active: 1 }].map((c) => {
    const f = { name: h('input', { value: c.name, placeholder: '새 팀 이름', 'aria-label': c.name ? `${c.name} 이름` : '새 팀 이름' }), device_id: h('select', { 'aria-label': '연결 휴대폰' }, h('option', { value: '' }, '(없음)'), ...boot.devices.filter((d) => d.status === 'ACTIVE').map((d) => h('option', { value: d.id, selected: d.id === c.device_id }, `${d.id.slice(0, 8)} ${d.label || ''}`))),
      capacity_m3: h('input', { type: 'number', step: '0.5', value: c.capacity_m3, class: 'w-xs', 'aria-label': '적재(m³)' }),
      capacity_kg: h('input', { type: 'number', step: '50', min: 0, value: c.capacity_kg || '', placeholder: '0', class: 'w-xs', 'aria-label': '최대 적재 중량(kg)', title: '비우거나 0이면 무게는 따지지 않음' }), shift_start: h('input', { type: 'time', value: c.shift_start, 'aria-label': '근무 시작' }), shift_end: h('input', { type: 'time', value: c.shift_end, 'aria-label': '근무 끝' }), active: h('input', { type: 'checkbox', checked: c.active, 'aria-label': '사용' }) };
    return h('tr', {}, ...Object.values(f).map((x) => h('td', {}, x)), h('td', {}, btn(c.id ? '저장' : '추가', wrap(async () => {
      await post('crews', { id: c.id || null, name: f.name.value, device_id: f.device_id.value, capacity_m3: f.capacity_m3.value, capacity_kg: f.capacity_kg.value || 0, shift_start: f.shift_start.value, shift_end: f.shift_end.value, active: f.active.checked });
      toast('저장했습니다'); setBoot(null); render();
    }, crewMsg), 'small primary')));
  });
  return [h('div', { class: 'card' }, h('h2', {}, '수거팀·차량'), h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['팀', '연결 휴대폰', '적재(m³)', '최대 중량(kg)', '근무 시작', '근무 끝', '사용', ''].map((t) => h('th', {}, t)))), h('tbody', {}, ...crewRows))),
      h('p', { class: 'small muted' }, '최대 중량은 차량 등록증의 최대 적재량입니다(1톤 차량이면 1000). 비우면 무게는 따지지 않고 부피만 봅니다.'), crewMsg),
    volumeCard()];
}
// 좌표 칸 도우미: [지도에서 고르기](지도를 눌러 핀) · [주소로 찾기](이 PC의 주소 자료). 위도·경도 칸을 채운다.
async function mapPickDlg(title, lat, lon) {
  const box = h('div', { class: 'mapbox pickmap' });
  const info = h('p', { class: 'muted small', 'aria-live': 'polite' }, lat ? `지금 좌표 ${lat}, ${lon}` : '지도를 눌러 위치를 고르세요.');
  let pick = null;
  const p = modal({ title, wide: true, ok: '이 위치로', body: [info, box], onOk: () => { if (!pick) throw new Error('지도를 눌러 위치를 고르세요.'); return pick; } });
  setTimeout(async () => {
    try {
      const d = boot.settings.depot;
      const has = lat !== '' && lat != null && !isNaN(+lat);
      const { map, ml } = await mapIn(box, { center: has ? [+lon, +lat] : [d.lon, d.lat], zoom: has ? 16 : 13 });
      let mk = has ? M.pin(ml, map, { lng: +lon, lat: +lat, label: '●', cls: 'me' }) : null;
      map.on('click', (e) => {
        pick = { lat: Math.round(e.lngLat.lat * 1e6) / 1e6, lon: Math.round(e.lngLat.lng * 1e6) / 1e6 };
        if (mk) mk.remove(); mk = M.pin(ml, map, { lng: pick.lon, lat: pick.lat, label: '●', cls: 'me' });
        info.textContent = `고른 좌표 ${pick.lat}, ${pick.lon}`;
      });
    } catch { box.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다(인터넷 연결 확인). [주소로 찾기]를 쓰거나 좌표를 직접 적으세요.')); }
  }, 30);
  return p;
}
function coordTools(latEl, lonEl, what, labelEl) {
  const pickMap = btn('지도에서 고르기', wrap(async () => {
    const r = await mapPickDlg(`${what} 위치 고르기`, latEl.value, lonEl.value);
    if (r) { latEl.value = r.lat; lonEl.value = r.lon; toast(`${what} 좌표를 채웠습니다. [저장]을 눌러야 반영됩니다.`); }
  }), 'small', { 'aria-label': `${what} 지도에서 고르기` });
  const byAddr = btn('주소로 찾기', wrap(async () => {
    const inp = h('input', { class: 'fill', placeholder: '도로명과 건물번호 (예: 필문대로 245)' });
    const r = await modal({ title: `${what} 주소로 찾기`, ok: '찾기', body: [h('label', { class: 'f' }, '주소', inp), h('p', { class: 'muted small' }, '이 PC에 넣은 주소 위치 자료에서 찾습니다(외부로 보내지 않음). 건물 출입구 기준입니다.')],
      onOk: async () => {
        if (!inp.value.trim()) throw new Error('주소를 적어 주세요.');
        const g = await api('geocode?address=' + encodeURIComponent(inp.value.trim()));
        if (!g || g.none) throw new Error(boot.addr_count ? '주소 자료에서 찾지 못했습니다. 도로명과 건물번호를 확인하거나 [지도에서 고르기]를 쓰세요.' : '이 PC에 주소 위치 자료가 없습니다. [지도에서 고르기]를 쓰세요.');
        if (g.ambiguous) throw new Error(`여러 지역에 있습니다(${g.options.join(', ')}). 구 이름을 넣어 주세요.`);
        return { ...g, q: inp.value.trim() };
      } });
    if (!r) return;
    latEl.value = r.lat; lonEl.value = r.lon; if (labelEl && !labelEl.value) labelEl.value = r.label || r.q;
    toast(`${what} 좌표를 채웠습니다 (${r.label || r.q}). [저장]을 눌러야 반영됩니다.`);
  }), 'small', { 'aria-label': `${what} 주소로 찾기` });
  return h('span', { class: 'row coordtools' }, pickMap, byAddr);
}
function settingsBasic() {
  const routeMsg = h('p', { class: 'msg' }), infoMsg = h('p', { class: 'msg' });
  const st = boot.settings;
  const dlat = h('input', { value: st.depot.lat, class: 'w-s', 'aria-label': '차고지 위도' }), dlon = h('input', { value: st.depot.lon, class: 'w-s', 'aria-label': '차고지 경도' }), dlabel = h('input', { value: st.depot.label, 'aria-label': '차고지 이름' });
  const osrm = h('input', { value: st.osrm_url, placeholder: 'http://127.0.0.1:5000 (이동시간 방식이 로컬 OSRM일 때 사용)', class: 'w-l', 'aria-label': '로컬 OSRM' });
  const sb = h('input', { type: 'number', value: st.service_min_base, class: 'w-xs', 'aria-label': '지점당 기본 작업(분)' }), sp = h('input', { type: 'number', value: st.service_min_per_item, class: 'w-xs', 'aria-label': '품목당 작업(분)' });
  const us = st.unload_site || {}, es = st.end_site || {};
  const jur = h('input', { value: st.jurisdiction || '', placeholder: '예: 동구', class: 'w-s', 'aria-label': '관할 구역' });
  const elat = h('input', { value: es.lat ?? '', placeholder: '위도', class: 'w-s', 'aria-label': '종료지 위도' }), elon = h('input', { value: es.lon ?? '', placeholder: '경도', class: 'w-s', 'aria-label': '종료지 경도' }), elabel = h('input', { value: es.label || '', placeholder: '종료지 이름', 'aria-label': '종료지 이름' });
  const ulat = h('input', { value: us.lat ?? '', placeholder: '위도', class: 'w-s', 'aria-label': '반입지 위도' }), ulon = h('input', { value: us.lon ?? '', placeholder: '경도', class: 'w-s', 'aria-label': '반입지 경도' }), ulabel = h('input', { value: us.label || '', placeholder: '반입지 이름', 'aria-label': '반입지 이름' });
  const uh = st.unload_hours || {}, lu = st.lunch || {};
  const uhs = h('input', { type: 'time', value: uh.start || '', 'aria-label': '반입지 받는 시간 시작' }), uhe = h('input', { type: 'time', value: uh.end || '', 'aria-label': '반입지 받는 시간 끝' });
  const lus = h('input', { type: 'time', value: lu.start || '', 'aria-label': '쉬는 시간 창 시작' }), lue = h('input', { type: 'time', value: lu.end || '', 'aria-label': '쉬는 시간 창 끝' });
  const lum = h('input', { type: 'number', value: lu.minutes || 60, min: 10, class: 'w-xs', 'aria-label': '쉬는 시간(분)' });
  const bfirst = h('input', { type: 'checkbox', checked: st.base_first !== false, 'aria-label': '큰 짐 먼저 싣기 선호' });
  const umin = h('input', { type: 'number', value: st.unload_min, class: 'w-xs', 'aria-label': '하차 시간(분)' }), umax = h('input', { type: 'number', value: st.max_unloads, min: 0, max: 5, class: 'w-xs', 'aria-label': '차량당 최대 하차 횟수' });
  const ft = (label, ...el) => h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, label), h('div', { class: 'ft-v' }, ...el));
  const contact = h('input', { value: st.contact_info, class: 'fill', 'aria-label': '문의처' });
  const guide = h('textarea', { rows: 2, class: 'fill', 'aria-label': '배출 장소 안내' }, st.disposal_guide);
  const title = h('input', { value: st.monthly_report_title, class: 'fill', 'aria-label': '월보 제목' });
  const payGuide = h('textarea', { rows: 2, class: 'fill', maxlength: 300, 'aria-label': '납부 안내' }, st.payment_guide || '');
  const roundUnit = h('select', { 'aria-label': '감면액 끝수 단위' }, ...(boot.reduction_round_units || [1, 10]).map((u) => h('option', { value: u, selected: +u === +(st.reduction_round_unit || 1) }, u === 1 ? '1원 (원 미만 버림)' : `${u}원 (${u}원 미만 버림)`)));
  return [h('div', { class: 'card' }, h('h2', {}, '노선 계산'), h('div', { class: 'ftable' },
      ft('공통 차고지', dlat, dlon, dlabel, coordTools(dlat, dlon, '차고지', dlabel), h('span', { class: 'muted small' }, '행정동에서 개별 차고지를 지정하지 않은 팀에 적용')), ft('로컬 OSRM', osrm), ft('작업 시간', '지점당 기본', sb, '분 + 품목당', sp, '분 (추정치)'),
      ft('반입지', ulat, ulon, ulabel, coordTools(ulat, ulon, '반입지', ulabel), '하차', umin, '분 · 차량당 최대', umax, '회'),
      ft('반입지 받는 시간', uhs, '~', uhe, h('span', { class: 'muted small' }, '비우면 따지지 않음')),
      ft('쉬는 시간', lus, '~', lue, '사이에', lum, '분', h('span', { class: 'muted small' }, '예: 11:30~13:30 사이 60분. 비우면 넣지 않음')),
      ft('적재 순서', h('label', { class: 'row' }, bfirst, '큰 짐(장롱·침대·대형 냉장고 등)은 차가 비어 있을 때 싣는 순서를 선호'), h('span', { class: 'muted small' }, '이동시간이 비슷하면 큰 짐을 먼저')), ft('종료지', elat, elon, elabel, coordTools(elat, elon, '종료지', elabel), h('span', { class: 'muted small' }, '비우면 차고지로 돌아옴')),
      ft('관할 구역', jur, h('span', { class: 'muted small' }, '주소의 구 이름이 다르면 접수할 때 경고 (막지는 않음)'))),
    h('p', { class: 'small muted' }, '반입지 좌표를 넣으면 적재가 찰 때 반입지에 들러 비우고 이어서 수거하는 순서를 추천합니다. 비우면 차량 1회 적재 안에서만 계산합니다.'),
    h('div', { class: 'row' }, btn('저장', wrap(async () => { await post('settings', { depot: { lat: dlat.value, lon: dlon.value, label: dlabel.value }, osrm_url: osrm.value, service_min_base: sb.value, service_min_per_item: sp.value,
      unload_site: { lat: ulat.value, lon: ulon.value, label: ulabel.value }, end_site: { lat: elat.value, lon: elon.value, label: elabel.value }, jurisdiction: jur.value, unload_min: umin.value, max_unloads: umax.value,
      unload_hours: { start: uhs.value, end: uhe.value }, lunch: { start: lus.value, end: lue.value, minutes: lum.value }, base_first: bfirst.checked }); toast('저장했습니다'); setBoot(null); render(); }, routeMsg), 'primary')), routeMsg),
    h('div', { class: 'card' }, h('h2', {}, '접수증·안내 문구·월보'), isPlaceholderContact() ? h('div', { class: 'notice warn small' }, '문의처가 아직 기본 문구입니다. 접수증·주민 안내 문구에 그대로 나가니 전화번호를 넣으세요.') : null,
      h('div', { class: 'ftable' }, ft('문의처', contact), ft('배출 장소 안내', guide), ft('납부 안내', payGuide, h('span', { class: 'muted small' }, '납부 방법·기한 (300자, 접수증·안내 문구)')), ft('감면액 끝수', roundUnit, h('span', { class: 'muted small' }, '새로 감면하는 접수부터 적용 (이미 감면한 접수는 그때 단위 그대로)')), ft('월보 제목', title)),
      h('p', { class: 'small muted' }, '문의처·배출 장소 안내·납부 안내는 접수증(신청인 보관용)과 [안내 문구 복사]에, 월보 제목은 통계 → [월보 출력]에 쓰입니다.'),
      h('div', { class: 'row' }, btn('저장', wrap(async () => { await post('settings', { contact_info: contact.value, disposal_guide: guide.value, payment_guide: payGuide.value, ...(+roundUnit.value !== +(st.reduction_round_unit || 1) ? { reduction_round_unit: +roundUnit.value } : {}), monthly_report_title: title.value }); toast('저장했습니다'); setBoot(null); render(); }, infoMsg), 'primary')), infoMsg),
    noticeCard()];
}
function settingsFees() {
  const st = boot.settings;
  const msg = h('p', { class: 'msg' });
  const rowsF = [...(st.reductions || []), { code: '', label: '', rate: '', note: '조례 확인 필요', active: true }].map((x) => ({
    code: h('input', { value: x.code, placeholder: '코드(영문)', class: 'w-s', 'aria-label': `${x.label || '새 감면'} 코드` }), label: h('input', { value: x.label, placeholder: '새 감면 사유', class: 'w-m', 'aria-label': `${x.label || '새 감면'} 사유` }),
    rate: h('input', { type: 'number', min: 1, max: 100, value: x.rate, class: 'w-xs', 'aria-label': `${x.label || '새 감면'} 감면율` }), note: h('input', { value: x.note || '', class: 'w-m', 'aria-label': `${x.label || '새 감면'} 비고` }),
    active: h('input', { type: 'checkbox', checked: x.active !== false, 'aria-label': `${x.label || '새 감면'} 사용` }) }));
  return [h('div', { class: 'card' }, h('h2', {}, '수수료표'), h('dl', { class: 'kv' }, h('dt', {}, '적용 표'), h('dd', {}, st.fee_label), h('dt', {}, '버전'), h('dd', {}, st.fee_version),
      h('dt', {}, '자료 확인일'), h('dd', {}, st.fee_checked_on), h('dt', {}, '시행일'), h('dd', {}, st.fee_effective_from || '미확인 (담당자 확인 필요)')),
      h('a', { href: '#/fees' }, '수수료표 관리 화면에서 시행일·확인자·확인 필요 행 정하기')),
    h('div', { class: 'card' }, h('h2', {}, '수수료 감면 사유 ', chip('조례 확인 필요', 'warn')),
      h('p', { class: 'muted' }, '아래 기본값(기초생활수급자 100%, 차상위 50%, 장애인 50%, 국가유공자 50%)은 예시입니다. 동구 조례·규칙의 감면 대상과 감면율을 확인해 고치세요. 이미 쓰인 사유는 지우지 말고 "사용"을 끄세요. 감면액은 원 미만을 버립니다.'),
      grid(['코드', '감면 사유', '감면율(%)', '비고', '사용'], rowsF.map((f) => [f.code, f.label, f.rate, f.note, f.active])),
      h('div', { class: 'row' }, btn('감면 사유 저장', wrap(async () => { await post('settings', { reductions: rowsF.map((f) => ({ code: f.code.value, label: f.label.value, rate: f.rate.value, note: f.note.value, active: f.active.checked })).filter((x) => x.label.trim()) }); toast('저장했습니다'); setBoot(null); render(); }, msg), 'primary')), msg)];
}

// 자동 백업: 백업 공개키(설정에서 생성, 개인키는 한 번만 내려받아 오프라인 보관) · 시각 · 두 번째 위치 · 보관 세대
function autoBackupCard() {
  const st = boot.settings;
  const msg = h('p', { class: 'msg' }), stat = h('div');
  let hasKey = false;
  const auto = h('input', { type: 'checkbox', checked: st.backup_auto, 'aria-label': '자동 백업 사용' });
  const time = h('input', { type: 'time', value: st.backup_time, 'aria-label': '자동 백업 시각' });
  const dest2 = h('input', { value: st.backup_dest2, placeholder: '예: \\\\파일서버\\동구백업 또는 E:\\동구백업 (비우면 안 함)', class: 'w-l', 'aria-label': '두 번째 보관 위치' });
  const kd = h('input', { type: 'number', min: 1, max: 365, value: st.backup_keep_daily, class: 'w-xs', 'aria-label': '일별 보관 개수' });
  const km = h('input', { type: 'number', min: 0, max: 120, value: st.backup_keep_monthly, class: 'w-xs', 'aria-label': '월별 보관 개수' });
  const rd = h('input', { type: 'number', min: 7, max: 3650, value: st.backup_retention_days, class: 'w-xs', 'aria-label': '백업 보관 일수' });
  const load = wrap(async () => {
    const b = await api('backup-status');
    hasKey = !!b.key;
    stat.replaceChildren(h('dl', { class: 'kv' },
      h('dt', {}, '최근 백업'), h('dd', {}, b.last_at ? [chip(b.stale ? '26시간 넘음' : '정상', b.stale ? 'bad' : 'ok'), ` ${kst(b.last_at)} (${b.age_hours}시간 전)`] : chip('없음', 'bad')),
      h('dt', {}, '최근 자동 백업'), h('dd', {}, b.auto && b.auto.at ? [chip(b.auto.ok ? '성공' : '실패', b.auto.ok ? 'ok' : 'bad'), ` ${kst(b.auto.at)} · ${b.auto.ok ? b.auto.file : b.auto.error}`,
        b.auto.dest2 ? h('span', { class: 'block small' }, `두 번째 위치: ${b.auto.dest2.ok ? '복사·해시 확인' : '실패 — ' + (b.auto.dest2.error || '')}`) : null] : '아직 없음'),
      h('dt', {}, '백업 키'), h('dd', {}, b.key ? `지문 ${b.key.fpr} · ${kst(b.key.created_at)} 생성` : chip('없음 — 자동 백업 안 함', 'warn'))));
  }, msg);
  load();
  const keygen = wrap(async () => {
    const p1 = h('input', { type: 'password', autocomplete: 'new-password', class: 'fill' }), p2 = h('input', { type: 'password', autocomplete: 'new-password', class: 'fill' });
    const r = await modal({ title: hasKey ? '백업 키 다시 만들기' : '백업 키 만들기', ok: '만들고 개인키 내려받기', wide: false,
      body: [h('p', {}, '자동 백업은 이 키의 공개키로 암호화됩니다. 개인키 파일은 지금 한 번만 내려받을 수 있고 PC에는 남지 않습니다. 보안 매체에 오프라인으로 보관하고, 비밀번호는 다른 사람이 따로 보관하세요. 복원할 때 둘 다 필요합니다.'),
        hasKey ? h('div', { class: 'notice warn' }, '새 키를 만들면 이전 자동 백업은 이전 개인키로만 열립니다.') : null,
        h('label', { class: 'f' }, '개인키 보호 비밀번호 (12자 이상)', p1), h('label', { class: 'f' }, '한 번 더', p2)],
      onOk: async () => { if (p1.value !== p2.value) throw new Error('두 번 입력한 비밀번호가 다릅니다.'); return post('backup-key', { password: p1.value }); } });
    if (!r) return;
    const url = URL.createObjectURL(new Blob([r.pem], { type: 'application/x-pem-file' }));
    const a = h('a', { href: url, download: `donggu-backup-key-${r.fpr}.pem` }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast(`백업 키를 만들었습니다 (지문 ${r.fpr}). 내려받은 개인키 파일을 오프라인으로 보관하세요.`); load();
  }, msg);
  return h('div', { class: 'card' }, h('h2', {}, '자동 백업'), stat,
    h('div', { class: 'row' }, btn('백업 키 만들기', keygen, 'small'), btn('지금 자동 백업', wrap(async () => { const r = await runJob('backup/auto', {}, '자동 백업'); toast(r.ok ? `자동 백업: ${r.file}` : (r.error || '자동 백업 실패'), !r.ok); load(); }, msg), 'small'),
      btn('보관 기간 지난 백업 정리', wrap(async () => { const r = await runJob('backup/retention', {}, '백업 보관 정리'); toast(`${(r.deleted || []).length}개 지웠습니다`); }, msg), 'small')),
    h('div', { class: 'ftable' },
      h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '자동 백업'), h('div', { class: 'ft-v' }, h('label', { class: 'row' }, auto, '서버가 켜져 있는 동안 매일'), time, h('span', { class: 'muted small' }, '(늦게 켜면 켜자마자) · 마감 확인 때도 백업'))),
      h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '두 번째 보관 위치'), h('div', { class: 'ft-v' }, dest2, h('span', { class: 'muted small' }, '복사 뒤 해시로 확인'))),
      h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '보관'), h('div', { class: 'ft-v' }, '자동 백업 최근', kd, '일(하루 1개) + 최근', km, '개월(달마다 1개) · 모든 백업', rd, '일 지나면 삭제'))),
    h('div', { class: 'row' }, btn('자동 백업 설정 저장', wrap(async () => { await post('settings', { backup_auto: auto.checked, backup_time: time.value, backup_dest2: dest2.value, backup_keep_daily: kd.value, backup_keep_monthly: km.value, backup_retention_days: rd.value }); toast('저장했습니다'); setBoot(null); render(); }, msg), 'primary')),
    h('p', { class: 'small muted' }, '복원: 업무 화면을 끄고 ', h('code', {}, 'python pc/pc_tool.py restore-auto <파일.dgabk> --key <개인키.pem>'), ' (개인키 보호 비밀번호 필요). 백업 비밀번호를 PC에 저장하지 않습니다.'),
    h('p', { class: 'small muted' }, '예전 백업 파일에는 그때의 개인정보가 그대로 들어 있습니다. 개인정보를 파기해도 백업은 따로 지워야 하므로, 보관 일수(기본 90일)가 지나면 자동으로 지웁니다. 두 번째 위치·외부 매체에 옮긴 사본도 같은 기간에 지우세요.'), msg);
}

// 개인정보 보유기간·파기 (관리자)
async function privacyCard() {
  const d = await api('privacy');
  const msg = h('p', { class: 'msg' });
  const years = h('input', { type: 'number', min: 1, max: 10, value: d.retention_years, class: 'w-xs', 'aria-label': '개인정보 보유기간(년)' });
  const ayears = h('input', { type: 'number', min: 2, max: 10, value: d.access_retention_years, class: 'w-xs', 'aria-label': '접속기록 보관(년)' });
  const p = d.preview;
  return [h('div', { class: 'card' }, h('h2', {}, '개인정보 보유기간 ', chip('정책 값 · 기관 결정 필요', 'warn')),
      h('div', { class: 'ftable' }, h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '보유기간'), h('div', { class: 'ft-v' }, '처리 완료(수거 완료)·취소일부터', years, '년 (기본 3년)')),
        h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '접속기록 보관'), h('div', { class: 'ft-v' }, ayears, '년 (2년 이상)'))),
      h('div', { class: 'row' }, btn('저장', wrap(async () => { await post('settings', { pii_retention_years: years.value, access_log_retention_years: ayears.value }); toast('저장했습니다'); render(); }, msg), 'primary')),
      h('p', { class: 'small muted' }, '보유기간은 개인정보 처리방침·기록물 관리 기준에 맞춰 기관이 정하는 값입니다. 지나면 이름·연락처·주소·상세 위치·메모·좌표·민원 내용·현장 메모·사진을 가명 처리하고, 접수번호·부과번호·행정동·품목·금액·날짜는 통계·세입 확인을 위해 남깁니다.')),
    h('div', { class: 'card' }, h('h2', {}, '파기'),
      h('p', {}, `기준일 ${p.cutoff} 이전에 ${p.basis}된 접수 중 파기 대상 `, h('b', {}, `${p.count}건`), ` (환불 진행 중인 건 제외)`),
      h('div', { class: 'row' }, btn('파기 실행', wrap(async () => {
        if (!p.count) throw new Error('파기할 대상이 없습니다.');
        if (!(await confirmDlg('개인정보 파기', `${p.count}건의 이름·연락처·주소 등을 가명 처리합니다. 되돌릴 수 없습니다. 파기 대장에 일시·건수·작업자·기준이 남습니다. 계속할까요?`, { ok: '파기', danger: true }))) return;
        const r = await runJob('privacy/purge', {}, '개인정보 파기'); toast(`${r.count}건 파기했습니다`); render();
      }, msg), 'danger', { disabled: !p.count }), h('span', { class: 'muted small' }, '마감 확인(팀장) 때와 명령 ', h('code', {}, 'python pc/pc_tool.py purge-pii'), '로도 실행됩니다.')),
      h('div', { class: 'notice warn' }, '이전 백업 파일에는 파기 전 개인정보가 남아 있습니다. 백업은 [백업] 탭의 보관 일수에 따라 지워지며, 외부 매체로 옮긴 사본은 담당자가 같은 기간에 지워야 합니다.'), msg),
    h('div', { class: 'card' }, h('h2', {}, '파기 대장'), purgeLogTable(d.log), h('p', { class: 'small muted' }, '사용자·이력 화면에서도 볼 수 있습니다.'))];
}
function addrCard() {
  const msg = h('p', { class: 'msg' });
  const stat = h('span', { class: 'muted addr-stat' }, '불러오는 중…');
  const staleBox = h('div');
  api('addr-stats').then((x) => {
    stat.textContent = x.count ? `${x.count.toLocaleString()}곳 (${x.sigungu.join(', ')})` + (x.imported_at ? ` · ${x.imported_at.slice(0, 10)} 가져옴` : ' · 가져온 날짜 모름')
      + (x.count && x.with_codes < x.count ? ` · 행안부 식별 번호 ${x.with_codes.toLocaleString()}곳(다시 가져오면 채워짐)` : '') : '아직 없음';
    staleBox.replaceChildren(x.stale ? h('p', { class: 'notice warn small addr-stale' }, `가져온 지 ${x.stale_days}일이 넘었습니다. 새로 생긴 주소는 찾지 못하니 아래 순서로 새 파일을 받아 다시 가져오세요.`) : '');
  }).catch(() => { stat.textContent = '-'; });
  const file = h('input', { type: 'file', accept: '.txt,.csv' });
  const region = h('input', { placeholder: '시군구 거르기 (예: 동구)', value: '동구', style: 'width:180px' });
  const cols = h('input', { placeholder: '열 번호 직접 지정 (비우면 자동)', style: 'width:260px' });
  const out = h('div');
  const send = async (preview) => {
    const f = file.files[0]; if (!f) throw new Error('파일을 고르세요.');
    if (/\.zip$/i.test(f.name)) throw new Error('압축 파일(.zip)은 먼저 풀고, 안의 .txt 파일을 고르세요.');
    const buf = new Uint8Array(await f.arrayBuffer());
    return api(`addr-import?region=${encodeURIComponent(region.value)}&preview=${preview ? 1 : 0}&columns=${encodeURIComponent(cols.value)}`, { method: 'POST', raw: buf });
  };
  const prev = wrap(async () => {
    const r = await send(true);
    out.replaceChildren(h('div', { class: 'notice' }, h('p', {}, `찾은 열: ${Object.entries(r.columns).filter(([k]) => k !== 'header').map(([k, v]) => `${k}=${v}`).join(', ')} · ${r.count.toLocaleString()}건`
      + (r.has_codes ? ' · 행안부 식별 번호(도로명코드) 있음' : ' · 도로명코드 열 없음(행안부 결과는 도로명·번호로 찾음)')),
      h('ul', {}, ...r.sample.map((x) => h('li', {}, `${x.sigungu} ${x.road} ${x.underground ? '지하 ' : ''}${x.main}${x.sub ? '-' + x.sub : ''} → ${x.lat.toFixed(6)}, ${x.lon.toFixed(6)}`))),
      h('p', { class: 'small' }, '표본 주소가 지도에서 맞는 곳인지 확인한 뒤 가져오세요.'),
      btn('이대로 가져오기', wrap(async () => { const x = await send(false); toast(`${x.count.toLocaleString()}건 가져옴 (전체 ${x.total.toLocaleString()}건)`); out.replaceChildren(); setBoot(null); render(); }, msg), 'primary')));
  }, msg);
  return h('div', { class: 'card' }, h('h2', {}, '주소 위치 자료'),
    h('p', { class: 'muted' }, '행정안전부 도로명주소 "위치정보요약DB" 파일을 이 PC에 넣으면 접수 화면의 주소 찾기와 [주소로 위치 찾기]가 이 PC 안에서 위치(건물 출입구)를 채웁니다. '
      + '행안부 주소 검색 결과를 고를 때도 이 자료에서 먼저 좌표를 찾으므로 좌표 승인키는 없어도 됩니다. 모든 처리는 이 PC 안에서 하며 외부로 보내지 않습니다.'),
    h('div', { class: 'row' }, '저장된 자료', stat), staleBox,
    h('h3', {}, '파일 받기·넣기 (월 1회 갱신 권장)'),
    h('ol', { class: 'small steps-list addr-steps' },
      h('li', {}, '행정안전부 주소정보 누리집(business.juso.go.kr)에서 주소 DB → "위치정보요약DB"로 갑니다(메뉴 이름은 바뀔 수 있음).'),
      h('li', {}, '지역에서 해당 시도(동구청은 광주광역시)를 골라 내려받습니다. 행안부가 매월 새로 올립니다.'),
      h('li', {}, '내려받은 압축 파일(.zip)을 풉니다. 이 화면은 압축 파일을 읽지 못하니, 안에 든 .txt 파일을 씁니다(구분자 |, 글자 인코딩 CP949·UTF-8 모두 자동 인식, 머리글 없어도 됨).'),
      h('li', {}, '아래 [파일 선택]으로 .txt 파일을 고르고, 시군구 거르기는 "동구" 그대로 둔 채 [미리보기] → 표본 주소가 맞으면 [이대로 가져오기]. .txt가 여러 개면 하나씩 가져옵니다.'),
      h('li', {}, '파일이 40MB보다 크거나 여러 개면 명령으로 한 번에 가져옵니다(폴더를 주면 그 안의 .txt를 모두): ', h('code', {}, 'python pc/pc_tool.py import-addr <파일 또는 폴더> --region 동구'), ' (먼저 --preview로 확인).'),
      h('li', {}, '다시 가져오면 있던 주소는 그대로 두고 새 주소를 더하며, 빠져 있던 행안부 식별 번호(도로명코드·법정동코드)를 채웁니다. 45일이 지나면 오늘 현황에 갱신 안내가 뜹니다.')),
    h('div', { class: 'row' }, file, region, cols, btn('미리보기', prev, 'small')), out, msg);
}

// ------------------------------------------------------------ 외부 서비스 (운영 관리자, v0.11): 행안부 주소 API·카카오모빌리티
// 키는 PC의 암호화 DB에만 저장하고 화면에는 가린 값만 돌아온다. 외부 호출은 PC 서버가 하며 브라우저는 직접 연결하지 않는다.
function externalCard() {
  const ex = boot.settings.external || { keys: {}, travel_method: 'ESTIMATE' };
  const msg = h('p', { class: 'msg' });
  const keyRow = (k) => {
    const info = ex.keys[k] || { set: false, mask: '', label: k };
    const inp = h('input', { type: 'password', autocomplete: 'off', spellcheck: 'false', class: 'w-l', 'aria-label': info.label, placeholder: info.set ? `저장됨 ${info.mask} (바꿀 때만 입력)` : '(없음)' });
    const clr = info.set ? btn('지우기', wrap(async () => { await post('settings', { external: { keys: { [k]: '' } } }); toast(`${info.label}를 지웠습니다`); setBoot(null); render(); }, msg), 'small') : null;
    return { k, inp, el: h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, info.label), h('div', { class: 'ft-v' }, inp, info.set ? chip('저장됨 ' + info.mask, 'ok') : chip('없음'), clr)) };
  };
  const rows = ['juso_search_key', 'juso_coord_key', 'kakao_rest_key', 'vworld_api_key'].map(keyRow);
  const vworldEnabled = h('input', { type: 'checkbox', checked: ex.vworld?.enabled, 'aria-label': 'VWorld 참고 조회 사용' });
  const vworldDomain = h('input', { value: ex.vworld?.domain || '', class: 'fill', 'aria-label': 'VWorld 등록 서비스 URL', placeholder: '키 발급 시 등록한 URL (선택)' });
  const method = h('select', { 'aria-label': '이동시간 계산 방식' }, ...[['ESTIMATE', '직선거리 추정 (기본, 외부 전송 없음)'], ['OSRM', '로컬 OSRM (기본 설정의 OSRM 주소)'], ['KAKAO', '카카오모빌리티 API (REST 키)']]
    .map(([v, t]) => h('option', { value: v, selected: v === ex.travel_method }, t)));
  const testOut = h('span', { class: 'muted small', role: 'status' });
  const test = (svc) => wrap(async () => { testOut.textContent = '연결 시험 중…'; const r = await post('external/test', { service: svc }); testOut.textContent = (r.ok ? '✓ ' : '✗ ') + r.message; }, msg);
  return h('div', { class: 'card' }, h('h2', {}, '외부 서비스 (주소 검색·도로 이동시간)'),
    h('p', { class: 'muted' }, '사용자 요청으로 더한 기능입니다. 이 PC 서버가 대신 묻고, 브라우저는 외부 서버에 직접 연결하지 않습니다. 이름·연락처는 보내지 않습니다.'),
    h('ul', { class: 'small muted' },
      h('li', {}, '행안부 도로명주소 API(business.juso.go.kr): 접수 화면 [주소 찾기]에 적은 검색어만 보냅니다. 전화번호 모양 값·신청인 이름이 든 검색어는 보내지 않습니다. 고른 결과의 좌표는 먼저 이 PC의 주소 위치 자료에서 찾고, 없을 때만 좌표 승인키(선택)로 건물 식별 번호를 보내 묻습니다.'),
      h('li', {}, '카카오모빌리티 길찾기 API: 노선 계산 때 지점 좌표만 보냅니다. 받은 시간은 이 PC에 저장하지 않고 노선을 계산할 때마다 새로 묻습니다. 못 받은 구간은 직선거리로 추정해 화면에 몇 쌍인지 표시합니다.'),
      h('li', {}, 'VWorld: 전화 접수의 [VWorld 위치 참고]에서 조회한 건물 주소를 보냅니다. 브이월드(국토교통부) 결과는 참고 창에만 표시하며 접수·초안·QR에는 반영하지 않습니다. 키와 등록 서비스 URL은 발급 내용을 확인해 입력하세요.'),
      h('li', {}, '기관망 프록시는 PC 서버를 실행할 때의 HTTPS_PROXY 환경변수를 따릅니다. 검색어는 서버 로그·접속기록에 남지 않습니다(접속기록에는 검색했다는 사실과 건수만).')),
    h('div', { class: 'ftable' }, ...rows.map((r) => r.el),
      h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, 'VWorld 사용'), h('div', { class: 'ft-v' }, h('label', {}, vworldEnabled, ' 위치 참고 조회 사용'))),
      h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, 'VWorld 등록 서비스 URL'), h('div', { class: 'ft-v' }, vworldDomain)),
      h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '이동시간 계산 방식'), h('div', { class: 'ft-v' }, method))),
    h('div', { class: 'row' }, btn('저장', wrap(async () => {
      const keys = {}; rows.forEach((r) => { if (r.inp.value.trim()) keys[r.k] = r.inp.value.trim(); });
      await post('settings', { external: { keys, travel_method: method.value, vworld: { enabled: vworldEnabled.checked, domain: vworldDomain.value.trim() } } }); rows.forEach((r) => { r.inp.value = ''; });
      toast('저장했습니다'); setBoot(null); render();
    }, msg), 'primary'), btn('주소 검색 연결 시험', test('juso'), 'small'), btn('카카오모빌리티 연결 시험', test('kakao'), 'small'), btn('VWorld 연결 시험', test('vworld'), 'small'), testOut), msg);
}

// ------------------------------------------------------------ 백업·사진 보관 (운영 관리자)
function backupCard() {
  const st = boot.settings;
  const msg = h('p', { class: 'msg' });
  const list = h('tbody'), dirEl = h('code');
  const pass = h('input', { type: 'password', placeholder: '백업 비밀번호 (12자 이상, DB 키와 다르게)', autocomplete: 'new-password', style: 'width:300px' });
  const pass2 = h('input', { type: 'password', placeholder: '한 번 더', autocomplete: 'new-password', style: 'width:160px' });
  const days = h('input', { type: 'number', value: st.photo_retention_days, min: 7, max: 3650, style: 'width:90px' });
  const bdir = h('input', { value: st.backup_dir, placeholder: '비우면 프로그램 폴더 state/backups', style: 'width:320px' });
  const purgeInfo = h('span', { class: 'muted' });
  const load = wrap(async () => {
    const b = await api('backups');
    dirEl.textContent = b.dir;
    list.replaceChildren(...(b.files.length ? b.files.slice(0, 20).map((f) => {
      const vp = h('input', { type: 'password', placeholder: f.auto ? '개인키 비밀번호' : '백업 비밀번호', style: 'width:150px', 'aria-label': `${f.name} 비밀번호` });
      const kf = f.auto ? h('input', { type: 'file', accept: '.pem', 'aria-label': `${f.name} 개인키 파일`, class: 'w-m' }) : null;
      return h('tr', {}, h('td', { class: 'mono' }, f.name), h('td', {}, f.auto ? chip('자동', 'info') : '수동'), h('td', {}, f.at), h('td', { class: 'num' }, `${(f.bytes / 1024 / 1024).toFixed(1)}MB`),
        h('td', {}, kf, vp, ' ', btn('열어 확인', wrap(async () => {
          const r = f.auto ? await (async () => { if (!kf.files[0]) throw new Error('개인키 파일(.pem)을 고르세요.'); return post('backups/verify-auto', { name: f.name, key: await kf.files[0].text(), password: vp.value }); })()
            : await post('backups/verify', { name: f.name, passphrase: vp.value });
          toast(`정상: 접수 ${r.requests}건이 들어 있습니다`); }, msg), 'small')));
    }) : [h('tr', {}, h('td', { colspan: 5, class: 'muted' }, '아직 백업이 없습니다.'))]));
    const pc = await api('photo-purge');
    purgeInfo.textContent = pc.count ? `정리 대상 ${pc.count}장 (${(pc.bytes / 1024 / 1024).toFixed(1)}MB)` : '정리 대상 없음';
  }, msg);
  load();
  return h('div', { class: 'card' }, h('h2', {}, '수동 백업·사진 보관'),
    h('p', { class: 'muted' }, '백업 파일은 별도 백업 비밀번호로 암호화됩니다. 비밀번호를 잃으면 열 수 없으니 책임자가 따로 보관하세요. 백업 파일은 승인된 보안 매체로만 옮깁니다.'),
    h('div', { class: 'row' }, pass, pass2, btn('지금 백업', wrap(async () => {
      if (pass.value !== pass2.value) throw new Error('두 번 입력한 백업 비밀번호가 다릅니다.');
      const pw = pass.value; pass.value = pass2.value = '';
      const r = await runJob('backup', { passphrase: pw }, '수동 백업');
      toast(`백업했습니다: ${r.name} (${(r.bytes / 1024 / 1024).toFixed(1)}MB)`); load();
    }, msg), 'primary')),
    h('p', { class: 'small muted' }, '백업 폴더: ', dirEl),
    h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['파일', '종류', '만든 시각', '크기', '확인'].map((t) => h('th', {}, t)))), list)),
    h('p', { class: 'small muted' }, '복원은 PC 업무 화면을 끄고 명령으로 실행합니다: ', h('code', {}, 'python pc/pc_tool.py restore <백업 파일>'), ' (기존 DB는 지우지 않고 옆에 보관)'),
    h('hr'),
    h('div', { class: 'row' }, '회수한 사진 보관', days, '일', h('span', { class: 'grow' })),
    h('div', { class: 'row' }, '백업 폴더', bdir, btn('저장', wrap(async () => { await post('settings', { photo_retention_days: days.value, backup_dir: bdir.value }); toast('저장했습니다'); setBoot(null); render(); }, msg), 'small')),
    h('div', { class: 'row' }, purgeInfo, btn('기간 지난 사진 정리', wrap(async () => {
      if (!(await confirmDlg('기간 지난 사진 정리', `회수일로부터 ${st.photo_retention_days}일이 지나고 처리가 끝난 접수의 사진을 지웁니다. 기록(해시·시각)은 남습니다. 계속할까요?`, { ok: '정리', danger: true }))) return;
      const r = await runJob('photo-purge', {}, '사진 정리'); toast(`사진 ${r.count}장을 정리했습니다${r.bytes ? ` (${(r.bytes / 1024 / 1024).toFixed(1)}MB)` : ''}`); load();
    }, msg), 'small')),
    h('p', { class: 'small muted' }, '처리가 끝난 접수(수거 완료·취소 또는 그날 마감)의 사진만 정리합니다. 사진 원본은 지워지고 해시·회수 시각·정리 시각은 이력으로 남습니다.'), msg);
}

// ------------------------------------------------------------ 도움말 (역할별 하루 순서 + 예외 처리, 운영 매뉴얼 4·5장 요약). 화면 안 정적 글, 외부 연결 없음

export {
  SETTING_TABS, WD, addrCard, autoBackupCard, backupCard, capacityCard, coordTools, crewChooser,
  dayChecks, dongCard, externalCard, fp9, holidayCard, mapPickDlg, noticeCard, privacyCard,
  regState, screenSettings, settingsBasic, settingsCrews, settingsDevices, settingsFees, unloadCard, volumeCard,
};
