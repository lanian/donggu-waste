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

const $app = document.getElementById('app');
let boot = null;       // 설정·팀·기기
let liveMaps = [];
const STATUS = { RECEIVED: '접수', ASSIGNED: '배정', IN_PROGRESS: '처리 중', PARTIAL: '일부 수거', DONE: '수거 완료', NOT_COLLECTED: '미수거', ON_HOLD: '보류', CANCELLED: '취소' };
const CONTACT = { NOT_CONTACTED: '연락 전', REACHED: '통화 완료', NO_ANSWER: '부재', CALLBACK: '재연락 예정', WRONG_NUMBER: '번호 오류' };
const PAY = { NONE: '해당 없음', AMOUNT_PENDING: '금액 확인 대기', PAID_AMOUNT_PENDING: '납부 기록 있음 · 금액 미확정', UNPAID: '미납', UNDERPAID: '부족', PAID: '납부 확인', OVERPAID_REVIEW: '과납 확인 필요' };
const REASON = { NOT_PUT_OUT: '배출 안 됨', DIFFERENT: '접수와 다름', CANNOT_LOAD: '적재 불가', POSTPONED: '주민 요청 연기', OTHER: '기타' };

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
const statusChip = (s) => chip(STATUS[s] || s, s === 'DONE' ? 'ok' : ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(s) ? 'warn' : s === 'CANCELLED' ? 'neutral' : ['IN_PROGRESS', 'ASSIGNED'].includes(s) ? 'info' : '');

async function api(path, { method = 'GET', body, raw } = {}) {
  const r = await fetch('/api/' + path, {
    method, credentials: 'same-origin',
    headers: { 'X-DG-CSRF': '1', ...(raw ? { 'Content-Type': 'application/octet-stream' } : body ? { 'Content-Type': 'application/json' } : {}) },
    body: raw || (body ? JSON.stringify(body) : undefined),
  });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && data.login) { boot = null; render(); throw new Error('로그인이 필요합니다'); }
  if (!r.ok) throw new Error(data.error || '오류');
  return data;
}
const post = (path, body) => api(path, { method: 'POST', body });
// 작업이 끝나 다시 그릴 때, 그 사이 다른 화면으로 옮겼으면 새 화면(입력 중인 양식)을 덮지 않는다
let actionHash = null;
const wrap = (fn, msgEl) => async (...a) => {
  const prev = actionHash; actionHash = location.hash;
  try { if (msgEl) { msgEl.textContent = ''; msgEl.className = 'msg'; } await fn(...a); }
  catch (e) { if (msgEl) { msgEl.textContent = e.message; msgEl.className = 'err'; } else toast(e.message, true); }
  finally { actionHash = prev; }
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
    h('div', { class: 'brand' }, h('span', { class: 'logo' }, '동'), h('div', {}, h('strong', {}, '동구 대형폐기물'), h('small', {}, 'PC 업무 화면'))),
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
      }, msg), 'primary'), msg));
}

// ------------------------------------------------------------ 사용자·변경 이력 (운영 관리자)
const ROLE = { ADMIN: '운영 관리자', INTAKE: '접수 담당자', FIELD: '수거팀' };
const ACTION = { CREATE: '등록', UPDATE: '수정', CANCEL: '취소', LOCATION: '위치 지정', ASSIGN: '배정', LOGIN: '로그인', LOGIN_FAIL: '로그인 실패', PASSWORD: '비밀번호 변경',
  ACTIVE: '사용', DEACTIVATE: '중지', IMPORT: '가져오기', RESOLVE: '충돌 처리', VOID: '정정', CLOSE: '마감', RECEIPT: '수신 확인', BACKUP: '백업', RESTORE: '복원', PURGE: '사진 정리',
  CONFIRM: '배정 확정', PRINT: '배차표 인쇄', DISPLAY: 'QR 표시', SAVE: '저장', REGISTER: '휴대폰 등록', STATUS: '상태 변경', RESOLVE_APPLY: '충돌 반영', RESOLVE_REJECT: '충돌 거부',
  APPROVE: '승인', REJECT: '반려', DRAFT: '개정안 작성', RESULT: '결과 반영', SOLO: '단독 자료 옮김', EXPORT: '내보내기', LINK_SIMILAR: '유사 품목 적용', UNLINK_SIMILAR: '유사 품목 해제',
  REVISIT_APPT: '재방문 약속', ADD: '추가', CREW: '팀 연결', OVERRIDE: '보정값', PAY: '납부', REFUND: '환불' };
const ENTITY = { request: '접수', payment: '수납', batch: '배정', bundle: '목록 전송', import: '결과 가져오기', photo: '사진', photo_import: '사진 가져오기', event: '충돌',
  day: '마감', device: '휴대폰', crew: '수거팀', user: '사용자', backup: '백업', fee: '수수료표', address: '주소 자료', report: '보고서', unload: '하차', volume: '적재 보정' };
