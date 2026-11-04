// 동구 대형폐기물 수거 시제품 - 휴대폰 앱 (서버 없는 PWA)
// 업무 데이터는 이 기기 안에서만 암호화해 보관한다. 외부로 보내는 통신 코드는 없다.
import * as P from './proto.js';
import * as S from './store.js';
import { searchFees, deriveStop, won, LABEL, matchText, feeExtra, needsFeeNotice } from './fees.js';
import * as F from './field.js';
import * as BIO from './bio.js';
import { BUILTIN_FEES } from './fees-data.js';
import * as B from './backup.js';
import * as M from './map.js';
import * as NAV from './nav.js';
import * as J from './juso.js';
import qrcode from './vendor/qrcode.mjs';

export const APP_VERSION = '0.10.1';
const BACKUP_WARN_MS = 24 * 3600 * 1000; // 단독 모드: 마지막 백업 후 24시간 지나면 경고
const LOCK_CHOICES = [5, 10, 15];        // 무조작 자동 잠금(분), 설정에서 고름. 기본 10분
const HIDDEN_LOCK_MS = 2 * 60 * 1000;    // 다른 앱에 2분 넘게 있으면 잠금
const HANDOFF_MS = 10 * 60 * 1000;       // 업무 중 카메라·전화·문자·길안내 앱으로 넘어갔을 때는 10분까지 봐 준다
const COLLECT_TTL_MS = 24 * 3600 * 1000; // 받다 만 QR 조각 보관 시간
const END_OF_DAY_H = 17;                 // 이 시각(한국 시간) 뒤에는 미전송이 1건이라도 있으면 알림

const $app = document.getElementById('app');
let st = null;               // 복호화된 업무 상태 (잠금 해제 동안만)
let ui = { filter: 'ALL' };  // 화면 상태
let saving = Promise.resolve();
let lastActive = Date.now(), hiddenAt = 0, handoffUntil = 0;
const pendingPhotos = []; // 잠긴 사이 찍힌 사진: 메모리에만 두고(평문으로 저장하지 않음) 잠금 해제 뒤 암호화 저장
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
// 아래 탭 막대: 맨 위 단계 화면(목록·지도·설정)끼리 바로 옮겨 다닌다. 다른 화면은 위 [← 목록]으로 돌아온다.
function tabBar(active) {
  const tabs = [['list', '#/home', '목록'], ...(st && st.mapEnabled ? [['map', '#/map', '지도']] : []), ['set', '#/settings', '설정·정보']];
  return h('nav', { class: 'tabbar', 'aria-label': '주 메뉴' }, ...tabs.map(([k, href, t]) =>
    h('a', { href, class: 't-' + k, 'aria-current': k === active ? 'page' : null }, t)));
}
const appLogo = (cls = 'logo') => h('img', { src: 'icons/icon.svg', alt: '', class: cls, width: 72, height: 72 });
// 알림: 안내는 4초 뒤 사라지고, 오류는 눌러서 닫을 때까지 남는다(장갑 낀 손으로 놓치지 않게)
const toast = (msg, kind = '') => {
  document.querySelectorAll('.toast').forEach((x) => x.remove());  // 한 번에 하나만
  const bad = kind === 'bad';
  const t = h('div', { class: 'toast ' + kind, role: bad ? 'alert' : 'status' }, msg, bad ? h('span', { class: 'tx' }, '닫기 ✕') : null);
  if (bad) { t.tabIndex = 0; const close = () => t.remove(); t.addEventListener('click', close); t.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === 'Escape') close(); }); }
  else setTimeout(() => t.remove(), 4000);
  document.body.append(t);
};

// 아래에서 올라오는 확인 창 (window.prompt·confirm 대신). 큰 단추, 큰 글씨 설정을 따른다.
// choices: [{label, value, cls, needInput}] → 고른 value (입력칸이 있으면 {value, text}), 취소·뒤로 가기는 null
const openSheets = new Set();
function sheet({ title, text = '', body = null, choices = [], input = null, cancel = '취소' }) {
  return new Promise((resolve) => {
    const dlg = h('dialog', { class: 'sheet', 'aria-label': title });
    const inp = input ? h(input.multiline ? 'textarea' : 'input', { type: input.type || 'text', class: 'search', placeholder: input.placeholder || '', 'aria-label': input.label || input.placeholder || title, autocomplete: 'off', rows: input.multiline ? 3 : null }) : null;
    if (inp && input.value) inp.value = input.value;
    const msg = h('p', { class: 'msg err', role: 'alert' });
    const done = (v) => { openSheets.delete(done); try { dlg.close(); } catch {} dlg.remove(); resolve(v); };
    openSheets.add(done);
    const kids = [h('div', { class: 'grip', 'aria-hidden': 'true' }), h('h2', {}, title), text ? h('p', { class: 'sheet-text' }, text) : null, body,
      inp ? h('label', { class: 'fld' }, h('span', {}, input.label || ''), inp) : null, msg,
      h('div', { class: 'sheet-actions' }, ...choices.map((c) => btn(c.label, () => {
        if (inp && c.needInput && !inp.value.trim()) { msg.textContent = input.required || '내용을 적어 주세요.'; inp.focus(); return; }
        done(inp ? { value: c.value, text: inp.value.trim() } : c.value);
      }, 'huge ' + (c.cls || ''))), cancel ? btn(cancel, () => done(null), 'ghost big') : null)];
    dlg.append(...kids.filter(Boolean));
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); done(null); });
    document.body.append(dlg);
    try { dlg.showModal(); } catch { dlg.setAttribute('open', ''); }
    setTimeout(() => (inp || dlg.querySelector('.btn')).focus(), 30);
  });
}
const ask = async (text, ok = '계속', { title = '확인해 주세요', danger = false } = {}) =>
  (await sheet({ title, text, choices: [{ label: ok, value: true, cls: danger ? 'danger' : 'primary' }] })) === true;
// 단계 표시 (등록·목록 받기·결과 보내기·백업): 끝난 단계 ✓, 지금 단계 강조
function flowSteps(labels, cur) {
  return h('ol', { class: 'flow', 'aria-label': '진행 단계' }, ...labels.map((t, i) =>
    h('li', { class: i < cur ? 'done' : i === cur ? 'cur' : '', 'aria-current': i === cur ? 'step' : null },
      h('span', { class: 'n' }, i < cur ? '✓' : String(i + 1)), h('span', { class: 't' }, t))));
}
// 아래에 붙는 주 단추 영역
const footBar = (...kids) => h('nav', { class: 'footbar', 'aria-label': '주 동작' }, ...kids);

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
  requests: [], receiptCounter: {}, lastBackupAt: null, intakeDraft: null, mapEnabled: true,
  lockMin: 10, workers: [], worker: '', smsTemplates: null, navApp: NAV.DEFAULT_NAV_APP, jusoKey: '', jusoCoordKey: '', addrRoute: '', unloadSite: null, unsentWarnH: 3, retentionDays: 90, listChanges: {}, recentDongs: [], geoEvidence: true });
// 기기에 남겨 두는 화면 설정(업무 자료 아님, 잠금 화면에도 적용): 테마, 야외 고대비
let prefs = { theme: 'auto', hc: false };
function applyPrefs() {
  const r = document.documentElement;
  if (prefs.theme === 'light' || prefs.theme === 'dark') r.dataset.theme = prefs.theme; else delete r.dataset.theme;
  r.classList.toggle('hc', !!prefs.hc);
}
const isDark = () => prefs.theme === 'dark' || (prefs.theme !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
// 첫 화면 위 ☀/🌙: 지금 보이는 것의 반대로 바로 바꾼다(설정의 '자동'을 밝게/어둡게로 고정)
const themeToggle = () => btn(isDark() ? '☀' : '🌙', async () => { await setPrefs({ theme: isDark() ? 'light' : 'dark' }); render(); }, 'ghost theme-toggle',
  { 'aria-label': isDark() ? '밝은 화면으로' : '어두운 화면으로', title: '밝기 바꾸기 (설정에서 자동으로 되돌릴 수 있음)' });
async function setPrefs(patch) { prefs = { ...prefs, ...patch }; applyPrefs(); try { await S.putRaw('prefs', prefs); } catch {} }
async function loadState() {
  st = { ...emptyState(), ...((await S.getSecure('state')) || {}) };
  if (st.textSize === 'big') st.textSize = 'l'; // v0.10.0까지의 '큰 글씨'는 '크게'로
  if (!(st.navApp in NAV.NAV_APPS)) st.navApp = NAV.DEFAULT_NAV_APP; // v0.10.0의 '길안내 켬/끔'은 없어짐(항상 켬)
  applyTextSize();
  if (!st.mode && st.daylist) st.mode = 'PC'; // 0.1.0에서 쓰던 자료는 PC 연결 방식
  lockMs = (LOCK_CHOICES.includes(st.lockMin) ? st.lockMin : 10) * 60000;
  lastActive = Date.now();
}
// 잠금 해제 직후: 화면 그리기, 기다리던 사진 저장
async function afterUnlock() {
  await loadState();
  if (!location.hash || location.hash === '#/') go('#/home');
  await render();
  if (pendingPhotos.length) await flushPhotos();
  if (ui.afterCall && location.hash === `#/stop/${ui.afterCall}`) { const c = document.getElementById('st-contact'); if (c) c.scrollIntoView({ block: 'start' }); }
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
// 모든 기록에 지금 작업자(설정의 이름 중 고른 사람)를 남긴다
const withWorker = (body) => (st.worker ? { ...body, worker: st.worker } : body);
async function addEvent(stop, kind, body) {
  st.seq += 1;
  st.events.push({
    event_id: crypto.randomUUID(), seq: st.seq, request_id: stop.request_id,
    assign_ver: stop.assign_ver ?? (st.daylist ? st.daylist.assign_ver : 1), base_req_ver: stop.req_ver, kind, body: withWorker(body), ts: new Date().toISOString(),
  });
  await commit();
}
const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
// 전화: 전화 앱에 번호만 넘긴다(자동 발신 없음). 돌아오면 연락 결과를 묻는다.
async function dial(stop) {
  await addEvent(stop, 'DIAL_INTENT', {});
  ui.afterCall = stop.request_id;
  handoff();
  openExternal('tel:' + stop.phone);
}
// 전화·문자·지도 앱 열기. (시험용: 자동 시험은 window.__dgExternal로 가로채 주소만 확인한다 —
// 헤드리스 브라우저는 외부 앱 주소를 열면 그 뒤 입력을 받지 않기 때문)
function openExternal(url, { newTab = false } = {}) {
  if (typeof window.__dgExternal === 'function') return window.__dgExternal(url);
  // 웹 지도(https)는 새 창으로: 앱 화면(입력 중인 내용)을 떠나지 않게. 막히면(팝업 차단) 이 창에서 연다.
  if (newTab && window.open(url, '_blank', 'noopener,noreferrer')) return;
  location.href = url;
}
// 다른 앱(카메라·전화·문자·길안내)으로 넘어가는 동안은 잠금 기준을 늘린다
function handoff() { handoffUntil = Date.now() + HANDOFF_MS; }
// 접수와 무관한 팀 기록(하차 등)
async function addCrewEvent(kind, body) {
  st.seq += 1;
  st.events.push({ event_id: crypto.randomUUID(), seq: st.seq, request_id: '', assign_ver: st.daylist ? st.daylist.assign_ver : 0, base_req_ver: null, kind, body: withWorker(body), ts: new Date().toISOString() });
  await commit();
}
const unsent = () => st.events.filter((e) => e.seq > st.releasedSeq);

// ------------------------------------------------------------ 잠금
let lockMs = 10 * 60000; // 잠금 해제 동안 설정값(5·10·15분)
async function lockNow() {
  stopCamera(); S.lock(); st = null; wantWake(false);
  for (const done of [...openSheets]) done(null);
  await render();
}
['pointerdown', 'keydown'].forEach((t) => addEventListener(t, () => { lastActive = Date.now(); }, { passive: true }));
setInterval(() => {
  // 화면이 가려져 있거나 다른 앱으로 넘어간 동안에는 무조작으로 세지 않는다(돌아올 때 따로 판단)
  if (S.isUnlocked() && !document.hidden && Date.now() > handoffUntil && Date.now() - lastActive > lockMs) lockNow();
}, 15000);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { hiddenAt = Date.now(); stopCamera(); return; }
  const away = hiddenAt ? Date.now() - hiddenAt : 0;
  const limit = hiddenAt && hiddenAt <= handoffUntil ? HANDOFF_MS : HIDDEN_LOCK_MS;
  handoffUntil = 0; hiddenAt = 0;
  if (S.isUnlocked() && away > limit) { lockNow(); return; }
  lastActive = Date.now();
  if (S.isUnlocked() && wakeRoute()) wantWake(true);
  if (S.isUnlocked() && ui.afterCall && location.hash === `#/stop/${ui.afterCall}`) render().then(() => { const c = document.getElementById('st-contact'); if (c) c.scrollIntoView({ block: 'start' }); });
  else if (location.hash.startsWith('#/scan') || location.hash.startsWith('#/register')) render();
});

// 화면 켜짐 유지: 목록·상세 화면에서만. 지원하지 않거나 거절되면 조용히 넘어간다.
let wakeLock = null;
const wakeRoute = () => /^#\/(home|stop)\b/.test(location.hash || '#/home');
async function wantWake(on) {
  const r = document.documentElement;
  if (!('wakeLock' in navigator)) { r.dataset.wake = 'unsupported'; return; }
  try {
    if (on && !wakeLock && document.visibilityState === 'visible') {
      const w = await navigator.wakeLock.request('screen');
      wakeLock = w; r.dataset.wake = 'on';
      w.addEventListener('release', () => { if (wakeLock === w) { wakeLock = null; r.dataset.wake = 'off'; } });
    } else if (!on && wakeLock) { const w = wakeLock; wakeLock = null; r.dataset.wake = 'off'; await w.release(); }
  } catch { wakeLock = null; r.dataset.wake = 'denied'; }
}

