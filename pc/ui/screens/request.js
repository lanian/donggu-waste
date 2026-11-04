// PC 화면: 접수 등록·수정·상세(감면·민원·수납 카드)
import * as M from '/web/map.js';
import { searchFees } from '/web/fees.js';
import { won } from '/web/fees.js';
import { CONTACT, FEES, IDLE_RESET_MS, NAV_APP, NOTICE, NO_PHOTO, PAY, REASON, REFUND_CAUSE, REFUND_ST, SMS_T, STATUS, STICKER, api, boot, btn, canWork, chip, confirmDlg, copyNotice, dongOpts, fmtPhone, go, grid, h, isPlaceholderContact, isStale, kst, modal, pagehead, post, postOnce, promptDlg, reasonDlg, render, reqDone, statusChip, toast, wrap } from '../core.js';
import { itemPicker, mapIn } from '../widgets.js';
import { screenRefunds } from './refunds.js';

// 새 접수 작성 중 내용(이 화면 메모리에만, 브라우저 저장소에 남기지 않음): 다른 화면에 다녀오거나 다시 로그인해도 이어서 쓴다
let intakeDraft = null;
// 시범 운영 비교용: 새 접수 양식을 연 때부터 저장까지 걸린 시간(초). 자리 비움 같은 이상값은 서버가 거른다.
let intakeT0 = null, intakeSeen = 0;
let dupAckKey = null;  // 중복 경고를 확인한 새 접수의 "연락처|주소"
let justSaved = null;  // 방금 등록한 접수 id: 상세 위에 [+ 다음 접수]·[주민 안내 문구 복사]
const staleMine = new Map();  // 409(다른 곳에서 바뀜) 뒤 [다시 열기]: 접수 id → 내가 적었던 값(다시 넣기용)
const FORM_LABEL = { name: '이름', phone: '연락처', address: '배출 주소', spot_note: '상세 배출 위치', memo: '요청 메모', planned_date: '수거 예정일', appt_start: '방문 약속 시작', appt_end: '방문 약속 끝', dong: '행정동', channel: '접수 경로' };
const digitsOnly = (v) => String(v || '').replace(/\D/g, '');
async function screenRequest(id) {
  const isNew = id === 'new';
  if (isNew) { if (!intakeT0 || Date.now() - intakeSeen > IDLE_RESET_MS) intakeT0 = Date.now(); intakeSeen = Date.now(); }
  const blank = { name: '', phone: '', address: '', spot_note: '', memo: '', planned_date: '', appt_start: '', appt_end: '', must_today: 0, urgent: 0, priority: 0, lat: null, lon: null,
    items: [], versions: [], locked_lines: [], status: 'RECEIVED', dong: null, channel: '전화', contacts: [] };
  const fromDraft = isNew && intakeDraft;
  if (isNew && !fromDraft) dupAckKey = null;  // 중복 경고 확인은 같은 작성 중 접수에만 유효 (새로 쓰기·다른 접수에는 다시 확인)
  const r = isNew ? { ...blank, ...(intakeDraft ? intakeDraft.r : {}), items: intakeDraft ? intakeDraft.items : [] } : await api('requests/' + id);
  const baseVer = r.req_ver;  // 이 화면이 연 접수 버전: 수정·취소·감면에 함께 보내고, 다르면 서버가 409
  const showDone = !isNew && justSaved === id; if (showDone) justSaved = null;
  const items = r.items.map((i) => ({ ...i }));
  const locked = new Set(r.locked_lines || []);
  const inp = (k, attrs = {}) => h('input', { value: r[k] ?? '', ...attrs });
  const F = { name: inp('name'), phone: inp('phone', { inputmode: 'tel', placeholder: '010-0000-0000', value: fmtPhone(r.phone) }), address: inp('address'), spot_note: inp('spot_note', { placeholder: '예: 101동 분리수거장 옆' }),
    memo: h('textarea', {}, r.memo || ''), planned_date: inp('planned_date', { type: 'date' }), appt_start: inp('appt_start', { type: 'time' }), appt_end: inp('appt_end', { type: 'time' }),
    must_today: h('input', { type: 'checkbox', checked: r.must_today }), urgent: h('input', { type: 'checkbox', checked: r.urgent }),
    priority: h('select', {}, ...[[0, '보통'], [1, '높음'], [2, '매우 높음']].map(([v, t]) => h('option', { value: v, selected: +r.priority === v }, t))),
    lat: inp('lat', { readonly: true, style: 'width:110px' }), lon: inp('lon', { readonly: true, style: 'width:120px' }),
    channel: h('select', {}, ...boot.channels.map((c) => h('option', { value: c, selected: c === (r.channel || '전화') }, c))),
    dong: h('select', {}, ...dongOpts(r.dong || '')) };
  // 행정동: 주소로 자동 추정(고친 뒤에는 그대로 둠), 못 찾으면 경고
  let dongTouched = !isNew && !!r.dong;
  const dongMsg = h('span', { class: 'muted small', 'aria-live': 'polite' });
  F.dong.addEventListener('change', () => { dongTouched = true; dongMsg.textContent = ''; loadCal(); });
  // 배출 주소 확인(형식·관할·전화번호 섞임 — 막지 않고 알림)
  const addrWarn = h('div', { class: 'addr-warn', 'aria-live': 'polite' });
  const detect = wrap(async () => {
    if (!F.address.value.trim()) { addrWarn.replaceChildren(); return; }
    const d = await api('dong-detect?address=' + encodeURIComponent(F.address.value));
    addrWarn.replaceChildren(...(d.warnings || []).map((w) => h('div', { class: 'notice warn small' }, w)));
    if (dongTouched || !boot.dongs.some((x) => x.active)) return;
    if (d.dong) { F.dong.value = d.dong; dongMsg.textContent = d.source === 'ADDR' ? '주소 자료의 행정동으로 정했습니다.' : '주소 낱말로 추정했습니다. 맞는지 확인하세요.'; dongMsg.className = 'muted small'; }
    else { F.dong.value = ''; dongMsg.textContent = d.ambiguous ? `여러 동에 해당(${d.candidates.join(', ')}) — 골라 주세요.` : '주소로 동을 찾지 못했습니다. 골라 주세요.'; dongMsg.className = 'err small'; }
    loadCal();
  });
  F.address.addEventListener('change', detect);
  // 예약 달력: 다음 업무일 14일의 예약/한도 (선택한 동의 수거 요일·한도 반영), 가장 빠른 가능일, 휴무·한도 경고(막지 않음)
  const calBox = h('div', { class: 'cal', role: 'group', 'aria-label': '예약 현황' });
  const calWarn = h('div', { 'aria-live': 'polite' });
  let earliest = null;
  const loadCal = wrap(async () => {
    const qs = new URLSearchParams({ start: boot.today, days: 14, dong: F.dong.value, date: F.planned_date.value, exclude: isNew ? '' : id });
    const c = await api('calendar?' + qs);
    earliest = c.earliest;
    calBox.replaceChildren(...c.days.map((d) => {
      const lim = d.dong_limit ?? d.limit, n = d.dong_limit != null ? d.dong_booked : d.booked;
      return h('button', { type: 'button', class: 'calday' + (d.over.length ? ' full' : '') + (d.date === F.planned_date.value ? ' on' : ''), title: d.over.join(', ') || '여유 있음',
        onclick: () => { F.planned_date.value = d.date; loadCal(); saveDraft(); } }, h('b', {}, `${d.date.slice(5).replace('-', '.')}(${d.weekday})`), h('span', {}, lim ? `${n}/${lim}` : `${n}건`));
    }));
    calWarn.replaceChildren(...(c.warnings || []).map((w) => h('div', { class: 'notice warn small' }, w)));
  });
  F.planned_date.addEventListener('change', loadCal);
  F.phone.addEventListener('blur', () => { const d = F.phone.value.replace(/\D/g, ''); if (d.length === 11) F.phone.value = fmtPhone(d); });
  const lab = (t, el) => h('label', { class: 'f' }, t, el);
  const itemsBox = h('div', { class: 'items' });
  let saveDraft = () => {};
  const total = h('p', { class: 'muted' });
  const barTotal = h('span', { class: 'bar-total' });
  const drawItems = () => {
    itemsBox.replaceChildren(...items.map((it, i) => h('div', { class: 'it' },
      h('span', { class: 'nm' }, `${it.name} ${it.spec || ''}`, it.price_status === 'PENDING' || (it._row && it._row.status === 'REVIEW') || (!it.fee_rate_id) ? chip('금액 확인 대기', 'warn') : null),
      h('span', { class: 'muted' }, it.unit_price != null ? `${won(it.unit_price)} / ${it.unit_label}` : it.unit_label),
      locked.has(it.line_id) ? chip('현장 기록 있음 — 변경 불가') : h('input', { type: 'number', min: 1, value: it.qty, style: 'width:70px', onchange: (e) => { it.qty = Math.max(1, +e.target.value || 1); drawItems(); } }),
      locked.has(it.line_id) ? h('span', {}, it.qty) : btn('삭제', () => { items.splice(i, 1); drawItems(); }, 'small'))));
    const conf = items.filter((x) => x.fee_rate_id && (x._row ? x._row.status === 'CONFIRMED' : x.price_status === 'CONFIRMED'));
    const sum = conf.reduce((a, x) => a + (x._row ? x._row.price : x.unit_price) * x.qty, 0);
    const pend = items.length - conf.length;
    total.textContent = items.length ? `접수 금액(확인분): ${won(sum)}${pend ? ` · 금액 확인 대기 ${pend}건 → 최종 금액 미확정` : ''}` : '품목을 추가하세요.';
    if (typeof barTotal !== 'undefined' && barTotal) barTotal.textContent = items.length ? `${items.length}개 품목 · ${won(sum)}${pend ? ` + 확인 대기 ${pend}건` : ''}` : '품목 없음';
    // 접수 시 안내: 품목 이름에 설정한 낱말이 있으면 안내문 (예: 폐가전 무상수거)
    const names = items.map((x) => `${x.name} ${x.spec || ''}`).join(' ');
    const hits = (boot.settings.intake_notices || []).filter((n) => n.keywords.some((k) => names.includes(k)));
    noticeBox.replaceChildren(...hits.map((n) => h('div', { class: 'notice info' }, h('strong', {}, '안내 '), n.text)));
    saveDraft();
  };
  const noticeBox = h('div', { 'aria-live': 'polite' });
  drawItems();
  const picker = itemPicker((it) => { if (it._row) Object.assign(it, { name: it._row.name, spec: it._row.spec, unit_label: it._row.unit_label, unit_price: it._row.price }); items.push(it); drawItems(); });

  // 중복 후보
  const dupBox = h('div');
  const checkDup = wrap(async () => {
    if (!F.phone.value && !F.address.value) return;
    const d = await api(`duplicates?phone=${encodeURIComponent(F.phone.value)}&address=${encodeURIComponent(F.address.value)}&exclude=${isNew ? '' : id}`);
    dupBox.replaceChildren(...(d.length ? [h('div', { class: 'notice' }, h('strong', {}, `비슷한 미완료 접수 ${d.length}건 — 같은 건이면 기존 접수를 수정하세요 (자동으로 합치지 않습니다)`),
      h('ul', {}, ...d.map((x) => h('li', {}, h('a', { href: `#/request/${x.id}` }, x.receipt_no), ` ${x.name} · ${fmtPhone(x.phone)} · ${x.address} · ${x.planned_date || '-'} · ${STATUS[x.status]}`))))] : []));
  });
  F.phone.addEventListener('change', checkDup); F.address.addEventListener('change', checkDup);

  // 위치: 이 PC의 주소 자료로 찾거나 지도를 눌러 지정 (주소를 외부로 보내지 않음)
  const mapBox = h('div', { class: 'mapbox' });
  let geoSource = r.geo_source || null, setPin = null;
  // 주소 찾기: 도로명·지번·건물명(아파트)·초성 → 고르면 표준 주소·좌표·동을 채움
  const addrQ = h('input', { type: 'search', placeholder: '도로명·지번·건물명·아파트 (초성 가능, 예: ㄱㅅㅇㅍㅌ)', autocomplete: 'off', 'aria-label': '주소 찾기', role: 'combobox', 'aria-expanded': 'false', 'aria-controls': 'addr-sug' });
  const addrSug = h('div', { class: 'results addr-sug', id: 'addr-sug', role: 'listbox', hidden: true });
  let addrT = null;
  // 행안부 결과(kind '행안부')는 PC 서버가 대신 물어 온 것: 고르면 서버가 좌표 API로 출입구 좌표를 받아 온다(좌표 승인키가 있을 때)
  let jusoSeq = 0;
  const pickAddr = wrap(async (x) => {
    const ext = x.kind === '행안부';
    let lat = x.lat, lon = x.lon, note = '';
    if (ext) {
      const c = await api('juso-coord?' + new URLSearchParams(x.ref));
      if (c.none) note = c.reason || '좌표를 받지 못했습니다. 지도에서 위치를 지정하세요.';
      else { lat = c.lat; lon = c.lon; }
    }
    F.address.value = x.address;
    if (lat != null) { F.lat.value = lat; F.lon.value = lon; geoSource = 'ADDR'; }
    if (x.dong && [...F.dong.options].some((o) => o.value === x.dong)) { F.dong.value = x.dong; dongTouched = true; dongMsg.textContent = ext ? '행안부 주소의 행정동으로 정했습니다.' : '주소 자료의 행정동으로 정했습니다.'; dongMsg.className = 'muted small'; }
    geoMsg.textContent = lat != null ? `${x.kind} 찾기: ${x.address} — 건물 출입구 기준이니 배출 위치를 지도에서 확인하세요.` : `${x.kind} 찾기: ${x.address} — ${note}`;
    if (setPin && lat != null) setPin(lat, lon, true);
    addrSug.hidden = true; addrSug.replaceChildren(); jusoSeq++; addrQ.setAttribute('aria-expanded', 'false'); addrQ.value = '';
    checkDup(); loadCal(); saveDraft(); F.spot_note.focus();
  });
  // 도로 이름만 맞은 결과(hint)는 고르면 검색어를 '도로명 '으로 채워 번호를 이어 적게 한다
  const sugBtn = (x) => h('button', { type: 'button', role: 'option', onclick: () => {
    if (x.hint) { addrQ.value = x.road + ' '; addrQ.focus(); addrQ.dispatchEvent(new Event('input')); return; }
    pickAddr(x);
  } }, chip(x.kind, x.kind === '행안부' ? 'warn' : 'info'), ` ${x.label}`, x.dong ? h('span', { class: 'p' }, x.dong) : null);
  // 주소 자료도 행안부 승인키도 없으면 검색할 곳이 없다: 이유와 할 일을 미리 보여 준다
  const addrSetupNote = () => h('div', { class: 'notice warn small addr-setup' },
    h('b', {}, '주소 검색 자료가 아직 없습니다. '), '그래서 주소 찾기에서 아무것도 나오지 않습니다. 지금은 배출 주소를 직접 적고 오른쪽 지도를 눌러 위치를 정하세요. ',
    boot.me.role === 'ADMIN'
      ? h('span', {}, '검색이 되게 하려면 ', h('a', { href: '#/settings/addr' }, '설정 → 주소·외부 서비스'), '에서 ① 주소 위치 자료(도로명주소 위치정보요약DB, 이 PC 안에서만 검색)를 가져오거나 ② 행안부 주소 검색 승인키를 넣으세요.')
      : '운영 관리자에게 주소 자료 가져오기(설정 → 주소·외부 서비스)를 요청하세요.');
  const noAddrSource = () => !boot.addr_count && !jusoOn();
  // 이 PC의 주소 자료 결과를 먼저, 그 아래 행안부 결과(설정에 승인키가 있을 때). ext: 행안부 응답 또는 { loading: true }
  const drawSug = (list, ext = null) => {
    // 같은 건물(도로명+건물번호)이 이 PC 자료에 이미 있으면 행안부 결과에서 뺀다
    const roadKey = (a) => { const m = /([가-힣A-Za-z0-9·.]+(?:대로|로|길))\s*(\d+(?:-\d+)?)/.exec(a || ''); return m ? m[1] + m[2] : a; };
    const seen = new Set(list.map((x) => roadKey(x.address)));
    const extItems = ((ext && ext.items) || []).filter((x) => !seen.has(roadKey(x.address)));
    const kids = [...list.map(sugBtn), ...extItems.map(sugBtn)];
    if (ext && ext.loading) kids.push(h('p', { class: 'muted pad-s small' }, '행안부 주소 검색 중…'));
    if (ext && (ext.blocked || ext.error)) kids.push(h('p', { class: 'muted pad-s small jusonote' }, '행안부 검색: ' + (ext.blocked || ext.error)));
    if (!kids.length && addrQ.value.trim().length >= 2) kids.push(noAddrSource() ? addrSetupNote()
      : h('p', { class: 'muted pad-s' }, `주소 자료(${(boot.addr_count || 0).toLocaleString()}곳)에서 찾지 못했습니다. 도로명과 건물번호(예: 필문대로 245), 동과 번지(예: 계림동 123), 아파트 이름으로 다시 찾거나, 배출 주소 칸에 직접 적고 지도로 위치를 지정하세요.`));
    addrSug.replaceChildren(...kids);
    addrSug.hidden = !addrQ.value.trim(); addrQ.setAttribute('aria-expanded', addrSug.hidden ? 'false' : 'true');
  };
  const jusoOn = () => !!(boot.settings.external && boot.settings.external.keys.juso_search_key.set);
  addrQ.addEventListener('input', () => { clearTimeout(addrT); addrT = setTimeout(wrap(async () => {
    const q0 = addrQ.value.trim(), seq = ++jusoSeq;
    if (q0.length < 2) return drawSug([]);
    const local = await api('addr-search?q=' + encodeURIComponent(q0));
    if (seq !== jusoSeq) return;
    if (!jusoOn()) return drawSug(local);
    drawSug(local, { loading: true });
    // 서버가 검색어에 전화번호·신청인 이름이 있으면 보내지 않는다(이름 칸 값을 함께 넘겨 대조)
    const j = await api('juso-search?' + new URLSearchParams({ q: q0, name: F.name.value.trim() }));
    if (seq === jusoSeq) drawSug(local, j);
  }), 300); });
  addrQ.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') { const b = addrSug.querySelector('button'); if (b) { e.preventDefault(); b.focus(); } } else if (e.key === 'Escape') { addrSug.hidden = true; } else if (e.key === 'Enter') { e.preventDefault(); const b = addrSug.querySelector('button'); if (b) b.click(); } });
  addrSug.addEventListener('keydown', (e) => { const bs = [...addrSug.querySelectorAll('button')], i = bs.indexOf(document.activeElement); if (e.key === 'ArrowDown' && bs[i + 1]) { e.preventDefault(); bs[i + 1].focus(); } else if (e.key === 'ArrowUp') { e.preventDefault(); (i > 0 ? bs[i - 1] : addrQ).focus(); } });
  // 수수료 감면(새 접수): 사유·증빙 확인 방법·확인자 (식별번호는 적지 않음)
  const redSel = h('select', { 'aria-label': '감면 사유' }, h('option', { value: '' }, '감면 없음'), ...(boot.settings.reductions || []).filter((x) => x.active !== false).map((x) => h('option', { value: x.code, selected: x.code === r.reduction_code }, `${x.label} ${x.rate}%`)));
  const redProof = h('select', { 'aria-label': '증빙 확인 방법' }, h('option', { value: '' }, '(증빙 확인 방법)'), ...(boot.settings.reduction_proofs || []).map((x) => h('option', { value: x, selected: x === r.reduction_proof }, x)));
  const redWho = h('input', { 'aria-label': '확인자', placeholder: '확인자', value: r.reduction_checker || '' });
  const geoMsg = h('span', { class: 'muted' }, r.geo_source === 'ADDR' ? '주소 자료로 찾은 위치 (건물 출입구 기준)' : '');
  const findAddr = wrap(async () => {
    const g = await api('geocode?address=' + encodeURIComponent(F.address.value));
    if (!g || g.none) { geoMsg.textContent = boot.addr_count ? '주소 자료에서 찾지 못했습니다. 도로명과 건물번호를 확인하거나 지도를 눌러 지정하세요.' : '이 PC에 주소 위치 자료가 없어 찾을 수 없습니다. 지도를 눌러 위치를 지정하세요.'; return; }
    if (g.ambiguous) { geoMsg.textContent = `같은 도로명·번호가 여러 지역에 있습니다(${g.options.join(', ')}). 주소에 구 이름을 넣어 주세요.`; return; }
    F.lat.value = g.lat; F.lon.value = g.lon; geoSource = 'ADDR';
    geoMsg.textContent = `${g.sigungu || ''} ${g.label}${g.exact ? '' : ' (부번 없이 본번으로 찾음)'} — 건물 출입구 기준이니 배출 위치를 지도에서 확인하세요.`;
    if (setPin) setPin(g.lat, g.lon, true);
  }, geoMsg);
  setTimeout(async () => {
    try {
      const { map, ml } = await mapIn(mapBox, { center: r.lat != null ? [r.lon, r.lat] : [boot.settings.depot.lon, boot.settings.depot.lat], zoom: r.lat != null ? 17 : 14 });
      let marker = r.lat != null ? M.pin(ml, map, { lng: r.lon, lat: r.lat, label: '●', cls: 'me' }) : null;
      setPin = (lat, lon, fly) => { if (marker) marker.remove(); marker = M.pin(ml, map, { lng: lon, lat, label: '●', cls: 'me' }); if (fly) map.jumpTo({ center: [lon, lat], zoom: 17 }); };
      map.on('click', (e) => {
        const lat = Math.round(e.lngLat.lat * 1e6) / 1e6, lon = Math.round(e.lngLat.lng * 1e6) / 1e6;
        F.lat.value = lat; F.lon.value = lon; geoSource = 'MANUAL'; geoMsg.textContent = '지도에서 지정한 위치';
        setPin(lat, lon, false);
      });
    } catch { mapBox.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다(인터넷 연결 확인). 위치는 나중에 지정해도 됩니다.')); }
  }, 0);

  const reason = h('input', { placeholder: '수정 사유 (필수)', style: 'flex:1' });
  const msg = h('p', { class: 'msg' });
  const data = () => ({ name: F.name.value, phone: F.phone.value, address: F.address.value, spot_note: F.spot_note.value, memo: F.memo.value,
    planned_date: F.planned_date.value, appt_start: F.appt_start.value, appt_end: F.appt_end.value, must_today: F.must_today.checked, urgent: F.urgent.checked,
    priority: +F.priority.value, lat: F.lat.value || null, lon: F.lon.value || null, geo_source: geoSource, dong: F.dong.value, channel: F.channel.value,
    ...(isNew ? { reduction_code: redSel.value, reduction_proof: redProof.value, reduction_checker: redWho.value || (redSel.value ? boot.me.display_name : '') } : {}),
    items: items.map((i) => ({ line_id: i.line_id, fee_rate_id: i.fee_rate_id || null, name: i.name, spec: i.spec, unit: i.unit, unit_label: i.unit_label, qty: i.qty })) });
  // 중복 경고를 본 연락처·주소는 기억한다(화면 밖 변수): 그사이 화면이 다시 그려져도 [접수 등록]을 또 눌러야 하는 일이 없게
  const dupKey = () => `${digitsOnly(F.phone.value)}|${F.address.value.trim()}`;
  // 409: 저장하지 않고 안내. 적은 값은 화면에 그대로 두고, [다시 열기]를 누르면 새 내용을 불러온 뒤 내 값을 옆에 보여 준다.
  const staleNote = h('div', { class: 'stale-note', 'aria-live': 'assertive' });
  const showStale = () => {
    staleNote.replaceChildren(h('div', { class: 'notice bad', role: 'alert' }, h('strong', {}, '다른 곳에서 바뀌었습니다. '),
      '저장하지 않았습니다. 내가 적은 내용은 화면에 그대로 있습니다. [다시 열기]를 누르면 바뀐 내용을 불러오고, 내가 적은 값을 옆에 남겨 다시 넣을 수 있습니다. ',
      btn('다시 열기', () => { staleMine.set(id, { data: data(), base: JSON.parse(baseSnap), reason: reason.value }); render(true); }, 'small primary')));
    staleNote.scrollIntoView({ block: 'center' });
  };
  const save = wrap(async () => {
    if (isNew) {
      if (dupAckKey !== dupKey()) {
        await checkDup();
        if (dupBox.childNodes.length) { dupAckKey = dupKey(); msg.textContent = '비슷한 접수가 있습니다. 별개 접수가 맞으면 [접수 등록]을 한 번 더 누르세요.'; return; }
      }
      // 예정일을 비워 두면 가장 빠른 가능일을 권한다(예정일 없는 접수는 배정·마감에서 빠지기 쉽다)
      if (!F.planned_date.value) {
        if (earliest == null) await loadCal();
        const e = earliest;
        const v = await modal({ title: '수거 예정일이 비어 있습니다', ok: e ? '이 날로 정하고 등록' : '예정일 없이 등록', cancel: '돌아가기',
          extra: e ? [{ label: '예정일 없이 등록', value: 'NONE' }] : [],
          body: [h('p', {}, e ? `가장 빠른 가능일은 ${e.slice(5).replace('-', '월 ')}일입니다(${F.dong.value || '전체'} 기준). 이 날로 정할까요?` : '120일 안에 여유 있는 날이 없습니다. 예정일 없이 등록할까요?'),
            h('p', { class: 'muted small' }, '예정일 없는 접수는 오늘 현황의 [예정일 없음]에 모입니다.')] });
        if (!v) return;
        if (v === true && e) { F.planned_date.value = e; saveDraft(); loadCal(); }
      }
      const res = await postOnce('intake', 'requests', { ...data(), intake_secs: intakeT0 ? Math.round((Date.now() - intakeT0) / 1000) : null });
      intakeDraft = null; dupAckKey = null; intakeT0 = null; justSaved = res.id;
      toast(`${res.receipt_no} 등록했습니다`); (res.warnings || []).forEach((w) => toast(w, true)); go(`#/request/${res.id}`);
    } else {
      let res;
      try { res = await post('requests/' + id, { ...data(), reason: reason.value, req_ver: baseVer }); } catch (e) { if (isStale(e)) { showStale(); throw new Error('다른 곳에서 바뀌어 저장하지 않았습니다.'); } throw e; }
      staleMine.delete(id);
      toast(res.resend_needed ? '수정했습니다. 이미 배정된 건이라 목록 재전송이 필요합니다.' : '수정했습니다'); (res.warnings || []).forEach((w) => toast(w, true)); render();
    }
  }, msg);
  const cancel = wrap(async () => {
    const paid = r.payment ? r.payment.paid : 0;
    const res = await reasonDlg('접수 취소', boot.cancel_codes, { ok: '취소 기록', danger: true, label: '취소 사유',
      lead: [h('p', {}, '기록은 지워지지 않습니다.'), paid > 0 ? h('div', { class: 'notice bad' }, `납부 기록 ${won(paid)}이 있습니다. 취소하면 '환불 필요'로 표시되고 마감 수납 확인 목록에 나옵니다.`) : null,
        r.closed ? h('div', { class: 'notice warn' }, `마감한 날(${r.closed.work_date})의 접수입니다. '마감 후 수정'으로 기록됩니다.`) : null],
      submit: async (v) => { try { return await post(`requests/${id}/cancel`, { code: v.code, reason: v.text, req_ver: baseVer }); } catch (e) { if (isStale(e)) { showStale(); return 'STALE'; } throw e; } } });
    if (!res || res === 'STALE') return;
    if (res.refund_needed) toast(`환불 필요: 납부 기록 ${won(res.paid)}을 돌려주고 수납 기록에 환불을 남기세요.`, true);
    toast(res.field_notice_needed ? '취소했습니다. 이미 휴대폰에 전달된 건이니 수거팀에 전화로 알리세요.' : '취소했습니다'); render();
  }, msg);
  const editable = r.status !== 'CANCELLED' && canWork();
  // 작성 중인 새 접수는 메모리에 담아 둔다
  saveDraft = () => { if (isNew) intakeDraft = { r: { ...data(), must_today: F.must_today.checked ? 1 : 0, urgent: F.urgent.checked ? 1 : 0 }, items: items.map((i) => ({ ...i })) }; };

  // 유사 품목 적용: 표에 없는 품목(확인 대기)을 단가가 확정된 수수료표 행에 연결 (근거·확인자 기록)
  let linkOpen = null;
  const linkBox = (l) => {
    document.querySelectorAll('tr.linkrow').forEach((tr) => { tr.hidden = tr.dataset.line !== linkOpen; });
    const tr = document.querySelector(`tr.linkrow[data-line="${l.line_id}"]`); if (!tr || tr.hidden) return;
    const q = h('input', { type: 'search', placeholder: `${l.name}와 비슷한 품목 찾기 (단위: ${l.unit_label})`, style: 'width:320px' });
    const res = h('div', { class: 'results' }); let sel = null;
    const why = h('input', { placeholder: '적용 근거 (예: 같은 재질·크기, 담당 부서 확인)', style: 'flex:1;min-width:260px' });
    const who = h('input', { placeholder: '확인자', value: boot.me.display_name, style: 'width:140px' });
    const m = h('span', { class: 'msg' });
    const draw = () => res.replaceChildren(...searchFees(FEES(), q.value).filter((x) => x.status === 'CONFIRMED').slice(0, 20).map((x) =>
      h('button', { type: 'button', class: sel === x.id ? 'on' : '', onclick: () => { sel = x.id; draw(); } }, `${x.name} ${x.spec}`, h('span', { class: 'p' }, `${won(x.price)} / ${x.unit_label}`),
        x.unit_label !== l.unit_label ? h('span', { class: 'w' }, '단위 다름') : null)));
    q.addEventListener('input', () => { sel = null; draw(); });
    tr.firstChild.replaceChildren(h('div', { class: 'linkbox' }, q, res, h('div', { class: 'row' }, why, who,
      btn('적용', wrap(async () => { if (!sel) throw new Error('적용할 품목 행을 고르세요.'); await post(`requests/${id}/link`, { line_id: l.line_id, fee_rate_id: sel, reason: why.value, confirmed_by: who.value }); toast('유사 품목 단가를 적용했습니다'); linkOpen = null; afterAction(); }, m), 'primary'), m),
      h('p', { class: 'small muted' }, '단가가 확정된 행만, 부과단위가 같은 행만 적용됩니다. 원래 품목 이름은 그대로 두고 적용 기록을 따로 남깁니다.')));
    setTimeout(() => q.focus(), 20);
  };

  // 현장 결과 (아래 카드들은 수정 양식과 따로 다시 그린다: 수납·통화 기록을 남겨도 적던 수정 내용이 지워지지 않게)
  const fieldCard = (r) => { const st = r.state; return !isNew && st ? h('div', { class: 'card' }, h('h2', {}, '현장 결과'),
    h('div', { class: 'row' }, statusChip(r.status), chip('연락: ' + CONTACT[st.contact]), st.outcome ? chip('저장된 결과: ' + (STATUS[st.outcome] || st.outcome)) : null),
    fieldExtras(st, r),
    h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['품목', '구분', '처음 접수', '현장 수량', '실은 수량', '못 실은 수량', '사유', '스티커', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...st.lines.flatMap((l) => [h('tr', {}, h('td', {}, `${l.name} ${l.spec}`,
          l.link ? h('div', { class: 'small muted' }, `유사 품목 적용: ${l.link.name} ${l.link.spec} ${won(l.price)}/${l.unit_label} · 근거 ${l.link.reason} · 확인 ${l.link.confirmed_by}`) : null),
        h('td', {}, l.kind === 'FIELD' ? chip('현장 추가', 'info') : '처음 접수'),
        h('td', {}, l.qty_received), h('td', {}, l.qty_confirmed), h('td', {}, l.qty_collected), h('td', {}, l.remaining ? chip(`${l.remaining}${l.unit_label}`, 'warn') : '0'), h('td', {}, REASON[l.reason] || l.reason || ''),
        h('td', {}, (() => { const k = st.field && st.field.stickers[l.line_id]; return k ? chip(STICKER[k.result] || k.result, k.result === 'OK' ? 'ok' : 'warn', ) : ''; })(),
          (() => { const k = st.field && st.field.stickers[l.line_id]; return k && k.result !== 'OK' ? h('div', { class: 'small muted' }, `${k.number ? '번호 ' + k.number + ' · ' : ''}${k.decision === 'LEAVE' ? '미수거' : k.decision === 'COLLECT' ? '그래도 수거' : ''}`) : null; })()),
        h('td', {}, l.price == null && editable && boot.me.role !== 'FIELD' ? btn('유사 품목 적용', () => { linkOpen = linkOpen === l.line_id ? null : l.line_id; linkBox(l); }, 'small') : null,
          l.link && editable && boot.me.role !== 'FIELD' ? btn('해제', wrap(async () => { const why = await promptDlg('유사 품목 적용 해제', '해제 사유', { ok: '해제', danger: true }); if (!why) return; await post(`links/${l.link.id}/void`, { reason: why }); toast('해제했습니다'); afterAction(); }), 'small') : null)),
        h('tr', { class: 'linkrow', hidden: true, 'data-line': l.line_id }, h('td', { colspan: 9 }))])))),
    h('dl', { class: 'kv' }, h('dt', {}, '접수 당시 금액'), h('dd', {}, won(st.fee.received)), h('dt', {}, '현장 추가분'), h('dd', {}, won(st.fee.field_added)),
      h('dt', {}, '접수 대비 증감'), h('dd', {}, (st.fee.diff > 0 ? '+' : '') + won(st.fee.diff)), h('dt', {}, '금액 확인 대기'), h('dd', {}, st.fee.pending_count + '건'),
      h('dt', {}, '최종 금액'), h('dd', {}, st.fee.final == null ? '미확정' : won(st.fee.final))),
    h('p', { class: 'muted' }, '금액은 현장 확인 수량 기준이며 수납 완료를 뜻하지 않습니다. 일부 수거로 금액을 자동 감액하지 않습니다.'),
    (st.memos || []).length ? h('div', { class: 'notice' }, h('strong', {}, '현장 메모'), h('ul', {}, ...st.memos.map((m) => h('li', {}, m)))) : null,
    r.photos && r.photos.length ? h('div', { class: 'thumbs' }, ...r.photos.map((p) => p.purged_at ? chip(`보관 기간 만료로 정리됨 (${p.purged_at.slice(0, 10)})`) : p.recovery_status === 'RECOVERED' ? h('figure', { class: 'ph' }, photoImg(p.photo_id), h('figcaption', {}, (p.phase === 'BEFORE' ? '수거 전' : '수거 후') + (p.line_id && st.lines.find((l) => l.line_id === p.line_id) ? ` · ${st.lines.find((l) => l.line_id === p.line_id).name}` : ''),
      p.taken_at ? h('small', { class: 'block' }, `촬영 ${kst(p.taken_at)}${p.has_gps ? ` · 위치 ±${Math.round(p.acc || 0)}m` : ''}`) : null)) : chip(`사진 미회수 (${p.phase === 'BEFORE' ? '전' : '후'})`, 'warn'))) : null) : null; };
  // 수정 양식에 저장하지 않은 내용이 있는지(처음 그린 값과 비교)
  let baseSnap = null;
  const dirty = () => !isNew && baseSnap != null && JSON.stringify(data()) !== baseSnap;
  const lower = h('div', { class: 'req-lower' });
  const versionsCard = (rr) => (rr.versions.length ? h('div', { class: 'card' }, h('h2', {}, '수정 이력'), h('ul', {}, ...rr.versions.map((v) => h('li', {}, `버전 ${v.req_ver} → ${v.req_ver + 1} · ${kst(v.at)} · ${v.reason}`)))) : null);
  const ctx = { ver: () => baseVer, stale: () => showStale(), after: () => afterAction() };
  const lowerCards = (rr) => (isNew ? [] : [fieldCard(rr), paymentCard(rr, ctx), reductionCard(rr, ctx), contactCard(rr, ctx), versionsCard(rr)]);
  // 아래 카드의 작업(수납·감면·통화·유사 품목·확인 완료)이 끝난 뒤: 수정 중이면 아래 카드만 새로, 아니면 화면 전체를 새로
  const afterAction = async () => {
    if (!dirty()) return render();
    const r2 = await api('requests/' + id);
    lower.replaceChildren(...lowerCards(r2).filter(Boolean));
    toast('수정 중인 내용은 그대로 두고 아래 기록만 새로 불러왔습니다');
  };

  // 전화 받는 순서대로: 왼쪽 = 신청자 → 버릴 물건, 오른쪽 = 일정 → 위치. 저장 단추는 화면 아래에 늘 보이게.
  F.lat.tabIndex = -1; F.lon.tabIndex = -1;
  const actionbar = editable ? h('div', { class: 'actionbar' }, isNew ? null : reason,
    btn(isNew ? '접수 등록' : '수정 저장', save, 'primary', { 'data-save': '1' }), h('span', { class: 'muted small' }, h('kbd', {}, 'Ctrl'), '+', h('kbd', {}, 'Enter')),
    barTotal, h('span', { class: 'grow' }), msg, isNew ? null : btn('접수 취소', cancel, 'danger')) : null;
  setTimeout(() => { loadCal(); if (isNew && !(fromDraft && r.dong)) detect(); }, 0);
  const form = h('div', { class: 'reqform', oninput: () => { intakeSeen = Date.now(); saveDraft(); }, onchange: () => saveDraft() },
    h('div', { class: 'pagehead' }, h('h1', {}, isNew ? '전화 접수 등록' : `접수 ${r.receipt_no}`),
      h('div', { class: 'row' }, !isNew && r.levy_no ? h('span', { class: 'chip' }, `부과번호 ${r.levy_no}`) : null,
        !isNew ? btn('안내 문구 복사', wrap(() => copyNotice(id)), 'small', { title: '주민에게 보낼 안내 글(접수번호·배출일·금액·납부 방법·문의처, 이름·주소 없음)' }) : null,
        !isNew && canWork() ? btn('접수증 인쇄', wrap(async () => {
          // 문의처가 아직 기본 문구면 접수증에 그대로 찍힌다: 먼저 알린다
          if (isPlaceholderContact() && !(await confirmDlg('문의처가 아직 기본 문구입니다', `접수증의 문의처가 "${boot.settings.contact_info}"로 찍힙니다. 설정 → 기본 → 접수증·월보 문구에서 전화번호를 넣으세요.`,
            { ok: '그대로 인쇄', more: boot.me.role === 'ADMIN' ? [h('a', { href: '#/settings/basic' }, '설정에서 문의처 넣기 →')] : [] }))) return;
          const t = await post(`requests/${id}/receipt-print`); window.open('/print/doc/' + t.token, 'dg-print'); toast('접수증을 열었습니다 (출력 기록이 남습니다)');
        }), 'small') : null,
        !isNew && canWork() && r.status !== 'CANCELLED' ? btn('배출 표지 인쇄', wrap(async () => { const t = await post('requests/labels', { ids: [id] }); window.open('/print/doc/' + t.token, 'dg-print'); toast('배출 표지를 열었습니다'); }), 'small') : null,
        btn('← 목록', () => go('#/requests')))),
    fromDraft ? h('div', { class: 'notice info' }, '작성 중이던 접수를 불러왔습니다. ', btn('비우고 새로 쓰기', () => { intakeDraft = null; dupAckKey = null; reqDone('intake'); intakeT0 = Date.now(); render(true); }, 'small')) : null,
    showDone ? h('div', { class: 'notice ok-note', role: 'status' }, h('strong', {}, `${r.receipt_no} 등록을 마쳤습니다. `), r.levy_no ? `부과번호 ${r.levy_no} · ` : '', '주민에게 안내 문구를 보내거나 다음 전화를 받으세요. ',
      btn('+ 다음 접수', () => { intakeDraft = null; dupAckKey = null; go('#/request/new'); }, 'small primary'), ' ', btn('주민 안내 문구 복사', wrap(() => copyNotice(id)), 'small')) : null,
    !isNew && r.origin_device ? h('div', { class: 'notice ' + (r.field_checked_at ? 'info' : 'warn') }, h('strong', {}, '휴대폰 현장 접수 '),
      r.field_checked_at ? `— ${kst(r.field_checked_at)} 사무실 확인 (${r.field_checked_by || '-'})` : '— 수거팀이 현장에서 받은 접수입니다. 주소·동·품목·금액을 확인하고 필요하면 고친 뒤 [확인 완료]를 누르세요. ',
      !r.field_checked_at && canWork() ? btn('확인 완료', wrap(async (e) => { const b = e.currentTarget; await post(`requests/${id}/field-check`); toast('현장 접수를 확인했습니다');
        if (dirty()) { b.replaceWith(h('span', {}, `— 방금 사무실 확인 (${boot.me.display_name})`)); } else render(true); }), 'small primary') : null) : null,
    r.status === 'CANCELLED' ? h('div', { class: 'notice' }, '취소된 접수입니다. 사유: ' + (r.cancel_label || r.cancel_reason || '-')) : null,
    r.closed && r.status !== 'CANCELLED' ? h('div', { class: 'notice warn' }, `이 접수의 예정일(${r.closed.work_date})은 마감되었습니다(${r.closed.seq}회차 · ${r.closed.status === 'APPROVED' ? '팀장 확인' : '담당자 기록'}). 고치면 사유와 함께 "마감 후 수정"으로 기록됩니다.`) : null,
    h('div', { class: 'grid2' },
      h('div', {},
        h('div', { class: 'card formtable' }, h('h2', {}, '신청자'),
          h('div', { class: 'grid2' }, lab('이름', F.name), lab('연락처', F.phone)),
          editable ? h('div', { class: 'addr-find' }, h('label', { class: 'f' }, '주소 찾기', addrQ), addrSug, noAddrSource() ? addrSetupNote() : null) : null,
          lab('배출 주소', F.address), addrWarn,
          h('div', { class: 'grid2' }, lab('행정동', F.dong), lab('접수 경로', F.channel)), dongMsg && h('p', { class: 'dongmsg' }, dongMsg),
          lab('상세 배출 위치', F.spot_note), lab('요청 메모', F.memo), dupBox),
        h('div', { class: 'card' }, h('h2', {}, '버릴 물건'), noticeBox, itemsBox, total, editable ? picker : null,
          isNew ? h('details', { class: 'free redbox', open: !!r.reduction_code }, h('summary', {}, '수수료 감면 (해당자만)'),
            h('div', { class: 'row' }, redSel, redProof, redWho),
            h('p', { class: 'small muted' }, '감면 사유와 증빙을 어떻게 확인했는지만 남깁니다. 주민등록번호·수급자 번호 같은 식별번호는 적지 마세요. 감면율은 설정의 정책 값(조례 확인 필요)입니다.')) : null)),
      h('div', {},
        h('div', { class: 'card formtable' }, h('h2', {}, '일정·우선순위'),
          lab('수거 예정일', F.planned_date),
          h('div', { class: 'calwrap' }, h('div', { class: 'row' }, h('span', { class: 'small muted' }, '다음 업무일 예약/한도'), h('span', { class: 'grow' }),
            btn('가장 빠른 가능일', () => { if (!earliest) return toast('120일 안에 여유 있는 날이 없습니다.', true); F.planned_date.value = earliest; loadCal(); saveDraft(); }, 'small')), calBox, calWarn),
          h('div', { class: 'grid2' }, lab('방문 약속 시작', F.appt_start), lab('방문 약속 끝', F.appt_end)),
          h('div', { class: 'row checks' }, h('label', { class: 'row' }, F.must_today, '오늘 반드시 포함'), h('label', { class: 'row' }, F.urgent, '긴급'), lab('품목 우선순위', F.priority)),
          h('p', { class: 'muted' }, '오전에 먼저 가야 하면 방문 약속 시간을 넣으세요. 우선순위는 "포함 여부"에만 영향을 줍니다.')),
        h('div', { class: 'card' }, h('h2', {}, '배출 위치'), h('p', { class: 'muted' }, jusoOn() ? '이 PC의 도로명주소 위치 자료나 [주소 찾기]의 행안부 결과로 찾거나 지도를 눌러 지정합니다. 행안부 검색에는 주소 찾기 칸의 검색어만 PC 서버가 보냅니다(이름·연락처는 보내지 않음).' : '이 PC에 넣어 둔 도로명주소 위치 자료로 찾거나 지도를 눌러 지정합니다. 주소는 외부로 보내지 않습니다.'),
          h('div', { class: 'row' }, btn('주소로 위치 찾기', findAddr, 'small'), geoMsg),
          mapBox, h('div', { class: 'row' }, '위도', F.lat, '경도', F.lon)))),
    staleNote,
    actionbar,
    lower);
  lower.replaceChildren(...lowerCards(r).filter(Boolean));
  baseSnap = JSON.stringify(data());
  // 409 뒤 다시 연 화면: 내가 적었던 값을 보여 주고 [내 값 다시 넣기]
  const mine = !isNew && staleMine.get(id);
  if (mine) {
    const norm = (k, v) => (k === 'phone' ? digitsOnly(v) : String(v ?? ''));
    // 내가 고친 칸만(처음 연 값과 다른 칸): 다른 사람이 바꾼 칸은 그대로 둔다
    const diffs = Object.keys(FORM_LABEL).filter((k) => norm(k, mine.data[k]) !== norm(k, mine.base[k]));
    const itemsTxt = (arr) => arr.map((i) => `${i.name} ${i.spec || ''} ${i.qty}`).join(', ');
    const itemsDiff = itemsTxt(mine.data.items) !== itemsTxt(mine.base.items);
    const flagDiff = ['must_today', 'urgent', 'priority'].filter((k) => String(mine.data[k]) !== String(mine.base[k]));
    const geoDiff = String(mine.data.lat) !== String(mine.base.lat) || String(mine.data.lon) !== String(mine.base.lon);
    const refill = () => {
      diffs.forEach((k) => { if (F[k]) F[k].value = k === 'phone' ? fmtPhone(digitsOnly(mine.data[k])) || mine.data[k] : mine.data[k] ?? ''; });
      if (flagDiff.includes('must_today')) F.must_today.checked = !!mine.data.must_today;
      if (flagDiff.includes('urgent')) F.urgent.checked = !!mine.data.urgent;
      if (flagDiff.includes('priority')) F.priority.value = String(mine.data.priority ?? 0);
      if (geoDiff && mine.data.lat != null) { F.lat.value = mine.data.lat; F.lon.value = mine.data.lon; geoSource = mine.data.geo_source; }
      if (itemsDiff) { items.splice(0, items.length, ...mine.data.items.map((i) => ({ ...(r.items.find((x) => x.line_id && x.line_id === i.line_id) || {}), ...i }))); drawItems(); }
      reason.value = mine.reason || ''; staleMine.delete(id); mineBox.remove(); loadCal(); toast('내가 적었던 값을 다시 넣었습니다. 확인하고 [수정 저장]을 누르세요.');
    };
    const mineBox = h('div', { class: 'notice info mine-box' }, h('strong', {}, '바뀐 내용을 다시 불러왔습니다. '), diffs.length || itemsDiff || flagDiff.length || geoDiff ? '내가 고쳤던 값(아직 저장 안 됨):' : '내가 고친 칸은 없었습니다.',
      h('ul', {}, ...diffs.map((k) => h('li', {}, `${FORM_LABEL[k]}: `, h('b', {}, mine.data[k] || '(비움)'), h('span', { class: 'muted' }, ` (지금 ${r[k] || '(비움)'})`))),
        itemsDiff ? h('li', {}, '품목: ', h('b', {}, itemsTxt(mine.data.items) || '(없음)')) : null),
      h('div', { class: 'row' }, btn('내 값 다시 넣기', refill, 'small primary'), btn('버리기', () => { staleMine.delete(id); mineBox.remove(); }, 'small')));
    form.insertBefore(mineBox, form.children[1] || null);
  }
  return form;
}