const KEY = { crew: '팀', date: '날짜', ver: '갱신본', n: '건수', count: '건수', reason: '사유', role: '역할', username: '아이디', status: '상태', source: '출처', adjusted: '조정',
  rev_no: '개정', effective_from: '시행일', rows: '행', price: '단가', fee_rate_id: '품목', confirmed_by: '확인자', file: '파일', bytes: '크기', start: '시작', end: '끝', amount: '금액',
  kind: '구분', method: '방법', applied: '반영', conflict: '충돌', skipped_duplicate: '중복', stops: '지점', open_items: '미처리', retention_days: '보관 일수', line: '품목 행' };
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
  const utable = h('table', {}, h('thead', {}, h('tr', {}, ...['아이디', '이름', '역할', '팀', '상태', '마지막 로그인', ''].map((t) => h('th', {}, t)))),
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
  // 변경 이력
  const fEntity = h('select', {}, h('option', { value: '' }, '전체'), ...[['request', '접수'], ['payment', '수납'], ['assignment', '배정'], ['bundle', '전송'], ['result', '결과'], ['photo', '사진'], ['conflict', '충돌'], ['day', '마감'], ['device', '기기'], ['crew', '수거팀'], ['user', '사용자'], ['backup', '백업']].map(([v, t]) => h('option', { value: v }, t)));
  const fDate = h('input', { type: 'date', value: boot.today }), fActor = h('select', {}, h('option', { value: '' }, '전체 작업자'), h('option', { value: 'system' }, 'system'), ...users.map((u) => h('option', { value: u.username }, u.username)));
  const tbody = h('tbody');
  const load = wrap(async () => {
    const list = await api(`audit?entity=${fEntity.value}&date=${fDate.value}&actor=${encodeURIComponent(fActor.value)}`);
    tbody.replaceChildren(...(list.length ? list.map((a) => h('tr', {}, h('td', { class: 'nowrap' }, kst(a.at)), h('td', {}, a.actor || '—'), h('td', {}, ENTITY[a.entity] || a.entity), h('td', {}, ACTION[a.action] || a.action),
      h('td', { class: 'mono' }, a.entity_id ? a.entity_id.slice(0, 8) : ''), h('td', { class: 'detail' }, summarizeDetail(a.detail)))) : [h('tr', {}, h('td', { colspan: 6, class: 'muted' }, '기록이 없습니다.'))]));
  });
  [fEntity, fDate, fActor].forEach((x) => x.addEventListener('change', load));
  load();
  return h('div', {}, h('h1', {}, '사용자·변경 이력'),
    h('div', { class: 'card' }, h('h2', {}, '사용자'), utable,
      h('div', { class: 'row', style: 'margin-top:12px' }, F.username, F.display_name, F.role, F.crew, F.password,
        btn('사용자 추가', wrap(async () => { if (F.role.value === 'FIELD' && !F.crew.value) throw new Error('수거팀 계정은 팀을 골라 주세요.'); await post('users', { username: F.username.value, display_name: F.display_name.value, role: F.role.value, password: F.password.value, crew_id: F.crew.value }); toast('추가했습니다. 첫 로그인 때 비밀번호를 바꾸게 됩니다.'); render(); }, msg), 'primary')),
      h('p', { class: 'muted' }, '접수 담당자: 접수·배정·전송·결과·수납·마감. 수거팀: 연결한 팀에 배정된 접수만 조회, 그 팀 휴대폰의 결과·사진 가져오기, 그 팀 하차 기록. 운영 관리자: 전체와 설정·사용자·백업. 계정은 지우지 않고 중지합니다.'), msg),
    h('div', { class: 'card' }, h('h2', {}, '변경 이력'), h('div', { class: 'row' }, '날짜', fDate, '대상', fEntity, fActor, btn('새로 고침', load, 'small')),
      h('div', { style: 'overflow:auto;max-height:520px' }, h('table', { class: 'audit' }, h('thead', {}, h('tr', {}, ...['시각', '작업자', '대상', '작업', '번호', '내용'].map((t) => h('th', {}, t)))), tbody)),
      h('p', { class: 'muted' }, '이력은 지우거나 고칠 수 없습니다. 내용에는 이름·연락처·주소를 넣지 않습니다(사유·건수·코드만).')));
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
  const [sum, batches] = await Promise.all([api('close?date=' + s.date), api('batches?date=' + s.date)]);
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
  ].filter(([n, , , , r]) => n && r.split(' ').includes(role));
  return h('div', {}, h('div', { class: 'pagehead' }, h('h1', {}, '오늘 현황'), h('div', { class: 'row' }, date, sum.closed ? chip('마감됨', 'ok') : chip('마감 전'))),
    h('div', { class: 'kpis' },
      kpi('수거 예정', `${active}건`, (bs.CANCELLED ? `취소 ${bs.CANCELLED}건 제외` : ' '), '', '#/requests'),
      kpi('수거 완료', `${done}건`, h('span', { class: 'bar' }, h('i', { style: `width:${pct}%` })), 'ok'),
      kpi('진행·대기', `${open}건`, bs.IN_PROGRESS ? `처리 중 ${bs.IN_PROGRESS}` : ' ', ''),
      kpi('재방문·보류', `${revisit}건`, bs.NOT_COLLECTED ? `미수거 ${bs.NOT_COLLECTED}` : ' ', revisit ? 'warn' : ''),
      kpi('하차 기록', `${sum.unload_total.volume_m3}m³`, sum.unloads.length ? `${sum.unloads.length}회 · ${sum.unload_total.weight_kg}kg` : '기록 없음', '')),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('h2', {}, '처리할 일'),
        todo.length ? h('ul', { class: 'todo' }, ...todo.map(([, t, href, cls]) => h('li', {}, h('a', { href, class: 'todo-a ' + cls }, h('span', {}, t), h('span', { class: 'arr' }, '→')))))
          : h('p', { class: 'empty' }, sum.total ? '지금 처리할 일이 없습니다.' : '이 날짜의 접수가 없습니다.')),
      h('div', { class: 'card' }, h('h2', {}, '팀별 진행'),
        batches.filter((b) => b.status === 'ACTIVE').length ? h('div', { class: 'crews' }, ...batches.filter((b) => b.status === 'ACTIVE').map((b) => {
          const n = b.stops.length, d = b.stops.filter((x) => x.status === 'DONE').length, w = b.stops.filter((x) => ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(x.status)).length;
          const last = b.bundles[0];
          return h('div', { class: 'crew' }, h('div', { class: 'row', style: 'justify-content:space-between' }, h('b', {}, b.crew_name), h('span', { class: 'muted' }, `갱신본 ${b.assign_ver}`)),
            h('div', { class: 'seg' }, h('i', { class: 'ok', style: `flex:${d}` }), h('i', { class: 'warn', style: `flex:${w}` }), h('i', { class: 'rest', style: `flex:${Math.max(0, n - d - w)}` })),
            h('div', { class: 'row small' }, `${n}곳 중 완료 ${d} · 재방문 ${w}`, h('span', { class: 'grow' }),
              !last ? chip('미전송', 'warn') : last.receipt_confirmed_at ? chip('수신 확인', 'ok') : chip('수신 미확인', 'warn'), b.resend_needed ? chip('재전송 필요', 'bad') : null));
        })) : h('p', { class: 'empty' }, '확정된 배정이 없습니다.', role !== 'FIELD' ? h('a', { href: '#/dispatch' }, ' 배정·노선으로 →') : null))),
    h('p', { class: 'muted small' }, '현황은 이 PC에 가져온 결과 기준입니다. 휴대폰에서 아직 보내지 않은 결과는 반영되지 않습니다.'));
}

async function screenRequests() {
  const f = (screenRequests.f ||= { q: '', date: '', status: 'OPEN', quick: 'ALL' });
  const q = h('input', { type: 'search', placeholder: '이름·연락처·주소·접수번호', value: f.q });
  const date = h('input', { type: 'date', value: f.date });
  const status = h('select', {}, ...[['OPEN', '미완료 전체'], ['', '전체'], ...Object.entries(STATUS)].map(([v, t]) => h('option', { value: v, selected: v === f.status }, t)));
  const tbody = h('tbody');
  const quickBar = h('div', { class: 'quick' });
  const QUICK = [['ALL', '전체', () => true], ['TODAY', '오늘 예정', (r) => r.planned_date === boot.today], ['NOLOC', '위치 없음', (r) => r.lat == null],
    ['FEE', '금액 확인 대기', (r) => r.state.fee.final == null], ['UNASSIGNED', '미배정', (r) => !r.assigned_to && !['DONE', 'CANCELLED'].includes(r.status)]];
  let all = [];
  const draw = () => {
    quickBar.replaceChildren(...QUICK.map(([k, t, fn]) => h('button', { type: 'button', class: 'qchip' + (f.quick === k ? ' on' : ''), 'aria-pressed': f.quick === k ? 'true' : 'false',
      onclick: () => { f.quick = k; draw(); } }, t, h('b', {}, all.filter(fn).length))));
    const list = all.filter(QUICK.find(([k]) => k === f.quick)[2]);
    const shortDate = (d) => (!d ? '-' : d === boot.today ? '오늘' : d.slice(5));
    tbody.replaceChildren(...(list.length ? list.map((r) => h('tr', { class: 'click', onclick: (e) => { if (e.target.tagName !== 'A') go(`#/request/${r.id}`); } },
      h('td', { class: 'code' }, h('a', { href: `#/request/${r.id}`, title: r.receipt_no }, r.receipt_no.replace(/^DG-\d{4}/, ''))), h('td', { class: 'code' }, shortDate(r.planned_date)), h('td', { class: 'nowrap' }, r.name), h('td', { class: 'code' }, fmtPhone(r.phone)),
      h('td', {}, h('span', { class: 'clamp' }, r.address)),
      h('td', {}, h('span', { class: 'clamp' }, r.items.map((i) => `${i.name} ${i.spec} ${i.qty}${i.unit_label}`).join(', '))),
      h('td', {}, r.status !== 'RECEIVED' ? statusChip(r.status) : null, r.state.contact !== 'NOT_CONTACTED' ? chip(CONTACT[r.state.contact]) : null, r.must_today ? chip('오늘 필수', 'warn') : null, (r.warnings || []).length ? h('span', { class: 'chip bad', title: r.warnings.join(' ') }, '관할 확인') : null),
      h('td', {}, r.state.fee.final == null ? chip('금액 확인 대기', 'warn') : won(r.state.fee.final)),
      h('td', {}, r.lat == null ? chip('위치 없음', 'warn') : '✓'),
      h('td', {}, r.assigned_to ? `${r.assigned_to.name} ${r.assigned_to.work_date}` : ''))) : [h('tr', {}, h('td', { colspan: 10, class: 'muted' }, f.q ? `'${f.q}'에 맞는 접수가 없습니다. 초성(예: ㄱㅅ)이나 전화번호 뒷자리로도 찾을 수 있습니다.` : '해당 접수가 없습니다. ', !f.q && boot.me.role !== 'FIELD' ? h('a', { href: '#/request/new' }, '전화 접수 등록 →') : null))]));
  };
  const load = wrap(async () => {
    Object.assign(f, { q: q.value, date: date.value, status: status.value });
    all = await api(`requests?q=${encodeURIComponent(f.q)}&date=${f.date}&status=${f.status}`);
    draw();
  });
  let t = null;
  q.addEventListener('input', () => { clearTimeout(t); t = setTimeout(load, 300); });
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter') { clearTimeout(t); load(); } });
  [date, status].forEach((x) => x.addEventListener('change', load));
  load();
  setTimeout(() => { if (!document.activeElement || document.activeElement === document.body) q.focus(); }, 30);
  return h('div', {}, h('div', { class: 'pagehead' }, h('h1', {}, '접수'), boot.me.role !== 'FIELD' ? btn('+ 전화 접수 등록', () => go('#/request/new'), 'primary') : null),
    h('div', { class: 'card filters' }, h('div', { class: 'row' }, q, date, status, btn('검색', load), h('span', { class: 'muted small' }, '단축키: / 찾기 · N 새 접수')), quickBar),
    h('div', { class: 'card tablecard' }, h('table', {},
      h('thead', {}, h('tr', {}, ...['접수번호', '예정일', '이름', '연락처', '배출 주소', '품목', '상태', '금액', '위치', '배정'].map((t) => h('th', {}, t)))), tbody)));
}