// ------------------------------------------------------------ 사진 촬영 (잠금·새로고침에도 잃지 않게)
// 카메라 입력은 화면(#app) 밖에 두어 잠금 화면으로 바뀌어도 사라지지 않는다. 찍힌 사진은 메모리에 잡아 두고,
// 잠겨 있으면 잠금 해제 뒤 암호화해 저장한다. 평문 사진은 어디에도 저장하지 않는다.
const camInputs = {};
const routeStopId = () => { const m = (location.hash || '').match(/^#\/stop\/([^/]+)/); return m ? m[1] : null; };
function camInput(phase) {
  if (camInputs[phase]) return camInputs[phase];
  const input = h('input', { type: 'file', accept: 'image/*', capture: 'environment', class: 'hidden', id: 'cam-' + phase, tabindex: '-1', 'aria-hidden': 'true' });
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    const stopId = routeStopId() || (ui.photoTarget && ui.photoTarget.stopId);
    const lineId = ui.photoLine && ui.photoLine.stop === stopId ? ui.photoLine.line : null;
    input.value = '';
    // 위치는 이미 허용된 경우에만 (권한 창을 띄우지 않음)
    const item = { stopId, phase, lineId, file, at: new Date().toISOString(), fileTs: file.lastModified ? new Date(file.lastModified).toISOString() : null, geo: null };
    pendingPhotos.push(item);
    if (!S.isUnlocked()) { render(); }
    item.geo = await M.myFix({ ask: false, timeout: 2500 });
    if (S.isUnlocked()) await flushPhotos();
  });
  document.body.append(input);
  return (camInputs[phase] = input);
}
async function flushPhotos() {
  let n = 0;
  while (pendingPhotos.length && S.isUnlocked() && st) {
    const p = pendingPhotos[0];
    const stop = findStop(p.stopId);
    if (!stop) { pendingPhotos.shift(); toast('사진을 붙일 접수를 찾지 못했습니다. 다시 찍어 주세요.', 'bad'); continue; }
    try { await savePhoto(stop, p.phase, p.file, p.lineId, p); pendingPhotos.shift(); n++; }
    catch (e) { toast(e && e.name === 'QuotaExceededError' ? ERR.STORAGE_LOW : ERR.PHOTO_SAVE_FAIL, 'bad'); pendingPhotos.shift(); }
  }
  if (n) { toast(n > 1 ? `사진 ${n}장을 저장했습니다` : '사진을 저장했습니다'); await render(); }
}

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
  return h('main', { class: 'center' }, appLogo('app-icon'),
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
    try { await S.unlock(pin.value); await afterUnlock(); }
    catch (e) {
      ok.disabled = false; pin.value = '';
      msg.textContent = e.until && e.until > Date.now() ? `여러 번 틀렸습니다. ${Math.ceil((e.until - Date.now()) / 1000)}초 뒤 다시 시도하세요.` : 'PIN이 맞지 않습니다.';
    }
  }, 'primary big');
  pin.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok.click(); });
  // 지문·얼굴: 등록되어 있고 PRF로 데이터 키를 풀 수 있을 때만
  const bioBtn = btn('지문·얼굴로 열기', async () => {
    bioBtn.disabled = true; msg.textContent = '생체 인증 중…';
    try { const B = await S.getBio(); const out = await BIO.bioEvaluate(B.credId, B.prfSalt); await S.unlockBio(out); await afterUnlock(); }
    catch (e) { bioBtn.disabled = false; msg.textContent = e.code === 'BIO_FAIL' || e.code === 'NO_PRF' ? e.message : '생체 인증을 하지 못했습니다. PIN으로 여세요.'; }
  }, 'big', { hidden: true });
  S.getBio().then((b) => { if (b) bioBtn.hidden = false; }).catch(() => {});
  setTimeout(() => pin.focus(), 50);
  const waiting = pendingPhotos.length ? h('p', { class: 'notice pending-photo', role: 'status' }, `사진 ${pendingPhotos.length}장 저장 대기 — 잠금 해제 후 저장됩니다`) : null;
  return h('main', { class: 'center' }, appLogo('app-icon'), h('h1', {}, '잠겨 있습니다'), waiting, pin, ok, bioBtn, msg);
}

// ------------------------------------------------------------ 홈
// 오늘 진행 요약 띠: 완료/전체와 진행 막대 (0%여도 막대 바탕이 보이게)
function daySummary(items) {
  const n = items.length, done = items.filter((x) => x.d.progress === 'DONE').length;
  const hold = items.filter((x) => ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.d.outcome)).length;
  const pend = items.filter((x) => x.d.fee.pendingCount).length;
  return h('section', { class: 'summary', 'aria-label': '오늘 진행' },
    h('div', { class: 'sum-top', 'aria-hidden': 'true' }, h('span', {}, '오늘 진행'), h('span', { class: 'sum-pct' }, `${n ? Math.round((done / n) * 100) : 0}%`)),
    h('div', { class: 'sum-main' }, h('b', {}, done), h('span', {}, ` / ${n}곳 완료`)),
    h('div', { class: 'progress track', role: 'progressbar', 'aria-label': `완료 ${done}/${n}`, 'aria-valuemin': 0, 'aria-valuemax': n, 'aria-valuenow': done }, h('div', { class: 'fill', style: `width:${n ? (done / n) * 100 : 0}%` })),
    h('div', { class: 'sum-sub' }, hold ? h('span', { class: 'chip warn' }, `재방문·보류 ${hold}`) : null, pend ? h('span', { class: 'chip warn' }, `금액 확인 대기 ${pend}`) : null));
}
// 목록 찾기: 이름·주소·품목, 초성도 됨 (예: ㄱㅅ)
function stopSearch(total) {
  if (total < 6) return null;
  const q = h('input', { type: 'search', class: 'search', placeholder: '이름·주소·품목 찾기 (초성 가능)', value: ui.stopQ || '', 'aria-label': '목록에서 찾기' });
  q.addEventListener('input', () => { ui.stopQ = q.value; clearTimeout(stopSearch.t); stopSearch.t = setTimeout(() => { render().then(() => { const el = document.querySelector('input[aria-label="목록에서 찾기"]'); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }); }, 250); });
  return q;
}
const stopHay = (s) => `${s.name} ${s.address} ${s.dong || ''} ${s.spot_note || ''} ${s.receipt_no || ''} ${s.items.map((i) => i.name + ' ' + (i.spec || '')).join(' ')}`;
const filterQ = (list) => (ui.stopQ ? list.filter((x) => matchText(stopHay(x.s), ui.stopQ)) : list);

// 동(행정동): 2개 동 이상이면 거르기 단추, 노선 시간표는 동이 바뀌는 곳에 머리글
const dongOf = (s) => (s.dong || '').trim();
function dongChips(items) {
  const ds = [...new Set(items.map((x) => dongOf(x.s)).filter(Boolean))];
  if (ds.length < 2) { ui.dong = ''; return null; }
  if (ui.dong && !ds.includes(ui.dong)) ui.dong = '';
  return h('div', { class: 'tabs dongs', role: 'group', 'aria-label': '동 거르기' },
    btn('모든 동', () => { ui.dong = ''; render(); }, !ui.dong ? 'tab on' : 'tab'),
    ...ds.map((dg) => btn(`${dg} ${items.filter((x) => dongOf(x.s) === dg).length}`, () => { ui.dong = dg; render(); }, ui.dong === dg ? 'tab on' : 'tab')));
}
const filterDong = (list) => (ui.dong ? list.filter((x) => dongOf(x.s) === ui.dong) : list);

// 방문 카드 한 장 (PC 목록·단독 목록 공용). 개인정보를 지운 곳은 접수번호만 보인다.
function stopCard(s, d, { nowId = null, solo = false, showDate = false } = {}) {
  const cancelled = s.status === 'CANCELLED';
  const purged = !!s.pii_purged;
  const changes = (st.listChanges || {})[s.request_id] || [];
  const isNow = s.request_id === nowId;
  return h('a', { href: `#/stop/${s.request_id}`, class: `stop p-${cancelled ? 'CANCELLED' : d.outcome || d.progress} c-${d.contact}` + (d.progress === 'DONE' || cancelled ? ' done' : '') + (isNow ? ' now' : '') + (changes.length ? ' changed' : ''), 'aria-current': isNow ? 'step' : null },
    h('span', { class: 'seq' }, s.seq, isNow ? h('small', { class: 'now-tag' }, '지금') : null),
    h('span', { class: 'body' },
      h('strong', {}, purged ? '개인정보 정리됨' : s.name), h('span', { class: 'addr' }, purged ? s.receipt_no || '' : s.address),
      h('span', { class: 'items' }, s.items.map((i) => `${i.name} ${i.spec} ${i.qty}${i.unit_label}`).join(', ')),
      h('span', { class: 'chips' },
        ...changes.map((c) => h('em', { class: 'chip new' }, F.DIFF_LABEL[c] || c)),
        showDate ? h('em', { class: 'chip' }, s.planned_date) : null,
        dongOf(s) ? h('em', { class: 'chip dong' }, dongOf(s)) : null,
        !solo && s.revisit ? h('em', { class: 'chip warn' }, '재방문') : null,
        h('em', { class: 'chip c-' + d.contact }, LABEL.contact[d.contact]),
        h('em', { class: 'chip p-' + d.progress }, s.removed ? '목록에서 제외됨' : cancelled ? '취소' : LABEL.progress[d.progress]),
        d.fee.pendingCount ? h('em', { class: 'chip warn' }, '금액 확인 대기') : null)),
    h('span', { class: 'eta' }, s.eta || ''));
}
// 노선 시간표: 동이 바뀌는 곳에 동 이름 머리글
function routeItems(list, renderOne) {
  const anyDong = list.some((x) => dongOf(x.s));
  const out = [];
  let prev = null;
  for (const x of list) {
    const dg = dongOf(x.s);
    if (anyDong && !ui.dong && dg !== prev) out.push(h('li', { class: 'dong-head' }, h('span', {}, dg || '동 미지정')));
    prev = dg;
    out.push(renderOne(x));
  }
  return out;
}

// 작업자 바꾸기 (교대): PIN을 다시 넣지 않고 고르기만 한다. 이후 모든 기록에 이 이름이 남는다.
function workerBar() {
  const ws = (st.workers || []).filter(Boolean);
  if (!ws.length) return null;
  return h('div', { class: 'workers', role: 'group', 'aria-label': '작업자 바꾸기' }, h('span', { class: 'wl' }, '작업자'),
    ...ws.map((w) => btn(w, async () => { if (st.worker === w) return; st.worker = w; await commit(); toast(`작업자를 ${w}(으)로 바꿨습니다`); render(); }, 'opt chipbtn' + (st.worker === w ? ' on' : ''))));
}
const kstHour = () => Number(new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', hour: '2-digit', hour12: false }).format(new Date()));
async function batteryInfo() {
  try {
    if (!navigator.getBattery) return null;
    return await Promise.race([navigator.getBattery(), new Promise((r) => setTimeout(() => r(null), 300))]);
  } catch { return null; }
}
// 홈 위 알림: 미전송(PC), 배터리, 보관 기간(단독)
async function homeBanners() {
  const out = [];
  const u = F.unsentInfo(st.events, st.releasedSeq);
  if (!isSolo() && u.count && (u.ageH >= (st.unsentWarnH || 3) || kstHour() >= END_OF_DAY_H)) {
    const late = kstHour() >= END_OF_DAY_H;
    out.push(h('section', { class: 'card notice unsent-banner', role: 'status' },
      h('p', {}, h('strong', {}, `보내지 않은 기록 ${u.count}건`), ` · 가장 오래된 기록 ${ago(u.oldestTs)}`),
      h('p', { class: 'small' }, late ? '오늘 업무가 끝나기 전에 결과를 보내 주세요. 휴대폰이 고장 나면 보내지 않은 기록은 되살릴 수 없습니다.'
        : `${st.unsentWarnH || 3}시간 넘게 보내지 않은 기록이 있습니다. 중간 결과를 보내 두면 휴대폰이 고장 나도 안전합니다.`),
      btn('중간 결과 보내기', () => go('#/export'), 'primary big')));
  }
  const bat = await batteryInfo();
  if (bat && bat.level < 0.2 && !bat.charging && u.count) {
    out.push(h('section', { class: 'card notice battery', role: 'status' },
      h('p', {}, h('strong', {}, `배터리 ${Math.round(bat.level * 100)}%`), isSolo() ? ' — 지금 백업하세요' : ' — 결과를 지금 보내세요'),
      btn(isSolo() ? '지금 백업' : '결과 보내기', () => go(isSolo() ? '#/backup' : '#/export'), 'primary big')));
  }
  if (isSolo()) {
    const cand = F.retentionCandidates(st.requests, st.events, st.retentionDays || 90, (r) => r.status === 'CANCELLED' || deriveStop(r, st.events).progress === 'DONE');
    if (cand.length) {
      const ready = cand.filter((c) => st.lastBackupAt && Date.parse(st.lastBackupAt) > c.doneAt);
      out.push(h('section', { class: 'card notice retention', role: 'status' },
        h('p', {}, h('strong', {}, `보관 기간(${st.retentionDays || 90}일)이 지난 처리 끝난 접수 ${cand.length}건`)),
        ready.length ? h('p', { class: 'small' }, `백업이 끝난 ${ready.length}건의 이름·연락처·주소를 지울 수 있습니다. 접수번호·품목·수거 기록은 남습니다.`)
          : h('p', { class: 'small' }, '백업을 먼저 만들어야 개인정보를 지울 수 있습니다.'),
        ready.length ? btn(`${ready.length}건 개인정보 지우기`, async () => {
          if (!(await ask(`${ready.length}건의 이름·연락처·주소·메모를 이 휴대폰에서 지웁니다. 되돌릴 수 없습니다. 마지막 백업(${ago(st.lastBackupAt)})에는 남아 있습니다.`, '지우기', { danger: true }))) return;
          const at = new Date().toISOString();
          const ids = new Set(ready.map((c) => c.r.request_id));
          for (const c of ready) F.purgeStopPII(c.r, at);
          for (const e of st.events) if (ids.has(e.request_id)) F.purgeEventPII(e);
          await commit(); toast(`${ready.length}건의 개인정보를 지웠습니다`); render();
        }, 'big') : btn('지금 백업', () => go('#/backup'), 'primary big')));
    }
  }
  return out;
}

async function screenHome() {
  if (!st.mode) return screenMode();
  if (isSolo()) return screenHomeSolo();
  const registered = !!(await S.getRaw('pc')) && !!(await S.getRaw('device'));
  const n = unsent().length;
  const dl = st.daylist;
  const top = h('header', { class: 'bar' }, appLogo(), h('h1', { class: 'bar-title' }, '동구 수거'), themeToggle(), btn('잠금', lockNow, 'ghost'));
  // 목록이 있으면 하루 한두 번 쓰는 버튼은 작게, 방문 순서가 먼저 보이게
  const stale = n && dl && dl.work_date < todayKST();
  const actions = dl
    ? h('section', { class: 'actions grid2' },
      btn('오늘 목록 받기', () => go('#/scan'), 'big', { disabled: !registered }),
      btn(n ? `결과 보내기 ${n}건` : '결과 보내기', () => go('#/export'), 'big' + (stale ? ' warn' : '')))
    : h('section', { class: 'actions' },
      btn('오늘 목록 받기', () => go('#/scan'), 'primary huge', { disabled: !registered }),
      btn(n ? `결과 보내기 (미전송 ${n}건)` : '결과 보내기', () => go('#/export'), 'huge ' + (stale ? 'warn' : '')));
  const out = [top, workerBar(), ...(await homeBanners())];
  if (!registered) out.push(h('section', { class: 'card notice' }, h('p', {}, '먼저 관리자와 함께 기기 등록을 해 주세요.'), btn('기기 등록', () => go('#/register'), 'primary')));
  out.push(actions);
  if (!dl) { out.push(h('p', { class: 'empty' }, '받은 목록이 없습니다.')); }
  else {
    const stops = dl.stops.map((s) => ({ s, d: deriveStop(s, st.events) }));
    const remain = stops.filter((x) => x.d.progress !== 'DONE' && !x.s.removed);
    const hold = stops.filter((x) => ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.d.outcome) || x.d.contact === 'CALLBACK');
    const changed = Object.keys(st.listChanges || {}).length;
    out.push(h('p', { class: 'meta' }, `${dl.work_date} ${dl.crew}` + (dl.assign_ver > 1 ? ` · 목록 ${dl.assign_ver}차` : '') + (changed ? ` · 바뀐 곳 ${changed}` : '')));
    out.push(daySummary(stops.filter((x) => !x.s.removed)));
    out.push(h('div', { class: 'tabs', role: 'group', 'aria-label': '목록 거르기' },
      ...[['ALL', `전체 ${stops.length}`], ['REMAIN', `남음 ${remain.length}`], ['HOLD', `재방문·보류 ${hold.length}`]].map(([k, t]) =>
        btn(t, () => { ui.filter = k; render(); }, ui.filter === k ? 'tab on' : 'tab'))));
    out.push(dongChips(stops));
    out.push(stopSearch(stops.length));
    const list = filterDong(filterQ({ ALL: stops, REMAIN: remain, HOLD: hold }[ui.filter]));
    if (!list.length) out.push(h('p', { class: 'empty' }, ui.stopQ ? `'${ui.stopQ}'에 맞는 곳이 없습니다.` : '해당하는 곳이 없습니다.'));
    const nowId = (stops.find((x) => x.d.progress !== 'DONE' && !x.d.outcome && !x.s.removed) || {}).s?.request_id;  // 지금 갈 곳
    out.push(h('ul', { class: 'stops route' }, routeItems(list, ({ s, d }) => h('li', {},
      stopCard(s, d, { nowId }),
      s.unload_after && !ui.stopQ ? h('div', { class: 'unload-row' }, h('span', { class: 'seq u' }, '⇩'),
        h('span', { class: 'u-body' }, h('strong', {}, `${s.unload_after.label} 하차`), h('br'), h('span', { class: 'small' }, `도착 ${s.unload_after.eta || '-'} · 약 ${s.unload_after.volume_m3}m³ 비움 · ${s.unload_after.minutes}분`)),
        navBtn('길안내', null, unloadLoc(s.unload_after), NAV.navLabel('UNLOAD'), 'u-nav')) : null))));
    const myUnloads = st.events.filter((e) => e.kind === 'UNLOAD');
    out.push(btn(myUnloads.length ? `하차 기록 (${myUnloads.length}회)` : '하차 기록', () => go('#/unload'), 'big'));
  }
  out.push(tabBar('list'));
  return h('main', { class: 'with-tabs' }, ...out);
}

