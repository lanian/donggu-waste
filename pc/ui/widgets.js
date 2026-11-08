// 여러 화면이 함께 쓰는 부품: 품목 고르기·지도·웹캠 QR 읽기·파기 이력 표.
import * as M from '/web/map.js';
import { BUILTIN_FEES } from '/web/fees-data.js';
import { searchFees } from '/web/fees.js';
import { won } from '/web/fees.js';
import { FEES, api, btn, grid, h, kst, liveMaps, toast } from './core.js';

function itemPicker(onAdd, { usagePath = 'fee-usage' } = {}) {
  const st = { q: '', sel: null, qty: 1 };
  const q = h('input', { type: 'search', placeholder: '품목명·규격 검색 (예: 3인용 소파, 장롱, 2인용 매트리스)', style: 'flex:1' });
  const list = h('div', { class: 'results' });
  const qty = h('input', { type: 'number', min: 1, value: 1, style: 'width:80px' });
  const add = btn('품목 추가', null, 'primary', { disabled: true });
  let usage = [];
  if (usagePath) api(usagePath).then((u) => { usage = u; draw(); }).catch(() => {});
  const draw = () => {
    const quick = !st.q;
    const rows = quick ? usage.map((id) => FEES().rows.find((r) => r.id === id)).filter(Boolean) : searchFees(FEES(), st.q).slice(0, 40);
    list.replaceChildren(...rows.map((r) => btn(h('span', {}, `${r.name} ${r.spec}`, h('span', { class: 'p' }, `${won(r.price)} / ${r.unit_label}`),
      r.status === 'REVIEW' ? h('span', { class: 'w' }, '규격 확인 필요 → 금액 확인 대기') : null), () => { st.sel = r.id; draw(); }, st.sel === r.id ? 'on' : '')));
    list.style.display = rows.length ? '' : 'none';
    if (quick && rows.length) list.prepend(h('p', { class: 'muted', style: 'margin:4px 8px' }, '자주 쓰는 품목'));
    add.disabled = !st.sel;
  };
  q.addEventListener('input', () => { st.q = q.value; st.sel = null; draw(); }); // 검색어가 바뀌면 선택 해제
  // 키보드로만: Enter = 고른 행(없으면 첫 결과) 추가, ↓ = 결과로 이동, 수량 칸에서 Enter = 추가
  q.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { const b = list.querySelector('button'); if (b) { e.preventDefault(); b.focus(); } }
    else if (e.key === 'Enter' && st.q) { e.preventDefault(); if (!st.sel) { const first = searchFees(FEES(), st.q)[0]; if (first) st.sel = first.id; } if (st.sel) add.click(); }
  });
  list.addEventListener('keydown', (e) => {
    const bs = [...list.querySelectorAll('button')], i = bs.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' && i >= 0 && bs[i + 1]) { e.preventDefault(); bs[i + 1].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (i > 0 ? bs[i - 1] : q).focus(); }
  });
  qty.addEventListener('keydown', (e) => { if (e.key === 'Enter' && st.sel) { e.preventDefault(); add.click(); } });
  add.addEventListener('click', () => { const r = FEES().rows.find((x) => x.id === st.sel); if (!r) return; onAdd({ fee_rate_id: r.id, qty: Math.max(1, +qty.value || 1), _row: r }); st.sel = null; q.value = ''; st.q = ''; qty.value = 1; draw(); });
  const fname = h('input', { placeholder: '목록에 없는 품목 이름' });
  const funit = h('select', {}, ...[['EA', '개'], ['PIECE', '쪽'], ['SET', '세트'], ['L20', '20ℓ당']].map(([v, t]) => h('option', { value: v }, t)));
  const fqty = h('input', { type: 'number', min: 1, value: 1, style: 'width:80px' });
  const fadd = btn('확인 대기로 추가', () => { if (!fname.value.trim()) return toast('품목 이름을 적어 주세요', true); onAdd({ name: fname.value.trim(), unit: funit.value, unit_label: funit.selectedOptions[0].textContent, qty: Math.max(1, +fqty.value || 1) }); fname.value = ''; });
  draw();
  return h('div', {}, h('div', { class: 'row' }, q, qty, add), list,
    h('p', { class: 'small muted' }, '찾은 뒤 Enter로 바로 추가, ↓로 결과 고르기'),
    h('details', { class: 'free' }, h('summary', {}, '목록에 없는 품목 넣기 (금액 확인 대기)'), h('div', { class: 'row' }, fname, funit, fqty, fadd)),
    h('p', { class: 'muted' }, `수수료표: ${BUILTIN_FEES.label} (${FEES().version_id}) · 표에 없는 품목은 0원이 아니라 금액 확인 대기로 남습니다. 유사 품목 적용은 담당자가 확인해 정합니다.`));
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