// ------------------------------------------------------------ 접수 등록·수정
async function screenRequest(id) {
  const isNew = id === 'new';
  const r = isNew ? { name: '', phone: '', address: '', spot_note: '', memo: '', planned_date: '', appt_start: '', appt_end: '', must_today: 0, urgent: 0, priority: 0, lat: null, lon: null, items: [], versions: [], locked_lines: [], status: 'RECEIVED' } : await api('requests/' + id);
  const items = r.items.map((i) => ({ ...i }));
  const locked = new Set(r.locked_lines || []);
  const inp = (k, attrs = {}) => h('input', { value: r[k] ?? '', ...attrs });
  const F = { name: inp('name'), phone: inp('phone', { inputmode: 'tel', placeholder: '010-0000-0000', value: fmtPhone(r.phone) }), address: inp('address'), spot_note: inp('spot_note', { placeholder: '예: 101동 분리수거장 옆' }),
    memo: h('textarea', {}, r.memo || ''), planned_date: inp('planned_date', { type: 'date' }), appt_start: inp('appt_start', { type: 'time' }), appt_end: inp('appt_end', { type: 'time' }),
    must_today: h('input', { type: 'checkbox', checked: r.must_today }), urgent: h('input', { type: 'checkbox', checked: r.urgent }),
    priority: h('select', {}, ...[[0, '보통'], [1, '높음'], [2, '매우 높음']].map(([v, t]) => h('option', { value: v, selected: +r.priority === v }, t))),
    lat: inp('lat', { readonly: true, style: 'width:110px' }), lon: inp('lon', { readonly: true, style: 'width:120px' }) };
  F.phone.addEventListener('blur', () => { const d = F.phone.value.replace(/\D/g, ''); if (d.length === 11) F.phone.value = fmtPhone(d); });
  const lab = (t, el) => h('label', { class: 'f' }, t, el);
  const itemsBox = h('div', { class: 'items' });
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
  };
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
    priority: +F.priority.value, lat: F.lat.value || null, lon: F.lon.value || null, geo_source: geoSource,
    items: items.map((i) => ({ line_id: i.line_id, fee_rate_id: i.fee_rate_id || null, name: i.name, spec: i.spec, unit: i.unit, unit_label: i.unit_label, qty: i.qty })) });
  let dupAck = false;
  const save = wrap(async () => {
    if (isNew) {
      if (!dupAck) {
        await checkDup();
        if (dupBox.childNodes.length) { dupAck = true; msg.textContent = '비슷한 접수가 있습니다. 별개 접수가 맞으면 [접수 등록]을 한 번 더 누르세요.'; return; }
      }
      const res = await post('requests', data());
      toast(`${res.receipt_no} 등록했습니다`); (res.warnings || []).forEach((w) => toast(w, true)); go(`#/request/${res.id}`);
    } else {
      const res = await post('requests/' + id, { ...data(), reason: reason.value });
      toast(res.resend_needed ? '수정했습니다. 이미 배정된 건이라 목록 재전송이 필요합니다.' : '수정했습니다'); (res.warnings || []).forEach((w) => toast(w, true)); render();
    }
  }, msg);
  const cancel = wrap(async () => {
    const why = prompt('취소 사유를 적어 주세요 (기록은 지워지지 않습니다)');
    if (!why) return;
    const res = await post(`requests/${id}/cancel`, { reason: why });
    toast(res.field_notice_needed ? '취소했습니다. 이미 휴대폰에 전달된 건이니 수거팀에 전화로 알리세요.' : '취소했습니다'); render();
  }, msg);
  const editable = r.status !== 'CANCELLED';

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
    h('table', {}, h('thead', {}, h('tr', {}, ...['품목', '구분', '처음 접수', '현장 수량', '실은 수량', '못 실은 수량', '사유', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...st.lines.flatMap((l) => [h('tr', {}, h('td', {}, `${l.name} ${l.spec}`,
          l.link ? h('div', { class: 'small muted' }, `유사 품목 적용: ${l.link.name} ${l.link.spec} ${won(l.price)}/${l.unit_label} · 근거 ${l.link.reason} · 확인 ${l.link.confirmed_by}`) : null),
        h('td', {}, l.kind === 'FIELD' ? chip('현장 추가', 'info') : '처음 접수'),
        h('td', {}, l.qty_received), h('td', {}, l.qty_confirmed), h('td', {}, l.qty_collected), h('td', {}, l.remaining ? chip(`${l.remaining}${l.unit_label}`, 'warn') : '0'), h('td', {}, REASON[l.reason] || ''),
        h('td', {}, l.price == null && editable && boot.me.role !== 'FIELD' ? btn('유사 품목 적용', () => { linkOpen = linkOpen === l.line_id ? null : l.line_id; linkBox(l); }, 'small') : null,
          l.link && editable && boot.me.role !== 'FIELD' ? btn('해제', wrap(async () => { const why = prompt('유사 품목 적용을 해제하는 사유'); if (!why) return; await post(`links/${l.link.id}/void`, { reason: why }); toast('해제했습니다'); render(); }), 'small') : null)),
        h('tr', { class: 'linkrow', hidden: true, 'data-line': l.line_id }, h('td', { colspan: 8 }))]))),
    h('dl', { class: 'kv' }, h('dt', {}, '접수 당시 금액'), h('dd', {}, won(st.fee.received)), h('dt', {}, '현장 추가분'), h('dd', {}, won(st.fee.field_added)),
      h('dt', {}, '접수 대비 증감'), h('dd', {}, (st.fee.diff > 0 ? '+' : '') + won(st.fee.diff)), h('dt', {}, '금액 확인 대기'), h('dd', {}, st.fee.pending_count + '건'),
      h('dt', {}, '최종 금액'), h('dd', {}, st.fee.final == null ? '미확정' : won(st.fee.final))),
    h('p', { class: 'muted' }, '금액은 현장 확인 수량 기준이며 수납 완료를 뜻하지 않습니다. 일부 수거로 금액을 자동 감액하지 않습니다.'),
    (st.memos || []).length ? h('div', { class: 'notice' }, h('strong', {}, '현장 메모'), h('ul', {}, ...st.memos.map((m) => h('li', {}, m)))) : null,
    r.photos && r.photos.length ? h('div', { class: 'thumbs' }, ...r.photos.map((p) => p.purged_at ? chip(`보관 기간 만료로 정리됨 (${p.purged_at.slice(0, 10)})`) : p.recovery_status === 'RECOVERED' ? h('figure', { class: 'ph' }, photoImg(p.photo_id), h('figcaption', {}, (p.phase === 'BEFORE' ? '수거 전' : '수거 후') + (p.line_id && st.lines.find((l) => l.line_id === p.line_id) ? ` · ${st.lines.find((l) => l.line_id === p.line_id).name}` : ''))) : chip(`사진 미회수 (${p.phase === 'BEFORE' ? '전' : '후'})`, 'warn'))) : null) : null;

  // 전화 받는 순서대로: 왼쪽 = 신청자 → 버릴 물건, 오른쪽 = 일정 → 위치. 저장 단추는 화면 아래에 늘 보이게.
  F.lat.tabIndex = -1; F.lon.tabIndex = -1;
  const actionbar = editable ? h('div', { class: 'actionbar' }, isNew ? null : reason,
    btn(isNew ? '접수 등록' : '수정 저장', save, 'primary', { 'data-save': '1' }), h('span', { class: 'muted small' }, h('kbd', {}, 'Ctrl'), '+', h('kbd', {}, 'Enter')),
    barTotal, h('span', { class: 'grow' }), msg, isNew ? null : btn('접수 취소', cancel, 'danger')) : null;
  return h('div', { class: 'reqform' },
    h('div', { class: 'pagehead' }, h('h1', {}, isNew ? '전화 접수 등록' : `접수 ${r.receipt_no}`), btn('← 목록', () => go('#/requests'))),
    r.status === 'CANCELLED' ? h('div', { class: 'notice' }, '취소된 접수입니다. 사유: ' + (r.cancel_reason || '-')) : null,
    h('div', { class: 'grid2' },
      h('div', {},
        h('div', { class: 'card' }, h('h2', {}, '신청자'),
          h('div', { class: 'grid2' }, lab('이름', F.name), lab('연락처', F.phone)), lab('배출 주소', F.address), lab('상세 배출 위치', F.spot_note), lab('요청 메모', F.memo), dupBox),
        h('div', { class: 'card' }, h('h2', {}, '버릴 물건'), itemsBox, total, editable ? picker : null)),
      h('div', {},
        h('div', { class: 'card' }, h('h2', {}, '일정·우선순위'),
          h('div', { class: 'grid3' }, lab('수거 예정일', F.planned_date), lab('방문 약속 시작', F.appt_start), lab('방문 약속 끝', F.appt_end)),
          h('div', { class: 'row checks' }, h('label', { class: 'row' }, F.must_today, '오늘 반드시 포함'), h('label', { class: 'row' }, F.urgent, '긴급'), lab('품목 우선순위', F.priority)),
          h('p', { class: 'muted' }, '오전에 먼저 가야 하면 방문 약속 시간을 넣으세요. 우선순위는 "포함 여부"에만 영향을 줍니다.')),
        h('div', { class: 'card' }, h('h2', {}, '배출 위치'), h('p', { class: 'muted' }, '이 PC에 넣어 둔 도로명주소 위치 자료로 찾거나 지도를 눌러 지정합니다. 주소는 외부로 보내지 않습니다.'),
          h('div', { class: 'row' }, btn('주소로 위치 찾기', findAddr, 'small'), geoMsg),
          mapBox, h('div', { class: 'row' }, '위도', F.lat, '경도', F.lon)))),
    actionbar,
    field,
    !isNew ? paymentCard(r) : null,
    !isNew && r.versions.length ? h('div', { class: 'card' }, h('h2', {}, '수정 이력'), h('ul', {}, ...r.versions.map((v) => h('li', {}, `버전 ${v.req_ver} → ${v.req_ver + 1} · ${kst(v.at)} · ${v.reason}`)))) : null);
}