// ------------------------------------------------------------ 하차 기록 (반입지에서 비운 양)
function screenUnload() {
  const list = st.events.filter((e) => e.kind === 'UNLOAD');
  const plannedLabel = st.daylist && (st.daylist.stops.find((x) => x.unload_after) || {}).unload_after;
  const uLoc = unloadLoc(plannedLabel);
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
    h('section', { class: 'card' }, h('h2', {}, '반입지 길안내'),
      h('div', { class: 'grid2' }, navBtn('🧭 길안내', null, uLoc, NAV.navLabel('UNLOAD'), 'big'),
        btn(uLoc ? '반입지 위치 고치기' : '반입지 위치 지정', () => go('#/pick/unload'), 'opt', { disabled: !st.mapEnabled })),
      uLoc ? h('p', { class: 'small' }, (plannedLabel && plannedLabel.lat != null ? 'PC 목록의 반입지 위치' : '이 휴대폰에 지정한 반입지 위치') + '로 안내합니다. 지도 앱에는 좌표와 "반입지"만 넘깁니다.')
        : h('p', { class: 'small nav-none' }, `길안내: ${NO_LOC}`)),
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
  // 배터리 절약: QR을 못 찾는 동안은 읽는 간격을 90ms → 최대 450ms로 늘리고, 찾으면 바로 빠르게 돌아간다
  let misses = 0;
  const loop = () => {
    if (cam.stop) return;
    let found = false;
    if (video.readyState >= 2 && video.videoWidth) {
      const scale = Math.min(1, 1280 / Math.max(video.videoWidth, video.videoHeight)); // 촘촘한 QR(버전 20 이상)도 읽히도록 1280까지 원본 크기로
      canvas.width = Math.round(video.videoWidth * scale); canvas.height = Math.round(video.videoHeight * scale);
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const code = window.jsQR(img.data, img.width, img.height, { inversionAttempts: 'dontInvert' });
      if (code && code.data) { found = true; try { onText(code.data); } catch (e) { console.warn(e.code || e.message); } }
    }
    misses = found ? 0 : misses + 1;
    setTimeout(loop, misses < 20 ? 90 : Math.min(450, 90 + (misses - 20) * 15));
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
  const STEPS = ['PC 등록 QR 읽기', '기기 코드 전달', '지문 숫자 대조'];
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
    return h('main', {}, back, flowSteps(STEPS, 0), h('section', { class: 'card cam-card' }, video, msg));
  }
  const kexRaw = await P.exportRaw(dev.kex.publicKey), signRaw = await P.exportRaw(dev.sign.publicKey);
  const code = await P.deviceCode(dev.id, kexRaw, signRaw);
  const fp = await P.fingerprint(pc.pcSignPub, kexRaw, signRaw);
  const ta = h('textarea', { readonly: true, rows: 4, class: 'code' }); ta.value = code;
  return h('main', { class: 'with-dock' }, back, flowSteps(STEPS, 1),
    h('section', { class: 'card' },
      h('h2', {}, h('span', { class: 'stepno' }, '2'), '기기 코드 전달'),
      h('p', {}, '아래 기기 코드를 관리자에게 전달하면 PC에 등록합니다. (공개키만 들어 있습니다)'),
      ta, btn('코드 복사', async () => { try { await navigator.clipboard.writeText(code); toast('복사했습니다'); } catch { ta.select(); } }, 'big')),
    h('section', { class: 'card' },
      h('h2', {}, h('span', { class: 'stepno' }, '3'), '지문 숫자 대조'),
      h('p', {}, 'PC 화면에 나온 지문 숫자가 아래와 같은지 확인하세요.'),
      h('p', { class: 'fp' }, fp.slice(0, 3) + ' ' + fp.slice(3)),
      h('p', { class: 'small' }, '기기 번호 ' + P.uuidStr(dev.id).slice(0, 8))),
    btn('PC 등록 QR 다시 읽기', () => { ui.rescanPc = true; render(); }, 'ghost link'),
    footBar(btn('완료', () => go('#/home'), 'primary huge')));
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
  const bar = h('div', { class: 'progress track', role: 'progressbar', 'aria-label': '받은 조각' }, h('div', { class: 'fill' }));
  const count = h('p', { class: 'count' }, '목록 QR을 비추세요');
  const miss = h('p', { class: 'small' });
  const msg = h('p', { class: 'msg' });
  const show = () => {
    if (!current) return;
    const b = col.bundles[current], got = Object.keys(b.parts).length;
    bar.firstChild.style.width = `${(100 * got) / b.total}%`;
    bar.setAttribute('aria-label', `받은 조각 ${got}/${b.total}`);
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
        let diff = null;
        if (old && old.work_date === pl.work_date) {
          diff = F.diffDaylist(old, pl);
          const ids = new Set(pl.stops.map((s) => s.request_id));
          const withEvents = new Set(st.events.map((e) => e.request_id));
          for (const s of old.stops) if (!ids.has(s.request_id) && withEvents.has(s.request_id)) pl.stops.push({ ...s, removed: true });
          // 이미 개인정보를 지운 곳은 다시 받아도 지운 채로 둔다(보관 종료 확인 뒤에는 휴대폰에 다시 두지 않음)
          for (const s of pl.stops) { const o = old.stops.find((x) => x.request_id === s.request_id); if (o && o.pii_purged && !s.pii_purged) F.purgeStopPII(s, o.pii_purged); }
          // 바뀐 표시는 그 지점을 열어 볼 때까지 남긴다(이전 표시 중 아직 안 본 것도 유지)
          const keep = Object.fromEntries(Object.entries(st.listChanges || {}).filter(([id]) => ids.has(id)));
          for (const [id, f] of Object.entries(diff.changes)) keep[id] = [...new Set([...(keep[id] || []), ...f])];
          st.listChanges = keep;
        } else {
          // 새 날짜 목록: 이전 목록(결과 반영이 확인된 것)의 이름·연락처·주소는 이미 이 목록으로 바뀌어 남지 않는다.
          // 이전 날짜 기록에 남은 메모 글·좌표도 지운다(수량·결과 기록은 사진 회수 때까지 남김).
          for (const e of st.events) if (e.seq <= st.releasedSeq && e.request_id) F.purgeEventPII(e);
          st.listChanges = {};
        }
        st.daylist = pl; st.lastBundleHash = hash;
        ui.diffSummary = diff && diff.summary ? diff.summary : '';
        await commit();
        if (ui.diffSummary) toast(ui.diffSummary);
      }
      col.drop(current); await S.putRaw('collect', col.bundles);
      const rc = opened.receipt;
      ui.receipt = { code: rc.slice(0, 4) + ' ' + rc.slice(4), count: pl.stops.filter((s) => !s.removed).length, ver: pl.assign_ver, diff: ui.diffSummary || '' };
      ui.diffSummary = '';
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
    flowSteps(['QR 비추기', '조각 모으기', '수신 확인 코드'], current ? 1 : 0),
    h('section', { class: 'card cam-card' }, video, bar, count, miss, msg,
      h('p', { class: 'small' }, '다 받기 전에는 저장하지 않습니다. 중간에 멈춰도 24시간 안에 이어서 받을 수 있습니다.')),
    current ? btn('받던 조각 버리고 처음부터', async () => { col.drop(current); await S.putRaw('collect', col.bundles); render(); }, 'ghost link') : null);
}

function screenReceived() {
  const r = ui.receipt;
  if (!r) { go('#/home'); return h('main'); }
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, h('h1', { class: 'bar-title' }, '목록 받기 완료')),
    flowSteps(['QR 비추기', '조각 모으기', '수신 확인 코드'], 2),
    h('section', { class: 'card center-card' },
      h('h2', {}, '목록을 받았습니다'),
      h('p', {}, `${r.count}건 · 목록 갱신본 ${r.ver}`),
      r.diff ? h('p', { class: 'notice diff-summary', role: 'status' }, `바뀐 내용: ${r.diff}`) : null,
      h('p', {}, 'PC 담당자에게 아래 수신 확인 코드를 알려 주세요.'),
      h('p', { class: 'fp' }, r.code)),
    footBar(btn('오늘 순서 보기', () => { ui.receipt = null; go('#/home'); }, 'primary huge')));
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

// ------------------------------------------------------------ 현장 보조: 도착·문자·길안내·스티커·금액 고지·증거
const hhmm = (iso) => new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(iso));
// [도착]: 시각과(허용했으면) 휴대폰 위치·정확도. 위치는 결과 파일로만 PC에 가고 지도 서버로는 보내지 않는다.
async function arrive(stop) {
  const geo = st.geoEvidence !== false ? await M.myFix({ ask: true, timeout: 8000 }) : null;
  await addEvent(stop, 'ARRIVE', geo ? { lat: geo.lat, lon: geo.lon, acc: geo.acc } : {});
  toast(geo ? `도착을 기록했습니다 (위치 오차 약 ${geo.acc}m)` : '도착을 기록했습니다 (위치 없음)');
  render();
}
const smsTemplates = () => (Array.isArray(st.smsTemplates) && st.smsTemplates.length ? st.smsTemplates : F.SMS_DEFAULTS);
// [문자]: 문구를 골라 문자 앱을 연다. 기록에는 문구 종류만 남기고 내용은 남기지 않는다.
async function smsSheet(stop, d) {
  const tpls = smsTemplates();
  const v = await sheet({ title: '문자 보내기', text: '문구를 고르면 문자 앱이 열립니다. 보내기 전에 내용을 확인하세요. 이 주민의 접수번호·시각만 들어갑니다.',
    choices: tpls.map((t, i) => ({ label: t.label, value: i, cls: i === 0 ? 'primary' : '' })) });
  if (v == null) return;
  const t = tpls[v];
  await addEvent(stop, 'SMS_INTENT', { template: t.id });
  handoff();
  openExternal(F.smsHref(stop.phone, F.fillSms(t.text, stop, d.appt)));
  render();
}
// [길안내]: 설정에서 고른 지도 앱(기본 카카오맵)을 목적지 좌표로 연다. 넘기는 것은 좌표와 "N번 수거지"(반입지는 "반입지")뿐 —
// 이름·연락처·주소·접수번호는 넘기지 않는다(nav.js). 앱이 없으면(1.5초 안에 다른 앱으로 넘어가지 않으면) 웹 지도 또는 기본 지도 앱으로.
const NO_LOC = '위치 없음 — 위치 지정 필요';
const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
function launchApp({ url, fallback }) {
  handoff();
  let left = false;
  const away = () => { if (document.hidden) left = true; };
  const gone = () => { left = true; };
  document.addEventListener('visibilitychange', away); addEventListener('pagehide', gone); addEventListener('blur', gone);
  openExternal(url);
  setTimeout(() => {
    document.removeEventListener('visibilitychange', away); removeEventListener('pagehide', gone); removeEventListener('blur', gone);
    if (fallback && !left && !document.hidden) openExternal(fallback, { newTab: fallback.startsWith('https:') });
  }, NAV.NAV_FALLBACK_MS);
}
/** stop: 기록을 남길 접수(반입지는 null — 접수 없는 팀 기록은 PC에서 충돌이 되므로 남기지 않음), loc: {lat, lon}, label: "N번 수거지"|"반입지" */
async function openNav(stop, loc, label) {
  if (!NAV.hasCoord(loc)) { toast(NO_LOC, 'bad'); return; }
  const u = NAV.navUrls(st.navApp || NAV.DEFAULT_NAV_APP, { lat: loc.lat, lon: loc.lon, label }, { ios: isIOS(), appname: location.origin });
  if (stop) await addEvent(stop, 'NAV_INTENT', { app: u.app });
  launchApp(u);
}
const navBtn = (text, stop, loc, label, cls = '') => btn(text, () => openNav(stop, loc, label), 'nav-btn ' + cls,
  NAV.hasCoord(loc) ? { 'aria-label': `${text} (${NAV.NAV_APPS[st.navApp] || NAV.NAV_APPS.kakao})` } : { disabled: true, title: NO_LOC, 'aria-label': `${text}: ${NO_LOC}` });
