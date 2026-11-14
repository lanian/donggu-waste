// 휴대폰 앱 공통: 화면 조각(h·btn·시트)·업무 상태(st, 잠금 해제 동안만)·저장(commit)·기록 추가(addEvent)·잠금·사진 저장·카메라·라우터 연결(render).
// 화면 모듈은 이 파일과 widgets.js 를 기대고, 상태를 통째로 바꿀 때는 setSt() 등 설정 함수를 쓴다(다른 모듈의 let 은 다시 대입할 수 없다).
import * as F from './field.js';
import * as M from './map.js';
import * as NAV from './nav.js';
import * as P from './proto.js';
import * as S from './store.js';
import { BUILTIN_FEES } from './fees-data.js';

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
  // 한 번에 하나만. 단, 안내 알림이 오류 알림을 덮지 않는다(오류는 눌러서 닫을 때까지)
  document.querySelectorAll(kind === 'bad' ? '.toast' : '.toast:not(.bad)').forEach((x) => x.remove());
  const bad = kind === 'bad';
  const t = h('div', { class: 'toast ' + kind, role: bad ? 'alert' : 'status' }, msg, bad ? h('span', { class: 'tx' }, '닫기 ✕') : null);
  if (bad) { t.tabIndex = 0; const close = () => t.remove(); t.addEventListener('click', close); t.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === 'Escape') close(); }); }
  else setTimeout(() => t.remove(), 4000);
  document.body.append(t);
};

// 아래에서 올라오는 확인 창 (window.prompt·confirm 대신). 큰 단추, 큰 글씨 설정을 따른다.
// choices: [{label, value, cls, needInput}] → 고른 value (입력칸이 있으면 {value, text}), 취소·뒤로 가기는 null
const openSheets = new Set();
function sheet({ title, text = '', body = null, choices = [], input = null, cancel = '취소', cancelCls = 'ghost big', signal = null }) {
  return new Promise((resolve) => {
    const opener = document.activeElement, ownerState = st, ownerHash = location.hash;
    const dlg = h('dialog', { class: 'sheet', 'aria-label': title });
    const inp = input ? h(input.multiline ? 'textarea' : 'input', { type: input.type || 'text', class: 'search', placeholder: input.placeholder || '', 'aria-label': input.label || input.placeholder || title, autocomplete: 'off', rows: input.multiline ? 3 : null,
      min: input.min ?? null, max: input.max ?? null, step: input.step ?? null, inputmode: input.inputmode || null }) : null;
    if (inp && input.value != null) inp.value = input.value;
    const msg = h('p', { class: 'msg err', role: 'alert' });
    const abort = () => done(null);
    const done = (v) => {
      openSheets.delete(done); signal?.removeEventListener('abort', abort);
      try { dlg.close(); } catch {} dlg.remove();
      if (st === ownerState && location.hash === ownerHash && opener?.isConnected) opener.focus({ preventScroll: true });
      resolve(v);
    };
    openSheets.add(done);
    // 닫기 쉽게(취소할 수 있는 창만): 위 ✕, 바깥(어두운 곳) 누르기, 손잡이·제목을 아래로 끌기, 뒤로 가기
    const grip = h('div', { class: 'grip', 'aria-hidden': 'true' });
    const head = h('div', { class: 'sheet-head' }, h('h2', {}, title), cancel ? btn('✕', () => done(null), 'ghost sheet-x', { 'aria-label': '닫기' }) : null);
    const kids = [grip, head, text ? h('p', { class: 'sheet-text' }, text) : null, body,
      inp ? h('label', { class: 'fld' }, h('span', {}, input.label || ''), inp) : null, msg,
      h('div', { class: 'sheet-actions' }, ...choices.map((c) => btn(c.label, () => {
        if (inp && c.needInput && !inp.value.trim()) { msg.textContent = input.required || '내용을 적어 주세요.'; inp.focus(); return; }
        if (inp && input.validate) {
          const error = input.validate(inp.value.trim());
          if (error) { msg.textContent = error; inp.setAttribute('aria-invalid', 'true'); inp.focus(); return; }
          inp.removeAttribute('aria-invalid');
        }
        done(inp ? { value: c.value, text: inp.value.trim() } : c.value);
      }, 'huge ' + (c.cls || ''))), cancel ? btn(cancel, () => done(null), cancelCls) : null)];
    dlg.append(...kids.filter(Boolean));
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); if (cancel) done(null); });
    if (cancel) {
      dlg.addEventListener('click', (e) => {
        if (e.target !== dlg) return;
        const r = dlg.getBoundingClientRect();
        if (e.clientY < r.top || e.clientY > r.bottom || e.clientX < r.left || e.clientX > r.right) done(null);
      });
      let y0 = null, dy = 0;
      const startDrag = (e) => { y0 = e.clientY; dy = 0; dlg.style.transition = 'none'; };
      const moveDrag = (e) => { if (y0 == null) return; dy = Math.max(0, e.clientY - y0); dlg.style.transform = `translateY(${dy}px)`; };
      const endDrag = () => { if (y0 == null) return; y0 = null; dlg.style.transition = ''; if (dy > 70) done(null); else dlg.style.transform = ''; };
      for (const el of [grip, head]) { el.addEventListener('pointerdown', startDrag); }
      dlg.addEventListener('pointermove', moveDrag); dlg.addEventListener('pointerup', endDrag); dlg.addEventListener('pointercancel', endDrag);
    }
    document.body.append(dlg);
    if (signal?.aborted) { done(null); return; }
    signal?.addEventListener('abort', abort, { once: true });
    try { dlg.showModal(); } catch { dlg.setAttribute('open', ''); }
    setTimeout(() => { if (dlg.isConnected) (inp || dlg.querySelector('.sheet-actions .btn') || dlg.querySelector('.btn')).focus(); }, 30);
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
  QR_OLD_DATE: '지금 목록보다 지난 날짜의 목록입니다. 받지 않았습니다. PC 담당자에게 오늘 목록 QR을 띄워 달라고 하세요.',
  BAD_VERSION: '앱 업데이트가 필요합니다. 앱을 닫았다 다시 열고 위에 뜨는 [업데이트]를 누르세요.',
  QR_NOT_REGISTRATION: '기기 등록용 QR이 아닙니다.',
  UNSENT_PREVIOUS: '이전 날짜 결과가 아직 PC에 반영되지 않아 새 날짜 목록을 받지 않았습니다. 결과를 보내고 결과 반영 코드(D-)를 넣거나, PC가 결과를 가져온 뒤 만든 목록을 받아 주세요.',
  STORAGE_LOW: '저장 공간이 부족합니다. 결과를 먼저 보내 주세요.',
  STORAGE_LOW_PHOTO: '저장 공간이 부족해 사진을 아직 저장하지 못했습니다. 사진은 지우지 않고 대기 중입니다 — 앱을 닫지 말고, 결과·사진을 보내 휴대폰 공간을 비운 뒤 첫 화면의 [사진 다시 저장]을 누르세요.',
  PHOTO_SAVE_FAIL: '사진이 저장되지 않았습니다. 다시 찍어 주세요.',
  FEE_VERSION_MISSING: 'PC와 앱의 수수료표 버전이 다릅니다. 앱을 업데이트하거나 담당자에게 문의하세요.',
};
const errMsg = (e) => ERR[e && e.code] || (e && e.message) || '처리하지 못했습니다. 다시 시도하고, 계속되면 담당자에게 이 화면을 보여 주세요.';

