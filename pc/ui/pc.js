// 동구 대형폐기물 PC 업무 화면 (이 PC의 127.0.0.1 서버만 사용)
import { searchFees, won } from '/web/fees.js';
import { BUILTIN_FEES } from '/web/fees-data.js';
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
import * as M from '/web/map.js';
import { Collector } from '/web/proto.js';
import * as NAV from '/web/nav.js';

const $app = document.getElementById('app');
let boot = null;       // 설정·팀·기기
let liveMaps = [];
const STATUS = { RECEIVED: '접수', ASSIGNED: '배정', IN_PROGRESS: '처리 중', PARTIAL: '일부 수거', DONE: '수거 완료', NOT_COLLECTED: '미수거', ON_HOLD: '보류', CANCELLED: '취소' };
const CONTACT = { NOT_CONTACTED: '연락 전', REACHED: '통화 완료', NO_ANSWER: '부재', CALLBACK: '재연락 예정', WRONG_NUMBER: '번호 오류' };
const PAY = { NONE: '해당 없음', AMOUNT_PENDING: '금액 확인 대기', PAID_AMOUNT_PENDING: '납부 기록 있음 · 금액 미확정', UNPAID: '미납', UNDERPAID: '부족', PAID: '납부 확인', OVERPAID_REVIEW: '과납 확인 필요',
  REFUND_NEEDED: '환불 필요', OVERREFUNDED_REVIEW: '환불 초과 확인 필요', EXEMPT: '전액 감면' };
const REASON = { NOT_PUT_OUT: '배출 안 됨', DIFFERENT: '접수와 다름', CANNOT_LOAD: '적재 불가', POSTPONED: '주민 요청 연기', NO_STICKER: '스티커·필증 없음·불일치', FEE_REFUSED: '추가 금액 거부', OTHER: '기타' };
const STICKER = { OK: '확인', MISSING: '없음', MISMATCH: '번호 불일치' };
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
async function apiRaw(path, { method = 'GET', body, raw, retry = true } = {}) {
  const r = await fetch('/api/' + path, {
    method, credentials: 'same-origin',
    headers: { 'X-DG-CSRF': '1', ...(raw ? { 'Content-Type': 'application/octet-stream' } : body ? { 'Content-Type': 'application/json' } : {}) },
    body: raw || (body ? JSON.stringify(body) : undefined),
  });
  if (r.status === 401) {
    const data = await r.clone().json().catch(() => ({}));
    if (data.login) {
      if (boot && retry) { await relogin(); return apiRaw(path, { method, body, raw, retry: false }); }
      boot = null; render(); throw new Error('로그인이 필요합니다');
    }
  }
  if (r.ok) touchSession();
  return r;
}
async function api(path, opts = {}) {
  const r = await apiRaw(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || '오류');
  return data;
}
const post = (path, body) => api(path, { method: 'POST', body });
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
function modal({ title, body = [], ok = '확인', cancel = '취소', danger = false, wide = false, onOk } = {}) {
  return new Promise((resolve) => {
    const id = `dlg${++dlgSeq}`;
    const prev = document.activeElement;
    const msg = h('p', { class: 'msg', role: 'alert' });
    const okB = btn(ok, null, danger ? 'danger' : 'primary', { 'data-dlg-ok': '1' });
    const noB = cancel ? btn(cancel, () => close(null)) : null;
    const box = h('div', { class: 'dlg' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': id + 't', tabindex: '-1' },
      h('h2', { id: id + 't' }, title), h('div', { class: 'dlg-b' }, ...body), msg, h('div', { class: 'dlg-btns' }, noB, okB));
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
      } else if (e.key === 'Enter' && e.target && e.target.tagName === 'INPUT' && box.contains(e.target) && !['checkbox', 'file'].includes(e.target.type)) { e.preventDefault(); okB.click(); }
    };
    const close = (v) => { back.remove(); dlgStack.splice(dlgStack.indexOf(back), 1); document.removeEventListener('keydown', key, true); if (prev && prev.isConnected && prev.focus) prev.focus(); resolve(v); };
    okB.addEventListener('click', async () => {
      msg.textContent = ''; okB.disabled = true;
      try { const v = onOk ? await onOk() : true; close(v === undefined ? true : v); } catch (e) { msg.textContent = e.message; } finally { okB.disabled = false; }
    });
    document.addEventListener('keydown', key, true);
    document.body.append(back); dlgStack.push(back);
    setTimeout(() => (box.querySelector('input:not([type=hidden]):not([readonly]), select, textarea') || okB).focus(), 20);
  });
}
const confirmDlg = (title, text, opts = {}) => modal({ title, body: [h('p', {}, text)], ok: opts.ok || '확인', danger: !!opts.danger });
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
  reauth = modal({ title: '다시 로그인', ok: '로그인하고 계속', cancel: '로그인 화면으로',
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
const pendingActions = new Map();
const wrap = (fn, msgEl) => async (...a) => {
  const at = location.hash;
  pendingActions.set(at, (pendingActions.get(at) || 0) + 1);
  try { if (msgEl) { msgEl.textContent = ''; msgEl.className = 'msg'; } await fn(...a); }
  catch (e) { if (msgEl) { msgEl.textContent = e.message; msgEl.className = 'err'; } else toast(e.message, true); }
  finally { const n = pendingActions.get(at) - 1; if (n > 0) pendingActions.set(at, n); else pendingActions.delete(at); }
};

// ------------------------------------------------------------ 로그인
async function screenLogin() {
  const st = await api('state');
  const user = h('input', { placeholder: '아이디', autocomplete: 'username', value: st.setup_needed ? 'admin' : '' });
  const dname = st.setup_needed ? h('input', { placeholder: '이름 (예: 홍길동 주무관)' }) : null;
  const pw = h('input', { type: 'password', placeholder: '비밀번호', autocomplete: st.setup_needed ? 'new-password' : 'current-password' });
  const pw2 = st.setup_needed ? h('input', { type: 'password', placeholder: '비밀번호 한 번 더', autocomplete: 'new-password' }) : null;
  const msg = h('p', { class: 'msg' });
  const go1 = wrap(async () => {
    if (st.setup_needed) {
      if (pw.value !== pw2.value) throw new Error('두 번 입력한 비밀번호가 다릅니다.');
      await post('setup', { username: user.value, display_name: dname.value, password: pw.value });
    } else await post('login', { username: user.value, password: pw.value });
    boot = null; render();
  }, msg);
  (pw2 || pw).addEventListener('keydown', (e) => { if (e.key === 'Enter') go1(); });
  user.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); (dname || pw).focus(); } });
  pw.addEventListener('keydown', (e) => { if (e.key === 'Enter' && pw2) { e.preventDefault(); pw2.focus(); } });
  setTimeout(() => (st.setup_needed ? dname : user).focus(), 30);
  return h('main', { class: 'login-wrap' }, h('div', { class: 'login card' }, h('h1', { class: 'sr-only' }, st.setup_needed ? '관리자 계정 만들기' : '로그인'),
    h('div', { class: 'brand' }, h('span', { class: 'logo' }, '동'), h('div', {}, h('strong', {}, '동구 대형폐기물 관리시스템'), h('small', {}, '접수·배차·수거 업무 (PC)'))),
    h('p', { class: 'muted' }, st.setup_needed ? '처음 사용합니다. 운영 관리자 계정을 만드세요. 비밀번호는 10자 이상입니다.' : '아이디와 비밀번호를 입력하세요.'),
    h('label', { class: 'f' }, '아이디', user), dname && h('label', { class: 'f' }, '이름', dname), h('label', { class: 'f' }, '비밀번호', pw), pw2 && h('label', { class: 'f' }, '비밀번호 확인', pw2),
    btn(st.setup_needed ? '관리자 계정 만들기' : '로그인', go1, 'primary block'), msg,
    h('p', { class: 'muted small' }, '이 화면은 이 PC 안에서만 열립니다. 자료는 암호화된 DB에 저장되고, 모든 변경은 작업자와 함께 기록됩니다.')));
}

// ------------------------------------------------------------ 내 계정 (비밀번호 변경)
function screenMe() {
  const me = boot.me;
  const old = h('input', { type: 'password', autocomplete: 'current-password' }), pw = h('input', { type: 'password', autocomplete: 'new-password' }), pw2 = h('input', { type: 'password', autocomplete: 'new-password' });
  const msg = h('p', { class: 'msg' });
  return h('div', {}, h('h1', {}, '내 계정'),
    me.must_change ? h('div', { class: 'card notice warn' }, '관리자가 정한 임시 비밀번호입니다. 업무를 시작하기 전에 새 비밀번호로 바꿔 주세요.') : null,
    h('div', { class: 'card', style: 'max-width:460px' }, h('dl', { class: 'kv' }, h('dt', {}, '아이디'), h('dd', {}, me.username), h('dt', {}, '이름'), h('dd', {}, me.display_name), h('dt', {}, '역할'), h('dd', {}, me.role_label)),
      h('label', { class: 'f' }, '현재 비밀번호', old), h('label', { class: 'f' }, '새 비밀번호 (10자 이상)', pw), h('label', { class: 'f' }, '새 비밀번호 확인', pw2),
      btn('비밀번호 바꾸기', wrap(async () => {
        if (pw.value !== pw2.value) throw new Error('두 번 입력한 비밀번호가 다릅니다.');
        await post('password', { old: old.value, password: pw.value }); toast('바꿨습니다'); boot = null; go('#/requests'); render();
      }, msg), 'primary'), msg),
    h('div', { class: 'card', style: 'max-width:460px' }, h('h2', {}, '화면 설정'), h('p', { class: 'muted small' }, '테마와 글자 크기는 계정마다 저장되어 다른 PC에서 로그인해도 같게 보입니다. 지도와 인쇄물은 항상 밝게 표시합니다.'), prefsControls()));
}

// ------------------------------------------------------------ 사용자·변경 이력 (운영 관리자)
const ROLE = { ADMIN: '운영 관리자', INTAKE: '접수 담당자', FIELD: '수거팀', LEAD: '팀장(검토자)' };
const ACTION = { CREATE: '등록', UPDATE: '수정', CANCEL: '취소', LOCATION: '위치 지정', ASSIGN: '배정', LOGIN: '로그인', LOGIN_FAIL: '로그인 실패', PASSWORD: '비밀번호 변경',
  ACTIVE: '사용', DEACTIVATE: '중지', IMPORT: '가져오기', RESOLVE: '충돌 처리', VOID: '정정', CLOSE: '마감', RECEIPT: '수신 확인', BACKUP: '백업', RESTORE: '복원', PURGE: '사진 정리',
  CONFIRM: '배정 확정', PRINT: '배차표 인쇄', DISPLAY: 'QR 표시', SAVE: '저장', REGISTER: '휴대폰 등록', STATUS: '상태 변경', RESOLVE_APPLY: '충돌 반영', RESOLVE_REJECT: '충돌 거부',
  APPROVE: '승인', REJECT: '반려', DRAFT: '개정안 작성', RESULT: '결과 반영', SOLO: '단독 자료 옮김', EXPORT: '내보내기', LINK_SIMILAR: '유사 품목 적용', UNLINK_SIMILAR: '유사 품목 해제',
  REVISIT_APPT: '재방문 약속', ADD: '추가', CREW: '팀 연결', OVERRIDE: '보정값', PAY: '납부', REFUND: '환불',
  CARRY: '마감 이월', RESCHEDULE: '예정일 변경', SET_DONG: '동 지정', DELETE: '삭제', PRINT_RECEIPT: '접수증 출력', PRINT_LABEL: '배출 표지 출력', KEYGEN: '백업 키 생성',
  AUTO_BACKUP: '자동 백업', PURGE_PII: '개인정보 파기', CHECK: '접속기록 점검', VERIFY: '무결성 검사', RECON: '대사 반영', REQUEST: '환불 신청', REDUCTION: '감면 적용', REDUCTION_CLEAR: '감면 해제' };
// 변경 이력 대상 이름 (서버 service.AUDIT_ENTITIES 와 같음; 화면은 bootstrap 값을 우선)
const ENTITY = { request: '접수', payment: '수납', batch: '배정', bundle: '목록 전송', import: '결과 가져오기', event: '충돌 처리', photo: '사진', photo_import: '사진 가져오기',
  day: '마감', device: '휴대폰', crew: '수거팀', user: '사용자', backup: '백업', fee: '수수료표', address: '주소 자료', report: '보고서·내보내기', unload: '하차', volume: '적재 보정',
  dong: '행정동', holiday: '휴무일', settings: '설정', contact: '민원·통화', request_import: '접수 일괄 등록', privacy: '개인정보 파기', access: '접속기록', refund: '환불', revenue: '세입·대사' };
const KEY = { crew: '팀', date: '날짜', ver: '갱신본', n: '건수', count: '건수', reason: '사유', role: '역할', username: '아이디', status: '상태', source: '출처', adjusted: '조정',
  rev_no: '개정', effective_from: '시행일', rows: '행', price: '단가', fee_rate_id: '품목', confirmed_by: '확인자', file: '파일', bytes: '크기', start: '시작', end: '끝', amount: '금액',
  kind: '구분', method: '방법', applied: '반영', conflict: '충돌', skipped_duplicate: '중복', stops: '지점', open_items: '미처리', retention_days: '보관 일수', line: '품목 행',
  from: '이전', to: '이후', code: '사유 코드', seq: '회차', carried: '이월', after_close: '마감 후 수정', dong: '동', channel: '경로', pii: '개인정보 포함', refund_needed: '환불 필요', request: '접수', rate: '감면율', proof: '증빙 확인', checker: '확인자', ref: '영수번호', ok: '정상', trigger: '실행',
  cutoff: '기준일', years: '보유기간(년)', matched: '일치', cause: '사유', fpr: '키 지문', basis: '기준', unit: '단위', deleted: '삭제', dest2_ok: '두 번째 위치', error: '오류' };
async function screenUsers() {
  const msg = h('p', { class: 'msg' });
  const users = await api('users');
  const F = { username: h('input', { placeholder: '아이디 (영문)' }), display_name: h('input', { placeholder: '이름' }),
    role: h('select', {}, ...Object.entries(ROLE).map(([k, t]) => h('option', { value: k, selected: k === 'INTAKE' }, t))),
    crew: h('select', { hidden: true }, h('option', { value: '' }, '(팀 선택)'), ...boot.crews.map((c) => h('option', { value: c.id }, c.name))), password: h('input', { type: 'password', placeholder: '임시 비밀번호 (10자 이상)', autocomplete: 'new-password' }) };
  F.role.addEventListener('change', () => { F.crew.hidden = F.role.value !== 'FIELD'; });
  const crewSel = (u) => {
    const sel = h('select', { class: 'small', 'aria-label': `${u.username} 수거팀` }, h('option', { value: '' }, '(팀 없음 — 아무것도 안 보임)'), ...boot.crews.map((c) => h('option', { value: c.id, selected: c.id === u.crew_id }, c.name)));
    sel.addEventListener('change', wrap(async () => { await post(`users/${u.id}/crew`, { crew_id: sel.value }); toast('팀을 연결했습니다'); }));
    return sel;
  };
  const utable = h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['아이디', '이름', '역할', '팀', '상태', '마지막 로그인', ''].map((t) => h('th', {}, t)))),
    h('tbody', {}, ...users.map((u) => h('tr', {}, h('td', {}, u.username), h('td', {}, u.display_name), h('td', {}, ROLE[u.role]),
      h('td', {}, u.role === 'FIELD' ? crewSel(u) : h('span', { class: 'muted' }, '전체')),
      h('td', {}, u.active ? (u.must_change ? chip('임시 비밀번호', 'warn') : chip('사용', 'ok')) : chip('중지', 'bad')),
      h('td', {}, u.last_login_at ? kst(u.last_login_at) : '—'),
      h('td', {}, btn(u.active ? '중지' : '다시 사용', wrap(async () => { await post(`users/${u.id}/active`, { active: !u.active }); render(); }, msg), 'small'), ' ',
        (() => {
          // 임시 비밀번호는 화면에 그대로 보이지 않게 가린 입력칸으로 받는다
          const pw = h('input', { type: 'password', placeholder: '새 임시 비밀번호 (10자 이상)', autocomplete: 'new-password', 'aria-label': `${u.username} 새 임시 비밀번호`, hidden: true, class: 'w-m' });
          const ok = btn('저장', wrap(async () => { await post(`users/${u.id}/password`, { password: pw.value }); toast('재설정했습니다. 다음 로그인 때 바꾸게 됩니다.'); render(); }, msg), 'small primary', { hidden: true });
          return h('span', { class: 'row' }, btn('비밀번호 재설정', () => { pw.hidden = !pw.hidden; ok.hidden = pw.hidden; if (!pw.hidden) pw.focus(); }, 'small'), pw, ok);
        })())))));
  // 변경 이력: 기간·대상·작업자로 거르고 쪽 나눔, 거른 목록 CSV(개인정보 없음)
  const ents = boot.audit_entities || ENTITY;
  const fEntity = h('select', { 'aria-label': '대상' }, h('option', { value: '' }, '전체 대상'), ...Object.entries(ents).map(([v, t]) => h('option', { value: v }, t)));
  const fFrom = h('input', { type: 'date', value: boot.today, 'aria-label': '기간 시작' }), fTo = h('input', { type: 'date', value: boot.today, 'aria-label': '기간 끝' });
  const fActor = h('select', { 'aria-label': '작업자' }, h('option', { value: '' }, '전체 작업자'), h('option', { value: 'system' }, 'system'), ...users.map((u) => h('option', { value: u.username }, u.username)));
  const tbody = h('tbody'), info = h('span', { class: 'muted small' }), pager = h('div', { class: 'pager' });
  let page = 1;
  const qs = () => new URLSearchParams({ entity: fEntity.value, date_from: fFrom.value, date_to: fTo.value, actor: fActor.value });
  const load = wrap(async () => {
    const res = await api(`audit?${qs()}&page=${page}&size=100`);
    page = res.page;
    info.textContent = `총 ${res.total.toLocaleString()}건`;
    tbody.replaceChildren(...(res.rows.length ? res.rows.map((a) => h('tr', {}, h('td', { class: 'nowrap' }, kst(a.at)), h('td', {}, a.actor || '—'), h('td', {}, ents[a.entity] || a.entity), h('td', {}, ACTION[a.action] || a.action,
      a.detail && a.detail.includes('"after_close"') ? chip('마감 후 수정', 'warn') : null),
      h('td', { class: 'mono' }, a.entity_id ? a.entity_id.slice(0, 8) : ''), h('td', { class: 'detail' }, summarizeDetail(a.detail)))) : [h('tr', {}, h('td', { colspan: 6, class: 'muted' }, '기록이 없습니다.'))]));
    pager.replaceChildren(btn('‹ 이전', () => { page -= 1; load(); }, 'small', { disabled: page <= 1 }), h('span', { class: 'pg' }, `${res.page} / ${res.pages}쪽`), btn('다음 ›', () => { page += 1; load(); }, 'small', { disabled: page >= res.pages }));
  });
  [fEntity, fFrom, fTo, fActor].forEach((x) => x.addEventListener('change', () => { page = 1; load(); }));
  load();
  const admin = boot.me.role === 'ADMIN';
  const purge = await api('privacy').catch(() => null);
  return h('div', {}, pagehead(admin ? '사용자·변경 이력' : '변경 이력', h('a', { href: '#/access', class: 'small' }, '개인정보 접속기록 →')),
    admin ? h('div', { class: 'card' }, h('h2', {}, '사용자'), h('div', { class: 'tablewrap' }, utable),
      h('div', { class: 'row', style: 'margin-top:12px' }, F.username, F.display_name, F.role, F.crew, F.password,
        btn('사용자 추가', wrap(async () => { if (F.role.value === 'FIELD' && !F.crew.value) throw new Error('수거팀 계정은 팀을 골라 주세요.'); await post('users', { username: F.username.value, display_name: F.display_name.value, role: F.role.value, password: F.password.value, crew_id: F.crew.value }); toast('추가했습니다. 첫 로그인 때 비밀번호를 바꾸게 됩니다.'); render(); }, msg), 'primary')),
      h('p', { class: 'muted' }, '접수 담당자: 접수·배정·전송·결과·수납·마감 기록. 팀장(검토자): 모든 화면 조회·변경 이력·마감 확인(사용자·설정 관리와 업무 변경은 못 함). 수거팀: 연결한 팀에 배정된 접수만 조회, 그 팀 휴대폰의 결과·사진 가져오기, 그 팀 하차 기록. 운영 관리자: 전체와 설정·사용자·백업. 계정은 지우지 않고 중지합니다.'), msg) : null,
    h('div', { class: 'card' }, h('h2', {}, '변경 이력'), h('div', { class: 'row' }, '기간', fFrom, '~', fTo, fEntity, fActor, btn('새로 고침', load, 'small'), info, h('span', { class: 'grow' }),
      btn('CSV 내보내기', wrap(async () => { await download(`audit.csv?${qs()}`, `동구-변경이력-${fFrom.value || '처음'}_${fTo.value || '끝'}.csv`); toast('내려받았습니다 (내보낸 기록이 이력에 남습니다)'); }), 'small')),
      h('div', { class: 'tablewrap tall' }, h('table', { class: 'audit grid' }, h('thead', {}, h('tr', {}, ...['시각', '작업자', '대상', '작업', '번호', '내용'].map((t) => h('th', {}, t)))), tbody)), pager,
      h('p', { class: 'muted' }, '이력은 지우거나 고칠 수 없습니다. 새 기록은 해시 사슬로 이어져 고치거나 지우면 [접속기록 → 무결성 검사]에서 드러납니다. 내용에는 이름·연락처·주소를 넣지 않습니다(사유·건수·코드만). CSV도 같은 내용만 담습니다.')),
    purge ? h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '개인정보 파기 대장'), h('span', { class: 'muted small' }, `보유기간 ${purge.retention_years}년 · 지금 대상 ${purge.preview.count}건`)),
      purgeLogTable(purge.log), admin ? h('a', { href: '#/settings/privacy', class: 'small' }, '보유기간·파기 설정 →') : null) : null);
}
const kst = (iso) => { if (!iso) return ''; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); };
function summarizeDetail(raw) {
  if (!raw) return '';
  let d; try { d = JSON.parse(raw); } catch { return String(raw).slice(0, 120); }
  if (typeof d !== 'object' || d == null) return String(d);
  const val = (k, v) => {
    if (v === true) return '예';
    if (k === 'role') return ROLE[v] || v;
    if (k === 'fee_rate_id') { const r = BUILTIN_FEES.rows.find((x) => x.id === v); return r ? `${r.name} ${r.spec}` : v; }
    const t = typeof v === 'object' ? JSON.stringify(v) : String(v);
    return /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(t) ? t.slice(0, 8) : t.slice(0, 60);
  };
  return Object.entries(d).filter(([k]) => !['name', 'phone', 'address', 'memo', 'location_note', 'sha256'].includes(k)).slice(0, 6)
    .map(([k, v]) => `${KEY[k] || k}: ${val(k, v)}`).join(' · ');
}