const unloadLoc = (u) => (u && u.lat != null && u.lon != null ? { lat: u.lat, lon: u.lon } : st.unloadSite);
// 스티커·신고필증 확인 (품목별 3가지). 없음·불일치면 그 품목을 수거할지 바로 정한다.
async function stickerCheck(stop, l, result) {
  if (result === 'OK') { await addEvent(stop, 'STICKER_CHECK', { line_id: l.line_id, result }); return render(); }
  const v = await sheet({ title: `${l.name} — 스티커·필증 ${LABEL.sticker[result]}`,
    text: '이 품목을 어떻게 할까요? 미수거로 하면 "스티커·필증 없음·불일치" 사유로 남습니다.',
    input: result === 'MISMATCH' ? { label: '붙어 있는 번호 (선택)', placeholder: '예: 2611-0042' } : null,
    choices: [{ label: '그래도 수거', value: 'COLLECT', cls: 'primary' }, { label: '미수거 (사유 기록)', value: 'LEAVE' }] });
  if (v == null) return;
  const decision = typeof v === 'object' ? v.value : v;
  const number = typeof v === 'object' ? v.text.slice(0, 30) : '';
  await addEvent(stop, 'STICKER_CHECK', { line_id: l.line_id, result, ...(number ? { number } : {}), decision });
  if (decision === 'LEAVE') await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, qty_confirmed: l.qty_confirmed, qty_collected: 0, reason: 'NO_STICKER' });
  render();
}
// 현장 금액 변경 고지: 접수 때보다 금액이 늘면 주민 안내 결과를 기록한다. 거부하면 늘어난 품목은 미수거.
// 돌려주는 값: true = 계속 저장, false = 저장 멈춤
async function feeNotice(stop, d) {
  const x = feeExtra(d), f = d.fee;
  const v = await sheet({ title: `추가 금액 ${won(x)}`,
    text: `접수 당시 ${won(f.received)} → 현장 확인 ${won(f.confirmedSubtotal)}${f.pendingCount ? ` (금액 확인 대기 ${f.pendingCount}건 제외)` : ''}. 주민에게 안내하고 결과를 고르세요.`,
    choices: [{ label: LABEL.feeResponse.AGREED, value: 'AGREED', cls: 'primary' }, { label: LABEL.feeResponse.NOTIFIED, value: 'NOTIFIED' },
      { label: LABEL.feeResponse.REFUSED, value: 'REFUSED', cls: 'danger' }] });
  if (v == null) return false;
  const body = { received: f.received, total: f.confirmedSubtotal, extra: x, pending: f.pendingCount, response: v };
  if (v !== 'REFUSED') { await addEvent(stop, 'FEE_NOTICE', body); return true; }
  // 늘어난 부분: 현장 추가 품목 전체, 접수 품목은 접수 수량을 넘는 만큼
  const lines = [];
  for (const l of d.lines) {
    if (l.price == null) continue;
    if (l.kind === 'FIELD' && l.qty_confirmed > 0) { lines.push(l.line_id); await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, qty_confirmed: l.qty_confirmed, qty_collected: 0, reason: 'FEE_REFUSED' }); }
    else if (l.kind === 'RECEIVED' && l.qty_confirmed > l.qty_received) { lines.push(l.line_id); await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, qty_confirmed: l.qty_confirmed, qty_collected: Math.min(l.qty_collected, l.qty_received), reason: 'FEE_REFUSED' }); }
  }
  await addEvent(stop, 'FEE_NOTICE', { ...body, lines });
  ui.tried = stop.request_id; ui.outcomeMsg = '추가 금액을 거부한 품목을 미수거로 바꿨습니다. 결과를 다시 골라 주세요 (일부 수거·미수거).';
  render();
  return false;
}
// 부재·미배출로 미수거할 때는 사진 1장 이상, 또는 찍지 못한 이유가 있어야 한다.
async function evidence(stop, d) {
  const v = await sheet({ title: '증거 사진이 필요합니다', text: '부재·배출 안 됨으로 미수거하려면 현장 사진을 1장 이상 찍어 주세요. 찍을 수 없으면 이유를 고르세요.',
    choices: [{ label: '📷 사진 찍으러 가기', value: 'PHOTO', cls: 'primary' }, ...Object.entries(LABEL.noPhoto).map(([k, t]) => ({ label: `사진 불가: ${t}`, value: k }))] });
  if (v == null) return null;
  if (v === 'PHOTO') { const el = document.getElementById('st-photos'); if (el) el.scrollIntoView({ block: 'start' }); return null; }
  return v;
}

