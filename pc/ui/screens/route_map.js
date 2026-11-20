// PC 배차 지도: 방문 순서는 유지하고 로컬 OSRM 선만 서버에서 조회한다.
import * as M from '/web/map.js';
import { STATUS, api, boot, btn, h, liveMaps, sessionOwner } from '../core.js';
import { mapIn } from '../widgets.js';

const coords = (point) => Number.isFinite(point?.lon) && Number.isFinite(point?.lat) ? [point.lon, point.lat] : null;
const ALL = '', UNASSIGNED = 'unassigned';
const PATH_ERRORS = {
  STARTING: '도로 자료 준비 중', MISSING: '도로 자료 없음', INVALID_PACK: '도로 자료 확인 필요', FAILED: '도로 서비스 시작 실패',
  NoSegment: '도로 자료 범위 확인 필요', NoRoute: '연결된 도로 경로 없음', TooBig: '한 번에 조회할 지점 수 초과',
  SETTINGS_CHANGED: '도로 설정이 바뀜', UNAVAILABLE: '도로 조회 실패', TOLL_EXCLUSION_UNAVAILABLE: '현재 도로 자료에서 유료도로 제외를 적용할 수 없음',
};

function straightKm(path) {
  const rad = Math.PI / 180;
  let km = 0;
  for (let i = 1; i < path.length; i++) {
    const [a, b] = path[i - 1], [c, d] = path[i];
    const term = Math.sin((d - b) * rad / 2) ** 2 + Math.cos(b * rad) * Math.cos(d * rad) * Math.sin((c - a) * rad / 2) ** 2;
    km += 12742 * Math.asin(Math.sqrt(Math.min(1, term)));
  }
  return km;
}

function directionImage() {
  const canvas = document.createElement('canvas');
  canvas.width = 36;
  canvas.height = 32;
  const ink = canvas.getContext('2d');
  ink.scale(2, 2);
  ink.lineCap = ink.lineJoin = 'round';
  ink.beginPath();
  ink.moveTo(5.5, 3);
  ink.lineTo(12.5, 8);
  ink.lineTo(5.5, 13);
  ink.strokeStyle = '#fff';
  ink.lineWidth = 5;
  ink.stroke();
  ink.strokeStyle = '#10263d';
  ink.lineWidth = 2.75;
  ink.stroke();
  return ink.getImageData(0, 0, canvas.width, canvas.height);
}

export function confirmedMapRoutes(batches) {
  return batches.filter((batch) => batch.status === 'ACTIVE').map((batch) => ({
    id: batch.crew_id, name: batch.crew_name, batchId: batch.id, stops: batch.stops.filter((stop) => !stop.map_skipped),
    depot: batch.map_route?.depot, end: batch.map_route?.end, complete: batch.map_route?.complete,
  }));
}

