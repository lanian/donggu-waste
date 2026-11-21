// 휴대폰 화면: 지도·위치 지정
import * as M from '../map.js';
import { deriveStop } from '../fees.js';
import { addEvent, ask, backBtn, btn, commit, findStop, go, h, isSolo, liveMap, msgBox, pcStops, pcWorkDate, say, setLiveMap, setMapEnabled, st, toast, todayKST, ui } from '../core.js';
import { addrSearch } from './intake.js';
import { stopState } from './stoplist.js';

// 핀 하나의 표시: 번호, 이름 "{n}번 · {진행 상태}"(목록 카드와 같은 판정 stopState), 진행 모양(완료 ✓·미수거/보류 점선은 style.css), 고른 핀
function pinFor(s, d, focusId = null) {
  const state = stopState(s, d);
  return { label: String(s.seq), name: `${s.seq}번 · ${state.label}`, cls: 'p-' + state.progress + (s.request_id === focusId ? ' focus' : '') };
}
const mapNotice = () => h('p', { class: 'small' }, '바탕 지도 조각만 OpenFreeMap에서 받습니다. 이름·연락처·주소와 핀 위치는 지도 서버로 보내지 않습니다.');
function mapStops() {
  if (isSolo()) {
    const view = ui.soloView || 'TODAY', today = todayKST(), tomorrow = todayKST(1);
    return st.requests.filter((r) => r.status !== 'CANCELLED' && (view === 'TODAY' ? r.planned_date === today : view === 'TOMORROW' ? r.planned_date === tomorrow : true));
  }
  return pcStops().filter((s) => !s.removed && !(s.field && s.planned_date !== pcWorkDate()));
}

async function screenMap(focusId) {
  // 목록의 [지도]로 연 지도는 [← 목록](또는 연 곳), 접수에서 연 지도는 [← 접수]
  const back = h('header', { class: 'bar' }, focusId ? backBtn(`#/stop/${focusId}`) : backBtn(), h('h1', { class: 'bar-title' }, '지도'));
  // 지도가 꺼져 있으면 설정의 [지도 사용 켜기]와 같은 동작으로 바로 켠다
  if (!st.mapEnabled) return h('main', {}, back, h('p', { class: 'empty' }, '지도가 꺼져 있습니다.'),
    h('div', { class: 'empty-actions' }, btn('지도 켜기', () => setMapEnabled(true), 'big')));
  const items = mapStops().map((s) => ({ s, d: deriveStop(s, st.events) }));
  const placed = items.filter((x) => x.d.loc), missing = items.filter((x) => !x.d.loc);
  const box = h('div', { class: 'mapbox', id: 'map' });
  const msg = msgBox();
  const view = h('main', {}, back, box,
    h('div', { class: 'grid2' }, btn('📍 내 위치', async () => {
      try { const p = await M.myPosition(); if (liveMap) { liveMap.flyTo({ center: p, zoom: 16 }); M.pin(ui.ml, liveMap, { lng: p[0], lat: p[1], label: '나', name: '내 위치', cls: 'me' }); } }
      catch (e) { say(msg, e.message, true); }
    }, 'opt'), btn('전체 보기', () => fitAll(), 'opt')),
    msg, mapNotice(),
    missing.length ? h('section', { class: 'card' }, h('h2', {}, `위치 미지정 ${missing.length}건`),
      h('ul', { class: 'stops' }, missing.map(({ s }) => h('li', {}, h('a', { href: `#/pick/${s.request_id}`, class: 'stop' },
        h('span', { class: 'seq' }, s.seq), h('span', { class: 'body' }, h('strong', {}, s.name), h('span', { class: 'addr' }, s.address)),
        h('span', { class: 'eta' }, '위치 지정 →')))))) : null);
  const fitAll = () => {
    if (!liveMap || !placed.length) return;
    if (placed.length === 1) return liveMap.flyTo({ center: [placed[0].d.loc.lon, placed[0].d.loc.lat], zoom: 16 });
    const b = new ui.ml.LngLatBounds();
    placed.forEach((x) => b.extend([x.d.loc.lon, x.d.loc.lat]));
    liveMap.fitBounds(b, { padding: 48, maxZoom: 16, duration: 0 });
  };
  setTimeout(async () => {
    try {
      const focus = placed.find((x) => x.s.request_id === focusId);
      const { map, ml } = await M.createMap(box, { center: focus ? [focus.d.loc.lon, focus.d.loc.lat] : placed[0] ? [placed[0].d.loc.lon, placed[0].d.loc.lat] : M.DEFAULT_CENTER, zoom: focus ? 17 : 14 });
      setLiveMap(map); ui.ml = ml;
      for (const { s, d } of placed) {
        M.pin(ml, map, { lng: d.loc.lon, lat: d.loc.lat, ...pinFor(s, d, focusId), onClick: () => go(`#/stop/${s.request_id}`) });
      }
      if (!focus) fitAll();
    } catch (e) { say(msg, '지도를 불러오지 못했습니다. 인터넷 연결을 확인하세요.', true); }
  }, 0);
  return view;
}