// 수납: 담당자가 확인한 납부 기록. 계산 금액·납부 금액·상태를 따로 보여 주고, 정정은 지우지 않고 사유를 남긴다
function paymentCard(r) {
  const p = r.payment;
  const msg = h('span', { class: 'msg' });
  const kind = h('select', {}, h('option', { value: 'PAY' }, '납부'), h('option', { value: 'REFUND' }, '환불'));
  const remain = p.calculated != null ? Math.max(0, p.calculated - p.paid) : '';
  const amount = h('input', { type: 'number', min: 1, value: remain || '', placeholder: '금액(원)', style: 'width:120px' });
  const method = h('select', {}, ...['현장 납부', '무통장 입금', '기타'].map((m) => h('option', { value: m }, m)));
  const paidOn = h('input', { type: 'date', value: boot.today });
  const note = h('input', { placeholder: '메모(입금자명 등)', style: 'flex:1' });
  const cls = { PAID: 'ok', UNPAID: 'bad', UNDERPAID: 'warn', OVERPAID_REVIEW: 'warn', AMOUNT_PENDING: 'warn', PAID_AMOUNT_PENDING: 'warn' }[p.status] || '';
  return h('div', { class: 'card' }, h('h2', {}, '수납 기록 ', chip(PAY[p.status], cls)),
    h('dl', { class: 'kv' }, h('dt', {}, '계산 금액'), h('dd', {}, p.calculated == null ? '미확정 (금액 확인 대기 품목 있음)' : won(p.calculated)),
      h('dt', {}, '납부 확인 금액'), h('dd', {}, won(p.paid))),
    p.records.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['날짜', '구분', '금액', '방법', '메모', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...p.records.map((x) => h('tr', { class: x.void_reason ? 'muted' : '' }, h('td', {}, x.paid_on), h('td', {}, x.kind === 'PAY' ? '납부' : '환불'),
        h('td', {}, x.void_reason ? h('s', {}, won(x.amount)) : won(x.amount)), h('td', {}, x.method), h('td', {}, x.note || '', x.void_reason ? chip('정정: ' + x.void_reason) : null),
        h('td', {}, x.void_reason ? '' : btn('정정', wrap(async () => { const why = prompt('정정 사유 (기록은 지워지지 않습니다)'); if (!why) return; await post(`payments/${x.id}/void`, { reason: why }); render(); }, msg), 'small')))))) : null,
    h('div', { class: 'row' }, kind, amount, method, paidOn, note, btn('수납 기록', wrap(async () => {
      await post(`requests/${r.id}/payments`, { kind: kind.value, amount: amount.value, method: method.value, paid_on: paidOn.value, note: note.value }); toast('수납을 기록했습니다'); render();
    }, msg), 'primary'), msg),
    h('p', { class: 'muted' }, '계산 금액은 현장 확인 수량 기준입니다. 일부 수거로 금액을 자동으로 줄이거나 환불을 확정하지 않습니다. 과납·부족은 담당자가 확인해 기록합니다.'));
}

function photoImg(pid) {
  const img = h('img', { alt: '현장 사진' });
  fetch('/api/photo/' + pid, { headers: { 'X-DG-CSRF': '1' }, credentials: 'same-origin' }).then((r) => r.ok && r.blob()).then((b) => { if (b) img.src = URL.createObjectURL(b); });
  return img;
}

