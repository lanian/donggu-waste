// 휴대폰 화면: 단독 모드 접수 등록·수정·주소 검색
import * as J from '../juso.js';
import { BUILTIN_FEES } from '../fees-data.js';
import { deriveStop } from '../fees.js';
import { won } from '../fees.js';
import { isUnlocked } from '../store.js';
import { addEvent, addrRoute, allStops, btn, commit, digits, dongOf, feeTable, fieldEditable, findStop, fmtPhone, go, h, isSolo, nextSeq, normAddr, pcWorkDate, registeredNow, render, st, stepFlush, stepper, toast, todayKST, ui, withWorker } from '../core.js';
import { pickScreen } from '../widgets.js';

// PC 연결 방식 휴대폰에서 받는 접수의 경로(PC 접수 경로와 같은 이름)
const FIELD_CHANNELS = ['현장', '전화', '방문'];
// 같은 양식을 다시 그려도 저장 예약은 하나다. 잠금 전 기존 flush 경계에서 초안을 저장한다.
const draftSaves = new WeakMap();
function draftFor(form, state) {
  const previous = draftSaves.get(form);
  if (previous?.state === state) return previous;
  previous?.cancel();
  const key = Symbol('intake-draft');
  let timer = null, pending = false;
  const cancel = () => {
    clearTimeout(timer); timer = null; pending = false; stepFlush.delete(key);
    if (draftSaves.get(form) === draft) draftSaves.delete(form);
  };
  const flush = () => {
    if (!pending) return;
    cancel();
    if (st !== state || ui.form !== form || !isUnlocked()) return;
    const { pick, dupAck, addrAck, reason, id: _i, lat, lon, dongCands, picking, ...data } = form;
    state.intakeDraft = { ...data, addrLat: lat, addrLon: lon };
    return commit();
  };
  const schedule = () => {
    if (st !== state || ui.form !== form || !isUnlocked()) return;
    draftSaves.get(form)?.cancel(); pending = true; draftSaves.set(form, draft);
    stepFlush.set(key, flush); timer = setTimeout(flush, 600);
  };
  const draft = { state, cancel, flush, schedule };
  return draft;
}
function screenIntake(id) {
  // PC 연결 방식: 새 현장 접수만(PC 원본은 고치지 않음 — 바꿀 내용은 지점 화면의 [정정 요청])
  const onField = !isSolo();
  // PC 연결 방식에서 고치기는 '아직 보내지 않은 이 휴대폰의 접수'만(보낸 뒤에는 정정 요청)
  const fieldEdit = id && onField ? fieldEditable(id) : null;
  const editing = id ? (onField ? (fieldEdit && fieldEdit.stop) : findStop(id)) : null;
  if (id && !editing) { go('#/home'); return h('main'); }
  const table = (onField && feeTable()) || BUILTIN_FEES;
  // 현장 기록(품목 결과·스티커 확인·품목 사진 등)이 가리키는 품목 줄은 고치기에서 빼거나 바꾸지 못한다
  const locked = new Set(editing ? st.events.filter((e) => e.request_id === id && e.body && e.body.line_id).map((e) => e.body.line_id) : []);
  if (!ui.form || ui.form.id !== (id || null)) {
    const src = editing || st.intakeDraft || {};
    ui.form = { id: id || null, name: src.name || '', phone: src.phone || '', address: src.address || '', dong: src.dong || '', spot_note: src.spot_note || '', memo: src.memo || '',
      lat: src.addrLat ?? null, lon: src.addrLon ?? null, zip: src.zip || '', dongCands: [],
      planned_date: src.planned_date || (onField ? pcWorkDate() : todayKST(1)), eta: src.eta || '', channel: src.channel || '현장', items: JSON.parse(JSON.stringify(src.items || [])), reason: '', dupAck: false,
      pick: { q: '', sel: null, qty: 1 } };
  }
  const f = ui.form;
  const ownerState = st;
  const currentForm = () => st === ownerState && ui.form === f && isUnlocked();
  const draft = editing ? null : draftFor(f, ownerState);
  const saveDraft = () => draft?.schedule();
  if (f.picking) return pickScreen({ items: f.items, state: f.pick, table, locked, onChange: saveDraft,
    onDone: () => { f.picking = false; render(); setTimeout(() => { const el = document.getElementById('intake-items'); if (el) el.scrollIntoView({ block: 'start' }); }, 0); } });
  const field = (key, label, attrs = {}) => {
    const input = h(attrs.multiline ? 'textarea' : 'input', { ...attrs, multiline: null, class: 'search', value: attrs.multiline ? null : f[key], rows: attrs.multiline ? 2 : null });
    if (attrs.multiline) input.value = f[key];
    input.addEventListener('input', () => { f[key] = input.value; f.dupAck = false; if (key === 'dong') f.dongAuto = false; if (key === 'address' || key === 'dong') { f.addrAck = false; addrCheck(); } saveDraft(); });
    if (key === 'address') input.addEventListener('blur', () => addrCheck(true));
    return h('label', { class: 'fld' }, h('span', {}, label), input);
  };
  // 배출 주소 확인·행정동 추천(휴대폰 안 자료로만): 형식·관할·전화번호 섞임, 같은 도로의 이전 접수로 동 추천, 동 이름 오타
  const addrBox = h('div', { class: 'addr-check', 'aria-live': 'polite' });
  const dongBox = h('div', { class: 'dong-check', 'aria-live': 'polite' });
  const history = () => allStops().filter((x) => x.dong && x.address && !x.pii_purged).map((x) => ({ address: x.address, dong: x.dong }));
  const addrIssues = () => {
    const c = J.checkAddress(f.address);
    const out = [...c.warnings];
    if (f.dong && !J.isKnownDong(f.dong, knownDongs())) out.push(`'${f.dong}'은(는) 동구 행정동 목록에 없는 이름입니다. 오타인지 확인하세요.`);
    return out;
  };
  const addrCheck = (final = false) => {
    if (!f.address.trim()) { addrBox.replaceChildren(); dongBox.replaceChildren(); return; }
    const c = J.checkAddress(f.address);
    addrBox.replaceChildren(...((final || c.warnings.length && f.address.length > 6) ? c.warnings.map((w) => h('p', { class: 'small warnp' }, '⚠ ' + w)) : []));
    const g = J.guessDong(f.address, history(), knownDongs());
    const dongIn = document.querySelector('#intake-dong');
    if (g.dong && (!f.dong || f.dongAuto) && f.dong !== g.dong) {
      f.dong = g.dong; f.dongAuto = true; if (dongIn) dongIn.value = g.dong; saveDraft();
    }
    const kids = [];
    if (g.dong && f.dong === g.dong) kids.push(h('p', { class: 'small ok-note' }, `✓ 동: ${g.dong} (${g.reason})`));
    else if (g.candidates.length > 1 || (g.dong && f.dong !== g.dong)) {
      kids.push(h('div', { class: 'dong-cands', role: 'group', 'aria-label': '동 추천' }, h('span', { class: 'small' }, `동 후보${g.reason ? ` (${g.reason})` : ''}: `),
        ...(g.candidates.length ? g.candidates : [g.dong]).map((x) => btn(x, () => { f.dong = x; f.dongAuto = false; f.addrAck = false; if (dongIn) dongIn.value = x; saveDraft(); addrCheck(); }, 'opt chipbtn' + (f.dong === x ? ' on' : '')))));
    } else if (!f.dong && final) kids.push(h('p', { class: 'small muted' }, onField ? '동을 고르지 않으면 PC가 주소로 정합니다.' : '동을 모르면 비워 두어도 됩니다.'));
    if (f.dong && !J.isKnownDong(f.dong, knownDongs())) kids.push(h('p', { class: 'small warnp' }, `⚠ '${f.dong}'은(는) 동구 행정동 목록에 없습니다. 오타인지 확인하세요.`));
    dongBox.replaceChildren(...kids);
  };
  const itemsBox = h('div', {});
  const totalBox = h('p', { class: 'small' });
  const drawItems = () => {
    itemsBox.replaceChildren(...(f.items.length ? f.items.map((it, i) => h('div', { class: 'line orig' },
      h('div', { class: 'lhead' }, h('div', { class: 'lname' }, h('strong', {}, `${it.name} ${it.spec}`)),
        it.price_status === 'CONFIRMED' ? h('span', { class: 'amt' }, won(it.unit_price * it.qty)) : h('span', { class: 'amt pending' }, '확인 대기')),
      locked.has(it.line_id) ? h('em', { class: 'tag' }, '현장 기록 있음') : null,
      h('div', { class: 'price' }, it.price_status === 'CONFIRMED' ? `${won(it.unit_price)} / ${it.unit_label}` : it.unit_price != null ? `금액 확인 대기 (원문 ${won(it.unit_price)} / ${it.unit_label})` : '금액 확인 대기'),
      h('div', { class: 'row' }, h('span', {}, '수량'), locked.has(it.line_id) ? h('span', { class: 'val' }, it.qty) :
        stepper(it.qty, (n) => { it.qty = n; it.amount = it.price_status === 'CONFIRMED' ? it.unit_price * n : null; drawItems(); saveDraft(); }, 1)),
      locked.has(it.line_id) ? null : btn('삭제', () => { f.items.splice(i, 1); drawItems(); saveDraft(); }, 'ghost link'))) :
      [h('p', { class: 'small' }, '[+ 품목 담기]로 버릴 물건을 담으세요.')]));
    const pend = f.items.filter((x) => x.price_status !== 'CONFIRMED').length;
    const sum = f.items.filter((x) => x.price_status === 'CONFIRMED').reduce((a, x) => a + x.amount, 0);
    totalBox.textContent = `접수 금액: ${won(sum)}${pend ? ` + 확인 대기 ${pend}건 (최종 금액 미확정)` : ''}`;
  };
  drawItems();
  const msg = h('p', { class: 'msg' });
  const dupBox = h('div', {});
  const saveLabel = fieldEdit ? '고친 내용 저장' : editing ? '수정 저장' : onField ? '접수 저장' : '접수 등록';
  const save = btn(saveLabel, async () => {
    if (!currentForm()) return;
    const phone = digits(f.phone);
    if (!f.name.trim()) return (msg.textContent = '이름을 적어 주세요.');
    if (!/^0\d{8,10}$/.test(phone)) return (msg.textContent = '연락처를 확인해 주세요. (0으로 시작하는 9~11자리)');
    if (!f.address.trim()) return (msg.textContent = '배출 주소를 적어 주세요.');
    if (!f.planned_date) return (msg.textContent = '수거 예정일을 골라 주세요.');
    if (!f.items.length) return (msg.textContent = '버릴 물건을 하나 이상 추가해 주세요.');
    // 주소 확인: 걱정되는 점이 있으면 한 번 보여 주고, 맞으면 한 번 더 눌러 저장(막지 않음)
    const issues = addrIssues();
    if (issues.length && !f.addrAck) {
      f.addrAck = true; addrCheck(true);
      msg.textContent = '주소를 확인해 주세요: ' + issues.join(' / ') + ` — 맞으면 [${saveLabel}]을 한 번 더 누르세요.`;
      document.querySelector('label.fld input[placeholder^="예: 광주 동구"]')?.scrollIntoView({ block: 'center' });
      return;
    }
    if (editing && !fieldEdit && !f.reason.trim()) return (msg.textContent = '수정 사유를 적어 주세요.');
    if (!editing && !f.dupAck) {
      const dups = allStops().filter((r) => r.status !== 'CANCELLED' && !r.pii_purged && deriveStop(r, st.events).progress !== 'DONE' && (digits(r.phone) === phone || normAddr(r.address) === normAddr(f.address)));
      if (dups.length) {
        f.dupAck = true;
        dupBox.replaceChildren(h('section', { class: 'card notice' },
          h('p', {}, `비슷한 접수가 ${dups.length}건 있습니다. ` + (onField ? '같은 건이면 새로 받지 말고 그 지점에서 품목 추가·정정 요청을 하세요.' : '같은 건이면 등록하지 말고 기존 접수를 수정하세요.')),
          h('ul', { class: 'small list' }, ...dups.map((r) => h('li', {}, `${r.receipt_no} · ${r.name} · ${fmtPhone(r.phone)} · ${r.address}` + (r.planned_date ? ` · ${r.planned_date}` : '')))),
          h('p', {}, `별개 접수가 맞으면 [${onField ? '접수 저장' : '접수 등록'}]을 한 번 더 누르세요. (자동으로 합치지 않습니다)`)));
        dupBox.scrollIntoView({ block: 'center' });
        return;
      }
    }
    draft?.cancel();
    const now = new Date().toISOString();
    const dong = (f.dong || '').trim().slice(0, 20);
    if (dong) st.recentDongs = [dong, ...(st.recentDongs || []).filter((x) => x !== dong)].slice(0, 12);
    const data = { name: f.name.trim(), phone, address: f.address.trim(), dong, spot_note: f.spot_note.trim(), memo: f.memo.trim(), planned_date: f.planned_date, eta: f.eta, items: f.items,
      ...(f.lat != null && f.lon != null ? { lat: f.lat, lon: f.lon, geo_source: 'ADDR' } : {}) };
    if (fieldEdit) {
      // 보내기 전의 휴대폰 접수 고치기: PC는 아직 모르므로 보낼 기록(FIELD_INTAKE)과 목록을 함께 바꾼다
      const ch = FIELD_CHANNELS.includes(f.channel) ? f.channel : (editing.channel || '현장');
      const moved = editing.address !== data.address && data.lat == null;  // 주소를 바꿨는데 새 좌표가 없으면 옛 위치를 버린다
      Object.assign(editing, data, { channel: ch, appt: data.eta ? [data.eta, null] : null, updated_at: now });
      const body0 = { ...fieldEdit.ev.body };
      if (moved) { for (const k of ['lat', 'lon', 'geo_source']) { delete editing[k]; delete body0[k]; } }
      fieldEdit.ev.body = { ...body0, ...withWorker({}), channel: ch, ...data,
        items: data.items.map(({ line_id, fee_rate_id, name, spec, unit, unit_label, qty }) => ({ line_id, fee_rate_id, name, spec, unit, unit_label, qty })) };
      await commit(); if (!currentForm()) return; ui.form = null; toast('접수를 고쳤습니다(보내기 전)'); go(`#/stop/${id}`);
      return;
    }
    if (onField) {
      // 현장 접수: 임시번호로 이 휴대폰 목록에 넣고, 결과 보내기 때 PC로 보내 PC 원본 접수가 된다(금액은 PC 표로 다시 계산)
      const rd = todayKST();
      const n = (st.receiptCounter[rd] || 0) + 1; st.receiptCounter[rd] = n;
      const ch = FIELD_CHANNELS.includes(f.channel) ? f.channel : '현장';
      const temp = `${ch}-${rd.slice(5).replace('-', '')}-${n}`;
      const seq = allStops().reduce((m, x) => Math.max(m, Number(x.seq) || 0), 0) + 1;
      const fstop = { request_id: crypto.randomUUID(), receipt_no: temp, field: true, channel: ch, req_ver: 1, assign_ver: 0, seq, ...data,
        appt: data.eta ? [data.eta, null] : null, status: 'ACTIVE', received_at: now };
      st.fieldStops = [...(st.fieldStops || []), fstop]; st.intakeDraft = null;
      await addEvent(fstop, 'FIELD_INTAKE', { temp_no: temp, channel: ch, ...data,
        items: data.items.map(({ line_id, fee_rate_id, name, spec, unit, unit_label, qty }) => ({ line_id, fee_rate_id, name, spec, unit, unit_label, qty })) });
      if (!currentForm()) return;
      ui.form = null; toast(`${temp} ${ch} 접수를 저장했습니다. 결과 보내기 때 PC로 갑니다`);
      // 지금 그 자리에서 받은 현장 접수는 바로 처리할 수 있게 그 지점으로, 전화·방문 접수는 목록으로
      go(ch === '현장' && data.planned_date === pcWorkDate() ? `#/stop/${fstop.request_id}` : '#/home');
      return;
    }
    if (editing) {
      const { versions = [], ...prev } = editing;
      versions.push({ req_ver: editing.req_ver, at: now, reason: f.reason.trim(), snapshot: prev });
      const moved = editing.planned_date !== data.planned_date;
      Object.assign(editing, data, { req_ver: editing.req_ver + 1, versions, updated_at: now });
      if (moved) editing.seq = nextSeq(data.planned_date, editing.request_id);
      await commit(); if (!currentForm()) return; ui.form = null; toast('수정했습니다'); go(`#/stop/${id}`);
    } else {
      const rd = todayKST();
      const n = (st.receiptCounter[rd] || 0) + 1; st.receiptCounter[rd] = n;
      const req = { request_id: crypto.randomUUID(), receipt_no: `DG-${rd.replace(/-/g, '')}-${String(n).padStart(4, '0')}`, req_ver: 1, assign_ver: 1,
        ...data, seq: nextSeq(data.planned_date), status: 'ACTIVE', received_at: now, updated_at: now, versions: [] };
      st.requests.push(req); st.intakeDraft = null;
      await commit(); if (!currentForm()) return; ui.form = null; toast(`${req.receipt_no} 등록했습니다`); go('#/home');
    }
  }, 'primary huge');
  const view = h('main', {},
    h('header', { class: 'bar' }, btn('← 취소', async () => { await draft?.flush(); if (!currentForm()) return; ui.form = null; go(editing ? `#/stop/${id}` : '#/home'); }, 'ghost'), h('h1', { class: 'bar-title' }, fieldEdit ? `접수 고치기 ${editing.receipt_no}` : editing ? `접수 수정 ${editing.receipt_no}` : onField ? '접수 등록 (PC로 보냄)' : '전화 접수 등록')),
    onField && !registeredNow ? h('section', { class: 'card notice warn' }, h('p', {}, '이 휴대폰은 아직 PC에 등록되지 않았습니다. 접수는 이 휴대폰에 저장되지만, 기기 등록을 마쳐야 [결과 보내기]로 PC에 보낼 수 있습니다.'),
      btn('기기 등록', () => go('#/register'), 'ghost')) : null,
    onField ? h('section', { class: 'card notice' }, h('p', {}, '수거 중 주민이 새로 요청했거나 전화로 받은 건을 접수합니다. 결과 보내기 때 PC로 가서 PC 원본 접수가 되고, 사무실이 주소·품목·금액을 확인합니다.'),
      h('p', { class: 'small' }, '금액은 PC 수수료표로 다시 계산됩니다. 표에 없는 품목은 "확인 대기"로 갑니다. 이미 목록에 있는 집의 품목이 늘었으면 새로 받지 말고 그 지점에서 [품목 추가]를 하세요.')) : null,
    !editing && st.intakeDraft ? h('p', { class: 'small' }, '작성 중이던 내용을 불러왔습니다. ',
      btn('지우고 새로 쓰기', async () => { draft?.cancel(); if (!currentForm()) return; ownerState.intakeDraft = null; await commit(); if (!currentForm()) return; ui.form = null; render(); }, 'ghost')) : null,
    h('section', { class: 'card' },
      onField ? h('div', {}, h('div', { class: 'fld-label' }, '접수 경로'),
        h('div', { class: 'grid3', role: 'group', 'aria-label': '접수 경로' }, ...FIELD_CHANNELS.map((c) =>
          btn(c, () => { f.channel = c; saveDraft(); render(); }, f.channel === c ? 'opt on' : 'opt', { 'aria-pressed': f.channel === c ? 'true' : 'false' })))) : null,
      field('name', '이름', { autocomplete: 'off' }),
      field('phone', '연락처', { type: 'tel', inputmode: 'tel', autocomplete: 'off', placeholder: '010-0000-0000' }),
      addrSearch({
        forbid: () => [f.name, f.phone],
        onPick: (it, ll) => {
          f.address = it.road; f.zip = it.zip; f.lat = ll ? ll.lat : null; f.lon = ll ? ll.lon : null; f.dupAck = false;
          const sg = J.suggestDong(it, knownDongs());
          if (sg.dong) f.dong = sg.dong;
          f.dongCands = sg.dong ? [] : sg.candidates;
          saveDraft(); render();
          toast(ll ? '주소와 위치(건물 출입구)를 채웠습니다' : '주소를 채웠습니다. 위치는 접수 뒤 지도에서 지정하세요');
        } }),
      field('address', '배출 주소', { autocomplete: 'off', placeholder: '예: 광주 동구 예시로 12 / 대인동 34-5' }), addrBox,
      f.lat != null ? h('p', { class: 'small addr-geo' }, `위치: 주소 검색 좌표(건물 출입구)${f.zip ? ` · 우편번호 ${f.zip}` : ''}. 배출 위치가 다르면 접수 뒤 [위치 고치기]로 맞추세요.`) : null,
      field('dong', '동 (행정동, 선택)', { autocomplete: 'off', list: 'dong-list', placeholder: '예: 충장동', id: 'intake-dong' }), dongBox,
      f.dongCands.length ? h('div', { class: 'dong-cands', role: 'group', 'aria-label': '동 제안' }, h('span', { class: 'small' }, '주소 검색 결과의 동 후보: '),
        ...f.dongCands.map((x) => btn(x, () => { f.dong = x; f.dongCands = []; saveDraft(); render(); }, 'opt chipbtn'))) : null,
      h('datalist', { id: 'dong-list' }, ...knownDongs().map((x) => h('option', { value: x }))),
      field('spot_note', '상세 배출 위치', { autocomplete: 'off', placeholder: '예: 101동 분리수거장 옆' }),
      field('memo', '요청 메모', { multiline: true }),
      h('div', { class: 'grid2' }, field('planned_date', '수거 예정일', { type: 'date' }), field('eta', '방문 시각(선택)', { type: 'time' }))),
    h('section', { class: 'card', id: 'intake-items' }, h('h2', {}, '버릴 물건'), itemsBox, totalBox,
      btn(f.items.length ? '+ 품목 더 담기' : '+ 품목 담기', () => { f.picking = true; render(); }, 'primary big')),
    editing && !fieldEdit ? h('section', { class: 'card' }, field('reason', '수정 사유 (필수)', { autocomplete: 'off' })) : null,
    dupBox, h('div', { class: 'cta' }, msg, save));
  if (f.address) setTimeout(() => addrCheck(true), 0);
  return view;
}
const knownDongs = () => [...new Set([...(st.recentDongs || []), ...allStops().map((r) => dongOf(r)).filter(Boolean)])];