// ------------------------------------------------------------ 품목 고르기
function itemPicker(onAdd) {
  const st = { q: '', sel: null, qty: 1 };
  const q = h('input', { type: 'search', placeholder: '품목명·규격 검색 (예: 3인용 소파, 장롱, 2인용 매트리스)', style: 'flex:1' });
  const list = h('div', { class: 'results' });
  const qty = h('input', { type: 'number', min: 1, value: 1, style: 'width:80px' });
  const add = btn('품목 추가', null, 'primary', { disabled: true });
  let usage = [];
  api('fee-usage').then((u) => { usage = u; draw(); }).catch(() => {});
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
// ------------------------------------------------------------ 오늘 현황 (대시보드)
async function screenHome() {
  const s = (screenHome.s ||= { date: boot.today });
  const date = h('input', { type: 'date', value: s.date, onchange: (e) => { s.date = e.target.value; render(); } });
  const role0 = boot.me.role, overseer = ['ADMIN', 'LEAD'].includes(role0);
  const [sum, batches, accSt, bkSt] = await Promise.all([api('close?date=' + s.date), api('batches?date=' + s.date),
    overseer ? api('access/status').catch(() => null) : null, overseer ? api('backup-status').catch(() => null) : null]);
  const bs = sum.by_status || {};
  const done = bs.DONE || 0, open = (bs.RECEIVED || 0) + (bs.ASSIGNED || 0) + (bs.IN_PROGRESS || 0), revisit = (bs.PARTIAL || 0) + (bs.NOT_COLLECTED || 0) + (bs.ON_HOLD || 0);
  const active = sum.total - (bs.CANCELLED || 0);
  const pct = active ? Math.round((done / active) * 100) : 0;
  const role = boot.me.role;
  const kpi = (label, value, sub, cls = '', href) => h(href ? 'a' : 'div', { class: 'kpi ' + cls, href }, h('span', { class: 'k' }, label), h('b', { class: 'v' }, value), sub ? h('span', { class: 's' }, sub) : null);
  // 해야 할 일: 담당자가 바로 눌러 처리할 수 있게
  const todo = [
    [sum.unconfirmed_bundles, `수신 미확인 전송 ${sum.unconfirmed_bundles}건`, '#/send', 'warn', 'ADMIN INTAKE'],
    [sum.no_result.length, `결과 미회수 ${sum.no_result.length}건`, '#/results', 'warn', 'ADMIN INTAKE FIELD'],
    [sum.conflicts, `충돌 검토 ${sum.conflicts}건`, '#/results', 'bad', 'ADMIN INTAKE'],
    [sum.pending_fee.length, `금액 확인 대기 ${sum.pending_fee.length}건`, '#/close', 'warn', 'ADMIN INTAKE'],
    [sum.payments.length, `수납 확인 필요 ${sum.payments.length}건`, '#/close', 'warn', 'ADMIN INTAKE'],
    [sum.photos_missing, `사진 미회수 ${sum.photos_missing}장`, '#/results', '', 'ADMIN INTAKE FIELD'],
    [revisit, `재방문 후보 ${revisit}건`, '#/close', 'warn', 'ADMIN INTAKE'],
    [(sum.sticker_issues || []).length, `스티커 없음·불일치 ${(sum.sticker_issues || []).length}건`, '#/close', 'warn', 'ADMIN INTAKE LEAD'],
    [(sum.fee_refused || []).length, `추가 금액 고지 거부 ${(sum.fee_refused || []).length}건`, '#/close', 'warn', 'ADMIN INTAKE LEAD'],
    [accSt && !accSt.done ? 1 : 0, `이번 달 접속기록 점검 (${accSt ? accSt.month : ''})`, '#/access', 'warn', 'ADMIN LEAD'],
    [bkSt && bkSt.stale ? 1 : 0, bkSt && bkSt.last_at ? `백업이 ${Math.floor(bkSt.age_hours)}시간 지났습니다` : '백업 기록이 없습니다', '#/settings/backup', 'bad', 'ADMIN LEAD'],
  ].filter(([n, , , , r]) => n && r.split(' ').includes(role));
  const act = batches.filter((b) => b.status === 'ACTIVE');
  // 배차 지도: 팀 색 핀 + 순서 선 (핀은 이 화면에서만 그림, 지도 서버로 보내지 않음)
  const mapBox = h('div', { class: 'mapbox dash-map' });
  const dep = boot.settings.depot;
  setTimeout(async () => {
    try {
      const { map, ml } = await mapIn(mapBox, { center: [dep.lon, dep.lat], zoom: 13 });
      const bnd = new ml.LngLatBounds(); bnd.extend([dep.lon, dep.lat]);
      M.pin(ml, map, { lng: dep.lon, lat: dep.lat, label: '출발', cls: 'me' });
      const lines = [];
      act.forEach((b, bi) => {
        const coords = [[dep.lon, dep.lat]];
        b.stops.forEach((x, i) => { if (x.lat == null) return; coords.push([x.lon, x.lat]); bnd.extend([x.lon, x.lat]); M.pin(ml, map, { lng: x.lon, lat: x.lat, label: `${bi + 1}-${i + 1}`, cls: x.status === 'DONE' ? 'done' : 'c' + ((bi % 4) + 1) }); });
        lines.push({ type: 'Feature', properties: { c: bi % 4 }, geometry: { type: 'LineString', coordinates: coords } });
      });
      // 핀이 모두 보이게 먼저 맞춘다 (바탕 지도가 늦거나 못 와도 핀은 보이게)
      if (lines.length) map.fitBounds(bnd, { padding: 50, maxZoom: 15, duration: 0 });
      const whenStyle = (f) => (map.isStyleLoaded() ? f() : map.once('style.load', f));
      whenStyle(() => {
        map.addSource('routes', { type: 'geojson', data: { type: 'FeatureCollection', features: lines } });
        map.addLayer({ id: 'routes', type: 'line', source: 'routes', paint: { 'line-width': 3, 'line-dasharray': [2, 1], 'line-color': ['match', ['get', 'c'], 0, '#2557a7', 1, '#a5560d', 2, '#6c3fb5', '#0e7490'] } });
      });
    } catch { mapBox.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다(인터넷 연결 확인). 팀별 진행은 오른쪽 목록에서 볼 수 있습니다.')); }
  }, 30);
  if (!act.length) mapBox.append(h('div', { class: 'map-empty' }, h('b', {}, '확정된 배정이 없습니다'), h('span', {}, role0 === 'FIELD' ? '배정이 확정되면 방문 순서가 표시됩니다.' : '[배정·노선]에서 확정하면 팀별 핀과 방문 순서가 지도에 표시됩니다.')));
  // 관리자·팀장: 접속기록 월 점검, 최근 백업
  const overseeCard = overseer ? h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '점검'), null),
    h('dl', { class: 'kv compact' },
      h('dt', {}, '이번 달 접속기록 점검'), h('dd', {}, accSt ? (accSt.done ? [chip('점검 완료', accSt.done.result && accSt.done.result.ok ? 'ok' : 'bad'), ` ${kst(accSt.done.at)} · ${accSt.done.actor}`]
        : [chip('점검 전', 'warn'), ' ', h('a', { href: '#/access' }, '접속기록에서 점검 →')]) : '—'),
      h('dt', {}, '최근 백업'), h('dd', {}, bkSt ? (bkSt.last_at ? [chip(bkSt.stale ? `${Math.floor(bkSt.age_hours)}시간 전 · 확인 필요` : '정상', bkSt.stale ? 'bad' : 'ok'), ` ${kst(bkSt.last_at)}`] : chip('없음', 'bad')) : '—',
        bkSt && bkSt.auto && bkSt.auto.at && !bkSt.auto.ok ? h('div', { class: 'small err' }, `자동 백업 실패: ${bkSt.auto.error || ''}`) : null,
        bkSt && !bkSt.key ? h('div', { class: 'small muted' }, '백업 키가 없어 자동 백업을 하지 않습니다.') : null))) : null;
  const nodeCls = (st) => (st === 'DONE' ? 'ok' : ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(st) ? 'warn' : st === 'CANCELLED' ? 'neutral' : st === 'IN_PROGRESS' ? 'info' : '');
  return h('div', { class: 'dash' }, h('div', { class: 'pagehead' }, h('h1', {}, '오늘 현황'), h('div', { class: 'row' }, h('label', { class: 'f inline' }, h('span', { class: 'sr-only' }, '기준일'), date), sum.closed ? chip('마감됨', 'ok') : chip('마감 전'))),
    h('div', { class: 'kpis' },
      kpi('수거 예정', `${active}건`, (bs.CANCELLED ? `취소 ${bs.CANCELLED}건 제외` : ' '), '', '#/requests'),
      kpi('수거 완료', `${done}건`, h('span', { class: 'bar' }, h('i', { style: `width:${pct}%` })), 'ok'),
      kpi('진행·대기', `${open}건`, bs.IN_PROGRESS ? `처리 중 ${bs.IN_PROGRESS}` : ' ', ''),
      kpi('재방문·보류', `${revisit}건`, bs.NOT_COLLECTED ? `미수거 ${bs.NOT_COLLECTED}` : ' ', revisit ? 'warn' : ''),
      kpi('하차 기록', `${sum.unload_total.volume_m3}m³`, sum.unloads.length ? `${sum.unloads.length}회 · ${sum.unload_total.weight_kg}kg` : '기록 없음', '')),
    h('div', { class: 'dash-grid' },
      h('div', { class: 'card mapcard' }, h('div', { class: 'panel-h' }, h('h2', {}, '배차 지도'), h('span', { class: 'muted small' }, act.length ? `확정 배정 ${act.length}팀 · ${act.reduce((a, b) => a + b.stops.length, 0)}곳` : '확정된 배정 없음')), mapBox,
        h('p', { class: 'muted small' }, '핀 번호는 팀-방문 순서, 녹색 핀은 수거 완료. 선은 방문 순서를 잇는 직선이며 실제 도로가 아닙니다.')),
      h('div', { class: 'dash-side' },
      overseeCard,
      h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '처리할 일'), todo.length ? h('span', { class: 'muted small' }, `${todo.length}종`) : null),
        todo.length ? h('ul', { class: 'todo' }, ...todo.map(([, t, href, cls]) => h('li', {}, h('a', { href, class: 'todo-a ' + cls }, h('span', {}, t), h('span', { class: 'arr' }, '→')))))
          : h('p', { class: 'empty' }, sum.total ? '지금 처리할 일이 없습니다. 결과를 가져오면 미회수·충돌·수납 확인이 여기에 모입니다.' : '이 날짜의 접수가 없습니다. 다른 날짜를 고르거나 접수를 등록하세요.')),
      (sum.dongs || []).length ? h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '동별 현황'), sum.capacity && sum.capacity.limit ? h('span', { class: 'muted small' }, `전체 ${sum.capacity.booked}/${sum.capacity.limit}건`) : null),
        h('div', { class: 'tablewrap' }, h('table', { class: 'dongtbl' }, h('thead', {}, h('tr', {}, ...['동', '담당 팀', '예정', '완료', '미처리', '한도 대비'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, ...sum.dongs.map((x) => h('tr', {}, h('td', {}, x.dong ? h('a', { href: '#/requests', onclick: () => { screenRequests.f = { ...(screenRequests.f || {}), size: 50, dong: x.dong, status: '', date_from: s.date, date_to: s.date, date_field: 'planned', page: 1, quick: 'ALL' }; } }, x.dong) : h('span', { class: 'muted' }, '(동 미지정)'),
            x.working ? null : chip('수거일 아님', 'neutral')),
            h('td', {}, x.crew || '-'), h('td', { class: 'num' }, x.total), h('td', { class: 'num' }, x.done), h('td', { class: 'num' }, x.open),
            h('td', {}, x.limit ? chip(`${x.total}/${x.limit}건`, x.total > x.limit ? 'bad' : x.total === x.limit ? 'warn' : 'ok') : '-', x.limit_m3 ? chip(`${x.m3}/${x.limit_m3}m³`, x.m3 > x.limit_m3 ? 'bad' : '') : null))))))) : null,
      h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '팀별 진행'), act.length ? h('span', { class: 'legend small' }, h('span', { class: 'lg ok' }, '완료'), h('span', { class: 'lg warn' }, '재방문'), h('span', { class: 'lg track' }, '남음')) : null),
        act.length ? h('div', { class: 'crews' }, ...act.map((b, bi) => {
          const n = b.stops.length, d = b.stops.filter((x) => x.status === 'DONE').length, w = b.stops.filter((x) => ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.status)).length;
          const last = b.bundles[0];
          return h('div', { class: `crew route c${(bi % 4) + 1}` }, h('div', { class: 'row crew-h' }, h('span', { class: 'swatch', 'aria-hidden': 'true' }), h('b', {}, b.crew_name), h('span', { class: 'muted small' }, `갱신본 ${b.assign_ver}`), h('span', { class: 'grow' }),
              !last ? chip('미전송', 'warn') : last.receipt_confirmed_at ? chip('수신 확인', 'ok') : chip('수신 미확인', 'warn'), b.resend_needed ? chip('재전송 필요', 'bad') : null),
            h('div', { class: 'seg' }, h('i', { class: 'ok', style: `flex:${d}` }), h('i', { class: 'warn', style: `flex:${w}` }), h('i', { class: 'rest', style: `flex:${Math.max(0, n - d - w)}` })),
            h('div', { class: 'row small' }, `${n}곳 중 완료 ${d} · 재방문 ${w}`),
            n ? h('ol', { class: 'tline', 'aria-label': `${b.crew_name} 방문 순서` }, ...b.stops.map((x, i) => h('li', { class: nodeCls(x.status), title: `${i + 1}. ${x.name} · ${STATUS[x.status] || x.status}` },
              h('b', {}, i + 1), h('small', {}, x.eta || '—'), h('span', { class: 'sr-only' }, ` ${x.name} ${STATUS[x.status] || ''}`)))) : null);
        })) : h('p', { class: 'empty' }, '확정된 배정이 없습니다.', role !== 'FIELD' ? h('a', { href: '#/dispatch' }, ' 배정·노선으로 →') : null)))),
    h('p', { class: 'muted small' }, '현황은 이 PC에 가져온 결과 기준입니다. 휴대폰에서 아직 보내지 않은 결과는 반영되지 않습니다.'));
}
const canWork = () => ['ADMIN', 'INTAKE'].includes(boot.me.role);  // 접수·수정·일괄 처리
const dongOpts = (cur, { all = false, none = '(동 미지정)' } = {}) => [h('option', { value: '' }, all ? '전체' : none),
  ...(all ? [h('option', { value: '-', selected: cur === '-' }, '(동 미지정)')] : []),
  ...boot.dongs.filter((d) => d.active || d.name === cur).map((d) => h('option', { value: d.name, selected: d.name === cur }, d.name + (d.active ? '' : ' (사용 안 함)')))];
async function screenRequests() {
  const f = (screenRequests.f ||= { q: '', date_field: 'planned', date_from: '', date_to: '', status: 'OPEN', crew: '', dong: '', pay: '', channel: '', quick: 'ALL', sort: '', dir: '', page: 1, size: 50 });
  const sel = (screenRequests.sel ||= new Set());
  const q = h('input', { type: 'search', id: 'rq-q', placeholder: '이름·연락처·주소·접수번호', value: f.q });
  const dfield = h('select', { id: 'rq-df', 'aria-label': '기간 기준' }, h('option', { value: 'planned', selected: f.date_field === 'planned' }, '예정일'), h('option', { value: 'received', selected: f.date_field === 'received' }, '접수일'));
  const dfrom = h('input', { type: 'date', id: 'rq-from', 'aria-label': '기간 시작', value: f.date_from }), dto = h('input', { type: 'date', id: 'rq-to', 'aria-label': '기간 끝', value: f.date_to });
  const status = h('select', { id: 'rq-status' }, ...[['OPEN', '미완료 전체'], ['', '전체'], ...Object.entries(STATUS)].map(([v, t]) => h('option', { value: v, selected: v === f.status }, t)));
  const crew = h('select', { id: 'rq-crew' }, h('option', { value: '' }, '전체'), ...boot.crews.map((c) => h('option', { value: c.id, selected: c.id === f.crew }, c.name)));
  const dong = h('select', { id: 'rq-dong' }, ...dongOpts(f.dong, { all: true }));
  const pay = h('select', { id: 'rq-pay' }, h('option', { value: '' }, '전체'), ...Object.entries(PAY).map(([k, t]) => h('option', { value: k, selected: k === f.pay }, t)));
  const channel = h('select', { id: 'rq-channel' }, h('option', { value: '' }, '전체'), ...boot.channels.map((c) => h('option', { value: c, selected: c === f.channel }, c)));
  const count = h('b', {}, '0');
  const tbody = h('tbody');
  const thead = h('thead');
  const quickBar = h('div', { class: 'quick' });
  const pager = h('div', { class: 'pager' });
  const selBar = h('div', { class: 'selbar' });
  const QUICK = [['ALL', '전체'], ['TODAY', '오늘 예정'], ['NOLOC', '위치 없음'], ['FEE', '금액 확인 대기'], ['UNASSIGNED', '미배정'], ['REFUND', '환불 필요']];
  const COLS = [['', null], ['번호', null], ['접수번호', 'receipt_no'], ['예정일', 'planned_date'], ['동', 'dong'], ['이름', 'name'], ['연락처', null], ['배출 주소', 'address'],
    ['품목', null], ['상태', 'status'], ['금액', null], ['위치', null], ['배정', null]];
  let res = { rows: [], total: 0, page: 1, pages: 1, counts: {} };
  const work = canWork();
  const drawHead = () => {
    const pageIds = res.rows.map((r) => r.id);
    const all = h('input', { type: 'checkbox', 'aria-label': '이 쪽 모두 고르기', checked: pageIds.length && pageIds.every((i) => sel.has(i)),
      onchange: (e) => { pageIds.forEach((i) => (e.target.checked ? sel.add(i) : sel.delete(i))); draw(); } });
    thead.replaceChildren(h('tr', {}, ...COLS.map(([t, key], i) => {
      if (i === 0) return h('th', { class: 'chk', scope: 'col' }, work ? all : null);
      if (!key) return h('th', { class: i === 1 ? 'num' : null, scope: 'col' }, t);
      const on = f.sort === key;
      return h('th', { scope: 'col', 'aria-sort': on ? (f.dir === 'desc' ? 'descending' : 'ascending') : 'none' },
        h('button', { type: 'button', class: 'sort' + (on ? ' on' : ''), onclick: () => { if (on) f.dir = f.dir === 'desc' ? '' : 'desc'; else { f.sort = key; f.dir = ''; } f.page = 1; reload(); } },
          t, h('span', { class: 'arr', 'aria-hidden': 'true' }, on ? (f.dir === 'desc' ? '▼' : '▲') : '↕')));
    })));
  };
  const draw = () => {
    quickBar.replaceChildren(...QUICK.map(([k, t]) => h('button', { type: 'button', class: 'qchip' + (f.quick === k ? ' on' : ''), 'aria-pressed': f.quick === k ? 'true' : 'false',
      onclick: () => { f.quick = k; f.page = 1; reload(); } }, t, h('b', {}, (k === 'ALL' ? res.counts.ALL : res.counts[k]) ?? 0))));
    count.textContent = res.total.toLocaleString();
    drawHead();
    const shortDate = (d) => (!d ? '-' : d === boot.today ? '오늘' : d.slice(5));
    const base = (res.page - 1) * res.size;
    tbody.replaceChildren(...(res.rows.length ? res.rows.map((r, ri) => h('tr', { class: 'click' + (sel.has(r.id) ? ' sel' : ''), onclick: (e) => { if (!['A', 'INPUT', 'LABEL'].includes(e.target.tagName)) go(`#/request/${r.id}`); } },
      h('td', { class: 'chk' }, work ? h('input', { type: 'checkbox', 'aria-label': `${r.receipt_no} 고르기`, checked: sel.has(r.id), onchange: (e) => { e.target.checked ? sel.add(r.id) : sel.delete(r.id); draw(); } }) : null),
      h('td', { class: 'num rn' }, base + ri + 1), h('td', { class: 'code' }, h('a', { href: `#/request/${r.id}`, title: r.receipt_no }, r.receipt_no.replace(/^DG-\d{4}/, ''))), h('td', { class: 'code' }, shortDate(r.planned_date)),
      h('td', { class: 'nowrap' }, r.dong || h('span', { class: 'muted' }, '-')), h('td', { class: 'nowrap' }, r.name), h('td', { class: 'code' }, fmtPhone(r.phone)),
      h('td', {}, h('span', { class: 'clamp' }, r.address)),
      h('td', {}, h('span', { class: 'clamp' }, r.items.map((i) => `${i.name} ${i.spec} ${i.qty}${i.unit_label}`).join(', '))),
      h('td', {}, r.status !== 'RECEIVED' ? statusChip(r.status) : null, r.state.contact !== 'NOT_CONTACTED' ? chip(CONTACT[r.state.contact]) : null, r.must_today ? chip('오늘 필수', 'warn') : null,
        (r.warnings || []).length ? h('span', { class: 'chip bad', title: r.warnings.join(' ') }, '관할 확인') : null, r.channel && r.channel !== '전화' ? chip(r.channel) : null),
      h('td', {}, r.status === 'CANCELLED' ? null : r.state.fee.final == null ? chip('금액 확인 대기', 'warn') : won(r.state.fee.final)),
      h('td', { class: 'c' }, r.lat == null ? chip('위치 없음', 'warn') : '✓'),
      h('td', {}, r.assigned_to ? `${r.assigned_to.name} ${r.assigned_to.work_date}` : ''))) : [h('tr', {}, h('td', { colspan: COLS.length, class: 'muted' }, f.q ? `'${f.q}'에 맞는 접수가 없습니다. 초성(예: ㄱㅅ)이나 전화번호 뒷자리로도 찾을 수 있습니다.` : '해당 접수가 없습니다. ', !f.q && work ? h('a', { href: '#/request/new' }, '전화 접수 등록 →') : null))]));
    const sizeSel = h('select', { 'aria-label': '쪽당 건수', onchange: (e) => { f.size = +e.target.value; f.page = 1; reload(); } }, ...[20, 50, 100, 200].map((n) => h('option', { value: n, selected: n === f.size }, `${n}건씩`)));
    pager.replaceChildren(btn('‹ 이전', () => { f.page = res.page - 1; reload(); }, 'small', { disabled: res.page <= 1 }), h('span', { class: 'pg' }, `${res.page} / ${res.pages}쪽`),
      btn('다음 ›', () => { f.page = res.page + 1; reload(); }, 'small', { disabled: res.page >= res.pages }), sizeSel);
    selBar.replaceChildren(...(work && sel.size ? [h('b', {}, `${sel.size}건 선택`), btn('선택 해제', () => { sel.clear(); draw(); }, 'small'),
      btn('예정일 변경', bulkDate, 'small'), btn('동 지정', bulkDong, 'small'), btn('취소', bulkCancel, 'small danger'), btn('목록 인쇄', bulkPrint, 'small'), btn('배출 표지', bulkLabels, 'small'), btn('CSV', bulkCsv, 'small')] : []));
  };
  const load = wrap(async () => {
    Object.assign(f, { q: q.value, date_field: dfield.value, date_from: dfrom.value, date_to: dto.value, status: status.value, crew: crew.value, dong: dong.value, pay: pay.value, channel: channel.value });
    const qs = new URLSearchParams(Object.entries(f).map(([k, v]) => [k, String(v ?? '')]));
    res = await api('requests?' + qs);
    f.page = res.page;
    draw();
  });
  // 조건을 바꾸면 이전 결과를 지우고 불러오는 중 표시 (옛 목록을 새 결과로 오인하지 않게)
  const reload = () => { tbody.replaceChildren(h('tr', {}, h('td', { colspan: COLS.length, class: 'muted' }, '불러오는 중…'))); load(); };
  let t = null;
  q.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { f.page = 1; load(); }, 300); });
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter') { clearTimeout(t); f.page = 1; load(); } });
  [dfield, dfrom, dto, status, crew, dong, pay, channel].forEach((x) => x.addEventListener('change', () => { f.page = 1; reload(); }));
  // 일괄 처리 (고른 건, 한 번에 한 트랜잭션·건별 이력)
  const ids = () => [...sel];
  const done = (m) => { toast(m); sel.clear(); load(); };
  const bulkDate = wrap(async () => {
    const d = h('input', { type: 'date', value: boot.today });
    const r = await reasonDlg(`예정일 변경 · ${sel.size}건`, boot.postpone_codes, { ok: '변경', before: [h('label', { class: 'f' }, '새 수거 예정일', d)],
      submit: async (v) => { if (!d.value) throw new Error('새 예정일을 고르세요.'); return post('requests/bulk', { action: 'reschedule', ids: ids(), date: d.value, ...v }); } });
    if (!r) return;
    (r.warnings || []).forEach((w) => toast(w, true));
    done(`${r.changed}건 예정일을 바꿨습니다`);
  });
  const bulkDong = wrap(async () => {
    const ds = h('select', {}, ...dongOpts(''));
    const why = h('input', { placeholder: '마감한 날의 접수가 있으면 필수', class: 'fill' });
    const r = await modal({ title: `동 지정 · ${sel.size}건`, ok: '지정', body: [h('label', { class: 'f' }, '행정동', ds), h('label', { class: 'f' }, '사유', why)],
      onOk: () => post('requests/bulk', { action: 'dong', ids: ids(), dong: ds.value, reason: why.value }) });
    if (r) done(`${r.changed}건 동을 지정했습니다`);
  });
  const bulkCancel = wrap(async () => {
    const r = await reasonDlg(`접수 취소 · ${sel.size}건`, boot.cancel_codes, { ok: '취소 기록', danger: true, label: '취소 사유',
      lead: [h('p', {}, '기록은 지워지지 않습니다. 이미 휴대폰에 보낸 건은 수거팀에 전화로 알리세요.')], submit: (v) => post('requests/bulk', { action: 'cancel', ids: ids(), ...v }) });
    if (!r) return;
    if (r.refund_needed.length) toast(`납부 기록이 있는 ${r.refund_needed.length}건은 '환불 필요'입니다: ${r.refund_needed.join(', ')}`, true);
    if (r.field_notice_needed) toast(`휴대폰에 보낸 ${r.field_notice_needed}건은 수거팀에 전화로 알리세요.`, true);
    done(`${r.cancelled}건 취소했습니다` + (r.skipped ? ` (이미 취소 ${r.skipped}건 제외)` : ''));
  });
  const bulkPrint = wrap(async () => {
    if (!(await confirmDlg('목록 인쇄', `고른 ${sel.size}건을 이름·연락처·주소와 함께 인쇄합니다. 출력 기록이 건마다 남고, 업무가 끝나면 파쇄해야 합니다.`, { ok: '인쇄 화면 열기' }))) return;
    const r = await post('requests/print', { ids: ids() });
    window.open('/print/requests/' + r.token, 'dg-print');
  });
  const bulkLabels = wrap(async () => {
    const r = await post('requests/labels', { ids: ids() });
    window.open('/print/doc/' + r.token, 'dg-print'); toast('배출 표지 인쇄 화면을 열었습니다 (A4 한 장에 6개, 품목 수량만큼)');
  });
  const bulkCsv = wrap(async () => {
    const pii = h('input', { type: 'checkbox' });
    const ok = await modal({ title: `고른 ${sel.size}건 CSV`, ok: '내려받기', body: [h('p', {}, '기본은 통계 CSV와 같이 이름·연락처·주소 없이 내보냅니다.'),
      h('label', { class: 'row' }, pii, '이름·연락처·주소 포함 (개인정보 — 승인된 용도만, 건마다 내보낸 기록이 남음)')],
      onOk: async () => { if (pii.checked && !(await confirmDlg('개인정보 포함 내보내기', '이름·연락처·주소가 든 파일입니다. 승인된 보안 매체에만 저장하고 쓴 뒤 지우세요. 계속할까요?', { ok: '개인정보 포함 내보내기', danger: true }))) throw new Error('취소했습니다.'); return { pii: pii.checked }; } });
    if (!ok) return;
    await download('requests/export', `동구-선택-${sel.size}건${ok.pii ? '-개인정보' : ''}.csv`, { method: 'POST', body: { ids: ids(), include_pii: ok.pii } });
    toast('내려받았습니다');
  });
  load();
  setTimeout(() => { if (!document.activeElement || document.activeElement === document.body) q.focus(); }, 30);
  // 조회조건 상자(회색 이름 칸 + 값 칸) → 목록 머리줄(총 N건 · 단추) → 목록 표
  const cl = (t, forId) => h('label', { class: 'cond-l', for: forId }, t);
  return h('div', {}, h('div', { class: 'pagehead' }, h('h1', {}, '접수'), h('span', { class: 'muted small' }, '단축키: ', h('kbd', {}, '/'), ' 찾기 · ', h('kbd', {}, 'N'), ' 새 접수')),
    h('div', { class: 'card filters searchbox', role: 'search', 'aria-label': '조회조건' },
      h('div', { class: 'cond' }, cl('검색어', 'rq-q'), h('div', { class: 'cond-v' }, q), cl('기간', 'rq-from'), h('div', { class: 'cond-v' }, dfield, dfrom, '~', dto), cl('처리 상태', 'rq-status'), h('div', { class: 'cond-v' }, status),
        cl('수거팀', 'rq-crew'), h('div', { class: 'cond-v' }, crew), cl('행정동', 'rq-dong'), h('div', { class: 'cond-v' }, dong), cl('수납 상태', 'rq-pay'), h('div', { class: 'cond-v' }, pay),
        cl('접수 경로', 'rq-channel'), h('div', { class: 'cond-v' }, channel),
        h('span', { class: 'cond-l' }, '빠른 조회'), h('div', { class: 'cond-v wide2' }, quickBar)),
      h('div', { class: 'cond-btns' }, btn('검색', () => { f.page = 1; load(); }, 'dark'))),
    h('div', { class: 'gridbar' }, h('span', { class: 'cnt' }, '총 ', count, '건'), selBar, h('span', { class: 'grow' }),
      work ? btn('일괄 등록(CSV)', () => importDlg().then((n) => { if (n) load(); }), '') : null, work ? btn('+ 전화 접수 등록', () => go('#/request/new'), 'primary') : null),
    h('div', { class: 'card tablecard' }, h('table', { class: 'grid' }, thead, tbody)), pager);
}

// 접수 일괄 등록: 서식 내려받기 → 파일 고르기 → 미리보기(오류·중복 후보) → 등록
async function importDlg() {
  const file = h('input', { type: 'file', accept: '.csv,.txt', 'aria-label': '접수 CSV 파일' });
  const skip = h('input', { type: 'checkbox' });
  const out = h('div', { class: 'imp-out' });
  let previewed = false;
  const raw = async () => { const f0 = file.files[0]; if (!f0) throw new Error('파일을 고르세요.'); return new Uint8Array(await f0.arrayBuffer()); };
  const prev = async () => {
    const r = await api('requests/import?preview=1', { method: 'POST', raw: await raw() });
    previewed = true;
    out.replaceChildren(h('p', {}, `${r.rows}행 · 등록 가능 ${r.ok}행 · 오류 ${r.errors}행 · 중복 후보 ${r.duplicates}행`),
      h('div', { class: 'imp-scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ...['행', '이름', '연락처', '주소', '예정일', '동', '경로', '품목', '확인'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...r.lines.map((x) => h('tr', { class: x.errors.length ? 'bad' : '' }, h('td', {}, x.line), h('td', {}, x.summary.name), h('td', {}, fmtPhone(x.summary.phone)), h('td', {}, x.summary.address),
          h('td', {}, x.summary.planned_date || '-'), h('td', {}, x.summary.dong || '-'), h('td', {}, x.summary.channel), h('td', {}, x.summary.items),
          h('td', {}, ...x.errors.map((e) => chip(e, 'bad')), x.duplicates.length ? chip('중복 후보: ' + x.duplicates.join(', '), 'warn') : null, ...x.warnings.map((w) => h('div', { class: 'small muted' }, w)))))))));
  };
  file.addEventListener('change', () => { previewed = false; out.replaceChildren(); });
  return modal({ title: '접수 일괄 등록 (CSV)', ok: '등록', wide: true,
    body: [h('p', { class: 'muted' }, '서식에 맞춘 CSV(엑셀에서 "CSV UTF-8"로 저장)를 넣습니다. 미리보기에서 오류 행은 등록되지 않습니다. 같은 연락처·주소는 중복 후보로 표시하며 자동으로 합치지 않습니다.'),
      h('div', { class: 'row' }, btn('서식 내려받기', wrap(() => download('requests/import-template', '동구-접수-일괄등록-서식.csv')), 'small'), file, btn('미리보기', wrap(prev), 'small')),
      h('label', { class: 'row' }, skip, '중복 후보 행은 건너뛰기'), out],
    onOk: async () => {
      if (!previewed) { await prev(); throw new Error('미리보기를 확인한 뒤 [등록]을 누르세요.'); }
      const r = await api(`requests/import?preview=0&skip_dup=${skip.checked ? 1 : 0}`, { method: 'POST', raw: await raw() });
      toast(`${r.created}건 등록했습니다` + (r.skipped_lines.length ? ` (건너뜀: ${r.skipped_lines.join(', ')}행)` : ''));
      return r.created || true;
    } });
}

