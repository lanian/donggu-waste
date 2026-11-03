// 동구 대형폐기물 수거 시제품 - 휴대폰 앱 (서버 없는 PWA)
// 업무 데이터는 이 기기 안에서만 암호화해 보관한다. 외부로 보내는 통신 코드는 없다.
import * as P from './proto.js';
import * as S from './store.js';
import { searchFees, deriveStop, won, LABEL, matchText } from './fees.js';
import { BUILTIN_FEES } from './fees-data.js';
import * as B from './backup.js';
import * as M from './map.js';
import qrcode from './vendor/qrcode.mjs';

export const APP_VERSION = '0.9.0';
const BACKUP_WARN_MS = 24 * 3600 * 1000; // 단독 모드: 마지막 백업 후 24시간 지나면 경고
const AUTO_LOCK_MS = 5 * 60 * 1000;      // 5분 무조작
const HIDDEN_LOCK_MS = 2 * 60 * 1000;    // 다른 앱(전화 등)에 2분 넘게 있으면 잠금
const COLLECT_TTL_MS = 24 * 3600 * 1000; // 받다 만 QR 조각 보관 시간

const $app = document.getElementById('app');
let st = null;               // 복호화된 업무 상태 (잠금 해제 동안만)
let ui = { filter: 'ALL' };  // 화면 상태
let saving = Promise.resolve();
let lastActive = Date.now(), hiddenAt = 0;
let camera = null;
let photoUrls = [];
let liveMap = null; // 화면을 바꿀 때 지도 자원을 정리한다

// ------------------------------------------------------------ 작은 DOM 도우미 (문자열은 항상 textContent)
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'text') el.textContent = v;
    else if (k === 'style') el.style.cssText = v; // CSP(style-src 'self')는 style 속성을 막지만 CSSOM 지정은 허용
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
// 고르는 단추(opt·tab)는 선택 상태를 색만이 아니라 aria-pressed로도 알린다
const btn = (label, onclick, cls = '', extra = {}) => h('button', { class: 'btn ' + cls, onclick, type: 'button', ...(/\b(opt|tab)\b/.test(cls) ? { 'aria-pressed': /\bon\b/.test(cls) ? 'true' : 'false' } : {}), ...extra }, label);
const fmtPhone = (p) => p.length === 11 ? `${p.slice(0, 3)}-${p.slice(3, 7)}-${p.slice(7)}` : p;
const go = (hash) => { location.hash = hash; };
const toast = (msg, kind = '') => {
  document.querySelectorAll('.toast').forEach((x) => x.remove());  // 한 번에 하나만, 다음 화면으로 넘어가지 않게
  const t = h('div', { class: 'toast ' + kind, role: kind === 'bad' ? 'alert' : 'status' }, msg);
  document.body.append(t); setTimeout(() => t.remove(), kind === 'bad' ? 3500 : 2000);
};

const ERR = {
  QR_INCOMPLETE: '목록을 아직 다 받지 못했습니다.',
  QR_NOT_OURS: '업무용 QR이 아닙니다.',
  QR_CORRUPT: 'QR을 잘못 읽었습니다. 계속 비춰 주세요.',
  QR_BAD_ENCODING: 'QR을 잘못 읽었습니다. 계속 비춰 주세요.',
  AUTH_FAIL: '목록 확인에 실패했습니다. 담당자에게 다시 요청하세요.',
  WRONG_DEVICE: '다른 휴대폰용 목록입니다. PC 담당자에게 이 휴대폰 팀의 목록인지 확인해 달라고 하세요.',
  QR_STALE_VERSION: '이미 받은 것보다 오래된 목록입니다. PC 담당자에게 최신 목록 QR을 다시 띄워 달라고 하세요.',
  BAD_VERSION: '앱 업데이트가 필요합니다. 앱을 닫았다 다시 열고 위에 뜨는 [업데이트]를 누르세요.',
  QR_NOT_REGISTRATION: '기기 등록용 QR이 아닙니다.',
  UNSENT_PREVIOUS: '이전 날짜 결과를 먼저 보내고 확인 코드를 입력해 주세요.',
  STORAGE_LOW: '저장 공간이 부족합니다. 결과를 먼저 보내 주세요.',
  PHOTO_SAVE_FAIL: '사진이 저장되지 않았습니다. 다시 찍어 주세요.',
  FEE_VERSION_MISSING: 'PC와 앱의 수수료표 버전이 다릅니다. 앱을 업데이트하거나 담당자에게 문의하세요.',
};
const errMsg = (e) => ERR[e && e.code] || (e && e.message) || '처리하지 못했습니다. 다시 시도하고, 계속되면 담당자에게 이 화면을 보여 주세요.';

// ------------------------------------------------------------ 상태 저장
const emptyState = () => ({ mode: null, daylist: null, events: [], seq: 0, releasedSeq: 0, exports: [], lastBundleHash: null,
  requests: [], receiptCounter: {}, lastBackupAt: null, intakeDraft: null, mapEnabled: true });
async function loadState() {
  st = { ...emptyState(), ...((await S.getSecure('state')) || {}) };
  applyTextSize();
  if (!st.mode && st.daylist) st.mode = 'PC'; // 0.1.0에서 쓰던 자료는 PC 연결 방식
}

// 사용 방식: 'PC' = PC가 원본, QR로 목록 받기 / 'SOLO' = 이 휴대폰에서 접수부터 수거까지
const isSolo = () => st.mode === 'SOLO';
const allStops = () => (isSolo() ? st.requests : (st.daylist ? st.daylist.stops : []));
const findStop = (id) => allStops().find((s) => s.request_id === id);
// 수수료표: 앱에 내장된 표를 쓰고, PC 목록은 버전만 알려 준다. 버전이 다르면 계산하지 않는다.
// PC가 승인한 개정(확인 필요 행의 확정 단가 등)이 있으면 내장 표에 덧씌운다.
let feeMemo = null;
const withOverrides = (ft) => {
  const key = ft.version_id + JSON.stringify(ft.overrides || {});
  if (feeMemo && feeMemo.key === key) return feeMemo.table;
  const ov = ft.overrides || {};
  const table = { ...BUILTIN_FEES, version_id: ft.version_id, effective_from: ft.effective_from || BUILTIN_FEES.effective_from,
    rows: BUILTIN_FEES.rows.map((r) => (ov[r.id] != null ? { ...r, price: ov[r.id], status: 'CONFIRMED', overridden: true } : r)) };
  feeMemo = { key, table };
  return table;
};
const feeTable = () => {
  if (isSolo()) {
    // 단독 방식: 담당자가 이 휴대폰에서 확정한 단가(근거 기록)를 내장 표에 덧씌운다
    const ov = st.feeOverrides || {};
    if (!Object.keys(ov).length) return BUILTIN_FEES;
    return withOverrides({ version_id: `${BUILTIN_FEES.version_id}-s${st.feeRev || 1}`, base_version: BUILTIN_FEES.version_id, overrides: Object.fromEntries(Object.entries(ov).map(([k, v]) => [k, v.price])) });
  }
  const ft = st.daylist && st.daylist.fee_table;
  if (!ft) return null;
  if (ft.version_id === BUILTIN_FEES.version_id) return BUILTIN_FEES;
  if (ft.base_version === BUILTIN_FEES.version_id) return withOverrides(ft);
  return ft.rows ? ft : null;
};
const todayKST = (offsetDays = 0) => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul' }).format(new Date(Date.now() + offsetDays * 864e5));
const ago = (iso) => { if (!iso) return '없음'; const m = Math.round((Date.now() - Date.parse(iso)) / 60000); return m < 60 ? `${m}분 전` : m < 1440 ? `${Math.round(m / 60)}시간 전` : `${Math.round(m / 1440)}일 전`; };
function commit() {
  const snapshot = JSON.parse(JSON.stringify(st));
  saving = saving.then(() => S.putSecure('state', snapshot)).catch((e) => {
    toast(e && e.name === 'QuotaExceededError' ? ERR.STORAGE_LOW : '저장 실패: ' + errMsg(e), 'bad');
  });
  return saving;
}
async function addEvent(stop, kind, body) {
  st.seq += 1;
  st.events.push({
    event_id: crypto.randomUUID(), seq: st.seq, request_id: stop.request_id,
    assign_ver: stop.assign_ver ?? (st.daylist ? st.daylist.assign_ver : 1), base_req_ver: stop.req_ver, kind, body, ts: new Date().toISOString(),
  });
  await commit();
}
const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
// 전화: 전화 앱에 번호만 넘긴다(자동 발신 없음). 돌아오면 연락 결과를 묻는다.
async function dial(stop) {
  await addEvent(stop, 'DIAL_INTENT', {});
  ui.afterCall = stop.request_id;
  location.href = 'tel:' + stop.phone;
}
// 접수와 무관한 팀 기록(하차 등)
async function addCrewEvent(kind, body) {
  st.seq += 1;
  st.events.push({ event_id: crypto.randomUUID(), seq: st.seq, request_id: '', assign_ver: st.daylist ? st.daylist.assign_ver : 0, base_req_ver: null, kind, body, ts: new Date().toISOString() });
  await commit();
}
const unsent = () => st.events.filter((e) => e.seq > st.releasedSeq);

// ------------------------------------------------------------ 잠금
async function lockNow() { stopCamera(); S.lock(); st = null; await render(); }
['pointerdown', 'keydown'].forEach((t) => addEventListener(t, () => { lastActive = Date.now(); }, { passive: true }));
setInterval(() => { if (S.isUnlocked() && Date.now() - lastActive > AUTO_LOCK_MS) lockNow(); }, 15000);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { hiddenAt = Date.now(); stopCamera(); }
  else if (S.isUnlocked() && hiddenAt && Date.now() - hiddenAt > HIDDEN_LOCK_MS) lockNow();
  else if (S.isUnlocked() && ui.afterCall && location.hash === `#/stop/${ui.afterCall}`) render().then(() => { const c = document.getElementById('st-contact'); if (c) c.scrollIntoView({ block: 'start' }); });
  else if (location.hash.startsWith('#/scan') || location.hash.startsWith('#/register')) render();
});

function screenSetup() {
  const p1 = h('input', { type: 'password', inputmode: 'numeric', autocomplete: 'new-password', placeholder: '숫자 6자리 이상', class: 'code-in' });
  const p2 = h('input', { type: 'password', inputmode: 'numeric', autocomplete: 'new-password', placeholder: '한 번 더', class: 'code-in' });
  const msg = h('p', { class: 'msg' });
  const ok = btn('PIN 설정', async () => {
    if (!/^\d{6,}$/.test(p1.value)) return (msg.textContent = '숫자 6자리 이상으로 정해 주세요.');
    if (p1.value !== p2.value) return (msg.textContent = '두 번 입력한 PIN이 다릅니다.');
    ok.disabled = true; msg.textContent = '설정 중…';
    await S.setupPin(p1.value);
    await S.persist();
    await loadState(); go('#/mode'); render();
  }, 'primary big');
  return h('main', { class: 'center' },
    h('h1', {}, '동구 대형폐기물 수거'),
    h('p', {}, '이 휴대폰에서 쓸 잠금 PIN을 정하세요. 업무 자료는 이 PIN으로 잠긴 채 휴대폰 안에만 저장됩니다.'),
    p1, p2, ok, msg,
    h('p', { class: 'small' }, '시제품입니다. 가상 자료로만 사용하세요.'));
}

function screenLock() {
  const pin = h('input', { type: 'password', inputmode: 'numeric', autocomplete: 'current-password', placeholder: 'PIN', class: 'code-in' });
  const msg = h('p', { class: 'msg' });
  const ok = btn('잠금 해제', async () => {
    ok.disabled = true; msg.textContent = '확인 중…';
    try { await S.unlock(pin.value); await loadState(); if (!location.hash || location.hash === '#/') go('#/home'); await render(); if (ui.afterCall && location.hash === `#/stop/${ui.afterCall}`) { const c = document.getElementById('st-contact'); if (c) c.scrollIntoView({ block: 'start' }); } }
    catch (e) {
      ok.disabled = false; pin.value = '';
      msg.textContent = e.until && e.until > Date.now() ? `여러 번 틀렸습니다. ${Math.ceil((e.until - Date.now()) / 1000)}초 뒤 다시 시도하세요.` : 'PIN이 맞지 않습니다.';
    }
  }, 'primary big');
  pin.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok.click(); });
  setTimeout(() => pin.focus(), 50);
  return h('main', { class: 'center' }, h('h1', {}, '잠겨 있습니다'), pin, ok, msg);
}

// ------------------------------------------------------------ 홈
// 오늘 진행 요약 띠: 완료/전체와 진행 막대
function daySummary(items) {
  const n = items.length, done = items.filter((x) => x.d.progress === 'DONE').length;
  const hold = items.filter((x) => ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.d.outcome)).length;
  const pend = items.filter((x) => x.d.fee.pendingCount).length;
  return h('section', { class: 'summary', 'aria-label': '오늘 진행' },
    h('div', { class: 'sum-main' }, h('b', {}, done), h('span', {}, ` / ${n}곳 완료`)),
    h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': n, 'aria-valuenow': done }, h('div', { class: 'fill', style: `width:${n ? (done / n) * 100 : 0}%` })),
    h('div', { class: 'sum-sub' }, hold ? h('span', { class: 'chip warn' }, `재방문·보류 ${hold}`) : null, pend ? h('span', { class: 'chip warn' }, `금액 확인 대기 ${pend}`) : null));
}
// 목록 찾기: 이름·주소·품목, 초성도 됨 (예: ㄱㅅ)
function stopSearch(total) {
  if (total < 6) return null;
  const q = h('input', { type: 'search', class: 'search', placeholder: '이름·주소·품목 찾기 (초성 가능)', value: ui.stopQ || '', 'aria-label': '목록에서 찾기' });
  q.addEventListener('input', () => { ui.stopQ = q.value; clearTimeout(stopSearch.t); stopSearch.t = setTimeout(() => { render().then(() => { const el = document.querySelector('input[aria-label="목록에서 찾기"]'); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }); }, 250); });
  return q;
}
const stopHay = (s) => `${s.name} ${s.address} ${s.spot_note || ''} ${s.receipt_no || ''} ${s.items.map((i) => i.name + ' ' + (i.spec || '')).join(' ')}`;
const filterQ = (list) => (ui.stopQ ? list.filter((x) => matchText(stopHay(x.s), ui.stopQ)) : list);