async function screenPick(id) {
  // id === 'unload': 반입지 위치(이 휴대폰에만 저장, 길안내용)
  const isUnload = id === 'unload';
  const stop = isUnload ? null : findStop(id);
  if (!isUnload && !stop) { go('#/home'); return h('main'); }
  const back = isUnload ? '#/unload' : `#/stop/${id}`;
  const d = isUnload ? { loc: st.unloadSite } : deriveStop(stop, st.events);
  let found = null; // 주소 검색으로 받은 좌표
  const box = h('div', { class: 'mapbox pick', id: 'map' }, h('div', { class: 'crosshair', 'aria-hidden': 'true' }));
  const msg = msgBox();
  // 지도 조각을 받지 못해도(인터넷·지도 서버 문제) 휴대폰 위치로 저장할 수 있게: 내 위치를 잡으면 그 좌표를 쓴다
  let gps = null, mapReady = false;
  const gpsNote = h('p', { class: 'small gps-note' });
  const save = btn('이 위치로 저장', async () => {
    let lat, lon, source = 'MANUAL';
    if (liveMap && mapReady) { const c = liveMap.getCenter(); lat = c.lat; lon = c.lng; }
    else if (found) { lat = found.lat; lon = found.lon; }
    else if (gps) { lat = gps.lat; lon = gps.lon; source = 'GPS'; }
    else return;
    lat = Math.round(lat * 1e6) / 1e6; lon = Math.round(lon * 1e6) / 1e6;
    if (found && Math.abs(lat - found.lat) < 2e-6 && Math.abs(lon - found.lon) < 2e-6) source = 'ADDR'; // 주소 검색 좌표 그대로
    if (isUnload) { st.unloadSite = { lat, lon }; await commit(); toast('반입지 위치를 저장했습니다'); go(back); return; }
    if (isSolo()) { stop.lat = lat; stop.lon = lon; stop.geo_source = source; stop.updated_at = new Date().toISOString(); await commit(); }
    else await addEvent(stop, 'LOCATION_FIX', { lat, lon, source, ...(source === 'GPS' && gps.acc != null ? { acc: gps.acc } : {}) }); // PC 방식: 결과 파일로 PC에 전달
    toast('위치를 저장했습니다'); go(back);
  }, 'primary big', { disabled: true });
  const search = addrSearch({
    manual: '지도를 움직이거나 [지금 내 위치로 이동]으로 정해 주세요.', officeNote: false,
    forbid: () => (stop ? [stop.name, stop.phone] : []),
    onPick: (it, ll) => {
      if (!ll) { say(msg, '이 주소의 좌표를 받지 못했습니다. 지도를 움직여 맞춰 주세요.', true); return; }
      found = ll; gps = null; save.disabled = false;
      gpsNote.textContent = `주소 검색: ${it.road} (건물 출입구 좌표). 배출 위치가 다르면 지도를 움직여 맞춘 뒤 저장하세요.`;
      if (liveMap) liveMap.jumpTo({ center: [ll.lon, ll.lat], zoom: 18 });
    } });
  const myLoc = btn('📍 지금 내 위치로 이동', async () => {
    say(msg, '휴대폰 위치를 찾는 중…');
    const fix = await M.myFix({ ask: true, timeout: 15000 });
    if (!view.isConnected) return;
    if (!fix) { say(msg, '위치를 가져오지 못했습니다. 위치 권한과 GPS를 확인해 주세요.', true); return; }
    gps = fix; found = null; say(msg, '');
    gpsNote.textContent = `내 위치: 오차 약 ${fix.acc}m. 서 있는 곳이 배출 위치면 [이 위치로 저장]을 누르세요.`;
    if (liveMap) { liveMap.jumpTo({ center: [fix.lon, fix.lat], zoom: 18 }); mapReady = true; }
    save.disabled = false;
  }, 'big', { 'aria-label': '지금 내 위치로 이동' });
  const tilesFailed = () => {
    if (mapReady || box.querySelector('.map-empty')) return;
    myLoc.classList.add('primary');
    box.append(h('div', { class: 'map-empty' }, h('p', {}, '지도가 안 보입니다 (인터넷 또는 지도 서버 문제).'),
      h('p', { class: 'small' }, '배출 위치에 서서 아래 [지금 내 위치로 이동]을 누르면 지도 없이도 휴대폰 위치로 저장할 수 있습니다.')));
  };
  const view = h('main', { class: 'location-picker' },
    h('header', { class: 'bar' }, btn('← 취소', () => go(back), 'ghost'), h('h1', { class: 'bar-title' }, isUnload ? '반입지 위치 지정' : '배출 위치 지정')),
    h('div', { class: 'location-picker-content' },
      h('p', {}, isUnload ? '반입지(하차하는 곳)' : stop.pii_purged ? stop.receipt_no : `${stop.name} · ${stop.address}`),
      h('p', { class: 'small' }, '지도를 움직여 가운데 표시를 실제 위치에 맞추세요.'),
      box, gpsNote, msg, search, mapNotice()),
    h('footer', { class: 'footbar location-picker-actions' }, myLoc, save));
  setTimeout(async () => {
    if (!view.isConnected) return;
    // 화면 키보드가 열린 휴대폰에서는 레이아웃 높이와 실제로 보이는 높이가 다를 수 있다.
    const viewport = window.visualViewport;
    if (viewport) {
      const fit = () => {
        if (viewport.scale === 1) {
          view.style.setProperty('--picker-height', viewport.height + 'px');
          view.style.setProperty('--picker-top', viewport.offsetTop + 'px');
        } else {
          view.style.removeProperty('--picker-height');
          view.style.removeProperty('--picker-top');
        }
      };
      fit();
      viewport.addEventListener('resize', fit);
      viewport.addEventListener('scroll', fit);
      const observer = new MutationObserver(() => {
        if (view.isConnected) return;
        viewport.removeEventListener('resize', fit);
        viewport.removeEventListener('scroll', fit);
        observer.disconnect();
      });
      observer.observe(view.parentNode, { childList: true });
    }
    try {
      const { map, ml } = await M.createMap(box, { center: d.loc ? [d.loc.lon, d.loc.lat] : M.DEFAULT_CENTER, zoom: d.loc ? 18 : 15 });
      if (!view.isConnected) { map.remove(); return; }
      setLiveMap(map); ui.ml = ml;
      // 지도가 실제로 보일 때만(또는 원래 위치가 있거나 내 위치를 잡았을 때) 지도 가운데로 저장: 빈 화면에서 기본 좌표를 저장하지 않도록
      const ready = () => { mapReady = true; save.disabled = false; const e = box.querySelector('.map-empty'); if (e) e.remove(); myLoc.classList.remove('primary'); };
      const selected = found || gps;
      if (selected) map.jumpTo({ center: [selected.lon, selected.lat], zoom: 18 });
      if (selected || d.loc) ready();
      map.on('data', (e) => { if (e.dataType === 'source' && e.tile) ready(); });
      map.once('idle', () => { if (map.areTilesLoaded && map.areTilesLoaded() && map.loaded()) ready(); });
      map.on('error', () => setTimeout(tilesFailed, 1500));
      setTimeout(tilesFailed, 8000);
    } catch { say(msg, '지도를 불러오지 못했습니다.', true); tilesFailed(); }
  }, 0);
  return view;
}

// ------------------------------------------------------------ 결과 QR 표시 (역방향: 휴대폰 → PC 웹캠)

export {
  mapNotice, mapStops, pinFor, screenMap, screenPick,
};
