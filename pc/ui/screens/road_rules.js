import * as M from '/web/map.js';
import { api, btn, confirmDlg, h, routeEpoch, runJob, sessionOwner } from '../core.js';
import { mapIn } from '../widgets.js';

const DIRECTIONS = { forward: 'A→B', reverse: 'B→A' };
const PHASES = { before: '적용 전', after: '적용 후' };
const versionName = (id) => id === 'base' ? '기본 도로 자료 (base)' : id || '없음';
const roadName = (road) => road.name || '이름 없는 도로';
const validLine = (coords) => Array.isArray(coords) && coords.length >= 2 && coords.every((point) =>
  Array.isArray(point) && point.length === 2 && Number.isFinite(point[0]) && Number.isFinite(point[1])
  && Math.abs(point[0]) <= 180 && Math.abs(point[1]) <= 90);
const validRoad = (road) => !!road?.id && validLine(road.coords);
const km = (metres) => (metres / 1000).toFixed(2) + ' km';
const time = (seconds) => {
  const rounded = Math.round(seconds), minutes = Math.floor(rounded / 60), rest = rounded % 60;
  return minutes ? `${minutes}분${rest ? ` ${rest}초` : ''}` : `${rest}초`;
};
const hasMetrics = (trip) => trip?.code === 'Ok' && [trip.distance_m, trip.duration_s].every((value) => Number.isFinite(value) && value >= 0);
const verifiedTrip = (trip) => trip?.code === 'NoRoute' || (hasMetrics(trip) && validLine(trip.coordinates));
const verifiedPreview = (preview) => !!preview && Object.keys(PHASES).every((phase) =>
  Object.keys(DIRECTIONS).every((direction) => verifiedTrip(preview[phase]?.[direction])));
function tripText(trip) {
  if (hasMetrics(trip)) return `${km(trip.distance_m)} · ${time(trip.duration_s)}`;
  return trip?.code === 'NoRoute' ? '경로 없음' : '경로를 확인하지 못했습니다';
}
function comparison(preview) {
  const rows = Object.entries(DIRECTIONS).map(([key, label]) => {
    const before = preview.before?.[key], after = preview.after?.[key];
    const extra = hasMetrics(before) && hasMetrics(after) && after.distance_m > before.distance_m
      ? `우회 · +${km(after.distance_m - before.distance_m)}` : '';
    return h('tr', {}, h('th', { scope: 'row' }, label), h('td', {}, tripText(before)),
      h('td', {}, tripText(after), extra && h('div', { class: 'muted small' }, extra)));
  });
  return h('div', { class: 'tablewrap' }, h('table', { class: 'grid', 'aria-label': '양방향 검증 거리와 시간' },
    h('thead', {}, h('tr', {}, ...['검증 방향', ...Object.values(PHASES)].map((title) => h('th', { scope: 'col' }, title)))), h('tbody', {}, rows)));
}

