// PC 화면 공통: 화면 조각 만들기(h·btn·표·칩)·서버 부르기(api)·알림(toast)·대화 상자(modal)·세션·작업 감싸기(wrap)·라우터 연결(render).
// 화면 모듈은 이 파일과 widgets.js 만 기대고, 화면 사이 이동은 go()/render() 로 한다(라우터는 pc.js).
import { BUILTIN_FEES } from '/web/fees-data.js';
import { LABELS } from '/web/labels-data.js';

// 운영 수수료표 = 내장 원문 표 + PC에서 승인된 확정 단가
let feeMemo = null;
function FEES() {
  const ov = (boot && boot.fee && boot.fee.overrides) || {};
  const key = JSON.stringify(ov);
  if (feeMemo && feeMemo.key === key) return feeMemo.t;
  const t = { ...BUILTIN_FEES, version_id: (boot && boot.fee && boot.fee.version_id) || BUILTIN_FEES.version_id,
    rows: BUILTIN_FEES.rows.map((r) => (ov[r.id] != null ? { ...r, price: ov[r.id], status: 'CONFIRMED', overridden: true } : r)) };
  feeMemo = { key, t };
  return t;
}
const $app = document.getElementById('app');
let boot = null;       // 설정·팀·기기
// 라우터보다 먼저 탐색 세대를 올린다. 늦게 읽은 화면의 응답도 같은 탐색 기준으로 검사한다.
let routeEpoch = 0;
addEventListener('hashchange', () => { routeEpoch++; });
let liveMaps = [];
// 접수 상태·연락·미수거 사유·스티커 확인: 휴대폰·PC 파이썬과 같은 표(data/labels.json → /web/labels-data.js)
const STATUS = LABELS.status;
const CONTACT = LABELS.contact;
const PAY = { NONE: '해당 없음', AMOUNT_PENDING: '금액 확인 대기', PAID_AMOUNT_PENDING: '납부 기록 있음 · 금액 미확정', UNPAID: '미납', UNDERPAID: '부족', PAID: '납부 확인', OVERPAID_REVIEW: '과납 확인 필요',
  REFUND_NEEDED: '환불 필요', OVERREFUNDED_REVIEW: '환불 초과 확인 필요', EXEMPT: '전액 감면' };
const REASON = LABELS.reason;
const STICKER = LABELS.sticker;
const NOTICE = { AGREED: '동의', NOTIFIED: '안내함(답 없음)', REFUSED: '거부' };
const SMS_T = { SOON: '곧 도착', ABSENT: '부재 안내', REVISIT: '재방문 안내' };
const NAV_APP = { kakao: '카카오맵', tmap: 'T맵', naver: '네이버 지도', geo: '기본 지도 앱', 'apple-maps': '애플 지도' };
const NO_PHOTO = { DARK: '어두움', REFUSED: '주민 촬영 거부', CAMERA: '카메라 고장', OTHER: '기타' };
const REFUND_ST = { DRAFT: '자동 생성(신청 전)', REQUESTED: '결재 대기', APPROVED: '결재 완료(지급 전)', REJECTED: '반려', PAID: '지급 완료', VOID: '해소·취소' };
const REFUND_CAUSE = { CANCELLED: '납부 뒤 취소', OVERPAID: '과납', PARTIAL: '일부 수거 차액', MANUAL: '기타' };
const ACCESS = { VIEW: '상세 조회', LIST: '목록 조회', PRINT: '출력', EXPORT: '내보내기', PHOTO: '사진 보기', SEARCH: '주소·중복 조회' };
const RECON = { MATCH: '일치', AMOUNT_DIFF: '금액 다름', UNMATCHED: '부과번호 없음', DUPLICATE: '파일 안 중복', ALREADY: '이미 반영', AMOUNT_PENDING: '금액 미확정', CANCELLED: '취소된 접수', BAD: '형식 오류' };

// ------------------------------------------------------------ 도우미
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'style') el.style.cssText = v; // CSP(style-src 'self')는 style 속성을 막지만 CSSOM 지정은 허용
    else if (k === 'checked') el.checked = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