// 휴대폰 v0.10 현장 기록: 도착·작업자·문자/길안내(횟수만)·추가 금액 고지·부재 증거·모르는 기록 종류
function fieldExtras(st, r) {
  const fx = st.field;
  if (!fx) return null;
  const cells = [];
  if (fx.arrive) cells.push(['도착', `${kst(fx.arrive.at)}${fx.arrive.acc != null ? ` · 위치 정확도 ±${Math.round(fx.arrive.acc)}m` : fx.arrive.has_gps ? '' : ' · 위치 없음'}`]);
  if (fx.workers.length) cells.push(['작업자', fx.workers.join(', ')]);
  const sms = Object.entries(fx.sms || {});
  if (sms.length || fx.nav) cells.push(['문자·길안내', [sms.map(([k, n]) => `문자(${SMS_T[k] || k}) ${n}회`).join(', '), fx.nav ? `길안내 앱 ${fx.nav}회 (${Object.entries(fx.nav_apps || {}).map(([a, n]) => `${NAV_APP[a] || a} ${n}`).join(', ')})` : ''].filter(Boolean).join(' · ') + ' (앱을 연 횟수, 내용 없음)']);
  if (fx.fee_notices.length) cells.push(['추가 금액 고지', h('span', {}, ...fx.fee_notices.map((n) => h('span', { class: 'block' }, chip(NOTICE[n.response] || n.response || '-', n.response === 'REFUSED' ? 'bad' : n.response === 'AGREED' ? 'ok' : 'warn'),
    ` ${kst(n.at)} · 접수 ${won(n.received || 0)} → 현장 ${won(n.total || 0)} (추가 ${won(n.extra || 0)})${n.pending ? ` · 확인 대기 ${n.pending}건` : ''}${n.worker ? ' · ' + n.worker : ''}`)))]);
  if (fx.no_photo_reason) cells.push(['사진 없는 사유', NO_PHOTO[fx.no_photo_reason] || fx.no_photo_reason]);
  else if (r.status === 'NOT_COLLECTED' && !(r.photos || []).length) cells.push(['부재 증거', chip('사진도 사유도 없음 — 확인 필요', 'warn')]);
  const other = Object.entries(fx.other_kinds || {});
  if (other.length) cells.push(['기타 기록', other.map(([k, n]) => `${k} ${n}건`).join(', ') + ' (이 PC가 모르는 종류 — 보관만)']);
  return cells.length ? h('dl', { class: 'kv' }, ...cells.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)])) : null;
}