// ------------------------------------------------------------ 상태 저장
const emptyState = () => ({ mode: null, daylist: null, events: [], seq: 0, releasedSeq: 0, exports: [], lastBundleHash: null,
  requests: [], receiptCounter: {}, lastBackupAt: null, intakeDraft: null, mapEnabled: true,
  lockMin: 10, workers: [], worker: '', smsTemplates: null, navApp: NAV.DEFAULT_NAV_APP, jusoKey: '', jusoCoordKey: '', addrRoute: '', unloadSite: null, unsentWarnH: 3, retentionDays: 90, listChanges: {}, recentDongs: [], geoEvidence: true,
  fieldStops: [], localOrder: null });
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
  F.ensureGen(st); // 기록 세대(v0.13): 처음 쓰는 휴대폰·이전 버전 자료는 여기서 세대를 만든다(복원 때는 mergeRestore가 새로 만듦)
  lockMs = (LOCK_CHOICES.includes(st.lockMin) ? st.lockMin : 10) * 60000;
  autoLockEnabled = await S.readAutoLock().catch(() => true);
  lastActive = Date.now();
}
// 잠금 해제 직후: 화면 그리기, 기다리던 사진 저장
// 창(설치한 앱·브라우저 탭)이 둘 이상 열려 있으면 서로 저장을 덮어쓴다: 잠금을 푼 창 하나만 쓰고 나머지는 잠근다
const WIN_ID = crypto.randomUUID();
let sessionChan = null;
try {
  sessionChan = new BroadcastChannel('dg-session');
  sessionChan.onmessage = async (e) => {
    if (e.data && e.data.type === 'active' && e.data.id !== WIN_ID && S.isUnlocked()) {
      await saving; await lockNow(); toast('다른 창에서 앱을 열어 이 창은 잠갔습니다. 기록은 한 창에서만 하세요.', 'bad');
    }
  };
} catch {}
async function afterUnlock() {
  try { sessionChan && sessionChan.postMessage({ type: 'active', id: WIN_ID }); } catch {}
  await loadState();
  if (S.hasPendingSecure('state')) await commit();  // 잠금 전에 못 쓴 암호문을 복원했으면 다시 저장한다.
  // 자료 보존(persist) 요청 결과를 상태에 남긴다: 거절되면 첫 화면에 알림(바뀌었을 때만 저장)
  S.persist().then((ok) => { if (st && st.storagePersisted !== !!ok) { st.storagePersisted = !!ok; commit(); if (location.hash === '#/home') render(); } }).catch(() => {});
  if (!location.hash || location.hash === '#/') go('#/home');
  await render();
  if (pendingPhotos.length) await flushPhotos();
  if (ui.afterCall && location.hash === `#/stop/${ui.afterCall}`) { const c = document.getElementById('st-contact'); if (c) c.scrollIntoView({ block: 'start' }); }
}