const btn = (label, onclick, cls = '', extra = {}) => h('button', { type: 'button', class: cls, onclick, ...extra }, label);
// 알림: 오른쪽 위. 오류는 누를 때까지 남기고, 보통 알림은 3초 뒤·화면을 옮기면 사라진다.
const toast = (m, bad) => {
  let box = document.querySelector('.toasts');
  if (!box) { box = h('div', { class: 'toasts', 'aria-live': 'polite' }); document.body.append(box); }
  const t = h('div', { class: 'toast' + (bad ? ' bad' : ''), role: bad ? 'alert' : 'status', onclick: () => t.remove(), title: bad ? '눌러서 닫기' : null }, m, bad ? h('span', { class: 'x', 'aria-hidden': 'true' }, ' ✕') : null);
  t.dataset.at = Date.now();
  box.append(t); while (box.children.length > 2) box.firstChild.remove();
  if (!bad) setTimeout(() => t.remove(), 3000);
};
addEventListener('hashchange', () => document.querySelectorAll('.toasts .toast:not(.bad)').forEach((t) => { if (Date.now() - +t.dataset.at > 1500) t.remove(); }));
const fmtPhone = (p) => (p && p.length === 11 ? `${p.slice(0, 3)}-${p.slice(3, 7)}-${p.slice(7)}` : p || '');
const go = (hash) => { if (location.hash === hash) render(true); else location.hash = hash; };  // 같은 주소면 다시 그린다
const chip = (text, cls = '') => h('span', { class: 'chip ' + cls }, text);
// 색 규칙(휴대폰과 같음): 녹색=완료, 노랑=사람이 봐야 함(확인 필요·재방문), 빨강=막힘·거부, 파랑=정보(처리 중·현장 추가), 회색=중립(취소 포함)
// 화면 공통 틀: 제목 줄(오른쪽 도구), 목록 머리줄(총 N건 · 단추), 가로 스크롤되는 격자 표
const pagehead = (title, ...tools) => h('div', { class: 'pagehead' }, h('h1', {}, title), tools.length ? h('div', { class: 'row' }, ...tools) : null);
const gridbar = (count, ...right) => h('div', { class: 'gridbar' }, count != null ? h('span', { class: 'cnt' }, '총 ', h('b', {}, String(count)), '건') : null, h('span', { class: 'grow' }), ...right);
function grid(head, rowsArr, { cls = '', empty = '자료가 없습니다.', num = [] } = {}) {
  return h('div', { class: 'tablewrap' }, h('table', { class: 'grid ' + cls }, h('thead', {}, h('tr', {}, ...head.map((t, i) => h('th', { scope: 'col', class: num.includes(i) ? 'num' : null }, t)))),
    h('tbody', {}, ...(rowsArr.length ? rowsArr.map((cells) => (cells instanceof Node ? cells : h('tr', {}, ...cells.map((c, i) => (c instanceof Node && c.tagName === 'TD' ? c : h('td', { class: num.includes(i) ? 'num' : null }, c)))))) : [h('tr', {}, h('td', { colspan: head.length, class: 'muted' }, empty))]))));
}
const searchbox = (pairs, ...btns) => h('div', { class: 'card searchbox', role: 'search', 'aria-label': '조회조건' }, h('div', { class: 'cond' }, ...pairs.flatMap(([l, v, wide]) => [h('span', { class: 'cond-l' }, l), h('div', { class: 'cond-v' + (wide ? ' ' + wide : '') }, ...[].concat(v))])),
  btns.length ? h('div', { class: 'cond-btns' }, ...btns) : null);
const statusChip = (s) => chip(STATUS[s] || s, s === 'DONE' ? 'ok' : ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(s) ? 'warn' : s === 'CANCELLED' ? 'neutral' : ['IN_PROGRESS', 'ASSIGNED'].includes(s) ? 'info' : '');