// ------------------------------------------------------------ 주소 검색 (행정안전부 도로명주소 API, juso.js)
function addrSearch({ onPick, forbid = () => [] }) {
  const box = h('div', { class: 'addr-search' }, h('div', { class: 'fld-label' }, '주소 검색'));
  if (addrRoute() === 'pc') {
    box.append(h('p', { class: 'small addr-status' }, '주소 검색 경로가 "PC 경유"입니다. PC 연결 방식에서는 PC 접수 화면의 [주소 찾기](PC의 주소 자료 + PC가 대신 묻는 행안부 검색)로 주소·위치를 정해 목록으로 보냅니다. 여기서는 직접 적거나 지도에서 위치를 지정하세요.'));
    return box;
  }
  const input = h('input', { type: 'search', class: 'search', placeholder: '도로명·건물명·지번 (예: 서남로 1)', 'aria-label': '주소 검색어', autocomplete: 'off', enterkeyhint: 'search', disabled: !st.jusoKey });
  const status = h('p', { class: 'small addr-status', role: 'status' }, st.jusoKey ? '' : '승인키를 설정에서 입력해 주세요 (설정 → 주소 검색). 지금은 주소를 직접 적고 지도에서 위치를 지정하세요.');
  const list = h('ul', { class: 'addr-results' });
  let timer = null, seq = 0;
  const leaks = (q) => {
    const sq = q.replace(/\s+/g, ''), dq = q.replace(/\D/g, '');
    // 지금 칸의 이름·연락처뿐 아니라 이 휴대폰에 있는 모든 주민 이름도 막는다(PC와 같게)
    const names = allStops().filter((r) => !r.pii_purged).map((r) => r.name);
    return [...forbid(), ...names].some((x) => { const v = String(x || ''); const dv = v.replace(/\D/g, ''); return (dv.length >= 8 && dq.includes(dv)) || (!dv && v.replace(/\s+/g, '').length >= 2 && sq.includes(v.replace(/\s+/g, ''))); });
  };
  const run = async () => {
    const q = J.cleanKeyword(input.value), my = ++seq;
    if (q.length < 2) return;
    if (J.phoneLike(q)) { list.replaceChildren(); status.textContent = '검색어에 전화번호 같은 숫자가 있어 보내지 않았습니다. 주소 낱말만 적어 주세요.'; return; }
    if (leaks(q)) { list.replaceChildren(); status.textContent = '검색어에 주민 이름·연락처가 들어 있어 보내지 않았습니다. 주소 낱말만 적어 주세요.'; return; }
    status.textContent = '찾는 중…';
    try {
      const r = await J.searchAddress({ key: st.jusoKey, keyword: q });
      if (my !== seq) return;
      status.textContent = r.items.length ? `${r.total}건${r.total > r.items.length ? ` 중 ${r.items.length}건` : ''} — 고르면 주소와 위치를 채웁니다` : '찾은 주소가 없습니다. 도로명과 건물번호로 다시 찾아 보세요.';
      list.replaceChildren(...r.items.map((it) => h('li', {}, h('button', { type: 'button', class: 'btn addr-item', onclick: async () => {
        status.textContent = '위치를 받는 중…';
        let ll = null;
        if (st.jusoCoordKey) {
          try { ll = await J.coordOf({ key: st.jusoCoordKey, item: it }); } catch (e) { toast(e.message || '위치를 받지 못했습니다', 'bad'); }
        }
        list.replaceChildren(); input.value = ''; status.textContent = st.jusoCoordKey ? '' : '좌표 승인키가 없어 위치는 채우지 않았습니다(설정 → 주소 검색).';
        onPick(it, ll);
      } },
      h('span', { class: 'ai' }, h('strong', {}, it.road), it.jibun ? h('span', {}, '지번 ' + it.jibun) : null,
        (it.bdNm || it.zip) ? h('span', { class: 'ai-meta' }, [it.bdNm, it.zip ? '우편번호 ' + it.zip : ''].filter(Boolean).join(' · ')) : null)))));
    } catch (e) {
      if (my !== seq) return;
      list.replaceChildren(); status.textContent = e.message || '주소 검색에 실패했습니다. 직접 적어 주세요.';
    }
  };
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { seq++; list.replaceChildren(); status.textContent = q ? '두 글자 이상 적어 주세요.' : ''; return; }
    timer = setTimeout(run, 300);
  });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); run(); } });
  box.append(input, status, list, h('p', { class: 'small' }, '검색어만 행정안전부 주소 서버(business.juso.go.kr)로 보냅니다. 이름·연락처는 보내지 않습니다.'));
  return box;
}

export {
  FIELD_CHANNELS, addrSearch, knownDongs, screenIntake,
};