async function screenStop(id) {
  const stop = findStop(id);
  if (!stop) { go('#/home'); return h('main'); }
  const d = deriveStop(stop, st.events);
  // 새 목록에서 바뀐 표시는 한 번 열어 보면 지운다
  const seen = (st.listChanges || {})[id];
  if (seen) { delete st.listChanges[id]; commit(); }
  const purged = !!stop.pii_purged;
  const setLine = async (l, patch) => {
    const v = { qty_confirmed: l.qty_confirmed, qty_collected: l.qty_collected, reason: l.reason, ...patch };
    if (v.qty_collected > v.qty_confirmed) v.qty_collected = v.qty_confirmed;
    await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, ...v });
    render();
  };

  const smsLog = st.events.filter((e) => e.request_id === id && e.kind === 'SMS_INTENT');
  const info = h('section', { class: 'card who' },
    seen ? h('p', { class: 'notice changed-note' }, '새 목록에서 바뀐 곳: ' + seen.map((c) => F.DIFF_LABEL[c] || c).join(', ')) : null,
    purged ? h('p', { class: 'name' }, '개인정보 정리됨') : h('p', { class: 'name' }, stop.name),
    purged ? h('p', { class: 'small' }, '보관 종료가 확인되어 이 휴대폰에서 이름·연락처·주소를 지웠습니다. 수거 기록은 남아 있습니다.')
      : h('p', { class: 'phone' }, h('a', { href: 'tel:' + stop.phone, onclick: async (e) => { e.preventDefault(); await dial(stop); } }, fmtPhone(stop.phone))),
    purged ? null : h('p', { class: 'addr' }, stop.address),
    dongOf(stop) ? h('p', { class: 'dong-line' }, h('em', { class: 'chip dong' }, dongOf(stop))) : null,
    stop.spot_note ? h('p', { class: 'spot' }, '배출 위치: ' + stop.spot_note) : null,
    stop.memo ? h('p', { class: 'memo' }, '요청: ' + stop.memo) : null,
    h('p', { class: 'small' }, stop.eta ? `${stop.receipt_no} · 도착 예정 ${stop.eta}` : stop.receipt_no),
    stop.removed ? h('p', { class: 'notice' }, '새 목록에서 빠진 건입니다. PC 담당자 확인이 필요합니다.') : null,
    stop.status === 'CANCELLED' ? h('p', { class: 'notice' }, '취소된 접수입니다. 사유: ' + (stop.cancel_reason || '-')) : null,
    stop.revisit ? h('p', { class: 'notice' }, '재방문 건입니다. 남은 품목만 표시하며, 이전에 수거한 수량에서 이어서 셉니다.') : null,
    stop.appt ? h('p', { class: 'memo' }, `방문 약속: ${stop.appt[0] || ''}~${stop.appt[1] || ''}`) : null,
    // 현장 동작: 도착 · 문자 · 길안내
    h('div', { class: 'grid2 field-acts' },
      btn(d.arrive ? `도착 ${hhmm(d.arrive.ts)} ✓` : '📍 도착', () => arrive(stop), 'opt arrive' + (d.arrive ? ' on' : ''), { 'aria-label': d.arrive ? `도착 기록됨 ${hhmm(d.arrive.ts)}, 다시 기록` : '도착' }),
      btn('✉ 문자', () => smsSheet(stop, d), 'opt sms', { disabled: purged || !stop.phone, 'aria-label': '문자' })),
    d.loc ? null : h('p', { class: 'small nav-none' }, `길안내: ${NO_LOC}` + (st.mapEnabled ? ' (아래 [위치 지정])' : '')),
    d.arrive ? h('p', { class: 'small' }, `도착 기록 ${hhmm(d.arrive.ts)}` + (d.arrive.acc != null ? ` · 위치 오차 약 ${d.arrive.acc}m` : ' · 위치 없음')) : null,
    smsLog.length ? h('p', { class: 'small sms-log' }, '문자 안내: ' + smsLog.map((e) => `${(smsTemplates().find((t) => t.id === e.body.template) || { label: e.body.template }).label} ${hhmm(e.ts)}`).join(', ')) : null,
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
  const stickerRow = (l) => {
    const sk = d.stickers[l.line_id];
    return h('div', { class: 'chk' },
      h('span', { class: 'chk-label' }, '스티커·필증'),
      h('div', { class: 'chk-opts', role: 'group', 'aria-label': `${l.name} 스티커·신고필증` },
        ...['OK', 'MISSING', 'MISMATCH'].map((k) => btn(LABEL.sticker[k], () => stickerCheck(stop, l, k), 'opt sk' + (sk && sk.result === k ? ' on' : '') + (k !== 'OK' ? ' sk-bad' : '')))),
      sk && sk.result !== 'OK' ? h('p', { class: 'small sk-note' }, `${LABEL.sticker[sk.result]}${sk.number ? ` (${sk.number})` : ''} → ${sk.decision === 'LEAVE' ? '미수거' : sk.decision === 'COLLECT' ? '수거' : '결정 전'}`) : null);
  };
  const lines = h('section', { class: 'card' }, h('h2', {}, '품목'),
    ...d.lines.map((l) => h('div', { class: 'line ' + (l.kind === 'FIELD' ? 'field' : 'orig') },
      h('div', { class: 'lhead' },
        h('div', { class: 'lname' }, h('em', { class: 'tag' }, l.kind === 'FIELD' ? '현장 추가' : '처음 접수'), h('strong', {}, `${l.name} ${l.spec}`)),
        l.price == null ? h('span', { class: 'amt pending' }, '확인 대기') : h('span', { class: 'amt', 'aria-label': `금액 ${won(l.price * l.qty_confirmed)}` }, won(l.price * l.qty_confirmed))),
      h('div', { class: 'price' }, l.price == null ? '금액 확인 대기' : `${won(l.price)} / ${l.unit_label} × ${l.qty_confirmed}`),
      l.kind === 'RECEIVED' ? h('div', { class: 'row' }, h('span', {}, '접수 수량'), h('span', { class: 'val' }, l.qty_received)) : null,
      h('div', { class: 'row' }, h('span', {}, '현장 수량'), stepper(l.qty_confirmed, (n) => setLine(l, { qty_confirmed: n }), 0, 99, 350)),
      h('div', { class: 'row got' }, h('span', {}, '실은 수량'), stepper(l.qty_collected, (n) => setLine(l, { qty_collected: n }), 0, l.qty_confirmed, 350)),
      showLeft(l) ? h('div', { class: 'row' + (l.remaining ? ' left' : '') }, h('span', {}, '못 실은 수량'), h('span', { class: 'val' }, `${l.remaining} ${l.unit_label}`)) : null,
      l.remaining && showLeft(l) ? (() => {
        const sel = h('select', { class: 'reason' + (l.reason || ui.tried !== id ? '' : ' need'), 'aria-label': `${l.name} 못 실은 사유`, onchange: (e) => setLine(l, { reason: e.target.value }) },
          ...Object.entries(LABEL.reason).map(([k, t]) => h('option', { value: k, selected: k === l.reason }, t)));
        return sel;
      })() : null,
      stickerRow(l))),
    btn('+ 현장 품목 추가', () => { ui.add = { stop: id, q: '', sel: null, qty: 1, free: false }; go(`#/add/${id}`); }, 'big'));

  const f = d.fee;
  const units = Object.entries(f.qtyByUnit).map(([u, n]) => `${n}${u}`).join(' · ');
  const extra = feeExtra(d);
  const noticeLine = d.notice ? h('p', { class: 'notice fee-notice' }, `추가 금액 안내: ${won(d.notice.extra)} · ${LABEL.feeResponse[d.notice.response] || d.notice.response} (${hhmm(d.notice.ts)})`) : null;
  const fee = h('section', { class: 'card fee' }, h('h2', {}, '계산 내역'),
    h('dl', {},
      h('dt', {}, '접수 당시 금액'), h('dd', {}, won(f.received)),
      h('dt', {}, '현장 추가분'), h('dd', {}, won(f.fieldAdded)),
      h('dt', {}, '확인된 소계'), h('dd', {}, won(f.confirmedSubtotal)),
      h('dt', {}, '접수 대비 증감'), h('dd', {}, (f.diff > 0 ? '+' : '') + won(f.diff)),
      h('dt', {}, '금액 확인 대기'), h('dd', {}, f.pendingCount + '건')),
    h('div', { class: 'total-box' + (f.final == null ? ' pending' : ''), role: 'group', 'aria-label': '총 수수료' },
      h('span', { class: 'tb-label' }, '총 수수료', h('small', {}, f.final == null ? '최종 금액 · 금액 확인 대기 있음' : '최종 금액')),
      h('strong', { class: 'tb-val' }, f.final == null ? '미확정' : won(f.final))),
    noticeLine,
    extra > 0 && needsFeeNotice(d) ? btn(`추가 금액 ${won(extra)} 안내 기록`, () => feeNotice(stop, d), 'big warn') : null,
    h('p', { class: 'small' }, `수량(단위별): ${units}. 금액은 현장 확인 수량 기준이며 수납 완료를 뜻하지 않습니다.`));

  // 사진: 카메라 입력은 화면 밖(몸통)에 하나씩 두고, 이 단추는 그것을 연다
  const photoIn = (phase) => {
    camInput(phase);
    return h('label', { class: 'btn big', for: 'cam-' + phase, onclick: () => { ui.photoTarget = { stopId: id, phase }; handoff(); } }, phase === 'BEFORE' ? '📷 수거 전' : '📷 수거 후');
  };
  const lineName = (lid) => { const l = d.lines.find((x) => x.line_id === lid); return l ? l.name : ''; };
  const photoFor = h('select', { 'aria-label': '사진을 묶을 품목', onchange: (e) => { ui.photoLine = { stop: id, line: e.target.value || null }; } },
    h('option', { value: '' }, '사진 대상: 접수 건 전체'),
    ...d.lines.map((l) => h('option', { value: l.line_id, selected: ui.photoLine && ui.photoLine.stop === id && ui.photoLine.line === l.line_id }, `사진 대상: ${l.name} ${l.spec || ''}`)));
  const thumbs = h('div', { class: 'thumbs' });
  for (const p of d.photos) {
    const img = h('img', { alt: p.phase === 'BEFORE' ? '수거 전' : '수거 후' });
    thumbs.append(h('figure', {}, img, h('figcaption', {}, (p.phase === 'BEFORE' ? '전' : '후') + (p.line_id ? ` · ${lineName(p.line_id)}` : '') + (p.taken_at ? ` · ${hhmm(p.taken_at)}` : ''))));
    S.getSecureBytes('ph:' + p.photo_id).then((b) => { if (b) { const u = URL.createObjectURL(new Blob([b], { type: 'image/jpeg' })); photoUrls.push(u); img.src = u; } });
  }
  const waiting = pendingPhotos.filter((p) => p.stopId === id).length;
  const photos = h('section', { class: 'card' }, h('h2', {}, `사진 ${d.photos.length}장`), d.lines.length > 1 ? photoFor : null, h('div', { class: 'grid2' }, photoIn('BEFORE'), photoIn('AFTER')),
    waiting ? h('p', { class: 'notice pending-photo' }, `사진 ${waiting}장 저장 중…`) : null, thumbs,
    h('p', { class: 'small' }, '사진은 앱 안에만 암호화 저장되며 갤러리에 남지 않습니다. 찍은 시각(과 허용했으면 위치)이 함께 기록됩니다.'));

  const msg = h('p', { class: 'msg' });
  const fillAll = async () => {
    for (const l of d.lines) if (l.qty_collected !== l.qty_confirmed) await addEvent(stop, 'ITEM_RESULT', { line_id: l.line_id, line_kind: l.kind, qty_confirmed: l.qty_confirmed, qty_collected: l.qty_confirmed, reason: '' });
  };
  const fail = (t) => { ui.tried = id; ui.outcomeMsg = t; render(); };
  const outMsg = h('p', { class: 'msg err', role: 'alert' }, ui.outcomeMsg && ui.tried === id ? ui.outcomeMsg : '');
  const outcome = h('section', { class: 'card' }, h('h2', {}, '수거 결과', d.outcome ? h('span', { class: 'hint' }, ' — 저장됨: ' + LABEL.outcome[d.outcome]) : null),
    // 가장 흔한 경우를 한 번에: 모든 품목을 실은 것으로 채우고 완료 저장
    btn('전부 실음 · 수거 완료', async () => {
      if (needsFeeNotice(d) && !(await feeNotice(stop, d))) return;
      ui.tried = null; ui.outcomeMsg = ''; await fillAll(); await addEvent(stop, 'ATTEMPT', { outcome: 'DONE' }); toast('수거 완료로 저장했습니다'); render();
    }, d.outcome === 'DONE' ? 'primary huge on' : 'primary huge'),
    outMsg,
    h('div', { class: 'grid3' }, ...['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].map((k) => btn(LABEL.outcome[k], async () => {
      const left = d.lines.filter((l) => l.remaining > 0);
      if (k !== 'ON_HOLD' && !left.length) return fail('못 실은 품목이 없습니다. 모두 실었으면 [전부 실음 · 수거 완료]를 누르세요.');
      if (k !== 'ON_HOLD' && left.some((l) => !l.reason)) return fail('못 실은 품목마다 사유를 골라 주세요 (위 품목 칸).');
      if (k === 'PARTIAL' && !d.lines.some((l) => l.qty_collected > 0)) return fail('실은 품목이 없으면 미수거로 저장하세요.');
      if (k === 'NOT_COLLECTED' && d.lines.some((l) => l.qty_collected > 0)) return fail('실은 품목이 있으면 일부 수거로 저장하세요.');
      const body = { outcome: k };
      // 부재·배출 안 됨으로 미수거: 사진 또는 "사진 불가" 이유가 있어야 한다
      if (k === 'NOT_COLLECTED' && (d.contact === 'NO_ANSWER' || left.some((l) => l.reason === 'NOT_PUT_OUT')) && !d.photos.length && !pendingPhotos.some((p) => p.stopId === id)) {
        const why = await evidence(stop, d);
        if (!why) return;
        body.no_photo_reason = why;
      }
      if (k === 'PARTIAL' && needsFeeNotice(d) && !(await feeNotice(stop, d))) return;
      ui.tried = null; ui.outcomeMsg = '';
      await addEvent(stop, 'ATTEMPT', body); toast('저장했습니다'); render();
    }, d.outcome === k ? 'opt on' : 'opt'))),
    d.outcome ? h('div', { class: 'result-sum' },
      h('p', {}, `저장된 결과: ${LABEL.outcome[d.outcome]}`),
      d.notice ? h('p', {}, `추가 금액 ${won(d.notice.extra)}: ${LABEL.feeResponse[d.notice.response] || d.notice.response}`) : null,
      d.noPhotoReason ? h('p', {}, `사진 불가 사유: ${LABEL.noPhoto[d.noPhotoReason] || d.noPhotoReason}`) : null,
      Object.values(d.stickers).some((x) => x.result !== 'OK') ? h('p', {}, '스티커·필증 문제 품목 있음') : null) : null,
    btn('품목 수량을 모두 실음으로 채우기', async () => { await fillAll(); render(); }, 'ghost link'),
    msg, fieldMemo(stop, d));

  const manage = isSolo() ? h('section', { class: 'card' }, h('h2', {}, '접수 관리'),
    h('p', { class: 'small' }, `원본 ${stop.req_ver}번째 버전 · 수정 ${(stop.versions || []).length}회`),
    h('div', { class: 'grid2' },
      btn('접수 수정', () => { ui.form = null; go(`#/intake/${id}`); }, 'opt', { disabled: stop.status === 'CANCELLED' || purged }),
      btn('접수 취소', async () => {
        const r = await sheet({ title: '접수 취소', text: '취소 사유를 적어 주세요. 기록은 지워지지 않습니다.', input: { label: '취소 사유', placeholder: '예: 주민이 직접 처리', required: '취소 사유를 적어 주세요.' },
          choices: [{ label: '취소로 기록', value: true, cls: 'danger', needInput: true }], cancel: '돌아가기' });
        if (!r || !r.text) return;
        stop.status = 'CANCELLED'; stop.cancel_reason = r.text; stop.cancelled_at = new Date().toISOString();
        await commit(); toast('취소로 기록했습니다'); render();
      }, 'opt', { disabled: stop.status === 'CANCELLED' })),
    (stop.versions || []).length ? h('ul', { class: 'small list' }, ...stop.versions.slice().reverse().map((v) => h('li', {}, `${v.at.slice(0, 16).replace('T', ' ')} · ${v.reason}`))) : null) : null;
  // 아래 고정 영역: 장갑 낀 손으로도 누르기 쉬운 전화·다음 지점
  const seqList = (isSolo() ? st.requests.filter((r) => r.planned_date === stop.planned_date && r.status !== 'CANCELLED').sort((a, b) => a.seq - b.seq) : (st.daylist ? st.daylist.stops.filter((x) => !x.removed) : []));
  const next = seqList.slice(seqList.indexOf(stop) + 1).find((x) => deriveStop(x, st.events).progress !== 'DONE');
  // 결과를 저장하기 전에는 "결과 저장 ↓", 저장한 뒤에는 "다음 N번 ▶" — 기록 없이 건너뛰지 않게
  // 길안내는 전화 옆: 결과 저장 전에는 이 지점, 저장한 뒤에는 다음 미완료 지점
  const saved = d.outcome || stop.status === 'CANCELLED';
  const nextLoc = next ? deriveStop(next, st.events).loc : null;
  const dock = h('nav', { class: 'dock four', 'aria-label': '빠른 동작' },
    btn('←', () => go('#/home'), 'dock-back', { 'aria-label': '목록으로' }),
    btn('전화', () => dial(stop), 'dock-call', { disabled: purged || !stop.phone }),
    saved ? (next ? navBtn('다음 길안내', next, nextLoc, NAV.navLabel(next.seq), 'dock-nav') : h('span'))
      : navBtn('길안내', stop, d.loc, NAV.navLabel(stop.seq), 'dock-nav'),
    saved
      ? btn(next ? `다음 ${next.seq}번 ▶` : '목록으로', () => go(next ? `#/stop/${next.request_id}` : '#/home'), 'primary dock-next')
      : btn('결과 저장 ↓', () => document.getElementById('st-result').scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' }), 'primary dock-next'));
  // 단계 표시: 연락 → 품목 → 사진 → 결과. 누르면 그 단계로 이동, 끝난 단계는 ✓
  contact.id = 'st-contact'; lines.id = 'st-items'; photos.id = 'st-photos'; outcome.id = 'st-result';
  const itemsDone = d.lines.length > 0 && st.events.some((e) => e.request_id === stop.request_id && e.kind === 'ITEM_RESULT');
  const steps = [['st-contact', '연락', d.contact !== 'NOT_CONTACTED'], ['st-items', '품목', itemsDone], ['st-photos', '사진', d.photos.length > 0], ['st-result', '결과', !!d.outcome]];
  const nextStep = d.outcome ? null : steps.find(([, , done]) => !done);
  const stepNav = h('nav', { class: 'steps', 'aria-label': '처리 단계' }, ...steps.map(([sid, label, done], i) =>
    h('a', { href: '#', class: 'step' + (done ? ' done' : '') + (nextStep && nextStep[0] === sid ? ' next' : ''), 'aria-current': nextStep && nextStep[0] === sid ? 'step' : null,
      onclick: (e) => { e.preventDefault(); document.getElementById(sid).scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' }); } },
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
  // 다시 그려도(사진 저장 등) 펼친 상태를 유지
  return h('details', { class: 'revisit', open: ui.openAppt === stop.request_id, ontoggle: (e) => { ui.openAppt = e.target.open ? stop.request_id : false; } },
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

async function savePhoto(stop, phase, file, lineId = null, meta = {}) {
  const bmp = await createImageBitmap(file);
  const scale = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
  const c = document.createElement('canvas'); c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
  c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
  const blob = await new Promise((res) => c.toBlob(res, 'image/jpeg', 0.8));
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const photo_id = crypto.randomUUID();
  await S.putSecureBytes('ph:' + photo_id, bytes);
  // 찍은 시각(파일 선택 시각)과 파일 시각, 허용된 경우 위치·정확도. 사진 바이트에는 위치 정보(EXIF)를 남기지 않는다(캔버스로 다시 그림).
  const geo = st.geoEvidence !== false && meta.geo ? { lat: meta.geo.lat, lon: meta.geo.lon, acc: meta.geo.acc } : {};
  await addEvent(stop, 'PHOTO_META', { photo_id, phase, sha256: P.toHex(await P.sha256(bytes)), bytes: bytes.length, line_id: lineId || null,
    taken_at: meta.at || new Date().toISOString(), file_ts: meta.fileTs || null, ...geo });
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
    const n = purgeReleased();
    await commit(); toast('PC 반영이 확인되었습니다' + (n ? ` · 끝난 ${n}곳의 개인정보를 이 휴대폰에서 지웠습니다` : '')); render();
  }, 'primary huge');

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
    if (!(await ask('반영이 확인된 목록과 기록을 이 휴대폰에서 지웁니다. 회수가 확인된 사진만 지우고, 나머지 사진은 남깁니다.', '정리하기'))) return;
    for (const p of allPhotos) if (recovered.has(p.photo_id)) await S.delSecureBytes('ph:' + p.photo_id);
    const keepCfg = Object.fromEntries(['mapEnabled', 'textSize', 'lockMin', 'workers', 'worker', 'smsTemplates', 'navApp', 'jusoKey', 'jusoCoordKey', 'addrRoute', 'unloadSite', 'unsentWarnH', 'retentionDays', 'geoEvidence', 'feeFavs', 'feeUsage'].map((k) => [k, st[k]]));
    st = { ...emptyState(), ...keepCfg, mode: st.mode, seq: st.seq, releasedSeq: st.seq, keptPhotos: notRecovered, photoExports: st.photoExports, recoveredPhotos: [] };
    await commit(); toast('정리했습니다'); go('#/home');
  }, 'ghost', { disabled: !allConfirmed || !st.daylist });

  const waitingCode = st.exports.some((x) => !x.confirmed);
  const sentPhotos = new Set((st.photoExports || []).flatMap((x) => x.ids));
  const cur = (pending.length && !waitingCode) || (!st.exports.length && !allPhotos.length) ? 0 : waitingCode ? 1 : notRecovered.some((p) => !sentPhotos.has(p.photo_id)) ? 2 : notRecovered.length ? 3 : 4;
  // 확인 코드 입력: 보낸 뒤 기다리는 코드가 있으면 맨 위에 크게
  const codeCard = h('section', { class: 'card code-card' + (waitingCode ? ' ask' : '') },
    h('h2', {}, h('span', { class: 'stepno' }, '2'), 'PC 확인 코드 입력'),
    h('p', { class: 'small' }, 'PC 담당자가 결과를 가져온 뒤 알려 주는 보관 종료 확인 코드 8자리를 넣으세요. 넣어야 "보낸 것"으로 처리되고, 끝난 곳의 개인정보가 이 휴대폰에서 지워집니다.'),
    codeIn, confirm,
    h('ul', { class: 'small list' }, ...st.exports.slice(-5).reverse().map((x) => h('li', {}, `${x.name} · ${x.confirmed ? '반영 확인됨' : '확인 대기'}`))));
  const sendCard = h('section', { class: 'card' },
    h('h2', {}, h('span', { class: 'stepno' }, '1'), pending.length ? `결과 보내기 · 미전송 ${pending.length}건` : '결과 보내기'),
    h('p', {}, '결과 파일에는 이름·연락처·주소·메모가 들어가지 않으며, PC에서만 열 수 있게 암호화됩니다. 사진은 넣지 않습니다. 하루 중간에 보내도 되고(중간 결과), 그 뒤 기록은 다음 파일에 이어서 들어갑니다.'),
    showQr, h('p', { class: 'small' }, '업무 메일을 쓸 수 없으면 [PC 화면 카메라로 보내기]로 PC 웹캠에 QR을 보여 주세요.'), msg);
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '결과 보내기')),
    flowSteps(['결과 파일', '확인 코드', '사진 파일', '사진 회수 코드', '정리'], cur),
    waitingCode ? codeCard : null, sendCard, waitingCode ? null : codeCard,
    h('section', { class: 'card' }, h('h2', {}, h('span', { class: 'stepno' }, '3'), `사진 ${allPhotos.length}장 · 미회수 ${notRecovered.length}장`),
      h('p', { class: 'small' }, '사진 파일은 PC에서만 열 수 있게 암호화됩니다. 업무 메일이 아니라 승인된 보안 매체로 옮기세요. 회수가 확인되기 전에는 휴대폰에서 지우지 않습니다.'),
      makePhotos, pmsg, pcode, pconfirm),
    h('section', { class: 'card' }, h('h2', {}, h('span', { class: 'stepno' }, '5'), '정리'), cleanup,
      allConfirmed ? null : h('p', { class: 'small' }, '모든 결과의 PC 반영이 확인되어야 정리할 수 있습니다.')),
    footBar(make));
}
// 보관 종료 확인 코드를 넣은 뒤: 결과가 끝났고(완료·일부·미수거) 그 기록이 모두 PC에 반영된 곳의 개인정보를 지운다.
// 수량·결과 같은 기록은 사진 회수까지 남긴다. 보류·처리 전인 곳은 그대로 둔다.
function purgeReleased() {
  if (!st.daylist) return 0;
  const at = new Date().toISOString();
  let n = 0;
  for (const s of st.daylist.stops) {
    if (s.pii_purged) continue;
    const mine = st.events.filter((e) => e.request_id === s.request_id);
    const d = deriveStop(s, st.events);
    if (!['DONE', 'PARTIAL', 'NOT_COLLECTED'].includes(d.outcome) || mine.some((e) => e.seq > st.releasedSeq)) continue;
    F.purgeStopPII(s, at);
    for (const e of mine) F.purgeEventPII(e);
    n++;
  }
  return n;
}

// ------------------------------------------------------------ 사용 방식 선택
function screenMode() {
  const pick = async (mode) => {
    if (mode === 'SOLO' && !(await ask('이 휴대폰만으로 쓰면 접수 원본이 이 휴대폰에만 저장됩니다. 분실·고장·데이터 삭제 시 원본까지 사라지므로 매일 백업 파일을 만들어 안전한 곳에 보관해야 합니다.', '이 휴대폰만으로 시작', { title: '이 휴대폰만으로 사용' }))) return;
    st.mode = mode; await commit(); go('#/home'); render();
  };
  return h('main', {},
    h('header', { class: 'bar' }, appLogo(), h('h1', { class: 'bar-title' }, '사용 방식 선택')),
    h('section', { class: 'card mode-card' },
      h('h2', {}, 'PC와 연결해서 사용 (권장)'),
      h('p', {}, 'PC가 접수 원본을 갖고, 휴대폰은 QR로 오늘 목록을 받아 현장 기록 후 결과 파일을 보냅니다.'),
      btn('PC와 연결해서 사용', () => pick('PC'), 'primary big')),
    h('section', { class: 'card mode-card' },
      h('h2', {}, '이 휴대폰만으로 사용'),
      h('p', {}, '전화 접수 등록부터 연락·수거 기록까지 이 휴대폰에서 모두 합니다. PC가 필요 없습니다.'),
      h('p', { class: 'small' }, '원본이 이 휴대폰에만 있으므로 매일 암호화 백업이 필요합니다. 여러 휴대폰이 같은 접수를 나눠 처리할 수는 없습니다.'),
      btn('이 휴대폰만으로 사용', () => pick('SOLO'), 'big')),
    h('p', { class: 'small' }, '사용 방식은 자료가 없을 때만 설정 화면에서 바꿀 수 있습니다.'));
}

// ------------------------------------------------------------ 단독 모드 홈
async function screenHomeSolo() {
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
  const list = filterDong(filterQ(lists[view].slice().sort((a, b) => (a.s.planned_date || '').localeCompare(b.s.planned_date || '') || a.s.seq - b.s.seq)));
  const needBackup = st.requests.length && (!st.lastBackupAt || Date.now() - Date.parse(st.lastBackupAt) > BACKUP_WARN_MS);
  const move = async (item, dir) => {
    const same = lists[view].map((x) => x.s).filter((r) => r.planned_date === item.planned_date).sort((a, b) => a.seq - b.seq);
    const i = same.indexOf(item), j = i + dir;
    if (j < 0 || j >= same.length) return;
    [same[i].seq, same[j].seq] = [same[j].seq, same[i].seq];
    await commit(); render();
  };
  const out = [
    h('header', { class: 'bar' }, appLogo(), h('h1', { class: 'bar-title' }, '동구 수거'), h('span', { class: 'mode-tag' }, '단독'), themeToggle(), btn('잠금', lockNow, 'ghost')),
    workerBar(), ...(await homeBanners()),
    needBackup ? h('section', { class: 'card notice' },
      h('p', {}, `마지막 백업: ${ago(st.lastBackupAt)}. 휴대폰을 잃어버리거나 고장 나면 접수 원본이 사라집니다.`),
      btn('지금 백업', () => go('#/backup'), 'primary')) : null,
    h('section', { class: 'actions' },
      btn('+ 전화 접수 등록', () => { ui.form = null; go('#/intake'); }, 'primary huge'),
      h('div', { class: 'grid2' }, btn('백업·복원', () => go('#/backup'), 'big'), btn(`하차 기록${st.events.some((e) => e.kind === 'UNLOAD') ? ` (${st.events.filter((e) => e.kind === 'UNLOAD').length}회)` : ''}`, () => go('#/unload'), 'big'))),
    h('div', { class: 'tabs', role: 'group', 'aria-label': '목록 거르기' }, ...[['TODAY', `오늘 ${lists.TODAY.length}`], ['TOMORROW', `내일 ${lists.TOMORROW.length}`], ['OPEN', `미처리 ${open.length}`], ['CANCELLED', `취소 ${lists.CANCELLED.length}`]]
      .map(([k, t]) => btn(t, () => { ui.soloView = k; ui.reorder = false; render(); }, view === k ? 'tab on' : 'tab'))),
  ];
  if (view === 'TODAY' && lists.TODAY.length) out.splice(out.indexOf(out.find((x) => x && x.classList && x.classList.contains('actions'))), 0, daySummary(lists.TODAY));
  out.push(dongChips(lists[view]));
  out.push(stopSearch(lists[view].length));
  if (!list.length) out.push(h('p', { class: 'empty' }, ui.stopQ ? `'${ui.stopQ}'에 맞는 접수가 없습니다.` : view === 'TODAY' ? '오늘 수거 예정 접수가 없습니다. [+ 전화 접수 등록]으로 시작하세요.' : '해당 접수가 없습니다.'));
  else {
    if (view === 'TODAY' || view === 'TOMORROW') out.push(btn(ui.reorder ? '순서 정하기 끝' : '방문 순서 바꾸기', () => { ui.reorder = !ui.reorder; render(); }, 'ghost'));
    const route = view === 'TODAY' || view === 'TOMORROW';
    const one = ({ s, d }) => h('li', { class: 'srow' }, stopCard(s, d, { solo: true, showDate: view === 'OPEN' }),
      ui.reorder ? h('span', { class: 'mv' }, btn('▲', () => move(s, -1), 'step', { 'aria-label': '위로' }), btn('▼', () => move(s, 1), 'step', { 'aria-label': '아래로' })) : null);
    out.push(h('ul', { class: 'stops' + (route ? ' route' : '') }, route && !ui.reorder ? routeItems(list, one) : list.map(one)));
  }
  out.push(tabBar('list'));
  return h('main', { class: 'with-tabs' }, ...out);
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
    ui.form = { id: id || null, name: src.name || '', phone: src.phone || '', address: src.address || '', dong: src.dong || '', spot_note: src.spot_note || '', memo: src.memo || '',
      lat: src.addrLat ?? null, lon: src.addrLon ?? null, zip: src.zip || '', dongCands: [],
      planned_date: src.planned_date || todayKST(1), eta: src.eta || '', items: JSON.parse(JSON.stringify(src.items || [])), reason: '', dupAck: false,
      pick: { q: '', sel: null, qty: 1 } };
  }
  const f = ui.form;
  let draftTimer = null;
  const saveDraft = () => { if (editing) return; clearTimeout(draftTimer); draftTimer = setTimeout(() => { const { pick, dupAck, reason, id: _i, lat, lon, dongCands, ...d } = f; st.intakeDraft = { ...d, addrLat: lat, addrLon: lon }; commit(); }, 600); };
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
      h('div', { class: 'lhead' }, h('div', { class: 'lname' }, h('strong', {}, `${it.name} ${it.spec}`)),
        it.price_status === 'CONFIRMED' ? h('span', { class: 'amt' }, won(it.unit_price * it.qty)) : h('span', { class: 'amt pending' }, '확인 대기')),
      locked.has(it.line_id) ? h('em', { class: 'tag' }, '현장 기록 있음') : null,
      h('div', { class: 'price' }, it.price_status === 'CONFIRMED' ? `${won(it.unit_price)} / ${it.unit_label}` : it.unit_price != null ? `금액 확인 대기 (원문 ${won(it.unit_price)} / ${it.unit_label})` : '금액 확인 대기'),
      h('div', { class: 'row' }, h('span', {}, '수량'), locked.has(it.line_id) ? h('span', { class: 'val' }, it.qty) :
        stepper(it.qty, (n) => { it.qty = n; it.amount = it.price_status === 'CONFIRMED' ? it.unit_price * n : null; drawItems(); saveDraft(); }, 1)),
      locked.has(it.line_id) ? null : btn('삭제', () => { f.items.splice(i, 1); drawItems(); saveDraft(); }, 'ghost link'))) :
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
    const dong = (f.dong || '').trim().slice(0, 20);
    if (dong) st.recentDongs = [dong, ...(st.recentDongs || []).filter((x) => x !== dong)].slice(0, 12);
    const data = { name: f.name.trim(), phone, address: f.address.trim(), dong, spot_note: f.spot_note.trim(), memo: f.memo.trim(), planned_date: f.planned_date, eta: f.eta, items: f.items,
      ...(f.lat != null && f.lon != null ? { lat: f.lat, lon: f.lon, geo_source: 'ADDR' } : {}) };
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
      field('address', '배출 주소', { autocomplete: 'off' }),
      f.lat != null ? h('p', { class: 'small addr-geo' }, `위치: 주소 검색 좌표(건물 출입구)${f.zip ? ` · 우편번호 ${f.zip}` : ''}. 배출 위치가 다르면 접수 뒤 [위치 고치기]로 맞추세요.`) : null,
      field('dong', '동 (행정동, 선택)', { autocomplete: 'off', list: 'dong-list', placeholder: '예: 충장동' }),
      f.dongCands.length ? h('div', { class: 'dong-cands', role: 'group', 'aria-label': '동 제안' }, h('span', { class: 'small' }, '주소 검색 결과의 동 후보: '),
        ...f.dongCands.map((x) => btn(x, () => { f.dong = x; f.dongCands = []; saveDraft(); render(); }, 'opt chipbtn'))) : null,
      h('datalist', { id: 'dong-list' }, ...knownDongs().map((x) => h('option', { value: x }))),
      field('spot_note', '상세 배출 위치', { autocomplete: 'off', placeholder: '예: 101동 분리수거장 옆' }),
      field('memo', '요청 메모', { multiline: true }),
      h('div', { class: 'grid2' }, field('planned_date', '수거 예정일', { type: 'date' }), field('eta', '방문 시각(선택)', { type: 'time' }))),
    h('section', { class: 'card' }, h('h2', {}, '버릴 물건'), itemsBox, totalBox),
    picker.search, picker.free,
    editing ? h('section', { class: 'card' }, field('reason', '수정 사유 (필수)', { autocomplete: 'off' })) : null,
    dupBox, h('div', { class: 'cta' }, msg, save));
}
const knownDongs = () => [...new Set([...(st.recentDongs || []), ...st.requests.map((r) => dongOf(r)).filter(Boolean)])];