// 세션이 끝난 뒤(401) 요청하면 화면(작성 중인 양식)은 그대로 두고 로그인 대화 상자를 띄운 뒤, 다시 로그인하면 같은 요청을 한 번 더 보낸다.
async function apiRaw(path, { method = 'GET', body, raw, retry = true, signal, _readBudget } = {}) {
  const r = await fetch('/api/' + path, {
    method, credentials: 'same-origin',
    headers: { 'X-DG-CSRF': '1', ...(raw ? { 'Content-Type': 'application/octet-stream' } : body ? { 'Content-Type': 'application/json' } : {}) },
    body: raw || (body ? JSON.stringify(body) : undefined),
    ...(signal ? { signal } : {}),
  });
  if (r.status === 401) {
    const data = await r.clone().json().catch((e) => { if (signal?.aborted) throw e; return {}; });
    if (data.login) {
      if (boot && retry) {
        // 사람의 비밀번호 입력은 네트워크 조회 시간이 아니다. 재인증 뒤에는 새 예산으로 같은 조회를 잇는다.
        _readBudget?.pause();
        try { await relogin(); } finally { _readBudget?.resume(); }
        return apiRaw(path, { method, body, raw, retry: false, signal: _readBudget?.signal || signal, _readBudget });
      }
      boot = null; render(); throw new Error('로그인이 필요합니다');
    }
  }
  if (r.ok) touchSession();
  return r;
}
// 오류에는 HTTP 상태(status)와 응답(data)을 붙인다: 409(다른 곳에서 바뀜)는 화면이 따로 안내한다
async function api(path, opts = {}) {
  // 시작 화면의 GET만 명시적으로 제한한다. 저장·가져오기·파일 응답의 기존 수명은 바꾸지 않는다.
  const bounded = (opts.method || 'GET').toUpperCase() === 'GET' && Number.isFinite(opts.timeoutMs) && opts.timeoutMs > 0;
  let timer = null, forwardAbort = null, expired = false;
  const readBudget = bounded ? {
    signal: null,
    pause() {
      if (timer !== null) { clearTimeout(timer); timer = null; }
      if (forwardAbort) { opts.signal.removeEventListener('abort', forwardAbort); forwardAbort = null; }
    },
    resume() {
      this.pause(); expired = false;
      const controller = new AbortController(); this.signal = controller.signal;
      if (opts.signal) {
        forwardAbort = () => controller.abort(opts.signal.reason);
        if (opts.signal.aborted) forwardAbort();
        else opts.signal.addEventListener('abort', forwardAbort, { once: true });
      }
      timer = setTimeout(() => { expired = true; controller.abort(); }, opts.timeoutMs);
    },
  } : null;
  readBudget?.resume();
  try {
    const r = await apiRaw(path, readBudget ? { ...opts, signal: readBudget.signal, _readBudget: readBudget } : opts);
    const data = await r.json().catch((e) => { if ((readBudget?.signal || opts.signal)?.aborted) throw e; return {}; });
    if (!r.ok) { const e = new Error(data.error || '오류'); e.status = r.status; e.data = data; throw e; }
    return data;
  } catch (e) {
    if (expired) { const timeout = new Error('PC 서버 응답을 기다리는 시간이 지났습니다. 서버 상태를 확인한 뒤 다시 시도해 주세요.'); timeout.code = 'READ_TIMEOUT'; throw timeout; }
    throw e;
  } finally { readBudget?.pause(); }
}
const post = (path, body) => api(path, { method: 'POST', body });
const isStale = (e) => e && e.status === 409;
// 요청 번호와 보낸 본문을 함께 보관한다. 응답을 잃었을 때 다른 입력을 과거 요청의 성공으로 처리하지 않는다.
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, '0')).join(''));
const REQ_IDS = new Map(), pendingRequests = new Map();
const RETRY_WINDOW_MS = 24 * 60 * 60 * 1000;  // pc/dgpc/core.py의 IDEM_HOURS와 같은 보관 기간
const reqId = (key) => { if (!REQ_IDS.has(key)) REQ_IDS.set(key, uuid()); return REQ_IDS.get(key); };
const reqDone = (key) => { pendingRequests.delete(key); REQ_IDS.delete(key); };
function requestSignature(path, json) {
  const body = JSON.parse(json);
  // 재시도하는 동안 늘어난 측정 시간은 같은 업무다. 서버에는 처음 보낸 시간을 그대로 보낸다.
  if (path === 'requests') delete body.intake_secs;
  if (path === 'close') delete body.close_secs;
  return path + '\n' + JSON.stringify(body);
}
function checkPending(request) {
  if (request.owner !== (boot?.me?.username || '')) throw new Error('로그인 사용자가 바뀌었습니다. 이전 기록을 확인한 뒤 저장해 주세요.');
  if (request.uncertain && Date.now() - request.sentAt >= RETRY_WINDOW_MS)
    throw new Error('이전 요청의 확인 기간(24시간)이 지났습니다. 중복 처리를 막기 위해 다시 보내지 않았습니다. 기존 기록을 직접 확인해 주세요.');
}
function sendPending(key, request) {
  checkPending(request);
  if (request.inFlight) return request.inFlight;
  request.inFlight = (async () => {
    try {
      const result = await post(request.path, { ...JSON.parse(request.json), client_req_id: request.id });
      if (pendingRequests.get(key) === request) reqDone(key);
      return result;
    } catch (e) {
      // 첫 응답이 입력 오류 등으로 확정 거절된 때는 고쳐 쓸 수 있다. 응답 유실 이력이 있으면 확인 전까지 보존한다.
      if (!request.uncertain && e.status >= 400 && e.status < 500 && e.status !== 408) {
        if (pendingRequests.get(key) === request) reqDone(key);
      } else request.uncertain = true;
      throw e;
    } finally { request.inFlight = null; }
  })();
  return request.inFlight;
}
async function postOnce(key, path, body) {
  const json = JSON.stringify(body || {}), signature = requestSignature(path, json), owner = boot?.me?.username || '';
  let request = pendingRequests.get(key);
  if (request && request.owner !== owner) { reqDone(key); request = null; }  // 다음 사용자에게 이전 본문을 넘기지 않는다.
  if (request) checkPending(request);
  if (request && request.signature !== signature) {
    const confirmed = await modal({ title: '이전 요청의 반영 확인', ok: '이전 요청 확인', cancel: '돌아가기',
      body: [h('p', {}, '앞서 보낸 요청의 응답을 받지 못했습니다. 현재 변경값은 저장하지 않고, 이전 내용을 같은 요청 번호로 다시 보내 반영 여부부터 확인합니다.'),
        h('p', { class: 'muted small' }, '확인한 뒤 기존 기록을 살펴보고 필요한 내용을 별도로 저장해 주세요.')],
      onOk: async () => { await sendPending(key, request); return true; } });
    throw new Error(confirmed ? '이전 요청의 반영을 확인했습니다. 현재 변경값은 저장하지 않았습니다. 기존 기록을 확인한 뒤 다시 저장해 주세요.'
      : '이전 요청의 반영 여부를 먼저 확인해 주세요. 현재 변경값은 저장하지 않았습니다.');
  }
  if (!request) {
    request = { id: reqId(key), path, json, signature, owner, sentAt: Date.now(), uncertain: false, inFlight: null };
    pendingRequests.set(key, request);
  }
  return sendPending(key, request);
}