// 수수료 감면 (접수 상세): 사유·감면율·증빙 확인 방법·확인자. 식별번호는 받지 않는다.
function reductionCard(r, ctx = {}) {
  const msg = h('span', { class: 'msg' });
  const sel = h('select', { 'aria-label': '감면 사유' }, h('option', { value: '' }, '감면 없음'), ...(boot.settings.reductions || []).filter((x) => x.active !== false || x.code === r.reduction_code).map((x) => h('option', { value: x.code, selected: x.code === r.reduction_code }, `${x.label} ${x.rate}%`)));
  const proof = h('select', { 'aria-label': '증빙 확인 방법' }, h('option', { value: '' }, '(증빙 확인 방법)'), ...(boot.settings.reduction_proofs || []).map((x) => h('option', { value: x, selected: x === r.reduction_proof }, x)));
  const who = h('input', { 'aria-label': '확인자', placeholder: '확인자', value: r.reduction_checker || boot.me.display_name, class: 'w-s' });
  const why = r.closed ? h('input', { placeholder: '마감 후 수정 사유 (필수)', 'aria-label': '감면 변경 사유', class: 'w-m' }) : null;
  return h('div', { class: 'card' }, h('h2', {}, '수수료 감면 ', r.reduction_code ? chip(`${r.reduction_label} ${r.reduction_rate}%`, 'info') : chip('없음', 'neutral')),
    r.reduction_code ? h('p', { class: 'small muted' }, `증빙 ${r.reduction_proof || '-'} · 확인 ${r.reduction_checker || '-'} · ${kst(r.reduction_at)}`) : null,
    canWork() && r.status !== 'CANCELLED' ? h('div', { class: 'row' }, sel, proof, who, why, btn('감면 저장', wrap(async () => {
      if (!sel.value && !r.reduction_code) throw new Error('감면 사유를 고르세요.');
      try { await post(`requests/${r.id}/reduction`, { reduction_code: sel.value, reduction_proof: proof.value, reduction_checker: who.value, reason: why ? why.value : '', req_ver: ctx.ver ? ctx.ver() : r.req_ver }); }
      catch (e) { if (isStale(e) && ctx.stale) { ctx.stale(); throw new Error('다른 곳에서 바뀌어 저장하지 않았습니다.'); } throw e; }
      toast(sel.value ? '감면을 적용했습니다' : '감면을 해제했습니다'); await (ctx.after ? ctx.after() : render());
    }, msg), 'primary small'), msg) : null,
    h('p', { class: 'small muted' }, '감면율은 설정의 정책 값입니다(조례 확인 필요). 주민등록번호·수급자 번호 같은 식별번호는 저장하지 않습니다.'));
}