// ------------------------------------------------------------ 주소 검색 (행정안전부 도로명주소 API, juso.js)
// 보내는 것은 검색칸에 적은 낱말과 승인키뿐. 이름·연락처가 검색어에 들어 있으면 보내지 않는다(forbid).
const addrRoute = () => st.addrRoute || (isSolo() ? 'direct' : 'pc');
function addrSearch({ onPick, forbid = () => [] }) {
  const box = h('div', { class: 'addr-search' }, h('div', { class: 'fld-label' }, '주소 검색'));
  if (addrRoute() === 'pc') {
    box.append(h('p', { class: 'small addr-status' }, '주소 검색 경로가 "PC 경유"입니다. PC 연결 방식에서는 주소·위치를 PC에서 정해 목록으로 보냅니다. 여기서는 직접 적거나 지도에서 위치를 지정하세요.'));
    return box;
  }
  const input = h('input', { type: 'search', class: 'search', placeholder: '도로명·건물명·지번 (예: 서남로 1)', 'aria-label': '주소 검색어', autocomplete: 'off', enterkeyhint: 'search', disabled: !st.jusoKey });
  const status = h('p', { class: 'small addr-status', role: 'status' }, st.jusoKey ? '' : '승인키를 설정에서 입력해 주세요 (설정 → 주소 검색). 지금은 주소를 직접 적고 지도에서 위치를 지정하세요.');
  const list = h('ul', { class: 'addr-results' });
  let timer = null, seq = 0;
  const leaks = (q) => {
    const sq = q.replace(/\s+/g, ''), dq = q.replace(/\D/g, '');
    return forbid().some((x) => { const v = String(x || ''); const dv = v.replace(/\D/g, ''); return (dv.length >= 8 && dq.includes(dv)) || (!dv && v.replace(/\s+/g, '').length >= 2 && sq.includes(v.replace(/\s+/g, ''))); });
  };
  const run = async () => {
    const q = J.cleanKeyword(input.value), my = ++seq;
    if (q.length < 2) return;
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

function nextSeq(date, exceptId) {
  return st.requests.filter((r) => r.planned_date === date && r.request_id !== exceptId).reduce((m, r) => Math.max(m, r.seq || 0), 0) + 1;
}

// ------------------------------------------------------------ 단독 모드 백업·복원
async function screenBackup() {
  const BSTEPS = ['비밀번호 정하기', '파일 만들기', '안전한 곳에 보관'];
  let flow = flowSteps(BSTEPS, ui.backupDone ? 2 : 0);
  ui.backupDone = false;
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
      ui.backupDone = true; flow.replaceWith(flow = flowSteps(BSTEPS, 2));
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
      if ((obj.state.mode || 'SOLO') !== st.mode) { rmsg.textContent = '다른 사용 방식(PC 연결/단독)의 백업입니다. 같은 방식에서만 복원할 수 있습니다.'; return; }
      if (!(await ask(`${obj.created_at.slice(0, 16).replace('T', ' ')} 백업 (접수 ${n}건)으로 이 휴대폰 자료를 바꿉니다. 지금 휴대폰에만 있는 기록은 사라집니다.`, '복원하기', { title: '백업에서 복원' }))) { rmsg.textContent = ''; return; }
      for (const [pid, b] of Object.entries(obj.photos || {})) await S.putSecureBytes('ph:' + pid, B.unb64(b));
      st = { ...emptyState(), ...obj.state, lastBackupAt: obj.created_at };
      await commit(); toast(`복원했습니다 (접수 ${n}건)`); go('#/home');
    } catch (e) { rmsg.textContent = errMsg(e); }
  }, 'big');

  pw1.addEventListener('input', () => { if (pw1.value.length >= 8) flow.replaceWith(flow = flowSteps(BSTEPS, 1)); });
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '백업·복원')),
    flow,
    isSolo() ? null : h('section', { class: 'card notice' }, h('h2', {}, 'PC 연결 방식의 안전용 백업'),
      h('p', { class: 'small' }, 'PC 연결 방식에서는 [결과 보내기]가 기본입니다. 이 백업은 휴대폰 고장·초기화에 대비한 예비본이며, PC는 이 파일이 아니라 결과 파일(.dgr)로 반영합니다. 받은 목록(주민 정보)이 함께 들어가므로 승인된 보안 매체에만 보관하세요.')),
    h('section', { class: 'card' }, h('h2', {}, h('span', { class: 'stepno' }, '1'), '백업 만들기'),
      h('p', { class: 'small' }, `마지막 백업: ${ago(st.lastBackupAt)} · ` + (isSolo() ? `접수 ${st.requests.length}건` : `목록 ${st.daylist ? st.daylist.stops.length : 0}곳 · 기록 ${st.events.length}건`) + ` · 사진 ${photoIds.length}장`),
      h('p', { class: 'small' }, '백업 파일은 백업 비밀번호로 암호화됩니다. 비밀번호를 잊으면 복원할 수 없으니 기관 기준에 따라 따로 보관하세요. 개인 메일·메신저·공개 클라우드에는 올리지 마세요.'),
      pw1, pw2, h('label', { class: 'check' }, withPhotos, ' 사진 포함'), msg),
    isSolo() ? h('section', { class: 'card' }, h('h2', {}, 'PC로 옮기기'), h('p', { class: 'small' }, '나중에 PC 프로그램을 쓰게 되면, 이 백업 파일을 승인된 보안 매체로 PC에 옮긴 뒤 PC [결과 가져오기 → 휴대폰 단독 방식 자료 옮기기]에서 백업 비밀번호로 열면 접수·현장 기록·사진·하차 기록이 PC 원본으로 들어갑니다. 이미 PC에 있는 접수는 덮어쓰지 않습니다.')) : null,
    h('section', { class: 'card' }, h('h2', {}, isSolo() ? '복원 (새 휴대폰으로 옮길 때)' : '복원 (이 휴대폰 자료가 지워졌을 때)'), fileIn, rpw, restore, rmsg),
    footBar(make));
}