// ------------------------------------------------------------ 백그라운드 작업 (백업·파기·사진 정리·전체 무결성 검사): 오른쪽 아래 진행 띠, GET /api/jobs/{id}를 불러 끝나면 결과
function jobsPanel() {
  let box = document.querySelector('.jobs');
  if (!box) { box = h('div', { class: 'jobs', 'aria-live': 'polite', role: 'status' }); document.body.append(box); }
  return box;
}
async function runJob(path, body, label) {
  const start = await post(path, body);
  if (!start.job_id) return start;  // 이전 서버(바로 결과)
  const bar = h('progress', { max: 1, 'aria-label': `${label} 진행` });
  const txt = h('span', { class: 'jt' }, '대기 중');
  const row = h('div', { class: 'jobrow', 'data-job': start.job_id }, h('b', {}, label), txt, bar);
  jobsPanel().append(row);
  try {
    for (let i = 0; ; i++) {
      await new Promise((res) => setTimeout(res, i < 4 ? 400 : 900));
      const j = await api('jobs/' + start.job_id);
      if (j.total) { bar.max = j.total; bar.value = j.progress || 0; txt.textContent = `${(j.progress || 0).toLocaleString()} / ${j.total.toLocaleString()}`; }
      else txt.textContent = j.status === 'RUNNING' ? '진행 중…' : j.status === 'QUEUED' ? '대기 중' : '';
      if (j.status === 'DONE') { row.classList.add('done'); txt.textContent = '끝'; bar.max = 1; bar.value = 1; setTimeout(() => row.remove(), 4000); return j.result; }
      if (j.status === 'FAILED') { row.classList.add('bad'); txt.textContent = '실패'; setTimeout(() => row.remove(), 8000); throw new Error(`${label} 실패: ${j.error || ''}`); }
    }
  } catch (e) { if (!row.classList.contains('bad')) { row.classList.add('bad'); setTimeout(() => row.remove(), 8000); } throw e; }
}