// 사용 방식: 'PC' = PC가 원본, QR로 목록 받기 / 'SOLO' = 이 휴대폰에서 접수부터 수거까지
const isSolo = () => st.mode === 'SOLO';
// PC 연결 방식: PC 목록 + 이 휴대폰에서 받은 현장 접수(PC 목록에 들어올 때까지). 방문 순서를 이 휴대폰에서만 바꿨으면 그 순서로.
const pcWorkDate = () => (st.daylist ? st.daylist.work_date : todayKST());
function pcStops() {
  const dl = st.daylist ? st.daylist.stops : [];
  const ids = new Set(dl.map((s) => s.request_id));
  const list = [...dl, ...(st.fieldStops || []).filter((s) => !ids.has(s.request_id))];
  const lo = st.localOrder;
  if (!lo || lo.work_date !== pcWorkDate() || lo.assign_ver !== (st.daylist ? st.daylist.assign_ver : 0)) return list;
  const idx = (id) => { const i = lo.ids.indexOf(id); return i < 0 ? 1e6 : i; };
  return list.map((s, i) => [s, i]).sort((a, b) => idx(a[0].request_id) - idx(b[0].request_id) || a[1] - b[1]).map((x) => x[0]);
}
const allStops = () => (isSolo() ? st.requests : pcStops());
// PC 연결 방식에서 보낸 변경 요청(정정·취소): PC 확인 전까지는 원본을 바꾸지 않고 표시만 한다
const changeReqs = (id) => st.events.filter((e) => e.request_id === id && (e.kind === 'CORRECTION' || e.kind === 'CANCEL_REQUEST'));
const cancelAsked = (id) => !isSolo() && changeReqs(id).some((e) => e.kind === 'CANCEL_REQUEST');
// 이 휴대폰에서 받은 접수 중 아직 결과 파일로 내보내지 않은 것(내보낸 적 있으면 PC가 받았을 수 있어 고치지 않음)
function fieldEditable(id) {
  const stop = (st.fieldStops || []).find((x) => x.request_id === id);
  const ev = st.events.find((e) => e.kind === 'FIELD_INTAKE' && e.request_id === id);
  if (!stop || !ev || ev.seq <= st.releasedSeq || (st.exports || []).some((x) => ev.seq >= x.from && ev.seq <= x.to)) return null;
  return { stop, ev };
}
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
// 저장 실패는 알림 한 번으로 끝내지 않는다: 다시 저장될 때까지 화면에 계속 띄운다(메모리에만 있는 기록이 앱 종료로 사라지지 않게)
let saveFailed = false;
function saveBanner(msg) {
  let b = document.getElementById('save-fail');
  if (!msg) { if (b) b.remove(); return; }
  if (!b) {
    b = h('div', { id: 'save-fail', class: 'save-fail', role: 'alert' },
      h('details', {}, h('summary'), h('p', { class: 'save-fail-detail' })));
    document.body.append(b);
  }
  b.querySelector('summary').textContent = `${msg.includes('공간') ? '저장 공간 부족' : '기록 저장 실패'} · 앱을 닫지 마세요`;
  b.querySelector('.save-fail-detail').textContent = `${msg} 앱을 닫지 말고 이 창의 기록을 [결과 보내기] 또는 백업으로 먼저 내보내 주세요.`;
}
function commit() {
  const snapshot = JSON.parse(JSON.stringify(st));
  saving = saving.then(() => S.putSecure('state', snapshot)).then(() => {
    if (saveFailed) { saveFailed = false; saveBanner(''); toast('다시 저장됩니다'); if (reloadWanted) setTimeout(reloadWhenSafe, 0); }
    return true;
  }).catch((e) => {
    saveFailed = true;
    saveBanner(e && e.name === 'QuotaExceededError' ? ERR.STORAGE_LOW : '기록을 저장하지 못했습니다(' + errMsg(e) + ').');
    return false;
  });
  return saving;
}
// 모든 기록에 지금 작업자(설정의 이름 중 고른 사람)를 남긴다
const withWorker = (body) => (st.worker ? { ...body, worker: st.worker } : body);
async function addEvent(stop, kind, body) {
  if (!st) throw Object.assign(new Error('잠겨 있습니다'), { code: 'LOCKED' });
  st.seq += 1;
  st.events.push({
    event_id: crypto.randomUUID(), seq: st.seq, gen: F.ensureGen(st), request_id: stop.request_id,
    assign_ver: stop.assign_ver ?? (st.daylist ? st.daylist.assign_ver : 1), base_req_ver: stop.req_ver, kind,
    body: withWorker({ ...body, ...(stop.pc_collection_ver ? { pc_collection_ver: stop.pc_collection_ver } : {}) }), ts: new Date().toISOString(),
  });
  return commit();
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
  st.events.push({ event_id: crypto.randomUUID(), seq: st.seq, gen: F.ensureGen(st), request_id: '', assign_ver: st.daylist ? st.daylist.assign_ver : 0, base_req_ver: null, kind, body: withWorker(body), ts: new Date().toISOString() });
  await commit();
}
const unsent = () => st.events.filter((e) => e.seq > st.releasedSeq);