// ------------------------------------------------------------ 지도 (온라인 바탕 지도, 개인정보는 보내지 않음)
const mapNotice = () => h('p', { class: 'small' }, '바탕 지도 조각만 OpenFreeMap에서 받습니다. 이름·연락처·주소와 핀 위치는 지도 서버로 보내지 않습니다.');
function mapStops() {
  if (isSolo()) {
    const view = ui.soloView || 'TODAY', today = todayKST(), tomorrow = todayKST(1);
    return st.requests.filter((r) => r.status !== 'CANCELLED' && (view === 'TODAY' ? r.planned_date === today : view === 'TOMORROW' ? r.planned_date === tomorrow : true));
  }
  return st.daylist ? st.daylist.stops.filter((s) => !s.removed) : [];
}

async function screenMap(focusId) {
  // 아래 탭에서 연 지도에는 [← 목록]을 두지 않는다(탭과 중복). 접수에서 연 지도만 [← 접수]
  const back = h('header', { class: 'bar' }, focusId ? btn('← 접수', () => go(`#/stop/${focusId}`), 'ghost') : null, h('h1', { class: 'bar-title' }, '지도'));
  if (!st.mapEnabled) return h('main', {}, back, h('p', { class: 'empty' }, '설정에서 지도 사용이 꺼져 있습니다.'));
  const items = mapStops().map((s) => ({ s, d: deriveStop(s, st.events) }));
  const placed = items.filter((x) => x.d.loc), missing = items.filter((x) => !x.d.loc);
  const box = h('div', { class: 'mapbox', id: 'map' });
  const msg = h('p', { class: 'msg' });
  const view = h('main', { class: focusId ? '' : 'with-tabs' }, back, box,
    h('div', { class: 'grid2' }, btn('📍 내 위치', async () => {
      try { const p = await M.myPosition(); if (liveMap) { liveMap.flyTo({ center: p, zoom: 16 }); M.pin(ui.ml, liveMap, { lng: p[0], lat: p[1], label: '나', cls: 'me' }); } }
      catch (e) { msg.textContent = e.message; }
    }, 'opt'), btn('전체 보기', () => fitAll(), 'opt')),
    msg, mapNotice(),
    missing.length ? h('section', { class: 'card' }, h('h2', {}, `위치 미지정 ${missing.length}건`),
      h('ul', { class: 'stops' }, missing.map(({ s }) => h('li', {}, h('a', { href: `#/pick/${s.request_id}`, class: 'stop' },
        h('span', { class: 'seq' }, s.seq), h('span', { class: 'body' }, h('strong', {}, s.name), h('span', { class: 'addr' }, s.address)),
        h('span', { class: 'eta' }, '위치 지정 →')))))) : null,
    focusId ? null : tabBar('map'));
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
  // id === 'unload': 반입지 위치(이 휴대폰에만 저장, 길안내용)
  const isUnload = id === 'unload';
  const stop = isUnload ? null : findStop(id);
  if (!isUnload && !stop) { go('#/home'); return h('main'); }
  const back = isUnload ? '#/unload' : `#/stop/${id}`;
  const d = isUnload ? { loc: st.unloadSite } : deriveStop(stop, st.events);
  let found = null; // 주소 검색으로 받은 좌표
  const box = h('div', { class: 'mapbox pick', id: 'map' }, h('div', { class: 'crosshair', 'aria-hidden': 'true' }));
  const msg = h('p', { class: 'msg' });
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
    forbid: () => (stop ? [stop.name, stop.phone] : []),
    onPick: (it, ll) => {
      if (!ll) { msg.textContent = '이 주소의 좌표를 받지 못했습니다. 지도를 움직여 맞춰 주세요.'; return; }
      found = ll; save.disabled = false;
      gpsNote.textContent = `주소 검색: ${it.road} (건물 출입구 좌표). 배출 위치가 다르면 지도를 움직여 맞춘 뒤 저장하세요.`;
      if (liveMap) liveMap.jumpTo({ center: [ll.lon, ll.lat], zoom: 18 });
    } });
  const myLoc = btn('📍 지금 내 위치로 이동', async () => {
    msg.textContent = '휴대폰 위치를 찾는 중…';
    const fix = await M.myFix({ ask: true, timeout: 15000 });
    if (!fix) { msg.textContent = '위치를 가져오지 못했습니다. 위치 권한과 GPS를 확인해 주세요.'; return; }
    gps = fix; msg.textContent = '';
    gpsNote.textContent = `내 위치: 오차 약 ${fix.acc}m. 서 있는 곳이 배출 위치면 [이 위치로 저장]을 누르세요.`;
    if (liveMap) { liveMap.jumpTo({ center: [fix.lon, fix.lat], zoom: 18 }); if (ui.pickReady) ui.pickReady(); }
    save.disabled = false;
  }, 'big', { 'aria-label': '지금 내 위치로 이동' });
  const tilesFailed = () => {
    if (mapReady || box.querySelector('.map-empty')) return;
    myLoc.classList.add('primary');
    box.append(h('div', { class: 'map-empty' }, h('p', {}, '지도가 안 보입니다 (인터넷 또는 지도 서버 문제).'),
      h('p', { class: 'small' }, '배출 위치에 서서 아래 [지금 내 위치로 이동]을 누르면 지도 없이도 휴대폰 위치로 저장할 수 있습니다.')));
  };
  setTimeout(async () => {
    try {
      const { map, ml } = await M.createMap(box, { center: d.loc ? [d.loc.lon, d.loc.lat] : M.DEFAULT_CENTER, zoom: d.loc ? 18 : 15 });
      liveMap = map; ui.ml = ml;
      // 지도가 실제로 보일 때만(또는 원래 위치가 있거나 내 위치를 잡았을 때) 지도 가운데로 저장: 빈 화면에서 기본 좌표를 저장하지 않도록
      const ready = () => { mapReady = true; save.disabled = false; const e = box.querySelector('.map-empty'); if (e) e.remove(); myLoc.classList.remove('primary'); };
      if (d.loc) ready();
      map.on('data', (e) => { if (e.dataType === 'source' && e.tile) ready(); });
      map.once('idle', () => { if (map.areTilesLoaded && map.areTilesLoaded() && map.loaded()) ready(); });
      map.on('error', () => setTimeout(tilesFailed, 1500));
      setTimeout(tilesFailed, 8000);
      ui.pickReady = () => { if (gps) { mapReady = true; save.disabled = false; } };
    } catch { msg.textContent = '지도를 불러오지 못했습니다.'; tilesFailed(); }
  }, 0);
  return h('main', {},
    h('header', { class: 'bar' }, btn('← 취소', () => go(back), 'ghost'), h('h1', { class: 'bar-title' }, isUnload ? '반입지 위치 지정' : '배출 위치 지정')),
    isUnload ? h('p', {}, '반입지(하차하는 곳)') : h('p', {}, stop.pii_purged ? stop.receipt_no : `${stop.name} · ${stop.address}`),
    h('p', { class: 'small' }, '주소로 찾거나, 지도를 움직여 가운데 표시를 실제 위치에 맞추거나, 그 자리에 서서 [지금 내 위치로 이동]을 누르세요.'),
    search, box, myLoc, gpsNote, save, msg, mapNotice());
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
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 결과 보내기', () => { clearTimeout(timer); ui.resultQr = null; go('#/export'); }, 'ghost'), h('h1', { class: 'bar-title' }, '결과 QR')),
    flowSteps(['결과 파일', '확인 코드', '사진 파일', '사진 회수 코드', '정리'], 0),
    h('section', { class: 'card center-card' },
      h('p', {}, 'PC [결과 가져오기] → [웹캠으로 읽기]를 누르고 이 화면을 웹캠에 비추세요. 화면 밝기를 최대로 하면 잘 읽힙니다.'),
      h('div', { class: 'qrbox' }, canvas), count,
      h('p', { class: 'small' }, `${r.name} · 이름·연락처·주소는 들어 있지 않습니다. PC가 알려 주는 확인 코드를 [결과 보내기]에 입력하세요.`)),
    footBar(h('div', { class: 'grid2' }, btn('느리게', () => { fps = Math.max(1, fps - 1); }, 'opt'), btn('빠르게', () => { fps = Math.min(8, fps + 1); }, 'opt'))));
}