// 민원·통화 기록 (접수별): 일시·유형·내용·담당자. 내용은 변경 이력에 남기지 않는다.
function contactCard(r, ctx = {}) {
  const msg = h('span', { class: 'msg' });
  const now = new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 16);
  const at = h('input', { type: 'datetime-local', value: now, 'aria-label': '일시' });
  const kind = h('select', { 'aria-label': '유형' }, ...boot.contact_kinds.map((k) => h('option', { value: k }, k)));
  const content = h('input', { placeholder: '내용 (예: 수거 날짜 문의, 오후 방문 요청)', class: 'fill', 'aria-label': '내용' });
  return h('div', { class: 'card' }, h('h2', {}, `민원·통화 기록 ${r.contacts.length}건`),
    r.contacts.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['일시', '유형', '내용', '담당자'].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...r.contacts.map((c) => h('tr', {}, h('td', { class: 'nowrap' }, c.at.replace('T', ' ')), h('td', {}, chip(c.kind, c.kind === '불만' ? 'bad' : c.kind === '독촉' ? 'warn' : '')), h('td', {}, c.content), h('td', {}, c.actor || ''))))) : h('p', { class: 'empty' }, '기록 없음'),
    canWork() ? h('div', { class: 'row' }, at, kind, content, btn('기록', wrap(async () => { await postOnce(`contact:${r.id}`, `requests/${r.id}/contacts`, { at: at.value, kind: kind.value, content: content.value }); toast('기록했습니다'); await (ctx.after ? ctx.after() : render()); }, msg), 'primary'), msg) : null);
}