// ------------------------------------------------------------ 잠금
let lockMs = 10 * 60000; // 잠금 해제 동안 설정값(5·10·15분)
let autoLockEnabled = true, autoLockChange = null, autoLockError = '';
const autoLockStatus = () => ({ enabled: autoLockEnabled, pending: autoLockChange, error: autoLockError });
function setAutoLockEnabled(enabled) {
  if (autoLockChange) return autoLockChange;
  if (typeof enabled !== 'boolean' || !st || !S.isUnlocked()) return Promise.resolve(false);
  autoLockError = '';
  autoLockChange = (async () => {
    try {
      await S.saveAutoLock(enabled);
      autoLockEnabled = enabled;
      lastActive = Date.now(); hiddenAt = 0; handoffUntil = 0;
      return true;
    } catch {
      autoLockError = '자동 잠금 설정을 저장하지 못했습니다. 이전 설정을 유지합니다.';
      return false;
    } finally { autoLockChange = null; }
  })();
  return autoLockChange;
}
// 잠그기 전에: 모아 둔 수량(stepper)·저장 중인 기록·저장 중인 사진이 모두 끝나야 데이터 키를 버린다.
// (키를 먼저 버리면 뒤따르는 암호화 저장이 '잠겨 있습니다'로 실패해 메모리에만 있던 기록·사진이 사라진다)
let locking = null;
async function settleWrites() {
  for (let i = 0; i < 10; i++) {
    const s0 = saving, p0 = photoFlushing;
    try { await flushSteps(); } catch {}
    try { await s0; } catch {}
    if (p0) { try { await p0; } catch {} }
    if (s0 === saving && p0 === photoFlushing && !stepFlush.size) return;
  }
}
function lockNow() {
  if (locking) return locking;
  locking = (async () => {
    try { if (S.isUnlocked() && st) await settleWrites(); } catch {}
    stopCamera(); S.lock(); st = null; wantWake(false);
    for (const done of [...openSheets]) done(null);
  })().finally(() => { locking = null; });
  return locking.then(() => render());
}
['pointerdown', 'keydown'].forEach((t) => addEventListener(t, () => { lastActive = Date.now(); }, { passive: true }));
setInterval(() => {
  // 화면이 가려져 있거나 다른 앱으로 넘어간 동안에는 무조작으로 세지 않는다(돌아올 때 따로 판단)
  if (autoLockEnabled && S.isUnlocked() && !document.hidden && Date.now() > handoffUntil && Date.now() - lastActive > lockMs) lockNow();
}, 15000);
document.addEventListener('visibilitychange', () => {
  // 최근 앱 화면 미리보기에 이름·주소가 남지 않게 가린다
  document.documentElement.classList.toggle('veil', document.hidden && S.isUnlocked());
  if (document.hidden) { hiddenAt = Date.now(); stopCamera(); return; }
  const away = hiddenAt ? Date.now() - hiddenAt : 0;
  const limit = hiddenAt && hiddenAt <= handoffUntil ? HANDOFF_MS : HIDDEN_LOCK_MS;
  handoffUntil = 0; hiddenAt = 0;
  if (autoLockEnabled && S.isUnlocked() && away > limit) { lockNow(); return; }
  lastActive = Date.now();
  if (S.isUnlocked() && wakeRoute()) wantWake(true);
  if (S.isUnlocked() && ui.afterCall && location.hash === `#/stop/${ui.afterCall}`) render().then(() => { const c = document.getElementById('st-contact'); if (c) c.scrollIntoView({ block: 'start' }); });
  else if (location.hash.startsWith('#/scan') || location.hash.startsWith('#/register')) render();
});