// ------------------------------------------------------------ 접수 등록·수정
// 새 접수 작성 중 내용(이 화면 메모리에만, 브라우저 저장소에 남기지 않음): 다른 화면에 다녀오거나 다시 로그인해도 이어서 쓴다
let intakeDraft = null;
async function screenRequest(id) {
  const isNew = id === 'new';
  const blank = { name: '', phone: '', address: '', spot_note: '', memo: '', planned_date: '', appt_start: '', appt_end: '', must_today: 0, urgent: 0, priority: 0, lat: null, lon: null,
    items: [], versions: [], locked_lines: [], status: 'RECEIVED', dong: null, channel: '전화', contacts: [] };
  const fromDraft = isNew && intakeDraft;
  const r = isNew ? { ...blank, ...(intakeDraft ? intakeDraft.r : {}), items: intakeDraft ? intakeDraft.items : [] } : await api('requests/' + id);
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
  const detect = wrap(async () => {
    if (dongTouched || !F.address.value.trim() || !boot.dongs.some((d) => d.active)) return;
    const d = await api('dong-detect?address=' + encodeURIComponent(F.address.value));
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
  const sugBtn = (x) => h('button', { type: 'button', role: 'option', onclick: () => pickAddr(x) }, chip(x.kind, x.kind === '행안부' ? 'warn' : 'info'), ` ${x.label}`, x.dong ? h('span', { class: 'p' }, x.dong) : null);
  // 이 PC의 주소 자료 결과를 먼저, 그 아래 행안부 결과(설정에 승인키가 있을 때). ext: 행안부 응답 또는 { loading: true }
  const drawSug = (list, ext = null) => {
    // 같은 건물(도로명+건물번호)이 이 PC 자료에 이미 있으면 행안부 결과에서 뺀다
    const roadKey = (a) => { const m = /([가-힣A-Za-z0-9·.]+(?:대로|로|길))\s*(\d+(?:-\d+)?)/.exec(a || ''); return m ? m[1] + m[2] : a; };
    const seen = new Set(list.map((x) => roadKey(x.address)));
    const extItems = ((ext && ext.items) || []).filter((x) => !seen.has(roadKey(x.address)));
    const kids = [...list.map(sugBtn), ...extItems.map(sugBtn)];
    if (ext && ext.loading) kids.push(h('p', { class: 'muted pad-s small' }, '행안부 주소 검색 중…'));
    if (ext && (ext.blocked || ext.error)) kids.push(h('p', { class: 'muted pad-s small jusonote' }, '행안부 검색: ' + (ext.blocked || ext.error)));
    if (!kids.length && addrQ.value.trim().length >= 2) kids.push(h('p', { class: 'muted pad-s' }, '주소 자료에서 찾지 못했습니다. 배출 주소 칸에 직접 적고 지도로 위치를 지정하세요.'));
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
    if (!g || g.none) { geoMsg.textContent = '주소 자료에서 찾지 못했습니다. 도로명과 건물번호를 확인하거나 지도를 눌러 지정하세요.'; return; }
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
  let dupAck = false;
  const save = wrap(async () => {
    if (isNew) {
      if (!dupAck) {
        await checkDup();
        if (dupBox.childNodes.length) { dupAck = true; msg.textContent = '비슷한 접수가 있습니다. 별개 접수가 맞으면 [접수 등록]을 한 번 더 누르세요.'; return; }
      }
      const res = await post('requests', data());
      intakeDraft = null;
      toast(`${res.receipt_no} 등록했습니다`); (res.warnings || []).forEach((w) => toast(w, true)); go(`#/request/${res.id}`);
    } else {
      const res = await post('requests/' + id, { ...data(), reason: reason.value });
      toast(res.resend_needed ? '수정했습니다. 이미 배정된 건이라 목록 재전송이 필요합니다.' : '수정했습니다'); (res.warnings || []).forEach((w) => toast(w, true)); render();
    }
  }, msg);
  const cancel = wrap(async () => {
    const paid = r.payment ? r.payment.paid : 0;
    const res = await reasonDlg('접수 취소', boot.cancel_codes, { ok: '취소 기록', danger: true, label: '취소 사유',
      lead: [h('p', {}, '기록은 지워지지 않습니다.'), paid > 0 ? h('div', { class: 'notice bad' }, `납부 기록 ${won(paid)}이 있습니다. 취소하면 '환불 필요'로 표시되고 마감 수납 확인 목록에 나옵니다.`) : null,
        r.closed ? h('div', { class: 'notice warn' }, `마감한 날(${r.closed.work_date})의 접수입니다. '마감 후 수정'으로 기록됩니다.`) : null],
      submit: (v) => post(`requests/${id}/cancel`, { code: v.code, reason: v.text }) });
    if (!res) return;
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
      btn('적용', wrap(async () => { if (!sel) throw new Error('적용할 품목 행을 고르세요.'); await post(`requests/${id}/link`, { line_id: l.line_id, fee_rate_id: sel, reason: why.value, confirmed_by: who.value }); toast('유사 품목 단가를 적용했습니다'); render(); }, m), 'primary'), m),
      h('p', { class: 'small muted' }, '단가가 확정된 행만, 부과단위가 같은 행만 적용됩니다. 원래 품목 이름은 그대로 두고 적용 기록을 따로 남깁니다.')));
    setTimeout(() => q.focus(), 20);
  };

  // 현장 결과
  const st = r.state;
  const field = !isNew && st ? h('div', { class: 'card' }, h('h2', {}, '현장 결과'),
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
          l.link && editable && boot.me.role !== 'FIELD' ? btn('해제', wrap(async () => { const why = await promptDlg('유사 품목 적용 해제', '해제 사유', { ok: '해제', danger: true }); if (!why) return; await post(`links/${l.link.id}/void`, { reason: why }); toast('해제했습니다'); render(); }), 'small') : null)),
        h('tr', { class: 'linkrow', hidden: true, 'data-line': l.line_id }, h('td', { colspan: 9 }))])))),
    h('dl', { class: 'kv' }, h('dt', {}, '접수 당시 금액'), h('dd', {}, won(st.fee.received)), h('dt', {}, '현장 추가분'), h('dd', {}, won(st.fee.field_added)),
      h('dt', {}, '접수 대비 증감'), h('dd', {}, (st.fee.diff > 0 ? '+' : '') + won(st.fee.diff)), h('dt', {}, '금액 확인 대기'), h('dd', {}, st.fee.pending_count + '건'),
      h('dt', {}, '최종 금액'), h('dd', {}, st.fee.final == null ? '미확정' : won(st.fee.final))),
    h('p', { class: 'muted' }, '금액은 현장 확인 수량 기준이며 수납 완료를 뜻하지 않습니다. 일부 수거로 금액을 자동 감액하지 않습니다.'),
    (st.memos || []).length ? h('div', { class: 'notice' }, h('strong', {}, '현장 메모'), h('ul', {}, ...st.memos.map((m) => h('li', {}, m)))) : null,
    r.photos && r.photos.length ? h('div', { class: 'thumbs' }, ...r.photos.map((p) => p.purged_at ? chip(`보관 기간 만료로 정리됨 (${p.purged_at.slice(0, 10)})`) : p.recovery_status === 'RECOVERED' ? h('figure', { class: 'ph' }, photoImg(p.photo_id), h('figcaption', {}, (p.phase === 'BEFORE' ? '수거 전' : '수거 후') + (p.line_id && st.lines.find((l) => l.line_id === p.line_id) ? ` · ${st.lines.find((l) => l.line_id === p.line_id).name}` : ''),
      p.taken_at ? h('small', { class: 'block' }, `촬영 ${kst(p.taken_at)}${p.has_gps ? ` · 위치 ±${Math.round(p.acc || 0)}m` : ''}`) : null)) : chip(`사진 미회수 (${p.phase === 'BEFORE' ? '전' : '후'})`, 'warn'))) : null) : null;

  // 전화 받는 순서대로: 왼쪽 = 신청자 → 버릴 물건, 오른쪽 = 일정 → 위치. 저장 단추는 화면 아래에 늘 보이게.
  F.lat.tabIndex = -1; F.lon.tabIndex = -1;
  const actionbar = editable ? h('div', { class: 'actionbar' }, isNew ? null : reason,
    btn(isNew ? '접수 등록' : '수정 저장', save, 'primary', { 'data-save': '1' }), h('span', { class: 'muted small' }, h('kbd', {}, 'Ctrl'), '+', h('kbd', {}, 'Enter')),
    barTotal, h('span', { class: 'grow' }), msg, isNew ? null : btn('접수 취소', cancel, 'danger')) : null;
  setTimeout(() => { loadCal(); if (isNew && !(fromDraft && r.dong)) detect(); }, 0);
  const form = h('div', { class: 'reqform', oninput: () => saveDraft(), onchange: () => saveDraft() },
    h('div', { class: 'pagehead' }, h('h1', {}, isNew ? '전화 접수 등록' : `접수 ${r.receipt_no}`),
      h('div', { class: 'row' }, !isNew && r.levy_no ? h('span', { class: 'chip' }, `부과번호 ${r.levy_no}`) : null,
        !isNew && canWork() ? btn('접수증 인쇄', wrap(async () => { const t = await post(`requests/${id}/receipt-print`); window.open('/print/doc/' + t.token, 'dg-print'); toast('접수증을 열었습니다 (출력 기록이 남습니다)'); }), 'small') : null,
        !isNew && canWork() && r.status !== 'CANCELLED' ? btn('배출 표지 인쇄', wrap(async () => { const t = await post('requests/labels', { ids: [id] }); window.open('/print/doc/' + t.token, 'dg-print'); toast('배출 표지를 열었습니다'); }), 'small') : null,
        btn('← 목록', () => go('#/requests')))),
    fromDraft ? h('div', { class: 'notice info' }, '작성 중이던 접수를 불러왔습니다. ', btn('비우고 새로 쓰기', () => { intakeDraft = null; render(true); }, 'small')) : null,
    r.status === 'CANCELLED' ? h('div', { class: 'notice' }, '취소된 접수입니다. 사유: ' + (r.cancel_label || r.cancel_reason || '-')) : null,
    r.closed && r.status !== 'CANCELLED' ? h('div', { class: 'notice warn' }, `이 접수의 예정일(${r.closed.work_date})은 마감되었습니다(${r.closed.seq}회차 · ${r.closed.status === 'APPROVED' ? '팀장 확인' : '담당자 기록'}). 고치면 사유와 함께 "마감 후 수정"으로 기록됩니다.`) : null,
    h('div', { class: 'grid2' },
      h('div', {},
        h('div', { class: 'card formtable' }, h('h2', {}, '신청자'),
          h('div', { class: 'grid2' }, lab('이름', F.name), lab('연락처', F.phone)),
          editable ? h('div', { class: 'addr-find' }, h('label', { class: 'f' }, '주소 찾기', addrQ), addrSug) : null,
          lab('배출 주소', F.address),
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
    actionbar,
    field,
    !isNew ? paymentCard(r) : null,
    !isNew ? reductionCard(r) : null,
    !isNew ? contactCard(r) : null,
    !isNew && r.versions.length ? h('div', { class: 'card' }, h('h2', {}, '수정 이력'), h('ul', {}, ...r.versions.map((v) => h('li', {}, `버전 ${v.req_ver} → ${v.req_ver + 1} · ${kst(v.at)} · ${v.reason}`)))) : null);
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
function reductionCard(r) {
  const msg = h('span', { class: 'msg' });
  const sel = h('select', { 'aria-label': '감면 사유' }, h('option', { value: '' }, '감면 없음'), ...(boot.settings.reductions || []).filter((x) => x.active !== false || x.code === r.reduction_code).map((x) => h('option', { value: x.code, selected: x.code === r.reduction_code }, `${x.label} ${x.rate}%`)));
  const proof = h('select', { 'aria-label': '증빙 확인 방법' }, h('option', { value: '' }, '(증빙 확인 방법)'), ...(boot.settings.reduction_proofs || []).map((x) => h('option', { value: x, selected: x === r.reduction_proof }, x)));
  const who = h('input', { 'aria-label': '확인자', placeholder: '확인자', value: r.reduction_checker || boot.me.display_name, class: 'w-s' });
  const why = r.closed ? h('input', { placeholder: '마감 후 수정 사유 (필수)', 'aria-label': '감면 변경 사유', class: 'w-m' }) : null;
  return h('div', { class: 'card' }, h('h2', {}, '수수료 감면 ', r.reduction_code ? chip(`${r.reduction_label} ${r.reduction_rate}%`, 'info') : chip('없음', 'neutral')),
    r.reduction_code ? h('p', { class: 'small muted' }, `증빙 ${r.reduction_proof || '-'} · 확인 ${r.reduction_checker || '-'} · ${kst(r.reduction_at)}`) : null,
    canWork() && r.status !== 'CANCELLED' ? h('div', { class: 'row' }, sel, proof, who, why, btn('감면 저장', wrap(async () => {
      if (!sel.value && !r.reduction_code) throw new Error('감면 사유를 고르세요.');
      await post(`requests/${r.id}/reduction`, { reduction_code: sel.value, reduction_proof: proof.value, reduction_checker: who.value, reason: why ? why.value : '' });
      toast(sel.value ? '감면을 적용했습니다' : '감면을 해제했습니다'); render();
    }, msg), 'primary small'), msg) : null,
    h('p', { class: 'small muted' }, '감면율은 설정의 정책 값입니다(조례 확인 필요). 주민등록번호·수급자 번호 같은 식별번호는 저장하지 않습니다.'));
}

// 민원·통화 기록 (접수별): 일시·유형·내용·담당자. 내용은 변경 이력에 남기지 않는다.
function contactCard(r) {
  const msg = h('span', { class: 'msg' });
  const now = new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 16);
  const at = h('input', { type: 'datetime-local', value: now, 'aria-label': '일시' });
  const kind = h('select', { 'aria-label': '유형' }, ...boot.contact_kinds.map((k) => h('option', { value: k }, k)));
  const content = h('input', { placeholder: '내용 (예: 수거 날짜 문의, 오후 방문 요청)', class: 'fill', 'aria-label': '내용' });
  return h('div', { class: 'card' }, h('h2', {}, `민원·통화 기록 ${r.contacts.length}건`),
    r.contacts.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['일시', '유형', '내용', '담당자'].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...r.contacts.map((c) => h('tr', {}, h('td', { class: 'nowrap' }, c.at.replace('T', ' ')), h('td', {}, chip(c.kind, c.kind === '불만' ? 'bad' : c.kind === '독촉' ? 'warn' : '')), h('td', {}, c.content), h('td', {}, c.actor || ''))))) : h('p', { class: 'empty' }, '기록 없음'),
    canWork() ? h('div', { class: 'row' }, at, kind, content, btn('기록', wrap(async () => { await post(`requests/${r.id}/contacts`, { at: at.value, kind: kind.value, content: content.value }); toast('기록했습니다'); render(); }, msg), 'primary'), msg) : null);
}

// 수납: 담당자가 확인한 납부 기록. 계산 금액·납부 금액·상태를 따로 보여 주고, 정정은 지우지 않고 사유를 남긴다
function paymentCard(r) {
  const p = r.payment;
  const msg = h('span', { class: 'msg' });
  const kind = h('select', {}, h('option', { value: 'PAY' }, '납부'), h('option', { value: 'REFUND' }, '환불'));
  const remain = p.levied != null ? Math.max(0, p.levied - p.paid) : '';
  const amount = h('input', { type: 'number', min: 1, value: remain || '', placeholder: '금액(원)', style: 'width:120px', 'aria-label': '금액' });
  const methods = () => (kind.value === 'REFUND' ? boot.settings.refund_methods : boot.settings.pay_methods) || ['기타'];
  const method = h('select', { 'aria-label': '납부 방법' });
  const fillMethods = () => method.replaceChildren(...methods().map((m) => h('option', { value: m }, m)));
  fillMethods(); kind.addEventListener('change', fillMethods);
  const ref = h('input', { placeholder: '영수번호·거래번호', class: 'w-s', 'aria-label': '영수번호·거래번호' });
  const paidOn = h('input', { type: 'date', value: boot.today });
  const note = h('input', { placeholder: '메모(입금자명 등)', style: 'flex:1' });
  const why = r.closed ? h('input', { placeholder: '마감 후 수정 사유 (필수)', style: 'flex:1', 'aria-label': '마감 후 수정 사유' }) : null;
  if (p.status === 'REFUND_NEEDED') { kind.value = 'REFUND'; amount.value = p.paid; fillMethods(); }
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
        h('td', {}, x.void_reason ? '' : btn('정정', wrap(async () => { const why = await promptDlg('수납 기록 정정', '정정 사유', { ok: '정정', lead: [h('p', { class: 'muted' }, '기록은 지워지지 않고 정정 표시와 사유가 남습니다.' + (r.closed ? ' 마감한 날의 접수라 "마감 후 수정"으로 기록됩니다.' : ''))] }); if (!why) return; await post(`payments/${x.id}/void`, { reason: why }); render(); }, msg), 'small'))))))) : null,
    p.status === 'REFUND_NEEDED' ? h('div', { class: 'notice bad' }, `취소된 접수에 납부 기록 ${won(p.paid)}이 남아 있습니다. `, h('a', { href: '#/refunds' }, '환불 목록'), '에서 신청 → 결재 → 지급 기록을 남기세요.') : null,
    (r.refunds || []).length ? h('div', { class: 'notice info' }, h('strong', {}, '환불 '), ...(r.refunds || []).map((f) => h('span', { class: 'block' }, chip(REFUND_ST[f.status] || f.status, f.status === 'PAID' ? 'ok' : f.status === 'REJECTED' || f.status === 'VOID' ? 'neutral' : 'warn'),
      ` ${REFUND_CAUSE[f.cause] || f.cause} ${won(f.amount)}${f.approved_by ? ' · 결재 ' + f.approved_by : ''}${f.paid_on ? ' · 지급 ' + f.paid_on : ''}`)), ' ', h('a', { href: '#/refunds' }, '환불 목록 →')) : null,
    canWork() && !openRefund && r.status === 'PARTIAL' && p.paid > 0 ? h('div', { class: 'row' }, btn('일부 수거 차액 환불 신청', wrap(async () => { go('#/refunds'); }), 'small')) : null,
    canWork() ? h('div', { class: 'row' }, kind, amount, method, ref, paidOn, note, why, btn('수납 기록', wrap(async () => {
      await post(`requests/${r.id}/payments`, { kind: kind.value, amount: amount.value, method: method.value, receipt_ref: ref.value, paid_on: paidOn.value, note: note.value, reason: why ? why.value : '' }); toast('수납을 기록했습니다'); render();
    }, msg), 'primary'), msg) : null,
    h('p', { class: 'muted' }, '수납 상태는 부과 금액(계산 금액 − 감면액)과 비교합니다. 일부 수거로 금액을 자동으로 줄이거나 환불을 확정하지 않습니다. 환불은 환불 목록에서 결재를 거쳐 지급 기록을 남깁니다.'));
}

function photoImg(pid) {
  const img = h('img', { alt: '현장 사진' });
  fetch('/api/photo/' + pid, { headers: { 'X-DG-CSRF': '1' }, credentials: 'same-origin' }).then((r) => r.ok && r.blob()).then((b) => { if (b) img.src = URL.createObjectURL(b); });
  return img;
}

