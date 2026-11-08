// 여러 화면이 함께 쓰는 부품: 품목 고르기·지도·웹캠 QR 읽기·파기 이력 표.
import * as M from '/web/map.js';
import { BUILTIN_FEES } from '/web/fees-data.js';
import { searchFees } from '/web/fees.js';
import { won } from '/web/fees.js';
import { FEES, api, btn, grid, h, kst, liveMaps, toast } from './core.js';

let pickerSequence = 0;
function itemPicker(onAdd, { usagePath = 'fee-usage' } = {}) {
  const maxQty = 9999, quantityId = `item-picker-qty-${++pickerSequence}`;
  const st = { q: '', category: 'quick', group: null, sel: null };
  const q = h('input', { type: 'search', placeholder: '품목명·규격 검색 (예: 소파, 장농, ㅈㄹ)', 'aria-label': '품목명·규격 검색', autocomplete: 'off' });
  const heading = h('p', { class: 'wp-heading' }), catalog = h('div', { class: 'wp-catalog' });
  const list = h('div', { class: 'results wp-specs', 'aria-label': '물건 규격' });
  const qty = h('input', { id: quantityId, type: 'number', min: 1, max: maxQty, step: 1, value: 1, inputmode: 'numeric', 'aria-label': '추가 수량' });
  const add = btn('품목 추가', null, 'primary', { disabled: true });
  const selectedCopy = h('div', { class: 'wp-choice' }), subtotal = h('strong', { class: 'wp-subtotal' });
  const message = h('p', { class: 'wp-message', role: 'alert' }), status = h('p', { class: 'wp-status', role: 'status' });
  let usage = [], root;
  const key = (r) => JSON.stringify([r.category, r.name]);
  const validQty = (input) => Number.isInteger(+input.value) && +input.value > 0 && +input.value <= maxQty;
  const confirmed = (r) => r.status === 'CONFIRMED' && r.price != null;
  const minus = btn('−', () => step(-1), '', { 'aria-label': '수량 줄이기' });
  const plus = btn('+', () => step(1), '', { 'aria-label': '수량 늘리기' });
  const selected = h('section', { class: 'wp-selected', 'aria-label': '선택한 물건' }, selectedCopy,
    h('div', { class: 'wp-add-row' }, h('div', { class: 'wp-quantity' }, h('label', { for: quantityId }, '수량'), h('span', { class: 'wp-stepper' }, minus, qty, plus)),
      h('div', { class: 'wp-amount' }, h('small', {}, '이 품목 금액'), subtotal), add), message);
  const back = btn('← 다른 물건 선택', () => { st.group = null; st.sel = null; draw(); q.focus(); }, 'small');
  const categories = [['quick', '빠른 선택'], ['all', '전체'], ...[...new Set(FEES().rows.map((r) => r.category))].map((c) => [c, c])];
  const categoryButtons = categories.map(([value, label]) => btn(label, () => {
    st.category = value; st.group = null; st.sel = null; st.q = ''; q.value = ''; status.textContent = ''; draw();
  }, 'wp-category', { 'aria-pressed': value === st.category ? 'true' : 'false' }));

  function groups() {
    const map = new Map();
    for (const r of FEES().rows) { const k = key(r); if (!map.has(k)) map.set(k, []); map.get(k).push(r); }
    return map;
  }
  function showChoice() {
    const row = FEES().rows.find((r) => r.id === st.sel);
    add.disabled = !row; qty.disabled = !row; plus.disabled = !row || +qty.value >= maxQty; minus.disabled = !row || !Number.isInteger(+qty.value) || +qty.value <= 1;
    selected.classList.toggle('has-choice', !!row);
    if (!row) {
      selectedCopy.replaceChildren(h('strong', {}, '규격을 선택하세요'), h('span', {}, '물건과 규격을 확인한 뒤 수량을 정해 추가합니다.'));
      subtotal.textContent = '—';
      return;
    }
    selectedCopy.replaceChildren(h('strong', {}, row.name), h('span', {}, `${row.spec || '단일 규격'} · ${confirmed(row) ? won(row.price) : '금액 확인 대기'} / ${row.unit_label}`));
    subtotal.textContent = !validQty(qty) ? '수량 확인' : confirmed(row) ? won(row.price * +qty.value) : '금액 확인 대기';
  }
  function choose(row) {
    st.sel = row.id; draw(); qty.focus({ preventScroll: true }); selected.scrollIntoView({ block: 'nearest' });
  }
  function draw() {
    categoryButtons.forEach((button, i) => button.setAttribute('aria-pressed', String(categories[i][0] === st.category)));
    message.textContent = ''; qty.removeAttribute('aria-invalid');
    const allGroups = groups();
    const rows = st.q ? searchFees(FEES(), st.q) : st.group ? allGroups.get(st.group) || [] : [];
    if (st.sel && !rows.some((r) => r.id === st.sel)) st.sel = null;
    const browsing = !st.q && !st.group;
    catalog.hidden = !browsing; list.hidden = browsing || !rows.length; back.hidden = !st.group;
    if (browsing) {
      let keys = [...allGroups.keys()].filter((k) => st.category === 'all' || st.category === 'quick' || allGroups.get(k)[0].category === st.category);
      const examples = new Map(['소파', '장롱', '매트리스', '의자', '책상', '냉장고', '세탁기', '식탁']
        .flatMap((name) => searchFees(FEES(), name).slice(0, 1).map((row) => [key(row), row.spec])));
      if (st.category === 'quick') {
        const popular = usage.map((id) => FEES().rows.find((r) => r.id === id)).filter(Boolean).map(key);
        keys = [...new Set([...popular, ...examples.keys(), ...keys])].slice(0, 8);
      }
      heading.textContent = st.category === 'quick' ? '물건을 고르면 규격을 비교할 수 있습니다' : `${st.category === 'all' ? '전체 분류' : st.category} · 물건 ${keys.length}종`;
      catalog.replaceChildren(...keys.map((k) => {
        const variants = allGroups.get(k), first = variants[0], prices = variants.filter(confirmed).map((r) => r.price);
        const price = !prices.length ? '금액 확인 대기' : Math.min(...prices) === Math.max(...prices) ? won(prices[0]) : `${won(Math.min(...prices))} ~ ${won(Math.max(...prices))}`;
        return btn(h('strong', {}, first.name), () => {
          st.group = k; st.sel = variants.length === 1 ? first.id : null; draw();
          if (variants.length === 1) { qty.focus(); } else { list.querySelector('button')?.focus(); }
        }, 'wp-object', { 'aria-label': `${first.name} · 규격 ${variants.length}개`, title: `${variants.length}개 규격 · ${price}` });
      }));
      [...catalog.children].forEach((button, i) => {
        const variants = allGroups.get(keys[i]);
        const example = examples.get(keys[i]) || variants[0].spec;
        button.append(h('span', { class: 'wp-object-meta' }, example ? `${example} 등 · ` : '', `규격 ${variants.length}개`, variants.some((r) => !confirmed(r)) ? ' · 확인 대기 포함' : ''));
      });
    } else {
      heading.textContent = st.q ? (rows.length ? `검색 결과 · 규격 ${rows.length}개` : '맞는 물건이 없습니다. 다른 이름으로 찾거나 아래에서 확인 대기로 추가하세요.')
        : `${rows[0]?.name || ''} · 규격 ${rows.length}개 중 선택`;
    }
    list.replaceChildren(...rows.map((r) => btn(h('span', { class: 'wp-spec-copy' }, h('strong', {}, r.name), h('span', {}, r.spec || '단일 규격')),
      () => choose(r), st.sel === r.id ? 'on' : '', { 'data-fee-id': r.id, 'aria-pressed': String(st.sel === r.id) })));
    [...list.children].forEach((button, i) => button.append(h('span', { class: 'wp-price' },
      h('span', { class: 'p' }, `${confirmed(rows[i]) ? won(rows[i].price) : '금액 확인 대기'} / ${rows[i].unit_label}`),
      h('small', {}, st.sel === rows[i].id ? '✓ 선택됨' : confirmed(rows[i]) ? '선택' : '규격 확인 필요'))));
    showChoice();
  }
  function step(delta) {
    qty.value = Math.min(maxQty, Math.max(1, (Number.isInteger(+qty.value) ? +qty.value : 1) + delta));
    qty.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function checkQuantity(input) {
    if (validQty(input)) return true;
    input.setAttribute('aria-invalid', 'true'); message.textContent = `수량은 1~${maxQty} 사이의 정수로 입력하세요.`; input.focus();
    return false;
  }
  function finished(name, count, unit, pending = false) {
    st.sel = null; st.group = null; st.q = ''; q.value = ''; qty.value = 1;
    if (st.category === 'all') st.category = 'quick';
    draw(); status.textContent = `${name} ${count}${unit} 추가했습니다.${pending ? ' 금액은 확인 대기입니다.' : ''}`;
  }
  q.addEventListener('input', () => {
    st.q = q.value.trim(); st.group = null; st.category = 'all';
    const rows = searchFees(FEES(), st.q); st.sel = rows.length === 1 ? rows[0].id : null;
    status.textContent = ''; draw();
  });
  q.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229 || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'ArrowDown' || e.key === 'Enter' && !st.sel) {
      const button = list.querySelector('button') || catalog.querySelector('button');
      if (button) { e.preventDefault(); button.focus(); }
    } else if (e.key === 'Enter' && st.sel) { e.preventDefault(); add.click(); }
  });
  list.addEventListener('keydown', (e) => {
    const buttons = [...list.querySelectorAll('button')], i = buttons.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' && i >= 0 && buttons[i + 1]) { e.preventDefault(); buttons[i + 1].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (i > 0 ? buttons[i - 1] : q).focus(); }
  });
  qty.addEventListener('input', () => { qty.removeAttribute('aria-invalid'); message.textContent = ''; showChoice(); });
  qty.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing && !e.ctrlKey && !e.metaKey && !e.altKey && st.sel) { e.preventDefault(); add.click(); } });
  add.addEventListener('click', () => {
    const row = FEES().rows.find((r) => r.id === st.sel);
    if (!row || !checkQuantity(qty)) return;
    const count = +qty.value;
    onAdd({ fee_rate_id: row.id, qty: count, _row: row });
    finished(`${row.name} ${row.spec}`, count, row.unit_label, !confirmed(row)); q.focus();
  });
  const fname = h('input', { placeholder: '목록에 없는 품목 이름' });
  const funit = h('select', { 'aria-label': '확인 대기 부과 단위' }, ...[['EA', '개'], ['PIECE', '쪽'], ['SET', '세트'], ['L20', '20ℓ당']].map(([v, t]) => h('option', { value: v }, t)));
  const fqty = h('input', { type: 'number', min: 1, max: maxQty, step: 1, value: 1, 'aria-label': '확인 대기 수량' });
  const fadd = btn('확인 대기로 추가', () => {
    if (!fname.value.trim()) { toast('품목 이름을 적어 주세요', true); fname.focus(); return; }
    if (!checkQuantity(fqty)) return;
    const name = fname.value.trim(), count = +fqty.value, unit = funit.selectedOptions[0].textContent;
    onAdd({ name, unit: funit.value, unit_label: unit, qty: count });
    fname.value = ''; fqty.value = 1; finished(name, count, unit, true); fname.focus();
  });
  fqty.addEventListener('input', () => { fqty.removeAttribute('aria-invalid'); message.textContent = ''; });
  root = h('section', { class: 'waste-picker', 'aria-label': '버릴 물건 선택' },
    h('div', { class: 'wp-search row' }, q), h('div', { class: 'wp-categories', 'aria-label': '물건 분류' }, ...categoryButtons),
    h('div', { class: 'wp-browser' }, h('div', { class: 'wp-browser-head' }, heading, back), catalog, list), selected, status,
    h('p', { class: 'wp-help' }, '개·쪽·20ℓ 등 부과 단위를 확인하세요. 규격이 여러 개면 고른 뒤 추가합니다.'),
    h('details', { class: 'free wp-free' }, h('summary', {}, '목록에 없는 품목 넣기 (금액 확인 대기)'),
      h('div', { class: 'wp-free-fields' }, fname, funit, fqty, fadd)),
    h('details', { class: 'wp-source' }, h('summary', {}, '수수료표·확인 대기 안내'),
      h('p', { class: 'muted small' }, `${BUILTIN_FEES.label} (${FEES().version_id}) · 표에 없는 품목은 0원이 아니라 금액 확인 대기로 남습니다. 유사 품목 적용은 담당자가 확인해 정합니다.`)));
  draw();
  if (usagePath) api(usagePath).then((value) => {
    usage = Array.isArray(value) ? value : [];
    if (st.category === 'quick' && !st.q && !st.group && !root.contains(document.activeElement)) draw();
  }).catch(() => {});
  return root;
}