async function screenHome() {
  if (!st.mode) return screenMode();
  if (isSolo()) return screenHomeSolo();
  const registered = !!(await S.getRaw('pc')) && !!(await S.getRaw('device'));
  const n = unsent().length;
  const dl = st.daylist;
  const top = h('header', { class: 'bar' }, h('h1', { class: 'bar-title' }, '동구 수거'), btn('잠금', lockNow, 'ghost'));
  // 목록이 있으면 하루 한두 번 쓰는 버튼은 작게, 방문 순서가 먼저 보이게
  const stale = n && dl && dl.work_date < todayKST();
  const actions = dl
    ? h('section', { class: 'actions grid2' },
      btn('오늘 목록 받기', () => go('#/scan'), 'big', { disabled: !registered }),
      btn(n ? `결과 보내기 ${n}건` : '결과 보내기', () => go('#/export'), 'big' + (stale ? ' warn' : '')))
    : h('section', { class: 'actions' },
      btn('오늘 목록 받기', () => go('#/scan'), 'primary huge', { disabled: !registered }),
      btn(n ? `결과 보내기 (미전송 ${n}건)` : '결과 보내기', () => go('#/export'), 'huge ' + (stale ? 'warn' : '')));
  const out = [top];
  if (!registered) out.push(h('section', { class: 'card notice' }, h('p', {}, '먼저 관리자와 함께 기기 등록을 해 주세요.'), btn('기기 등록', () => go('#/register'), 'primary')));
  out.push(actions);
  if (!dl) { out.push(h('p', { class: 'empty' }, '받은 목록이 없습니다.')); }
  else {
    const stops = dl.stops.map((s) => ({ s, d: deriveStop(s, st.events) }));
    const remain = stops.filter((x) => x.d.progress !== 'DONE' && !x.s.removed);
    const hold = stops.filter((x) => ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.d.outcome) || x.d.contact === 'CALLBACK');
    const list = filterQ({ ALL: stops, REMAIN: remain, HOLD: hold }[ui.filter]);
    out.push(h('p', { class: 'meta' }, `${dl.work_date} ${dl.crew}` + (dl.assign_ver > 1 ? ` · 목록 ${dl.assign_ver}차` : '')));
    out.push(daySummary(stops.filter((x) => !x.s.removed)));
    if (st.mapEnabled) out.push(btn('🗺 지도로 보기', () => go('#/map'), 'big'));
    out.push(h('div', { class: 'tabs', role: 'group', 'aria-label': '목록 거르기' },
      ...[['ALL', `전체 ${stops.length}`], ['REMAIN', `남음 ${remain.length}`], ['HOLD', `재방문·보류 ${hold.length}`]].map(([k, t]) =>
        btn(t, () => { ui.filter = k; render(); }, ui.filter === k ? 'tab on' : 'tab'))));
    out.push(stopSearch(stops.length));
    if (!list.length) out.push(h('p', { class: 'empty' }, ui.stopQ ? `'${ui.stopQ}'에 맞는 곳이 없습니다.` : '해당하는 곳이 없습니다.'));
    const nowId = (stops.find((x) => x.d.progress !== 'DONE' && !x.d.outcome && !x.s.removed) || {}).s?.request_id;  // 지금 갈 곳
    out.push(h('ul', { class: 'stops route' }, list.map(({ s, d }) => h('li', {},
      h('a', { href: `#/stop/${s.request_id}`, class: `stop p-${d.outcome || d.progress} c-${d.contact}` + (d.progress === 'DONE' ? ' done' : '') + (s.request_id === nowId ? ' now' : ''), 'aria-current': s.request_id === nowId ? 'step' : null },
        h('span', { class: 'seq' }, s.seq, s.request_id === nowId ? h('small', { class: 'now-tag' }, '지금') : null),
        h('span', { class: 'body' },
          h('strong', {}, s.name), h('span', { class: 'addr' }, s.address),
          h('span', { class: 'items' }, s.items.map((i) => `${i.name} ${i.spec} ${i.qty}${i.unit_label}`).join(', ')),
          h('span', { class: 'chips' },
            s.revisit ? h('em', { class: 'chip warn' }, '재방문') : null,
            h('em', { class: 'chip c-' + d.contact }, LABEL.contact[d.contact]),
            h('em', { class: 'chip p-' + d.progress }, s.removed ? '목록에서 제외됨' : LABEL.progress[d.progress]),
            d.fee.pendingCount ? h('em', { class: 'chip warn' }, '금액 확인 대기') : null)),
        h('span', { class: 'eta' }, s.eta || '')),
      s.unload_after && !ui.stopQ ? h('div', { class: 'unload-row' }, h('span', { class: 'seq u' }, '⇩'),
        h('span', {}, h('strong', {}, `${s.unload_after.label} 하차`), h('br'), h('span', { class: 'small' }, `도착 ${s.unload_after.eta || '-'} · 약 ${s.unload_after.volume_m3}m³ 비움 · ${s.unload_after.minutes}분`))) : null))));
    const myUnloads = st.events.filter((e) => e.kind === 'UNLOAD');
    out.push(btn(myUnloads.length ? `하차 기록 (${myUnloads.length}회)` : '하차 기록', () => go('#/unload'), 'big'));
  }
  out.push(h('footer', { class: 'foot' }, h('a', { href: '#/settings' }, '설정·정보')));
  return h('main', {}, ...out);
}

// ------------------------------------------------------------ 하차 기록 (반입지에서 비운 양)
function screenUnload() {
  const list = st.events.filter((e) => e.kind === 'UNLOAD');
  const plannedLabel = st.daylist && (st.daylist.stops.find((x) => x.unload_after) || {}).unload_after;
  const now = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date());
  const place = h('input', { type: 'text', class: 'search', value: (list.length ? list[list.length - 1].body.place : (plannedLabel && plannedLabel.label)) || '' , placeholder: '반입지 이름' });
  const time = h('input', { type: 'time', value: now });
  const vol = h('input', { type: 'number', inputmode: 'decimal', step: '0.1', min: 0, placeholder: '예: 5.5' });
  const kg = h('input', { type: 'number', inputmode: 'decimal', step: '1', min: 0, placeholder: '계근표 중량' });
  const note = h('input', { type: 'text', placeholder: '메모 (선택, 개인정보 적지 않기)' });
  const msg = h('p', { class: 'msg' });
  const fld = (t, el) => h('label', { class: 'fld' }, h('span', {}, t), el);
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '하차 기록')),
    h('section', { class: 'card' }, fld('반입지', place), h('div', { class: 'grid2' }, fld('시각', time), fld('부피 (m³, 추정)', vol)), fld('중량 (kg)', kg), fld('메모', note),
      h('p', { class: 'small' }, isSolo() ? '이 휴대폰에 저장되고 백업 파일에 함께 들어갑니다. 부피나 중량 중 하나는 적어 주세요.' : '결과 보내기 때 PC로 함께 갑니다. 부피나 중량 중 하나는 적어 주세요.'), msg),
    list.length ? h('section', { class: 'card' }, h('h2', {}, `오늘 하차 ${list.length}회`),
      h('ul', { class: 'list' }, ...list.map((e) => h('li', {}, `${e.body.at_time} ${e.body.place} · ${e.body.volume_m3 ? e.body.volume_m3 + 'm³' : ''} ${e.body.weight_kg ? e.body.weight_kg + 'kg' : ''} ${e.seq > st.releasedSeq ? '(미전송)' : ''}`)))) : null,
    h('nav', { class: 'dock' }, h('span', {}), h('span', {}), btn('하차 기록', async () => {
      if (!vol.value && !kg.value) return (msg.textContent = '부피나 중량 중 하나는 적어 주세요.');
      if (!place.value.trim()) return (msg.textContent = '반입지 이름을 적어 주세요.');
      await addCrewEvent('UNLOAD', { place: place.value.trim(), at_time: time.value, volume_m3: vol.value ? +vol.value : null, weight_kg: kg.value ? +kg.value : null, note: note.value.trim(), work_date: st.daylist ? st.daylist.work_date : todayKST() });
      toast('하차를 기록했습니다'); go('#/home');
    }, 'primary dock-next')));
}

// ------------------------------------------------------------ 카메라 + QR
function stopCamera() {
  if (camera) { camera.stop = true; camera.stream && camera.stream.getTracks().forEach((t) => t.stop()); camera = null; }
}
async function startCamera(video, onText) {
  stopCamera();
  const cam = { stop: false };
  camera = cam;
  try {
    cam.stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } } });
  } catch (e) { camera = null; throw Object.assign(new Error('카메라를 열 수 없습니다. 카메라 권한을 허용해 주세요.'), { code: 'CAMERA' }); }
  if (cam.stop) { cam.stream.getTracks().forEach((t) => t.stop()); return; }
  video.srcObject = cam.stream; await video.play().catch(() => {});
  const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d', { willReadFrequently: true });
  const loop = () => {
    if (cam.stop) return;
    if (video.readyState >= 2 && video.videoWidth) {
      const scale = Math.min(1, 1280 / Math.max(video.videoWidth, video.videoHeight)); // 촘촘한 QR(버전 20 이상)도 읽히도록 1280까지 원본 크기로
      canvas.width = Math.round(video.videoWidth * scale); canvas.height = Math.round(video.videoHeight * scale);
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const code = window.jsQR(img.data, img.width, img.height, { inversionAttempts: 'dontInvert' });
      if (code && code.data) { try { onText(code.data); } catch (e) { console.warn(e.code || e.message); } }
    }
    setTimeout(loop, 90);
  };
  loop();
}

// ------------------------------------------------------------ 기기 등록
async function screenRegister() {
  const pc = await S.getRaw('pc');
  let dev = await S.getRaw('device');
  if (!dev) {
    const keys = await P.generateDeviceKeys();
    dev = { id: P.randomBytes(16), kex: keys.kex, sign: keys.sign };
    await S.putRaw('device', dev);
  }
  const back = h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '기기 등록'));
  if (!pc || ui.rescanPc) {
    const video = h('video', { playsinline: true, muted: true, class: 'cam' });
    const msg = h('p', { class: 'msg' }, '관리자 PC의 [기기 등록 QR]을 비추세요.');
    setTimeout(() => startCamera(video, async (text) => {
      try {
        const reg = P.parseRegistration(text);
        stopCamera();
        await S.putRaw('pc', reg); ui.rescanPc = false; render();
      } catch (e) { msg.textContent = errMsg(e); }
    }).catch((e) => { msg.textContent = errMsg(e); }), 0);
    return h('main', {}, back, video, msg);
  }
  const kexRaw = await P.exportRaw(dev.kex.publicKey), signRaw = await P.exportRaw(dev.sign.publicKey);
  const code = await P.deviceCode(dev.id, kexRaw, signRaw);
  const fp = await P.fingerprint(pc.pcSignPub, kexRaw, signRaw);
  const ta = h('textarea', { readonly: true, rows: 4, class: 'code' }); ta.value = code;
  return h('main', {}, back,
    h('section', { class: 'card' },
      h('p', {}, '1. 아래 기기 코드를 관리자에게 전달하면 PC에 등록합니다. (공개키만 들어 있습니다)'),
      ta, btn('코드 복사', async () => { try { await navigator.clipboard.writeText(code); toast('복사했습니다'); } catch { ta.select(); } }),
      h('p', {}, '2. PC 화면에 나온 지문 숫자가 아래와 같은지 확인하세요.'),
      h('p', { class: 'fp' }, fp.slice(0, 3) + ' ' + fp.slice(3)),
      h('p', { class: 'small' }, '기기 번호 ' + P.uuidStr(dev.id).slice(0, 8))),
    btn('PC 등록 QR 다시 읽기', () => { ui.rescanPc = true; render(); }, 'ghost'),
    btn('완료', () => go('#/home'), 'primary big'));
}

// ------------------------------------------------------------ 목록 받기
async function loadCollector() {
  const saved = (await S.getRaw('collect')) || {};
  for (const [k, b] of Object.entries(saved)) if (Date.now() - b.firstSeen > COLLECT_TTL_MS) delete saved[k];
  return new P.Collector(saved);
}