// ------------------------------------------------------------ 배정·노선
// 카카오맵 웹 길찾기 링크(지점마다): 좌표와 "N번 수거지"만 담는다. 이름·주소는 넣지 않는다. (휴대폰 web/nav.js와 같은 형식)
function kakaoLink(q, n) {
  if (!q || !NAV.hasCoord(q)) return null;
  const label = NAV.navLabel(n);
  return h('a', { class: 'small maplink', href: NAV.navUrls('kakao', { lat: q.lat, lon: q.lon, label }).fallback, target: '_blank', rel: 'noopener noreferrer',
    title: `카카오맵에서 ${label}까지 길찾기 (좌표만 전달)`, 'aria-label': `${label} 카카오맵 길찾기` }, ' 카카오맵');
}
async function screenDispatch() {
  const s = (screenDispatch.s ||= { date: boot.today, crews: new Set(), seen: new Set(), plan: null });
  const date = h('input', { type: 'date', id: 'dp-date', value: s.date });
  const crews = boot.crews.filter((c) => c.active);
  crews.forEach((c) => { if (!s.seen.has(c.id)) { s.seen.add(c.id); s.crews.add(c.id); } });  // 새로 만든 팀은 자동으로 포함
  const calcBtn = btn('노선 계산', null, 'primary');
  const crewHint = h('span', { class: 'muted small' });
  const syncCalc = () => { const none = ![...s.crews].some((id) => crews.some((c) => c.id === id)); calcBtn.disabled = none; crewHint.textContent = none ? '팀을 하나 이상 고르세요.' : ''; };
  const crewBox = h('div', { class: 'row crewpick' }, ...crews.map((c) => h('label', { class: 'row' }, h('input', { type: 'checkbox', checked: s.crews.has(c.id), onchange: (e) => { e.target.checked ? s.crews.add(c.id) : s.crews.delete(c.id); syncCalc(); } }),
    `${c.name} (${c.capacity_m3}m³, ${c.shift_start}~${c.shift_end}${c.device_id ? '' : ', 휴대폰 미연결'})`)), crewHint);
  const msg = h('p', { class: 'msg' });
  const planMsg = h('p', { class: 'msg' });
  const out = h('div');
  const calc = wrap(async () => {
    s.date = date.value; calcBtn.disabled = true;
    out.replaceChildren(h('div', { class: 'card loading', 'aria-busy': 'true' }, '노선을 계산하는 중입니다 (몇 초 걸립니다)'));
    let res;
    try { res = await post('route', { date: s.date, crew_ids: [...s.crews].filter((id) => crews.some((c) => c.id === id)) }); } finally { syncCalc(); }
    s.plan = { res, routes: res.routes.map((r) => ({ crew_id: r.crew_id, crew_name: r.crew_name, stops: r.stops.map((x) => ({ ...x, unload_after: x.unload_after ? { ...x.unload_after } : undefined })), end: r.end, unloads: r.unloads })),
      unassigned: res.unassigned.map((u) => ({ ...u })) };
    draw();
  }, msg);
  calcBtn.addEventListener('click', calc); syncCalc();
  const draw = () => {
    liveMaps.forEach((m) => { try { m.remove(); } catch {} }); liveMaps = [];
    const p = s.plan;
    if (!p) { out.replaceChildren(h('div', { class: 'card empty-state' }, h('p', { class: 'muted' }, '날짜와 팀을 고르고 [노선 계산]을 누르세요. 결과는 추천안이며 순서를 바꿔 확정할 수 있습니다.'))); return; }
    const R = p.res.requests;
    const clearUnload = (ri) => { if (p.routes[ri].stops.some((x) => x.unload_after)) { p.routes[ri].stops.forEach((x) => delete x.unload_after); p.routes[ri].unloadStale = true; } };
    const move = (ri, i, d) => { const a = p.routes[ri].stops; const j = i + d; if (j < 0 || j >= a.length) return; [a[i], a[j]] = [a[j], a[i]]; clearUnload(ri); p.adjusted = true; draw(); };
    const toCrew = (ri, i, target) => { p.adjusted = true; clearUnload(ri); if (target !== 'OUT') clearUnload(+target); const [x] = p.routes[ri].stops.splice(i, 1); if (target === 'OUT') p.unassigned.push({ request_id: x.request_id, reason: '담당자가 제외' }); else p.routes[+target].stops.push({ request_id: x.request_id, eta: '' }); draw(); };
    const cols = p.routes.map((rt, ri) => h('div', { class: `card route c${(ri % 4) + 1}` }, h('h2', {}, h('span', { class: 'swatch', 'aria-hidden': 'true' }), `${rt.crew_name} · ${rt.stops.length}곳`, rt.end ? h('span', { class: 'muted' }, ` 예상 종료 ${rt.end}`) : null, rt.unloads ? chip(`반입지 경유 ${rt.unloads}회`) : null),
      rt.unloadStale ? h('div', { class: 'notice warn small' }, '순서를 바꿔 반입지 경유 위치가 지워졌습니다. 적재가 넘치지 않는지 확인하거나 [노선 계산]을 다시 하세요.') : null,
      ...rt.stops.flatMap((x, i) => [h('div', { class: 'stopline' }, h('span', { class: 's' }, i + 1),
        h('span', { class: 't' }, `${R[x.request_id].name} · ${R[x.request_id].address}`, h('br'), h('span', { class: 'muted' }, `${x.eta ? '도착 ' + x.eta : '시간 재계산 필요'}${x.load_m3 != null ? ` · 누적 적재(추정) ${x.load_m3}m³` : ''}${R[x.request_id].appt[0] ? ` · 약속 ${R[x.request_id].appt[0]}~${R[x.request_id].appt[1] || ''}` : ''}`),
          R[x.request_id].must_today ? chip('오늘 필수', 'warn') : null, x.same_place > 1 ? chip(`같은 장소 ${x.same_place}건`) : null,
          R[x.request_id].dong ? chip(R[x.request_id].dong) : null,
          R[x.request_id].pref_crew_id && R[x.request_id].pref_crew_id !== rt.crew_id ? chip('동 담당 팀 아님', 'info') : null,
          kakaoLink(R[x.request_id], i + 1)),
        h('span', { class: 'ctl' }, btn('▲', () => move(ri, i, -1), 'small', { 'aria-label': `${i + 1}번 위로`, disabled: i === 0 }), btn('▼', () => move(ri, i, 1), 'small', { 'aria-label': `${i + 1}번 아래로`, disabled: i === rt.stops.length - 1 }),
          h('select', { class: 'small', 'aria-label': '다른 팀으로 이동', onchange: (e) => toCrew(ri, i, e.target.value) }, h('option', { value: '' }, '이동…'), ...p.routes.map((o, oi) => oi !== ri ? h('option', { value: oi }, o.crew_name) : null), h('option', { value: 'OUT' }, '이번 배정에서 빼기')))),
        x.unload_after ? h('div', { class: 'stopline unload' }, h('span', { class: 's' }, '⇩'), h('span', { class: 't' }, `${x.unload_after.label} 하차`, h('br'),
          h('span', { class: 'muted' }, `도착 ${x.unload_after.eta} · 약 ${x.unload_after.volume_m3}m³ 비움 · ${x.unload_after.minutes}분`))) : null])));
    const noLoc = p.unassigned.filter((u) => R[u.request_id] && R[u.request_id].lat == null).length;
    const un = h('div', { class: 'card' }, h('h2', {}, `미배정 ${p.unassigned.length}건`),
      noLoc ? h('div', { class: 'row' }, btn(`위치 없는 ${noLoc}건 주소 자료로 찾기`, wrap(async () => { const g = await post('geocode/missing', { date: s.date }); toast(`찾음 ${g.found}건 · 못 찾음 ${g.missing}건. 노선을 다시 계산합니다.`); calc(); }, msg), 'small'),
        h('span', { class: 'muted' }, '찾은 위치는 건물 출입구 기준입니다.')) : null, ...p.unassigned.map((u, i) => h('div', { class: 'stopline' },
      h('span', { class: 't' }, R[u.request_id] ? `${R[u.request_id].name} · ${R[u.request_id].address}` : u.request_id, h('br'), h('span', { class: 'muted' }, u.reason)),
      R[u.request_id] && R[u.request_id].lat == null ? btn('위치 지정', () => go(`#/request/${u.request_id}`), 'small') : null,
      R[u.request_id] ? btn('다른 날로 미루기', wrap(async () => {
        const r = await postponeDlg([u.request_id], R[u.request_id].dong, s.date, `${R[u.request_id].name} · ${R[u.request_id].address}`);
        if (!r) return;
        p.unassigned.splice(p.unassigned.indexOf(u), 1); draw();
      }, msg), 'small') : null,
      h('select', { class: 'small', 'aria-label': '팀에 넣기', onchange: (e) => { if (e.target.value === '') return; const [x] = p.unassigned.splice(i, 1); p.routes[+e.target.value].stops.push({ request_id: x.request_id, eta: '' }); p.adjusted = true; draw(); } },
        h('option', { value: '' }, '팀에 넣기…'), ...p.routes.map((o, oi) => h('option', { value: oi }, o.crew_name))))));
    const mapBox = h('div', { class: 'mapbox' });
    const adjReason = h('input', { placeholder: '조정 사유 (예: 골목 진입 불가로 순서 변경)', value: p.adjustReason || '', style: 'flex:1;min-width:280px', oninput: (e) => { p.adjustReason = e.target.value; } });
    const confirmBtn = btn('배정 확정', wrap(async () => {
      if (p.adjusted && !adjReason.value.trim()) { adjReason.focus(); throw new Error('추천에서 순서·팀을 바꿨습니다. 조정 사유를 적어 주세요.'); }
      const plan = {}; p.routes.forEach((rt) => { plan[rt.crew_id] = rt.stops.map((x) => ({ request_id: x.request_id, eta: x.eta || '', unload_after: x.unload_after || null })); });
      const res = await post('assign', { date: s.date, plan, adjusted: !!p.adjusted, adjust_reason: adjReason.value });
      toast(`배정 확정: ${res.map((b) => `${b.count}곳(갱신본 ${b.assign_ver})`).join(', ')}`); s.plan = null; go('#/send');
    }, planMsg), 'primary');
    const nStops = p.routes.reduce((a, rt) => a + rt.stops.length, 0);
    const lastEnd = p.routes.map((rt) => rt.end).filter(Boolean).sort().pop();
    const summary = p.routes.map((rt) => `${rt.crew_name} ${rt.stops.length}곳`).join(' · ') + (p.unassigned.length ? ` · 미배정 ${p.unassigned.length}건` : '');
    const note = p.res.matrix_source === 'KAKAO' ? p.res.matrix_note : `이동시간: ${(p.res.matrix_note || '').replace('로컬 OSRM', 'PC 안의 도로 자료') || '계산할 지점 없음'}`;
    out.replaceChildren(h('div', { class: 'notice matrix-note' }, note, p.res.unload_site ? ` · 적재가 차면 ${p.res.unload_site.label || '반입지'}에 들러 비움` : ' · 반입지 좌표가 없어 차량 1회 적재 안에서만 계산 (설정에서 지정)', p.res.end_site ? ` · 마지막에 ${p.res.end_site.label || '종료지'}로` : '', p.res.infeasible ? h('strong', {}, ' · 해를 찾지 못했습니다. 차량·근무시간·필수 포함 조건을 조정하세요.') : null),
      h('div', { class: 'kpis compact' }, ...[['배정 팀', `${p.routes.length}팀`, ''], ['방문 지점', `${nStops}곳`, ''], ['미배정', `${p.unassigned.length}건`, p.unassigned.length ? 'warn' : 'ok'],
        ['반입지 경유', `${p.routes.reduce((a, rt) => a + (rt.unloads || 0), 0)}회`, ''], ['마지막 예상 종료', lastEnd || '—', '']].map(([k, v, c]) => h('div', { class: 'kpi ' + c }, h('span', { class: 'k' }, k), h('b', { class: 'v' }, v)))),
      h('div', { class: 'dispatch-grid' }, h('div', { class: 'mapcol' }, h('div', { class: 'card mapcard' }, h('div', { class: 'panel-h' }, h('h2', {}, '지도'), h('span', { class: 'legend small' }, ...p.routes.map((rt, ri) => h('span', { class: `lg route c${(ri % 4) + 1}` }, rt.crew_name)))), mapBox,
        h('p', { class: 'muted small' }, '선은 방문 순서를 잇는 직선이며 실제 도로가 아닙니다. 핀 위치와 이름은 지도 서버로 보내지 않습니다.'))),
        h('div', { class: 'routecol' }, h('div', { class: 'routecol-h' }, h('b', {}, '팀별 방문 순서'), h('span', { class: 'muted small' }, '▲▼로 순서 변경, [이동…]으로 팀 변경')), h('div', { class: 'cols' }, ...cols), un)),
      h('div', { class: 'actionbar' }, h('b', {}, summary), p.adjusted ? adjReason : h('span', { class: 'muted' }, '추천 순서 그대로 확정합니다.'), h('span', { class: 'grow' }), planMsg, confirmBtn));
    setTimeout(async () => {
      try {
        const { map, ml } = await mapIn(mapBox, { center: [boot.settings.depot.lon, boot.settings.depot.lat], zoom: 13 });
        const b = new ml.LngLatBounds(); b.extend([boot.settings.depot.lon, boot.settings.depot.lat]);
        M.pin(ml, map, { lng: boot.settings.depot.lon, lat: boot.settings.depot.lat, label: '출발', cls: 'me' });
        const lines = [];
        p.routes.forEach((rt, ri) => {
          const coords = [[boot.settings.depot.lon, boot.settings.depot.lat]];
          rt.stops.forEach((x, i) => { const q = R[x.request_id]; if (q.lat == null) return; coords.push([q.lon, q.lat]); b.extend([q.lon, q.lat]); M.pin(ml, map, { lng: q.lon, lat: q.lat, label: `${ri + 1}-${i + 1}`, cls: 'c' + ((ri % 4) + 1) }); });
          lines.push({ type: 'Feature', properties: { c: ri % 4 }, geometry: { type: 'LineString', coordinates: coords } });
        });
        p.unassigned.forEach((u) => { const q = R[u.request_id]; if (q && q.lat != null) { b.extend([q.lon, q.lat]); M.pin(ml, map, { lng: q.lon, lat: q.lat, label: '미', cls: 'un' }); } });
        map.fitBounds(b, { padding: 50, maxZoom: 16, duration: 0 });  // 바탕 지도가 늦어도 핀은 모두 보이게
        const whenStyle = (f) => (map.isStyleLoaded() ? f() : map.once('style.load', f));
        whenStyle(() => {
          map.addSource('routes', { type: 'geojson', data: { type: 'FeatureCollection', features: lines } });
          map.addLayer({ id: 'routes', type: 'line', source: 'routes', paint: { 'line-width': 3, 'line-dasharray': [2, 1], 'line-color': ['match', ['get', 'c'], 0, '#2557a7', 1, '#a5560d', 2, '#6c3fb5', '#0e7490'] } });
        });
      } catch { mapBox.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다.')); }
    }, 0);
  };
  draw();
  return h('div', { class: 'dispatch' }, h('h1', {}, '배정·노선'),
    h('div', { class: 'card searchbox', role: 'group', 'aria-label': '조회조건' },
      h('div', { class: 'cond' }, h('label', { class: 'cond-l', for: 'dp-date' }, '수거일'), h('div', { class: 'cond-v' }, date),
        h('span', { class: 'cond-l' }, '수거팀'), h('div', { class: 'cond-v wide' }, crewBox)),
      h('div', { class: 'cond-btns' }, calcBtn),
      crews.length ? null : h('p', { class: 'err' }, '설정에서 수거팀을 먼저 만드세요.'), msg),
    out);
}

// 다른 날로 미루기: 날짜 직접 또는 가장 빠른 가능일(동 수거 요일·한도·휴무일 반영) + 사유 코드
async function postponeDlg(ids, dong, fromDate, label) {
  const d = h('input', { type: 'date' });
  const hint = h('span', { class: 'muted small' });
  const next = new Date(fromDate + 'T00:00:00'); next.setDate(next.getDate() + 1);
  const start = next.toISOString().slice(0, 10) > boot.today ? next.toISOString().slice(0, 10) : boot.today;
  const pick = wrap(async () => {
    const c = await api(`calendar?${new URLSearchParams({ start, days: 1, dong: dong || '' })}`);
    if (!c.earliest) throw new Error('120일 안에 여유 있는 날이 없습니다.');
    d.value = c.earliest; hint.textContent = `${dong || '전체'} 기준 가장 빠른 가능일`;
  });
  pick();
  const r = await reasonDlg('다른 날로 미루기', boot.postpone_codes, { ok: '미루기', code: 'OVER_CAPACITY', label: '미루는 사유',
    lead: [h('p', {}, label)], before: [h('label', { class: 'f' }, '새 수거 예정일', d), h('div', { class: 'row' }, btn('가장 빠른 가능일', pick, 'small'), hint)],
    submit: (v) => { if (!d.value) throw new Error('날짜를 고르세요.'); return post('requests/bulk', { action: 'reschedule', ids, date: d.value, ...v }); } });
  if (r) { (r.warnings || []).forEach((w) => toast(w, true)); toast(`${d.value}로 미뤘습니다`); }
  return r;
}

// ------------------------------------------------------------ 전송
async function screenSend() {
  const s = (screenSend.s ||= { date: boot.today });
  const date = h('input', { type: 'date', value: s.date });
  const body = h('div');
  const load = wrap(async () => {
    s.date = date.value;
    const batches = await api('batches?date=' + s.date);
    body.replaceChildren(...(batches.length ? batches.map((b) => {
      const last = b.bundles[0];
      const code = h('input', { placeholder: '휴대폰 수신 확인 코드 8자리', style: 'width:220px' });
      const msg = h('span', { class: 'msg' });
      return h('div', { class: 'card' + (b.status !== 'ACTIVE' ? ' muted' : '') },
        h('div', { class: 'row', style: 'justify-content:space-between' },
          h('h2', {}, `${b.crew_name} · ${b.stops.length}곳 `, b.assign_ver > 1 ? h('span', { class: 'muted' }, `${b.assign_ver}차 목록 `) : null, b.status === 'ACTIVE' ? chip('현재', 'ok') : chip('이전 목록')),
          h('div', { class: 'row' }, b.status === 'ACTIVE' ? btn('배차표 인쇄', () => window.open('/print/batch/' + b.id.slice(0, 8), 'dg-print'), 'small') : null,
          b.status === 'ACTIVE' ? btn('QR 표시', wrap(async () => {
            const r = await post('bundles', { batch_id: b.id });
            window.open('/bundle/' + r.bundle_id.slice(0, 8), 'dg-qr', 'width=760,height=900');
            toast(`QR ${r.frames}장 (${r.count}건) 표시`); load();
          }, msg), 'primary', { disabled: !b.device_id }) : null)),
        !b.device_id ? h('div', { class: 'notice' }, '이 팀에 휴대폰이 연결되어 있지 않아 QR을 보낼 수 없습니다. ', boot.me.role === 'ADMIN' ? h('a', { href: '#/settings' }, '설정에서 연결하기 →') : '운영 관리자에게 연결을 요청하세요.') : null,
        b.resend_needed && b.status === 'ACTIVE' ? h('div', { class: 'notice' }, '전송 뒤에 수정된 접수가 있습니다. QR을 다시 표시해 새 목록을 전달하세요. 급한 변경은 수거팀에 전화로 먼저 알리세요.') : null,
        h('div', { class: 'stopchain', 'aria-label': '방문 순서' }, ...b.stops.map((x) => h('span', { class: 'node' }, h('b', {}, x.seq), x.name))),
        last ? h('div', { class: 'row' }, last.receipt_confirmed_at ? chip('휴대폰 수신 확인됨', 'ok') : chip('QR 표시함 · 수신 미확인', 'warn'),
          last.receipt_confirmed_at ? null : code, last.receipt_confirmed_at ? null : btn('수신 확인', wrap(async () => { await post(`bundles/${last.id}/receipt`, { code: code.value }); toast('수신이 확인되었습니다'); load(); }, msg)), msg)
          : h('p', { class: 'muted' }, '아직 전송하지 않았습니다.'));
    }) : [h('p', { class: 'muted' }, '이 날짜의 배정이 없습니다. [배정·노선]에서 확정하세요.')]));
  });
  date.addEventListener('change', load); load();
  date.setAttribute('aria-label', '수거일'); date.id = 'send-date';
  return h('div', {}, pagehead('목록 전송 (QR)'),
    h('div', { class: 'card searchbox', role: 'search', 'aria-label': '조회조건' }, h('div', { class: 'cond' }, h('label', { class: 'cond-l', for: 'send-date' }, '수거일'), h('div', { class: 'cond-v' }, date),
      h('span', { class: 'cond-l' }, '안내'), h('div', { class: 'cond-v wide2' }, h('span', { class: 'muted small' }, 'QR 화면 표시와 휴대폰 수신은 다릅니다. 휴대폰이 보여 주는 확인 코드를 입력해야 "수신 확인"이 됩니다.')))), body);
}

// ------------------------------------------------------------ 결과·사진 가져오기, 충돌함
async function screenResults() {
  const rep = h('div');
  const fileIn = (label, path, accept) => {
    const f = h('input', { type: 'file', accept });
    const msg = h('p', { class: 'msg' });
    return h('div', { class: 'card' }, h('h2', {}, label), f, btn('가져오기', wrap(async () => {
      if (!f.files[0]) throw new Error('파일을 골라 주세요.');
      const r = await api(path, { method: 'POST', raw: new Uint8Array(await f.files[0].arrayBuffer()) });
      rep.replaceChildren(h('div', { class: 'card' }, h('h2', {}, r.duplicate_file ? '이미 가져온 파일입니다 (변경 없음)' : '가져오기 결과'),
        h('dl', { class: 'kv' }, ...Object.entries(r).filter(([k]) => !['release_code', 'import_id', 'duplicate_file'].includes(k)).map(([k, v]) => [h('dt', {}, ({ applied: '반영', skipped_duplicate: '중복 건너뜀', conflict: '충돌함으로', photos_expected: '사진 기록', recovered: '사진 회수', already: '이미 회수', hash_mismatch: '해시 불일치', unexpected: '결과보다 먼저 온 사진' })[k] || k), h('dd', {}, v)])),
        r.release_code ? h('div', { class: 'notice' }, path.includes('photos') ? '사진 회수 확인 코드' : '보관 종료 확인 코드', ' — 휴대폰에 입력하게 알려 주세요: ', h('span', { class: 'big' }, r.release_code))
          : path.includes('photos') ? h('p', { class: 'err' }, '해시가 맞지 않는 사진이 있어 확인 코드를 발급하지 않았습니다. 휴대폰에서 사진을 지우지 마세요.') : null));
      f.value = ''; loadConf();
    }, msg), 'primary'), msg);
  };
  const conf = h('div', { class: 'card', id: 'conflicts' });
  const loadConf = wrap(async () => {
    const list = await api('conflicts');
    conf.replaceChildren(h('div', { class: 'panel-h' }, h('h2', {}, `충돌함 ${list.length}건`), h('span', { class: 'muted small' }, '사유를 보고 반영 또는 거부 (거부해도 원 기록은 남음)')),
      list.length ? grid(['접수', '기록 종류', '기록 내용', '휴대폰 시각', '충돌 사유', '처리'], list.map((c) => {
        const why = h('input', { placeholder: '판단 사유', class: 'w-m', 'aria-label': '판단 사유' });
        return [h('span', {}, c.request_id ? h('a', { href: `#/request/${c.request_id}` }, c.receipt_no || c.request_id.slice(0, 8)) : '-', ' ', c.name || '', h('br'), c.request_status ? statusChip(c.request_status) : null),
          EVENT_KIND[c.kind] || c.kind, eventBody(c.kind, c.body_json), kst(c.device_ts), c.conflict_reason,
          h('span', { class: 'row' }, why, btn('반영', wrap(async () => { await post('conflicts/' + c.event_id, { action: 'APPLY', reason: why.value }); toast('반영했습니다'); loadConf(); }), 'small'),
            btn('거부', wrap(async () => { await post('conflicts/' + c.event_id, { action: 'REJECT', reason: why.value }); toast('거부로 기록했습니다'); loadConf(); }), 'small danger'))];
      }), { cls: 'conflicts' }) : h('p', { class: 'empty' }, '확인할 충돌이 없습니다.'));
  });
  loadConf();
  const showReport = (r, isPhoto) => rep.replaceChildren(h('div', { class: 'card' }, h('h2', {}, r.duplicate_file ? '이미 가져온 파일입니다 (변경 없음)' : '가져오기 결과'),
    h('dl', { class: 'kv' }, ...Object.entries(r).filter(([k]) => !['release_code', 'import_id', 'duplicate_file'].includes(k)).map(([k, v]) => [h('dt', {}, ({ applied: '반영', skipped_duplicate: '중복 건너뜀', conflict: '충돌함으로', photos_expected: '사진 기록', recovered: '사진 회수', already: '이미 회수', hash_mismatch: '해시 불일치', unexpected: '결과보다 먼저 온 사진' })[k] || k), h('dd', {}, v)])),
    r.release_code ? h('div', { class: 'notice' }, isPhoto ? '사진 회수 확인 코드' : '보관 종료 확인 코드', ' — 휴대폰에 입력하게 알려 주세요: ', h('span', { class: 'big' }, r.release_code)) : null));
  return h('div', {}, pagehead('결과 가져오기', h('a', { href: '#conflicts', class: 'small', onclick: (e) => { e.preventDefault(); conf.scrollIntoView(); } }, '충돌함으로 ↓')),
    h('div', { class: 'grid2' }, fileIn('결과 파일 (.dgr) — 업무 메일 첨부', 'import/result', '.dgr'), fileIn('사진 파일 (.dgp) — 승인된 보안 매체', 'import/photos', '.dgp')),
    webcamCard((r) => { showReport(r, false); loadConf(); }),
    soloCard(),
    rep, conf, h('p', { class: 'muted' }, '같은 결과를 여러 번 가져와도 한 번만 반영됩니다. 오래된 배정·취소된 접수의 결과는 자동 반영하지 않고 충돌함에 둡니다.'));
}

// 휴대폰 기록 종류·내용을 사람이 읽는 표로 (충돌함)
const EVENT_KIND = { CONTACT: '연락 결과', FIELD_ITEM_ADD: '현장 품목 추가', ITEM_RESULT: '품목 결과', ATTEMPT: '수거 결과', LOCATION_FIX: '위치 보정', FIELD_MEMO: '현장 메모',
  PHOTO_META: '사진', REVISIT_APPT: '재방문 약속', DIAL_INTENT: '전화 걸기', UNLOAD: '하차', ARRIVE: '도착', STICKER_CHECK: '스티커 확인', FEE_NOTICE: '추가 금액 고지', SMS_INTENT: '문자 앱 열기', NAV_INTENT: '길안내 앱 열기' };
const BODY_KEY = { result: '결과', outcome: '결과', line_id: '품목 행', qty_confirmed: '현장 수량', qty_collected: '실은 수량', reason: '사유', name: '품목', spec: '규격', qty: '수량', unit_label: '단위',
  date: '날짜', start: '시작', end: '끝', phase: '단계', text: '메모', worker: '작업자', response: '응답', extra: '추가 금액', total: '현장 금액', received: '접수 금액', template: '문구', app: '앱',
  number: '번호', decision: '판단', acc: '정확도(m)', lat: '위도', lon: '경도', volume_m3: '부피(m³)', weight_kg: '중량(kg)', place: '장소', no_photo_reason: '사진 없는 사유', taken_at: '촬영 시각', source: '출처', line_kind: '구분' };
const BODY_VAL = { ...STATUS, ...CONTACT, ...REASON, ...STICKER, ...NOTICE, ...SMS_T, ...NO_PHOTO, BEFORE: '수거 전', AFTER: '수거 후', RECEIVED: '처음 접수', FIELD: '현장 추가', LEAVE: '미수거', COLLECT: '그래도 수거' };
function eventBody(kind, raw) {
  let b; try { b = JSON.parse(raw || '{}'); } catch { return h('span', { class: 'muted' }, '(읽을 수 없는 내용)'); }
  const skip = new Set(['photo_id', 'sha256', 'bytes', 'lines', 'pii_purged']);
  const ent = Object.entries(b || {}).filter(([k, v]) => !skip.has(k) && v !== '' && v != null);
  if (!ent.length) return h('span', { class: 'muted' }, '-');
  return h('dl', { class: 'kv mini' }, ...ent.slice(0, 8).flatMap(([k, v]) => [h('dt', {}, BODY_KEY[k] || k),
    h('dd', {}, k === 'line_id' ? String(v).slice(0, 8) : typeof v === 'object' ? JSON.stringify(v).slice(0, 60) : (BODY_VAL[v] || String(v)).slice(0, 80))]));
}

// 휴대폰 단독 방식 백업(.dgb) → PC 원본으로 옮기기
function soloCard() {
  const f = h('input', { type: 'file', accept: '.dgb' });
  const pw = h('input', { type: 'password', placeholder: '휴대폰 백업 비밀번호', autocomplete: 'off', style: 'width:200px' });
  const crew = h('select', {}, h('option', { value: '' }, '(하차 기록 붙일 팀 없음)'), ...boot.crews.map((c) => h('option', { value: c.id }, c.name)));
  const out = h('div'), msg = h('p', { class: 'msg' });
  const send = async (preview) => {
    const file = f.files[0]; if (!file) throw new Error('파일을 고르세요.');
    const raw = new Uint8Array(await file.arrayBuffer());
    const pass = btoa(String.fromCharCode(...new TextEncoder().encode(pw.value)));
    const r = await fetch(`/api/import/solo?preview=${preview ? 1 : 0}&crew=${crew.value}`, { method: 'POST', credentials: 'same-origin', body: raw,
      headers: { 'X-DG-CSRF': '1', 'X-DG-Pass': pass, 'Content-Type': 'application/octet-stream' } });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || '가져오기 실패');
    return d;
  };
  const prev = wrap(async () => {
    const d = await send(true);
    out.replaceChildren(h('div', { class: 'notice' },
      h('p', {}, `백업 ${d.created_at ? kst(d.created_at) : ''} · 접수 ${d.requests}건 중 새 접수 ${d.new}건 (이미 PC에 있음 ${d.already}건) · 현장 기록 ${d.events}건 · 사진 ${d.photos}장 · 하차 ${d.unloads}회`),
      h('p', { class: 'small' }, '이미 PC에 있는 접수와 그 기록은 덮어쓰지 않습니다. 접수번호가 PC와 겹치면 끝에 S를 붙입니다.'),
      btn('PC로 옮기기', wrap(async () => { const r = await send(false); toast(r.duplicate_file ? '이미 가져온 파일입니다' : `접수 ${r.requests}건 · 기록 ${r.events}건 · 사진 ${r.photos_recovered}장 · 하차 ${r.unloads_applied}회 옮김`); out.replaceChildren(); }, msg), 'primary')));
  }, msg);
  return h('div', { class: 'card' }, h('h2', {}, '휴대폰 단독 방식 자료 옮기기 (.dgb)'),
    h('p', { class: 'muted' }, '휴대폰만으로 운영하던 접수·현장 기록·사진을 PC 원본으로 옮깁니다. 휴대폰 [백업·복원]에서 만든 암호화 백업 파일을 승인된 보안 매체로 가져와 넣으세요.'),
    h('div', { class: 'row' }, f, pw, crew, btn('미리보기', prev, 'small')), out, msg);
}

// 역방향 QR: 휴대폰 화면의 결과 QR을 이 PC 웹캠으로 읽는다 (카메라 사용이 허용된 PC에서만)
let jsqrReady = null;
function loadJsQR() {
  return (jsqrReady ||= new Promise((res, rej) => { const sc = document.createElement('script'); sc.src = '/web/vendor/jsQR.js'; sc.onload = res; sc.onerror = rej; document.head.append(sc); }));
}
function webcamCard(onDone) {
  const video = h('video', { playsinline: true, muted: true, style: 'width:100%;max-width:520px;border-radius:10px;background:#000;display:none' });
  const status = h('p', { class: 'muted' }, '휴대폰 [결과 보내기] → [PC 화면 카메라로 보내기]를 누른 뒤 이 PC 웹캠에 비추세요.');
  const msg = h('p', { class: 'msg' });
  let stream = null, stop = false;
  const end = () => { stop = true; if (stream) stream.getTracks().forEach((t) => t.stop()); video.style.display = 'none'; startBtn.disabled = false; };
  const startBtn = btn('웹캠으로 읽기', wrap(async () => {
    await loadJsQR();
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
    stop = false; startBtn.disabled = true; video.style.display = ''; video.srcObject = stream; await video.play();
    const col = new Collector(); let current = null;
    const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d', { willReadFrequently: true });
    const loop = async () => {
      if (stop || !video.isConnected) return end();
      if (video.videoWidth) {
        canvas.width = video.videoWidth; canvas.height = video.videoHeight; ctx.drawImage(video, 0, 0);
        const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const code = window.jsQR(img.data, img.width, img.height, { inversionAttempts: 'dontInvert' });
        if (code && code.data) {
          try {
            const r = col.add(code.data);
            current ||= r.key;
            if (r.key === current) {
              status.textContent = `조각 ${r.got} / ${r.total} 받음` + (col.missing(current).length <= 10 ? ` · 남은 번호 ${col.missing(current).map((x) => x + 1).join(', ')}` : '');
              if (col.complete(current)) {
                end();
                const res = await api('import/result?channel=QR', { method: 'POST', raw: col.assemble(current) });
                status.textContent = '결과를 받았습니다.'; onDone(res); return;
              }
            }
          } catch (e) { if (e.message && !e.code) { msg.textContent = e.message; msg.className = 'err'; return end(); } }
        }
      }
      setTimeout(loop, 80);
    };
    loop();
  }, msg), 'primary');
  return h('div', { class: 'card' }, h('h2', {}, '휴대폰 화면 QR로 받기 (웹캠)'), status, h('div', { class: 'row' }, startBtn, btn('멈추기', end)), video, msg,
    h('p', { class: 'muted' }, 'QR 조각을 모두 받고 서명·암호 검증이 끝나야 반영됩니다. 카메라 영상은 저장하지 않습니다.'));
}