// 화면 켜짐 유지: 목록·상세·PC 입력 참고 화면. 지원하지 않거나 거절되면 조용히 넘어간다.
let wakeLock = null;
const wakeRoute = () => /^#\/(home|stop|pc-input)\b/.test(location.hash || '#/home');
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
// 사진 저장은 한 번에 하나의 흐름만(photoFlushing). 잠금은 이 흐름이 끝날 때까지 기다린다.
// 잠김·저장 공간 부족으로 실패하면 사진을 대기열에 그대로 두고(버리지 않음) 잠금 해제·공간 확보 뒤 다시 저장한다.
let photoFlushing = null;
const isQuota = (e) => !!e && (e.name === 'QuotaExceededError' || e.code === 'STORAGE_LOW' || (e.inner && e.inner.name === 'QuotaExceededError'));
function flushPhotos() {
  if (photoFlushing) return photoFlushing.then(() => (pendingPhotos.length && S.isUnlocked() && st && !locking ? flushPhotos() : 0));
  photoFlushing = flushPhotosNow().finally(() => { photoFlushing = null; });
  return photoFlushing;
}
async function flushPhotosNow() {
  let n = 0;
  while (pendingPhotos.length && S.isUnlocked() && st && !locking) {
    const p = pendingPhotos[0];
    const stop = findStop(p.stopId);
    if (!stop) { pendingPhotos.shift(); toast('사진을 붙일 접수를 찾지 못했습니다. 다시 찍어 주세요.', 'bad'); continue; }
    try { await savePhoto(stop, p.phase, p.file, p.lineId, p); pendingPhotos.shift(); n++; }
    catch (e) {
      if (e && e.code === 'LOCKED') break;  // 잠금 해제 뒤 afterUnlock에서 다시
      if (isQuota(e)) { toast(ERR.STORAGE_LOW_PHOTO, 'bad'); break; }  // 대기열에 남김
      p.fails = (p.fails || 0) + 1;
      if (p.fails < 3) continue;  // 일시 오류는 몇 번 더
      pendingPhotos.shift(); toast(ERR.PHOTO_SAVE_FAIL, 'bad');  // 읽을 수 없는 그림 파일 등
    }
  }
  if (n) toast(n > 1 ? `사진 ${n}장을 저장했습니다` : '사진을 저장했습니다');
  if (n && S.isUnlocked() && st && !locking) await render();
  if (reloadWanted && !pendingPhotos.length) setTimeout(reloadWhenSafe, 0);
}