// 주민 안내 문구(이름·연락처·주소 없음)를 받아 클립보드로. 클립보드를 못 쓰면(LAN의 http 등) 고를 수 있는 글 상자로 보여 준다.
async function copyNotice(id) {
  const r = await apiRaw(`requests/${id}/notice`);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || '안내 문구를 만들지 못했습니다.');
  const text = await r.text();
  try { await navigator.clipboard.writeText(text); toast('주민 안내 문구를 복사했습니다 (문자·메신저에 붙여 넣기)'); return text; } catch { /* 아래 상자 */ }
  const ta = h('textarea', { rows: 9, class: 'fill', readonly: true, 'aria-label': '주민 안내 문구' }, text);
  setTimeout(() => ta.select(), 40);
  await modal({ title: '주민 안내 문구', ok: '닫기', cancel: null, body: [h('p', { class: 'muted small' }, '복사가 막혀 있어 글을 보여 줍니다. Ctrl+C로 복사하세요.'), ta] });
  return text;
}
// 파일 내려받기 (CSV 등): 서버가 준 파일 이름 대신 화면에서 정한 이름으로 저장
async function download(path, filename, opts = {}) {
  const r = await apiRaw(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || '내보내기 실패');
  const url = URL.createObjectURL(await r.blob());
  const a = h('a', { href: url, download: filename });
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// ------------------------------------------------------------ 대화 상자 (window.prompt·confirm 대신: 제목 연결·초점 가둠·Esc 닫기)
let dlgSeq = 0;
const dlgStack = [];
// extra: [{ label, value, cls }] — 확인·취소 말고 다른 답(누르면 value로 닫힘). sticky: 화면을 옮겨도 닫지 않음(다시 로그인)
function modal({ title, body = [], ok = '확인', cancel = '취소', danger = false, wide = false, onOk, extra = [], sticky = false, enterSubmit = true } = {}) {
  return new Promise((resolve) => {
    const id = `dlg${++dlgSeq}`;
    const prev = document.activeElement;
    const msg = h('p', { class: 'msg', role: 'alert' });
    const okB = btn(ok, null, danger ? 'danger' : 'primary', { 'data-dlg-ok': '1' });
    const noB = cancel ? btn(cancel, () => close(null)) : null;
    const exB = extra.map((x) => btn(x.label, () => close(x.value), x.cls || ''));
    const box = h('div', { class: 'dlg' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': id + 't', tabindex: '-1' },
      h('h2', { id: id + 't' }, title), h('div', { class: 'dlg-b' }, ...body), msg, h('div', { class: 'dlg-btns' }, noB, ...exB, okB));
    const back = h('div', { class: 'dlg-back' }, box);
    const focusables = () => [...box.querySelectorAll('button, input, select, textarea, a[href], [tabindex="0"]')].filter((el) => !el.disabled && !el.hidden && el.offsetParent !== null);
    const key = (e) => {
      if (dlgStack[dlgStack.length - 1] !== back) return;
      if (e.key === 'Escape' && cancel) { e.preventDefault(); e.stopPropagation(); close(null); }
      else if (e.key === 'Tab') {
        const f = focusables(); if (!f.length) return;
        const i = f.indexOf(document.activeElement);
        if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
        else if (i < 0) { e.preventDefault(); f[0].focus(); }
      } else if (enterSubmit && e.key === 'Enter' && e.target && e.target.tagName === 'INPUT' && box.contains(e.target) && !['checkbox', 'file'].includes(e.target.type)) { e.preventDefault(); okB.click(); }
    };
    let closed = false;
    const close = (v) => { if (closed) return; closed = true; back.remove(); const i = dlgStack.indexOf(back); if (i >= 0) dlgStack.splice(i, 1); document.removeEventListener('keydown', key, true); if (prev && prev.isConnected && prev.focus) prev.focus(); resolve(v); };
    back._close = close; back._sticky = sticky;
    okB.addEventListener('click', async () => {
      msg.textContent = ''; okB.disabled = true;
      try { const v = onOk ? await onOk() : true; close(v === undefined ? true : v); } catch (e) { msg.textContent = e.message; } finally { okB.disabled = false; }
    });
    document.addEventListener('keydown', key, true);
    document.body.append(back); dlgStack.push(back);
    // 처음 초점: 이미 상자 안 칸에 초점이 있으면(바로 입력을 시작함) 옮기지 않는다
    setTimeout(() => { if (!box.contains(document.activeElement) || document.activeElement === box) (box.querySelector('input:not([type=hidden]):not([readonly]), select, textarea') || okB).focus(); }, 20);
  });
}
const confirmDlg = (title, text, opts = {}) => modal({ title, body: [h('p', {}, text), ...(opts.more || [])], ok: opts.ok || '확인', danger: !!opts.danger, cancel: opts.cancel || '취소' });
// 화면을 옮기면(주소 바뀜) 열린 대화 상자를 닫는다(취소로) — 옛 화면의 대화 상자가 새 화면 위에 남지 않게. 다시 로그인 상자는 남긴다.
addEventListener('hashchange', () => { [...dlgStack].reverse().forEach((b) => { if (!b._sticky && b._close) b._close(null); }); });
function promptDlg(title, label, opts = {}) {
  const inp = h('input', { placeholder: opts.placeholder || '', class: 'fill' });
  return modal({ title, ok: opts.ok || '확인', danger: !!opts.danger, body: [...(opts.lead || []), h('label', { class: 'f' }, label, inp)],
    onOk: () => { if (!inp.value.trim()) throw new Error(`${label}을(를) 적어 주세요.`); return inp.value.trim(); } });
}
// 사유 코드 고르기 + 직접 입력 (기타는 글 필수)
function reasonFields(codes, label = '사유') {
  const sel = h('select', {}, h('option', { value: '' }, `(${label} 고르기)`), ...Object.entries(codes).map(([k, t]) => h('option', { value: k }, k === 'OTHER' ? '기타(직접 입력)' : t)));
  const txt = h('input', { placeholder: '직접 입력 (기타는 필수, 그 밖에는 덧붙일 말)', class: 'fill' });
  return { nodes: [h('label', { class: 'f' }, label, sel), h('label', { class: 'f' }, '내용', txt)],
    get: () => { if (!sel.value) throw new Error(`${label}를 고르세요.`); if (sel.value === 'OTHER' && !txt.value.trim()) throw new Error('기타 사유는 내용을 적어 주세요.'); return { code: sel.value, text: txt.value.trim() }; },
    sel, txt };
}
function reasonDlg(title, codes, opts = {}) {
  const f = reasonFields(codes, opts.label || '사유');
  if (opts.code) f.sel.value = opts.code;
  return modal({ title, ok: opts.ok || '확인', danger: !!opts.danger, wide: !!opts.wide, body: [...(opts.lead || []), ...(opts.before || []), ...f.nodes, ...(opts.extra || [])],
    onOk: async () => { const v = { ...f.get(), ...(opts.collect ? await opts.collect() : {}) }; return opts.submit ? opts.submit(v) : v; } });
}

// 다시 로그인 (세션 만료 뒤): 같은 계정으로만, 화면은 그대로
let reauth = null;
function relogin() {
  if (reauth) return reauth;
  const me = boot.me;
  const pw = h('input', { type: 'password', autocomplete: 'current-password', 'aria-label': '비밀번호' });
  reauth = modal({ title: '다시 로그인', ok: '로그인하고 계속', cancel: '로그인 화면으로', sticky: true,
    body: [h('p', {}, '오래 조작하지 않아 로그아웃되었습니다. 작성 중인 내용은 그대로 있습니다. 다시 로그인하면 하던 작업을 이어서 보냅니다.'),
      h('label', { class: 'f' }, '아이디', h('input', { value: me.username, readonly: true })), h('label', { class: 'f' }, '비밀번호', pw)],
    onOk: async () => {
      const r = await fetch('/api/login', { method: 'POST', credentials: 'same-origin', headers: { 'X-DG-CSRF': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ username: me.username, password: pw.value }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || '로그인하지 못했습니다.');
      return true;
    } }).then((v) => {
    reauth = null;
    if (!v) { boot = null; render(); throw new Error('로그인이 필요합니다'); }
    touchSession(); toast('다시 로그인했습니다');
  });
  return reauth;
}
// 세션 만료 2분 전 알림 + [연장]
let sessTimer = null;
function touchSession() {
  if (!boot) return;
  clearTimeout(sessTimer);
  document.querySelectorAll('.sess-warn').forEach((x) => x.remove());
  const idle = (boot.session_idle_s || 1800) * 1000;
  sessTimer = setTimeout(() => {
    if (!boot) return;
    const bar = h('div', { class: 'sess-warn', role: 'alert' }, h('span', {}, '2분 뒤 자동으로 로그아웃됩니다. 작성 중인 내용은 화면에 남습니다.'),
      btn('연장', async () => { try { await api('me'); toast('로그인 시간을 연장했습니다'); } catch (e) { toast(e.message, true); } }, 'primary small'));
    document.body.append(bar);
  }, Math.max(5000, idle - 120000));
}
// 작업이 끝나 다시 그릴 때, 그 사이 다른 화면으로 옮겼으면 새 화면(입력 중인 양식)을 덮지 않는다.
// 진행 중인 작업을 시작한 화면(hash)별로 센다: 작업이 겹쳐도(화면 안 여러 카드가 동시에 불러오기) 기준이 흐트러지지 않게.
// 두 번 누름 막기: 누른 단추(e.currentTarget)를 작업이 끝날 때까지 끈다. Ctrl+Enter도 같은 단추를 누르므로 같은 보호를 받는다.
const pendingActions = new Map();
const wrap = (fn, msgEl) => async (...a) => {
  const ev = a[0];
  const b = ev && typeof ev === 'object' && 'currentTarget' in ev && ev.currentTarget instanceof HTMLButtonElement ? ev.currentTarget : null;
  if (b && (b.disabled || b.dataset.busy)) return;
  if (b) { b.dataset.busy = '1'; b.disabled = true; b.setAttribute('aria-busy', 'true'); }
  const at = location.hash;
  pendingActions.set(at, (pendingActions.get(at) || 0) + 1);
  try { if (msgEl) { msgEl.textContent = ''; msgEl.className = 'msg'; } await fn(...a); }
  catch (e) { if (msgEl) { msgEl.textContent = e.message; msgEl.className = 'err'; } else toast(e.message, true); }
  finally {
    const n = pendingActions.get(at) - 1; if (n > 0) pendingActions.set(at, n); else pendingActions.delete(at);
    if (b && b.dataset.busy) { delete b.dataset.busy; b.disabled = false; b.removeAttribute('aria-busy'); }
  }
};

// ------------------------------------------------------------ 로그인
const ACTION = { CREATE: '등록', UPDATE: '수정', CANCEL: '취소', LOCATION: '위치 지정', ASSIGN: '배정', LOGIN: '로그인', LOGIN_FAIL: '로그인 실패', PASSWORD: '비밀번호 변경',
  ACTIVE: '사용', DEACTIVATE: '중지', IMPORT: '가져오기', RESOLVE: '충돌 처리', VOID: '정정', CLOSE: '마감', RECEIPT: '수신 확인', BACKUP: '백업', RESTORE: '복원', PURGE: '사진 정리',
  CONFIRM: '배정 확정', PRINT: '배차표 인쇄', DISPLAY: 'QR 표시', SAVE: '저장', REGISTER: '휴대폰 등록', STATUS: '상태 변경', RESOLVE_APPLY: '충돌 반영', RESOLVE_REJECT: '충돌 거부',
  APPROVE: '승인', REJECT: '반려', DRAFT: '개정안 작성', RESULT: '결과 반영', SOLO: '단독 자료 옮김', EXPORT: '내보내기', LINK_SIMILAR: '유사 품목 적용', UNLINK_SIMILAR: '유사 품목 해제',
  REVISIT_APPT: '재방문 약속', ADD: '추가', CREW: '팀 연결', OVERRIDE: '보정값', PAY: '납부', REFUND: '환불', REFUND_PAY: '지급',
  CARRY: '마감 이월', RESCHEDULE: '예정일 변경', SET_DONG: '동 지정', DELETE: '삭제', PRINT_RECEIPT: '접수증 출력', PRINT_LABEL: '배출 표지 출력', KEYGEN: '백업 키 생성',
  AUTO_BACKUP: '자동 백업', PURGE_PII: '개인정보 파기', CHECK: '접속기록 점검', VERIFY: '무결성 검사', RECON: '대사 반영', REQUEST: '환불 신청', REDUCTION: '감면 적용', REDUCTION_CLEAR: '감면 해제' };
// 변경 이력 대상 이름 (서버 service.AUDIT_ENTITIES 와 같음; 화면은 bootstrap 값을 우선)
const ENTITY = { request: '접수', payment: '수납', batch: '배정', bundle: '목록 전송', import: '결과 가져오기', event: '충돌 처리', photo: '사진', photo_import: '사진 가져오기',
  day: '마감', device: '휴대폰', crew: '수거팀', user: '사용자', backup: '백업', fee: '수수료표', address: '주소 자료', report: '보고서·내보내기', unload: '하차', volume: '적재 보정',
  dong: '행정동', holiday: '휴무일', settings: '설정', contact: '민원·통화', request_import: '접수 일괄 등록', privacy: '개인정보 파기', access: '접속기록', refund: '환불', revenue: '세입·대사' };
// 진단 (운영 관리자): DB·사진 크기, 남은 공간, 표별 건수, 예약 작업 마지막 시각(오래되면 빨강)
const mb = (b) => (b == null ? '—' : b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(2)}GB` : `${(b / 1024 / 1024).toFixed(1)}MB`);
const kst = (iso) => { if (!iso) return ''; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); };
// ------------------------------------------------------------ 오늘 현황 (대시보드)
// 지금 시각(한국, HH:MM)과 근무 끝(사용 중 팀의 가장 늦은 끝 시각): 결과 미회수는 근무가 끝난 뒤(또는 지난 날짜)에만 경고한다
const nowHM = () => new Date().toLocaleTimeString('en-GB', { timeZone: 'Asia/Seoul', hour12: false }).slice(0, 5);
const crewEnd = (cid) => ((boot.crews.find((c) => c.id === cid) || {}).shift_end || '17:00');
const workEnd = () => boot.crews.filter((c) => c.active).map((c) => c.shift_end || '17:00').sort().pop() || '17:00';
const afterWork = (date, cid) => date < boot.today || (date === boot.today && nowHM() >= (cid ? crewEnd(cid) : workEnd()));
const md = (d) => (d ? `${+d.slice(5, 7)}/${+d.slice(8, 10)}` : '');
const canWork = () => ['ADMIN', 'INTAKE'].includes(boot.me.role);  // 접수·수정·일괄 처리
const dongOpts = (cur, { all = false, none = '(동 미지정)' } = {}) => [h('option', { value: '' }, all ? '전체' : none),
  ...(all ? [h('option', { value: '-', selected: cur === '-' }, '(동 미지정)')] : []),
  ...boot.dongs.filter((d) => d.active || d.name === cur).map((d) => h('option', { value: d.name, selected: d.name === cur }, d.name + (d.active ? '' : ' (사용 안 함)')))];
const IDLE_RESET_MS = 15 * 60 * 1000;  // 15분 넘게 그 화면을 떠나 있었으면 다시 잰다(다른 일 한 시간을 빼려고)
const isPlaceholderContact = () => { const c = (boot.settings.contact_info || '').trim(); return !c || c.includes('설정에서'); };
const THEME = { auto: '자동(시스템)', light: '밝게', dark: '어둡게' };
const FONT = { 14: '작게', 15: '보통', 16: '크게', 17: '아주 크게' };
function applyPrefs(p) {
  const root = document.documentElement;
  const t = (p && p.theme) || 'auto';
  if (t === 'auto') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', t);
  root.style.setProperty('--fs', `${(p && p.font) || 15}px`);
}
async function savePrefs(patch) {
  const p = await post('prefs', patch);
  boot.me.prefs = p; applyPrefs(p);
  return p;
}
function prefsControls() {
  const p = boot.me.prefs || { theme: 'auto', font: 14 };
  const th = h('select', { class: 'tb-sel', 'aria-label': '화면 테마', title: '화면 테마' }, ...Object.entries(THEME).map(([k, t]) => h('option', { value: k, selected: k === p.theme }, t)));
  const fs = h('select', { class: 'tb-sel', 'aria-label': '글자 크기', title: '글자 크기' }, ...Object.entries(FONT).map(([k, t]) => h('option', { value: k, selected: +k === +p.font }, `글자 ${t}`)));
  th.addEventListener('change', wrap(async () => { await savePrefs({ theme: th.value }); toast(`테마: ${THEME[th.value]}`); }));
  fs.addEventListener('change', wrap(async () => { await savePrefs({ font: +fs.value }); toast(`글자 크기: ${FONT[fs.value]}`); }));
  return h('span', { class: 'tb-prefs' }, th, fs);
}

// ------------------------------------------------------------ 화면 모듈과 라우터 잇기
// 라우터(render)는 pc.js 에 있다. 화면 모듈은 core 의 render()/go() 만 부르고, pc.js 가 시작할 때 진짜 render 를 등록한다.
let renderFn = () => {};
const setRender = (fn) => { renderFn = fn; };
const render = (...a) => renderFn(...a);
// boot(설정·팀·기기·나)는 여기 한 곳에 둔다. 다른 모듈은 읽기만 하고, 바꿀 때는 setBoot().
const setBoot = (b) => { boot = b; };
// 열린 지도(MapLibre)를 모두 닫는다(화면을 바꿀 때)
const clearMaps = () => { liveMaps.forEach((m) => { try { m.remove(); } catch {} }); liveMaps = []; };
const stopSessionTimer = () => clearTimeout(sessTimer);

export {
  $app, ACCESS, ACTION, CONTACT, ENTITY, FEES, FONT, IDLE_RESET_MS,
  NAV_APP, NOTICE, NO_PHOTO, PAY, REASON, RECON, REFUND_CAUSE, REFUND_ST,
  REQ_IDS, SMS_T, STATUS, STICKER, THEME, afterWork, api, apiRaw,
  applyPrefs, boot, btn, canWork, chip, clearMaps, confirmDlg, copyNotice,
  crewEnd, dlgSeq, dlgStack, dongOpts, download, feeMemo, fmtPhone, go,
  grid, gridbar, h, isPlaceholderContact, isStale, jobsPanel, kst, liveMaps,
  mb, md, modal, nowHM, pagehead, pendingActions, post, postOnce,
  prefsControls, promptDlg, reasonDlg, reasonFields, reauth, relogin, render, reqDone,
  reqId, routeEpoch, runJob, savePrefs, searchbox, sessTimer, setBoot, setRender, statusChip,
  stopSessionTimer, toast, touchSession, uuid, workEnd, wrap,
};