// ------------------------------------------------------------ 마감
async function screenClose() {
  const s = (screenClose.s ||= { date: boot.today });
  const date = h('input', { type: 'date', value: s.date });
  const body = h('div');
  let lastWant = null, seq = 0;
  const load = wrap(async (force) => {
    const want = date.value;
    if (force !== true && want === lastWant) return;  // 같은 날짜로 두 번 불러와 입력 중인 칸을 덮지 않게
    lastWant = want; const my = ++seq;
    s.date = want;
    const d = await api('close?date=' + s.date);
    if (my !== seq) return;
    const note = h('input', { placeholder: '마감 메모', style: 'flex:1' });
    const role = boot.me.role;
    const last = d.closed;
    const list = (title, arr, fmt) => h('div', { class: 'card' }, h('h2', {}, `${title} ${arr.length}건`), arr.length ? h('ul', { class: 'linklist' }, ...arr.map((x) => h('li', {}, x.id ? h('a', { href: `#/request/${x.id}` }, fmt(x)) : fmt(x)))) : h('p', { class: 'empty' }, '없음'));
    body.replaceChildren(...[
      h('div', { class: 'card' }, h('h2', {}, `${s.date} 접수 ${d.total}건`), h('div', { class: 'row' }, ...Object.entries(d.by_status).map(([k, v]) => { const el = statusChip(k); el.textContent = `${STATUS[k] || k} ${v}`; return el; })),
        h('p', {}, `충돌 미처리 ${d.conflicts}건 · 사진 미회수 ${d.photos_missing}장 · 수신 미확인 전송 ${d.unconfirmed_bundles}건`),
        d.working ? h('p', { class: 'notice warn small' }, `${s.date}은(는) ${d.working}입니다.`) : null,
        last ? h('p', {}, chip('마감 기록됨', 'ok'), chip(last.status === 'APPROVED' ? `팀장 확인 (${last.approved_by})` : '팀장 확인 전', last.status === 'APPROVED' ? 'ok' : 'warn'),
          ` ${last.seq}회차 · ${kst(last.closed_at)} · ${last.closed_by || '-'} ${last.note || ''}`) : null),
      // 마감 이월 대상: 마감 기록 때 예정일을 다음 업무일로 옮긴다
      h('div', { class: 'card' }, h('h2', {}, `마감 이월 대상 ${d.carry.length}건`),
        d.carry.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['접수', '동', '상태', '까닭', '옮길 날'].map((t) => h('th', {}, t)))),
          h('tbody', {}, ...d.carry.map((x) => h('tr', {}, h('td', {}, h('a', { href: `#/request/${x.id}` }, `${x.receipt_no} ${x.name}`)), h('td', {}, x.dong || '-'), h('td', {}, statusChip(x.status)), h('td', {}, x.kind), h('td', { class: 'code' }, x.to)))))
          : h('p', { class: 'empty' }, '이월할 접수가 없습니다.'),
        h('p', { class: 'muted small' }, '미배정·결과 없음·재방문 약속이 없는 일부 수거·미수거·보류 건입니다. 마감을 기록하면 예정일이 다음 업무일(휴무일·쉬는 요일·동 수거 요일 반영)로 옮겨지고, 배정은 풀리며, 수정 이력에 "마감 이월"이 남습니다.')),
      h('div', { class: 'grid2' },
        list('결과 미회수 (배정했으나 결과 없음)', d.no_result, (x) => `${x.receipt_no} ${x.name}`),
        list('재방문 후보 (일부·미수거·보류)', d.revisit, (x) => `${x.receipt_no} ${x.name} · ${STATUS[x.status]} · 남은 ${x.remaining}${x.next_date ? ` · 재방문 약속 ${x.next_date}${x.appt ? ' ' + x.appt[0] + (x.appt[1] ? '~' + x.appt[1] : '') : ''}` : ''}`),
        list('금액 확인 대기', d.pending_fee, (x) => `${x.receipt_no} ${x.name} · ${x.count}건`),
        list('수납 확인 필요 (미납·부족·과납·금액 미확정)', d.payments, (x) => `${x.receipt_no} ${x.name} · ${PAY[x.status]} · 부과 ${x.levied == null ? '미확정' : won(x.levied)} / 납부 ${won(x.paid)}`),
        list('스티커·신고필증 없음·번호 불일치', d.sticker_issues || [], (x) => `${x.receipt_no} ${x.name} · ${x.kinds.join('·')} ${x.count}건${x.left ? ` · 미수거 ${x.left}` : ''}`),
        list('추가 금액 고지 거부', d.fee_refused || [], (x) => `${x.receipt_no} ${x.name} · 추가 ${won(x.extra || 0)} 거부 (해당 품목 미수거)`),
        list('미수거 부재 증거 없음 (사진·사유 없음)', d.no_evidence || [], (x) => `${x.receipt_no} ${x.name}`)),
      d.post_close_edits.length ? h('div', { class: 'card' }, h('h2', {}, `마감 후 수정 ${d.post_close_edits.length}건`),
        h('table', {}, h('thead', {}, h('tr', {}, ...['시각', '작업자', '대상', '작업', '사유'].map((t) => h('th', {}, t)))),
          h('tbody', {}, ...d.post_close_edits.map((x) => h('tr', {}, h('td', { class: 'nowrap' }, kst(x.at)), h('td', {}, x.actor || ''), h('td', {}, x.request_id ? h('a', { href: `#/request/${x.request_id}` }, x.receipt_no || '접수') : ''),
            h('td', {}, `${ENTITY[x.entity] || x.entity} ${ACTION[x.action] || x.action}`), h('td', {}, x.reason)))))) : null,
      unloadCard(d),
      d.closes.length ? h('div', { class: 'card' }, h('h2', {}, '마감 기록 회차'), h('table', {}, h('thead', {}, h('tr', {}, ...['회차', '기록', '기록자', '이월', '메모', '팀장 확인'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...d.closes.map((c) => h('tr', {}, h('td', {}, `${c.seq}회차`), h('td', { class: 'nowrap' }, kst(c.closed_at)), h('td', {}, c.closed_by || '-'), h('td', {}, `${c.carried}건`), h('td', {}, c.note || ''),
          h('td', {}, c.status === 'APPROVED' ? `${c.approved_by} · ${kst(c.approved_at)}${c.approve_note ? ' · ' + c.approve_note : ''}` : chip('확인 전', 'warn'))))))) : null,
      h('div', { class: 'card row' }, ['ADMIN', 'INTAKE'].includes(role) ? [note, btn(last ? '마감 다시 기록' : '마감 기록', wrap(async () => {
        if (!(await confirmDlg(last ? '마감 다시 기록' : '마감 기록', `${s.date} 마감을 기록합니다.` + (d.carry.length ? ` 미처리 ${d.carry.length}건의 예정일이 다음 업무일로 옮겨집니다.` : '') + ' 기록은 회차로 쌓이며 팀장 확인을 받아야 합니다.', { ok: '마감 기록' }))) return;
        const r = await post('close', { date: s.date, note: note.value }); toast(`마감을 기록했습니다 (${r.seq}회차${r.carried.length ? ` · 이월 ${r.carried.length}건` : ''})`); load(true);
      }), 'primary')] : null,
        ['ADMIN', 'LEAD'].includes(role) && last && last.status === 'RECORDED' ? btn('팀장 확인', wrap(async () => {
          const why = await modal({ title: `${s.date} 마감 확인 (${last.seq}회차)`, ok: '확인 기록', body: [h('p', {}, '담당자가 기록한 마감 내용(이월·수납 확인·마감 후 수정)을 검토했으면 확인을 기록합니다. 기록한 사람은 확인할 수 없습니다.'), h('label', { class: 'f' }, '메모', h('input', { id: 'approve-note', class: 'fill' }))],
            onOk: () => post('close/approve', { date: s.date, note: document.getElementById('approve-note').value }) });
          if (why) { toast('마감을 확인했습니다'); load(true); }
        }), 'primary') : null,
        h('span', { class: 'muted' }, '미처리 건이 있어도 마감할 수 있습니다. 위 이월 대상은 마감 기록 때 다음 업무일로 옮겨집니다. 마감한 날의 접수·수납을 고치면 사유와 함께 "마감 후 수정"으로 남습니다.'))].filter(Boolean));
  });
  date.addEventListener('change', load); load();
  return h('div', {}, h('div', { class: 'pagehead' }, h('h1', {}, '일일 마감'), h('label', { class: 'f inline' }, '수거일', date)), body);
}

// 행정동 관리: 이름·사용·기본 담당 팀·수거 요일·일일 한도·주소 판별 낱말 (실제 동 이름을 미리 넣지 않음)
const WD = '월화수목금토일';
function dayChecks(cur, label) {
  const boxes = [...WD].map((t, i) => h('input', { type: 'checkbox', checked: cur.includes(i), 'aria-label': `${label} ${t}요일` }));
  return { el: h('span', { class: 'wdays' }, ...boxes.map((b, i) => h('label', {}, b, WD[i]))), get: () => boxes.map((b, i) => (b.checked ? i : null)).filter((x) => x != null) };
}
function dongCard() {
  const msg = h('p', { class: 'msg' });
  const rowsEl = [...boot.dongs, { id: '', name: '', active: true, crew_id: '', weekdays: [], daily_limit: null, daily_limit_m3: null, keywords: [] }].map((d) => {
    const lbl = d.name || '새 동';
    const f = { name: h('input', { value: d.name, placeholder: '새 동 이름 (예: ○○1동)', 'aria-label': `${lbl} 이름`, class: 'w-s' }),
      code: h('input', { value: d.code || '', placeholder: '코드', 'aria-label': `${lbl} 부과번호 코드`, class: 'w-xs', maxlength: 4 }), active: h('input', { type: 'checkbox', checked: d.active, 'aria-label': `${lbl} 사용` }),
      crew: h('select', { 'aria-label': `${lbl} 기본 담당 팀` }, h('option', { value: '' }, '(없음)'), ...boot.crews.map((c) => h('option', { value: c.id, selected: c.id === d.crew_id }, c.name))),
      days: dayChecks(d.weekdays, lbl), limit: h('input', { type: 'number', min: 1, value: d.daily_limit ?? '', placeholder: '건', 'aria-label': `${lbl} 일일 한도 건수`, class: 'w-xs' }),
      m3: h('input', { type: 'number', step: '0.5', min: 0, value: d.daily_limit_m3 ?? '', placeholder: 'm³', 'aria-label': `${lbl} 일일 한도 m³`, class: 'w-xs' }),
      kw: h('input', { value: (d.keywords || []).join(', '), placeholder: '법정동·도로명 (쉼표로)', 'aria-label': `${lbl} 주소 판별 낱말`, class: 'w-l' }) };
    return h('tr', {}, h('td', {}, f.name), h('td', {}, f.code), h('td', { class: 'c' }, f.active), h('td', {}, f.crew), h('td', {}, f.days.el), h('td', {}, f.limit), h('td', {}, f.m3), h('td', {}, f.kw),
      h('td', { class: 'nowrap' }, btn(d.id ? '저장' : '추가', wrap(async () => {
        await post('dongs', { id: d.id || null, name: f.name.value, code: f.code.value, active: f.active.checked, crew_id: f.crew.value, weekdays: f.days.get(), daily_limit: f.limit.value, daily_limit_m3: f.m3.value, keywords: f.kw.value, sort: d.sort || 0 });
        toast('저장했습니다'); boot = null; render();
      }, msg), 'small primary'), d.id ? btn('삭제', wrap(async () => {
        if (!(await confirmDlg('동 삭제', `${d.name}을(를) 지웁니다. 이 동으로 접수된 건이 있으면 지울 수 없고 '사용'을 끄면 됩니다.`, { ok: '삭제', danger: true }))) return;
        await post(`dongs/${d.id}/delete`); toast('지웠습니다'); boot = null; render();
      }, msg), 'small danger') : null));
  });
  return h('div', { class: 'card' }, h('h2', {}, '행정동 관리'),
    h('p', { class: 'muted' }, '코드는 부과번호(연도-동코드-일련번호)에 쓰는 숫자·영문 1~4자입니다(비우면 00). 접수 화면은 주소에 판별 낱말(법정동 이름·도로명 등)이 있거나 주소 위치 자료의 관할 행정동이 같으면 동을 자동으로 고릅니다. 수거 요일을 비우면 쉬는 요일 말고 매일입니다. 기본 담당 팀은 노선 계산에서 우선(약한 선호)합니다. 바꾼 내용은 변경 이력에 남습니다.'),
    h('div', { class: 'tablewrap' }, h('table', { class: 'dongedit grid' }, h('thead', {}, h('tr', {}, ...['동', '코드', '사용', '기본 담당 팀', '수거 요일', '한도(건)', '한도(m³)', '주소 판별 낱말', ''].map((t) => h('th', {}, t)))), h('tbody', {}, ...rowsEl))), msg);
}
// 날짜별 수거 한도·쉬는 요일·휴무일
function capacityCard() {
  const c = boot.settings.capacity;
  const msg = h('p', { class: 'msg' });
  const lim = h('input', { type: 'number', min: 1, value: c.daily_count ?? '', placeholder: '없음', class: 'w-xs', 'aria-label': '전체 일일 한도 건수' });
  const m3 = h('input', { type: 'number', step: '0.5', min: 0, value: c.daily_m3 ?? '', placeholder: '없음', class: 'w-xs', 'aria-label': '전체 일일 한도 m³' });
  const closed = dayChecks(c.closed_weekdays, '쉬는 요일');
  const pref = h('input', { type: 'checkbox', checked: c.prefer_dong_crew });
  return h('div', { class: 'card' }, h('h2', {}, '수거 한도'),
    h('div', { class: 'row' }, '전체 일일 한도', lim, '건 ·', m3, 'm³', h('span', { class: 'muted small' }, '비우면 한도 없음. 넘어도 막지 않고 접수 화면에서 경고합니다. 동별 한도는 [행정동] 탭에서 정합니다.')),
    h('div', { class: 'row' }, '쉬는 요일', closed.el),
    h('label', { class: 'row' }, pref, '노선 계산에서 동 기본 담당 팀 우선 (약한 선호, 끄면 팀 구분 없이 계산)'),
    btn('저장', wrap(async () => { await post('settings', { capacity: { daily_count: lim.value, daily_m3: m3.value, closed_weekdays: closed.get(), prefer_dong_crew: pref.checked } }); toast('저장했습니다'); boot = null; render(); }, msg), 'primary'), msg);
}
function holidayCard() {
  const msg = h('p', { class: 'msg' });
  const list = h('tbody');
  const hd = h('input', { type: 'date', 'aria-label': '휴무일' }), hl = h('input', { placeholder: '이름 (예: 개교기념일, 시설 점검)', class: 'w-m', 'aria-label': '휴무일 이름' });
  const load = wrap(async () => {
    const hs = await api(`holidays?start=${boot.today.slice(0, 4)}-01-01`);
    list.replaceChildren(...(hs.length ? hs.map((x) => h('tr', {}, h('td', { class: 'code' }, x.day), h('td', {}, x.label), h('td', {}, btn('지우기', wrap(async () => { await post(`holidays/${x.day}/delete`); load(); }, msg), 'small'))))
      : [h('tr', {}, h('td', { colspan: 3, class: 'muted' }, '등록한 휴무일이 없습니다. 공휴일은 미리 들어 있지 않으니 담당자가 넣으세요.'))]));
  }, msg);
  load();
  return h('div', { class: 'card' }, h('h2', {}, '휴무일·공휴일'), h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['날짜', '이름', ''].map((t) => h('th', {}, t)))), list)),
    h('div', { class: 'row' }, hd, hl, btn('휴무일 추가', wrap(async () => { await post('holidays', { day: hd.value, label: hl.value }); hd.value = ''; hl.value = ''; toast('추가했습니다'); load(); }, msg), 'small primary')), msg);
}
// 접수 시 안내: 품목 낱말이 맞으면 접수 화면에 안내문 (예: 폐가전 무상수거)
function noticeCard() {
  const msg = h('p', { class: 'msg' });
  const rowsF = [...(boot.settings.intake_notices || []), { keywords: [], text: '' }].map((n) => ({ kw: h('input', { value: n.keywords.join(', '), placeholder: '품목 낱말 (예: 냉장고, 세탁기)', class: 'w-l', 'aria-label': '안내 품목 낱말' }),
    text: h('input', { value: n.text, placeholder: '안내문 (비우면 지움)', class: 'fill', 'aria-label': '안내문' }) }));
  return h('div', { class: 'card' }, h('h2', {}, '접수 시 안내'),
    h('p', { class: 'muted' }, '접수 화면에서 고른 품목 이름에 낱말이 들어 있으면 안내문을 보여 줍니다. 예: "냉장고, 세탁기, 에어컨" → "폐가전 무상 방문수거 서비스를 먼저 안내하세요."'),
    ...rowsF.map((f) => h('div', { class: 'row' }, f.kw, f.text)),
    btn('저장', wrap(async () => { await post('settings', { intake_notices: rowsF.map((f) => ({ keywords: f.kw.value, text: f.text.value })) }); toast('저장했습니다'); boot = null; render(); }, msg), 'primary'), msg);
}

// 적재량 보정: 품목별 추정 부피와 담당자 보정값을 따로 관리 (노선 계산의 적재 제약에 사용)
function volumeCard() {
  const tbody = h('tbody');
  const msg = h('span', { class: 'msg' });
  api('volumes').then((list) => tbody.replaceChildren(...(list.length ? list.map((v) => {
    const inp = h('input', { type: 'number', step: '0.1', min: 0, value: v.override ?? '', placeholder: '보정값', style: 'width:90px' });
    return h('tr', {}, h('td', {}, `${v.name} ${v.spec}`), h('td', {}, `${v.estimate} m³/${v.unit_label}`), h('td', {}, inp, ' m³'),
      h('td', {}, btn('저장', wrap(async () => { await post('volumes', { fee_rate_id: v.fee_rate_id, m3: inp.value === '' ? null : +inp.value }); toast(inp.value === '' ? '보정값을 지웠습니다(추정치 사용)' : '보정값을 저장했습니다'); }, msg), 'small')));
  }) : [h('tr', {}, h('td', { colspan: 4, class: 'muted' }, '접수에 쓰인 품목이 생기면 여기에 나타납니다.'))]))).catch(() => {});
  return h('div', { class: 'card' }, h('h2', {}, '적재량 보정'), h('p', { class: 'muted' }, '자주 쓰는 품목의 1단위 부피입니다. 추정치는 그대로 두고, 실측한 값이 있으면 보정값에 적으세요(비우면 추정치 사용).'),
    h('table', {}, h('thead', {}, h('tr', {}, ...['품목', '추정치', '보정값', ''].map((t) => h('th', {}, t)))), tbody), msg);
}

// 하차 기록: 반입지에서 내린 양(부피·중량)을 팀별로 기록
function unloadCard(d) {
  const msg = h('span', { class: 'msg' });
  const crew = h('select', {}, ...boot.crews.map((c) => h('option', { value: c.id }, c.name)));
  const at = h('input', { type: 'time' }), place = h('input', { placeholder: '반입지' });
  const vol = h('input', { type: 'number', step: '0.1', placeholder: 'm³', style: 'width:90px' }), kg = h('input', { type: 'number', placeholder: 'kg', style: 'width:100px' });
  const note = h('input', { placeholder: '메모', style: 'flex:1' });
  return h('div', { class: 'card' }, h('h2', {}, `하차 기록 · 합계 ${d.unload_total.volume_m3}m³ · ${d.unload_total.weight_kg}kg`),
    d.unloads.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['팀', '시각', '반입지', '부피', '중량', '메모'].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...d.unloads.map((u) => h('tr', {}, h('td', {}, u.crew_name), h('td', {}, u.at_time || ''), h('td', {}, u.place), h('td', {}, u.volume_m3 ?? ''), h('td', {}, u.weight_kg ?? ''), h('td', {}, u.note || ''))))) : h('p', { class: 'muted' }, '기록 없음'),
    boot.crews.length ? h('div', { class: 'row' }, crew, at, place, vol, kg, note, btn('하차 기록', wrap(async () => {
      await post('unloads', { crew_id: crew.value, work_date: d.work_date, at_time: at.value, place: place.value, volume_m3: vol.value, weight_kg: kg.value, note: note.value });
      toast('하차를 기록했습니다'); render();
    }, msg), 'primary'), msg) : null);
}

// ------------------------------------------------------------ QR 실측 (가상 자료)
async function screenQrTest() {
  const devs = boot.devices.filter((d) => d.status === 'ACTIVE');
  const sel = (opts, def) => h('select', {}, ...opts.map(([v, t]) => h('option', { value: v, selected: v === def }, t)));
  const F = { device: sel(devs.map((d) => [d.id, `${d.id.slice(0, 8)} ${d.label || ''}`]), devs[0] && devs[0].id), count: sel([['20', '20건'], ['50', '50건'], ['100', '100건']], '20'),
    addr: sel([['mixed', '섞음'], ['short', '짧은 주소'], ['medium', '보통(동·호)'], ['long', '긴 주소(설명 포함)']], 'mixed'),
    chunk: sel([['400', '400B'], ['600', '600B'], ['800', '800B'], ['1200', '1,200B']], '800'), ec: sel([['L', 'L (용량 큼)'], ['M', 'M'], ['Q', 'Q (복원력 큼)']], 'M'),
    fps: sel([['2', '초당 2장'], ['3', '초당 3장'], ['4', '초당 4장'], ['6', '초당 6장']], '3') };
  const msg = h('p', { class: 'msg' });
  const tbody = h('tbody');
  const load = wrap(async () => {
    const list = await api('qrtests');
    tbody.replaceChildren(...list.map((t) => {
      const el = h('input', { type: 'number', step: '0.1', placeholder: '초', style: 'width:80px', value: t.elapsed_s ?? '' });
      const rd = h('input', { type: 'number', placeholder: '인식 횟수', style: 'width:90px', value: t.reads ?? '' });
      const ck = h('input', { placeholder: '확인 4자리', style: 'width:100px' });
      return h('tr', {}, h('td', {}, t.created_at.replace('T', ' ').slice(5, 16)), h('td', {}, `${t.params.count}건 · ${t.params.addr}`), h('td', {}, `${t.params.chunk}B · ${t.params.ec} · ${t.params.fps}장/초`),
        h('td', {}, `${t.bytes.toLocaleString()}B · ${t.frames}장`), h('td', {}, t.verified == null ? '' : t.verified ? chip(`${t.elapsed_s}초 · 인식 ${t.reads}회 · 복원 일치`, 'ok') : chip('확인 코드 불일치', 'bad')),
        h('td', {}, btn('QR 다시 표시', () => window.open('/qrtest/' + t.id.slice(0, 8), 'dg-qrtest', 'width=760,height=900'), 'small'), ' ', el, rd, ck,
          btn('기록', wrap(async () => { const r = await post(`qrtest/${t.id}/result`, { elapsed_s: el.value, reads: rd.value, check: ck.value }); toast(r.verified ? '기록했습니다 (복원 일치)' : '확인 코드가 맞지 않습니다', !r.verified); load(); }), 'small primary')));
    }));
  });
  load();
  const run = wrap(async () => {
    const r = await post('qrtest', { device_id: F.device.value, count: +F.count.value, addr: F.addr.value, chunk: +F.chunk.value, ec: F.ec.value, fps: +F.fps.value });
    window.open('/qrtest/' + r.test_id.slice(0, 8), 'dg-qrtest', 'width=760,height=900');
    toast(`실측 QR ${r.frames}장 (${r.bytes.toLocaleString()}B) 표시`); load();
  }, msg);
  const lab = (t, el) => h('label', { class: 'f' }, t, el);
  return h('div', {}, pagehead('QR 실측'),
    h('div', { class: 'card' }, h('p', { class: 'muted' }, '기관 휴대폰으로 QR 전송 시간과 인식률을 잽니다. 가상 자료만 쓰며 업무 자료에 섞이지 않습니다. 휴대폰은 [오늘 목록 받기]로 읽으면 "실측 모드"로 결과만 보여 주고 저장하지 않습니다.'),
      devs.length ? h('div', { class: 'row' }, lab('휴대폰', F.device), lab('건수', F.count), lab('주소 길이', F.addr), lab('조각 크기', F.chunk), lab('오류 복원', F.ec), lab('표시 속도', F.fps), btn('실측 QR 표시', run, 'primary'))
        : h('p', { class: 'err' }, '설정에서 휴대폰을 먼저 등록하세요.'), msg),
    h('div', { class: 'card tablecard' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['시각', '자료', '조건', '크기', '결과', '휴대폰 결과 입력'].map((x) => h('th', {}, x)))), tbody)),
    h('p', { class: 'muted' }, '휴대폰에 나온 시간·인식 횟수·확인 4자리를 입력하세요. 확인 4자리가 맞아야 전체 복원이 정확했다는 뜻입니다. 같은 조건을 10회 반복해 평균을 비교하세요.'));
}