async function screenScan() {
  const pc = await S.getRaw('pc'), dev = await S.getRaw('device');
  const col = await loadCollector();
  let current = Object.keys(col.bundles).sort((a, b) => col.bundles[b].firstSeen - col.bundles[a].firstSeen)[0];
  const video = h('video', { playsinline: true, muted: true, class: 'cam' });
  const bar = h('div', { class: 'progress' }, h('div', { class: 'fill' }));
  const count = h('p', { class: 'count' }, '목록 QR을 비추세요');
  const miss = h('p', { class: 'small' });
  const msg = h('p', { class: 'msg' });
  const show = () => {
    if (!current) return;
    const b = col.bundles[current], got = Object.keys(b.parts).length;
    bar.firstChild.style.width = `${(100 * got) / b.total}%`;
    count.textContent = `조각 ${got} / ${b.total}`;
    const m = col.missing(current);
    miss.textContent = m.length && m.length <= 12 ? '남은 번호: ' + m.map((i) => i + 1).join(', ') : '';
  };
  show();
  let finishing = false, reads = 0;
  const finish = async () => {
    finishing = true; stopCamera(); msg.textContent = '확인 중…';
    try {
      const envBytes = col.assemble(current);
      const opened = await P.openEnvelope(envBytes, await P.importEcdsaPub(pc.pcSignPub), dev.kex.privateKey, dev.id);
      if (opened.payload.schema === 'dg.qrtest/1') {
        // 실측 모드: 가상 목록이므로 저장하지 않고 시간·장수·인식 횟수와 복원 확인 코드만 보여 준다
        const b = col.bundles[current];
        const exp = P.expandDaylist({ ...opened.payload, schema: 'dg.daylist/2' }, BUILTIN_FEES);
        ui.qrtest = { secs: ((Date.now() - b.firstSeen) / 1000).toFixed(1), frames: b.total, reads: reads, count: exp.stops.length, code: opened.receipt.slice(0, 4), params: opened.payload.params };
        col.drop(current); await S.putRaw('collect', col.bundles);
        go('#/qrtest-result');
        return;
      }
      const pl = P.expandDaylist(opened.payload, BUILTIN_FEES);
      if (pl.schema !== 'dg.daylist/1') throw Object.assign(new Error('목록 형식 아님'), { code: 'BAD_VERSION' });
      if (!(pl.fee_table && (pl.fee_table.version_id === BUILTIN_FEES.version_id || pl.fee_table.base_version === BUILTIN_FEES.version_id || pl.fee_table.rows))) throw Object.assign(new Error(''), { code: 'FEE_VERSION_MISSING' });
      const hash = P.toHex(await P.sha256(envBytes));
      const old = st.daylist;
      if (hash === st.lastBundleHash) { msg.textContent = '이미 받은 목록입니다. 바뀐 내용이 없습니다.'; }
      else {
        if (old && old.work_date === pl.work_date && pl.assign_ver < old.assign_ver) throw Object.assign(new Error(''), { code: 'QR_STALE_VERSION' });
        if (old && old.work_date !== pl.work_date && unsent().length) throw Object.assign(new Error(''), { code: 'UNSENT_PREVIOUS' });
        // 새 갱신본: 이름·주소·품목·순서만 바꾸고, 현장 기록(이벤트)은 그대로 둔다.
        if (old && old.work_date === pl.work_date) {
          const ids = new Set(pl.stops.map((s) => s.request_id));
          const withEvents = new Set(st.events.map((e) => e.request_id));
          for (const s of old.stops) if (!ids.has(s.request_id) && withEvents.has(s.request_id)) pl.stops.push({ ...s, removed: true });
        }
        st.daylist = pl; st.lastBundleHash = hash;
        await commit();
      }
      col.drop(current); await S.putRaw('collect', col.bundles);
      const rc = opened.receipt;
      ui.receipt = { code: rc.slice(0, 4) + ' ' + rc.slice(4), count: pl.stops.length, ver: pl.assign_ver };
      go('#/received');
    } catch (e) {
      msg.textContent = errMsg(e);
      if (['AUTH_FAIL', 'WRONG_DEVICE', 'QR_STALE_VERSION', 'BAD_VERSION'].includes(e.code)) { col.drop(current); await S.putRaw('collect', col.bundles); current = null; }
      finishing = false;
    }
  };
  setTimeout(() => startCamera(video, async (text) => {
    if (finishing) return;
    let r;
    try { r = col.add(text); } catch (e) { if (e.code === 'QR_NOT_OURS' || e.code === 'BAD_VERSION') msg.textContent = errMsg(e); return; }
    if (!current) current = r.key;
    if (r.key !== current) { msg.textContent = '다른 목록의 QR이 섞였습니다. 지금 받던 목록을 계속 비춰 주세요.'; return; }
    reads += 1;
    if (!r.dup) { msg.textContent = ''; S.putRaw('collect', col.bundles); if (navigator.vibrate) navigator.vibrate(20); }
    show();
    if (col.complete(current)) finish();
  }).catch((e) => { msg.textContent = errMsg(e); }), 0);
  return h('main', {},
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '오늘 목록 받기')),
    video, bar, count, miss, msg,
    h('p', { class: 'small' }, '다 받기 전에는 저장하지 않습니다. 중간에 멈춰도 24시간 안에 이어서 받을 수 있습니다.'),
    current ? btn('받던 조각 버리고 처음부터', async () => { col.drop(current); await S.putRaw('collect', col.bundles); render(); }, 'ghost') : null);
}

function screenReceived() {
  const r = ui.receipt;
  if (!r) { go('#/home'); return h('main'); }
  return h('main', { class: 'center' },
    h('h1', {}, '목록을 받았습니다'),
    h('p', {}, `${r.count}건 · 목록 갱신본 ${r.ver}`),
    h('p', {}, 'PC 담당자에게 아래 수신 확인 코드를 알려 주세요.'),
    h('p', { class: 'fp' }, r.code),
    btn('오늘 순서 보기', () => { ui.receipt = null; go('#/home'); }, 'primary big'));
}

function screenQrTestResult() {
  const r = ui.qrtest;
  if (!r) { go('#/home'); return h('main'); }
  return h('main', { class: 'center' },
    h('h1', {}, 'QR 실측 결과'),
    h('p', {}, `가상 ${r.count}건 · 조각 ${r.params.chunk}B · 오류 복원 ${r.params.ec}`),
    h('section', { class: 'card' }, h('dl', {},
      h('dt', {}, '걸린 시간'), h('dd', { class: 'fp' }, `${r.secs}초`),
      h('dt', {}, 'QR 장수'), h('dd', {}, `${r.frames}장`),
      h('dt', {}, '인식 횟수'), h('dd', {}, `${r.reads}회 (중복 포함)`),
      h('dt', {}, '확인 코드'), h('dd', { class: 'fp' }, r.code))),
    h('p', { class: 'small' }, 'PC [QR 실측] 표에 이 값을 입력하세요. 실측 자료는 이 휴대폰에 저장하지 않았습니다.'),
    btn('처음으로', () => { ui.qrtest = null; go('#/home'); }, 'primary big'));
}

// ------------------------------------------------------------ 접수 상세
// 빠르게 여러 번 눌러도 수량이 빠지지 않게: 화면 숫자는 바로 바꾸고 저장은 잠깐 모아서 한 번
function stepper(value, onChange, min = 0, max = 99, delay = 0) {
  let cur = value, timer = null;
  const v = h('span', { class: 'val', 'aria-live': 'polite' }, value);
  const minus = btn('−', () => bump(-1), 'step', { 'aria-label': '빼기', disabled: value <= min });
  const plus = btn('+', () => bump(1), 'step', { 'aria-label': '더하기', disabled: value >= max });
  function bump(dlt) {
    const n = Math.min(max, Math.max(min, cur + dlt));
    if (n === cur) return;
    cur = n; v.textContent = n; minus.disabled = n <= min; plus.disabled = n >= max;
    if (!delay) return onChange(cur);
    clearTimeout(timer); timer = setTimeout(() => onChange(cur), delay);
  }
  return h('span', { class: 'stepper' }, minus, v, plus);
}