// ------------------------------------------------------------ 설정
async function screenSettings() {
  const dev = await S.getRaw('device');
  let persisted = '확인 불가';
  try { persisted = navigator.storage && navigator.storage.persisted ? ((await navigator.storage.persisted()) ? '보존 요청됨' : '보존 요청 안 됨') : '지원 안 함'; } catch {}
  let quota = '';
  try { const e = await navigator.storage.estimate(); quota = `${(e.usage / 1048576).toFixed(1)}MB 사용`; } catch {}
  const set = async (patch, msg) => { Object.assign(st, patch); if ('lockMin' in patch) lockMs = patch.lockMin * 60000; await commit(); if (msg) toast(msg); render(); };
  const choice = (opts, cur, onPick) => h('div', { class: 'grid' + Math.min(opts.length, 3) }, ...opts.map(([v, t]) => btn(t, () => onPick(v), cur === v ? 'opt on' : 'opt')));

  // 지문·얼굴 (WebAuthn PRF)
  const bio = await S.getBio().catch(() => null);
  const bioOk = bio ? true : await BIO.bioAvailable();
  const bioCard = h('section', { class: 'card' }, h('h2', {}, '잠금'),
    h('p', { class: 'small' }, '조작하지 않으면 자동으로 잠깁니다. 카메라·전화·문자·길안내 앱에 다녀오는 동안은 10분까지 잠그지 않습니다(그 밖의 다른 앱은 2분).'),
    h('div', { class: 'fld-label' }, '자동 잠금'),
    choice(LOCK_CHOICES.map((m) => [m, `${m}분`]), LOCK_CHOICES.includes(st.lockMin) ? st.lockMin : 10, (v) => set({ lockMin: v }, `${v}분 동안 조작이 없으면 잠급니다`)),
    h('div', { class: 'fld-label' }, '지문·얼굴로 열기'),
    bio ? h('div', {}, h('p', { class: 'small' }, `등록됨 (${bio.at.slice(0, 10)}). 데이터 키는 생체 인증 뒤에만 풀리는 키로 감싸 저장됩니다.`),
      btn('지문·얼굴 등록 해제', async () => { await S.removeBio(); toast('지문·얼굴 잠금 해제를 껐습니다'); render(); }, 'big'))
      : bioOk ? btn('지문·얼굴 잠금 해제 등록', async () => {
        const r = await sheet({ title: '지문·얼굴 등록', text: '먼저 지금 PIN을 넣어 주세요. 그 다음 휴대폰의 지문·얼굴 인증 창이 뜹니다. PIN은 계속 쓸 수 있습니다.',
          input: { type: 'password', label: 'PIN', placeholder: 'PIN', required: 'PIN을 넣어 주세요.' }, choices: [{ label: '다음', value: true, cls: 'primary', needInput: true }] });
        if (!r) return;
        try { const res = await BIO.bioRegister(); await S.enrollBio(r.text, res); toast('지문·얼굴 잠금 해제를 등록했습니다'); }
        catch (e) { toast(e.code === 'BAD_PIN' ? 'PIN이 맞지 않습니다.' : e.code === 'NO_PRF' ? e.message : e.name === 'NotAllowedError' ? '생체 인증을 취소했습니다.' : '등록하지 못했습니다: ' + (e.message || e.name), 'bad'); }
        render();
      }, 'big') : h('p', { class: 'small' }, '이 휴대폰(또는 브라우저)은 키 보호(PRF)를 지원하는 지문·얼굴 인증이 없어 PIN만 씁니다.'));

  // 작업자
  const wIns = [0, 1, 2, 3, 4].map((i) => h('input', { type: 'text', class: 'search', placeholder: `작업자 ${i + 1} (예: 김수거)`, value: (st.workers || [])[i] || '', 'aria-label': `작업자 ${i + 1} 이름`, maxlength: 10 }));
  const workerCard = h('section', { class: 'card' }, h('h2', {}, '작업자'),
    h('p', { class: 'small' }, '이 휴대폰을 함께 쓰는 수거팀 이름(짧게). 첫 화면에서 고른 작업자가 모든 현장 기록에 남습니다. 교대할 때 PIN 없이 바꿀 수 있습니다.'),
    ...wIns, btn('작업자 저장', async () => {
      const ws = [...new Set(wIns.map((x) => x.value.trim().slice(0, 10)).filter(Boolean))];
      await set({ workers: ws, worker: ws.includes(st.worker) ? st.worker : ws[0] || '' }, ws.length ? `작업자 ${ws.length}명을 저장했습니다` : '작업자 목록을 비웠습니다');
    }, 'big'));

  // 문자 문구
  const tpls = smsTemplates();
  const tIns = tpls.map((t) => { const ta = h('textarea', { rows: 3, 'aria-label': `문자 문구: ${t.label}` }); ta.value = t.text; return ta; });
  const smsMsg = h('p', { class: 'msg err', role: 'alert' });
  const smsCard = h('section', { class: 'card' }, h('h2', {}, '문자 문구'),
    h('p', { class: 'small' }, `넣을 수 있는 자리표시: ${Object.entries(F.SMS_VARS).map(([k, v]) => `${k} ${v}`).join(', ')}. 주민 이름·연락처·주소는 넣을 수 없습니다.`),
    ...tpls.map((t, i) => h('label', { class: 'fld' }, h('span', {}, t.label), tIns[i])),
    smsMsg,
    h('div', { class: 'grid2' },
      btn('문구 저장', async () => {
        for (let i = 0; i < tpls.length; i++) { const err = F.checkSmsTemplate(tIns[i].value, allStops()); if (err) { smsMsg.textContent = `${tpls[i].label}: ${err}`; return; } }
        await set({ smsTemplates: tpls.map((t, i) => ({ id: t.id, label: t.label, text: tIns[i].value.trim() })) }, '문자 문구를 저장했습니다');
      }, 'opt'),
      btn('기본 문구로', () => set({ smsTemplates: null }, '기본 문구로 되돌렸습니다'), 'opt')));

  // 지도
  const tc = await M.tileCacheInfo();
  const pmsg = h('p', { class: 'msg', role: 'status' });
  const prefetch = btn('동구 지도 미리 받기', async () => {
    if (!navigator.serviceWorker || !navigator.serviceWorker.controller) { pmsg.textContent = '앱을 한 번 닫았다 다시 연 뒤에 받을 수 있습니다(오프라인 저장 준비 중).'; return; }
    prefetch.disabled = true;
    const pts = allStops().filter((s) => !s.removed && s.status !== 'CANCELLED').map((s) => deriveStop(s, st.events).loc).filter(Boolean);
    try {
      const r = await M.prefetchTiles(M.bboxAround(pts), (n, t) => { pmsg.textContent = `받는 중 ${n} / ${t}`; });
      pmsg.textContent = r.fail === r.total ? '지도 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.' : `${pts.length ? '오늘 지점 주변' : '동구 전체'} 지도 조각 ${r.total - r.fail}개를 받았습니다${r.fail ? ` (실패 ${r.fail})` : ''}.`;
    } catch { pmsg.textContent = '지도 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.'; }
    prefetch.disabled = false;
  }, 'big', { disabled: !st.mapEnabled });
  const mapCard = h('section', { class: 'card' }, h('h2', {}, '지도'),
    h('p', { class: 'small' }, '지도를 켜면 바탕 지도 조각을 인터넷(OpenFreeMap)에서 받습니다. 이름·연락처·주소·핀 위치는 지도 서버로 보내지 않습니다.'),
    btn(st.mapEnabled ? '지도 사용 끄기' : '지도 사용 켜기', async () => { st.mapEnabled = !st.mapEnabled; await commit(); render(); }, 'big'),
    h('p', { class: 'small' }, `인터넷이 약한 곳에 대비해 오늘 지점 주변(없으면 동구 전체)의 넓은 범위 지도(줌 14 이하)를 미리 받아 둡니다. 보관 ${tc.count}개 · ${(tc.bytes / 1048576).toFixed(1)}MB (최대 50MB, 오래 안 쓴 것부터 지움)`),
    prefetch, pmsg);

  // 길안내 앱
  const navCard = h('section', { class: 'card' }, h('h2', {}, '길안내 앱'),
    h('div', { class: 'grid2', role: 'group', 'aria-label': '길안내 앱' }, ...Object.entries(NAV.NAV_APPS).map(([k, t]) =>
      btn(t, () => set({ navApp: k }, `길안내는 ${t}로 엽니다`), (st.navApp || NAV.DEFAULT_NAV_APP) === k ? 'opt on' : 'opt'))),
    h('p', { class: 'small' }, "지도 앱에는 목적지 좌표와 'N번 수거지'(반입지는 '반입지') 표시만 전달됩니다. 이름·연락처·주소는 넘기지 않습니다. 앱이 없으면 카카오맵·네이버지도는 웹 지도로, T맵은 기본 지도 앱으로 엽니다."));

  // 주소 검색 (행정안전부 도로명주소 API)
  const kIn = h('input', { type: 'text', class: 'search', autocomplete: 'off', spellcheck: 'false', 'aria-label': '주소 검색 승인키', placeholder: '주소 검색 승인키 (confmKey)', value: st.jusoKey || '' });
  const cIn = h('input', { type: 'text', class: 'search', autocomplete: 'off', spellcheck: 'false', 'aria-label': '좌표 승인키', placeholder: '좌표제공 승인키 (confmKey)', value: st.jusoCoordKey || '' });
  const jusoCard = h('section', { class: 'card' }, h('h2', {}, '주소 검색'),
    h('p', { class: 'small' }, '행정안전부 도로명주소 검색·좌표제공 API(business.juso.go.kr)를 씁니다. 보내는 것은 검색칸에 적은 주소 낱말과 승인키, 고른 건물의 식별 번호뿐이며 이름·연락처는 보내지 않습니다. 승인키는 주소정보누리집에서 기관이 발급받아 넣습니다(이 휴대폰에 암호화 저장).'),
    h('div', { class: 'fld-label' }, '주소 검색 경로'),
    choice([['direct', '직접'], ['pc', 'PC 경유']], addrRoute(), (v) => set({ addrRoute: v })),
    h('p', { class: 'small' }, addrRoute() === 'pc' ? 'PC 경유: 휴대폰에서는 검색하지 않습니다. PC 연결 방식에서는 PC가 주소·위치를 정해 목록으로 보냅니다(휴대폰과 PC는 실시간으로 연결되지 않음).'
      : '직접: 이 휴대폰이 주소 서버에 바로 묻습니다. 브라우저가 주소 서버 응답을 막으면(CORS) 검색이 되지 않으니, 그때는 직접 적고 지도에서 위치를 지정하거나 PC에서 주소 작업을 하세요.'),
    h('label', { class: 'fld' }, h('span', {}, '주소 검색 승인키'), kIn),
    h('label', { class: 'fld' }, h('span', {}, '좌표 승인키'), cIn),
    btn('승인키 저장', () => set({ jusoKey: kIn.value.trim(), jusoCoordKey: cIn.value.trim() }, kIn.value.trim() ? '승인키를 저장했습니다' : '승인키를 지웠습니다'), 'big'));

  return h('main', { class: 'with-tabs' },
    h('header', { class: 'bar' }, h('h1', { class: 'bar-title' }, '설정·정보')),
    // 화면(밝기·글자 크기)은 맨 위에: 자주 바꾸고, '자동'이 휴대폰의 어두운 화면을 따라가 어둡게만 보이는 경우가 있어 찾기 쉽게
    h('section', { class: 'card screen-card' }, h('h2', {}, '화면'),
      h('div', { class: 'fld-label' }, '밝기'),
      h('div', { class: 'seg theme-seg', role: 'group', 'aria-label': '밝기 테마' }, ...[['auto', '자동'], ['light', '밝게'], ['dark', '어둡게']].map(([k, t]) =>
        btn([h('span', { class: 'tp tp-' + k, 'aria-hidden': 'true' }, h('i'), h('i')), h('span', {}, t)], async () => { await setPrefs({ theme: k }); render(); },
          'opt seg-opt' + ((prefs.theme || 'auto') === k ? ' on' : ''), { 'aria-label': t }))),
      h('p', { class: 'small' }, (prefs.theme || 'auto') === 'auto' ? `자동: 휴대폰 설정을 따릅니다 (지금 ${isDark() ? '어둡게' : '밝게'}). 첫 화면 위 ☀/🌙로 바로 바꿀 수 있습니다.` : '첫 화면 위 ☀/🌙로도 바꿀 수 있습니다.'),
      h('div', { class: 'fld-label' }, '글자 크기'),
      h('div', { class: 'seg size-seg', role: 'group', 'aria-label': '글자 크기' }, ...TEXT_SIZES.map(([k, t, px]) =>
        btn(t, async () => { st.textSize = k; applyTextSize(); await commit(); render(); }, 'opt seg-opt' + ((st.textSize || '') === k ? ' on' : ''), { 'data-px': px }))),
      h('div', { class: 'fld-label' }, '야외 고대비'),
      h('p', { class: 'small' }, '햇빛 아래에서 잘 보이게 글자·테두리를 진하게 합니다. 휴대폰의 "대비 높이기" 설정을 켜도 적용됩니다.'),
      choice([[false, '끔'], [true, '켬']], !!prefs.hc, async (v) => { await setPrefs({ hc: v }); render(); })),
    h('section', { class: 'card' }, h('dl', {},
      h('dt', {}, '앱 버전'), h('dd', {}, APP_VERSION),
      h('dt', {}, '사용 방식'), h('dd', {}, st.mode === 'SOLO' ? '이 휴대폰만 사용' : 'PC와 연결'),
      h('dt', {}, '기기 번호'), h('dd', {}, dev ? P.uuidStr(dev.id).slice(0, 8) : '미등록'),
      h('dt', {}, '저장소'), h('dd', {}, `${persisted} ${quota}`),
      h('dt', {}, '미전송 기록'), h('dd', {}, unsent().length + '건'))),
    bioCard, workerCard,
    h('section', { class: 'card' }, h('h2', {}, '현장 기록'),
      h('div', { class: 'fld-label' }, '도착·사진에 휴대폰 위치 남기기'),
      h('p', { class: 'small' }, '[도착]을 누르거나 사진을 찍을 때 위치 권한이 허용되어 있으면 위치·오차를 함께 기록합니다(결과 파일로 PC에만 전달).'),
      choice([[true, '남김'], [false, '안 남김']], st.geoEvidence !== false, (v) => set({ geoEvidence: v })),
      isSolo() ? null : h('div', {}, h('div', { class: 'fld-label' }, '미전송 알림'),
        h('p', { class: 'small' }, '보내지 않은 기록이 이 시간보다 오래되면 첫 화면에 알립니다. 오후 5시 뒤에는 1건이라도 있으면 알립니다.'),
        choice([[1, '1시간'], [3, '3시간'], [6, '6시간']], st.unsentWarnH || 3, (v) => set({ unsentWarnH: v })),
        btn('안전용 암호화 백업', () => go('#/backup'), 'big')),
      isSolo() ? h('div', {}, h('div', { class: 'fld-label' }, '개인정보 보관 기간'),
        h('p', { class: 'small' }, '처리가 끝난 접수는 이 기간이 지나고 백업이 끝났으면 첫 화면에서 알린 뒤 이름·연락처·주소를 지웁니다.'),
        choice([[30, '30일'], [90, '90일'], [180, '180일']], st.retentionDays || 90, (v) => set({ retentionDays: v }))) : null),
    smsCard, navCard, jusoCard, mapCard,
    isSolo() ? soloFeeCard() : null,
    h('section', { class: 'card' },
      isSolo() ? null : btn('기기 등록 화면', () => go('#/register'), 'big'),
      btn('사용 방식 바꾸기', async () => {
        if (st.requests.length || st.daylist || st.events.length) return toast('자료가 있어 바꿀 수 없습니다. 백업·정리 후 바꿔 주세요.', 'bad');
        st.mode = null; await commit(); go('#/mode');
      }, 'big'),
      h('p', { class: 'small' }, '이 앱은 서버에 자료를 보내지 않습니다. 브라우저의 "사이트 데이터 삭제"를 하면 보내지 않은 기록이 사라지니 주의하세요.'),
      btn('이 휴대폰의 모든 자료 지우기', async () => {
        const u = unsent().length;
        if (!(await ask((u ? `보내지 않은 기록 ${u}건이 사라집니다. ` : '') + 'PIN, 기기 키, 목록, 사진을 모두 지웁니다. 다시 등록해야 합니다. 되돌릴 수 없습니다.', '모두 지우기', { title: '이 휴대폰의 모든 자료 지우기', danger: true }))) return;
        stopCamera(); await S.wipeAll(); st = null; location.hash = ''; location.reload();
      }, 'danger')),
    tabBar('set'));
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
// 글자 크기: 뿌리 글자 크기(--fs) 하나로 모든 글자가 함께 바뀐다(style.css). 아주 크게는 배치도 넓힌다(text-big).
const TEXT_SIZES = [['s', '작게', 14], ['', '보통', 15], ['l', '크게', 17], ['xl', '아주 크게', 19]];
function applyTextSize() {
  const r = document.documentElement, k = st && TEXT_SIZES.some(([x]) => x === st.textSize) ? st.textSize : '';
  for (const [x] of TEXT_SIZES) if (x) r.classList.toggle('fs-' + x, x === k);
  r.classList.toggle('text-big', k === 'xl');
}

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
    if (!isSolo() && ['intake'].includes(route)) route = 'home';
    view = await ({
      home: screenHome, scan: screenScan, received: screenReceived, register: screenRegister,
      'qrtest-result': screenQrTestResult, resultqr: screenResultQr, stop: () => screenStop(arg), add: () => screenAdd(arg), export: screenExport, settings: screenSettings, unload: screenUnload,
      mode: screenMode, intake: () => screenIntake(arg), backup: screenBackup, map: () => screenMap(arg), pick: () => screenPick(arg),
    }[route] || screenHome)();
  }
  if (my !== renderSeq) return; // 그 사이 다른 화면으로 이동
  wantWake(S.isUnlocked() && wakeRoute());
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

// 화면 설정(테마·고대비)은 잠금 화면 전에 적용
S.getRaw('prefs').then((p) => { if (p) prefs = { ...prefs, ...p }; }).catch(() => {}).finally(() => { applyPrefs(); render(); });