const dongOf = (s) => (s.dong || '').trim();
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
const mdOf = (d) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d || ''); return m ? `${Number(m[2])}월 ${Number(m[3])}일` : d || ''; };
// 빠르게 여러 번 눌러도 수량이 빠지지 않게: 화면 숫자는 바로 바꾸고 저장은 잠깐 모아서 한 번
const stepFlush = new Map();  // 모아 두었다 저장할 수량 변경: 다른 동작(결과 저장·화면 이동) 전에 먼저 저장
async function flushSteps() { for (const f of [...stepFlush.values()]) await f(); }
async function quantityInput(value, min = 0, max = 9999, label = '수량') {
  if (!st || !S.isUnlocked()) return null;
  const ownerState = st, ownerHash = location.hash, controller = new AbortController();
  const leave = () => controller.abort();
  window.addEventListener('hashchange', leave, { once: true });
  try {
    const result = await sheet({ title: '수량 직접 입력', text: `${min}~${max} 사이의 정수를 입력하세요.`, signal: controller.signal,
      input: { type: 'number', inputmode: 'numeric', label, value, min, max, step: 1,
        validate: (raw) => /^\d+$/.test(raw) && Number.isSafeInteger(Number(raw)) && Number(raw) >= min && Number(raw) <= max ? '' : `${min}~${max} 사이의 정수를 입력해 주세요.` },
      choices: [{ label: '적용', value: true, cls: 'primary' }] });
    return result && st === ownerState && location.hash === ownerHash && S.isUnlocked() ? Number(result.text) : null;
  } finally { window.removeEventListener('hashchange', leave); }
}
function quantityValue(value, onChange, min = 0, max = 9999, label = '수량') {
  const ownerState = st, ownerHash = location.hash, ownerForm = ui.form;
  let ownerDialog = null;
  const name = `${label} 직접 입력`;
  const restore = () => {
    if (st !== ownerState || location.hash !== ownerHash || ui.form !== ownerForm || !S.isUnlocked()) return;
    const topDialog = [...document.querySelectorAll('dialog[open]')].at(-1);
    if (topDialog && topDialog !== ownerDialog) return;
    if (document.activeElement !== document.body && document.activeElement !== v && document.activeElement !== ownerDialog && document.activeElement?.isConnected) return;
    const matches = [...(ownerDialog?.isConnected ? ownerDialog : document).querySelectorAll('.qty-value')].filter((e) => e.getAttribute('aria-label') === name && !e.disabled);
    if (matches.length === 1) matches[0].focus({ preventScroll: true });
  };
  const v = btn(String(value), async () => {
    ownerDialog = v.closest('dialog[open]');
    const n = await quantityInput(Number(v.textContent), min, max, label);
    if (n == null || !v.isConnected || st !== ownerState || location.hash !== ownerHash || ui.form !== ownerForm) return;
    await onChange(n);
    restore();
  }, 'val qty-value', { 'aria-label': name });
  v.restoreQuantityFocus = restore;
  return v;
}
function stepper(value, onChange, min = 0, max = 99, delay = 0, label = '', options = {}) {
  let cur = value, timer = null;
  let restoreValueFocus = false;
  const key = Symbol('step');
  const v = options.editable ? quantityValue(value, (n) => change(n, true), min, max, label) : h('span', { class: 'val', 'aria-live': 'polite' }, value);
  const minus = btn('−', () => bump(-1), 'step', { 'aria-label': `${label} 빼기`.trim(), disabled: value <= min });
  const plus = btn('+', () => bump(1), 'step', { 'aria-label': `${label} 더하기`.trim(), disabled: value >= max });
  function bump(dlt) {
    const n = Math.min(max, Math.max(min, cur + dlt));
    return change(n);
  }
  function change(n, direct = false) {
    if (n === cur) return;
    cur = n; v.textContent = n; minus.disabled = n <= min; plus.disabled = n >= max;
    if (!delay) return onChange(cur);
    restoreValueFocus = direct;
    if (options.editable) v.disabled = true;
    clearTimeout(timer);
    const fire = () => {
      clearTimeout(timer); stepFlush.delete(key);
      const result = onChange(cur), restore = restoreValueFocus;
      const settled = () => { if (v.isConnected) v.disabled = false; if (restore) v.restoreQuantityFocus?.(); };
      Promise.resolve(result).then(settled, settled);
      return result;
    };
    stepFlush.set(key, fire); timer = setTimeout(fire, delay);
  }
  return h('span', { class: 'stepper' }, minus, v, plus);
}