async function screenStop(id) {
  const stop = findStop(id);
  if (!stop) { go('#/home'); return h('main'); }
  const d = deriveStop(stop, st.events);
  const setLine = async (l, patch) => {
    const v = { qty_confirmed: l.qty_confirmed, qty_collected: l.qty_collected, reason: l.reason, ...patch };
    if (v.qty_collected > v.qty_confirmed) v.qty_collected = v.qty_confirmed;
    await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, ...v });
    render();
  };

  const info = h('section', { class: 'card who' },
    h('p', { class: 'name' }, stop.name),
    h('p', { class: 'phone' }, h('a', { href: 'tel:' + stop.phone, onclick: async (e) => { e.preventDefault(); await dial(stop); } }, fmtPhone(stop.phone))),
    h('p', { class: 'addr' }, stop.address),
    stop.spot_note ? h('p', { class: 'spot' }, '배출 위치: ' + stop.spot_note) : null,
    stop.memo ? h('p', { class: 'memo' }, '요청: ' + stop.memo) : null,
    h('p', { class: 'small' }, stop.eta ? `${stop.receipt_no} · 도착 예정 ${stop.eta}` : stop.receipt_no),
    stop.removed ? h('p', { class: 'notice' }, '새 목록에서 빠진 건입니다. PC 담당자 확인이 필요합니다.') : null,
    stop.status === 'CANCELLED' ? h('p', { class: 'notice' }, '취소된 접수입니다. 사유: ' + (stop.cancel_reason || '-')) : null,
    stop.revisit ? h('p', { class: 'notice' }, '재방문 건입니다. 남은 품목만 표시하며, 이전에 수거한 수량에서 이어서 셉니다.') : null,
    stop.appt ? h('p', { class: 'memo' }, `방문 약속: ${stop.appt[0] || ''}~${stop.appt[1] || ''}`) : null,
    st.mapEnabled ? h('div', { class: 'grid2' },
      btn(d.loc ? '지도' : '위치 미지정', () => go(`#/map/${id}`), 'opt', { disabled: !d.loc }),
      btn(d.loc ? '위치 고치기' : '위치 지정', () => go(`#/pick/${id}`), 'opt')) : null);

  const contact = h('section', { class: 'card' + (ui.afterCall === id && d.contact === 'NOT_CONTACTED' ? ' ask' : '') },
    h('h2', {}, '연락 결과', ui.afterCall === id ? h('span', { class: 'hint' }, ' — 통화 결과를 골라 주세요') : null),
    h('div', { class: 'grid2' }, ...['REACHED', 'NO_ANSWER', 'CALLBACK', 'WRONG_NUMBER'].map((k) =>
      btn(LABEL.contact[k], async () => { ui.afterCall = null; await addEvent(stop, 'CONTACT', { result: k }); render(); }, d.contact === k ? 'opt on' : 'opt'))),
    d.appt ? h('p', { class: 'notice' }, `재방문 약속: ${d.appt.date} ${d.appt.start || ''}${d.appt.end ? '~' + d.appt.end : ''}`) : null,
    ['REACHED', 'CALLBACK'].includes(d.contact) && d.progress !== 'DONE' ? revisitForm(stop, d) : null);

  const touched = new Set(st.events.filter((e) => e.request_id === stop.request_id && e.kind === 'ITEM_RESULT').map((e) => e.body.line_id));
  const showLeft = (l) => touched.has(l.line_id) || !!d.outcome || ui.tried === id;
  const lines = h('section', { class: 'card' }, h('h2', {}, '품목'),
    ...d.lines.map((l) => h('div', { class: 'line ' + (l.kind === 'FIELD' ? 'field' : 'orig') },
      h('div', { class: 'lhead' },
        h('strong', {}, `${l.name} ${l.spec}`),
        h('em', { class: 'tag' }, l.kind === 'FIELD' ? '현장 추가' : '처음 접수')),
      h('div', { class: 'price' }, l.price == null ? '금액 확인 대기' : `${won(l.price)} / ${l.unit_label}`),
      l.kind === 'RECEIVED' ? h('div', { class: 'row' }, h('span', {}, '접수 수량'), h('span', { class: 'val' }, l.qty_received)) : null,
      h('div', { class: 'row' }, h('span', {}, '현장 수량'), stepper(l.qty_confirmed, (n) => setLine(l, { qty_confirmed: n }), 0, 99, 350)),
      h('div', { class: 'row got' }, h('span', {}, '실은 수량'), stepper(l.qty_collected, (n) => setLine(l, { qty_collected: n }), 0, l.qty_confirmed, 350)),
      showLeft(l) ? h('div', { class: 'row' + (l.remaining ? ' left' : '') }, h('span', {}, '못 실은 수량'), h('span', { class: 'val' }, `${l.remaining} ${l.unit_label}`)) : null,
      l.remaining && showLeft(l) ? (() => {
        const sel = h('select', { class: 'reason' + (l.reason || ui.tried !== id ? '' : ' need'), 'aria-label': `${l.name} 못 실은 사유`, onchange: (e) => setLine(l, { reason: e.target.value }) },
          ...Object.entries(LABEL.reason).map(([k, t]) => h('option', { value: k, selected: k === l.reason }, t)));
        return sel;
      })() : null)),
    btn('+ 현장 품목 추가', () => { ui.add = { stop: id, q: '', sel: null, qty: 1, free: false }; go(`#/add/${id}`); }, 'big'));

  const f = d.fee;
  const units = Object.entries(f.qtyByUnit).map(([u, n]) => `${n}${u}`).join(' · ');
  const fee = h('section', { class: 'card fee' }, h('h2', {}, '계산 내역'),
    h('dl', {},
      h('dt', {}, '접수 당시 금액'), h('dd', {}, won(f.received)),
      h('dt', {}, '현장 추가분'), h('dd', {}, won(f.fieldAdded)),
      h('dt', {}, '확인된 소계'), h('dd', {}, won(f.confirmedSubtotal)),
      h('dt', {}, '접수 대비 증감'), h('dd', {}, (f.diff > 0 ? '+' : '') + won(f.diff)),
      h('dt', {}, '금액 확인 대기'), h('dd', {}, f.pendingCount + '건'),
      h('dt', { class: 'total' }, '최종 금액'), h('dd', { class: 'total' }, f.final == null ? '미확정 (금액 확인 대기 있음)' : won(f.final))),
    h('p', { class: 'small' }, `수량(단위별): ${units}. 금액은 현장 확인 수량 기준이며 수납 완료를 뜻하지 않습니다.`));

  const photoIn = (phase) => {
    const input = h('input', { type: 'file', accept: 'image/*', capture: 'environment', class: 'hidden' });
    input.addEventListener('change', async () => {
      const file = input.files && input.files[0]; if (!file) return;
      try { await savePhoto(stop, phase, file, ui.photoLine && ui.photoLine.stop === id ? ui.photoLine.line : null); toast('사진을 저장했습니다'); render(); }
      catch (e) { toast(e.name === 'QuotaExceededError' ? ERR.STORAGE_LOW : ERR.PHOTO_SAVE_FAIL, 'bad'); }
    });
    return h('label', { class: 'btn big' }, phase === 'BEFORE' ? '📷 수거 전' : '📷 수거 후', input);
  };
  // 사진을 특정 품목에 묶을 수 있다(선택). 기본은 접수 건 전체.
  const lineName = (lid) => { const l = d.lines.find((x) => x.line_id === lid); return l ? l.name : ''; };
  const photoFor = h('select', { 'aria-label': '사진을 묶을 품목', onchange: (e) => { ui.photoLine = { stop: id, line: e.target.value || null }; } },
    h('option', { value: '' }, '사진 대상: 접수 건 전체'),
    ...d.lines.map((l) => h('option', { value: l.line_id, selected: ui.photoLine && ui.photoLine.stop === id && ui.photoLine.line === l.line_id }, `사진 대상: ${l.name} ${l.spec || ''}`)));
  const thumbs = h('div', { class: 'thumbs' });
  for (const p of d.photos) {
    const img = h('img', { alt: p.phase === 'BEFORE' ? '수거 전' : '수거 후' });
    thumbs.append(h('figure', {}, img, h('figcaption', {}, (p.phase === 'BEFORE' ? '전' : '후') + (p.line_id ? ` · ${lineName(p.line_id)}` : ''))));
    S.getSecureBytes('ph:' + p.photo_id).then((b) => { if (b) { const u = URL.createObjectURL(new Blob([b], { type: 'image/jpeg' })); photoUrls.push(u); img.src = u; } });
  }
  const photos = h('section', { class: 'card' }, h('h2', {}, `사진 ${d.photos.length}장`), d.lines.length > 1 ? photoFor : null, h('div', { class: 'grid2' }, photoIn('BEFORE'), photoIn('AFTER')), thumbs,
    h('p', { class: 'small' }, '사진은 앱 안에만 암호화 저장되며 갤러리에 남지 않습니다.'));

  const msg = h('p', { class: 'msg' });
  const fillAll = async () => {
    for (const l of d.lines) if (l.qty_collected !== l.qty_confirmed) await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, qty_confirmed: l.qty_confirmed, qty_collected: l.qty_confirmed, reason: '' });
  };
  const fail = (t) => { ui.tried = id; ui.outcomeMsg = t; render(); };
  const outMsg = h('p', { class: 'msg err', role: 'alert' }, ui.outcomeMsg && ui.tried === id ? ui.outcomeMsg : '');
  const outcome = h('section', { class: 'card' }, h('h2', {}, '수거 결과', d.outcome ? h('span', { class: 'hint' }, ' — 저장됨: ' + LABEL.outcome[d.outcome]) : null),
    // 가장 흔한 경우를 한 번에: 모든 품목을 실은 것으로 채우고 완료 저장
    btn('전부 실음 · 수거 완료', async () => { ui.tried = null; ui.outcomeMsg = ''; await fillAll(); await addEvent(stop, 'ATTEMPT', { outcome: 'DONE' }); toast('수거 완료로 저장했습니다'); render(); }, d.outcome === 'DONE' ? 'primary huge on' : 'primary huge'),
    outMsg,
    h('div', { class: 'grid3' }, ...['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].map((k) => btn(LABEL.outcome[k], async () => {
      const left = d.lines.filter((l) => l.remaining > 0);
      if (k !== 'ON_HOLD' && !left.length) return fail('못 실은 품목이 없습니다. 모두 실었으면 [전부 실음 · 수거 완료]를 누르세요.');
      if (k !== 'ON_HOLD' && left.some((l) => !l.reason)) return fail('못 실은 품목마다 사유를 골라 주세요 (위 품목 칸).');
      if (k === 'PARTIAL' && !d.lines.some((l) => l.qty_collected > 0)) return fail('실은 품목이 없으면 미수거로 저장하세요.');
      if (k === 'NOT_COLLECTED' && d.lines.some((l) => l.qty_collected > 0)) return fail('실은 품목이 있으면 일부 수거로 저장하세요.');
      ui.tried = null; ui.outcomeMsg = '';
      await addEvent(stop, 'ATTEMPT', { outcome: k }); toast('저장했습니다'); render();
    }, d.outcome === k ? 'opt on' : 'opt'))),
    btn('품목 수량을 모두 실음으로 채우기', async () => { await fillAll(); render(); }, 'ghost link'),
    msg, fieldMemo(stop, d));

  const manage = isSolo() ? h('section', { class: 'card' }, h('h2', {}, '접수 관리'),
    h('p', { class: 'small' }, `원본 ${stop.req_ver}번째 버전 · 수정 ${(stop.versions || []).length}회`),
    h('div', { class: 'grid2' },
      btn('접수 수정', () => { ui.form = null; go(`#/intake/${id}`); }, 'opt', { disabled: stop.status === 'CANCELLED' }),
      btn('접수 취소', async () => {
        const why = window.prompt('취소 사유를 적어 주세요 (기록은 지워지지 않습니다)');
        if (!why || !why.trim()) return;
        stop.status = 'CANCELLED'; stop.cancel_reason = why.trim(); stop.cancelled_at = new Date().toISOString();
        await commit(); toast('취소로 기록했습니다'); render();
      }, 'opt', { disabled: stop.status === 'CANCELLED' })),
    (stop.versions || []).length ? h('ul', { class: 'small list' }, ...stop.versions.slice().reverse().map((v) => h('li', {}, `${v.at.slice(0, 16).replace('T', ' ')} · ${v.reason}`))) : null) : null;
  // 아래 고정 영역: 장갑 낀 손으로도 누르기 쉬운 전화·다음 지점
  const seqList = (isSolo() ? st.requests.filter((r) => r.planned_date === stop.planned_date && r.status !== 'CANCELLED').sort((a, b) => a.seq - b.seq) : (st.daylist ? st.daylist.stops.filter((x) => !x.removed) : []));
  const next = seqList.slice(seqList.indexOf(stop) + 1).find((x) => deriveStop(x, st.events).progress !== 'DONE');
  // 결과를 저장하기 전에는 "결과 저장 ↓", 저장한 뒤에는 "다음 N번 ▶" — 기록 없이 건너뛰지 않게
  const dock = h('nav', { class: 'dock', 'aria-label': '빠른 동작' },
    btn('← 목록', () => go('#/home'), 'dock-back'),
    btn('전화', () => dial(stop), 'dock-call'),
    d.outcome || stop.status === 'CANCELLED'
      ? btn(next ? `다음 ${next.seq}번 ▶` : '목록으로', () => go(next ? `#/stop/${next.request_id}` : '#/home'), 'primary dock-next')
      : btn('결과 저장 ↓', () => document.getElementById('st-result').scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' }), 'primary dock-next'));
  // 단계 표시: 연락 → 품목 → 사진 → 결과. 누르면 그 단계로 이동, 끝난 단계는 ✓
  contact.id = 'st-contact'; lines.id = 'st-items'; photos.id = 'st-photos'; outcome.id = 'st-result';
  const itemsDone = d.lines.length > 0 && st.events.some((e) => e.request_id === stop.request_id && e.kind === 'ITEM_RESULT');
  const steps = [['st-contact', '연락', d.contact !== 'NOT_CONTACTED'], ['st-items', '품목', itemsDone], ['st-photos', '사진', d.photos.length > 0], ['st-result', '결과', !!d.outcome]];
  const nextStep = d.outcome ? null : steps.find(([, , done]) => !done);
  const stepNav = h('nav', { class: 'steps', 'aria-label': '처리 단계' }, ...steps.map(([id, label, done], i) =>
    h('a', { href: '#', class: 'step' + (done ? ' done' : '') + (nextStep && nextStep[0] === id ? ' next' : ''), 'aria-current': nextStep && nextStep[0] === id ? 'step' : null,
      onclick: (e) => { e.preventDefault(); document.getElementById(id).scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' }); } },
      h('span', { class: 'n' }, done ? '✓' : String(i + 1)), h('span', {}, label))));
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, `${stop.seq}번`),
      h('span', { class: `chip state p-${d.outcome || d.progress}` }, stop.status === 'CANCELLED' ? '취소' : d.outcome ? LABEL.outcome[d.outcome] : LABEL.progress[d.progress])),
    stepNav, info, contact, lines, fee, photos, outcome, manage, dock);
}

// 현장 메모: 짧게 남기고 고치지 않는다(새 메모를 더함). 결과 파일로 PC에 전달.
function fieldMemo(stop, d) {
  const ta = h('textarea', { rows: 2, placeholder: '현장 메모 (예: 현관 앞 아님, 지하주차장 입구에 배출)', 'aria-label': '현장 메모' });
  return h('div', { class: 'memo-box' },
    (d.memos || []).length ? h('ul', { class: 'small list' }, ...d.memos.map((m) => h('li', {}, m.text))) : null,
    ta, btn('메모 남기기', async () => { const t = ta.value.trim(); if (!t) return; await addEvent(stop, 'FIELD_MEMO', { text: t.slice(0, 300) }); toast('메모를 남겼습니다'); render(); }, 'opt'));
}

// 통화로 약속한 재방문 날짜·시간: PC에서는 그 날짜 노선 후보와 방문 시간창이 된다
function revisitForm(stop, d) {
  const date = h('input', { type: 'date', value: d.appt ? d.appt.date : todayKST(1) });
  const start = h('input', { type: 'time', value: d.appt ? d.appt.start || '' : '' });
  const end = h('input', { type: 'time', value: d.appt ? d.appt.end || '' : '' });
  return h('details', { class: 'revisit', open: !!ui.openAppt },
    h('summary', {}, d.appt ? '재방문 약속 바꾸기' : '재방문 약속 기록'),
    h('div', { class: 'grid2' }, h('label', { class: 'fld' }, h('span', {}, '날짜'), date), h('div', {})),
    h('div', { class: 'grid2' }, h('label', { class: 'fld' }, h('span', {}, '부터'), start), h('label', { class: 'fld' }, h('span', {}, '까지'), end)),
    btn('약속 저장', async () => {
      if (!date.value) return toast('날짜를 골라 주세요', 'bad');
      if (start.value && end.value && start.value >= end.value) return toast('시간 범위를 확인해 주세요', 'bad');
      await addEvent(stop, 'REVISIT_APPT', { date: date.value, start: start.value || null, end: end.value || null });
      if (isSolo()) { // 단독 방식: 원본 일정도 바꾸고 이전 일정은 버전으로 남김
        const { versions = [], ...prev } = stop;
        versions.push({ req_ver: stop.req_ver, at: new Date().toISOString(), reason: '통화로 재방문 약속', snapshot: prev });
        Object.assign(stop, { versions, req_ver: stop.req_ver + 1, planned_date: date.value, eta: start.value || '', appt: start.value ? [start.value, end.value || null] : null, seq: nextSeq(date.value, stop.request_id) });
        await commit();
      }
      ui.openAppt = false; toast('재방문 약속을 저장했습니다'); render();
    }, 'primary big'));
}

async function savePhoto(stop, phase, file, lineId = null) {
  const bmp = await createImageBitmap(file);
  const scale = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
  const c = document.createElement('canvas'); c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
  c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
  const blob = await new Promise((res) => c.toBlob(res, 'image/jpeg', 0.8));
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const photo_id = crypto.randomUUID();
  await S.putSecureBytes('ph:' + photo_id, bytes);
  await addEvent(stop, 'PHOTO_META', { photo_id, phase, sha256: P.toHex(await P.sha256(bytes)), bytes: bytes.length, line_id: lineId || null });
}