// ------------------------------------------------------------ 설정 (탭: 기본·수거팀·기기·행정동·한도·휴무일·수수료/감면·주소 자료·백업·개인정보)
const SETTING_TABS = [['basic', '기본'], ['crews', '수거팀'], ['devices', '기기'], ['dongs', '행정동'], ['capacity', '한도'], ['holidays', '휴무일'], ['fees', '수수료·감면'], ['addr', '주소·외부 서비스'], ['backup', '백업'], ['privacy', '개인정보']];
async function screenSettings(tab) {
  tab = SETTING_TABS.some(([k]) => k === tab) ? tab : 'basic';
  const tabs = h('nav', { class: 'tabs', 'aria-label': '설정 묶음' }, ...SETTING_TABS.map(([k, t]) => h('a', { href: '#/settings/' + k, class: k === tab ? 'on' : '', 'aria-current': k === tab ? 'page' : null }, t)));
  const body = await ({ basic: settingsBasic, crews: settingsCrews, devices: settingsDevices, dongs: () => [dongCard()], capacity: () => [capacityCard()], holidays: () => [holidayCard()],
    fees: settingsFees, addr: () => [addrCard(), externalCard()], backup: () => [autoBackupCard(), backupCard()], privacy: privacyCard }[tab])();
  return h('div', {}, pagehead('설정'), tabs, ...[].concat(body));
}
function settingsDevices() {
  const devMsg = h('p', { class: 'msg' }), regMsg = h('p', { class: 'msg' });
  const regImg = h('img', { alt: '기기 등록 QR', style: 'width:260px;image-rendering:pixelated' });
  api('registration-qr').then((r) => { regImg.src = r.image; }).catch(() => {});
  const code = h('textarea', { placeholder: '휴대폰 화면의 기기 코드 (DGD1.…)', rows: 3, style: 'width:100%' });
  const label = h('input', { placeholder: '기기 이름 (예: 1팀 업무폰)' });
  const fp = h('p', { class: 'big' });
  const devs = grid(['기기', '이름', '상태', '등록', ''], boot.devices.map((d) => [d.id.slice(0, 8), d.label || '', d.status === 'ACTIVE' ? chip('사용', 'ok') : chip(d.status === 'LOST' ? '분실' : '중지', 'bad'),
    d.registered_at.slice(0, 10), h('span', { class: 'row' }, ...[['ACTIVE', '사용'], ['LOST', '분실 처리'], ['RETIRED', '사용 중지']].filter(([k]) => k !== d.status).map(([k, t]) =>
      btn(t, wrap(async () => { if (k !== 'ACTIVE' && !(await confirmDlg(`휴대폰 ${t}`, `${d.id.slice(0, 8)} 기기를 '${t}'합니다. 이 기기로는 목록을 보낼 수 없고 결과도 받지 않습니다. 계속할까요?`, { ok: t, danger: true }))) return; await post(`devices/${d.id}/status`, { status: k }); boot = null; render(); }, devMsg), 'small')))]), { empty: '등록된 휴대폰이 없습니다.' });
  return [h('div', { class: 'grid2' },
    h('div', { class: 'card' }, h('h2', {}, '휴대폰 등록'), h('p', { class: 'muted' }, '1) 휴대폰 앱 [기기 등록]으로 아래 QR을 비춥니다 (공개키만 들어 있음). 2) 휴대폰의 기기 코드를 붙여 넣습니다. 3) 양쪽 6자리 지문이 같은지 확인합니다.'),
      regImg, code, label, btn('기기 등록', wrap(async () => { const r = await post('devices/register', { code: code.value, label: label.value }); fp.textContent = `지문 ${r.fingerprint.slice(0, 3)} ${r.fingerprint.slice(3)}`; toast('등록했습니다. 지문을 휴대폰과 대조하세요.'); boot = null; setTimeout(() => { if (location.hash.startsWith('#/settings')) render(); }, 2500); }, regMsg), 'primary'), fp, regMsg),
    h('div', { class: 'card' }, h('h2', {}, '등록된 휴대폰'), devs, h('p', { class: 'muted' }, '분실 시 바로 "분실 처리"하세요. 오프라인 휴대폰의 자료를 원격으로 지울 수는 없지만, 이후 그 기기로는 목록을 보내지 않고 결과도 받지 않습니다.'), devMsg))];
}
function settingsCrews() {
  const crewMsg = h('p', { class: 'msg' });
  const crewRows = [...boot.crews, { id: '', name: '', device_id: '', capacity_m3: 8, shift_start: '09:00', shift_end: '17:00', active: 1 }].map((c) => {
    const f = { name: h('input', { value: c.name, placeholder: '새 팀 이름', 'aria-label': c.name ? `${c.name} 이름` : '새 팀 이름' }), device_id: h('select', { 'aria-label': '연결 휴대폰' }, h('option', { value: '' }, '(없음)'), ...boot.devices.filter((d) => d.status === 'ACTIVE').map((d) => h('option', { value: d.id, selected: d.id === c.device_id }, `${d.id.slice(0, 8)} ${d.label || ''}`))),
      capacity_m3: h('input', { type: 'number', step: '0.5', value: c.capacity_m3, class: 'w-xs', 'aria-label': '적재(m³)' }), shift_start: h('input', { type: 'time', value: c.shift_start, 'aria-label': '근무 시작' }), shift_end: h('input', { type: 'time', value: c.shift_end, 'aria-label': '근무 끝' }), active: h('input', { type: 'checkbox', checked: c.active, 'aria-label': '사용' }) };
    return h('tr', {}, ...Object.values(f).map((x) => h('td', {}, x)), h('td', {}, btn(c.id ? '저장' : '추가', wrap(async () => {
      await post('crews', { id: c.id || null, name: f.name.value, device_id: f.device_id.value, capacity_m3: f.capacity_m3.value, shift_start: f.shift_start.value, shift_end: f.shift_end.value, active: f.active.checked });
      toast('저장했습니다'); boot = null; render();
    }, crewMsg), 'small primary')));
  });
  return [h('div', { class: 'card' }, h('h2', {}, '수거팀·차량'), h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['팀', '연결 휴대폰', '적재(m³)', '근무 시작', '근무 끝', '사용', ''].map((t) => h('th', {}, t)))), h('tbody', {}, ...crewRows))), crewMsg),
    volumeCard()];
}
function settingsBasic() {
  const routeMsg = h('p', { class: 'msg' }), infoMsg = h('p', { class: 'msg' });
  const st = boot.settings;
  const dlat = h('input', { value: st.depot.lat, class: 'w-s', 'aria-label': '차고지 위도' }), dlon = h('input', { value: st.depot.lon, class: 'w-s', 'aria-label': '차고지 경도' }), dlabel = h('input', { value: st.depot.label, 'aria-label': '차고지 이름' });
  const osrm = h('input', { value: st.osrm_url, placeholder: 'http://127.0.0.1:5000 (이동시간 방식이 로컬 OSRM일 때 사용)', class: 'w-l', 'aria-label': '로컬 OSRM' });
  const sb = h('input', { type: 'number', value: st.service_min_base, class: 'w-xs', 'aria-label': '지점당 기본 작업(분)' }), sp = h('input', { type: 'number', value: st.service_min_per_item, class: 'w-xs', 'aria-label': '품목당 작업(분)' });
  const us = st.unload_site || {}, es = st.end_site || {};
  const jur = h('input', { value: st.jurisdiction || '', placeholder: '예: 동구', class: 'w-s', 'aria-label': '관할 구역' });
  const elat = h('input', { value: es.lat ?? '', placeholder: '위도', class: 'w-s', 'aria-label': '종료지 위도' }), elon = h('input', { value: es.lon ?? '', placeholder: '경도', class: 'w-s', 'aria-label': '종료지 경도' }), elabel = h('input', { value: es.label || '', placeholder: '종료지 이름', 'aria-label': '종료지 이름' });
  const ulat = h('input', { value: us.lat ?? '', placeholder: '위도', class: 'w-s', 'aria-label': '반입지 위도' }), ulon = h('input', { value: us.lon ?? '', placeholder: '경도', class: 'w-s', 'aria-label': '반입지 경도' }), ulabel = h('input', { value: us.label || '', placeholder: '반입지 이름', 'aria-label': '반입지 이름' });
  const umin = h('input', { type: 'number', value: st.unload_min, class: 'w-xs', 'aria-label': '하차 시간(분)' }), umax = h('input', { type: 'number', value: st.max_unloads, min: 0, max: 5, class: 'w-xs', 'aria-label': '차량당 최대 하차 횟수' });
  const ft = (label, ...el) => h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, label), h('div', { class: 'ft-v' }, ...el));
  const contact = h('input', { value: st.contact_info, class: 'fill', 'aria-label': '문의처' });
  const guide = h('textarea', { rows: 2, class: 'fill', 'aria-label': '배출 장소 안내' }, st.disposal_guide);
  const title = h('input', { value: st.monthly_report_title, class: 'fill', 'aria-label': '월보 제목' });
  return [h('div', { class: 'card' }, h('h2', {}, '노선 계산'), h('div', { class: 'ftable' },
      ft('차고지', dlat, dlon, dlabel), ft('로컬 OSRM', osrm), ft('작업 시간', '지점당 기본', sb, '분 + 품목당', sp, '분 (추정치)'),
      ft('반입지', ulat, ulon, ulabel, '하차', umin, '분 · 차량당 최대', umax, '회'), ft('종료지', elat, elon, elabel, h('span', { class: 'muted small' }, '비우면 차고지로 돌아옴')),
      ft('관할 구역', jur, h('span', { class: 'muted small' }, '주소의 구 이름이 다르면 접수할 때 경고 (막지는 않음)'))),
    h('p', { class: 'small muted' }, '반입지 좌표를 넣으면 적재가 찰 때 반입지에 들러 비우고 이어서 수거하는 순서를 추천합니다. 비우면 차량 1회 적재 안에서만 계산합니다.'),
    h('div', { class: 'row' }, btn('저장', wrap(async () => { await post('settings', { depot: { lat: dlat.value, lon: dlon.value, label: dlabel.value }, osrm_url: osrm.value, service_min_base: sb.value, service_min_per_item: sp.value,
      unload_site: { lat: ulat.value, lon: ulon.value, label: ulabel.value }, end_site: { lat: elat.value, lon: elon.value, label: elabel.value }, jurisdiction: jur.value, unload_min: umin.value, max_unloads: umax.value }); toast('저장했습니다'); boot = null; render(); }, routeMsg), 'primary')), routeMsg),
    h('div', { class: 'card' }, h('h2', {}, '접수증·월보 문구'), h('div', { class: 'ftable' }, ft('문의처', contact), ft('배출 장소 안내', guide), ft('월보 제목', title)),
      h('p', { class: 'small muted' }, '문의처·배출 장소 안내는 접수증(신청인 보관용)에, 월보 제목은 통계 → [월보 출력]에 쓰입니다.'),
      h('div', { class: 'row' }, btn('저장', wrap(async () => { await post('settings', { contact_info: contact.value, disposal_guide: guide.value, monthly_report_title: title.value }); toast('저장했습니다'); boot = null; render(); }, infoMsg), 'primary')), infoMsg),
    noticeCard()];
}
function settingsFees() {
  const st = boot.settings;
  const msg = h('p', { class: 'msg' });
  const rowsF = [...(st.reductions || []), { code: '', label: '', rate: '', note: '조례 확인 필요', active: true }].map((x) => ({
    code: h('input', { value: x.code, placeholder: '코드(영문)', class: 'w-s', 'aria-label': `${x.label || '새 감면'} 코드` }), label: h('input', { value: x.label, placeholder: '새 감면 사유', class: 'w-m', 'aria-label': `${x.label || '새 감면'} 사유` }),
    rate: h('input', { type: 'number', min: 1, max: 100, value: x.rate, class: 'w-xs', 'aria-label': `${x.label || '새 감면'} 감면율` }), note: h('input', { value: x.note || '', class: 'w-m', 'aria-label': `${x.label || '새 감면'} 비고` }),
    active: h('input', { type: 'checkbox', checked: x.active !== false, 'aria-label': `${x.label || '새 감면'} 사용` }) }));
  return [h('div', { class: 'card' }, h('h2', {}, '수수료표'), h('dl', { class: 'kv' }, h('dt', {}, '적용 표'), h('dd', {}, st.fee_label), h('dt', {}, '버전'), h('dd', {}, st.fee_version),
      h('dt', {}, '자료 확인일'), h('dd', {}, st.fee_checked_on), h('dt', {}, '시행일'), h('dd', {}, st.fee_effective_from || '미확인 (담당자 확인 필요)')),
      h('a', { href: '#/fees' }, '수수료표 관리 화면에서 시행일·확인자·확인 필요 행 정하기')),
    h('div', { class: 'card' }, h('h2', {}, '수수료 감면 사유 ', chip('조례 확인 필요', 'warn')),
      h('p', { class: 'muted' }, '아래 기본값(기초생활수급자 100%, 차상위 50%, 장애인 50%, 국가유공자 50%)은 예시입니다. 동구 조례·규칙의 감면 대상과 감면율을 확인해 고치세요. 이미 쓰인 사유는 지우지 말고 "사용"을 끄세요. 감면액은 원 미만을 버립니다.'),
      grid(['코드', '감면 사유', '감면율(%)', '비고', '사용'], rowsF.map((f) => [f.code, f.label, f.rate, f.note, f.active])),
      h('div', { class: 'row' }, btn('감면 사유 저장', wrap(async () => { await post('settings', { reductions: rowsF.map((f) => ({ code: f.code.value, label: f.label.value, rate: f.rate.value, note: f.note.value, active: f.active.checked })).filter((x) => x.label.trim()) }); toast('저장했습니다'); boot = null; render(); }, msg), 'primary')), msg)];
}

// 자동 백업: 백업 공개키(설정에서 생성, 개인키는 한 번만 내려받아 오프라인 보관) · 시각 · 두 번째 위치 · 보관 세대
function autoBackupCard() {
  const st = boot.settings;
  const msg = h('p', { class: 'msg' }), stat = h('div');
  let hasKey = false;
  const auto = h('input', { type: 'checkbox', checked: st.backup_auto, 'aria-label': '자동 백업 사용' });
  const time = h('input', { type: 'time', value: st.backup_time, 'aria-label': '자동 백업 시각' });
  const dest2 = h('input', { value: st.backup_dest2, placeholder: '예: \\\\파일서버\\동구백업 또는 E:\\동구백업 (비우면 안 함)', class: 'w-l', 'aria-label': '두 번째 보관 위치' });
  const kd = h('input', { type: 'number', min: 1, max: 365, value: st.backup_keep_daily, class: 'w-xs', 'aria-label': '일별 보관 개수' });
  const km = h('input', { type: 'number', min: 0, max: 120, value: st.backup_keep_monthly, class: 'w-xs', 'aria-label': '월별 보관 개수' });
  const rd = h('input', { type: 'number', min: 7, max: 3650, value: st.backup_retention_days, class: 'w-xs', 'aria-label': '백업 보관 일수' });
  const load = wrap(async () => {
    const b = await api('backup-status');
    hasKey = !!b.key;
    stat.replaceChildren(h('dl', { class: 'kv' },
      h('dt', {}, '최근 백업'), h('dd', {}, b.last_at ? [chip(b.stale ? '26시간 넘음' : '정상', b.stale ? 'bad' : 'ok'), ` ${kst(b.last_at)} (${b.age_hours}시간 전)`] : chip('없음', 'bad')),
      h('dt', {}, '최근 자동 백업'), h('dd', {}, b.auto && b.auto.at ? [chip(b.auto.ok ? '성공' : '실패', b.auto.ok ? 'ok' : 'bad'), ` ${kst(b.auto.at)} · ${b.auto.ok ? b.auto.file : b.auto.error}`,
        b.auto.dest2 ? h('span', { class: 'block small' }, `두 번째 위치: ${b.auto.dest2.ok ? '복사·해시 확인' : '실패 — ' + (b.auto.dest2.error || '')}`) : null] : '아직 없음'),
      h('dt', {}, '백업 키'), h('dd', {}, b.key ? `지문 ${b.key.fpr} · ${kst(b.key.created_at)} 생성` : chip('없음 — 자동 백업 안 함', 'warn'))));
  }, msg);
  load();
  const keygen = wrap(async () => {
    const p1 = h('input', { type: 'password', autocomplete: 'new-password', class: 'fill' }), p2 = h('input', { type: 'password', autocomplete: 'new-password', class: 'fill' });
    const r = await modal({ title: hasKey ? '백업 키 다시 만들기' : '백업 키 만들기', ok: '만들고 개인키 내려받기', wide: false,
      body: [h('p', {}, '자동 백업은 이 키의 공개키로 암호화됩니다. 개인키 파일은 지금 한 번만 내려받을 수 있고 PC에는 남지 않습니다. 보안 매체에 오프라인으로 보관하고, 비밀번호는 다른 사람이 따로 보관하세요. 복원할 때 둘 다 필요합니다.'),
        hasKey ? h('div', { class: 'notice warn' }, '새 키를 만들면 이전 자동 백업은 이전 개인키로만 열립니다.') : null,
        h('label', { class: 'f' }, '개인키 보호 비밀번호 (12자 이상)', p1), h('label', { class: 'f' }, '한 번 더', p2)],
      onOk: async () => { if (p1.value !== p2.value) throw new Error('두 번 입력한 비밀번호가 다릅니다.'); return post('backup-key', { password: p1.value }); } });
    if (!r) return;
    const url = URL.createObjectURL(new Blob([r.pem], { type: 'application/x-pem-file' }));
    const a = h('a', { href: url, download: `donggu-backup-key-${r.fpr}.pem` }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast(`백업 키를 만들었습니다 (지문 ${r.fpr}). 내려받은 개인키 파일을 오프라인으로 보관하세요.`); load();
  }, msg);
  return h('div', { class: 'card' }, h('h2', {}, '자동 백업'), stat,
    h('div', { class: 'row' }, btn('백업 키 만들기', keygen, 'small'), btn('지금 자동 백업', wrap(async () => { const r = await post('backup/auto'); toast(r.ok ? `자동 백업: ${r.file}` : r.error, !r.ok); load(); }, msg), 'small'),
      btn('보관 기간 지난 백업 정리', wrap(async () => { const r = await post('backup/retention'); toast(`${r.deleted.length}개 지웠습니다`); }, msg), 'small')),
    h('div', { class: 'ftable' },
      h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '자동 백업'), h('div', { class: 'ft-v' }, h('label', { class: 'row' }, auto, '서버가 켜져 있는 동안 매일'), time, h('span', { class: 'muted small' }, '(늦게 켜면 켜자마자) · 마감 확인 때도 백업'))),
      h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '두 번째 보관 위치'), h('div', { class: 'ft-v' }, dest2, h('span', { class: 'muted small' }, '복사 뒤 해시로 확인'))),
      h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '보관'), h('div', { class: 'ft-v' }, '자동 백업 최근', kd, '일(하루 1개) + 최근', km, '개월(달마다 1개) · 모든 백업', rd, '일 지나면 삭제'))),
    h('div', { class: 'row' }, btn('자동 백업 설정 저장', wrap(async () => { await post('settings', { backup_auto: auto.checked, backup_time: time.value, backup_dest2: dest2.value, backup_keep_daily: kd.value, backup_keep_monthly: km.value, backup_retention_days: rd.value }); toast('저장했습니다'); boot = null; render(); }, msg), 'primary')),
    h('p', { class: 'small muted' }, '복원: 업무 화면을 끄고 ', h('code', {}, 'python pc/pc_tool.py restore-auto <파일.dgabk> --key <개인키.pem>'), ' (개인키 보호 비밀번호 필요). 백업 비밀번호를 PC에 저장하지 않습니다.'),
    h('p', { class: 'small muted' }, '예전 백업 파일에는 그때의 개인정보가 그대로 들어 있습니다. 개인정보를 파기해도 백업은 따로 지워야 하므로, 보관 일수(기본 90일)가 지나면 자동으로 지웁니다. 두 번째 위치·외부 매체에 옮긴 사본도 같은 기간에 지우세요.'), msg);
}

// 개인정보 보유기간·파기 (관리자)
async function privacyCard() {
  const d = await api('privacy');
  const msg = h('p', { class: 'msg' });
  const years = h('input', { type: 'number', min: 1, max: 10, value: d.retention_years, class: 'w-xs', 'aria-label': '개인정보 보유기간(년)' });
  const ayears = h('input', { type: 'number', min: 2, max: 10, value: d.access_retention_years, class: 'w-xs', 'aria-label': '접속기록 보관(년)' });
  const p = d.preview;
  return [h('div', { class: 'card' }, h('h2', {}, '개인정보 보유기간 ', chip('정책 값 · 기관 결정 필요', 'warn')),
      h('div', { class: 'ftable' }, h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '보유기간'), h('div', { class: 'ft-v' }, '처리 완료(수거 완료)·취소일부터', years, '년 (기본 3년)')),
        h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '접속기록 보관'), h('div', { class: 'ft-v' }, ayears, '년 (2년 이상)'))),
      h('div', { class: 'row' }, btn('저장', wrap(async () => { await post('settings', { pii_retention_years: years.value, access_log_retention_years: ayears.value }); toast('저장했습니다'); render(); }, msg), 'primary')),
      h('p', { class: 'small muted' }, '보유기간은 개인정보 처리방침·기록물 관리 기준에 맞춰 기관이 정하는 값입니다. 지나면 이름·연락처·주소·상세 위치·메모·좌표·민원 내용·현장 메모·사진을 가명 처리하고, 접수번호·부과번호·행정동·품목·금액·날짜는 통계·세입 확인을 위해 남깁니다.')),
    h('div', { class: 'card' }, h('h2', {}, '파기'),
      h('p', {}, `기준일 ${p.cutoff} 이전에 ${p.basis}된 접수 중 파기 대상 `, h('b', {}, `${p.count}건`), ` (환불 진행 중인 건 제외)`),
      h('div', { class: 'row' }, btn('파기 실행', wrap(async () => {
        if (!p.count) throw new Error('파기할 대상이 없습니다.');
        if (!(await confirmDlg('개인정보 파기', `${p.count}건의 이름·연락처·주소 등을 가명 처리합니다. 되돌릴 수 없습니다. 파기 대장에 일시·건수·작업자·기준이 남습니다. 계속할까요?`, { ok: '파기', danger: true }))) return;
        const r = await post('privacy/purge'); toast(`${r.count}건 파기했습니다`); render();
      }, msg), 'danger', { disabled: !p.count }), h('span', { class: 'muted small' }, '마감 확인(팀장) 때와 명령 ', h('code', {}, 'python pc/pc_tool.py purge-pii'), '로도 실행됩니다.')),
      h('div', { class: 'notice warn' }, '이전 백업 파일에는 파기 전 개인정보가 남아 있습니다. 백업은 [백업] 탭의 보관 일수에 따라 지워지며, 외부 매체로 옮긴 사본은 담당자가 같은 기간에 지워야 합니다.'), msg),
    h('div', { class: 'card' }, h('h2', {}, '파기 대장'), purgeLogTable(d.log), h('p', { class: 'small muted' }, '사용자·이력 화면에서도 볼 수 있습니다.'))];
}
const purgeLogTable = (log) => grid(['일시', '건수', '작업자', '실행', '기준'], log.map((x) => [kst(x.at), x.count, x.actor || '', ({ MANUAL: '화면', CLOSE_APPROVE: '마감 확인', CLI: '명령' })[x.trigger] || x.trigger,
  `보유 ${x.criteria.retention_years}년 · ${x.criteria.cutoff} 이전 ${x.criteria.basis}`]), { num: [1], empty: '파기 기록이 없습니다.' });

// ------------------------------------------------------------ 통계·보고서 (개인정보 없이 집계)
const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attrs = {}, ...kids) => { const el = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); kids.forEach((c) => el.append(c)); return el; };
function dayChart(days) {
  // 날짜별 수거 예정 대비 완료·재방문(일부 수거·미수거) 막대. 남은 칸은 예정 중 아직 완료 아님.
  const W = Math.max(560, days.length * 28), H = 200, padL = 34, padB = 24, padT = 10;
  const max = Math.max(1, ...days.map((d) => d.planned));
  const step = (W - padL - 8) / Math.max(1, days.length), bw = Math.min(18, step - 4);
  const y = (v) => padT + (H - padT - padB) * (1 - v / max);
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': '날짜별 수거 예정·완료·재방문' });
  for (const t of [0, Math.ceil(max / 2), max]) {
    svg.append(sv('line', { x1: padL, x2: W - 4, y1: y(t), y2: y(t), class: 'grid' }), sv('text', { x: padL - 6, y: y(t) + 4, class: 'tick', 'text-anchor': 'end' }, document.createTextNode(t)));
  }
  const tip = h('div', { class: 'tip', hidden: true });
  days.forEach((d, i) => {
    const x = padL + i * step + (step - bw) / 2;
    const warn = d.partial + d.not_collected;
    const g = sv('g', { class: 'bar', tabindex: 0 });
    g.append(sv('rect', { x: padL + i * step, y: padT, width: step, height: H - padT - padB, class: 'hit' }));
    g.append(sv('rect', { x, y: y(d.planned), width: bw, height: Math.max(0, y(0) - y(d.planned)), rx: 3, class: 'b-track' }));
    if (d.done) g.append(sv('rect', { x, y: y(d.done), width: bw, height: y(0) - y(d.done), rx: 3, class: 'b-ok' }));
    if (warn) g.append(sv('rect', { x, y: y(d.done + warn), width: bw, height: Math.max(0, y(d.done) - y(d.done + warn) - 2), rx: 3, class: 'b-warn' }));
    if (days.length <= 16 || i % Math.ceil(days.length / 16) === 0) svg.append(sv('text', { x: x + bw / 2, y: H - 6, class: 'tick', 'text-anchor': 'middle' }, document.createTextNode(d.date.slice(5))));
    const show = () => { tip.hidden = false; tip.textContent = `${d.date} · 예정 ${d.planned} · 완료 ${d.done} · 재방문 ${warn} · 취소 ${d.cancelled}`; tip.style.left = `${Math.min(x, W - 260)}px`; };
    g.addEventListener('mouseenter', show); g.addEventListener('focus', show);
    g.addEventListener('mouseleave', () => { tip.hidden = true; }); g.addEventListener('blur', () => { tip.hidden = true; });
    svg.append(g);
  });
  return h('div', { class: 'chartbox' }, h('div', { class: 'legend' }, h('span', { class: 'lg ok' }, '완료'), h('span', { class: 'lg warn' }, '재방문(일부 수거·미수거)'), h('span', { class: 'lg track' }, '예정 중 남음')), h('div', { class: 'chartscroll' }, svg, tip));
}
async function screenStats() {
  const s = (screenStats.s ||= (() => { const t = boot.today; return { start: t.slice(0, 8) + '01', end: t, basis: 'planned', itemPage: 1 }; })());
  const start = h('input', { type: 'date', value: s.start, 'aria-label': '기간 시작' }), end = h('input', { type: 'date', value: s.end, 'aria-label': '기간 끝' });
  const basis = h('select', { 'aria-label': '집계 기준' }, ...[['planned', '수거 예정일'], ['received', '접수일'], ['completed', '완료일']].map(([v, t]) => h('option', { value: v, selected: v === s.basis }, t)));
  const preset = (label, fn) => btn(label, () => { const [a, b] = fn(new Date(boot.today + 'T00:00:00')); s.start = a; s.end = b; render(true); }, 'small');
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const shift = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const presets = h('div', { class: 'seg-btns' },
    preset('오늘', (t) => [iso(t), iso(t)]), preset('최근 7일', (t) => [iso(shift(t, -6)), iso(t)]),
    preset('이번 달', (t) => [boot.today.slice(0, 8) + '01', iso(t)]),
    preset('지난달', (t) => [iso(new Date(t.getFullYear(), t.getMonth() - 1, 1)), iso(new Date(t.getFullYear(), t.getMonth(), 0))]),
    preset('올해', (t) => [`${t.getFullYear()}-01-01`, iso(t)]));
  [start, end, basis].forEach((x) => x.addEventListener('change', () => { s.start = start.value; s.end = end.value; s.basis = basis.value; s.itemPage = 1; render(true); }));
  const d = await api(`stats?start=${s.start}&end=${s.end}&basis=${s.basis}`);
  const T = d.totals, bs = T.by_status;
  const active = T.requests - (bs.CANCELLED || 0);
  const doneRate = active ? Math.round(((bs.DONE || 0) / active) * 100) : 0;
  const kpi = (k, v, sub, cls = '') => h('div', { class: 'kpi ' + cls }, h('span', { class: 'k' }, k), h('b', { class: 'v' }, v), h('span', { class: 's' }, sub || ' '));
  const q = () => `start=${s.start}&end=${s.end}&basis=${s.basis}`;
  const csv = (kind, label) => wrap(async () => { await download(`stats.csv?${q()}&kind=${kind}`, `동구-${label}-${s.start}_${s.end}.csv`); toast('내려받았습니다 (내보낸 기록이 이력에 남습니다)'); });
  const csvBtn = (kind, label) => btn('CSV', csv(kind, label), 'small', { 'aria-label': `${label} CSV` });
  const card = (title, kind, label, body) => h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, title), kind ? csvBtn(kind, label) : null), body);
  // 많이 나온 품목: 전체를 쪽으로 나눠 보기
  const IP = 20, ipages = Math.max(1, Math.ceil(d.items.length / IP));
  s.itemPage = Math.min(s.itemPage, ipages);
  const itemBody = h('div');
  const drawItems = () => {
    const part = d.items.slice((s.itemPage - 1) * IP, s.itemPage * IP);
    itemBody.replaceChildren(grid(['순위', '품목', '규격', '확인 수량', '수거 수량'], part.map((x, i) => [(s.itemPage - 1) * IP + i + 1, x.name, x.spec, `${x.requested}${x.unit_label}`, `${x.collected}${x.unit_label}`]), { num: [0, 3, 4], empty: '없음' }),
      ipages > 1 ? h('div', { class: 'pager' }, btn('‹ 이전', () => { s.itemPage -= 1; drawItems(); }, 'small', { disabled: s.itemPage <= 1 }), h('span', { class: 'pg' }, `${s.itemPage} / ${ipages}쪽 · ${d.items.length}종`), btn('다음 ›', () => { s.itemPage += 1; drawItems(); }, 'small', { disabled: s.itemPage >= ipages })) : null);
  };
  drawItems();
  const printMonthly = wrap(async () => { const r = await post('stats/print', { start: s.start, end: s.end, basis: s.basis }); window.open('/print/doc/' + r.token, 'dg-print'); });
  return h('div', {}, pagehead('통계·보고서', btn('월보 출력', printMonthly, 'small'), btn('접수별 CSV', csv('requests', '접수별'), 'primary small')),
    searchbox([['기간', [start, '~', end]], ['집계 기준', basis], ['빠른 기간', presets, 'wide2']]),
    h('div', { class: 'kpis' },
      kpi('접수', `${T.received}건`, `${d.basis_label} 기준 ${T.requests}건 · 취소 제외 ${active}건`),
      kpi('수거 완료', `${bs.DONE || 0}건`, `완료율 ${doneRate}%`, 'ok'),
      kpi('재방문·보류', `${(bs.PARTIAL || 0) + (bs.NOT_COLLECTED || 0) + (bs.ON_HOLD || 0)}건`, bs.CANCELLED ? `취소 ${bs.CANCELLED}건 별도` : ' ', (bs.PARTIAL || bs.NOT_COLLECTED) ? 'warn' : ''),
      kpi('부과 금액', won(T.levied), `계산 ${won(T.calculated)} · 감면 ${won(T.reduction)} (${T.reduction_count}건) · 납부 ${won(T.paid)}`),
      kpi('확인 필요', `${T.amount_pending + T.unpaid}건`, `금액 대기 ${T.amount_pending} · 미납·부족 ${T.unpaid}`, (T.amount_pending + T.unpaid) ? 'warn' : '')),
    h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, `날짜별 수거 (${d.basis_label} 기준)`), h('span', { class: 'row' },
        h('span', { class: 'muted small' }, `하차 ${T.unload_m3}m³ · ${T.unload_kg.toLocaleString()}kg · 현장 추가 품목 ${T.field_added_lines}건${T.lead_days_avg != null ? ` · 접수→수거 평균 ${T.lead_days_avg}일` : ''}`), btn('날짜별 CSV', csv('days', '날짜별'), 'small'))),
      d.days.length ? dayChart(d.days) : h('p', { class: 'empty' }, '이 기간에 자료가 없습니다. 기간이나 집계 기준을 바꿔 보세요.'),
      d.days.length ? h('details', { class: 'tableview' }, h('summary', {}, '표로 보기'),
        grid(['날짜', '접수', '예정', '완료', '일부', '미수거', '취소', '계산 금액', '부과 금액', '납부 기록', '하차'], d.days.map((x) => [x.date, x.received, x.planned, x.done, x.partial, x.not_collected, x.cancelled, won(x.calculated), won(x.levied), won(x.paid), x.unload_m3 ? `${x.unload_m3}m³` : '']), { num: [1, 2, 3, 4, 5, 6, 7, 8, 9] })) : null),
    card('월별 추이', 'months', '월별', grid(['월', '건수', '완료', '재방문·보류', '취소', '계산 금액', '감면액', '부과 금액', '납부 기록'], d.months.map((m) => [m.month, m.count, m.done, m.revisit, m.cancelled, won(m.calculated), won(m.reduction), won(m.levied), won(m.paid)]), { num: [1, 2, 3, 4, 5, 6, 7, 8], empty: '없음' })),
    h('div', { class: 'grid2' },
      card('행정동별', 'dongs', '동별', grid(['행정동', '건수', '완료', '재방문·보류', '취소', '완료율', '부과 금액'], d.dongs.map((x) => { const act = x.planned - x.cancelled; return [x.dong || '(동 미지정)', x.planned, x.done, x.revisit, x.cancelled, `${act ? Math.round((x.done / act) * 100) : 0}%`, won(x.levied)]; }), { num: [1, 2, 3, 4, 5, 6], empty: '없음' })),
      card('품목 분류별', 'categories', '분류별', grid(['분류', '품목 행', '확인 수량', '수거 수량', '계산 금액'], d.categories.map((x) => [x.category, x.lines, x.requested, x.collected, won(x.amount)]), { num: [1, 2, 3, 4], empty: '없음' }))),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '세입 (납부 방법별, 납부일 기준)'), h('a', { href: '#/revenue', class: 'small' }, '세입·대사 →')),
        grid(['납부 방법', '납부 건수', '납부액', '환불액', '순수입'], d.revenue.methods.map((x) => [x.method, x.pay_n, won(x.pay), won(x.refund), won(x.net)]), { num: [1, 2, 3, 4], empty: '이 기간 납부 기록 없음' })),
      card('감면', 'reductions', '감면', grid(['감면 사유', '감면율', '건수', '감면액'], d.reductions.map((x) => [x.label, `${x.rate}%`, x.count, won(x.amount)]), { num: [1, 2, 3], empty: '감면 없음' }))),
    h('div', { class: 'grid2' },
      card('많이 나온 품목 (전체)', 'items', '품목별', itemBody),
      h('div', {}, card('접수 경로별', 'channels', '경로별', grid(['접수 경로', '건수', '완료', '취소'], d.channels.map((x) => [x.channel, x.planned, x.done, x.cancelled]), { num: [1, 2, 3], empty: '없음' })),
        card('팀별', null, null, grid(['팀', '배정', '완료', '재방문·보류', '완료율'], d.crews.map((c) => [c.crew, c.assigned, c.done, c.partial_or_not, `${c.assigned ? Math.round((c.done / c.assigned) * 100) : 0}%`]), { num: [1, 2, 3, 4], empty: '배정 기록 없음' })))),
    h('p', { class: 'muted' }, 'CSV는 엑셀에서 바로 열립니다. 이름·연락처·주소·메모는 넣지 않고, 상태는 한글 이름과 코드를 함께 담습니다. 월보 제목은 설정 → 기본에서 바꿉니다. 내보낸 기록은 변경 이력에 남습니다.'));
}