// ------------------------------------------------------------ 지도 도우미
async function mapIn(box, opts) {
  const m = await M.createMap(box, opts);
  liveMaps.push(m.map);
  // 지도 조작 단추·출처 링크는 Tab 순서에서 빼서 양식 입력 흐름을 끊지 않게 (마우스로는 그대로)
  const untab = () => box.querySelectorAll('.maplibregl-ctrl button, .maplibregl-ctrl a, .maplibregl-canvas').forEach((el) => { el.tabIndex = -1; });
  untab(); setTimeout(untab, 500); setTimeout(untab, 2000);
  return m;
}

// ------------------------------------------------------------ 접수 목록
let jsqrReady = null;
function loadJsQR() {
  return (jsqrReady ||= new Promise((res, rej) => { const sc = document.createElement('script'); sc.src = '/web/vendor/jsQR.js'; sc.onload = res; sc.onerror = rej; document.head.append(sc); }));
}
// 웹캠으로 QR 한 장 읽기: accept(text)가 참인 QR이 보이면 끝. video._stop()으로 멈춤
async function webcamReadOnce(video, accept) {
  await loadJsQR();
  const stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
  video.hidden = false; video.srcObject = stream; await video.play();
  const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d', { willReadFrequently: true });
  return new Promise((res, rej) => {
    let done = false;
    const stop = () => { done = true; stream.getTracks().forEach((t) => t.stop()); video.hidden = true; video._stop = null; };
    video._stop = () => { stop(); rej(new Error('읽기를 멈췄습니다.')); };
    const loop = () => {
      if (done) return;
      if (!video.isConnected) { stop(); rej(new Error('화면을 떠나 읽기를 멈췄습니다.')); return; }
      if (video.videoWidth) {
        canvas.width = video.videoWidth; canvas.height = video.videoHeight; ctx.drawImage(video, 0, 0);
        const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const code = window.jsQR(img.data, img.width, img.height, { inversionAttempts: 'dontInvert' });
        if (code && code.data && accept(code.data)) { stop(); res(code.data); return; }
      }
      setTimeout(loop, 80);
    };
    loop();
  });
}
const purgeLogTable = (log) => grid(['일시', '건수', '작업자', '실행', '기준'], log.map((x) => [kst(x.at), x.count, x.actor || '', ({ MANUAL: '화면', CLOSE_APPROVE: '마감 확인', CLI: '명령' })[x.trigger] || x.trigger,
  `보유 ${x.criteria.retention_years}년 · ${x.criteria.cutoff} 이전 ${x.criteria.basis}`]), { num: [1], empty: '파기 기록이 없습니다.' });

// ------------------------------------------------------------ 통계·보고서 (개인정보 없이 집계)

export {
  itemPicker, jsqrReady, loadJsQR, mapIn, purgeLogTable, webcamReadOnce,
};