function roadMap(alive, onPick, onRemove) {
  const box = h('div', { class: 'mapbox road-rules-map' });
  const note = h('p', { class: 'muted small', role: 'status', hidden: true });
  let map, ml, source, opening = false, fallbackTimer;
  let candidates = [], selected, route, phase, markedRoad, markers = [];
  const feature = (coords, kind) => ({ type: 'Feature', properties: { kind }, geometry: { type: 'LineString', coordinates: coords } });
  const data = () => ({ type: 'FeatureCollection', features: [
    ...(selected ? [feature(selected.coords, 'selected')] : candidates.map((road) => feature(road.coords, 'candidate'))),
    ...(route?.code === 'Ok' && validLine(route.coordinates) ? [feature(route.coordinates, phase)] : []),
  ] });

  function draw(fit = false) {
    if (!map) return;
    if (markedRoad !== selected) {
      markers.forEach((marker) => marker.remove());
      markers = [];
      markedRoad = selected;
      if (selected) {
        markers = [selected.coords[0], selected.coords.at(-1)].map(([lng, lat], i) => {
          const label = i ? 'B' : 'A';
          const marker = M.pin(ml, map, { lng, lat, label, name: `끝점 ${label}`, cls: 'c2 road-rules-end', onClick: () => {} });
          marker.getElement().tabIndex = -1;
          return marker;
        });
      }
    }
    const geojson = data();
    source?.setData(geojson);
    if (fit && selected) {
      const bounds = new ml.LngLatBounds();
      for (const { geometry } of geojson.features) geometry.coordinates.forEach((point) => bounds.extend(point));
      const pad = Math.min(48, Math.floor(Math.min(box.clientWidth, box.clientHeight) / 5));
      map.fitBounds(bounds, { padding: pad, maxZoom: 17, duration: 0 });
    }
  }

  async function mount() {
    if (map) { map.resize(); return; }
    if (opening || !alive()) return;
    opening = true;
    note.hidden = true;
    try {
      const built = await mapIn(box, { zoom: 15 });
      if (!alive()) { built.map.remove(); return; }
      ({ map, ml } = built);
      map.on('click', ({ lngLat }) => onPick(lngLat));
      map.on('remove', () => { clearTimeout(fallbackTimer); map = source = null; onRemove(); });
      const usePlainStyle = () => {
        if (!alive() || source || !note.hidden) return;
        note.textContent = '바탕 지도를 불러오지 못했습니다. 도로 후보, 선택한 구간과 검증 경로를 표시합니다.';
        note.hidden = false;
        map.setStyle({ version: 8, sources: {}, layers: [{ id: 'local-background', type: 'background', paint: { 'background-color': '#e9eef2' } }] });
      };
      const addRoadLayers = () => {
        clearTimeout(fallbackTimer);
        if (!alive()) return;
        map.off('error', usePlainStyle);
        map.addSource('road-rules', { type: 'geojson', data: data() });
        source = map.getSource('road-rules');
        const line = (id, filter, color, width) => map.addLayer({ id, filter, type: 'line', source: 'road-rules',
          layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': color, 'line-width': width } });
        const kind = ['get', 'kind'];
        line('road-rule-candidates', ['==', kind, 'candidate'], '#496782', 4);
        line('road-rule-segment-outline', ['==', kind, 'selected'], '#fff', 11);
        line('road-rule-segment', ['==', kind, 'selected'], '#a5560d', 8);
        line('road-rule-verification', ['in', kind, ['literal', ['before', 'after']]], ['match', kind, 'before', '#2557a7', '#006855'], 4);
        draw(true);
      };
      if (map.isStyleLoaded()) addRoadLayers();
      else {
        map.once('style.load', addRoadLayers);
        map.on('error', usePlainStyle);
        fallbackTimer = setTimeout(usePlainStyle, 4500);
        draw();
      }
    } catch {
      if (alive()) { note.textContent = '지도를 열지 못했습니다. 상태를 새로고침해 다시 시도하세요.'; note.hidden = false; }
    } finally { opening = false; }
  }

  return { box, note, mount, update(roads, choice, path, stage, fit) {
    candidates = roads; selected = choice; route = path; phase = stage; draw(fit);
  } };
}

export function roadRulesCard() {
  const owner = sessionOwner(), epoch = routeEpoch, hash = location.hash;
  const alive = () => card.isConnected && routeEpoch === epoch && location.hash === hash && sessionOwner() === owner;
  let state, refreshing = false, verified = false, action = '', stateRequest, lookup;
  let roads = [], road, preview, revision = 0, phase = 'after';
  const notice = (node, text, error = false) => {
    node.textContent = text;
    node.hidden = !text;
    node.className = error ? 'notice bad' : 'notice info';
    node.setAttribute('role', error ? 'alert' : 'status');
  };
  const cancelLookup = () => { lookup?.abort(); lookup = null; };
  const map = roadMap(alive, findRoads, () => { cancelLookup(); stateRequest?.abort(); });
  const statusNote = h('p', { role: 'status' });
  const actionNote = h('p', { role: 'status', hidden: true });
  const lookupNote = h('p', { role: 'status', class: 'small muted' }, '지도에서 도로를 누르세요.');
  const candidates = h('ul', { class: 'road-rules-candidates', 'aria-label': '가까운 도로 구간 후보' });
  const selection = h('p', { class: 'small' });
  const direction = h('select', { required: true }, h('option', { value: '' }, '허용할 방향 선택'),
    ...Object.entries(DIRECTIONS).map(([key, label]) => h('option', { value: key }, label)));
  const checkedOn = h('input', { type: 'date', required: true });
  const evidence = h('textarea', { rows: 2, required: true, maxlength: 300, placeholder: '현장 안내 표지 등' });
  const formFields = h('fieldset', { class: 'road-rules-fields' }, h('label', { class: 'f' }, '허용 방향', direction),
    h('label', { class: 'f' }, '확인일', checkedOn), h('label', { class: 'f' }, '확인 근거', evidence));
  const previewButton = h('button', { type: 'submit', class: 'primary' }, '양방향 미리보기');
  const form = h('form', { oninput: () => invalidate('입력 내용이 바뀌었습니다. 미리보기를 다시 실행하세요.'),
    onsubmit: (event) => { event.preventDefault(); makePreview(); } }, formFields, previewButton);
  const apply = btn('확인 후 적용', () => changeActive('apply'), 'primary');
  const rollback = btn('이전 판으로 되돌리기', () => changeActive('rollback'), 'small');
  const refresh = btn('상태 새로고침', () => loadState(), 'small');
  const activeVersion = h('b'), previousVersion = h('b');
  const versions = h('section', { 'aria-label': '도로 자료 버전', class: 'row road-rules-versions', hidden: true },
    h('span', {}, '현재 판 ', activeVersion), h('span', {}, '이전 판 ', previousVersion), h('span', { class: 'grow' }), rollback);
  const viewDirection = h('select', { 'aria-label': '지도 검증 방향', onchange: () => drawMap(true) },
    ...Object.entries(DIRECTIONS).map(([key, label]) => h('option', { value: key }, label)));
  const phaseButtons = Object.entries(PHASES).map(([key, label]) => btn(label, () => { phase = key; drawMap(true); }, 'small', { 'aria-pressed': 'false' }));
  const legend = h('p', { class: 'small muted', role: 'status' });
  const mapTools = h('div', { class: 'row road-rules-map-tools', hidden: true },
    h('label', {}, '검증 방향 ', viewDirection), h('div', { class: 'row', role: 'group', 'aria-label': '검증 경로 시점' }, phaseButtons));
  const result = h('section', { class: 'card', 'aria-label': '양방향 미리보기', hidden: true });
  const workspace = h('div', { class: 'road-rules-workspace', hidden: true },
    h('section', { class: 'card', 'aria-label': '도로 구간 지도' }, h('h3', {}, '도로 구간 지도'), mapTools, map.box, map.note, legend),
    h('section', { class: 'card', 'aria-label': '도로 구간 선택과 통행 규칙' },
      h('h3', {}, '가까운 도로 구간'), lookupNote, candidates, selection, form));
  const applied = h('section', { class: 'card', 'aria-label': '적용 규칙', hidden: true });
  const card = h('div', { class: 'road-rules' }, h('section', { class: 'card' },
    h('div', { class: 'row' }, h('h2', {}, '도로 통행 정보'), h('span', { class: 'grow' }), refresh), versions, statusNote, actionNote), workspace, result, applied);

  const workLocked = () => !verified || !state || state.busy || refreshing || !!action;
  const previewLocked = () => workLocked() || !state.available;
  const editLocked = () => action === 'apply' || action === 'rollback';
  const draft = () => ({ road_id: road?.id, direction: direction.value, checked_on: checkedOn.value, evidence: evidence.value.trim(), base_id: state?.active_id });
  const matchesDraft = (value, fields) => !!value?.preview_id && validRoad(value.road) && value.road.id === fields.road_id
    && ['direction', 'checked_on', 'evidence', 'base_id'].every((key) => value[key] === fields[key]);
  const complete = (fields) => fields.road_id && fields.direction && fields.checked_on && fields.evidence;

  function sync() {
    formFields.disabled = !road || editLocked();
    previewButton.disabled = previewLocked() || !!lookup || !complete(draft());
    apply.disabled = previewLocked() || !!lookup || !matchesDraft(preview, draft()) || !verifiedPreview(preview);
    rollback.disabled = workLocked() || !state?.previous_id || state.previous_id === state.active_id;
    refresh.disabled = refreshing || !!action;
    candidates.querySelectorAll('button').forEach((button) => { button.disabled = editLocked(); });
  }

  function drawMap(fit = false) {
    mapTools.hidden = !preview;
    const travel = preview?.[phase]?.[viewDirection.value];
    legend.textContent = preview ? `${PHASES[phase]} · ${DIRECTIONS[viewDirection.value]} · ${tripText(travel)}. 갈색 선은 선택한 구간입니다.`
      : road ? '갈색 선은 선택한 구간입니다. A와 B는 허용 방향을 정하는 기준입니다.' : '';
    phaseButtons.forEach((button, i) => button.setAttribute('aria-pressed', String(phase === Object.keys(PHASES)[i])));
    map.update(roads, road, travel, phase, fit);
  }

  function invalidate(message = '') {
    if (preview && message) notice(actionNote, message);
    revision++;
    preview = null;
    result.hidden = true;
    drawMap();
    sync();
  }

  function drawCandidates() {
    candidates.replaceChildren(...roads.map((candidate) => h('li', {},
      btn(h('span', { class: 'road-rules-option' }, h('span', {}, roadName(candidate)),
        Number.isFinite(candidate.distance_m) && h('small', { class: 'muted' }, `${Math.round(candidate.distance_m)}m`)), () => select(candidate), 'small',
      { disabled: editLocked(), 'aria-pressed': String(candidate.id === road?.id) }))));
    const format = ([lon, lat]) => `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
    selection.textContent = road ? `${roadName(road)} · A (${format(road.coords[0])}) · B (${format(road.coords.at(-1))})` : '';
  }

  function select(candidate) {
    if (!alive() || editLocked()) return;
    const changed = candidate.id !== road?.id;
    road = candidate;
    if (changed) {
      direction.value = checkedOn.value = evidence.value = '';
      invalidate('선택한 구간이 바뀌었습니다. 미리보기를 다시 실행하세요.');
    }
    drawCandidates();
    drawMap(true);
    direction.focus({ preventScroll: true });
  }

  async function findRoads({ lat, lng }) {
    if (!alive() || !state?.available || editLocked()) return;
    cancelLookup();
    const request = new AbortController();
    lookup = request;
    roads = [];
    drawCandidates();
    sync();
    notice(lookupNote, '가까운 도로 구간을 찾는 중입니다.');
    const current = () => alive() && lookup === request;
    try {
      const params = new URLSearchParams({ lat: lat.toFixed(6), lon: lng.toFixed(6) });
      const data = await api(`road-rules/roads?${params}`, { signal: request.signal, timeoutMs: 10000, _prefsOwner: owner });
      if (!current()) return;
      roads = (data.roads || []).filter(validRoad);
      drawCandidates();
      drawMap();
      notice(lookupNote, roads.length ? '확인한 도로 구간을 선택하세요.' : '가까운 도로 구간이 없습니다. 지도에서 다른 위치를 누르세요.');
    } catch (error) {
      if (current()) notice(lookupNote, error.message, true);
    } finally {
      if (current()) { lookup = null; sync(); }
    }
  }

  function showPreview(value) {
    preview = value;
    phase = 'after';
    viewDirection.value = value.direction === 'forward' ? 'reverse' : 'forward';
    result.replaceChildren(h('h3', {}, '양방향 미리보기'),
      h('p', {}, `${roadName(value.road)} · 허용 ${DIRECTIONS[value.direction]} · 기준 ${versionName(value.base_id)}`),
      comparison(value), h('div', { class: 'row road-rules-result-actions' }, apply));
    result.hidden = false;
    drawMap(true);
    const usable = verifiedPreview(value);
    notice(actionNote, usable ? '적용 대기 중입니다. 양방향 경로를 확인한 뒤 ‘확인 후 적용’을 누르세요.' : '양방향 경로를 모두 확인하지 못했습니다. 미리보기를 다시 실행하세요.', !usable);
    sync();
  }

  function setState(next, restorePending = false) {
    if (typeof next?.available !== 'boolean') throw new Error('도로 통행 정보 응답을 확인하지 못했습니다.');
    const versionChanged = !!state && next.active_id !== state.active_id;
    state = next;
    verified = true;
    if (versionChanged || !next.available) {
      cancelLookup();
      roads = [];
      drawCandidates();
      notice(lookupNote, '지도에서 도로를 누르세요.');
    }
    if (!next.available || versionChanged || (preview && preview.preview_id !== next.pending?.preview_id)) invalidate();
    const pending = next.pending;
    if (restorePending && next.available && pending?.preview_id && pending.base_id === next.active_id && validRoad(pending.road)) {
      road = pending.road;
      direction.value = pending.direction; checkedOn.value = pending.checked_on; evidence.value = pending.evidence;
      drawCandidates();
      if (matchesDraft(pending, draft())) showPreview(pending);
    }
    versions.hidden = false;
    activeVersion.textContent = versionName(next.active_id);
    previousVersion.textContent = versionName(next.previous_id);
    workspace.hidden = !next.available;
    if (next.available) map.mount();
    applied.hidden = false;
    const rules = next.rules || [];
    applied.replaceChildren(h('h3', {}, '적용 규칙'), rules.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ...['도로 구간', '허용 방향', '확인일', '확인 근거'].map((label) => h('th', { scope: 'col' }, label)))),
      h('tbody', {}, ...rules.map((rule) => h('tr', {}, h('td', {}, rule.name || rule.road_id), h('td', {}, DIRECTIONS[rule.direction]),
        h('td', {}, rule.checked_on), h('td', {}, rule.evidence)))))) : h('p', { class: 'muted' }, '적용된 규칙이 없습니다.'));
    const reason = next.available ? '' : next.reason || '도로 통행 정보를 사용할 수 없습니다.';
    const busy = next.busy ? '다른 도로 작업이 진행 중입니다. 완료 후 상태를 새로고침하세요.' : '';
    notice(statusNote, [reason, busy].filter(Boolean).join(' '));
    sync();
  }

  async function loadState() {
    if (refreshing || !alive()) return;
    refreshing = true;
    const request = new AbortController();
    stateRequest = request;
    notice(statusNote, '도로 통행 정보를 불러오는 중입니다.');
    sync();
    try {
      const next = await api('road-rules', { signal: request.signal, timeoutMs: 10000, _prefsOwner: owner });
      if (!alive()) return;
      const restore = !state && !road;
      if (preview && preview.base_id !== next?.active_id) notice(actionNote, '도로 자료의 버전이 바뀌었습니다. 미리보기를 다시 실행하세요.', true);
      else if (preview && preview.preview_id !== next?.pending?.preview_id) notice(actionNote, '적용 대기 중인 미리보기가 바뀌었습니다. 다시 계산하세요.', true);
      setState(next, restore);
    } catch (error) {
      if (alive()) { verified = false; notice(statusNote, error.message, true); }
    } finally {
      if (alive()) { refreshing = false; stateRequest = null; sync(); }
    }
  }

  async function makePreview() {
    const payload = draft();
    if (!alive() || previewLocked() || lookup || !complete(payload)) return;
    invalidate();
    const started = revision;
    action = 'preview';
    notice(actionNote, '양방향 미리보기를 계산 중입니다.');
    sync();
    try {
      const value = await runJob('road-rules/preview', payload, '도로 통행 미리보기');
      if (!alive()) return;
      if (started !== revision || !matchesDraft(value, draft())) {
        notice(actionNote, '입력값 또는 도로 자료가 바뀌었습니다. 미리보기를 다시 실행하세요.');
        return;
      }
      showPreview(value);
    } catch (error) {
      if (alive()) { notice(actionNote, error.message, true); await loadState(); }
    } finally {
      if (alive()) { action = ''; sync(); }
    }
  }

  async function changeActive(kind) {
    const applying = kind === 'apply', saved = preview, { active_id, previous_id } = state || {};
    const allowed = () => alive() && !workLocked() && state.active_id === active_id
      && (applying ? state.available && !lookup && preview === saved && matchesDraft(saved, draft()) && verifiedPreview(saved)
        : !!previous_id && previous_id !== active_id && state.previous_id === previous_id);
    if (!allowed()) return;
    const confirmed = applying ? await confirmDlg('도로 통행 정보 적용',
      `${roadName(saved.road)}의 ${DIRECTIONS[saved.direction]} 방향을 허용합니다. 확인일 ${saved.checked_on} · ${saved.evidence}`, { ok: '적용' })
      : await confirmDlg('이전 판으로 되돌리기', `${versionName(active_id)}에서 ${versionName(previous_id)}으로 되돌립니다.`, { ok: '되돌리기', danger: true });
    if (!confirmed || !allowed()) return;
    if (lookup) { cancelLookup(); notice(lookupNote, '지도에서 도로를 누르세요.'); }
    action = kind;
    const label = applying ? '도로 통행 정보 적용' : '도로 자료 되돌리기';
    notice(actionNote, `${label} 중입니다.`);
    sync();
    try {
      const next = await runJob(`road-rules/${kind}`, applying ? { preview_id: saved.preview_id } : { active_id }, label);
      if (!alive()) return;
      invalidate();
      setState(next);
      notice(actionNote, applying ? '도로 통행 규칙을 적용했습니다.' : '도로 자료를 이전 판으로 되돌렸습니다.');
    } catch (error) {
      if (alive()) { invalidate(); notice(actionNote, error.message, true); await loadState(); }
    } finally {
      if (alive()) { action = ''; sync(); }
    }
  }

  sync();
  setTimeout(loadState, 0);
  return card;
}
