// 동별 통합 업무. 전역 접수·배정 API를 재사용하지 않고 서버가 허용한 범위만 다룬다.
import { FEES, api, apiRaw, boot, btn, chip, confirmDlg, fmtPhone, go, h, modal, post, postOnce, reasonFields, routeEpoch, statusChip, toast, wrap } from '../core.js';
import { itemPicker } from '../widgets.js';
import { won } from '/web/fees.js';
import { collectionButton, unchangedCollectionItems } from './collection_entry.js';
import { kstDay, moveDay } from '../date_filters.js';

const READ = new Set(['ADMIN', 'INTAKE', 'LEAD', 'DONG']);
const WRITE = new Set(['ADMIN', 'INTAKE', 'DONG']);
const METRICS = [['scheduled', '예정', 'day'], ['done', '완료', 'done'], ['unassigned', '미배정', 'unassigned'], ['revisit', '재방문', 'revisit'], ['overdue', '지난 미처리', 'overdue'], ['no_date', '예정일 없음', 'no_date']];
const BUCKET = { all: '전체 기간 · 모든 상태', day: '선택일 접수', done: '선택일 완료', unassigned: '선택일 미배정', revisit: '선택일 재방문', overdue: '전체 기간 · 지난 미처리', no_date: '전체 기간 · 예정일 없음' };
let state = null, viewSeq = 0;
const owner = () => `${boot?.me?.id || ''}|${boot?.me?.username || ''}|${boot?.me?.role || ''}`;
const clone = (value) => JSON.parse(JSON.stringify(value));

function resetDongs() {
  state = null; viewSeq++;
  // 로그아웃·계정 전환 때 이전 주민 양식도 함께 닫는다. 초안은 저장소에 남기지 않는다.
  document.querySelectorAll('[data-dong-owner]').forEach((node) => node.replaceChildren());
}

