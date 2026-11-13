// VWorld 조회 결과는 이 상자에서만 사용한다. 접수 필드·초안·브라우저 저장소에 넣지 않는다.
import * as M from '/web/map.js';
import { api, boot, btn, h, modal } from '../core.js';

export async function vworldDialog({ address = '', name = '' } = {}) {
  const owner = boot?.me?.username;
  const available = !!(boot?.settings?.external?.vworld?.enabled && boot?.settings?.external?.keys?.vworld_api_key?.set);
  const query = h('input', { value: address, maxlength: 250, class: 'fill', 'aria-label': 'VWorld 조회 주소', autocomplete: 'off' });
  const kind = h('select', { 'aria-label': 'VWorld 주소 유형' }, h('option', { value: 'ROAD' }, '도로명주소'), h('option', { value: 'PARCEL' }, '지번주소'));
  const result = h('div', { 'aria-live': 'polite', 'data-vworld-result': '' });
  const status = h('p', { class: 'muted small', role: 'status' });
  let closed = false, busy = false, sequence = 0, active = null, preview = null;
  const clearMap = () => { if (preview) { preview.remove(); preview = null; } };
  const lookup = btn('위치 조회', async () => {
    if (busy || closed || !available) return;
    const token = ++sequence;
    busy = true; query.disabled = true; kind.disabled = true; lookup.disabled = true;
    lookup.setAttribute('aria-busy', 'true'); clearMap(); result.replaceChildren(); status.textContent = '위치를 조회하고 있습니다.';
    active = new AbortController();
    try {
      const value = await api('vworld/locate', { method: 'POST', body: { address: query.value, type: kind.value, name }, signal: active.signal });
      if (closed || !result.isConnected || token !== sequence || boot?.me?.username !== owner) return;
      if (value.none) { status.textContent = value.message; return; }
      status.textContent = '참고 위치입니다. 조회 결과는 접수·초안·QR에 반영하지 않습니다.';
      const mapBox = h('div', { class: 'map', style: 'height:260px', 'aria-label': 'VWorld 참고 위치 지도' });
      result.replaceChildren(h('p', {}, h('b', {}, '브이월드(국토교통부)')), h('p', {}, value.label),
        h('p', { class: 'code' }, `위도 ${value.lat} · 경도 ${value.lon}`), mapBox);
      try {
        const view = await M.createMap(mapBox, { center: [value.lon, value.lat], zoom: 16 });
        if (closed || !mapBox.isConnected || token !== sequence) { view.map.remove(); return; }
        preview = view.map;
        new view.ml.Marker().setLngLat([value.lon, value.lat]).addTo(preview);
      } catch { if (!closed && mapBox.isConnected) mapBox.replaceChildren(h('p', { class: 'muted' }, '지도를 불러오지 못했습니다. 위 좌표로 위치를 참고하세요.')); }
    } catch (error) {
      if (!closed && result.isConnected && token === sequence) status.textContent = error.message || '위치 조회에 실패했습니다. 다시 시도하세요.';
    } finally {
      busy = false; active = null; query.disabled = false; kind.disabled = false;
      lookup.disabled = !available; lookup.removeAttribute('aria-busy');
    }
  }, 'small primary', { disabled: !available });
  const changed = () => { sequence++; clearMap(); result.replaceChildren(); status.textContent = ''; };
  query.addEventListener('input', changed); kind.addEventListener('change', changed);
  try {
    await modal({ title: 'VWorld 위치 참고', ok: '닫기', cancel: '취소', wide: true, enterSubmit: false, body: [
      h('p', { class: 'notice info' }, '건물 주소를 VWorld에 보내 위치를 참고합니다. 이름·연락처·상세 동호수·메모는 보내지 않습니다. 결과는 이 창에서만 표시합니다.'),
      available ? null : h('p', { class: 'notice warn' }, '관리자가 VWorld API 키를 등록하고 참고 조회를 켜면 사용할 수 있습니다.'),
      h('label', { class: 'f' }, '조회할 건물 주소', query), h('label', { class: 'f' }, '주소 유형', kind),
      h('div', { class: 'row' }, lookup), status, result,
    ].filter(Boolean) });
  } finally {
    closed = true; sequence++; active?.abort(); clearMap(); result.replaceChildren(); query.value = '';
  }
}