// ------------------------------------------------------------ 현장 보조: 도착·문자·길안내·스티커·금액 고지·증거
const smsTemplates = () => (Array.isArray(st.smsTemplates) && st.smsTemplates.length ? st.smsTemplates : F.SMS_DEFAULTS);
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
// 순서: 1) 암호화 사진 저장(같은 ID면 덮어씀) 2) 잠금 여부를 다시 보고 같은 순간에 기록(PHOTO_META)을 메모리에 넣음 → 저장.
// 1)과 2) 사이에 잠기면 'LOCKED'로 끝나고 항목은 대기열에 남는다(다음 잠금 해제 때 같은 ID로 이어서). 기록 없이 남은 사진은 같은 ID로 덮어써진다.
const LOW_SPACE_MB = 50;  // 남은 공간이 이보다 적으면 알림
let lowSpaceWarned = 0;
async function storageLeft() {
  try { if (!navigator.storage || !navigator.storage.estimate) return null; const e = await navigator.storage.estimate(); return e.quota ? e.quota - (e.usage || 0) : null; } catch { return null; }
}
async function savePhoto(stop, phase, file, lineId = null, meta = {}) {
  if (!meta.bytes) {
    const bmp = await createImageBitmap(file);
    const scale = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas'); c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    const blob = await new Promise((res) => c.toBlob(res, 'image/jpeg', 0.8));
    meta.bytes = new Uint8Array(await blob.arrayBuffer());
    meta.sha = P.toHex(await P.sha256(meta.bytes));
    meta.photoId = meta.photoId || crypto.randomUUID();
    meta.file = null;  // 원본 파일(위치 정보가 들어 있을 수 있음)은 더 붙잡지 않는다
  }
  const bytes = meta.bytes, photo_id = meta.photoId;
  // 저장 공간: 사진 + 여유(기록 저장분)보다 적게 남았으면 저장하지 않고 대기열에 둔다
  const left = await storageLeft();
  if (left != null && left < bytes.length * 2 + 2 * 1048576) throw Object.assign(new Error(ERR.STORAGE_LOW), { code: 'STORAGE_LOW' });
  if (left != null && left < LOW_SPACE_MB * 1048576 && Date.now() - lowSpaceWarned > 10 * 60000) {
    lowSpaceWarned = Date.now();
    toast(`저장 공간이 약 ${Math.max(0, Math.round(left / 1048576))}MB 남았습니다. 결과·사진을 보내고 정리하세요.`, 'bad');
  }
  if (!S.isUnlocked() || !st || locking) throw Object.assign(new Error('잠겨 있습니다'), { code: 'LOCKED' });
  await S.putSecureBytes('ph:' + photo_id, bytes);
  if (!S.isUnlocked() || !st || locking) throw Object.assign(new Error('잠겨 있습니다'), { code: 'LOCKED' });
  if (st.events.some((e) => e.kind === 'PHOTO_META' && e.body && e.body.photo_id === photo_id)) return;  // 이미 기록됨(다시 시도)
  // 찍은 시각(파일 선택 시각)과 파일 시각, 허용된 경우 위치·정확도. 사진 바이트에는 위치 정보(EXIF)를 남기지 않는다(캔버스로 다시 그림).
  const geo = st.geoEvidence !== false && meta.geo ? { lat: meta.geo.lat, lon: meta.geo.lon, acc: meta.geo.acc } : {};
  // 기록 저장이 실패해도(공간 부족) 기록은 메모리에 있고 화면 위 저장 실패 띠가 다음 저장까지 남는다 — 사진은 이미 저장됨
  await addEvent(stop, 'PHOTO_META', { photo_id, phase, sha256: meta.sha, bytes: bytes.length, line_id: lineId || null,
    taken_at: meta.at || new Date().toISOString(), file_ts: meta.fileTs || null, ...geo });
}

// ------------------------------------------------------------ 품목 고르기 (현장 추가·접수 등록 공용)
const digits = (s) => (s || '').replace(/\D/g, '');
const normAddr = (s) => (s || '').replace(/\s+/g, '');
function lineFromRow(r, qty, table) {
  return { line_id: crypto.randomUUID(), fee_rate_id: r.id, fee_version_id: table.version_id, name: r.name, spec: r.spec, unit: r.unit, unit_label: r.unit_label,
    qty, unit_price: r.price, amount: r.status === 'CONFIRMED' ? r.price * qty : null, price_status: r.status === 'CONFIRMED' ? 'CONFIRMED' : 'PENDING' };
}
let registeredNow = true;  // 화면 그릴 때 갱신(접수 화면의 미등록 안내용)
// 보내는 것은 검색칸에 적은 낱말과 승인키뿐. 이름·연락처가 검색어에 들어 있으면 보내지 않는다(forbid).
const addrRoute = () => st.addrRoute || (isSolo() ? 'direct' : 'pc');
function nextSeq(date, exceptId) {
  return st.requests.filter((r) => r.planned_date === date && r.request_id !== exceptId).reduce((m, r) => Math.max(m, r.seq || 0), 0) + 1;
}

// ------------------------------------------------------------ 단독 모드 백업·복원
// 글자 크기: 뿌리 글자 크기(--fs) 하나로 모든 글자가 함께 바뀐다(style.css). 아주 크게는 배치도 넓힌다(text-big).
const TEXT_SIZES = [['s', '작게', 14], ['', '보통', 15], ['l', '크게', 17], ['xl', '아주 크게', 19]];
function applyTextSize() {
  const r = document.documentElement, k = st && TEXT_SIZES.some(([x]) => x === st.textSize) ? st.textSize : '';
  for (const [x] of TEXT_SIZES) if (x) r.classList.toggle('fs-' + x, x === k);
  r.classList.toggle('text-big', k === 'xl');
}