// 수납: 담당자가 확인한 납부 기록. 계산 금액·납부 금액·상태를 따로 보여 주고, 정정은 지우지 않고 사유를 남긴다
function paymentCard(r, ctx = {}) {
  const p = r.payment;
  const msg = h('span', { class: 'msg' });
  const after = () => (ctx.after ? ctx.after() : render());
  // 여기서는 납부만 기록한다. 환불은 [환불] 화면의 결재선(신청 → 다른 사람 결재 → 지급 기록)으로만 남긴다
  const remain = p.levied != null ? Math.max(0, p.levied - p.paid) : '';
  const amount = h('input', { type: 'number', min: 1, value: remain || '', placeholder: '금액(원)', style: 'width:120px', 'aria-label': '금액' });
  const method = h('select', { 'aria-label': '납부 방법' }, ...(boot.settings.pay_methods || ['기타']).map((m) => h('option', { value: m }, m)));
  const ref = h('input', { placeholder: '영수번호·거래번호', class: 'w-s', 'aria-label': '영수번호·거래번호' });
  const paidOn = h('input', { type: 'date', value: boot.today });
  const note = h('input', { placeholder: '메모(입금자명 등)', style: 'flex:1' });
  const why = r.closed ? h('input', { placeholder: '마감 후 수정 사유 (필수)', style: 'flex:1', 'aria-label': '마감 후 수정 사유' }) : null;
  if (p.status === 'REFUND_NEEDED') amount.value = '';
  const cls = { PAID: 'ok', UNPAID: 'bad', UNDERPAID: 'warn', OVERPAID_REVIEW: 'warn', AMOUNT_PENDING: 'warn', PAID_AMOUNT_PENDING: 'warn', REFUND_NEEDED: 'bad', OVERREFUNDED_REVIEW: 'warn' }[p.status] || '';
  const openRefund = (r.refunds || []).find((f) => ['DRAFT', 'REQUESTED', 'APPROVED'].includes(f.status));
  return h('div', { class: 'card' }, h('h2', {}, '수납 기록 ', chip(PAY[p.status], cls)),
    h('div', { class: 'feeflow', 'aria-label': '금액 계산' },
      h('div', {}, h('span', {}, '계산 금액'), h('b', {}, p.calculated == null ? '미확정' : won(p.calculated))), h('i', { 'aria-hidden': 'true' }, '→'),
      h('div', {}, h('span', {}, `감면액${p.reduction_rate ? ` (${p.reduction_label} ${p.reduction_rate}%)` : ''}`), h('b', {}, p.reduction_rate ? (p.reduction == null ? '미확정' : '−' + won(p.reduction)) : '없음')), h('i', { 'aria-hidden': 'true' }, '→'),
      h('div', { class: 'main' }, h('span', {}, '부과 금액'), h('b', {}, p.levied == null ? '미확정' : won(p.levied))),
      h('div', {}, h('span', {}, '납부 확인 금액'), h('b', {}, won(p.paid)))),
    p.calculated == null ? h('p', { class: 'small muted' }, '금액 확인 대기 품목이 있어 부과 금액이 정해지지 않았습니다.') : null,
    p.records.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['날짜', '구분', '금액', '방법', '영수번호', '출처', '메모', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...p.records.map((x) => h('tr', { class: x.void_reason ? 'muted' : '' }, h('td', {}, x.paid_on), h('td', {}, x.kind === 'PAY' ? '납부' : '환불'),
        h('td', { class: 'num' }, x.void_reason ? h('s', {}, won(x.amount)) : won(x.amount)), h('td', {}, x.method), h('td', { class: 'code' }, x.receipt_ref || ''), h('td', {}, x.source || '수기'), h('td', {}, x.note || '', x.void_reason ? chip('정정: ' + x.void_reason) : null),
        h('td', {}, x.void_reason ? '' : btn('정정', wrap(async () => { const why = await promptDlg('수납 기록 정정', '정정 사유', { ok: '정정', lead: [h('p', { class: 'muted' }, '기록은 지워지지 않고 정정 표시와 사유가 남습니다.' + (r.closed ? ' 마감한 날의 접수라 "마감 후 수정"으로 기록됩니다.' : ''))] }); if (!why) return; await post(`payments/${x.id}/void`, { reason: why }); await after(); }, msg), 'small'))))))) : null,
    p.status === 'REFUND_NEEDED' ? h('div', { class: 'notice bad' }, `취소된 접수에 납부 기록 ${won(p.paid)}이 남아 있습니다. `, h('a', { href: '#/refunds' }, '환불 목록'), '에서 신청 → 결재 → 지급 기록을 남기세요.') : null,
    (r.refunds || []).length ? h('div', { class: 'notice info' }, h('strong', {}, '환불 '), ...(r.refunds || []).map((f) => h('span', { class: 'block' }, chip(REFUND_ST[f.status] || f.status, f.status === 'PAID' ? 'ok' : f.status === 'REJECTED' || f.status === 'VOID' ? 'neutral' : 'warn'),
      ` ${REFUND_CAUSE[f.cause] || f.cause} ${won(f.amount)}${f.approved_by ? ' · 결재 ' + f.approved_by : ''}${f.paid_on ? ' · 지급 ' + f.paid_on : ''}`)), ' ', h('a', { href: '#/refunds' }, '환불 목록 →')) : null,
    canWork() && !openRefund && r.status === 'PARTIAL' && p.paid > 0 ? h('div', { class: 'row' }, h('span', { class: 'muted small' }, '일부 수거로 끝났고 납부 기록이 있습니다. 재방문하지 않기로 했으면 '),
      h('a', { href: '#/refunds', class: 'small', onclick: () => { screenRefunds.s = { status: 'OPEN' }; } }, '환불 화면에서 차액 신청 →')) : null,
    canWork() ? h('div', { class: 'row' }, h('span', { class: 'chip' }, '납부'), amount, method, ref, paidOn, note, why, btn('수납 기록', wrap(async () => {
      await postOnce(`pay:${r.id}`, `requests/${r.id}/payments`, { kind: 'PAY', amount: amount.value, method: method.value, receipt_ref: ref.value, paid_on: paidOn.value, note: note.value, reason: why ? why.value : '' }); toast('수납을 기록했습니다'); await after();
    }, msg), 'primary'), msg) : null,
    h('p', { class: 'muted' }, '수납 상태는 부과 금액(계산 금액 − 감면액)과 비교합니다. 일부 수거로 금액을 자동으로 줄이거나 환불을 확정하지 않습니다. 환불은 환불 목록에서 결재를 거쳐 지급 기록을 남깁니다.'));
}

function photoImg(pid) {
  const img = h('img', { alt: '현장 사진' });
  fetch('/api/photo/' + pid, { headers: { 'X-DG-CSRF': '1' }, credentials: 'same-origin' }).then((r) => r.ok && r.blob()).then((b) => { if (b) img.src = URL.createObjectURL(b); });
  return img;
}

// ------------------------------------------------------------ 배정·노선
// 로그아웃 때 접수 양식 임시 상태를 비운다(다음 사용자에게 남지 않게)
function resetIntake() { intakeDraft = null; dupAckKey = null; }

export {
  FORM_LABEL, contactCard, digitsOnly, dupAckKey, fieldExtras, intakeDraft, intakeSeen, intakeT0,
  justSaved, paymentCard, photoImg, reductionCard, resetIntake, screenRequest, staleMine,
};