// ------------------------------------------------------------ 품목 고르기 (현장 추가·접수 등록 공용)
// 검색어가 바뀌면 선택을 해제해 이전 단가가 잘못 쓰이지 않게 한다. 목록에 없는 품목은 "확인 대기"로만 추가한다.
function itemPicker(table, state, { onPick, onFree, addLabel = '추가' }) {
  const results = h('div', { class: 'results' });
  const qtyBox = h('div', { class: 'row' });
  const addBtn = btn(addLabel, null, 'primary big');
  const drawQty = () => { qtyBox.replaceChildren(h('span', {}, '수량'), stepper(state.qty, (n) => { state.qty = n; drawQty(); }, 1)); };
  // 검색어가 없을 때는 즐겨찾기와 자주 쓴 품목을 먼저 보여 준다(이 휴대폰 안에서만 집계)
  const quickRows = () => {
    const favs = st.feeFavs || [], use = st.feeUsage || {};
    const ids = [...favs, ...Object.keys(use).sort((a, b) => use[b] - use[a]).filter((x) => !favs.includes(x))].slice(0, 10);
    return ids.map((x) => table.rows.find((r) => r.id === x)).filter(Boolean);
  };
  const toggleFav = async (rid) => {
    const f = new Set(st.feeFavs || []);
    f.has(rid) ? f.delete(rid) : f.add(rid);
    st.feeFavs = [...f]; await commit(); drawResults();
  };
  const drawResults = () => {
    const quick = !state.q;
    const rows = quick ? quickRows() : searchFees(table, state.q).slice(0, 30);
    const favs = new Set(st.feeFavs || []);
    results.replaceChildren(
      ...(quick && rows.length ? [h('p', { class: 'small' }, '즐겨찾기·자주 쓴 품목')] : []),
      ...(state.q && !rows.length ? [h('p', { class: 'small' }, '검색 결과가 없습니다. 아래 [목록에 없음]으로 추가하세요.')] : []),
      ...rows.map((r) => h('div', { class: 'rrow' },
        btn(h('span', {}, h('strong', {}, `${r.name} ${r.spec}`),
          h('span', { class: 'p' }, `${won(r.price)} / ${r.unit_label}`), r.status === 'REVIEW' ? h('span', { class: 'p warnp' }, '규격 확인 필요 — 금액 확인 대기로 추가: ' + (r.note || '')) : null),
          () => { state.sel = { id: r.id, price: r.price }; drawResults(); }, 'result' + (state.sel && state.sel.id === r.id ? ' on' : '')),
        btn(favs.has(r.id) ? '★' : '☆', () => toggleFav(r.id), 'star' + (favs.has(r.id) ? ' on' : ''), { 'aria-label': favs.has(r.id) ? '즐겨찾기 해제' : '즐겨찾기' }))));
    addBtn.disabled = !state.sel;
  };
  const q = h('input', { type: 'search', placeholder: '품목명·규격·초성 (예: 2인용 매트리스, ㅈㄹ)', class: 'search', value: state.q, autocomplete: 'off' });
  q.addEventListener('input', () => { state.q = q.value; state.sel = null; drawResults(); });
  addBtn.addEventListener('click', async () => {
    const r = table.rows.find((x) => x.id === (state.sel && state.sel.id));
    if (!r || r.price !== state.sel.price) { state.sel = null; drawResults(); return toast('선택이 바뀌었습니다. 다시 골라 주세요.', 'bad'); }
    st.feeUsage = { ...(st.feeUsage || {}), [r.id]: ((st.feeUsage || {})[r.id] || 0) + 1 };
    await onPick(r, state.qty);
    state.q = ''; state.sel = null; state.qty = 1; q.value = ''; drawResults(); drawQty();
  });
  drawQty(); drawResults();

  const freeName = h('input', { type: 'text', placeholder: '품목 이름 (예: 안마기)', class: 'search' });
  const unitSel = h('select', {}, ...[['EA', '개'], ['PIECE', '쪽'], ['SET', '세트'], ['L20', '20ℓ당']].map(([v, t]) => h('option', { value: v }, t)));
  let freeQty = 1;
  const freeQtyBox = h('div', { class: 'row' });
  const drawFreeQty = () => freeQtyBox.replaceChildren(h('span', {}, '수량'), stepper(freeQty, (n) => { freeQty = n; drawFreeQty(); }, 1));
  drawFreeQty();
  const free = h('section', { class: 'card' }, h('h2', {}, '목록에 없음'),
    h('p', { class: 'small' }, '단가를 정하지 않고 "확인 대기"로 추가합니다. 0원으로 계산하지 않습니다.'),
    freeName, h('div', { class: 'row' }, h('span', {}, '단위'), unitSel), freeQtyBox,
    btn('확인 대기로 추가', async () => {
      if (!freeName.value.trim()) return toast('품목 이름을 적어 주세요', 'bad');
      await onFree(freeName.value.trim(), unitSel.value, unitSel.selectedOptions[0].textContent, freeQty);
      freeName.value = ''; freeQty = 1; drawFreeQty();
    }, 'big'));
  const search = h('section', { class: 'card' }, q, results, qtyBox, addBtn, h('p', { class: 'small' }, `수수료표: ${table.label}`));
  return { search, free, focus: () => q.focus(), pick: (r, qty) => onPick(r, qty) };
}

// ------------------------------------------------------------ 현장 품목 추가
function screenAdd(id) {
  const stop = findStop(id);
  if (!stop) { go('#/home'); return h('main'); }
  const a = ui.add && ui.add.stop === id ? ui.add : (ui.add = { stop: id, q: '', sel: null, qty: 1 });
  const table = feeTable();
  if (!table) return h('main', {}, h('header', { class: 'bar' }, btn('← 취소', () => go(`#/stop/${id}`), 'ghost')), h('p', { class: 'msg' }, ERR.FEE_VERSION_MISSING));
  const picker = itemPicker(table, a, {
    onPick: async (r, qty) => {
      // 같은 품목·규격을 이미 현장 추가했다면: 수량을 더할지(기존 행, 변경 기록 남김) 따로 둘지 고른다
      const prev = deriveStop(stop, st.events).lines.find((l) => l.kind === 'FIELD' && l.fee_rate_id === r.id);
      if (prev && !a.dupChoice) { a.dup = { r, qty, prev }; render(); return; }
      await addEvent(stop, 'FIELD_ITEM_ADD', { line_id: crypto.randomUUID(), fee_rate_id: r.id, fee_version_id: table.version_id, name: r.name, spec: r.spec, unit: r.unit, unit_label: r.unit_label, unit_price: r.price, price_status: r.status === 'CONFIRMED' ? 'CONFIRMED' : 'PENDING', qty });
      ui.add = null; go(`#/stop/${id}`);
    },
    onFree: async (name, unit, unit_label, qty) => {
      await addEvent(stop, 'FIELD_ITEM_ADD', { line_id: crypto.randomUUID(), fee_rate_id: null, name, spec: '', unit, unit_label, unit_price: null, price_status: 'PENDING', qty });
      ui.add = null; go(`#/stop/${id}`);
    },
  });
  if (a.dup) {
    const { r, qty, prev } = a.dup;
    return h('main', {},
      h('header', { class: 'bar' }, btn('← 취소', () => { a.dup = null; render(); }, 'ghost'), h('h1', { class: 'bar-title' }, '같은 품목이 있습니다')),
      h('section', { class: 'card notice' }, h('p', {}, `이미 현장에서 추가한 품목입니다: ${prev.name} ${prev.spec} ${prev.qty_confirmed}${prev.unit_label}`),
        h('p', { class: 'small' }, `이번에 고른 수량: ${qty}${r.unit_label}`)),
      btn(`기존 행에 더하기 (${prev.qty_confirmed} → ${prev.qty_confirmed + qty}${r.unit_label})`, async () => {
        await addEvent(stop, 'ITEM_RESULT', { line_id: prev.line_id, line_kind: 'FIELD', qty_confirmed: prev.qty_confirmed + qty, qty_collected: prev.qty_collected, reason: prev.reason || '' });
        ui.add = null; toast('수량을 더했습니다 (변경 기록 남음)'); go(`#/stop/${id}`);
      }, 'primary huge'),
      btn('따로 한 줄 더 추가', async () => { a.dupChoice = true; a.dup = null; await picker.pick(r, qty); }, 'huge'),
      h('p', { class: 'small' }, '어느 쪽이든 앞서 기록한 내용은 지워지지 않습니다.'));
  }
  setTimeout(picker.focus, 50);
  return h('main', {},
    h('header', { class: 'bar' }, btn('← 취소', () => { ui.add = null; go(`#/stop/${id}`); }, 'ghost'), h('h1', { class: 'bar-title' }, '현장 품목 추가')),
    picker.search, picker.free);
}

// ------------------------------------------------------------ 결과 보내기
async function screenExport() {
  const pc = await S.getRaw('pc'), dev = await S.getRaw('device');
  const pending = unsent();
  const photoMetas = st.events.filter((e) => e.kind === 'PHOTO_META');
  const msg = h('p', { class: 'msg' }, ui.exportMsg || '');
  ui.exportMsg = '';
  const say = (t) => { ui.exportMsg = t; msg.textContent = t; };
  // 결과 파일: 이름·연락처·주소·메모 없이 이벤트만, PC 공개키로 암호화 + 기기 서명
  const buildResult = async () => {
    const from = pending[0].seq, to = pending[pending.length - 1].seq;
    const payload = {
      schema: 'dg.result/1', work_date: st.daylist ? st.daylist.work_date : null, app_version: APP_VERSION,
      events: pending.map(({ event_id, seq, request_id, assign_ver, base_req_ver, kind, body, ts }) => ({ event_id, seq, request_id, assign_ver, base_req_ver, kind, body, device_ts: ts })),
      photos: pending.filter((e) => e.kind === 'PHOTO_META').map((e) => ({ photo_id: e.body.photo_id, request_id: e.request_id, phase: e.body.phase, sha256: e.body.sha256, bytes: e.body.bytes })),
    };
    const signRaw = await P.exportRaw(dev.sign.publicKey);
    const header = { btype: P.TYPE_RESULT, bundleId: P.randomBytes(16), deviceId: dev.id, assignVer: st.daylist ? st.daylist.assign_ver : 0, dataVer: 1, signerKeyId: await P.keyId(signRaw) };
    const sealed = await P.seal(payload, header, await P.importEcdhPub(pc.pcKexPub), dev.sign.privateKey);
    const name = `DG_R_${P.uuidStr(dev.id).slice(0, 8)}_${from}-${to}.dgr`;
    st.exports.push({ from, to, code: sealed.receipt, name, at: new Date().toISOString(), confirmed: false });
    await commit();
    return { sealed, name, bundleId: header.bundleId };
  };
  const showQr = btn('PC 화면 카메라로 보내기 (QR)', async () => {
    showQr.disabled = true;
    try {
      const { sealed, name, bundleId } = await buildResult();
      ui.resultQr = { frames: P.splitFrames(sealed.bytes, bundleId, 600), name };
      go('#/resultqr');
    } catch (e) { say('QR을 만들지 못했습니다: ' + errMsg(e)); showQr.disabled = false; }
  }, 'big', { disabled: !pending.length });
  const make = btn(pending.length ? `결과 파일 만들어 보내기 (${pending.length}건)` : '보낼 결과 없음', async () => {
    make.disabled = true; msg.textContent = '암호화하는 중…';
    try {
      const { sealed, name } = await buildResult();
      const file = new File([sealed.bytes], name, { type: 'application/octet-stream' });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: '수거 결과 파일' }); say('업무 메일로 보냈다면, PC 담당자가 알려 주는 확인 코드를 아래에 입력하세요.'); }
        catch (e) { say(e.name === 'AbortError' ? '보내기를 취소했습니다. 확인 코드를 받기 전에는 다시 만들어 보낼 수 있습니다.' : '공유 실패: ' + e.message); }
      } else {
        const u = URL.createObjectURL(file);
        const a = h('a', { href: u, download: name }); document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(u), 30000);
        say('파일을 저장했습니다. 업무 메일에 첨부해 보내 주세요.');
      }
    } catch (e) { say('파일을 만들지 못했습니다: ' + errMsg(e)); }
    make.disabled = false; render();
  }, 'primary huge', { disabled: !pending.length });

  const codeIn = h('input', { type: 'text', placeholder: '확인 코드 8자리', class: 'code-in', autocapitalize: 'characters', autocomplete: 'off' });
  const confirm = btn('확인 코드 입력', async () => {
    const c = codeIn.value.replace(/\s/g, '').toUpperCase();
    const ex = st.exports.find((x) => x.code === c);
    if (!ex) return toast('맞는 확인 코드가 없습니다.', 'bad');
    ex.confirmed = true; st.releasedSeq = Math.max(st.releasedSeq, ex.to);
    await commit(); toast('PC 반영이 확인되었습니다'); render();
  }, 'big');

  // 사진: 결과 파일과 따로, PC 공개키로 암호화한 사진 파일(.dgp)을 승인된 보안 매체로 옮긴다.
  // PC가 해시를 대조하고 알려 주는 "사진 회수 확인 코드"를 넣은 사진만 이 휴대폰에서 지울 수 있다.
  const recovered = new Set(st.recoveredPhotos || []);
  const allPhotos = [...photoMetas.map((e) => ({ ...e.body, request_id: e.request_id })), ...(st.keptPhotos || [])];
  const notRecovered = allPhotos.filter((p) => !recovered.has(p.photo_id));
  const pmsg = h('p', { class: 'msg' });
  const makePhotos = btn(`사진 파일 만들기 (${notRecovered.length}장)`, async () => {
    makePhotos.disabled = true; pmsg.textContent = '암호화하는 중…';
    try {
      const photos = [];
      for (const p of notRecovered) {
        const b = await S.getSecureBytes('ph:' + p.photo_id);
        if (b) photos.push({ photo_id: p.photo_id, request_id: p.request_id, phase: p.phase, sha256: p.sha256, data: B.b64(b) });
      }
      const signRaw = await P.exportRaw(dev.sign.publicKey);
      const header = { btype: 4, bundleId: P.randomBytes(16), deviceId: dev.id, assignVer: 0, dataVer: 1, signerKeyId: await P.keyId(signRaw) };
      const sealed = await P.seal({ schema: 'dg.photos/1', photos }, header, await P.importEcdhPub(pc.pcKexPub), dev.sign.privateKey);
      const name = `DG_P_${P.uuidStr(dev.id).slice(0, 8)}_${new Date().toISOString().slice(0, 16).replace(/\D/g, '')}.dgp`;
      st.photoExports = [...(st.photoExports || []), { code: sealed.receipt, ids: photos.map((p) => p.photo_id), name, at: new Date().toISOString() }];
      await commit();
      const u = URL.createObjectURL(new File([sealed.bytes], name, { type: 'application/octet-stream' }));
      const a = h('a', { href: u, download: name }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 30000);
      pmsg.textContent = `${name} (${(sealed.bytes.length / 1024).toFixed(0)}KB)을 저장했습니다. 승인된 보안 매체로 PC에 옮긴 뒤 확인 코드를 받으세요.`;
    } catch (e) { pmsg.textContent = '사진 파일을 만들지 못했습니다: ' + errMsg(e); }
    makePhotos.disabled = false;
  }, 'big', { disabled: !notRecovered.length });
  const pcode = h('input', { type: 'text', placeholder: '사진 회수 확인 코드 8자리', class: 'code-in', autocapitalize: 'characters', autocomplete: 'off' });
  const pconfirm = btn('사진 회수 확인', async () => {
    const c = pcode.value.replace(/\s/g, '').toUpperCase();
    const ex = (st.photoExports || []).find((x) => x.code === c);
    if (!ex) return toast('맞는 사진 회수 확인 코드가 없습니다.', 'bad');
    st.recoveredPhotos = [...new Set([...(st.recoveredPhotos || []), ...ex.ids])];
    await commit(); toast(`사진 ${ex.ids.length}장 회수가 확인되었습니다`); render();
  }, 'big');

  const allConfirmed = !unsent().length && st.exports.every((x) => x.confirmed);
  const cleanup = btn('오늘 자료 정리', async () => {
    if (!confirm2('반영이 확인된 목록과 기록을 이 휴대폰에서 지웁니다. 회수가 확인된 사진만 지우고, 나머지 사진은 남깁니다. 계속할까요?')) return;
    for (const p of allPhotos) if (recovered.has(p.photo_id)) await S.delSecureBytes('ph:' + p.photo_id);
    st = { ...emptyState(), mode: st.mode, mapEnabled: st.mapEnabled, textSize: st.textSize, seq: st.seq, releasedSeq: st.seq, keptPhotos: notRecovered, photoExports: st.photoExports, recoveredPhotos: [] };
    await commit(); toast('정리했습니다'); go('#/home');
  }, 'ghost', { disabled: !allConfirmed || !st.daylist });

  return h('main', {},
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '결과 보내기')),
    h('section', { class: 'card' },
      h('p', {}, '결과 파일에는 이름·연락처·주소·메모가 들어가지 않으며, PC에서만 열 수 있게 암호화됩니다. 사진은 넣지 않습니다.'),
      make, showQr, h('p', { class: 'small' }, '업무 메일을 쓸 수 없으면 [PC 화면 카메라로 보내기]로 PC 웹캠에 QR을 보여 주세요.'), msg),
    h('section', { class: 'card' }, h('h2', {}, 'PC 반영 확인'), codeIn, confirm,
      h('ul', { class: 'small list' }, ...st.exports.slice(-5).reverse().map((x) => h('li', {}, `${x.name} · ${x.confirmed ? '반영 확인됨' : '확인 대기'}`)))),
    h('section', { class: 'card' }, h('h2', {}, `사진 ${allPhotos.length}장 · 미회수 ${notRecovered.length}장`),
      h('p', { class: 'small' }, '사진 파일은 PC에서만 열 수 있게 암호화됩니다. 업무 메일이 아니라 승인된 보안 매체로 옮기세요. 회수가 확인되기 전에는 휴대폰에서 지우지 않습니다.'),
      makePhotos, pmsg, pcode, pconfirm),
    h('section', { class: 'card' }, h('h2', {}, '정리'), cleanup,
      allConfirmed ? null : h('p', { class: 'small' }, '모든 결과의 PC 반영이 확인되어야 정리할 수 있습니다.')));
}
const confirm2 = (m) => window.confirm(m);