// ------------------------------------------------------------ 세입 일계표·월계표 + 대사 (세외수입 담당자용)
async function screenRevenue() {
  const s = (screenRevenue.s ||= { start: boot.today.slice(0, 8) + '01', end: boot.today, unit: 'day' });
  const start = h('input', { type: 'date', value: s.start, 'aria-label': '기간 시작' }), end = h('input', { type: 'date', value: s.end, 'aria-label': '기간 끝' });
  const unit = h('select', { 'aria-label': '집계 단위' }, h('option', { value: 'day', selected: s.unit === 'day' }, '일계표(날짜별)'), h('option', { value: 'month', selected: s.unit === 'month' }, '월계표(월별)'));
  [start, end, unit].forEach((x) => x.addEventListener('change', () => { Object.assign(s, { start: start.value, end: end.value, unit: unit.value }); render(true); }));
  const d = await api(`revenue?start=${s.start}&end=${s.end}&unit=${s.unit}`);
  const qs = `start=${s.start}&end=${s.end}&unit=${s.unit}`;
  const dl = (kind, label) => btn(label, wrap(async () => { await download(`revenue.csv?${qs}&kind=${kind}`, `동구-세입-${label}-${s.start}_${s.end}.csv`); toast('내려받았습니다'); }), 'small');
  const per = s.unit === 'month' ? '월' : '납부일';
  const T = d.totals;
  // 대사: CSV(부과번호·금액·납부일) → 미리보기(일치·불일치) → 확인하면 일치 건만 '대사' 출처 납부로 기록
  const file = h('input', { type: 'file', accept: '.csv,.txt', 'aria-label': '대사 CSV 파일' });
  const out = h('div'), msg = h('p', { class: 'msg' });
  const raw = async () => { const f0 = file.files[0]; if (!f0) throw new Error('파일을 고르세요.'); return new Uint8Array(await f0.arrayBuffer()); };
  const canRecon = canWork();
  const preview = wrap(async () => {
    const r = await api('recon?preview=1', { method: 'POST', raw: await raw() });
    const bad = r.rows.filter((x) => x.status !== 'MATCH');
    out.replaceChildren(h('div', { class: 'notice info' }, `${r.total}행 · `, ...Object.entries(r.counts).map(([k, n]) => chip(`${RECON[k] || k} ${n}`, k === 'MATCH' ? 'ok' : k === 'ALREADY' ? 'neutral' : 'warn')),
        r.already_imported ? h('strong', {}, ` 이미 반영한 파일입니다(${kst(r.already_imported.at)}).`) : null),
      grid(['행', '부과번호', '접수번호', '금액', '납부일', '영수번호', '부과·납부', '판정', '설명'], r.rows.map((x) => [x.line, x.levy_no, x.receipt_no || '', x.amount != null ? won(x.amount) : '', x.paid_on, x.ref || '',
        x.levied != null ? `${won(x.levied)} / ${won(x.paid)}` : '', chip(RECON[x.status] || x.status, x.status === 'MATCH' ? 'ok' : x.status === 'ALREADY' ? 'neutral' : 'warn'), x.note || '']), { cls: 'recon', num: [0, 3] }),
      h('div', { class: 'row' }, btn(`일치 ${r.counts.MATCH || 0}건 납부로 기록`, wrap(async () => {
        if (!r.counts.MATCH) throw new Error('일치하는 행이 없습니다.');
        if (!(await confirmDlg('대사 반영', `일치 ${r.counts.MATCH}건을 납부(출처 '대사')로 기록합니다. 불일치 ${bad.length}건은 기록하지 않으니 따로 확인하세요.`, { ok: '기록' }))) return;
        const x = await api('recon?preview=0', { method: 'POST', raw: await raw() }); toast(`${x.recorded}건을 납부로 기록했습니다`); file.value = ''; render();
      }, msg), 'primary', { disabled: !r.counts.MATCH || !!r.already_imported }), h('span', { class: 'muted small' }, '불일치(부과번호 없음·금액 다름·중복)는 자동으로 고치지 않습니다. 접수 상세에서 확인해 수기로 기록하세요.')));
  }, msg);
  const hist = canRecon ? await api('recon-history').catch(() => []) : [];
  return h('div', {}, pagehead('세입·대사', dl('method', '방법별'), dl('dong', '동별'), dl('detail', '명세')),
    h('div', { class: 'notice warn' }, '세외수입시스템 연계 형식은 기관 확인 필요 — 이 화면의 CSV(일계표·월계표·명세)와 대사 서식은 담당자 확인용 기본 형식입니다. 실제 연계 파일 형식이 정해지면 맞춰야 합니다.'),
    searchbox([['기간', [start, '~', end]], ['집계 단위', unit]]),
    h('div', { class: 'kpis' }, ...[['납부', won(T.pay), `${T.pay_n}건`], ['환불', won(T.refund), `${T.refund_n}건`], ['순수입', won(T.net), `${d.count}건 기록`]].map(([k, v, sub]) => h('div', { class: 'kpi' }, h('span', { class: 'k' }, k), h('b', { class: 'v' }, v), h('span', { class: 's' }, sub)))),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('h2', {}, `납부 방법별 (${s.unit === 'month' ? '월계' : '일계'})`), grid([per, '납부 방법', '납부 건수', '납부액', '환불 건수', '환불액', '순수입'],
        d.by_method.map((x) => [x.period, x.method, x.pay_n, won(x.pay), x.refund_n, won(x.refund), won(x.net)]), { num: [2, 3, 4, 5, 6], empty: '이 기간 납부 기록이 없습니다.' })),
      h('div', { class: 'card' }, h('h2', {}, `행정동별 (${s.unit === 'month' ? '월계' : '일계'})`), grid([per, '행정동', '납부 건수', '납부액', '환불액', '순수입'],
        d.by_dong.map((x) => [x.period, x.dong || '(동 미지정)', x.pay_n, won(x.pay), won(x.refund), won(x.net)]), { num: [2, 3, 4, 5], empty: '이 기간 납부 기록이 없습니다.' }))),
    canRecon ? h('div', { class: 'card' }, h('h2', {}, '대사 (세외수입 납부 자료 맞추기)'),
      h('p', { class: 'muted' }, '세외수입 담당자에게 받은 납부 자료를 CSV(부과번호, 금액, 납부일, 영수번호 선택)로 넣으면 PC의 부과 금액과 맞춰 봅니다. 미리보기에서 일치·불일치를 확인한 뒤 [기록]을 눌러야 반영됩니다. 같은 파일은 한 번만 반영됩니다.'),
      h('div', { class: 'row' }, btn('서식 내려받기', wrap(() => download('recon-template', '동구-대사-서식.csv')), 'small'), file, btn('미리보기', preview, 'small primary')), msg, out,
      hist.length ? h('details', { class: 'tableview' }, h('summary', {}, `반영 기록 ${hist.length}건`), grid(['시각', '작업자', '행', '일치·기록'], hist.map((x) => [kst(x.at), x.actor, x.rows, x.matched]), { num: [2, 3] })) : null) : null);
}

// ------------------------------------------------------------ 환불 목록 (신청 → 결재 → 지급 기록)
async function screenRefunds() {
  const s = (screenRefunds.s ||= { status: 'OPEN' });
  const d = await api('refunds?status=' + s.status);
  const role = boot.me.role, work = canWork(), lead = ['ADMIN', 'LEAD'].includes(role);
  const msg = h('p', { class: 'msg' });
  const tabs = h('div', { class: 'quick' }, ...[['OPEN', '진행 중'], ['DRAFT', '신청 전'], ['REQUESTED', '결재 대기'], ['APPROVED', '지급 전'], ['PAID', '지급 완료'], ['', '전체']].map(([k, t]) =>
    h('button', { type: 'button', class: 'qchip' + (s.status === k ? ' on' : ''), 'aria-pressed': s.status === k ? 'true' : 'false', onclick: () => { s.status = k; render(true); } }, t,
      h('b', {}, k === 'OPEN' ? (d.counts.DRAFT + d.counts.REQUESTED + d.counts.APPROVED) : k ? d.counts[k] : Object.values(d.counts).reduce((a, b) => a + b, 0)))));
  const act = (c) => {
    const out = [];
    if (work && c.status === 'DRAFT') out.push(btn('신청', wrap(async () => {
      const amt = h('input', { type: 'number', value: c.amount, min: 1, max: c.amount, class: 'fill' }), note = h('input', { class: 'fill', placeholder: '메모 (예: 주민 요청, 입금 확인)' });
      if (!(await modal({ title: `환불 신청 · ${c.receipt_no}`, ok: '신청', body: [h('p', {}, `${REFUND_CAUSE[c.cause]} · 돌려줄 금액 ${won(c.amount)}`), h('label', { class: 'f' }, '환불 금액', amt), h('label', { class: 'f' }, '메모', note)],
        onOk: () => post('refunds/request', { case_id: c.id, amount: amt.value, note: note.value }) }))) return;
      toast('환불을 신청했습니다. 팀장(검토자)·관리자의 결재가 필요합니다.'); render();
    }, msg), 'small primary'));
    if (lead && c.status === 'REQUESTED') {
      out.push(btn('결재', wrap(async () => {
        const note = h('input', { class: 'fill', id: 'rf-note' });
        if (!(await modal({ title: `환불 결재 · ${c.receipt_no}`, ok: '승인', body: [h('p', {}, `${REFUND_CAUSE[c.cause]} ${won(c.amount)} · 신청 ${c.requested_by} ${kst(c.requested_at)}${c.note ? ' · ' + c.note : ''}`), h('label', { class: 'f' }, '메모', note)],
          onOk: () => post(`refunds/${c.id}/approve`, { note: note.value }) }))) return;
        toast('결재했습니다'); render();
      }, msg), 'small primary'), btn('반려', wrap(async () => { const why = await promptDlg('환불 반려', '반려 사유', { ok: '반려', danger: true }); if (!why) return; await post(`refunds/${c.id}/reject`, { note: why }); render(); }, msg), 'small danger'));
    }
    if (work && c.status === 'APPROVED') out.push(btn('지급 기록', wrap(async () => {
      const day = h('input', { type: 'date', value: boot.today }), meth = h('select', {}, ...(boot.settings.refund_methods || []).map((m) => h('option', { value: m }, m)));
      const chk = h('input', { type: 'checkbox' }), last4 = h('input', { inputmode: 'numeric', maxlength: 4, placeholder: '끝 4자리 (선택)', class: 'w-s' });
      if (!(await modal({ title: `환불 지급 기록 · ${c.receipt_no}`, ok: '지급 기록', body: [h('p', {}, `${won(c.amount)} · 결재 ${c.approved_by}`), h('label', { class: 'f' }, '지급일', day), h('label', { class: 'f' }, '지급 방법', meth),
        h('label', { class: 'row' }, chk, '예금주·계좌를 확인함'), h('label', { class: 'f' }, '계좌 끝 4자리', last4),
        h('p', { class: 'small muted' }, '전체 계좌번호는 이 PC에 저장하지 않습니다. 확인 여부와 끝 4자리만 남깁니다.')],
        onOk: () => post(`refunds/${c.id}/pay`, { paid_on: day.value, method: meth.value, account_checked: chk.checked, account_last4: last4.value }) }))) return;
      toast('지급을 기록했습니다'); render();
    }, msg), 'small primary'));
    if (work && ['DRAFT', 'REQUESTED', 'APPROVED'].includes(c.status)) out.push(btn('취소', wrap(async () => { const why = await promptDlg('환불 건 취소', '취소 사유', { ok: '취소 기록', danger: true }); if (!why) return; await post(`refunds/${c.id}/void`, { reason: why }); render(); }, msg), 'small'));
    return out;
  };
  const stCls = (st) => (st === 'PAID' ? 'ok' : ['REJECTED', 'VOID'].includes(st) ? 'neutral' : st === 'REQUESTED' ? 'info' : 'warn');
  return h('div', {}, pagehead('환불'),
    h('div', { class: 'card searchbox', role: 'group', 'aria-label': '상태' }, h('div', { class: 'cond' }, h('span', { class: 'cond-l' }, '상태'), h('div', { class: 'cond-v wide' }, tabs))),
    gridbar(d.rows.length), msg,
    h('div', { class: 'card tablecard' }, grid(['접수번호', '부과번호', '동', '사유', '금액', '상태', '신청', '결재', '지급', '처리'], d.rows.map((c) => [h('a', { href: `#/request/${c.request_id}` }, c.receipt_no), c.levy_no || '', c.dong || '',
      REFUND_CAUSE[c.cause] || c.cause, won(c.amount), chip(REFUND_ST[c.status] || c.status, stCls(c.status)), c.requested_by ? `${c.requested_by} ${kst(c.requested_at)}` : (c.auto ? '자동 생성' : ''),
      c.approved_by ? `${c.approved_by} ${kst(c.approved_at)}${c.approve_note ? ' · ' + c.approve_note : ''}` : '', c.paid_on ? `${c.paid_on} · ${c.pay_method}${c.account_checked ? ' · 계좌 확인' : ''}${c.account_last4 ? ' ·· ' + c.account_last4 : ''}` : (c.void_reason || ''),
      h('span', { class: 'row' }, ...act(c))]), { num: [4], empty: '해당하는 환불 건이 없습니다.' })),
    d.partial.length ? h('div', { class: 'card' }, h('h2', {}, `일부 수거 차액 환불 후보 ${d.partial.length}건`),
      h('p', { class: 'muted small' }, '일부 수거로 끝나 실제로 실은 수량 기준 부과 금액보다 더 낸 경우입니다. 재방문하지 않기로 했을 때만 신청하세요(자동으로 신청하지 않음).'),
      grid(['접수번호', '부과번호', '동', '부과 금액', '납부', '차액', ''], d.partial.map((x) => [h('a', { href: `#/request/${x.request_id}` }, x.receipt_no), x.levy_no || '', x.dong || '', won(x.levied), won(x.paid), won(x.due),
        work ? btn('차액 환불 신청', wrap(async () => { await post('refunds/request', { request_id: x.request_id, cause: 'PARTIAL', amount: x.due, note: '일부 수거 차액' }); toast('신청했습니다'); render(); }, msg), 'small') : '']), { num: [3, 4, 5] })) : null,
    h('p', { class: 'muted' }, '납부 뒤 취소(환불 필요)·과납은 자동으로 "신청 전" 건이 생깁니다. 접수 담당자가 신청하고, 팀장(검토자)·관리자 중 신청하지 않은 사람이 결재한 뒤, 지급일·방법을 기록합니다. 계좌번호는 저장하지 않습니다.'));
}

// ------------------------------------------------------------ 개인정보 접속기록 (관리자·팀장)
async function screenAccess() {
  const s = (screenAccess.s ||= { date_from: boot.today.slice(0, 8) + '01', date_to: boot.today, actor: '', action: '', page: 1 });
  const users = boot.me.role === 'ADMIN' ? await api('users').catch(() => []) : [];
  const st = await api('access/status');
  const from = h('input', { type: 'date', value: s.date_from, 'aria-label': '기간 시작' }), to = h('input', { type: 'date', value: s.date_to, 'aria-label': '기간 끝' });
  const actor = users.length ? h('select', { 'aria-label': '사용자' }, h('option', { value: '' }, '전체'), ...users.map((u) => h('option', { value: u.username, selected: u.username === s.actor }, `${u.username} (${u.display_name})`)))
    : h('input', { value: s.actor, placeholder: '아이디', 'aria-label': '사용자' });
  const action = h('select', { 'aria-label': '작업' }, h('option', { value: '' }, '전체'), ...Object.entries(ACCESS).map(([k, t]) => h('option', { value: k, selected: k === s.action }, t)));
  const tbody = h('tbody'), info = h('b', {}, '0'), pager = h('div', { class: 'pager' }), vres = h('div');
  const qs = () => new URLSearchParams({ date_from: s.date_from, date_to: s.date_to, actor: s.actor, action: s.action });
  const load = wrap(async () => {
    Object.assign(s, { date_from: from.value, date_to: to.value, actor: actor.value, action: action.value });
    const r = await api(`access?${qs()}&page=${s.page}&size=100`);
    s.page = r.page; info.textContent = r.total.toLocaleString();
    tbody.replaceChildren(...(r.rows.length ? r.rows.map((a) => h('tr', {}, h('td', { class: 'num rn' }, a.id), h('td', { class: 'nowrap' }, kst(a.at)), h('td', {}, a.actor), h('td', {}, ACCESS[a.action] || a.action),
      h('td', {}, a.receipt_no ? h('a', { href: `#/request/${a.target}` }, a.receipt_no) : (a.target ? a.target.slice(0, 8) : '')), h('td', { class: 'num' }, a.count ?? ''), h('td', { class: 'detail' }, a.filter), h('td', {}, a.client)))
      : [h('tr', {}, h('td', { colspan: 8, class: 'muted' }, '기록이 없습니다.'))]));
    pager.replaceChildren(btn('‹ 이전', () => { s.page -= 1; load(); }, 'small', { disabled: r.page <= 1 }), h('span', { class: 'pg' }, `${r.page} / ${r.pages}쪽`), btn('다음 ›', () => { s.page += 1; load(); }, 'small', { disabled: r.page >= r.pages }));
  });
  [from, to, actor, action].forEach((x) => x.addEventListener('change', () => { s.page = 1; load(); }));
  load();
  const showVerify = (v) => vres.replaceChildren(h('div', { class: 'notice ' + (v.ok ? 'info' : 'bad') }, h('strong', {}, v.ok ? '무결성 검사 정상' : '무결성 검사 실패'),
    ` · 접속기록 ${v.access.checked}행 ${v.access.ok ? '정상' : `— ${v.access.broken_id}번에서 끊김: ${v.access.why}`} · 변경 이력 ${v.audit.checked}행 ${v.audit.ok ? '정상' : `— ${v.audit.broken_id}번에서 끊김: ${v.audit.why}`}`,
    v.audit.legacy ? ` (해시 사슬 이전 기록 ${v.audit.legacy}행은 검사 대상 아님)` : ''));
  const verify = wrap(async () => showVerify(await api('integrity')));
  const done = st.done;
  return h('div', {}, pagehead('개인정보 접속기록', btn('무결성 검사', verify, 'small'), btn('CSV 내보내기', wrap(async () => { await download(`access.csv?${qs()}`, `동구-접속기록-${s.date_from}_${s.date_to}.csv`); toast('내려받았습니다 (내보낸 기록이 이력에 남습니다)'); }), 'small')),
    h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, `이번 달 접속기록 점검 (${st.month})`), done ? chip(`점검 완료 · ${done.actor} ${kst(done.at)}`, done.result && done.result.ok ? 'ok' : 'bad') : chip('점검 전', 'warn')),
      h('p', { class: 'muted' }, `이번 달 개인정보 조회 ${st.views_this_month.toLocaleString()}건. 매월 한 번 아래 목록에서 업무와 관계없는 조회·대량 조회·근무시간 외 조회가 없는지 보고 [점검 완료]를 기록하세요. 기록할 때 무결성 검사를 함께 합니다.`),
      h('div', { class: 'row' }, h('input', { id: 'acc-note', class: 'fill', placeholder: '점검 메모 (예: 이상 없음, 대량 조회 1건 사유 확인)', 'aria-label': '점검 메모' }),
        btn('점검 완료', wrap(async () => { const v = await post('access/check', { note: document.getElementById('acc-note').value }); showVerify(v); toast(v.ok ? '점검을 기록했습니다' : '점검을 기록했습니다 — 무결성 검사 실패, 정보보호 담당에 알리세요', !v.ok); render(); }), 'primary')),
      vres,
      st.checks.length ? h('details', { class: 'tableview' }, h('summary', {}, `점검 기록 ${st.checks.length}건`), grid(['월', '시각', '점검자', '결과', '메모'], st.checks.map((c) => [c.month, kst(c.at), c.actor, chip(c.ok ? '정상' : '실패', c.ok ? 'ok' : 'bad'), c.note || '']))) : null),
    searchbox([['기간', [from, '~', to]], ['사용자', actor], ['작업', action]], btn('검색', () => { s.page = 1; load(); }, 'dark')),
    h('div', { class: 'gridbar' }, h('span', { class: 'cnt' }, '총 ', info, '건'), h('span', { class: 'grow' })),
    h('div', { class: 'card tablecard' }, h('table', { class: 'grid audit' }, h('thead', {}, h('tr', {}, ...['번호', '시각', '사용자', '작업', '대상', '건수', '조건', '접속'].map((t) => h('th', { scope: 'col' }, t)))), tbody)), pager,
    h('p', { class: 'muted' }, '접수 상세·목록·사진·출력·개인정보 포함 내보내기를 기록합니다. 목록은 내용 없이 건수와 조건만, 검색어 값은 남기지 않습니다. 각 행은 이전 행의 해시를 이어 받아 고치거나 지우면 무결성 검사에서 드러납니다. 2년 이상 보관합니다(설정 → 개인정보).'));
}