// ------------------------------------------------------------ 배정·노선
async function screenDispatch() {
  const s = (screenDispatch.s ||= { date: boot.today, crews: new Set(), seen: new Set(), plan: null });
  const date = h('input', { type: 'date', value: s.date });
  const crews = boot.crews.filter((c) => c.active);
  crews.forEach((c) => { if (!s.seen.has(c.id)) { s.seen.add(c.id); s.crews.add(c.id); } });  // 새로 만든 팀은 자동으로 포함
  const calcBtn = btn('노선 계산', null, 'primary');
  const crewHint = h('span', { class: 'muted small' });
  const syncCalc = () => { const none = ![...s.crews].some((id) => crews.some((c) => c.id === id)); calcBtn.disabled = none; crewHint.textContent = none ? '팀을 하나 이상 고르세요.' : ''; };
  const crewBox = h('div', { class: 'row' }, ...crews.map((c) => h('label', { class: 'row' }, h('input', { type: 'checkbox', checked: s.crews.has(c.id), onchange: (e) => { e.target.checked ? s.crews.add(c.id) : s.crews.delete(c.id); syncCalc(); } }),
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
    if (!p) { out.replaceChildren(h('p', { class: 'muted' }, '날짜와 팀을 고르고 [노선 계산]을 누르세요. 결과는 추천안이며 순서를 바꿔 확정할 수 있습니다.')); return; }
    const R = p.res.requests;
    const clearUnload = (ri) => { if (p.routes[ri].stops.some((x) => x.unload_after)) { p.routes[ri].stops.forEach((x) => delete x.unload_after); p.routes[ri].unloadStale = true; } };
    const move = (ri, i, d) => { const a = p.routes[ri].stops; const j = i + d; if (j < 0 || j >= a.length) return; [a[i], a[j]] = [a[j], a[i]]; clearUnload(ri); p.adjusted = true; draw(); };
    const toCrew = (ri, i, target) => { p.adjusted = true; clearUnload(ri); if (target !== 'OUT') clearUnload(+target); const [x] = p.routes[ri].stops.splice(i, 1); if (target === 'OUT') p.unassigned.push({ request_id: x.request_id, reason: '담당자가 제외' }); else p.routes[+target].stops.push({ request_id: x.request_id, eta: '' }); draw(); };
    const cols = p.routes.map((rt, ri) => h('div', { class: `card route c${(ri % 4) + 1}` }, h('h2', {}, h('span', { class: 'swatch', 'aria-hidden': 'true' }), `${rt.crew_name} · ${rt.stops.length}곳`, rt.end ? h('span', { class: 'muted' }, ` 예상 종료 ${rt.end}`) : null, rt.unloads ? chip(`반입지 경유 ${rt.unloads}회`) : null),
      rt.unloadStale ? h('div', { class: 'notice warn small' }, '순서를 바꿔 반입지 경유 위치가 지워졌습니다. 적재가 넘치지 않는지 확인하거나 [노선 계산]을 다시 하세요.') : null,
      ...rt.stops.flatMap((x, i) => [h('div', { class: 'stopline' }, h('span', { class: 's' }, i + 1),
        h('span', { class: 't' }, `${R[x.request_id].name} · ${R[x.request_id].address}`, h('br'), h('span', { class: 'muted' }, `${x.eta ? '도착 ' + x.eta : '시간 재계산 필요'}${x.load_m3 != null ? ` · 누적 적재(추정) ${x.load_m3}m³` : ''}${R[x.request_id].appt[0] ? ` · 약속 ${R[x.request_id].appt[0]}~${R[x.request_id].appt[1] || ''}` : ''}`),
          R[x.request_id].must_today ? chip('오늘 필수', 'warn') : null, x.same_place > 1 ? chip(`같은 장소 ${x.same_place}건`) : null),
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
    const summary = p.routes.map((rt) => `${rt.crew_name} ${rt.stops.length}곳`).join(' · ') + (p.unassigned.length ? ` · 미배정 ${p.unassigned.length}건` : '');
    out.replaceChildren(h('div', { class: 'notice' }, `이동시간: ${(p.res.matrix_note || '').replace('로컬 OSRM', 'PC 안의 도로 자료') || '계산할 지점 없음'}`, p.res.unload_site ? ` · 적재가 차면 ${p.res.unload_site.label || '반입지'}에 들러 비움` : ' · 반입지 좌표가 없어 차량 1회 적재 안에서만 계산 (설정에서 지정)', p.res.end_site ? ` · 마지막에 ${p.res.end_site.label || '종료지'}로` : '', p.res.infeasible ? h('strong', {}, ' · 해를 찾지 못했습니다. 차량·근무시간·필수 포함 조건을 조정하세요.') : null),
      h('div', { class: 'dispatch-grid' }, h('div', {}, h('div', { class: 'cols' }, ...cols), un), h('div', { class: 'mapcol' }, h('div', { class: 'card' }, h('h2', {}, '지도'), mapBox,
        h('p', { class: 'muted' }, '선은 방문 순서를 잇는 직선이며 실제 도로가 아닙니다. 핀 위치와 이름은 지도 서버로 보내지 않습니다.')))),
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
        const whenStyle = (f) => (map.isStyleLoaded() ? f() : map.once('style.load', f));
        whenStyle(() => {
          map.addSource('routes', { type: 'geojson', data: { type: 'FeatureCollection', features: lines } });
          map.addLayer({ id: 'routes', type: 'line', source: 'routes', paint: { 'line-width': 3, 'line-dasharray': [2, 1], 'line-color': ['match', ['get', 'c'], 0, '#2557a7', 1, '#a5560d', 2, '#6c3fb5', '#0e7490'] } });
          map.fitBounds(b, { padding: 50, maxZoom: 16, duration: 0 });
        });
      } catch { mapBox.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다.')); }
    }, 0);
  };
  draw();
  return h('div', {}, h('h1', {}, '배정·노선'),
    h('div', { class: 'card' }, h('div', { class: 'row' }, h('label', { class: 'f' }, '수거일', date), calcBtn), h('h3', {}, '팀'), crewBox,
      crews.length ? null : h('p', { class: 'err' }, '설정에서 수거팀을 먼저 만드세요.'), msg),
    out);
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
  return h('div', {}, h('h1', {}, '목록 전송 (QR)'), h('div', { class: 'card row' }, h('label', { class: 'f' }, '수거일', date),
    h('p', { class: 'muted' }, 'QR 화면 표시와 휴대폰 수신은 다릅니다. 휴대폰이 보여 주는 확인 코드를 입력해야 "수신 확인"이 됩니다.')), body);
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
  const conf = h('div', { class: 'card' });
  const loadConf = wrap(async () => {
    const list = await api('conflicts');
    conf.replaceChildren(h('h2', {}, `충돌함 ${list.length}건`), list.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['접수', '종류', '내용', '사유', '처리'].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...list.map((c) => {
        const why = h('input', { placeholder: '판단 사유', style: 'width:180px' });
        return h('tr', {}, h('td', {}, `${c.receipt_no || '-'} ${c.name || ''}`, h('br'), statusChip(c.request_status)), h('td', {}, c.kind), h('td', { class: 'muted' }, c.body_json.slice(0, 120)),
          h('td', {}, c.conflict_reason), h('td', {}, why, ' ', btn('반영', wrap(async () => { await post('conflicts/' + c.event_id, { action: 'APPLY', reason: why.value }); toast('반영했습니다'); loadConf(); }), 'small'),
            ' ', btn('거부', wrap(async () => { await post('conflicts/' + c.event_id, { action: 'REJECT', reason: why.value }); toast('거부로 기록했습니다'); loadConf(); }), 'small danger')));
      }))) : h('p', { class: 'muted' }, '확인할 충돌이 없습니다.'));
  });
  loadConf();
  const showReport = (r, isPhoto) => rep.replaceChildren(h('div', { class: 'card' }, h('h2', {}, r.duplicate_file ? '이미 가져온 파일입니다 (변경 없음)' : '가져오기 결과'),
    h('dl', { class: 'kv' }, ...Object.entries(r).filter(([k]) => !['release_code', 'import_id', 'duplicate_file'].includes(k)).map(([k, v]) => [h('dt', {}, ({ applied: '반영', skipped_duplicate: '중복 건너뜀', conflict: '충돌함으로', photos_expected: '사진 기록', recovered: '사진 회수', already: '이미 회수', hash_mismatch: '해시 불일치', unexpected: '결과보다 먼저 온 사진' })[k] || k), h('dd', {}, v)])),
    r.release_code ? h('div', { class: 'notice' }, isPhoto ? '사진 회수 확인 코드' : '보관 종료 확인 코드', ' — 휴대폰에 입력하게 알려 주세요: ', h('span', { class: 'big' }, r.release_code)) : null));
  return h('div', {}, h('h1', {}, '결과 가져오기'),
    h('div', { class: 'grid2' }, fileIn('결과 파일 (.dgr) — 업무 메일 첨부', 'import/result', '.dgr'), fileIn('사진 파일 (.dgp) — 승인된 보안 매체', 'import/photos', '.dgp')),
    webcamCard((r) => { showReport(r, false); loadConf(); }),
    soloCard(),
    rep, conf, h('p', { class: 'muted' }, '같은 결과를 여러 번 가져와도 한 번만 반영됩니다. 오래된 배정·취소된 접수의 결과는 자동 반영하지 않고 충돌함에 둡니다.'));
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
    const list = (title, arr, fmt) => h('div', { class: 'card' }, h('h2', {}, `${title} ${arr.length}건`), arr.length ? h('ul', { class: 'linklist' }, ...arr.map((x) => h('li', {}, x.id ? h('a', { href: `#/request/${x.id}` }, fmt(x)) : fmt(x)))) : h('p', { class: 'empty' }, '없음'));
    body.replaceChildren(
      h('div', { class: 'card' }, h('h2', {}, `${s.date} 접수 ${d.total}건`), h('div', { class: 'row' }, ...Object.entries(d.by_status).map(([k, v]) => { const el = statusChip(k); el.textContent = `${STATUS[k] || k} ${v}`; return el; })),
        h('p', {}, `충돌 미처리 ${d.conflicts}건 · 사진 미회수 ${d.photos_missing}장 · 수신 미확인 전송 ${d.unconfirmed_bundles}건`),
        d.closed ? h('p', {}, chip('마감 기록됨', 'ok'), ` ${kst(d.closed.closed_at)} ${d.closed.note || ''}`) : null),
      h('div', { class: 'grid2' },
        list('결과 미회수 (배정했으나 결과 없음)', d.no_result, (x) => `${x.receipt_no} ${x.name}`),
        list('재방문 후보 (일부·미수거·보류)', d.revisit, (x) => `${x.receipt_no} ${x.name} · ${STATUS[x.status]} · 남은 ${x.remaining}${x.next_date ? ` · 재방문 약속 ${x.next_date}${x.appt ? ' ' + x.appt[0] + (x.appt[1] ? '~' + x.appt[1] : '') : ''}` : ''}`),
        list('금액 확인 대기', d.pending_fee, (x) => `${x.receipt_no} ${x.name} · ${x.count}건`),
        list('수납 확인 필요 (미납·부족·과납·금액 미확정)', d.payments, (x) => `${x.receipt_no} ${x.name} · ${PAY[x.status]} · 계산 ${x.calculated == null ? '미확정' : won(x.calculated)} / 납부 ${won(x.paid)}`)),
      unloadCard(d),
      h('div', { class: 'card row' }, note, btn(d.closed ? '마감 다시 기록' : '마감 기록', wrap(async () => { await post('close', { date: s.date, note: note.value }); toast('마감을 기록했습니다'); load(true); }), 'primary'),
        h('span', { class: 'muted' }, '미처리 건이 있어도 마감할 수 있으며, 그 목록이 함께 기록되어 다음 날로 이월됩니다.')));
  });
  date.addEventListener('change', load); load();
  return h('div', {}, h('div', { class: 'pagehead' }, h('h1', {}, '일일 마감'), h('label', { class: 'f inline' }, '수거일', date)), body);
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
  return h('div', {}, h('h1', {}, 'QR 실측'),
    h('div', { class: 'card' }, h('p', { class: 'muted' }, '기관 휴대폰으로 QR 전송 시간과 인식률을 잽니다. 가상 자료만 쓰며 업무 자료에 섞이지 않습니다. 휴대폰은 [오늘 목록 받기]로 읽으면 "실측 모드"로 결과만 보여 주고 저장하지 않습니다.'),
      devs.length ? h('div', { class: 'row' }, lab('휴대폰', F.device), lab('건수', F.count), lab('주소 길이', F.addr), lab('조각 크기', F.chunk), lab('오류 복원', F.ec), lab('표시 속도', F.fps), btn('실측 QR 표시', run, 'primary'))
        : h('p', { class: 'err' }, '설정에서 휴대폰을 먼저 등록하세요.'), msg),
    h('div', { class: 'card', style: 'padding:0;overflow:auto' }, h('table', {}, h('thead', {}, h('tr', {}, ...['시각', '자료', '조건', '크기', '결과', '휴대폰 결과 입력'].map((x) => h('th', {}, x)))), tbody)),
    h('p', { class: 'muted' }, '휴대폰에 나온 시간·인식 횟수·확인 4자리를 입력하세요. 확인 4자리가 맞아야 전체 복원이 정확했다는 뜻입니다. 같은 조건을 10회 반복해 평균을 비교하세요.'));
}

// ------------------------------------------------------------ 설정
async function screenSettings() {
  const msg = h('p', { class: 'msg' });
  // 메시지는 그 작업을 한 카드 안에 보여 준다
  const devMsg = h('p', { class: 'msg' }), crewMsg = h('p', { class: 'msg' }), regMsg = h('p', { class: 'msg' }), routeMsg = h('p', { class: 'msg' });
  const regImg = h('img', { alt: '기기 등록 QR', style: 'width:260px;image-rendering:pixelated' });
  api('registration-qr').then((r) => { regImg.src = r.image; }).catch(() => {});
  const code = h('textarea', { placeholder: '휴대폰 화면의 기기 코드 (DGD1.…)', rows: 3, style: 'width:100%' });
  const label = h('input', { placeholder: '기기 이름 (예: 1팀 업무폰)' });
  const fp = h('p', { class: 'big' });
  const devs = h('table', {}, h('thead', {}, h('tr', {}, ...['기기', '이름', '상태', '등록', ''].map((t) => h('th', {}, t)))),
    h('tbody', {}, ...boot.devices.map((d) => h('tr', {}, h('td', {}, d.id.slice(0, 8)), h('td', {}, d.label || ''), h('td', {}, d.status === 'ACTIVE' ? chip('사용', 'ok') : chip(d.status === 'LOST' ? '분실' : '중지', 'bad')),
      h('td', {}, d.registered_at.slice(0, 10)), h('td', {}, ...[['ACTIVE', '사용'], ['LOST', '분실 처리'], ['RETIRED', '사용 중지']].filter(([k]) => k !== d.status).map(([k, t]) =>
        btn(t, wrap(async () => { if (k !== 'ACTIVE' && !confirm(`${d.id.slice(0, 8)} 기기를 '${t}'합니다. 이 기기로는 목록을 보낼 수 없고 결과도 받지 않습니다. 계속할까요?`)) return; await post(`devices/${d.id}/status`, { status: k }); boot = null; render(); }, devMsg), 'small')))))));
  const crewRows = [...boot.crews, { id: '', name: '', device_id: '', capacity_m3: 8, shift_start: '09:00', shift_end: '17:00', active: 1 }].map((c) => {
    const f = { name: h('input', { value: c.name, placeholder: '새 팀 이름' }), device_id: h('select', {}, h('option', { value: '' }, '(없음)'), ...boot.devices.filter((d) => d.status === 'ACTIVE').map((d) => h('option', { value: d.id, selected: d.id === c.device_id }, `${d.id.slice(0, 8)} ${d.label || ''}`))),
      capacity_m3: h('input', { type: 'number', step: '0.5', value: c.capacity_m3, style: 'width:80px' }), shift_start: h('input', { type: 'time', value: c.shift_start }), shift_end: h('input', { type: 'time', value: c.shift_end }), active: h('input', { type: 'checkbox', checked: c.active }) };
    return h('tr', {}, ...Object.values(f).map((x) => h('td', {}, x)), h('td', {}, btn(c.id ? '저장' : '추가', wrap(async () => {
      await post('crews', { id: c.id || null, name: f.name.value, device_id: f.device_id.value, capacity_m3: f.capacity_m3.value, shift_start: f.shift_start.value, shift_end: f.shift_end.value, active: f.active.checked });
      toast('저장했습니다'); boot = null; render();
    }, crewMsg), 'small primary')));
  });
  const st = boot.settings;
  const dlat = h('input', { value: st.depot.lat, style: 'width:120px' }), dlon = h('input', { value: st.depot.lon, style: 'width:120px' }), dlabel = h('input', { value: st.depot.label });
  const osrm = h('input', { value: st.osrm_url, placeholder: 'http://127.0.0.1:5000 (비우면 직선거리 추정)', style: 'width:320px' });
  const sb = h('input', { type: 'number', value: st.service_min_base, style: 'width:70px' }), sp = h('input', { type: 'number', value: st.service_min_per_item, style: 'width:70px' });
  const us = st.unload_site || {}, es = st.end_site || {};
  const jur = h('input', { value: st.jurisdiction || '', placeholder: '예: 동구', style: 'width:120px' });
  const elat = h('input', { value: es.lat ?? '', placeholder: '위도', style: 'width:120px' }), elon = h('input', { value: es.lon ?? '', placeholder: '경도', style: 'width:120px' }), elabel = h('input', { value: es.label || '', placeholder: '종료지 이름' });
  const ulat = h('input', { value: us.lat ?? '', placeholder: '위도', style: 'width:120px' }), ulon = h('input', { value: us.lon ?? '', placeholder: '경도', style: 'width:120px' }), ulabel = h('input', { value: us.label || '', placeholder: '반입지 이름' });
  const umin = h('input', { type: 'number', value: st.unload_min, style: 'width:70px' }), umax = h('input', { type: 'number', value: st.max_unloads, min: 0, max: 5, style: 'width:60px' });
  return h('div', {}, h('h1', {}, '설정'),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('h2', {}, '휴대폰 등록'), h('p', { class: 'muted' }, '1) 휴대폰 앱 [기기 등록]으로 아래 QR을 비춥니다 (공개키만 들어 있음). 2) 휴대폰의 기기 코드를 붙여 넣습니다. 3) 양쪽 6자리 지문이 같은지 확인합니다.'),
        regImg, code, label, btn('기기 등록', wrap(async () => { const r = await post('devices/register', { code: code.value, label: label.value }); fp.textContent = `지문 ${r.fingerprint.slice(0, 3)} ${r.fingerprint.slice(3)}`; toast('등록했습니다. 지문을 휴대폰과 대조하세요.'); boot = null; setTimeout(() => { if (location.hash.startsWith('#/settings')) render(); }, 2500); }, regMsg), 'primary'), fp, regMsg),
      h('div', { class: 'card' }, h('h2', {}, '등록된 휴대폰'), devs, h('p', { class: 'muted' }, '분실 시 바로 "분실 처리"하세요. 오프라인 휴대폰의 자료를 원격으로 지울 수는 없지만, 이후 그 기기로는 목록을 보내지 않고 결과도 받지 않습니다.'), devMsg)),
    h('div', { class: 'card' }, h('h2', {}, '수거팀·차량'), h('table', {}, h('thead', {}, h('tr', {}, ...['팀', '연결 휴대폰', '적재(m³)', '근무 시작', '근무 끝', '사용', ''].map((t) => h('th', {}, t)))), h('tbody', {}, ...crewRows)), crewMsg),
    h('div', { class: 'card' }, h('h2', {}, '노선 계산'), h('div', { class: 'row' }, '차고지 위도', dlat, '경도', dlon, dlabel), h('div', { class: 'row' }, '로컬 OSRM', osrm),
      h('div', { class: 'row' }, '지점당 기본 작업', sb, '분 + 품목당', sp, '분 (추정치)'),
      h('div', { class: 'row' }, '반입지', ulat, ulon, ulabel, '하차', umin, '분 · 차량당 최대', umax, '회'),
      h('div', { class: 'row' }, '종료지', elat, elon, elabel, h('span', { class: 'muted' }, '비우면 차고지로 돌아옴')),
      h('div', { class: 'row' }, '관할 구역', jur, h('span', { class: 'muted' }, '주소의 구 이름이 다르면 접수할 때 경고 (막지는 않음)')),
      h('p', { class: 'small muted' }, '반입지 좌표를 넣으면 적재가 찰 때 반입지에 들러 비우고 이어서 수거하는 순서를 추천합니다. 비우면 차량 1회 적재 안에서만 계산합니다.'),
      btn('저장', wrap(async () => { await post('settings', { depot: { lat: dlat.value, lon: dlon.value, label: dlabel.value }, osrm_url: osrm.value, service_min_base: sb.value, service_min_per_item: sp.value,
        unload_site: { lat: ulat.value, lon: ulon.value, label: ulabel.value }, end_site: { lat: elat.value, lon: elon.value, label: elabel.value }, jurisdiction: jur.value, unload_min: umin.value, max_unloads: umax.value }); toast('저장했습니다'); boot = null; render(); }, routeMsg), 'primary'), routeMsg),
    volumeCard(),
    addrCard(),
    backupCard(),
    h('div', { class: 'card' }, h('h2', {}, '수수료표'), h('dl', { class: 'kv' }, h('dt', {}, '적용 표'), h('dd', {}, st.fee_label), h('dt', {}, '버전'), h('dd', {}, st.fee_version),
      h('dt', {}, '자료 확인일'), h('dd', {}, st.fee_checked_on), h('dt', {}, '시행일'), h('dd', {}, st.fee_effective_from || '미확인 (담당자 확인 필요)')),
      h('a', { href: '#/fees' }, '수수료표 관리 화면에서 시행일·확인자·확인 필요 행 정하기')),
    msg);
}

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
  const s = (screenStats.s ||= (() => { const t = boot.today; return { start: t.slice(0, 8) + '01', end: t }; })());
  const start = h('input', { type: 'date', value: s.start }), end = h('input', { type: 'date', value: s.end });
  const preset = (label, fn) => btn(label, () => { const [a, b] = fn(new Date(boot.today + 'T00:00:00')); s.start = a; s.end = b; render(); }, 'small');
  const iso = (d) => d.toISOString().slice(0, 10);
  const shift = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const presets = h('div', { class: 'seg-btns' },
    preset('오늘', (t) => [iso(t), iso(t)]), preset('최근 7일', (t) => [iso(shift(t, -6)), iso(t)]),
    preset('이번 달', (t) => [boot.today.slice(0, 8) + '01', iso(t)]),
    preset('지난달', (t) => { const a = new Date(t.getFullYear(), t.getMonth() - 1, 1), b = new Date(t.getFullYear(), t.getMonth(), 0); return [`${a.getFullYear()}-${String(a.getMonth() + 1).padStart(2, '0')}-01`, `${b.getFullYear()}-${String(b.getMonth() + 1).padStart(2, '0')}-${String(b.getDate()).padStart(2, '0')}`]; }));
  [start, end].forEach((x) => x.addEventListener('change', () => { s.start = start.value; s.end = end.value; render(); }));
  const d = await api(`stats?start=${s.start}&end=${s.end}`);
  const T = d.totals, bs = T.by_status;
  const active = T.requests - (bs.CANCELLED || 0);
  const doneRate = active ? Math.round(((bs.DONE || 0) / active) * 100) : 0;
  const kpi = (k, v, sub, cls = '') => h('div', { class: 'kpi ' + cls }, h('span', { class: 'k' }, k), h('b', { class: 'v' }, v), h('span', { class: 's' }, sub || ' '));
  const csv = wrap(async (kind) => {
    const r = await fetch(`/api/stats.csv?start=${s.start}&end=${s.end}&kind=${kind}`, { headers: { 'X-DG-CSRF': '1' }, credentials: 'same-origin' });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || '내보내기 실패');
    const url = URL.createObjectURL(await r.blob());
    const a = h('a', { href: url, download: `동구-${kind === 'days' ? '날짜별' : '접수별'}-${s.start}_${s.end}.csv` });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
  });
  return h('div', {}, h('div', { class: 'pagehead' }, h('h1', {}, '통계·보고서'),
      h('div', { class: 'row' }, start, '~', end, presets)),
    h('div', { class: 'kpis' },
      kpi('접수', `${T.received}건`, `수거 예정 ${active}건`),
      kpi('수거 완료', `${bs.DONE || 0}건`, `완료율 ${doneRate}%`, 'ok'),
      kpi('재방문·보류', `${(bs.PARTIAL || 0) + (bs.NOT_COLLECTED || 0) + (bs.ON_HOLD || 0)}건`, bs.CANCELLED ? `취소 ${bs.CANCELLED}건 별도` : ' ', (bs.PARTIAL || bs.NOT_COLLECTED) ? 'warn' : ''),
      kpi('계산 금액', won(T.calculated), `납부 기록 ${won(T.paid)}`),
      kpi('확인 필요', `${T.amount_pending + T.unpaid}건`, `금액 대기 ${T.amount_pending} · 미납·부족 ${T.unpaid}`, (T.amount_pending + T.unpaid) ? 'warn' : '')),
    h('div', { class: 'card' }, h('div', { class: 'row', style: 'justify-content:space-between' }, h('h2', {}, '날짜별 수거'),
        h('span', { class: 'muted' }, `하차 ${T.unload_m3}m³ · ${T.unload_kg.toLocaleString()}kg · 현장 추가 품목 ${T.field_added_lines}건${T.lead_days_avg != null ? ` · 접수→수거 평균 ${T.lead_days_avg}일` : ''}`)),
      d.days.length ? dayChart(d.days) : h('p', { class: 'empty' }, '이 기간에 자료가 없습니다.'),
      d.days.length ? h('details', { class: 'tableview' }, h('summary', {}, '표로 보기'),
        h('table', {}, h('thead', {}, h('tr', {}, ...['날짜', '접수', '예정', '완료', '일부', '미수거', '취소', '계산 금액', '납부 기록', '하차'].map((t) => h('th', {}, t)))),
          h('tbody', {}, ...d.days.map((x) => h('tr', {}, h('td', {}, x.date), h('td', {}, x.received), h('td', {}, x.planned), h('td', {}, x.done), h('td', {}, x.partial), h('td', {}, x.not_collected), h('td', {}, x.cancelled),
            h('td', { class: 'num' }, won(x.calculated)), h('td', { class: 'num' }, won(x.paid)), h('td', {}, x.unload_m3 ? `${x.unload_m3}m³` : '')))))) : null),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('h2', {}, '많이 나온 품목'), d.items.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['품목', '규격', '확인 수량', '수거 수량'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...d.items.map((x) => h('tr', {}, h('td', {}, x.name), h('td', {}, x.spec), h('td', { class: 'num' }, `${x.requested}${x.unit_label}`), h('td', { class: 'num' }, `${x.collected}${x.unit_label}`))))) : h('p', { class: 'empty' }, '없음')),
      h('div', { class: 'card' }, h('h2', {}, '팀별'), d.crews.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['팀', '배정', '완료', '재방문·보류', '완료율'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...d.crews.map((c) => h('tr', {}, h('td', {}, c.crew), h('td', { class: 'num' }, c.assigned), h('td', { class: 'num' }, c.done), h('td', { class: 'num' }, c.partial_or_not), h('td', { class: 'num' }, `${c.assigned ? Math.round((c.done / c.assigned) * 100) : 0}%`))))) : h('p', { class: 'empty' }, '배정 기록 없음'))),
    h('div', { class: 'card' }, h('h2', {}, '내보내기'),
      h('div', { class: 'row' }, btn('접수별 CSV', () => csv('requests'), 'primary'), btn('날짜별 CSV', () => csv('days'))),
      h('p', { class: 'muted' }, '엑셀에서 바로 열리는 CSV입니다. 이름·연락처·주소·메모는 넣지 않고 접수번호·날짜·상태·품목·금액만 담습니다. 내보낸 기록은 변경 이력에 남습니다.')));
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
  const reviewTable = h('table', {}, h('thead', {}, h('tr', {}, ...['품목', '규격', '단위', '원문 단가', '확인 필요 사유', '현재', isAdmin ? '확정 단가 · 근거' : ''].map((t) => h('th', {}, t)))),
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
  return h('div', {}, h('h1', {}, '수수료표'),
    h('div', { class: 'card' }, h('dl', { class: 'kv' },
      h('dt', {}, '원문'), h('dd', {}, `${d.source.label} · ${d.source.rows}행 · 자료 확인일 ${d.source.checked_on}`),
      h('dt', {}, '적용 버전'), h('dd', {}, stt.version_id),
      h('dt', {}, '시행일'), h('dd', {}, stt.effective_from || chip('미확인', 'warn')),
      h('dt', {}, '확인자 · 승인자'), h('dd', {}, stt.confirmed_by ? `${stt.confirmed_by} · ${stt.approved_by}` : '아직 없음')),
      h('p', { class: 'muted' }, '원문 변환본은 고치지 않습니다. 확정 단가·시행일·확인자는 개정으로 쌓고, 작성자와 다른 담당자가 승인하면 시행일부터 새 접수와 휴대폰 목록에 적용됩니다. 이미 접수된 품목의 단가·버전은 바뀌지 않습니다.')),
    draftCard,
    h('div', { class: 'card' }, h('h2', {}, `확인 필요 행 ${d.review_rows.length}개`), h('div', { style: 'overflow:auto' }, reviewTable),
      isAdmin ? h('div', {}, h('div', { class: 'row', style: 'margin-top:12px' }, '시행일', eff, '확인자', who, note), h('div', { class: 'row' }, btn('개정안 저장', saveDraft, 'primary'),
        h('span', { class: 'muted' }, '비워 둔 행은 금액 확인 대기로 남습니다. 0원이나 최저가로 정하지 마세요.'))) : h('p', { class: 'muted' }, '개정안 작성은 운영 관리자가 합니다. 승인은 작성자가 아닌 담당자가 할 수 있습니다.'), msg),
    h('div', { class: 'card' }, h('h2', {}, '개정 이력'), d.revisions.length ? h('table', {}, h('thead', {}, h('tr', {}, ...['개정', '상태', '시행일', '확인자', '작성', '승인·반려', '확정 행'].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...d.revisions.map((r) => h('tr', {}, h('td', {}, r.version_id), h('td', {}, r.active ? chip('적용 중', 'ok') : r.status === 'APPROVED' ? chip(r.effective_from > boot.today ? '시행 전' : '이전 승인') : r.status === 'DRAFT' ? chip('승인 대기', 'warn') : chip('반려', 'bad')),
        h('td', {}, r.effective_from), h('td', {}, r.confirmed_by), h('td', {}, `${r.created_by} ${kst(r.created_at)}`), h('td', {}, r.approved_by ? `${r.approved_by} ${kst(r.approved_at)}${r.reject_reason ? ' · ' + r.reject_reason : ''}` : ''),
        h('td', {}, Object.keys(r.overrides).length + '행'))))) : h('p', { class: 'empty' }, '아직 개정이 없습니다.')),
    h('div', { class: 'card' }, h('h2', {}, '전체 표'), q, h('div', { style: 'overflow:auto;max-height:520px;margin-top:8px' }, h('table', {}, h('thead', {}, h('tr', {}, ...['분류', '품목', '규격', '단위', '단가'].map((t) => h('th', {}, t)))), allBody))));
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
    list.replaceChildren(...(b.files.length ? b.files.slice(0, 10).map((f) => {
      const vp = h('input', { type: 'password', placeholder: '백업 비밀번호', style: 'width:150px' });
      return h('tr', {}, h('td', { class: 'mono' }, f.name), h('td', {}, f.at), h('td', {}, `${(f.bytes / 1024 / 1024).toFixed(1)}MB`),
        h('td', {}, vp, ' ', btn('열어 확인', wrap(async () => { const r = await post('backups/verify', { name: f.name, passphrase: vp.value }); toast(`정상: 접수 ${r.requests}건이 들어 있습니다`); }, msg), 'small')));
    }) : [h('tr', {}, h('td', { colspan: 4, class: 'muted' }, '아직 백업이 없습니다.'))]));
    const pc = await api('photo-purge');
    purgeInfo.textContent = pc.count ? `정리 대상 ${pc.count}장 (${(pc.bytes / 1024 / 1024).toFixed(1)}MB)` : '정리 대상 없음';
  }, msg);
  load();
  return h('div', { class: 'card' }, h('h2', {}, '백업·사진 보관'),
    h('p', { class: 'muted' }, '백업 파일은 별도 백업 비밀번호로 암호화됩니다. 비밀번호를 잃으면 열 수 없으니 책임자가 따로 보관하세요. 백업 파일은 승인된 보안 매체로만 옮깁니다.'),
    h('div', { class: 'row' }, pass, pass2, btn('지금 백업', wrap(async () => {
      if (pass.value !== pass2.value) throw new Error('두 번 입력한 백업 비밀번호가 다릅니다.');
      const r = await post('backup', { passphrase: pass.value }); pass.value = pass2.value = '';
      toast(`백업했습니다: ${r.name}`); load();
    }, msg), 'primary')),
    h('p', { class: 'small muted' }, '백업 폴더: ', dirEl),
    h('table', {}, h('thead', {}, h('tr', {}, ...['파일', '만든 시각', '크기', '확인'].map((t) => h('th', {}, t)))), list),
    h('p', { class: 'small muted' }, '복원은 PC 업무 화면을 끄고 명령으로 실행합니다: ', h('code', {}, 'python pc/pc_tool.py restore <백업 파일>'), ' (기존 DB는 지우지 않고 옆에 보관)'),
    h('hr'),
    h('div', { class: 'row' }, '회수한 사진 보관', days, '일', h('span', { class: 'grow' })),
    h('div', { class: 'row' }, '백업 폴더', bdir, btn('저장', wrap(async () => { await post('settings', { photo_retention_days: days.value, backup_dir: bdir.value }); toast('저장했습니다'); boot = null; render(); }, msg), 'small')),
    h('div', { class: 'row' }, purgeInfo, btn('기간 지난 사진 정리', wrap(async () => {
      if (!confirm(`회수일로부터 ${st.photo_retention_days}일이 지나고 처리가 끝난 접수의 사진을 지웁니다. 기록(해시·시각)은 남습니다. 계속할까요?`)) return;
      const r = await post('photo-purge'); toast(`사진 ${r.count}장을 정리했습니다`); load();
    }, msg), 'small')),
    h('p', { class: 'small muted' }, '처리가 끝난 접수(수거 완료·취소 또는 그날 마감)의 사진만 정리합니다. 사진 원본은 지워지고 해시·회수 시각·정리 시각은 이력으로 남습니다.'), msg);
}