// ------------------------------------------------------------ 사용 방식 선택
function screenMode() {
  const pick = async (mode) => {
    if (mode === 'SOLO' && !confirm2('이 휴대폰만으로 쓰면 접수 원본이 이 휴대폰에만 저장됩니다.\n분실·고장·데이터 삭제 시 원본까지 사라지므로 매일 백업 파일을 만들어 안전한 곳에 보관해야 합니다.\n계속할까요?')) return;
    st.mode = mode; await commit(); go('#/home'); render();
  };
  return h('main', {},
    h('header', { class: 'bar' }, h('h1', { class: 'bar-title' }, '사용 방식 선택')),
    h('section', { class: 'card' },
      h('h2', {}, 'PC와 연결해서 사용 (권장)'),
      h('p', {}, 'PC가 접수 원본을 갖고, 휴대폰은 QR로 오늘 목록을 받아 현장 기록 후 결과 파일을 보냅니다.'),
      btn('PC와 연결해서 사용', () => pick('PC'), 'primary big')),
    h('section', { class: 'card' },
      h('h2', {}, '이 휴대폰만으로 사용'),
      h('p', {}, '전화 접수 등록부터 연락·수거 기록까지 이 휴대폰에서 모두 합니다. PC가 필요 없습니다.'),
      h('p', { class: 'small' }, '원본이 이 휴대폰에만 있으므로 매일 암호화 백업이 필요합니다. 여러 휴대폰이 같은 접수를 나눠 처리할 수는 없습니다.'),
      btn('이 휴대폰만으로 사용', () => pick('SOLO'), 'big')),
    h('p', { class: 'small' }, '사용 방식은 자료가 없을 때만 설정 화면에서 바꿀 수 있습니다.'));
}

// ------------------------------------------------------------ 단독 모드 홈
function screenHomeSolo() {
  const today = todayKST(), tomorrow = todayKST(1);
  const view = ui.soloView || 'TODAY';
  const all = st.requests.map((s) => ({ s, d: deriveStop(s, st.events) }));
  const live = all.filter((x) => x.s.status !== 'CANCELLED');
  const open = live.filter((x) => x.d.progress !== 'DONE');
  const lists = {
    TODAY: live.filter((x) => x.s.planned_date === today),
    TOMORROW: live.filter((x) => x.s.planned_date === tomorrow),
    OPEN: open,
    CANCELLED: all.filter((x) => x.s.status === 'CANCELLED'),
  };
  const list = filterQ(lists[view].slice().sort((a, b) => (a.s.planned_date || '').localeCompare(b.s.planned_date || '') || a.s.seq - b.s.seq));
  const needBackup = st.requests.length && (!st.lastBackupAt || Date.now() - Date.parse(st.lastBackupAt) > BACKUP_WARN_MS);
  const move = async (item, dir) => {
    const same = lists[view].map((x) => x.s).filter((r) => r.planned_date === item.planned_date).sort((a, b) => a.seq - b.seq);
    const i = same.indexOf(item), j = i + dir;
    if (j < 0 || j >= same.length) return;
    [same[i].seq, same[j].seq] = [same[j].seq, same[i].seq];
    await commit(); render();
  };
  const out = [
    h('header', { class: 'bar' }, h('h1', { class: 'bar-title' }, '동구 수거'), h('span', { class: 'small' }, '단독'), btn('잠금', lockNow, 'ghost')),
    needBackup ? h('section', { class: 'card notice' },
      h('p', {}, `마지막 백업: ${ago(st.lastBackupAt)}. 휴대폰을 잃어버리거나 고장 나면 접수 원본이 사라집니다.`),
      btn('지금 백업', () => go('#/backup'), 'primary')) : null,
    h('section', { class: 'actions' },
      btn('+ 전화 접수 등록', () => { ui.form = null; go('#/intake'); }, 'primary huge'),
      h('div', { class: 'grid2' }, btn('백업·복원', () => go('#/backup'), 'big'), btn(`하차 기록${st.events.some((e) => e.kind === 'UNLOAD') ? ` (${st.events.filter((e) => e.kind === 'UNLOAD').length}회)` : ''}`, () => go('#/unload'), 'big'))),
    h('div', { class: 'tabs', role: 'group', 'aria-label': '목록 거르기' }, ...[['TODAY', `오늘 ${lists.TODAY.length}`], ['TOMORROW', `내일 ${lists.TOMORROW.length}`], ['OPEN', `미처리 ${open.length}`], ['CANCELLED', `취소 ${lists.CANCELLED.length}`]]
      .map(([k, t]) => btn(t, () => { ui.soloView = k; ui.reorder = false; render(); }, view === k ? 'tab on' : 'tab'))),
  ];
  if (view === 'TODAY' && lists.TODAY.length) out.splice(2, 0, daySummary(lists.TODAY));
  out.push(stopSearch(lists[view].length));
  if (!list.length) out.push(h('p', { class: 'empty' }, ui.stopQ ? `'${ui.stopQ}'에 맞는 접수가 없습니다.` : view === 'TODAY' ? '오늘 수거 예정 접수가 없습니다. [+ 전화 접수 등록]으로 시작하세요.' : '해당 접수가 없습니다.'));
  else {
    if (st.mapEnabled && view !== 'CANCELLED') out.push(btn('🗺 지도로 보기', () => go('#/map'), 'big'));
    if (view === 'TODAY' || view === 'TOMORROW') out.push(btn(ui.reorder ? '순서 정하기 끝' : '방문 순서 바꾸기', () => { ui.reorder = !ui.reorder; render(); }, 'ghost'));
    out.push(h('ul', { class: 'stops' + (view === 'TODAY' || view === 'TOMORROW' ? ' route' : '') }, list.map(({ s, d }) => h('li', { class: 'srow' },
      h('a', { href: `#/stop/${s.request_id}`, class: `stop p-${s.status === 'CANCELLED' ? 'CANCELLED' : d.outcome || d.progress}` + (d.progress === 'DONE' || s.status === 'CANCELLED' ? ' done' : '') },
        h('span', { class: 'seq' }, s.seq),
        h('span', { class: 'body' },
          h('strong', {}, s.name), h('span', { class: 'addr' }, s.address),
          h('span', { class: 'items' }, s.items.map((i) => `${i.name} ${i.spec} ${i.qty}${i.unit_label}`).join(', ')),
          h('span', { class: 'chips' },
            view === 'OPEN' ? h('em', { class: 'chip' }, s.planned_date) : null,
            h('em', { class: 'chip c-' + d.contact }, LABEL.contact[d.contact]),
            h('em', { class: 'chip p-' + d.progress }, s.status === 'CANCELLED' ? '취소' : LABEL.progress[d.progress]),
            d.fee.pendingCount ? h('em', { class: 'chip warn' }, '금액 확인 대기') : null)),
        h('span', { class: 'eta' }, s.eta || '')),
      ui.reorder ? h('span', { class: 'mv' }, btn('▲', () => move(s, -1), 'step', { 'aria-label': '위로' }), btn('▼', () => move(s, 1), 'step', { 'aria-label': '아래로' })) : null))));
  }
  out.push(h('footer', { class: 'foot' }, h('a', { href: '#/settings' }, '설정·정보')));
  return h('main', {}, ...out);
}