// ------------------------------------------------------------ 앱 설치 상태 (PWA)
// 업데이트는 잠겨 있거나, 저장 중인 기록·수량·사진이 있으면 미룬다(새로 열면 메모리에만 있는 사진·기록이 사라짐).
// 다른 창에서 업데이트해 이 창의 서비스 워커가 바뀐 경우(controllerchange)도 같은 조건이 풀릴 때까지 새로 열지 않는다.
let reloadWanted = false;
const writesPending = () => pendingPhotos.length > 0 || !!photoFlushing || stepFlush.size > 0 || saveFailed;
async function reloadWhenSafe() {
  reloadWanted = true;
  if (S.isUnlocked() && st) { await settleWrites(); }
  try { await saving; } catch {}
  if (pendingPhotos.length || photoFlushing || stepFlush.size || saveFailed) return;  // 사진 저장·다시 저장이 끝나면 다시 부름
  location.reload();
}
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('./sw.js').then((reg) => {
    const offer = (w) => {
      if (document.querySelector('.update')) return;
      const msg = h('span', {}, '새 버전이 있습니다. ');
      const bar = h('div', { class: 'update', role: 'status' }, msg, btn('업데이트', async () => {
        if (!S.isUnlocked()) return toast(pendingPhotos.length ? '잠금을 풀어 방금 찍은 사진을 저장한 뒤 업데이트하세요.' : '잠금을 푼 뒤 업데이트하세요.', 'bad');
        msg.textContent = '저장을 마치는 중… ';
        await settleWrites();
        if (writesPending()) { msg.textContent = '새 버전이 있습니다. '; return toast(saveFailed ? '저장되지 않은 기록이 있어 지금은 업데이트하지 않습니다. 결과를 먼저 보내 주세요.' : '사진·기록 저장이 끝난 뒤 다시 눌러 주세요.', 'bad'); }
        w.postMessage('SKIP_WAITING');
      }, 'primary'));
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
  navigator.serviceWorker.addEventListener('controllerchange', () => { if (hadController && !reloaded) { reloaded = true; reloadWhenSafe(); } });
}

// 저장소 열림 기다리기: 대개 바로 열린다. 다른 창(특히 이전 버전 앱 창)이 붙잡고 있으면 놓는 즉시 저절로 계속한다.
// ------------------------------------------------------------ 화면 모듈과 라우터 잇기
// 라우터(render)는 app.js 에 있다. 화면 모듈은 core 의 render() 를 부르고, app.js 가 시작할 때 진짜 render 를 등록한다.
let renderFn = () => {};
const setRender = (fn) => { renderFn = fn; };
const render = (...a) => renderFn(...a);
// 다른 모듈이 바꾸는 상태: ES 모듈은 남의 let 을 다시 대입할 수 없어 설정 함수로 바꾼다(읽기는 그대로 st·prefs 등)
const setSt = (v) => { st = v; };
const mergePrefs = (p) => { prefs = { ...prefs, ...p }; };
const setLockMs = (v) => { lockMs = v; };
const setRegisteredNow = (v) => { registeredNow = v; };
const setLiveMap = (m) => { liveMap = m; };
const clearLiveMap = () => { if (liveMap) { try { liveMap.remove(); } catch {} liveMap = null; } };
const clearPhotoUrls = () => { photoUrls.forEach((u) => URL.revokeObjectURL(u)); photoUrls = []; };

export {
  $app, BACKUP_WARN_MS, COLLECT_TTL_MS, END_OF_DAY_H, ERR, HANDOFF_MS, HIDDEN_LOCK_MS, LOCK_CHOICES,
  LOW_SPACE_MB, TEXT_SIZES, WIN_ID, addCrewEvent, addEvent, addrRoute, afterUnlock, ago,
  allStops, appLogo, applyPrefs, applyTextSize, ask, autoLockStatus, btn, camInput, camInputs,
  camera, cancelAsked, changeReqs, clearLiveMap, clearPhotoUrls, commit, dial, digits,
  dongOf, emptyState, errMsg, feeMemo, feeTable, fieldEditable, findStop, flowSteps,
  flushPhotos, flushPhotosNow, flushSteps, fmtPhone, footBar, go, h, handoff,
  handoffUntil, hiddenAt, isDark, isIOS, isQuota, isSolo, lastActive, launchApp,
  lineFromRow, liveMap, loadState, lockMs, lockNow, locking, lowSpaceWarned, mdOf,
  mergePrefs, nextSeq, normAddr, openExternal, openSheets, pcStops, pcWorkDate, pendingPhotos,
  photoFlushing, photoUrls, prefs, reduceMotion, registeredNow, reloadWanted, reloadWhenSafe, render,
  quantityInput, quantityValue,
  routeStopId, saveBanner, saveFailed, savePhoto, saving, sessionChan, setLiveMap, setLockMs,
  setAutoLockEnabled, setPrefs, setRegisteredNow, setRender, setSt, settleWrites, sheet, smsTemplates, st,
  startCamera, stepFlush, stepper, stopCamera, storageLeft, tabBar, themeToggle, toast,
  todayKST, ui, unsent, wakeLock, wakeRoute, wantWake, withOverrides, withWorker,
  writesPending,
};