// ------------------------------------------------------------ 틀·라우팅
let renderSeq = 0;
async function render(fromNav) {
  if (fromNav !== true && actionHash !== null && actionHash !== location.hash) return;
  const my = ++renderSeq;
  liveMaps.forEach((m) => { try { m.remove(); } catch {} }); liveMaps = [];
  let view;
  try {
    if (!boot) {
      const st = await api('state');
      if (!st.logged_in) { view = await screenLogin(); }
      else boot = await api('bootstrap');
    }
    if (boot) {
      const [, route, arg] = (location.hash || '#/home').split('/');
      const role = boot.me.role;
      const screens = { home: screenHome, requests: screenRequests, request: () => screenRequest(arg), dispatch: screenDispatch, send: screenSend, results: screenResults, close: screenClose, qrtest: screenQrTest, fees: screenFees, stats: screenStats, settings: screenSettings, users: screenUsers, me: screenMe };
      // 역할별 메뉴 (서버도 같은 기준으로 막는다)
      const NAV = [['home', '오늘 현황', 'ADMIN INTAKE FIELD'], ['requests', '접수', 'ADMIN INTAKE FIELD'], ['dispatch', '배정·노선', 'ADMIN INTAKE'], ['send', '목록 전송', 'ADMIN INTAKE'],
        ['results', '결과 가져오기', 'ADMIN INTAKE FIELD'], ['close', '일일 마감', 'ADMIN INTAKE FIELD'], ['qrtest', 'QR 실측', 'ADMIN INTAKE FIELD'], ['stats', '통계·보고서', 'ADMIN INTAKE'], ['fees', '수수료표', 'ADMIN INTAKE FIELD'], ['settings', '설정', 'ADMIN'], ['users', '사용자·이력', 'ADMIN']];
      const nav = NAV.filter(([, , r]) => r.split(' ').includes(role));
      let cur = route || 'home';
      if (boot.me.must_change) cur = 'me';
      if (!['request', 'me'].includes(cur) && !nav.some(([k]) => k === cur)) cur = 'home';
      const content = await (screens[cur] || screenHome)();
      const active = cur === 'request' ? 'requests' : cur;
      view = h('div', { class: 'shell' }, h('nav', { class: 'side' },
        h('div', { class: 'brand' }, h('span', { class: 'logo' }, '동'), h('div', {}, h('strong', {}, '동구 대형폐기물'), h('small', {}, '접수·수거 관리'))),
        ...nav.map(([k, t]) => h('a', { href: '#/' + k, class: active === k ? 'on' : '' }, t)),
        h('span', { class: 'grow' }),
        h('a', { href: '#/me', class: 'who' + (active === 'me' ? ' on' : '') }, h('b', {}, boot.me.display_name), h('small', {}, boot.me.role_label)),
        h('a', { href: '#', onclick: async (e) => { e.preventDefault(); await post('logout').catch(() => {}); boot = null; render(); } }, '로그아웃')),
      h('main', {}, content));
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