// ------------------------------------------------------------ 단독 모드 접수 등록·수정
const digits = (s) => (s || '').replace(/\D/g, '');
const normAddr = (s) => (s || '').replace(/\s+/g, '');
function lineFromRow(r, qty, table) {
  return { line_id: crypto.randomUUID(), fee_rate_id: r.id, fee_version_id: table.version_id, name: r.name, spec: r.spec, unit: r.unit, unit_label: r.unit_label,
    qty, unit_price: r.price, amount: r.status === 'CONFIRMED' ? r.price * qty : null, price_status: r.status === 'CONFIRMED' ? 'CONFIRMED' : 'PENDING' };
}
function screenIntake(id) {
  const editing = id ? findStop(id) : null;
  if (id && !editing) { go('#/home'); return h('main'); }
  const table = BUILTIN_FEES;
  const locked = new Set(editing ? st.events.filter((e) => e.request_id === id && e.kind === 'ITEM_RESULT').map((e) => e.body.line_id) : []);
  if (!ui.form || ui.form.id !== (id || null)) {
    const src = editing || st.intakeDraft || {};
    ui.form = { id: id || null, name: src.name || '', phone: src.phone || '', address: src.address || '', spot_note: src.spot_note || '', memo: src.memo || '',
      planned_date: src.planned_date || todayKST(1), eta: src.eta || '', items: JSON.parse(JSON.stringify(src.items || [])), reason: '', dupAck: false,
      pick: { q: '', sel: null, qty: 1 } };
  }
  const f = ui.form;
  let draftTimer = null;
  const saveDraft = () => { if (editing) return; clearTimeout(draftTimer); draftTimer = setTimeout(() => { const { pick, dupAck, reason, id: _i, ...d } = f; st.intakeDraft = d; commit(); }, 600); };
  const field = (key, label, attrs = {}) => {
    const input = h(attrs.multiline ? 'textarea' : 'input', { ...attrs, multiline: null, class: 'search', value: attrs.multiline ? null : f[key], rows: attrs.multiline ? 2 : null });
    if (attrs.multiline) input.value = f[key];
    input.addEventListener('input', () => { f[key] = input.value; f.dupAck = false; saveDraft(); });
    return h('label', { class: 'fld' }, h('span', {}, label), input);
  };
  const itemsBox = h('div', {});
  const totalBox = h('p', { class: 'small' });
  const drawItems = () => {
    itemsBox.replaceChildren(...(f.items.length ? f.items.map((it, i) => h('div', { class: 'line orig' },
      h('div', { class: 'lhead' }, h('strong', {}, `${it.name} ${it.spec}`), locked.has(it.line_id) ? h('em', { class: 'tag' }, '현장 기록 있음') :
        btn('삭제', () => { f.items.splice(i, 1); drawItems(); saveDraft(); }, 'ghost')),
      h('div', { class: 'price' }, it.price_status === 'CONFIRMED' ? `${won(it.unit_price)} / ${it.unit_label}` : it.unit_price != null ? `금액 확인 대기 (원문 ${won(it.unit_price)} / ${it.unit_label})` : '금액 확인 대기'),
      h('div', { class: 'row' }, h('span', {}, '수량'), locked.has(it.line_id) ? h('span', { class: 'val' }, it.qty) :
        stepper(it.qty, (n) => { it.qty = n; it.amount = it.price_status === 'CONFIRMED' ? it.unit_price * n : null; drawItems(); saveDraft(); }, 1)))) :
      [h('p', { class: 'small' }, '아래에서 품목을 검색해 추가하세요.')]));
    const pend = f.items.filter((x) => x.price_status !== 'CONFIRMED').length;
    const sum = f.items.filter((x) => x.price_status === 'CONFIRMED').reduce((a, x) => a + x.amount, 0);
    totalBox.textContent = `접수 금액: ${won(sum)}${pend ? ` + 확인 대기 ${pend}건 (최종 금액 미확정)` : ''}`;
  };
  drawItems();
  const picker = itemPicker(table, f.pick, {
    addLabel: '품목 추가',
    onPick: async (r, qty) => { f.items.push(lineFromRow(r, qty, table)); drawItems(); saveDraft(); },
    onFree: async (name, unit, unit_label, qty) => { f.items.push({ line_id: crypto.randomUUID(), fee_rate_id: null, name, spec: '', unit, unit_label, qty, unit_price: null, amount: null, price_status: 'PENDING' }); drawItems(); saveDraft(); },
  });
  const msg = h('p', { class: 'msg' });
  const dupBox = h('div', {});
  const save = btn(editing ? '수정 저장' : '접수 등록', async () => {
    const phone = digits(f.phone);
    if (!f.name.trim()) return (msg.textContent = '이름을 적어 주세요.');
    if (!/^0\d{8,10}$/.test(phone)) return (msg.textContent = '연락처를 확인해 주세요. (0으로 시작하는 9~11자리)');
    if (!f.address.trim()) return (msg.textContent = '배출 주소를 적어 주세요.');
    if (!f.planned_date) return (msg.textContent = '수거 예정일을 골라 주세요.');
    if (!f.items.length) return (msg.textContent = '버릴 물건을 하나 이상 추가해 주세요.');
    if (editing && !f.reason.trim()) return (msg.textContent = '수정 사유를 적어 주세요.');
    if (!editing && !f.dupAck) {
      const dups = st.requests.filter((r) => r.status !== 'CANCELLED' && deriveStop(r, st.events).progress !== 'DONE' && (r.phone === phone || normAddr(r.address) === normAddr(f.address)));
      if (dups.length) {
        f.dupAck = true;
        dupBox.replaceChildren(h('section', { class: 'card notice' },
          h('p', {}, `비슷한 접수가 ${dups.length}건 있습니다. 같은 건이면 등록하지 말고 기존 접수를 수정하세요.`),
          h('ul', { class: 'small list' }, ...dups.map((r) => h('li', {}, `${r.receipt_no} · ${r.name} · ${fmtPhone(r.phone)} · ${r.address} · ${r.planned_date}`))),
          h('p', {}, '별개 접수가 맞으면 [접수 등록]을 한 번 더 누르세요. (자동으로 합치지 않습니다)')));
        dupBox.scrollIntoView({ block: 'center' });
        return;
      }
    }
    const now = new Date().toISOString();
    const data = { name: f.name.trim(), phone, address: f.address.trim(), spot_note: f.spot_note.trim(), memo: f.memo.trim(), planned_date: f.planned_date, eta: f.eta, items: f.items };
    if (editing) {
      const { versions = [], ...prev } = editing;
      versions.push({ req_ver: editing.req_ver, at: now, reason: f.reason.trim(), snapshot: prev });
      const moved = editing.planned_date !== data.planned_date;
      Object.assign(editing, data, { req_ver: editing.req_ver + 1, versions, updated_at: now });
      if (moved) editing.seq = nextSeq(data.planned_date, editing.request_id);
      await commit(); ui.form = null; toast('수정했습니다'); go(`#/stop/${id}`);
    } else {
      const rd = todayKST();
      const n = (st.receiptCounter[rd] || 0) + 1; st.receiptCounter[rd] = n;
      const req = { request_id: crypto.randomUUID(), receipt_no: `DG-${rd.replace(/-/g, '')}-${String(n).padStart(4, '0')}`, req_ver: 1, assign_ver: 1,
        ...data, seq: nextSeq(data.planned_date), status: 'ACTIVE', received_at: now, updated_at: now, versions: [] };
      st.requests.push(req); st.intakeDraft = null;
      await commit(); ui.form = null; toast(`${req.receipt_no} 등록했습니다`); go('#/home');
    }
  }, 'primary huge');
  return h('main', {},
    h('header', { class: 'bar' }, btn('← 취소', () => { ui.form = null; go(editing ? `#/stop/${id}` : '#/home'); }, 'ghost'), h('h1', { class: 'bar-title' }, editing ? `접수 수정 ${editing.receipt_no}` : '전화 접수 등록')),
    !editing && st.intakeDraft ? h('p', { class: 'small' }, '작성 중이던 내용을 불러왔습니다. ',
      btn('지우고 새로 쓰기', async () => { st.intakeDraft = null; await commit(); ui.form = null; render(); }, 'ghost')) : null,
    h('section', { class: 'card' },
      field('name', '이름', { autocomplete: 'off' }),
      field('phone', '연락처', { type: 'tel', inputmode: 'tel', autocomplete: 'off', placeholder: '010-0000-0000' }),
      field('address', '배출 주소', { autocomplete: 'off' }),
      field('spot_note', '상세 배출 위치', { autocomplete: 'off', placeholder: '예: 101동 분리수거장 옆' }),
      field('memo', '요청 메모', { multiline: true }),
      h('div', { class: 'grid2' }, field('planned_date', '수거 예정일', { type: 'date' }), field('eta', '방문 시각(선택)', { type: 'time' }))),
    h('section', { class: 'card' }, h('h2', {}, '버릴 물건'), itemsBox, totalBox),
    picker.search, picker.free,
    editing ? h('section', { class: 'card' }, field('reason', '수정 사유 (필수)', { autocomplete: 'off' })) : null,
    dupBox, save, msg);
}
function nextSeq(date, exceptId) {
  return st.requests.filter((r) => r.planned_date === date && r.request_id !== exceptId).reduce((m, r) => Math.max(m, r.seq || 0), 0) + 1;
}

// ------------------------------------------------------------ 단독 모드 백업·복원
async function screenBackup() {
  const photoIds = st.events.filter((e) => e.kind === 'PHOTO_META').map((e) => e.body.photo_id);
  const msg = h('p', { class: 'msg' });
  const pw1 = h('input', { type: 'password', placeholder: '백업 비밀번호 (8자 이상)', class: 'search', autocomplete: 'new-password' });
  const pw2 = h('input', { type: 'password', placeholder: '한 번 더', class: 'search', autocomplete: 'new-password' });
  const withPhotos = h('input', { type: 'checkbox', checked: true });
  const make = btn('암호화 백업 파일 만들기', async () => {
    if (pw1.value.length < 8) return (msg.textContent = '백업 비밀번호는 8자 이상으로 정해 주세요.');
    if (pw1.value !== pw2.value) return (msg.textContent = '두 번 입력한 비밀번호가 다릅니다.');
    make.disabled = true; msg.textContent = '암호화하는 중…';
    try {
      const photos = {};
      if (withPhotos.checked) for (const pid of photoIds) { const b = await S.getSecureBytes('ph:' + pid); if (b) photos[pid] = B.b64(b); }
      const at = new Date().toISOString();
      const { intakeDraft, ...state } = st;
      const bytes = await B.makeBackup({ format: 'dg.backup/1', created_at: at, app_version: APP_VERSION, state, photos }, pw1.value);
      const name = `DG_B_${todayKST().replace(/-/g, '')}_${at.slice(11, 16).replace(':', '')}.dgb`;
      const file = new File([bytes], name, { type: 'application/octet-stream' });
      let sent = false;
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: '수거 업무 백업' }); sent = true; }
        catch (e) { if (e.name === 'AbortError') { msg.textContent = '백업을 취소했습니다.'; make.disabled = false; return; } }
      }
      if (!sent) { const u = URL.createObjectURL(file); const a = h('a', { href: u, download: name }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 30000); }
      st.lastBackupAt = at; await commit();
      pw1.value = pw2.value = '';
      msg.textContent = `백업 파일 ${name} (${(bytes.length / 1024).toFixed(0)}KB)을 만들었습니다. 업무 메일이나 승인된 저장 매체에 보관하세요.`;
    } catch (e) { msg.textContent = '백업 실패: ' + errMsg(e); }
    make.disabled = false;
  }, 'primary big');

  const fileIn = h('input', { type: 'file', accept: '.dgb,application/octet-stream' });
  const rpw = h('input', { type: 'password', placeholder: '백업 비밀번호', class: 'search', autocomplete: 'off' });
  const rmsg = h('p', { class: 'msg' });
  const restore = btn('백업에서 복원', async () => {
    const file = fileIn.files && fileIn.files[0];
    if (!file) return (rmsg.textContent = '백업 파일을 골라 주세요.');
    rmsg.textContent = '확인하는 중…';
    try {
      const obj = await B.openBackup(new Uint8Array(await file.arrayBuffer()), rpw.value);
      const n = (obj.state.requests || []).length;
      if (!confirm2(`${obj.created_at.slice(0, 16).replace('T', ' ')} 백업 (접수 ${n}건)으로 이 휴대폰 자료를 바꿉니다. 지금 휴대폰에만 있는 기록은 사라집니다. 계속할까요?`)) { rmsg.textContent = ''; return; }
      for (const [pid, b] of Object.entries(obj.photos || {})) await S.putSecureBytes('ph:' + pid, B.unb64(b));
      st = { ...emptyState(), ...obj.state, lastBackupAt: obj.created_at };
      await commit(); toast(`복원했습니다 (접수 ${n}건)`); go('#/home');
    } catch (e) { rmsg.textContent = errMsg(e); }
  }, 'big');

  return h('main', {},
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '백업·복원')),
    h('section', { class: 'card' }, h('h2', {}, '백업 만들기'),
      h('p', { class: 'small' }, `마지막 백업: ${ago(st.lastBackupAt)} · 접수 ${st.requests.length}건 · 사진 ${photoIds.length}장`),
      h('p', { class: 'small' }, '백업 파일은 백업 비밀번호로 암호화됩니다. 비밀번호를 잊으면 복원할 수 없으니 기관 기준에 따라 따로 보관하세요. 개인 메일·메신저·공개 클라우드에는 올리지 마세요.'),
      pw1, pw2, h('label', { class: 'check' }, withPhotos, ' 사진 포함'), make, msg),
    h('section', { class: 'card' }, h('h2', {}, 'PC로 옮기기'), h('p', { class: 'small' }, '나중에 PC 프로그램을 쓰게 되면, 이 백업 파일을 승인된 보안 매체로 PC에 옮긴 뒤 PC [결과 가져오기 → 휴대폰 단독 방식 자료 옮기기]에서 백업 비밀번호로 열면 접수·현장 기록·사진·하차 기록이 PC 원본으로 들어갑니다. 이미 PC에 있는 접수는 덮어쓰지 않습니다.')),
    h('section', { class: 'card' }, h('h2', {}, '복원 (새 휴대폰으로 옮길 때)'), fileIn, rpw, restore, rmsg));
}

// ------------------------------------------------------------ 지도 (온라인 바탕 지도, 개인정보는 보내지 않음)
const mapNotice = () => h('p', { class: 'small' }, '바탕 지도 조각만 OpenFreeMap에서 받습니다. 이름·연락처·주소와 핀 위치는 휴대폰 밖으로 보내지 않습니다.');
function mapStops() {
  if (isSolo()) {
    const view = ui.soloView || 'TODAY', today = todayKST(), tomorrow = todayKST(1);
    return st.requests.filter((r) => r.status !== 'CANCELLED' && (view === 'TODAY' ? r.planned_date === today : view === 'TOMORROW' ? r.planned_date === tomorrow : true));
  }
  return st.daylist ? st.daylist.stops.filter((s) => !s.removed) : [];
}