export function routeMap({ routes, depot, unassigned = [], view = {}, boxClass = '', empty, isCurrent = () => true }) {
  const points = new Map();
  routes.forEach((route, ri) => route.stops.forEach((stop, i) => {
    const at = coords(stop);
    if (!at) return;
    points.set(stop.request_id, { ...stop, at, team: route.id, label: `${ri + 1}-${i + 1}`,
      visit: `${route.name} · ${i + 1}번 방문`, color: stop.status === 'DONE' ? 'done' : `c${ri % 4 + 1}` });
  }));
  unassigned.forEach((stop) => {
    const at = coords(stop);
    if (at) points.set(stop.request_id, { ...stop, at, team: UNASSIGNED, label: '미', color: 'un', visit: '미배정' });
  });
  const groups = routes.map(({ id, name }) => ({ id, name }));
  if (Array.from(points.values()).some((point) => point.team === UNASSIGNED)) groups.push({ id: UNASSIGNED, name: '미배정' });
  if (!groups.some(({ id }) => id === view.team)) view.team = ALL;

  const box = h('div', { class: `mapbox ${boxClass}` }, empty);
  const count = h('span', { class: 'muted small', role: 'status' });
  const picker = h('select', { 'aria-label': '지도 수거팀', disabled: !groups.length, onchange: () => changeTeam(picker.value) },
    h('option', { value: ALL }, '전체 팀'), ...groups.map(({ id, name }) => h('option', { value: id }, name)));
  picker.value = view.team;
  const details = h('section', { class: 'route-map-details', 'aria-label': '선택한 배차 지점', 'aria-live': 'polite', tabindex: -1 });
  const distance = h('div', { class: 'route-map-distances muted small', role: 'status', 'aria-label': '지도 경로·거리' });
  const baseNote = h('p', { class: 'muted small', role: 'status', hidden: true }, '바탕 지도를 불러오지 못해 방문 지점과 경로를 표시합니다.');
  const dialog = h('dialog', { class: 'route-map-dialog', 'aria-label': '확대 노선 지도',
    onkeydown: (event) => event.stopPropagation() });
  const expand = btn('지도 크게 보기', openLargeMap, 'small', { 'aria-haspopup': 'dialog', disabled: true });
  const restore = btn('원래 크기로 보기', () => dialog.close(), 'small primary', { hidden: true });
  const panel = h('div', { class: 'route-map' }, h('div', { class: 'route-map-tools' }, h('label', {}, '지도 수거팀', picker),
    h('div', { class: 'row' }, count, expand, restore)), details, box, baseNote, distance);
  const element = h('div', { class: 'route-map-host' }, panel, dialog);
  const controls = new Map(), markers = new Map();
  const owner = sessionOwner(), roadEnabled = boot.settings?.external?.travel_method === 'OSRM';
  let mountedMap, disposed = false;
  const active = () => !disposed && box.isConnected && isCurrent() && sessionOwner() === owner;
  const releaseMap = (map) => {
    const index = liveMaps.indexOf(map);
    if (index >= 0) liveMaps.splice(index, 1);
    map.remove();
  };
  const resizeMap = () => requestAnimationFrame(() => { if (active()) mountedMap?.resize(); });
  function openLargeMap() {
    if (!active() || !mountedMap || dialog.open) return;
    dialog.showModal();
    dialog.append(panel);
    expand.hidden = true;
    restore.hidden = false;
    restore.focus();
    resizeMap();
  }
  dialog.addEventListener('close', () => {
    if (dialog.open || panel.parentNode !== dialog) return;
    element.prepend(panel);
    restore.hidden = true;
    expand.hidden = false;
    if (active() && mountedMap) {
      resizeMap();
      expand.focus({ preventScroll: true });
    }
  });
  let drawMap = () => {}, centerOn = () => {}, drawLines = () => {}, fitRoad = () => {}, pathsRequest;
  const includes = (team) => view.team === ALL || team === view.team;
  const visiblePoints = () => Array.from(points.values()).filter((point) => includes(point.team));
  const routeLines = routes.map((route, i) => {
    const origin = coords(route.depot) || coords(depot), end = coords(route.end);
    const stops = route.stops.flatMap((stop) => [coords(stop), coords(stop.via)].filter(Boolean));
    const lastStop = route.stops.at(-1);
    const finalUnload = lastStop?.final_unload ? lastStop.via : null;
    const path = [origin, ...stops, end].filter(Boolean);
    const complete = route.complete !== false && origin && end && route.stops.every((stop) => !!coords(stop));
    const straight = path.length > 1 && stops.length ? { type: 'LineString', coordinates: path } : null;
    return { id: route.id, name: route.name, batchId: route.batchId, color: i % 4, origin, finalUnload, path, straight, complete,
      km: straight ? straightKm(path) : 0, road: null, reason: '', pending: false };
  });
  const visibleRoutes = () => routeLines.filter((line) => includes(line.id));
  const visibleLines = () => visibleRoutes().filter((line) => line.straight);
  const geojson = () => ({ type: 'FeatureCollection', features: visibleLines().map((line) => ({ type: 'Feature',
    properties: { c: line.color, road: !!line.road }, geometry: line.road?.geometry || line.straight })) });

  function showPaths() {
    const visible = visibleLines();
    const hasRoad = visible.some((line) => line.road);
    distance.replaceChildren(...visible.map((line) => {
      const kind = line.road ? '도로' : line.complete ? '직선' : '표시 구간 직선';
      const km = line.road?.distance_km ?? line.km;
      const hint = line.pending ? ' · 도로 조회 중' : line.reason ? ` · ${line.reason}` : '';
      const toll = line.road?.toll_excluded === true ? ' · 유료도로 제외' : '';
      const finish = line.finalUnload ? ' · 최종 하차 후 차고지 복귀' : '';
      return h('div', {}, `${line.name} · ${kind} ${km.toFixed(2)}km${toll}${hint}${finish}`);
    }), ...(visible.some((line) => !line.road) ? [h('div', {}, '직선(점선) · 방향 없음 · 운행 참고 불가')] : []),
    ...(hasRoad ? [h('div', {}, '화살표는 도로 경로의 진행 방향입니다.'),
      h('a', { href: 'https://www.openstreetmap.org/copyright', target: '_blank', rel: 'noopener noreferrer' }, '© OpenStreetMap contributors')] : []));
    drawLines();
  }

  function loadPaths() {
    pathsRequest?.abort();
    if (!active()) return;
    const request = new AbortController();
    pathsRequest = request;
    for (const line of visibleLines()) {
      line.pending = false;
      if (!roadEnabled || line.road) continue;
      if (!line.complete || line.path.length > 500) {
        line.reason = line.complete ? PATH_ERRORS.TooBig : '방문·경유·종료 위치 확인 필요';
        continue;
      }
      line.pending = true;
      line.reason = '';
      const body = { points: line.path, ...(line.batchId ? { batch_id: line.batchId } : {}) };
      const current = () => active() && pathsRequest === request && !request.signal.aborted;
      api('route-path', { method: 'POST', body, signal: request.signal, _prefsOwner: owner }).then((result) => {
        if (!current()) return;
        if (result.source === 'OSRM' && result.geometry?.type === 'LineString' && Number.isFinite(result.distance_km)) line.road = result;
        else line.reason = PATH_ERRORS[result.reason] || (result.reason === 'DISABLED' ? '' : PATH_ERRORS.UNAVAILABLE);
      }).catch(() => {
        if (current()) line.reason = PATH_ERRORS.UNAVAILABLE;
      }).finally(() => {
        if (!current()) return;
        line.pending = false;
        showPaths();
        if (line.road) fitRoad(line.road.geometry);
      });
    }
    showPaths();
  }

  function updateSelection(id) {
    const point = points.get(id);
    const selected = point && includes(point.team) ? point : null;
    view.point = selected?.request_id;
    for (const buttons of [markers, controls]) {
      for (const [key, button] of buttons) button.setAttribute('aria-pressed', String(key === view.point));
    }
    const status = selected && (STATUS[selected.status] || selected.status);
    details.replaceChildren(...(selected ? [
      h('strong', {}, selected.name), selected.receipt_no ? h('span', { class: 'muted small' }, selected.receipt_no) : null,
      h('p', {}, selected.address),
      h('p', { class: 'route-map-meta' }, [selected.visit, status, selected.eta && `도착 ${selected.eta}`, selected.reason].filter(Boolean).join(' · ')),
    ].filter(Boolean) : [h('p', { class: 'muted small' }, points.size ? '핀 또는 방문 목록에서 지점을 선택하세요.' : '지도에 표시할 방문 지점이 없습니다.')]));
  }

  function changeTeam(team) {
    view.team = picker.value = team;
    drawMap();
    count.textContent = `${visiblePoints().length}곳 표시`;
    updateSelection(view.point);
    loadPaths();
  }

  function visitButton(id, label, cls) {
    const point = points.get(id);
    if (!point) return null;
    const button = btn(label, () => {
      if (!includes(point.team)) changeTeam(point.team);
      updateSelection(id);
      centerOn(point.at);
      (markers.get(id) || details).focus();
    }, cls, { 'aria-label': `${point.name} 지도에서 보기`, 'aria-pressed': String(id === view.point) });
    controls.set(id, button);
    return button;
  }
  changeTeam(view.team);

  const start = coords(routes[0]?.depot) || coords(depot);
  setTimeout(async () => {
    if (!active()) return;
    loadPaths();
    try {
      const { map, ml } = await mapIn(box, { center: start, zoom: 13 });
      if (!active()) { pathsRequest?.abort(); releaseMap(map); return; }
      mountedMap = map;
      expand.disabled = false;
      let fallbackTimer;
      map.on('remove', () => {
        mountedMap = null;
        expand.disabled = true;
        if (dialog.open) dialog.close();
        clearTimeout(fallbackTimer);
        pathsRequest?.abort();
        drawMap = centerOn = drawLines = fitRoad = () => {};
        markers.clear();
      });
      centerOn = (at) => {
        const { x, y } = map.project(at);
        const margin = 42;
        if (x < margin || y < margin || x > box.clientWidth - margin || y > box.clientHeight - margin) map.jumpTo({ center: at });
      };
      const drawn = [];
      let routeSource;
      let manualCamera = false, cameraTeam = view.team;
      map.on('movestart', (event) => { if (event.originalEvent) manualCamera = true; });
      const fitVisible = () => {
        const bounds = new ml.LngLatBounds();
        for (const { path, road } of visibleRoutes()) {
          path.forEach((at) => bounds.extend(at));
          road?.geometry.coordinates.forEach((at) => bounds.extend(at));
        }
        visiblePoints().forEach(({ at }) => bounds.extend(at));
        if (bounds.isEmpty() || !points.size) return;
        const padding = Math.min(50, Math.floor(Math.min(box.clientWidth, box.clientHeight) / 5));
        map.fitBounds(bounds, { padding, maxZoom: 15, duration: 0 });
      };
      fitRoad = (geometry) => {
        if (manualCamera || view.point || box.contains(document.activeElement) || map.isMoving()) return;
        const frame = map.getBounds();
        if (geometry.coordinates.some((at) => !frame.contains(at))) fitVisible();
      };
      drawMap = () => {
        if (cameraTeam !== view.team) {
          manualCamera = false;
          cameraTeam = view.team;
        }
        drawn.splice(0).forEach((marker) => marker.remove());
        markers.clear();
        const origins = new Set();
        const addOrigin = (at) => {
          if (!at || origins.has(String(at))) return;
          origins.add(String(at));
          const marker = M.pin(ml, map, { lng: at[0], lat: at[1], label: '출발', name: '출발지', cls: 'me' });
          marker.getElement().tabIndex = -1;
          drawn.push(marker);
        };
        if (!routeLines.length) addOrigin(start);
        visibleRoutes().forEach(({ origin }) => addOrigin(origin));
        const finalSites = new Set();
        for (const { finalUnload } of visibleRoutes()) {
          const at = coords(finalUnload);
          if (!at || origins.has(String(at)) || finalSites.has(String(at))) continue;
          finalSites.add(String(at));
          const marker = M.pin(ml, map, { lng: at[0], lat: at[1], label: '하차', name: `최종 하차지 · ${finalUnload.label || '반입지'}`, cls: 'me' });
          marker.getElement().tabIndex = -1;
          drawn.push(marker);
        }
        visiblePoints().forEach((point) => {
          const marker = M.pin(ml, map, { lng: point.lon, lat: point.lat, label: point.label, cls: point.color,
            name: `${point.visit} · ${point.name}`, onClick: () => updateSelection(point.request_id) });
          drawn.push(marker); markers.set(point.request_id, marker.getElement());
        });
        drawLines();
        fitVisible();
      };
      drawMap(); updateSelection(view.point);
      const usePlainStyle = () => {
        if (!active() || routeSource || !baseNote.hidden) return;
        baseNote.hidden = false;
        map.setStyle({ version: 8, sources: {}, layers: [{ id: 'local-background', type: 'background', paint: { 'background-color': '#eef1f4' } }] });
      };
      const addRoutes = () => {
        clearTimeout(fallbackTimer);
        map.off('error', usePlainStyle);
        if (!active()) return;
        map.addSource('routes', { type: 'geojson', data: geojson() });
        routeSource = map.getSource('routes');
        drawLines = () => routeSource.setData(geojson());
        const paint = { 'line-width': 3, 'line-color': ['match', ['get', 'c'], 0, '#2557a7', 1, '#a5560d', 2, '#6c3fb5', '#0e7490'] };
        const roadOnly = ['==', ['get', 'road'], true];
        map.addLayer({ id: 'routes', type: 'line', source: 'routes', filter: ['==', ['get', 'road'], false], paint: { ...paint, 'line-dasharray': [2, 1] } });
        map.addLayer({ id: 'routes-road', type: 'line', source: 'routes', filter: roadOnly, paint });
        map.addImage('route-direction-chevron', directionImage(), { pixelRatio: 2 });
        map.addLayer({ id: 'routes-direction', type: 'symbol', source: 'routes', filter: roadOnly,
          layout: { 'symbol-placement': 'line', 'symbol-spacing': 48, 'icon-image': 'route-direction-chevron',
            'icon-rotation-alignment': 'map', 'icon-keep-upright': false, 'icon-offset': [0, 4],
            'icon-allow-overlap': true, 'icon-ignore-placement': true } });
      };
      if (map.isStyleLoaded()) addRoutes();
      else {
        map.once('style.load', addRoutes);
        map.on('error', usePlainStyle);
        fallbackTimer = setTimeout(usePlainStyle, 4000);
      }
    } catch {
      if (active()) box.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다. 방문 목록에서 지점을 확인할 수 있습니다.'), ...(empty ? [empty] : []));
    }
  }, 0);
  return { element, visitButton, dispose() {
    disposed = true;
    pathsRequest?.abort();
    if (mountedMap) releaseMap(mountedMap);
  } };
}