// ------------------------------------------------------------ 수수료표 관리
async function screenFees() {
  const d = await api('fee-table');
  const me = boot.me, isAdmin = me.role === 'ADMIN';
  const stt = d.state;
  const msg = h('p', { class: 'msg' });
  const draft = d.revisions.find((r) => r.status === 'DRAFT');
  const approved = d.revisions.find((r) => r.active);
  const cur = stt.overrides || {};
  // 확인 필요 행 확정 입력
  const inputs = {};
  const reviewTable = h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['품목', '규격', '단위', '원문 단가', '확인 필요 사유', '현재', isAdmin ? '확정 단가 · 근거' : ''].map((t) => h('th', {}, t)))),
    h('tbody', {}, ...d.review_rows.map((r) => {
      const o = cur[r.id];
      const price = h('input', { type: 'number', min: 1, placeholder: '원', style: 'width:100px', value: o ? o.price : '' });
      const reason = h('input', { placeholder: '근거 (예: 원문 각주 2, 담당 부서 회신 11/3)', style: 'width:280px', value: o ? o.reason : '' });
      inputs[r.id] = { price, reason, had: !!o };
      return h('tr', {}, h('td', {}, r.name), h('td', {}, r.spec), h('td', {}, r.unit_label), h('td', {}, r.price != null ? won(r.price) : '—'),
        h('td', { class: 'muted' }, r.review_note || r.note || '원문 규격 겹침·단위 불명확'),
        h('td', {}, o ? chip(`확정 ${won(o.price)}`, 'ok') : chip('금액 확인 대기', 'warn')),
        h('td', {}, isAdmin ? h('div', { class: 'row' }, price, reason) : null));
    })));
  const eff = h('input', { type: 'date', value: (draft && draft.effective_from) || stt.effective_from || '' });
  const who = h('input', { placeholder: '원문 대조 확인자 (이름·부서)', value: (draft && draft.confirmed_by) || stt.confirmed_by || '' });
  const note = h('input', { placeholder: '메모 (선택)', style: 'width:320px' });
  const saveDraft = wrap(async () => {
    const overrides = {}, remove = [];
    for (const [rid, f] of Object.entries(inputs)) {
      if (f.price.value) overrides[rid] = { price: f.price.value, reason: f.reason.value };
      else if (f.had) remove.push(rid);
    }
    await post('fee-revisions', { effective_from: eff.value, confirmed_by: who.value, note: note.value, overrides, remove });
    toast('개정안을 저장했습니다. 다른 담당자가 승인해야 적용됩니다.'); render();
  }, msg);
  const reason = h('input', { placeholder: '반려 사유', style: 'width:220px' });
  const draftCard = draft ? h('div', { class: 'card notice' }, h('h2', {}, `승인 대기: 개정 ${draft.rev_no}번 (${draft.version_id})`),
    h('dl', { class: 'kv' }, h('dt', {}, '시행일'), h('dd', {}, draft.effective_from), h('dt', {}, '확인자'), h('dd', {}, draft.confirmed_by), h('dt', {}, '작성'), h('dd', {}, `${draft.created_by} · ${kst(draft.created_at)}`),
      h('dt', {}, '확정 행'), h('dd', {}, Object.entries(draft.overrides).map(([rid, o]) => { const r = BUILTIN_FEES.rows.find((x) => x.id === rid); return `${r ? r.name + ' ' + r.spec : rid} ${won(o.price)} (${o.reason})`; }).join(' / ') || '없음')),
    draft.created_by === me.username ? h('p', { class: 'muted' }, '작성한 사람은 승인할 수 없습니다. 다른 담당자가 원문과 대조해 승인해야 적용됩니다.')
      : h('div', { class: 'row' }, btn('원문과 대조했고 승인', wrap(async () => { await post(`fee-revisions/${draft.id}/approve`, {}); toast('승인했습니다. 시행일부터 새 접수에 적용됩니다.'); boot = null; render(); }, msg), 'primary'),
        reason, btn('반려', wrap(async () => { await post(`fee-revisions/${draft.id}/reject`, { reason: reason.value }); toast('반려했습니다'); render(); }, msg)))) : null;
  // 전체 표 조회
  const q = h('input', { type: 'search', placeholder: '품목·규격·초성 찾기', style: 'width:320px' });
  const allBody = h('tbody');
  const drawAll = () => {
    const rows = q.value ? searchFees(FEES(), q.value) : FEES().rows;
    allBody.replaceChildren(...rows.slice(0, 200).map((r) => h('tr', {}, h('td', {}, r.category || ''), h('td', {}, r.name), h('td', {}, r.spec), h('td', {}, r.unit_label),
      h('td', {}, r.status === 'CONFIRMED' ? won(r.price) : chip('금액 확인 대기', 'warn'), r.overridden ? chip('개정 확정', 'ok') : null))));
  };
  q.addEventListener('input', drawAll); drawAll();
  return h('div', {}, pagehead('수수료표', h('a', { href: '#/settings/fees', class: 'small' }, '감면 사유 설정 →')),
    h('div', { class: 'card' }, h('dl', { class: 'kv' },
      h('dt', {}, '원문'), h('dd', {}, `${d.source.label} · ${d.source.rows}행 · 자료 확인일 ${d.source.checked_on}`),
      h('dt', {}, '적용 버전'), h('dd', {}, stt.version_id),
      h('dt', {}, '시행일'), h('dd', {}, stt.effective_from || chip('미확인', 'warn')),
      h('dt', {}, '확인자 · 승인자'), h('dd', {}, stt.confirmed_by ? `${stt.confirmed_by} · ${stt.approved_by}` : '아직 없음')),
      h('p', { class: 'muted' }, '원문 변환본은 고치지 않습니다. 확정 단가·시행일·확인자는 개정으로 쌓고, 작성자와 다른 담당자가 승인하면 시행일부터 새 접수와 휴대폰 목록에 적용됩니다. 이미 접수된 품목의 단가·버전은 바뀌지 않습니다.')),
    draftCard,
    h('div', { class: 'card' }, h('h2', {}, `확인 필요 행 ${d.review_rows.length}개`), h('div', { class: 'tablewrap' }, reviewTable),
      isAdmin ? h('div', {}, h('div', { class: 'row', style: 'margin-top:12px' }, '시행일', eff, '확인자', who, note), h('div', { class: 'row' }, btn('개정안 저장', saveDraft, 'primary'),
        h('span', { class: 'muted' }, '비워 둔 행은 금액 확인 대기로 남습니다. 0원이나 최저가로 정하지 마세요.'))) : h('p', { class: 'muted' }, '개정안 작성은 운영 관리자가 합니다. 승인은 작성자가 아닌 담당자가 할 수 있습니다.'), msg),
    h('div', { class: 'card' }, h('h2', {}, '개정 이력'), d.revisions.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['개정', '상태', '시행일', '확인자', '작성', '승인·반려', '확정 행'].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...d.revisions.map((r) => h('tr', {}, h('td', {}, r.version_id), h('td', {}, r.active ? chip('적용 중', 'ok') : r.status === 'APPROVED' ? chip(r.effective_from > boot.today ? '시행 전' : '이전 승인') : r.status === 'DRAFT' ? chip('승인 대기', 'warn') : chip('반려', 'bad')),
        h('td', {}, r.effective_from), h('td', {}, r.confirmed_by), h('td', {}, `${r.created_by} ${kst(r.created_at)}`), h('td', {}, r.approved_by ? `${r.approved_by} ${kst(r.approved_at)}${r.reject_reason ? ' · ' + r.reject_reason : ''}` : ''),
        h('td', {}, Object.keys(r.overrides).length + '행')))))) : h('p', { class: 'empty' }, '아직 개정이 없습니다.')),
    h('div', { class: 'card' }, h('h2', {}, '전체 표'), q, h('div', { class: 'tablewrap tall' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['분류', '품목', '규격', '단위', '단가'].map((t) => h('th', {}, t)))), allBody))));
}

// ------------------------------------------------------------ 주소 위치 자료 (운영 관리자)
function addrCard() {
  const msg = h('p', { class: 'msg' });
  const stat = h('span', { class: 'muted' });
  api('addr-stats').then((x) => { stat.textContent = x.count ? `${x.count.toLocaleString()}건 (${x.sigungu.join(', ')})` : '아직 없음'; }).catch(() => {});
  const file = h('input', { type: 'file', accept: '.txt,.csv' });
  const region = h('input', { placeholder: '시군구 거르기 (예: 동구)', value: '동구', style: 'width:180px' });
  const cols = h('input', { placeholder: '열 번호 직접 지정 (비우면 자동)', style: 'width:260px' });
  const out = h('div');
  const send = async (preview) => {
    const f = file.files[0]; if (!f) throw new Error('파일을 고르세요.');
    const buf = new Uint8Array(await f.arrayBuffer());
    return api(`addr-import?region=${encodeURIComponent(region.value)}&preview=${preview ? 1 : 0}&columns=${encodeURIComponent(cols.value)}`, { method: 'POST', raw: buf });
  };
  const prev = wrap(async () => {
    const r = await send(true);
    out.replaceChildren(h('div', { class: 'notice' }, h('p', {}, `찾은 열: ${Object.entries(r.columns).filter(([k]) => k !== 'header').map(([k, v]) => `${k}=${v}`).join(', ')} · ${r.count.toLocaleString()}건`),
      h('ul', {}, ...r.sample.map((x) => h('li', {}, `${x.sigungu} ${x.road} ${x.main}${x.sub ? '-' + x.sub : ''} → ${x.lat.toFixed(6)}, ${x.lon.toFixed(6)}`))),
      h('p', { class: 'small' }, '표본 주소가 지도에서 맞는 곳인지 확인한 뒤 가져오세요.'),
      btn('이대로 가져오기', wrap(async () => { const x = await send(false); toast(`${x.count.toLocaleString()}건 가져옴 (전체 ${x.total.toLocaleString()}건)`); out.replaceChildren(); render(); }, msg), 'primary')));
  }, msg);
  return h('div', { class: 'card' }, h('h2', {}, '주소 위치 자료'),
    h('p', { class: 'muted' }, '행정안전부 도로명주소 "위치정보요약DB" 파일을 이 PC에 넣으면 접수 화면에서 주소로 위치를 찾습니다. 모든 처리는 이 PC 안에서 하며 외부로 보내지 않습니다. 큰 파일은 명령으로 가져오세요: ', h('code', {}, 'python pc/pc_tool.py import-addr <파일·폴더> --region 동구')),
    h('div', { class: 'row' }, '저장된 자료', stat),
    h('div', { class: 'row' }, file, region, cols, btn('미리보기', prev, 'small')), out, msg);
}

// ------------------------------------------------------------ 외부 서비스 (운영 관리자, v0.11): 행안부 주소 API·카카오모빌리티
// 키는 PC의 암호화 DB에만 저장하고 화면에는 가린 값만 돌아온다. 외부 호출은 PC 서버가 하며 브라우저는 직접 연결하지 않는다.
function externalCard() {
  const ex = boot.settings.external || { keys: {}, travel_method: 'ESTIMATE' };
  const msg = h('p', { class: 'msg' });
  const keyRow = (k) => {
    const info = ex.keys[k] || { set: false, mask: '', label: k };
    const inp = h('input', { type: 'password', autocomplete: 'off', spellcheck: 'false', class: 'w-l', 'aria-label': info.label, placeholder: info.set ? `저장됨 ${info.mask} (바꿀 때만 입력)` : '(없음)' });
    const clr = info.set ? btn('지우기', wrap(async () => { await post('settings', { external: { keys: { [k]: '' } } }); toast(`${info.label}를 지웠습니다`); boot = null; render(); }, msg), 'small') : null;
    return { k, inp, el: h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, info.label), h('div', { class: 'ft-v' }, inp, info.set ? chip('저장됨 ' + info.mask, 'ok') : chip('없음'), clr)) };
  };
  const rows = ['juso_search_key', 'juso_coord_key', 'kakao_rest_key'].map(keyRow);
  const method = h('select', { 'aria-label': '이동시간 계산 방식' }, ...[['ESTIMATE', '직선거리 추정 (기본, 외부 전송 없음)'], ['OSRM', '로컬 OSRM (기본 설정의 OSRM 주소)'], ['KAKAO', '카카오모빌리티 API (REST 키)']]
    .map(([v, t]) => h('option', { value: v, selected: v === ex.travel_method }, t)));
  const testOut = h('span', { class: 'muted small', role: 'status' });
  const test = (svc) => wrap(async () => { testOut.textContent = '연결 시험 중…'; const r = await post('external/test', { service: svc }); testOut.textContent = (r.ok ? '✓ ' : '✗ ') + r.message; }, msg);
  return h('div', { class: 'card' }, h('h2', {}, '외부 서비스 (주소 검색·도로 이동시간)'),
    h('p', { class: 'muted' }, '사용자 요청으로 더한 기능입니다. 이 PC 서버가 대신 묻고, 브라우저는 외부 서버에 직접 연결하지 않습니다. 이름·연락처는 보내지 않습니다.'),
    h('ul', { class: 'small muted' },
      h('li', {}, '행안부 도로명주소 API(business.juso.go.kr): 접수 화면 [주소 찾기]에 적은 검색어만 보냅니다. 전화번호 모양 값·신청인 이름이 든 검색어는 보내지 않습니다. 고른 결과의 좌표는 건물 식별 번호로 묻습니다.'),
      h('li', {}, '카카오모빌리티 길찾기 API: 노선 계산 때 지점 좌표만 보냅니다. 받은 시간은 30일 동안 이 PC에 보관해 다시 묻지 않고, 못 받은 구간은 직선거리로 추정해 화면에 몇 쌍인지 표시합니다.'),
      h('li', {}, '기관망 프록시는 PC 서버를 실행할 때의 HTTPS_PROXY 환경변수를 따릅니다. 검색어는 서버 로그·접속기록에 남지 않습니다(접속기록에는 검색했다는 사실과 건수만).')),
    h('div', { class: 'ftable' }, ...rows.map((r) => r.el), h('div', { class: 'ft-row' }, h('span', { class: 'ft-l' }, '이동시간 계산 방식'), h('div', { class: 'ft-v' }, method))),
    h('div', { class: 'row' }, btn('저장', wrap(async () => {
      const keys = {}; rows.forEach((r) => { if (r.inp.value.trim()) keys[r.k] = r.inp.value.trim(); });
      await post('settings', { external: { keys, travel_method: method.value } }); rows.forEach((r) => { r.inp.value = ''; });
      toast('저장했습니다'); boot = null; render();
    }, msg), 'primary'), btn('주소 검색 연결 시험', test('juso'), 'small'), btn('카카오모빌리티 연결 시험', test('kakao'), 'small'), testOut), msg);
}

// ------------------------------------------------------------ 백업·사진 보관 (운영 관리자)
function backupCard() {
  const st = boot.settings;
  const msg = h('p', { class: 'msg' });
  const list = h('tbody'), dirEl = h('code');
  const pass = h('input', { type: 'password', placeholder: '백업 비밀번호 (12자 이상, DB 키와 다르게)', autocomplete: 'new-password', style: 'width:300px' });
  const pass2 = h('input', { type: 'password', placeholder: '한 번 더', autocomplete: 'new-password', style: 'width:160px' });
  const days = h('input', { type: 'number', value: st.photo_retention_days, min: 7, max: 3650, style: 'width:90px' });
  const bdir = h('input', { value: st.backup_dir, placeholder: '비우면 프로그램 폴더 state/backups', style: 'width:320px' });
  const purgeInfo = h('span', { class: 'muted' });
  const load = wrap(async () => {
    const b = await api('backups');
    dirEl.textContent = b.dir;
    list.replaceChildren(...(b.files.length ? b.files.slice(0, 20).map((f) => {
      const vp = h('input', { type: 'password', placeholder: f.auto ? '개인키 비밀번호' : '백업 비밀번호', style: 'width:150px', 'aria-label': `${f.name} 비밀번호` });
      const kf = f.auto ? h('input', { type: 'file', accept: '.pem', 'aria-label': `${f.name} 개인키 파일`, class: 'w-m' }) : null;
      return h('tr', {}, h('td', { class: 'mono' }, f.name), h('td', {}, f.auto ? chip('자동', 'info') : '수동'), h('td', {}, f.at), h('td', { class: 'num' }, `${(f.bytes / 1024 / 1024).toFixed(1)}MB`),
        h('td', {}, kf, vp, ' ', btn('열어 확인', wrap(async () => {
          const r = f.auto ? await (async () => { if (!kf.files[0]) throw new Error('개인키 파일(.pem)을 고르세요.'); return post('backups/verify-auto', { name: f.name, key: await kf.files[0].text(), password: vp.value }); })()
            : await post('backups/verify', { name: f.name, passphrase: vp.value });
          toast(`정상: 접수 ${r.requests}건이 들어 있습니다`); }, msg), 'small')));
    }) : [h('tr', {}, h('td', { colspan: 5, class: 'muted' }, '아직 백업이 없습니다.'))]));
    const pc = await api('photo-purge');
    purgeInfo.textContent = pc.count ? `정리 대상 ${pc.count}장 (${(pc.bytes / 1024 / 1024).toFixed(1)}MB)` : '정리 대상 없음';
  }, msg);
  load();
  return h('div', { class: 'card' }, h('h2', {}, '수동 백업·사진 보관'),
    h('p', { class: 'muted' }, '백업 파일은 별도 백업 비밀번호로 암호화됩니다. 비밀번호를 잃으면 열 수 없으니 책임자가 따로 보관하세요. 백업 파일은 승인된 보안 매체로만 옮깁니다.'),
    h('div', { class: 'row' }, pass, pass2, btn('지금 백업', wrap(async () => {
      if (pass.value !== pass2.value) throw new Error('두 번 입력한 백업 비밀번호가 다릅니다.');
      const r = await post('backup', { passphrase: pass.value }); pass.value = pass2.value = '';
      toast(`백업했습니다: ${r.name}`); load();
    }, msg), 'primary')),
    h('p', { class: 'small muted' }, '백업 폴더: ', dirEl),
    h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['파일', '종류', '만든 시각', '크기', '확인'].map((t) => h('th', {}, t)))), list)),
    h('p', { class: 'small muted' }, '복원은 PC 업무 화면을 끄고 명령으로 실행합니다: ', h('code', {}, 'python pc/pc_tool.py restore <백업 파일>'), ' (기존 DB는 지우지 않고 옆에 보관)'),
    h('hr'),
    h('div', { class: 'row' }, '회수한 사진 보관', days, '일', h('span', { class: 'grow' })),
    h('div', { class: 'row' }, '백업 폴더', bdir, btn('저장', wrap(async () => { await post('settings', { photo_retention_days: days.value, backup_dir: bdir.value }); toast('저장했습니다'); boot = null; render(); }, msg), 'small')),
    h('div', { class: 'row' }, purgeInfo, btn('기간 지난 사진 정리', wrap(async () => {
      if (!(await confirmDlg('기간 지난 사진 정리', `회수일로부터 ${st.photo_retention_days}일이 지나고 처리가 끝난 접수의 사진을 지웁니다. 기록(해시·시각)은 남습니다. 계속할까요?`, { ok: '정리', danger: true }))) return;
      const r = await post('photo-purge'); toast(`사진 ${r.count}장을 정리했습니다`); load();
    }, msg), 'small')),
    h('p', { class: 'small muted' }, '처리가 끝난 접수(수거 완료·취소 또는 그날 마감)의 사진만 정리합니다. 사진 원본은 지워지고 해시·회수 시각·정리 시각은 이력으로 남습니다.'), msg);
}

// ------------------------------------------------------------ 화면 설정: 테마(자동·밝게·어둡게)·글자 크기 — 사용자별로 서버에 저장
const THEME = { auto: '자동(시스템)', light: '밝게', dark: '어둡게' };
const FONT = { 13: '작게', 14: '보통', 15: '크게', 16: '아주 크게' };
function applyPrefs(p) {
  const root = document.documentElement;
  const t = (p && p.theme) || 'auto';
  if (t === 'auto') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', t);
  root.style.setProperty('--fs', `${(p && p.font) || 14}px`);
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

// ------------------------------------------------------------ 틀·라우팅
let renderSeq = 0;
async function render(fromNav) {
  // 작업 끝의 다시 그리기(fromNav 아님)인데 진행 중인 작업이 모두 다른 화면에서 시작했으면, 지금 화면을 덮지 않는다
  if (fromNav !== true && pendingActions.size && !pendingActions.has(location.hash)) return;
  const my = ++renderSeq;
  liveMaps.forEach((m) => { try { m.remove(); } catch {} }); liveMaps = [];
  let view;
  try {
    if (!boot) {
      const st = await api('state');
      if (!st.logged_in) { view = await screenLogin(); }
      else { boot = await api('bootstrap'); touchSession(); }
    }
    if (boot) {
      const [, route, arg] = (location.hash || '#/home').split('/');
      const role = boot.me.role;
      applyPrefs(boot.me.prefs);
      const screens = { home: screenHome, requests: screenRequests, request: () => screenRequest(arg), dispatch: screenDispatch, send: screenSend, results: screenResults, close: screenClose, qrtest: screenQrTest, fees: screenFees, stats: screenStats,
        settings: () => screenSettings(arg), users: screenUsers, me: screenMe, revenue: screenRevenue, refunds: screenRefunds, access: screenAccess };
      // 역할별 메뉴 (서버도 같은 기준으로 막는다)
      const NAV = [['home', '오늘 현황', 'ADMIN INTAKE FIELD LEAD'], ['requests', '접수', 'ADMIN INTAKE FIELD LEAD'], ['dispatch', '배정·노선', 'ADMIN INTAKE'], ['send', '목록 전송', 'ADMIN INTAKE'],
        ['results', '결과 가져오기', 'ADMIN INTAKE FIELD'], ['close', '일일 마감', 'ADMIN INTAKE FIELD LEAD'], ['revenue', '세입·대사', 'ADMIN INTAKE LEAD'], ['refunds', '환불', 'ADMIN INTAKE LEAD'],
        ['qrtest', 'QR 실측', 'ADMIN INTAKE FIELD'], ['stats', '통계·보고서', 'ADMIN INTAKE LEAD'], ['fees', '수수료표', 'ADMIN INTAKE FIELD LEAD'], ['settings', '설정', 'ADMIN'],
        ['users', role === 'ADMIN' ? '사용자·이력' : '변경 이력', 'ADMIN LEAD'], ['access', '접속기록', 'ADMIN LEAD']];
      const nav = NAV.filter(([, , r]) => r.split(' ').includes(role));
      let cur = route || 'home';
      if (boot.me.must_change) cur = 'me';
      if (!['request', 'me'].includes(cur) && !nav.some(([k]) => k === cur)) cur = 'home';
      const content = await (screens[cur] || screenHome)();
      const active = cur === 'request' ? 'requests' : cur;
      // 메뉴 묶음 (행정 업무 시스템처럼 업무 단위로 묶는다). 묶음 이름은 제목(heading)이 아니라 글자로만 둔다.
      const GROUPS = [['업무 현황', ['home']], ['접수 업무', ['requests']], ['배차·수거', ['dispatch', 'send', 'results']], ['정산·마감', ['close', 'revenue', 'refunds', 'stats']], ['관리', ['fees', 'qrtest', 'settings', 'users', 'access']]];
      const ICON = { home: '현', requests: '접', dispatch: '배', send: '전', results: '결', close: '마', revenue: '세', refunds: '환', stats: '통', fees: '수', qrtest: 'Q', settings: '설', users: '이', access: '기' };
      const navLabel = Object.fromEntries(NAV.map(([k, t]) => [k, t]));
      const group = GROUPS.find(([, ks]) => ks.includes(active));
      const pageName = cur === 'request' ? (arg === 'new' ? '전화 접수 등록' : '접수 상세') : cur === 'me' ? '내 계정' : navLabel[active];
      const subName = cur === 'settings' ? (SETTING_TABS.find(([k]) => k === arg) || SETTING_TABS[0])[1] : null;
      const crumbs = [group ? group[0] : '개인', ...(cur === 'request' ? [navLabel.requests] : []), pageName, ...(subName ? [subName] : [])];
      const d = new Date(boot.today + 'T00:00:00');
      const dayTxt = isNaN(d) ? boot.today : `${boot.today.replace(/-/g, '.')} (${'일월화수목금토'[d.getDay()]})`;
      const logout = async (e) => { e.preventDefault(); await post('logout').catch(() => {}); boot = null; intakeDraft = null; clearTimeout(sessTimer); document.querySelectorAll('.sess-warn').forEach((x) => x.remove()); render(); };
      const navOpen = render.navOpen || false;
      const toggleNav = () => { render.navOpen = !render.navOpen; const sh = $app.querySelector('.shell'); sh.classList.toggle('nav-open', render.navOpen); sh.querySelector('.menu-btn').setAttribute('aria-expanded', render.navOpen ? 'true' : 'false'); };
      const railBtn = () => { render.rail = !render.rail; try { localStorage.setItem('dg-rail', render.rail ? '1' : ''); } catch {} $app.querySelector('.shell').classList.toggle('rail', render.rail); };
      if (render.rail === undefined) { try { render.rail = localStorage.getItem('dg-rail') === '1'; } catch { render.rail = false; } }
      view = h('div', { class: 'shell' + (navOpen ? ' nav-open' : '') + (render.rail ? ' rail' : '') },
        h('header', { class: 'topbar' },
          h('button', { type: 'button', class: 'menu-btn', 'aria-label': '메뉴 열기·닫기', 'aria-expanded': navOpen ? 'true' : 'false', 'aria-controls': 'side-nav', onclick: toggleNav }, '☰'),
          h('a', { class: 'brand', href: '#/home' }, h('span', { class: 'logo', 'aria-hidden': 'true' }, '동'), h('span', { class: 'bt' }, h('strong', {}, '동구 대형폐기물 관리시스템'), h('small', {}, '접수·배차·수거 업무 (PC)'))),
          h('span', { class: 'grow' }),
          h('span', { class: 'tb-date' }, h('span', { class: 'sr-only' }, '업무 기준일 '), dayTxt),
          prefsControls(),
          h('a', { href: '#/me', class: 'who' + (active === 'me' ? ' on' : ''), title: '내 계정' }, h('b', {}, boot.me.display_name), h('small', {}, boot.me.role_label)),
          h('a', { href: '#', class: 'tb-logout', onclick: logout }, '로그아웃')),
        h('nav', { class: 'side', id: 'side-nav', 'aria-label': '업무 메뉴' },
          h('button', { type: 'button', class: 'rail-btn', onclick: railBtn, title: '메뉴 접기·펴기' }, h('span', { 'aria-hidden': 'true' }, '⇤'), h('span', { class: 'rail-t' }, ' 메뉴 접기')),
          ...GROUPS.map(([gt, ks]) => {
            const items = nav.filter(([k]) => ks.includes(k));
            return items.length ? h('div', { class: 'navgrp' + (group && group[0] === gt ? ' cur' : '') }, h('span', { class: 'navgrp-t', 'aria-hidden': 'true' }, gt),
              ...items.map(([k, t]) => h('a', { href: '#/' + k, class: active === k ? 'on' : '', 'aria-current': active === k ? 'page' : null, title: t, onclick: () => { render.navOpen = false; } },
                h('span', { class: 'ic', 'aria-hidden': 'true' }, ICON[k] || '·'), h('span', { class: 'nt' }, t)))) : null;
          })),
        h('main', {}, h('nav', { class: 'crumb', 'aria-label': '현재 위치' }, h('span', {}, '홈'), ...crumbs.map((c, i) => h('span', { 'aria-current': i === crumbs.length - 1 ? 'page' : null }, c))), content));
    }
  } catch (e) {
    // 화면 하나가 실패해도 메뉴는 남기고 그 자리에 안내와 다시 시도를 보여 준다
    const shell = $app.querySelector('.shell');
    const box = h('div', { class: 'notice bad', role: 'alert' }, `화면을 불러오지 못했습니다: ${e.message} `, btn('다시 시도', () => render(true), 'small'));
    if (shell && boot) { const m = shell.querySelector('main'); if (m && my === renderSeq) { m.replaceChildren(box); return; } }
    view = h('div', { class: 'pad' }, box);
  }
  if (my !== renderSeq) return;
  const prevHash = render.lastHash; render.lastHash = location.hash;
  $app.replaceChildren(view);
  const h1 = $app.querySelector('main h1, .login h1, .login strong');
  if (h1) document.title = `${h1.textContent} · 동구 대형폐기물`;
  if (prevHash !== location.hash && h1 && !document.querySelector('main [autofocus]')) { h1.tabIndex = -1; if (!$app.querySelector('main input:focus')) h1.focus({ preventScroll: true }); }
}
addEventListener('hashchange', () => render(true));
// 좁은 화면에서 연 메뉴는 Esc로 닫는다
addEventListener('keydown', (e) => { if (e.key === 'Escape' && render.navOpen && !dlgStack.length) { render.navOpen = false; const sh = $app.querySelector('.shell'); if (sh) sh.classList.remove('nav-open'); } });
// 단축키 (입력 칸 밖에서): / 찾기, N 새 접수 / 입력 중: Ctrl+Enter 저장
addEventListener('keydown', (e) => {
  if (!boot) return;
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '');
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { const b = document.querySelector('[data-save]'); if (b) { e.preventDefault(); b.click(); } return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '/') { const q = document.querySelector('main input[type=search]'); if (q) { e.preventDefault(); q.focus(); } }
  else if (e.key === 'n' || e.key === 'N' || e.key === 'ㅜ') { if (['ADMIN', 'INTAKE'].includes(boot.me.role)) { e.preventDefault(); go('#/request/new'); } }
});
render();