async function screenDongs(arg = null) {
  if (!READ.has(boot?.me?.role)) return h('div', { class: 'dong-workspace' }, h('h1', {}, '동별 관리'), h('p', { class: 'notice', role: 'status' }, '이 계정은 동별 관리 화면을 사용할 수 없습니다.'));
  if (state?.owner !== owner()) resetDongs();
  const s = state ||= { owner: owner(), date: boot.today, dong: 'all', q: '', bucket: 'day', page: 1, crews: new Set(), seen: new Set(), drafts: new Map() };
  let restoreList = !arg && s.detailOpen;
  s.detailOpen = !!arg;
  const mine = ++viewSeq, epoch = routeEpoch, hash = location.hash;
  const canWrite = WRITE.has(boot.me.role), scoped = boot.me.role === 'DONG';
  let view, data = null, loadSeq = 0, routeSeq = 0, detailSeq = 0, plan = null, routeBusy = false;
  const current = () => view?.isConnected && mine === viewSeq && epoch === routeEpoch && hash === location.hash && s === state && s.owner === owner();
  const scopeKey = () => `${s.date}|${s.dong}`;
  const crewIds = () => [...s.crews].filter((id) => (data?.crews || []).some((c) => c.active && c.id === id)).sort();
  const planKey = () => `${scopeKey()}|${crewIds().join(',')}`;
  const msg = h('div', { class: 'dw-message', 'aria-live': 'polite' });
  const body = h('div', { class: 'dw-overview' }), list = h('div', { class: 'dw-list' }), routes = h('div', { class: 'dw-routes' });
  const date = h('input', { type: 'date', value: s.date, 'aria-label': '업무 기준일' });
  const dong = h('select', { 'aria-label': '행정동 범위' });
  const search = h('input', { type: 'search', value: s.q, placeholder: '접수번호 · 이름 · 연락처 · 주소', 'aria-label': '접수 검색어' });
  const teamBox = h('div', { class: 'dw-teams' });
  const enterDetail = (id) => { s.returnFocus = id; s.returnY = scrollY; go('/dongs/' + encodeURIComponent(id)); };
  const create = canWrite ? btn('접수 등록', () => enterDetail('new'), 'primary', { disabled: true }) : null;
  const calc = canWrite ? btn('노선 계산', () => calculate(), '', { disabled: true }) : null;
  const assign = canWrite ? btn('배정 확정', () => commit(), 'primary', { disabled: true }) : null;
  const scopeText = h('p', { class: 'dw-scope' }, scoped ? '지정된 담당 동의 자료만 표시합니다.' : '전체 동과 개별 동을 한 화면에서 관리합니다.');

  function syncActions() {
    if (!canWrite) return;
    create.disabled = !data?.dongs?.some((d) => d.active);
    calc.disabled = !data || !crewIds().length || routeBusy;
    assign.disabled = !plan || plan.key !== planKey() || routeBusy || !plan.res.routes?.some((r) => r.stops?.length);
  }
  function invalidate() {
    routeSeq++; detailSeq++; plan = null; routeBusy = false;
    routes.replaceChildren(h('p', { class: 'dw-idle' }, '선택일과 동 범위를 확인한 뒤 노선을 계산하세요. 계산 후 배정 확정을 눌러야 반영됩니다.'));
    syncActions();
  }
  function showError(error, retry = load) {
    msg.replaceChildren(h('div', { class: 'notice bad', role: 'alert' }, h('strong', {}, error.message || '처리하지 못했습니다.'), ' ',
      error.status === 403 ? '현재 계정의 담당 동 권한을 다시 확인해 주세요. ' : error.status === 409 ? '다른 곳에서 변경되었거나 선택 범위 밖 접수가 함께 배정된 팀입니다. 현재 자료를 다시 확인하세요. ' : '',
      btn('다시 시도', retry, 'small')));
  }
  function chooseOptions() {
    const choices = data?.dongs || boot.dongs || [];
    dong.replaceChildren(h('option', { value: 'all', selected: s.dong === 'all' }, scoped ? '담당 동 전체' : '전체 동'),
      ...choices.map((d) => h('option', { value: d.id, selected: s.dong === d.id }, `${d.name}${d.active ? '' : ' (중지)'}`)),
      ...(scoped ? [] : [h('option', { value: '-', selected: s.dong === '-' }, '동 미지정')]));
    if (![...dong.options].some((o) => o.value === s.dong)) dong.value = 'all';
  }
  function drawTeams() {
    if (!canWrite) return;
    (data.crews || []).filter((c) => c.active).forEach((c) => { if (!s.seen.has(c.id)) { s.seen.add(c.id); s.crews.add(c.id); } });
    teamBox.replaceChildren(h('span', { class: 'dw-label' }, '계산할 팀'), ...(data.crews || []).filter((c) => c.active).map((c) => h('label', {},
      h('input', { type: 'checkbox', checked: s.crews.has(c.id), onchange: (e) => { e.target.checked ? s.crews.add(c.id) : s.crews.delete(c.id); invalidate(); } }), c.name)),
      ...(!(data.crews || []).some((c) => c.active) ? [h('span', { class: 'muted' }, '활성 수거팀이 없습니다. 관리자에게 팀 설정을 요청하세요.')] : []));
  }
  function pickBucket(bucket, id = s.dong) {
    if (id !== s.dong) { s.dong = id; dong.value = id; invalidate(); }
    s.bucket = bucket; s.page = 1; load();
  }
  function drawOverview() {
    const countButton = (n, bucket, id, label) => btn(String(n || 0), () => pickBucket(bucket, id), 'dw-count', { 'aria-label': `${label} ${n || 0}건 보기` });
    const head = h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, '행정동'), ...METRICS.map(([, label], i) => h('th', { scope: 'col' }, label, h('small', {}, i < 4 ? '선택일' : '전체 기간')))));
    const rows = h('tbody', {}, ...(data.rows || []).map((row) => h('tr', { class: row.id === s.dong ? 'sel' : '' },
      h('th', { scope: 'row' }, btn(row.name || '동 미지정', () => pickBucket('day', row.id || '-'), 'dw-dong-link')),
      ...METRICS.map(([key, label, bucket]) => h('td', {}, countButton(row[key], bucket, row.id || '-', `${row.name || '동 미지정'} ${label}`))))));
    body.replaceChildren(h('div', { class: 'dw-metrics' }, ...METRICS.map(([key, label, bucket], i) => btn(h('span', {},
      h('small', {}, i < 4 ? '선택일' : '전체 기간'), h('span', { class: 'dw-metric-label' }, label), h('strong', {}, String(data.summary?.[key] || 0))),
      () => pickBucket(bucket), `dw-metric${s.bucket === bucket ? ' on' : ''}`, { 'aria-pressed': s.bucket === bucket ? 'true' : 'false' }))),
    h('div', { class: 'dw-table-card' }, h('div', { class: 'dw-section-head' }, h('h2', {}, '동별 현황'), h('span', { class: 'muted small' }, '숫자를 누르면 접수 목록으로 이동')),
      h('div', { class: 'tablewrap' }, h('table', { class: 'grid dw-summary' }, head, rows)),
        !(data.rows || []).length ? h('p', { class: 'dw-idle' }, '표시할 담당 동이 없습니다. 관리자에게 담당 동 지정을 요청하세요.') : null));
  }
  function drawList(result) {
    s.page = result.page;
    const rows = (result.rows || []).map((r) => h('tr', {},
      h('td', {}, btn(r.receipt_no, () => enterDetail(r.id), 'dw-receipt', { 'data-request-id': r.id }), statusChip(r.status)),
      h('td', {}, h('span', {}, r.planned_date || '예정일 없음'), h('small', {}, r.dong || '동 미지정')),
      h('td', {}, h('span', {}, r.name), h('small', {}, fmtPhone(r.phone))),
      h('td', {}, h('span', {}, r.address), h('small', {}, (r.items || []).map((i) => `${i.name} ${i.spec || ''} ${i.qty}${i.unit_label || '개'}`).join(', ')))));
    const table = h('table', { class: 'grid dw-requests' }, h('thead', {}, h('tr', {}, ...['접수번호 / 상태', '예정일 / 동', '신청인 / 연락처', '배출 주소 / 품목'].map((v) => h('th', { scope: 'col' }, v)))), h('tbody', {}, ...rows));
    list.replaceChildren(h('div', { class: 'dw-section-head' }, h('div', {}, h('h2', {}, BUCKET[s.bucket] || BUCKET.day), h('p', { class: 'muted small' }, `총 ${Number(result.total || 0).toLocaleString()}건 · ${s.dong === 'all' ? (scoped ? '담당 동 전체' : '전체 동') : dong.selectedOptions[0]?.textContent || ''}`)),
      h('select', { 'aria-label': '접수 목록 구분', onchange: (e) => pickBucket(e.target.value) }, ...Object.entries(BUCKET).map(([key, text]) => h('option', { value: key, selected: key === s.bucket }, text)))),
      h('div', { class: 'tablewrap' }, table),
      ...(!(result.rows || []).length ? [h('p', { class: 'dw-idle' }, '이 조건에 맞는 접수가 없습니다. 날짜·동 범위·검색어를 확인하세요.'),
        ...(s.bucket !== 'all' ? [btn('날짜 제한 없이 다시 찾기', () => pickBucket('all'), 'small')] : [])] : []),
      h('div', { class: 'pager' }, btn('‹ 이전', () => { s.page--; load(); }, 'small', { disabled: result.page <= 1 }),
        h('span', {}, `${result.page} / ${result.pages}쪽`), btn('다음 ›', () => { s.page++; load(); }, 'small', { disabled: result.page >= result.pages })));
  }
  async function load() {
    if (!current()) return;
    const seq = ++loadSeq, key = scopeKey(), qs = new URLSearchParams({ date: s.date, dong: s.dong });
    const listQs = new URLSearchParams({ ...Object.fromEntries(qs), q: s.q, bucket: s.bucket, page: s.page });
    const valid = () => current() && seq === loadSeq && key === scopeKey();
    msg.replaceChildren(); list.setAttribute('aria-busy', 'true');
    list.replaceChildren(h('p', { class: 'dw-idle', role: 'status' }, '접수를 불러오는 중…'));
    try {
      const [summary, requests, assignments] = await Promise.all([api(`dong-work?${qs}`), api(`dong-work/requests?${listQs}`), api(`dong-work/assignments?${qs}`)]);
      if (!valid()) return;
      data = summary; chooseOptions(); drawTeams(); drawOverview(); drawList(requests);
      const shared = (Array.isArray(assignments) ? assignments : assignments.rows || []).filter((b) => b.shared);
      scopeText.textContent = `${scoped ? '내 담당 동 범위' : '전체 업무 권한'} · 집계·배차 기준일 ${s.date} · 목록: ${BUCKET[s.bucket]}${shared.length ? ` · 다른 동과 함께 배정된 팀 ${shared.length}개 (전체 담당자 조정 필요)` : ''}${canWrite ? '' : ' · 조회 전용'}`;
      syncActions();
      if (restoreList) {
        restoreList = false;
        const target = s.returnFocus === 'new' ? create : [...list.querySelectorAll('[data-request-id]')].find((el) => el.dataset.requestId === s.returnFocus);
        (target || search).focus({ preventScroll: true }); scrollTo(0, s.returnY || 0);
      }
    } catch (error) {
      if (!valid()) return;
      data = null; body.replaceChildren(); list.replaceChildren(); invalidate(); showError(error);
    } finally { if (valid()) list.removeAttribute('aria-busy'); }
  }
  async function openDetail(id) {
    const seq = ++detailSeq, key = scopeKey(); msg.replaceChildren();
    try {
      const request = await api(`dong-work/requests/${encodeURIComponent(id)}`);
      if (!current() || seq !== detailSeq || key !== scopeKey()) return;
      await openForm(request);
    } catch (error) { if (current() && seq === detailSeq && key === scopeKey()) showError(error, () => openDetail(id)); }
  }
  async function openForm(request = null) {
    if (!current() || !data || (!request && !canWrite)) return;
    const isNew = !request, key = scopeKey(), identity = s.owner;
    const dongs = data.dongs || [], origin = isNew ? (dongs.find((d) => d.id === s.dong && d.active)?.id || dongs.find((d) => d.active)?.id) : request.dong_id || dongs.find((d) => d.name === request.dong)?.id || '-';
    const draftKey = `${isNew ? 'new' : request.id}|${origin}`;
    const saved = s.drafts.get(draftKey);
    const blank = { name: '', phone: '', address: '', spot_note: '', memo: '', planned_date: s.date, dong_id: origin, channel: '전화', items: [] };
    const base = request || blank, r = saved?.data || base, items = clone(r.items || []);
    let version = saved?.baseVersion ?? request?.req_ver;
    const readonly = !canWrite || request?.status === 'CANCELLED';
    const alive = () => current() && identity === owner() && key === scopeKey() && form.isConnected;
    const F = {};
    const input = (key0, label, attrs = {}) => { F[key0] = h('input', { value: r[key0] || '', 'aria-label': label, disabled: readonly, ...attrs }); return h('label', { class: 'f' }, label, F[key0]); };
    const fields = [input('name', '이름'), input('phone', '연락처', { type: 'tel' }), input('address', '배출 주소'), input('spot_note', '상세 배출 위치'), input('planned_date', '수거 예정일', { type: 'date' })];
    F.dong_id = h('select', { 'aria-label': '접수 행정동', disabled: readonly },
      !isNew && origin === '-' ? h('option', { value: '-', selected: !r.dong_id || r.dong_id === '-' }, '동 미지정 (변경 시 동 선택)') : null,
      ...dongs.filter((d) => d.active || d.id === origin).map((d) => h('option', { value: d.id, selected: d.id === (r.dong_id || origin) }, `${d.name}${d.active ? '' : ' (중지)'}`)));
    F.channel = h('select', { 'aria-label': '접수 경로', disabled: readonly }, ...(boot.channels || ['전화', '방문', '기타']).map((v) => h('option', { value: v, selected: v === (r.channel || '전화') }, v)));
    F.memo = h('textarea', { 'aria-label': '요청 메모', disabled: readonly }, r.memo || '');
    const why = h('input', { 'aria-label': '수정 사유', value: saved?.reason || '', placeholder: '수정 사유 (필수)' });
    const lines = h('div', { class: 'dw-items' }), total = h('strong', { class: 'dw-total' }), notice = h('div', { 'aria-live': 'polite' });
    const locked = new Set(request?.locked_lines || []);
    F.lat = h('input', { type: 'number', step: 'any', min: -90, max: 90, value: r.lat ?? '', disabled: readonly, 'aria-label': '위도' });
    F.lon = h('input', { type: 'number', step: 'any', min: -180, max: 180, value: r.lon ?? '', disabled: readonly, 'aria-label': '경도' });
    let acceptedDuplicate = '', savedResult = null, form, discarded = false, geoSeq = 0, geoSource = r.geo_source || null;
    const geoMessage = h('p', { class: 'muted small', 'aria-live': 'polite' }, r.lat != null && r.lon != null ? '배출 위치가 지정되어 있습니다. 주소나 동을 바꾸면 위치를 다시 확인하세요.' : '위치 없음 · 접수는 가능하지만 노선에 넣으려면 위치를 지정해야 합니다.');
    const payload = () => ({
      // 편집 대상의 일정·위치·우선순위를 사용자가 건드리지 않았으면 그대로 보존한다.
      appt_start: base.appt_start || '', appt_end: base.appt_end || '', must_today: !!base.must_today, urgent: !!base.urgent, priority: base.priority || 0,
      ...Object.fromEntries(Object.entries(F).map(([k, el]) => [k, el.value])), items: items.map(({ _row, ...item }) => item),
      lat: F.lat.value === '' ? null : +F.lat.value, lon: F.lon.value === '' ? null : +F.lon.value, geo_source: geoSource,
    });
    const clearPosition = () => { geoSeq++; F.lat.value = ''; F.lon.value = ''; geoSource = null; geoMessage.textContent = '주소·동이 바뀌어 기존 위치를 지웠습니다. 주소로 다시 찾거나 직접 지정하세요.'; remember(); };
    F.address.addEventListener('input', clearPosition); F.dong_id.addEventListener('change', clearPosition);
    [F.lat, F.lon].forEach((el) => el.addEventListener('input', () => { geoSeq++; geoSource = 'MANUAL'; geoMessage.textContent = F.lat.value && F.lon.value ? '직접 입력한 위치입니다. 위도와 경도를 확인하세요.' : '위도와 경도를 모두 입력하세요. 위치가 없으면 노선을 계산할 수 없습니다.'; }));
    const findPosition = readonly ? null : btn('주소로 위치 찾기', async () => {
      if (!alive()) return;
      const address = F.address.value.trim(), dongId = F.dong_id.value, seq = ++geoSeq;
      if (!address) { geoMessage.textContent = '배출 주소를 먼저 입력하세요.'; F.address.focus(); return; }
      const valid = () => alive() && seq === geoSeq && address === F.address.value.trim() && dongId === F.dong_id.value;
      findPosition.disabled = true; geoMessage.textContent = '주소의 위치를 확인하는 중…';
      try {
        const position = await api(`dong-work/geocode?${new URLSearchParams({ address, dong: dongId })}`);
        if (!valid()) return;
        if (position.none || position.lat == null || position.lon == null) { geoMessage.textContent = position.ambiguous ? '동일한 주소가 여러 곳입니다. 구·도로명·건물번호를 확인하거나 위치를 직접 입력하세요.' : '주소 자료에서 위치를 찾지 못했습니다. 도로명·건물번호를 확인하거나 위치를 직접 입력하세요.'; return; }
        F.lat.value = position.lat; F.lon.value = position.lon; geoSource = 'ADDR';
        geoMessage.textContent = `${position.label || '주소로 찾은 위치'} · 건물 출입구 기준입니다. 실제 배출 위치를 확인하세요.`; remember();
      } catch (error) { if (valid()) geoMessage.textContent = `${error.message} 주소로 위치 찾기를 다시 누르거나 직접 입력하세요.`; }
      finally { if (alive()) findPosition.disabled = false; }
    }, 'small');
    function remember() { if (identity === owner() && state === s) s.drafts.set(draftKey, { data: payload(), reason: why.value, baseVersion: version }); }
    function drawItems() {
      let amount = 0, pending = false;
      lines.replaceChildren(...items.map((item, i) => {
        const rate = FEES().rows.find((f) => f.id === item.fee_rate_id);
        const stored = item.price_status != null || Object.hasOwn(item, 'unit_price');
        const price = stored ? item.unit_price : item.price ?? rate?.price;
        const confirmed = price != null && (stored ? item.price_status === 'CONFIRMED' : !!item.fee_rate_id && rate?.status === 'CONFIRMED');
        if (confirmed) amount += Number(price) * Number(item.qty); else pending = true;
        return h('div', { class: 'dw-item-row' }, h('span', {}, item.name, ' ', item.spec || '', h('small', {}, confirmed ? won(price) : '금액 확인 대기')),
          h('input', { type: 'number', min: 1, value: item.qty, disabled: readonly || locked.has(item.line_id), 'aria-label': `${item.name} 수량`, onchange: (e) => { item.qty = Math.max(1, +e.target.value || 1); remember(); drawItems(); } }),
          h('span', {}, item.unit_label || '개'), !readonly ? btn('삭제', () => { items.splice(i, 1); remember(); drawItems(); }, 'small', { disabled: locked.has(item.line_id), 'aria-label': `${item.name} 삭제` }) : null);
      }));
      total.textContent = `${won(amount)}${pending ? ' + 금액 확인 대기' : ''}`;
      if (!items.length) lines.append(h('p', { class: 'muted' }, '수거할 품목을 추가하세요.'));
    }
    const picker = readonly ? null : itemPicker((item) => {
      if (!alive()) return;
      const row = item._row;
      items.push(row ? { fee_rate_id: row.id, name: row.name, spec: row.spec, unit: row.unit, unit_label: row.unit_label, qty: item.qty } : item);
      remember(); drawItems();
    }, { usagePath: scoped ? null : 'fee-usage' });
    if (picker) {
      const query = picker.querySelector('input[type=search]');
      query.setAttribute('aria-label', '품목명·규격 검색');
    }
    form = h('div', { class: 'dong-workspace dw-form', 'data-dong-owner': identity, oninput: remember, onchange: remember },
      h('p', { class: 'dw-form-context' }, `${isNew ? '새 접수' : request.receipt_no} · 원래 동: ${dongs.find((d) => d.id === origin)?.name || '미지정'}`, request ? statusChip(request.status) : null),
      !isNew && !readonly && !request.pii_purged_at ? collectionButton(request, { path: 'dong-work/requests', current: alive,
        before: () => { if (version !== request.req_ver) throw new Error('접수 수정 초안을 먼저 확인한 뒤 수거 처리하세요.'); unchangedCollectionItems(items, request.items); },
        after: async (result, authOwner) => {
          if (!alive()) return;
          const fresh = await api(`dong-work/requests/${request.id}`, { _prefsOwner: authOwner });
          if (!alive()) return;
          if (fresh.req_ver !== result.req_ver) { notice.textContent = '접수가 다시 바뀌었습니다. 최신 내용을 확인하세요.'; return; }
          version = fresh.req_ver; Object.assign(request, fresh); locked.clear(); fresh.locked_lines.forEach((line) => locked.add(line)); drawItems(); remember();
          form.querySelector('.dw-form-context').replaceChildren(`${request.receipt_no} · 원래 동: ${dongs.find((d) => d.id === origin)?.name || '미지정'}`, statusChip(fresh.status));
        } }) : null,
      saved ? h('p', { class: 'notice small' }, '보관한 초안을 복구했습니다. 저장 전 현재 예정일과 행정동을 확인하세요.') : null,
      h('div', { class: 'dw-form-grid' }, ...fields, h('label', { class: 'f' }, '행정동', F.dong_id), h('label', { class: 'f' }, '접수 경로', F.channel)),
      h('div', { class: 'dw-position' }, findPosition, geoMessage, h('details', {}, h('summary', {}, '위치 직접 입력'), h('div', { class: 'dw-form-grid' }, h('label', { class: 'f' }, '위도', F.lat), h('label', { class: 'f' }, '경도', F.lon)))),
      h('label', { class: 'f' }, '요청 메모', F.memo), h('div', { class: 'dw-section-head' }, h('h3', {}, '수거 품목'), total), lines, picker,
      !isNew && !readonly ? h('label', { class: 'f' }, '수정 사유', why) : null, notice,
      !isNew && !readonly ? btn('접수 취소', async () => {
        const codes = boot.cancel_codes || { RESIDENT: '주민 요청', OTHER: '기타' }, reasons = reasonFields(codes, '취소 사유');
        const cancelled = await modal({ title: '접수 취소', ok: '취소 확정', danger: true, body: [h('p', {}, '기록을 남겨 접수를 취소합니다. 배정된 접수는 수거팀에 알려 주세요.'), ...reasons.nodes],
          onOk: async () => { if (!alive()) throw new Error('화면이나 담당 계정이 바뀌었습니다. 다시 열어 주세요.'); const value = reasons.get(); const result = await post(`dong-work/requests/${request.id}/cancel`, { code: value.code, reason: value.text, req_ver: version }); return result || true; } });
        if (cancelled && alive()) { s.drafts.delete(draftKey); go('/dongs'); toast('접수를 취소했습니다'); }
      }, 'danger small') : null);
    drawItems();
    const saveForm = async () => {
        if (!alive()) throw new Error('화면이나 담당 계정이 바뀌었습니다. 목록에서 다시 열어 주세요.');
        if (readonly) return true;
        const value = payload(); remember(); const submittedDraft = s.drafts.get(draftKey); notice.replaceChildren();
        if (!value.dong_id || value.dong_id === '-' && isNew) throw new Error('담당 행정동을 선택하세요.');
        if (!value.name.trim() || !value.phone.trim() || !value.address.trim()) throw new Error('이름·연락처·배출 주소를 입력하세요.');
        if (!items.length) throw new Error('품목을 하나 이상 추가하세요.');
        if ((value.lat == null) !== (value.lon == null) || value.lat != null && (!Number.isFinite(value.lat) || Math.abs(value.lat) > 90 || !Number.isFinite(value.lon) || Math.abs(value.lon) > 180)) throw new Error('위도와 경도를 올바르게 모두 입력하거나 두 칸 모두 비워 주세요.');
        if (!isNew && !why.value.trim()) throw new Error('수정 사유를 입력하세요.');
        if (isNew) {
          const sig = `${value.dong_id}|${value.phone.replace(/\D/g, '')}|${value.address.trim()}`;
          if (acceptedDuplicate !== sig) {
            const duplicates = await api(`dong-work/duplicates?${new URLSearchParams({ phone: value.phone, address: value.address, exclude: '', dong: value.dong_id })}`);
            if (!alive()) throw new Error('화면이 바뀌었습니다. 다시 열어 주세요.');
            if (JSON.stringify(value) !== JSON.stringify(payload())) throw new Error('확인 중 입력이 바뀌었습니다. 내용을 확인하고 다시 저장하세요.');
            const rows = Array.isArray(duplicates) ? duplicates : duplicates.rows || [];
            if (rows.length) { acceptedDuplicate = sig; notice.append(h('p', { class: 'notice warn', role: 'alert' }, `담당 범위에 비슷한 접수 ${rows.length}건이 있습니다. 별개 접수가 맞으면 접수 저장을 다시 누르세요.`)); throw new Error('중복 가능성을 확인해 주세요.'); }
          }
        }
        try {
          savedResult = isNew ? await postOnce(`dong-intake:${identity}:${origin}`, 'dong-work/requests', value)
            : await post(`dong-work/requests/${request.id}`, { ...value, req_ver: version, reason: why.value });
        } catch (error) {
          if (!alive()) throw new Error('현재 화면이 바뀌었습니다. 저장 여부는 원래 동의 목록에서 확인하세요.');
          if (error.status === 409) notice.append(h('div', { class: 'notice bad', role: 'alert' }, '다른 곳에서 접수가 바뀌었습니다. 입력한 초안을 보관했습니다. 현재 접수를 다시 확인하세요. ',
            btn('최신 접수 확인', async () => { try { const fresh = await api(`dong-work/requests/${request.id}`); if (alive()) notice.replaceChildren(h('p', { class: 'notice' }, `현재 버전 ${fresh.req_ver} · 예정일 ${fresh.planned_date || '없음'} · ${fresh.dong || '미지정'}. 초안을 닫고 최신 내용으로 다시 시작하려면 초안 버리기를 누르세요.`)); } catch (e) { if (alive()) notice.append(h('p', { class: 'notice bad' }, e.message)); } }, 'small'),
            btn('초안 버리고 다시 열기', () => { discarded = true; s.drafts.delete(draftKey); openDetail(request.id); }, 'small')));
          throw error;
        }
        if (identity === owner() && state === s && s.drafts.get(draftKey) === submittedDraft) s.drafts.delete(draftKey);
        return savedResult || true;
    };
    const back = btn(readonly ? '목록으로' : '목록으로 · 초안 보관', () => { if (!readonly && !discarded) remember(); go('/dongs'); }, 'small');
    const error = h('p', { role: 'alert' });
    const save = readonly ? null : btn(isNew ? '접수 저장' : '수정 저장', wrap(async () => {
      if (form.querySelector('.collection-entry [aria-busy="true"], .collection-entry input:disabled')) throw new Error('수거 결과 저장 여부를 먼저 확인하세요.');
      const result = await saveForm();
      if (result && current()) { toast(isNew ? `${savedResult.receipt_no || ''} 접수를 등록했습니다` : '접수를 수정했습니다'); (savedResult.warnings || []).forEach((w) => toast(w, true)); go('/dongs'); }
    }, error), 'primary', { 'data-save': '1' });
    const title = isNew ? '동별 접수 등록' : '동별 접수 상세';
    const heading = h('h1', { tabindex: -1 }, title);
    form.setAttribute('role', 'region'); form.setAttribute('aria-label', title);
    form.append(h('div', { class: 'dw-detail-actions' }, save, error));
    view.replaceChildren(h('div', { class: 'dw-heading' }, h('div', {}, h('span', { class: 'dw-eyebrow' }, '동별 통합 업무'), heading,
      h('p', { class: 'muted small' }, '접수 내용과 수거 결과를 이 화면에서 확인하세요.')), back), msg, form);
    heading.focus({ preventScroll: true }); scrollTo(0, 0);
  }
  async function calculate() {
    if (!current() || !canWrite || routeBusy || !data || !crewIds().length) return;
    const seq = ++routeSeq, key = planKey(), value = { date: s.date, dong: s.dong, crew_ids: crewIds() };
    const valid = () => current() && seq === routeSeq && key === planKey();
    plan = null; routeBusy = true; syncActions(); msg.replaceChildren();
    routes.replaceChildren(h('p', { class: 'dw-idle', role: 'status' }, '선택 범위의 노선을 계산하는 중…'));
    try {
      const res = await post('dong-work/route', value);
      if (!valid()) return;
      plan = { res, key, date: value.date, dong: value.dong };
      routes.replaceChildren(h('div', { class: 'dw-section-head' }, h('h2', {}, '노선 미리보기'), chip('확정 전', 'warn')),
        h('p', { class: 'muted small' }, `${value.date} · ${dong.selectedOptions[0]?.textContent || ''} · 선택 범위의 예정일 접수만 배정합니다.`),
        ...(res.routes || []).map((r) => h('div', { class: 'dw-route' }, h('h3', {}, `${r.crew_name || r.crew_id} · ${(r.stops || []).length}곳`),
          r.depot ? h('p', { class: 'muted small' }, `출발: ${r.depot.label || '차고지'} · 종료: ${res.end_site?.label || r.depot.label || '차고지'}${r.end ? ` · 예상 ${r.end}` : ''}`) : null,
          h('ol', {}, ...(r.stops || []).map((stop) => { const q = res.requests?.[stop.request_id] || {}; return h('li', {}, h('time', {}, stop.eta || '—'), h('span', {}, q.receipt_no || '접수', h('small', {}, `${q.dong || ''} ${q.address || ''}`))); })))),
        ...((res.unassigned || []).length ? [h('p', { class: 'notice warn' }, `계산에 넣지 못한 접수 ${res.unassigned.length}건이 있습니다. 위치·담당팀·용량·예정일을 확인하세요.`),
          ...res.unassigned.map((u) => h('p', { class: 'muted small' }, `${res.requests?.[u.request_id]?.receipt_no || '접수'}: ${u.reason}`))] : []),
        ...(!(res.routes || []).some((r) => r.stops?.length) ? [h('p', { class: 'dw-idle' }, '배정할 접수가 없습니다. 예정일과 위치가 지정된 접수를 확인하세요.')] : []));
    } catch (error) { if (valid()) { plan = null; routes.replaceChildren(); showError(error, calculate); } }
    finally { if (valid()) { routeBusy = false; syncActions(); } }
  }
  async function commit() {
    const candidate = plan;
    if (!current() || !canWrite || !candidate || candidate.key !== planKey() || routeBusy) return;
    const valid = () => current() && plan === candidate && candidate.key === planKey();
    await modal({ title: '동별 배정 확정', ok: '이 범위로 배정 확정', body: [h('p', {}, `${candidate.date} · ${dong.selectedOptions[0]?.textContent || ''}의 계산 결과를 확정합니다.`),
      h('p', { class: 'muted small' }, '다른 동과 공유된 기존 배정은 교체하지 않습니다. 이미 전송한 팀은 전체 업무 담당자에게 새 목록 전송을 요청하세요.')],
      onOk: async () => {
        if (!valid()) throw new Error('날짜·동·팀 또는 화면이 바뀌었습니다. 노선을 다시 계산하세요.');
        routeBusy = true; syncActions();
        const assignedPlan = Object.fromEntries((candidate.res.routes || []).map((r) => [r.crew_id, (r.stops || []).map((stop) => ({ request_id: stop.request_id, eta: stop.eta || '', unload_after: stop.unload_after || null }))]));
        try {
          const result = await post('dong-work/assign', { date: candidate.date, dong: candidate.dong, plan: assignedPlan, basis: candidate.res.basis });
          if (valid()) { invalidate(); load(); toast('선택 범위의 배정을 확정했습니다'); }
          return result || true;
        } catch (error) {
          if (valid()) { invalidate(); showError(error, calculate); }
          throw error;
        } finally { if (current()) { routeBusy = false; syncActions(); } }
      } });
  }
  async function exportSummary() {
    if (!current() || !data) return;
    const key = scopeKey();
    try {
      const response = await apiRaw(`dong-work/summary.csv?${new URLSearchParams({ date: s.date, dong: s.dong })}`);
      if (!response.ok) throw new Error((await response.json()).error || '집계를 내려받지 못했습니다.');
      const file = await response.blob(); if (!current() || key !== scopeKey()) return;
      const url = URL.createObjectURL(file), link = h('a', { href: url, download: `동별현황-${s.date}.csv` });
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (error) { if (current() && key === scopeKey()) showError(error, exportSummary); }
  }
  const selectDay = (day) => { date.value = day; s.date = day; s.bucket = 'day'; s.page = 1; data = null; invalidate(); load(); };
  date.addEventListener('change', () => selectDay(date.value));
  dong.addEventListener('change', () => { s.dong = dong.value; s.page = 1; data = null; invalidate(); load(); });
  search.addEventListener('input', () => { s.q = search.value; });
  search.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); s.page = 1; load(); } });
  chooseOptions();
  if (arg) {
    view = h('section', { class: 'dong-workspace dw-detail', 'data-dong-owner': s.owner },
      h('h1', {}, arg === 'new' ? '동별 접수 등록' : '동별 접수 상세'), btn('목록으로', () => go('/dongs'), 'small'),
      h('p', { role: 'status' }, '접수를 불러오는 중…'), msg);
    const loadDetail = async () => {
      try {
        const summary = await api(`dong-work?${new URLSearchParams({ date: s.date, dong: s.dong })}`);
        if (!current()) return;
        data = summary;
        if (arg === 'new' && !canWrite) { view.replaceChildren(h('h1', {}, '동별 접수 등록'), h('p', { role: 'alert' }, '이 계정은 접수를 등록할 수 없습니다.'), btn('목록으로', () => go('/dongs'))); return; }
        if (arg === 'new') await openForm(); else await openDetail(arg);
      } catch (error) { if (current()) showError(error, loadDetail); }
    };
    setTimeout(loadDetail, 0);
    return view;
  }
  view = h('section', { class: 'dong-workspace' },
    h('div', { class: 'dw-heading' }, h('div', {}, h('span', { class: 'dw-eyebrow' }, '동별 통합 업무'), h('h1', {}, '동별 관리'), scopeText),
      h('div', { class: 'dw-actions' }, btn('빠른 수수료 계산', () => go('#/calculator')), create, calc, assign)),
    h('div', { class: 'dw-filters', role: 'search', 'aria-label': '동별 조회조건' }, h('label', { class: 'f' }, '집계·배차 기준일', date), h('label', { class: 'f' }, '행정동 범위', dong),
      h('label', { class: 'f dw-search' }, '접수 찾기', search), btn('조회', () => { s.page = 1; load(); }), btn('집계 CSV', exportSummary, 'small')),
    h('div', { class: 'row', role: 'group', 'aria-label': '날짜 빠른 조회' }, btn('이전날', () => selectDay(moveDay(s.date, -1)), 'small'),
      btn('오늘', () => selectDay(kstDay()), 'small'), btn('다음날', () => selectDay(moveDay(s.date, 1)), 'small'),
      btn('전체 기간 조회', () => pickBucket('all'), 'small'), h('span', { class: 'muted small' }, '전체 기간 조회는 완료·취소도 포함합니다. 집계·노선은 기준일을 따릅니다.')),
    teamBox, msg, body, list, routes);
  invalidate(); setTimeout(load, 0);
  return view;
}

export { resetDongs, screenDongs };