async function screenMap(focusId) {
  const back = h('header', { class: 'bar' }, btn(focusId ? '← 접수' : '← 목록', () => go(focusId ? `#/stop/${focusId}` : '#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '지도'));
  if (!st.mapEnabled) return h('main', {}, back, h('p', { class: 'empty' }, '설정에서 지도 사용이 꺼져 있습니다.'));
  const items = mapStops().map((s) => ({ s, d: deriveStop(s, st.events) }));
  const placed = items.filter((x) => x.d.loc), missing = items.filter((x) => !x.d.loc);
  const box = h('div', { class: 'mapbox', id: 'map' });
  const msg = h('p', { class: 'msg' });
  const view = h('main', {}, back, box,
    h('div', { class: 'grid2' }, btn('📍 내 위치', async () => {
      try { const p = await M.myPosition(); if (liveMap) { liveMap.flyTo({ center: p, zoom: 16 }); M.pin(ui.ml, liveMap, { lng: p[0], lat: p[1], label: '나', cls: 'me' }); } }
      catch (e) { msg.textContent = e.message; }
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
      liveMap = map; ui.ml = ml;
      for (const { s, d } of placed) {
        M.pin(ml, map, { lng: d.loc.lon, lat: d.loc.lat, label: String(s.seq), cls: 'p-' + d.progress + (s.request_id === focusId ? ' focus' : ''), onClick: () => go(`#/stop/${s.request_id}`) });
      }
      if (!focus) fitAll();
    } catch (e) { msg.textContent = '지도를 불러오지 못했습니다. 인터넷 연결을 확인하세요.'; }
  }, 0);
  return view;
}

async function screenPick(id) {
  const stop = findStop(id);
  if (!stop) { go('#/home'); return h('main'); }
  const d = deriveStop(stop, st.events);
  const box = h('div', { class: 'mapbox pick', id: 'map' }, h('div', { class: 'crosshair', 'aria-hidden': 'true' }));
  const msg = h('p', { class: 'msg' });
  const save = btn('이 위치로 저장', async () => {
    if (!liveMap) return;
    const c = liveMap.getCenter();
    const lat = Math.round(c.lat * 1e6) / 1e6, lon = Math.round(c.lng * 1e6) / 1e6;
    if (isSolo()) { stop.lat = lat; stop.lon = lon; stop.geo_source = 'MANUAL'; stop.updated_at = new Date().toISOString(); await commit(); }
    else await addEvent(stop, 'LOCATION_FIX', { lat, lon, source: 'MANUAL' }); // PC 방식: 결과 파일로 PC에 전달
    toast('위치를 저장했습니다'); go(`#/stop/${id}`);
  }, 'primary big', { disabled: true });
  setTimeout(async () => {
    try {
      const { map, ml } = await M.createMap(box, { center: d.loc ? [d.loc.lon, d.loc.lat] : M.DEFAULT_CENTER, zoom: d.loc ? 18 : 15 });
      liveMap = map; ui.ml = ml;
      // 지도가 실제로 보일 때만(또는 원래 위치가 있거나 내 위치를 잡았을 때) 저장할 수 있게: 빈 화면에서 기본 좌표를 저장하지 않도록
      const ready = () => { save.disabled = false; const e = box.querySelector('.map-empty'); if (e) e.remove(); };
      if (d.loc) ready();
      map.on('data', (e) => { if (e.dataType === 'source' && e.tile) ready(); });
      map.once('idle', ready);
      setTimeout(() => { if (save.disabled) box.append(h('div', { class: 'map-empty' }, '지도가 아직 안 보입니다. 인터넷 연결을 확인하거나 [지금 내 위치로 이동]을 누르세요.')); }, 8000);
      ui.pickReady = ready;
    } catch { msg.textContent = '지도를 불러오지 못했습니다. 인터넷 연결을 확인하세요.'; }
  }, 0);
  return h('main', {},
    h('header', { class: 'bar' }, btn('← 취소', () => go(`#/stop/${id}`), 'ghost'), h('h1', { class: 'bar-title' }, '배출 위치 지정')),
    h('p', {}, `${stop.name} · ${stop.address}`),
    h('p', { class: 'small' }, '지도를 움직여 가운데 표시를 실제 배출 위치에 맞추세요. 주소로 위치를 검색하지 않습니다(주소를 외부로 보내지 않기 위해).'),
    box,
    btn('지금 내 위치로 이동', async () => { try { const p = await M.myPosition(); if (liveMap) { liveMap.jumpTo({ center: p, zoom: 18 }); if (ui.pickReady) ui.pickReady(); } } catch (e) { msg.textContent = e.message; } }, 'big'),
    save, msg, mapNotice());
}

// ------------------------------------------------------------ 결과 QR 표시 (역방향: 휴대폰 → PC 웹캠)
function drawQr(canvas, text) {
  const q = qrcode(0, 'M');
  q.addData(text, 'Alphanumeric');
  q.make();
  const n = q.getModuleCount(), quiet = 4, size = Math.min(canvas.parentElement ? canvas.parentElement.clientWidth : 360, 520);
  const cell = Math.floor(size / (n + quiet * 2));
  canvas.width = canvas.height = cell * (n + quiet * 2);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#000';
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (q.isDark(r, c)) ctx.fillRect((c + quiet) * cell, (r + quiet) * cell, cell, cell);
}
function screenResultQr() {
  const r = ui.resultQr;
  if (!r) { go('#/export'); return h('main'); }
  const canvas = h('canvas', { class: 'qrcanvas', 'aria-label': '결과 QR' });
  const count = h('p', { class: 'count' });
  let i = 0, fps = 3, timer = null;
  const show = () => { if (!canvas.isConnected) return clearTimeout(timer); drawQr(canvas, r.frames[i]); count.textContent = `${i + 1} / ${r.frames.length}`; i = (i + 1) % r.frames.length; timer = setTimeout(show, 1000 / fps); };
  setTimeout(show, 0);
  return h('main', { class: 'center' },
    h('header', { class: 'bar' }, btn('← 결과 보내기', () => { clearTimeout(timer); ui.resultQr = null; go('#/export'); }, 'ghost'), h('h1', { class: 'bar-title' }, '결과 QR')),
    h('div', { class: 'qrbox' }, canvas), count,
    h('div', { class: 'grid2' }, btn('느리게', () => { fps = Math.max(1, fps - 1); }, 'opt'), btn('빠르게', () => { fps = Math.min(8, fps + 1); }, 'opt')),
    h('p', {}, 'PC [결과 가져오기] → [웹캠으로 읽기]를 누르고 이 화면을 웹캠에 비추세요. 화면 밝기를 최대로 하면 잘 읽힙니다.'),
    h('p', { class: 'small' }, `${r.name} · 이름·연락처·주소는 들어 있지 않습니다. PC가 알려 주는 확인 코드를 [결과 보내기]에 입력하세요.`));
}

// ------------------------------------------------------------ 설정
async function screenSettings() {
  const dev = await S.getRaw('device');
  let persisted = '확인 불가';
  try { persisted = navigator.storage && navigator.storage.persisted ? ((await navigator.storage.persisted()) ? '보존 요청됨' : '보존 요청 안 됨') : '지원 안 함'; } catch {}
  let quota = '';
  try { const e = await navigator.storage.estimate(); quota = `${(e.usage / 1048576).toFixed(1)}MB 사용`; } catch {}
  return h('main', {},
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '설정·정보')),
    h('section', { class: 'card' }, h('dl', {},
      h('dt', {}, '앱 버전'), h('dd', {}, APP_VERSION),
      h('dt', {}, '사용 방식'), h('dd', {}, st.mode === 'SOLO' ? '이 휴대폰만 사용' : 'PC와 연결'),
      h('dt', {}, '기기 번호'), h('dd', {}, dev ? P.uuidStr(dev.id).slice(0, 8) : '미등록'),
      h('dt', {}, '저장소'), h('dd', {}, `${persisted} ${quota}`),
      h('dt', {}, '미전송 기록'), h('dd', {}, unsent().length + '건'))),
    h('section', { class: 'card' }, h('h2', {}, '글씨 크기'),
      h('div', { class: 'grid2' }, ...[['', '보통'], ['big', '크게']].map(([k, t]) => btn(t, async () => { st.textSize = k; applyTextSize(); await commit(); render(); }, (st.textSize || '') === k ? 'opt on' : 'opt')))),
    isSolo() ? soloFeeCard() : null,
    h('section', { class: 'card' }, h('h2', {}, '지도'),
      h('p', { class: 'small' }, '지도를 켜면 바탕 지도 조각을 인터넷(OpenFreeMap)에서 받습니다. 이름·연락처·주소·핀 위치는 보내지 않으며, 주소 검색·길찾기 서비스는 쓰지 않습니다.'),
      btn(st.mapEnabled ? '지도 사용 끄기' : '지도 사용 켜기', async () => { st.mapEnabled = !st.mapEnabled; await commit(); render(); })),
    h('section', { class: 'card' },
      isSolo() ? null : btn('기기 등록 화면', () => go('#/register')),
      btn('사용 방식 바꾸기', async () => {
        if (st.requests.length || st.daylist || st.events.length) return toast('자료가 있어 바꿀 수 없습니다. 백업·정리 후 바꿔 주세요.', 'bad');
        st.mode = null; await commit(); go('#/mode');
      }),
      h('p', { class: 'small' }, '이 앱은 서버에 자료를 보내지 않습니다. 브라우저의 "사이트 데이터 삭제"를 하면 보내지 않은 기록이 사라지니 주의하세요.'),
      btn('이 휴대폰의 모든 자료 지우기', async () => {
        if (unsent().length && !confirm2(`보내지 않은 기록 ${unsent().length}건이 사라집니다. 정말 지울까요?`)) return;
        if (!confirm2('PIN, 기기 키, 목록, 사진을 모두 지웁니다. 다시 등록해야 합니다.')) return;
        stopCamera(); await S.wipeAll(); st = null; location.hash = ''; location.reload();
      }, 'danger')));
}

// 단독 방식: 확인 필요 품목의 단가 정하기 (근거 필수, 바꾼 이력 보존)
function soloFeeCard() {
  const ov = st.feeOverrides || {};
  const review = BUILTIN_FEES.rows.filter((r) => r.status !== 'CONFIRMED');
  const msg = h('p', { class: 'msg' });
  return h('section', { class: 'card' }, h('h2', {}, `확인 필요 품목 단가 (${review.length})`),
    h('p', { class: 'small' }, '원문 규격이 겹치거나 단위가 불명확한 품목입니다. 담당 부서 기준을 확인한 것만 정하세요. 정한 단가는 그 뒤 새로 접수·추가하는 품목에만 쓰입니다.'),
    ...review.map((r) => {
      const price = h('input', { type: 'number', inputmode: 'numeric', min: 1, placeholder: '원', value: ov[r.id] ? ov[r.id].price : '' });
      const why = h('input', { type: 'text', placeholder: '근거 (예: 청소과 확인 11/3)', value: ov[r.id] ? ov[r.id].reason : '' });
      return h('div', { class: 'line' }, h('div', { class: 'lhead' }, h('strong', {}, `${r.name} ${r.spec}`), h('em', { class: 'tag' }, ov[r.id] ? `확정 ${won(ov[r.id].price)}` : '확인 대기')),
        h('p', { class: 'small' }, r.note || ''), h('div', { class: 'grid2' }, price, why),
        h('div', { class: 'grid2' },
          btn('저장', async () => {
            const n = parseInt(price.value, 10);
            if (!(n > 0)) return (msg.textContent = '단가는 0원보다 커야 합니다. 모르면 확인 대기로 두세요.');
            if (!why.value.trim()) return (msg.textContent = '근거를 적어 주세요.');
            st.feeOverrides = { ...ov, [r.id]: { price: n, reason: why.value.trim(), at: new Date().toISOString() } };
            st.feeRev = (st.feeRev || 0) + 1; (st.feeLog ||= []).push({ id: r.id, price: n, reason: why.value.trim(), at: new Date().toISOString() });
            await commit(); toast('저장했습니다'); render();
          }, 'opt'),
          ov[r.id] ? btn('확인 대기로 되돌리기', async () => {
            const { [r.id]: _, ...rest } = ov; st.feeOverrides = rest; st.feeRev = (st.feeRev || 0) + 1;
            (st.feeLog ||= []).push({ id: r.id, price: null, reason: '되돌림', at: new Date().toISOString() }); await commit(); render();
          }, 'opt') : h('span')));
    }), msg);
}
function applyTextSize() { document.documentElement.classList.toggle('text-big', !!(st && st.textSize === 'big')); }

// ------------------------------------------------------------ 라우팅
let renderSeq = 0;
async function render() {
  const my = ++renderSeq;
  stopCamera();
  photoUrls.forEach((u) => URL.revokeObjectURL(u)); photoUrls = [];
  if (liveMap) { try { liveMap.remove(); } catch {} liveMap = null; }
  let view;
  if (!(await S.hasPin())) view = screenSetup();
  else if (!S.isUnlocked()) view = screenLock();
  else {
    let [, route, arg] = (location.hash || '#/home').split('/');
    if (!st.mode && route !== 'mode') route = 'mode';
    if (isSolo() && ['scan', 'register', 'export', 'received', 'resultqr'].includes(route)) route = 'home';
    if (!isSolo() && ['intake', 'backup'].includes(route)) route = 'home';
    view = await ({
      home: screenHome, scan: screenScan, received: screenReceived, register: screenRegister,
      'qrtest-result': screenQrTestResult, resultqr: screenResultQr, stop: () => screenStop(arg), add: () => screenAdd(arg), export: screenExport, settings: screenSettings, unload: screenUnload,
      mode: screenMode, intake: () => screenIntake(arg), backup: screenBackup, map: () => screenMap(arg), pick: () => screenPick(arg),
    }[route] || screenHome)();
  }
  if (my !== renderSeq) return; // 그 사이 다른 화면으로 이동
  const sameRoute = ui.lastRoute === location.hash, y = scrollY; // 같은 화면을 다시 그릴 때는 스크롤 유지
  $app.replaceChildren(view);
  scrollTo(0, sameRoute ? y : 0);
  ui.lastRoute = location.hash;
}
addEventListener('hashchange', render);

// ------------------------------------------------------------ 서비스 워커 (업데이트는 사용자가 눌러야 적용)
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('./sw.js').then((reg) => {
    const offer = (w) => {
      const bar = h('div', { class: 'update' }, '새 버전이 있습니다. ', btn('업데이트', () => w.postMessage('SKIP_WAITING'), 'primary'));
      document.body.append(bar);
    };
    if (reg.waiting) offer(reg.waiting);
    reg.addEventListener('updatefound', () => {
      const w = reg.installing;
      w && w.addEventListener('statechange', () => { if (w.state === 'installed' && navigator.serviceWorker.controller) offer(w); });
    });
  }).catch(() => {});
  // 첫 설치 때는 새로고침하지 않는다(입력 중인 화면 보호). 사용자가 [업데이트]를 눌렀을 때만 새로고침.
  const hadController = !!navigator.serviceWorker.controller;
  let reloaded = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => { if (hadController && !reloaded) { reloaded = true; location.reload(); } });
}

render();
