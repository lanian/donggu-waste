// 동구 대형폐기물 PC 업무 화면 (이 PC의 127.0.0.1 서버만 사용)
import { searchFees, won } from '/web/fees.js';
import { BUILTIN_FEES } from '/web/fees-data.js';
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
    else if (k === 'checked') el.checked = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
const btn = (label, onclick, cls = '', extra = {}) => h('button', { type: 'button', class: cls, onclick, ...extra }, label);
const toast = (m, bad) => {
  let box = document.querySelector('.toasts');
  if (!box) { box = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.append(box); }
  const t = h('div', { class: 'toast' + (bad ? ' bad' : '') }, m); box.append(t); setTimeout(() => t.remove(), 4000);
};
const fmtPhone = (p) => (p && p.length === 11 ? `${p.slice(0, 3)}-${p.slice(3, 7)}-${p.slice(7)}` : p || '');
const go = (hash) => { location.hash = hash; };
const chip = (text, cls = '') => h('span', { class: 'chip ' + cls }, text);
const statusChip = (s) => chip(STATUS[s] || s, s === 'DONE' ? 'ok' : ['PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(s) ? 'warn' : s === 'CANCELLED' ? 'bad' : '');

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
const wrap = (fn, msgEl) => async (...a) => { try { if (msgEl) msgEl.textContent = ''; await fn(...a); } catch (e) { if (msgEl) { msgEl.textContent = e.message; msgEl.className = 'err'; } else toast(e.message, true); } };

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
  setTimeout(() => (st.setup_needed ? dname : user).focus(), 30);
  return h('div', { class: 'login-wrap' }, h('div', { class: 'login card' },
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
  ACTIVE: '사용', DEACTIVATE: '중지', IMPORT: '가져오기', RESOLVE: '충돌 처리', VOID: '정정', CLOSE: '마감', RECEIPT: '수신 확인', BACKUP: '백업', RESTORE: '복원', PURGE: '사진 정리' };
async function screenUsers() {
  const msg = h('p', { class: 'msg' });
  const users = await api('users');
  const F = { username: h('input', { placeholder: '아이디 (영문)' }), display_name: h('input', { placeholder: '이름' }),
    role: h('select', {}, ...Object.entries(ROLE).map(([k, t]) => h('option', { value: k, selected: k === 'INTAKE' }, t))), password: h('input', { type: 'password', placeholder: '임시 비밀번호 (10자 이상)', autocomplete: 'new-password' }) };
  const utable = h('table', {}, h('thead', {}, h('tr', {}, ...['아이디', '이름', '역할', '상태', '마지막 로그인', ''].map((t) => h('th', {}, t)))),
    h('tbody', {}, ...users.map((u) => h('tr', {}, h('td', {}, u.username), h('td', {}, u.display_name), h('td', {}, ROLE[u.role]),
      h('td', {}, u.active ? (u.must_change ? chip('임시 비밀번호', 'warn') : chip('사용', 'ok')) : chip('중지', 'bad')),
      h('td', {}, u.last_login_at ? u.last_login_at.replace('T', ' ').slice(0, 16) : '—'),
      h('td', {}, btn(u.active ? '중지' : '다시 사용', wrap(async () => { await post(`users/${u.id}/active`, { active: !u.active }); render(); }, msg), 'small'), ' ',
        btn('비밀번호 재설정', wrap(async () => { const np = prompt(`${u.username}의 임시 비밀번호(10자 이상). 다음 로그인 때 바꾸게 됩니다.`); if (!np) return; await post(`users/${u.id}/password`, { password: np }); toast('재설정했습니다'); render(); }, msg), 'small'))))));
  // 변경 이력
  const fEntity = h('select', {}, h('option', { value: '' }, '전체'), ...[['request', '접수'], ['payment', '수납'], ['assignment', '배정'], ['bundle', '전송'], ['result', '결과'], ['photo', '사진'], ['conflict', '충돌'], ['day', '마감'], ['device', '기기'], ['crew', '수거팀'], ['user', '사용자'], ['backup', '백업']].map(([v, t]) => h('option', { value: v }, t)));
  const fDate = h('input', { type: 'date', value: boot.today }), fActor = h('select', {}, h('option', { value: '' }, '전체 작업자'), h('option', { value: 'system' }, 'system'), ...users.map((u) => h('option', { value: u.username }, u.username)));
  const tbody = h('tbody');
  const load = wrap(async () => {
    const list = await api(`audit?entity=${fEntity.value}&date=${fDate.value}&actor=${encodeURIComponent(fActor.value)}`);
    tbody.replaceChildren(...(list.length ? list.map((a) => h('tr', {}, h('td', { class: 'nowrap' }, kst(a.at)), h('td', {}, a.actor || '—'), h('td', {}, a.entity), h('td', {}, ACTION[a.action] || a.action),
      h('td', { class: 'mono' }, a.entity_id ? a.entity_id.slice(0, 8) : ''), h('td', { class: 'detail' }, summarizeDetail(a.detail)))) : [h('tr', {}, h('td', { colspan: 6, class: 'muted' }, '기록이 없습니다.'))]));
  });
  [fEntity, fDate, fActor].forEach((x) => x.addEventListener('change', load));
  load();
  return h('div', {}, h('h1', {}, '사용자·변경 이력'),
    h('div', { class: 'card' }, h('h2', {}, '사용자'), utable,
      h('div', { class: 'row', style: 'margin-top:12px' }, F.username, F.display_name, F.role, F.password,
        btn('사용자 추가', wrap(async () => { await post('users', { username: F.username.value, display_name: F.display_name.value, role: F.role.value, password: F.password.value }); toast('추가했습니다. 첫 로그인 때 비밀번호를 바꾸게 됩니다.'); render(); }, msg), 'primary')),
      h('p', { class: 'muted' }, '접수 담당자: 접수·배정·전송·결과·수납·마감. 수거팀: 조회, 결과·사진 가져오기, 하차 기록. 운영 관리자: 전체와 설정·사용자·백업. 계정은 지우지 않고 중지합니다.'), msg),
    h('div', { class: 'card' }, h('h2', {}, '변경 이력'), h('div', { class: 'row' }, '날짜', fDate, '대상', fEntity, fActor, btn('새로 고침', load, 'small')),
      h('div', { style: 'overflow:auto;max-height:520px' }, h('table', { class: 'audit' }, h('thead', {}, h('tr', {}, ...['시각', '작업자', '대상', '작업', 'ID', '내용'].map((t) => h('th', {}, t)))), tbody)),
      h('p', { class: 'muted' }, '이력은 지우거나 고칠 수 없습니다. 내용에는 이름·연락처·주소를 넣지 않습니다(사유·건수·코드만).')));
}
const kst = (iso) => { if (!iso) return ''; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); };
function summarizeDetail(raw) {
  if (!raw) return '';
  let d; try { d = JSON.parse(raw); } catch { return String(raw).slice(0, 120); }
  if (typeof d !== 'object' || d == null) return String(d);
  return Object.entries(d).filter(([k]) => !['name', 'phone', 'address', 'memo', 'location_note'].includes(k)).slice(0, 6)
    .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v).slice(0, 60) : String(v).slice(0, 60)}`).join(' · ');
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
    const rows = quick ? usage.map((id) => BUILTIN_FEES.rows.find((r) => r.id === id)).filter(Boolean) : searchFees(BUILTIN_FEES, st.q).slice(0, 40);
    list.replaceChildren(...rows.map((r) => btn(h('span', {}, `${r.name} ${r.spec}`, h('span', { class: 'p' }, `${won(r.price)} / ${r.unit_label}`),
      r.status === 'REVIEW' ? h('span', { class: 'w' }, '규격 확인 필요 → 금액 확인 대기') : null), () => { st.sel = r.id; draw(); }, st.sel === r.id ? 'on' : '')));
    list.style.display = rows.length ? '' : 'none';
    if (quick && rows.length) list.prepend(h('p', { class: 'muted', style: 'margin:4px 8px' }, '자주 쓰는 품목'));
    add.disabled = !st.sel;
  };
  q.addEventListener('input', () => { st.q = q.value; st.sel = null; draw(); }); // 검색어가 바뀌면 선택 해제
  add.addEventListener('click', () => { const r = BUILTIN_FEES.rows.find((x) => x.id === st.sel); if (!r) return; onAdd({ fee_rate_id: r.id, qty: Math.max(1, +qty.value || 1), _row: r }); st.sel = null; q.value = ''; st.q = ''; qty.value = 1; draw(); });
  const fname = h('input', { placeholder: '목록에 없는 품목 이름' });
  const funit = h('select', {}, ...[['EA', '개'], ['PIECE', '쪽'], ['SET', '세트'], ['L20', '20ℓ당']].map(([v, t]) => h('option', { value: v }, t)));
  const fqty = h('input', { type: 'number', min: 1, value: 1, style: 'width:80px' });
  const fadd = btn('확인 대기로 추가', () => { if (!fname.value.trim()) return toast('품목 이름을 적어 주세요', true); onAdd({ name: fname.value.trim(), unit: funit.value, unit_label: funit.selectedOptions[0].textContent, qty: Math.max(1, +fqty.value || 1) }); fname.value = ''; });
  draw();
  return h('div', {}, h('div', { class: 'row' }, q, qty, add), list,
    h('div', { class: 'row' }, fname, funit, fqty, fadd),
    h('p', { class: 'muted' }, `수수료표: ${BUILTIN_FEES.label} · 표에 없는 품목은 0원이 아니라 금액 확인 대기로 남습니다. 유사 품목 적용은 담당자가 확인해 정합니다.`));
}

// ------------------------------------------------------------ 지도 도우미
async function mapIn(box, opts) {
  const m = await M.createMap(box, opts);
  liveMaps.push(m.map);
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
    [revisit, `재방문 후보 ${revisit}건`, '#/close', '', 'ADMIN INTAKE'],
  ].filter(([n, , , , r]) => n && r.split(' ').includes(role));
  return h('div', {}, h('div', { class: 'row', style: 'justify-content:space-between' }, h('h1', {}, '오늘 현황'), h('div', { class: 'row' }, date, sum.closed ? chip('마감됨', 'ok') : chip('마감 전'))),
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
  const f = (screenRequests.f ||= { q: '', date: '', status: 'OPEN' });
  const q = h('input', { type: 'search', placeholder: '이름·연락처·주소·접수번호', value: f.q });
  const date = h('input', { type: 'date', value: f.date });
  const status = h('select', {}, ...[['OPEN', '미완료 전체'], ['', '전체'], ...Object.entries(STATUS)].map(([v, t]) => h('option', { value: v, selected: v === f.status }, t)));
  const tbody = h('tbody');
  const load = wrap(async () => {
    Object.assign(f, { q: q.value, date: date.value, status: status.value });
    const list = await api(`requests?q=${encodeURIComponent(f.q)}&date=${f.date}&status=${f.status}`);
    tbody.replaceChildren(...(list.length ? list.map((r) => h('tr', { class: 'click', onclick: () => go(`#/request/${r.id}`) },
      h('td', {}, r.receipt_no), h('td', {}, r.planned_date || '-'), h('td', {}, r.name), h('td', {}, fmtPhone(r.phone)), h('td', {}, r.address),
      h('td', {}, r.items.map((i) => `${i.name} ${i.spec} ${i.qty}${i.unit_label}`).join(', ')),
      h('td', {}, statusChip(r.status), r.state.contact !== 'NOT_CONTACTED' ? chip(CONTACT[r.state.contact]) : null, r.must_today ? chip('오늘 필수', 'warn') : null),
      h('td', {}, r.state.fee.final == null ? chip('금액 확인 대기', 'warn') : won(r.state.fee.final)),
      h('td', {}, r.lat == null ? chip('위치 없음', 'warn') : '✓'),
      h('td', {}, r.assigned_to ? `${r.assigned_to.name} ${r.assigned_to.work_date}` : ''))) : [h('tr', {}, h('td', { colspan: 10, class: 'muted' }, '해당 접수가 없습니다.'))]));
  });
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter') load(); });
  [date, status].forEach((x) => x.addEventListener('change', load));
  load();
  return h('div', {}, h('div', { class: 'row', style: 'justify-content:space-between' }, h('h1', {}, '접수'), btn('+ 전화 접수 등록', () => go('#/request/new'), 'primary')),
    h('div', { class: 'card row' }, q, date, status, btn('검색', load)),
    h('div', { class: 'card', style: 'padding:0;overflow:auto;max-height:70vh' }, h('table', {},
      h('thead', {}, h('tr', {}, ...['접수번호', '예정일', '이름', '연락처', '배출 주소', '품목', '상태', '금액', '위치', '배정'].map((t) => h('th', {}, t)))), tbody)));
}

// ------------------------------------------------------------ 접수 등록·수정
async function screenRequest(id) {
  const isNew = id === 'new';
  const r = isNew ? { name: '', phone: '', address: '', spot_note: '', memo: '', planned_date: '', appt_start: '', appt_end: '', must_today: 0, urgent: 0, priority: 0, lat: null, lon: null, items: [], versions: [], locked_lines: [], status: 'RECEIVED' } : await api('requests/' + id);
  const items = r.items.map((i) => ({ ...i }));
  const locked = new Set(r.locked_lines || []);
  const inp = (k, attrs = {}) => h('input', { value: r[k] ?? '', ...attrs });
  const F = { name: inp('name'), phone: inp('phone', { inputmode: 'tel', placeholder: '010-0000-0000' }), address: inp('address'), spot_note: inp('spot_note', { placeholder: '예: 101동 분리수거장 옆' }),
    memo: h('textarea', {}, r.memo || ''), planned_date: inp('planned_date', { type: 'date' }), appt_start: inp('appt_start', { type: 'time' }), appt_end: inp('appt_end', { type: 'time' }),
    must_today: h('input', { type: 'checkbox', checked: r.must_today }), urgent: h('input', { type: 'checkbox', checked: r.urgent }),
    priority: h('select', {}, ...[[0, '보통'], [1, '높음'], [2, '매우 높음']].map(([v, t]) => h('option', { value: v, selected: +r.priority === v }, t))),
    lat: inp('lat', { readonly: true, style: 'width:110px' }), lon: inp('lon', { readonly: true, style: 'width:120px' }) };
  const lab = (t, el) => h('label', { class: 'f' }, t, el);
  const itemsBox = h('div', { class: 'items' });
  const total = h('p', { class: 'muted' });
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

  // 위치 (지도를 눌러 지정. 주소로 검색하지 않음)
  const mapBox = h('div', { class: 'mapbox' });
  setTimeout(async () => {
    try {
      const { map, ml } = await mapIn(mapBox, { center: r.lat != null ? [r.lon, r.lat] : [boot.settings.depot.lon, boot.settings.depot.lat], zoom: r.lat != null ? 17 : 14 });
      let marker = r.lat != null ? M.pin(ml, map, { lng: r.lon, lat: r.lat, label: '●', cls: 'me' }) : null;
      map.on('click', (e) => {
        const lat = Math.round(e.lngLat.lat * 1e6) / 1e6, lon = Math.round(e.lngLat.lng * 1e6) / 1e6;
        F.lat.value = lat; F.lon.value = lon;
        if (marker) marker.remove();
        marker = M.pin(ml, map, { lng: lon, lat, label: '●', cls: 'me' });
      });
    } catch { mapBox.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다(인터넷 연결 확인). 위치는 나중에 지정해도 됩니다.')); }
  }, 0);

  const reason = h('input', { placeholder: '수정 사유 (필수)', style: 'flex:1' });
  const msg = h('p', { class: 'msg' });
  const data = () => ({ name: F.name.value, phone: F.phone.value, address: F.address.value, spot_note: F.spot_note.value, memo: F.memo.value,
    planned_date: F.planned_date.value, appt_start: F.appt_start.value, appt_end: F.appt_end.value, must_today: F.must_today.checked, urgent: F.urgent.checked,
    priority: +F.priority.value, lat: F.lat.value || null, lon: F.lon.value || null,
    items: items.map((i) => ({ line_id: i.line_id, fee_rate_id: i.fee_rate_id || null, name: i.name, spec: i.spec, unit: i.unit, unit_label: i.unit_label, qty: i.qty })) });
  let dupAck = false;
  const save = wrap(async () => {
    if (isNew) {
      if (!dupAck) {
        await checkDup();
        if (dupBox.childNodes.length) { dupAck = true; msg.textContent = '비슷한 접수가 있습니다. 별개 접수가 맞으면 [접수 등록]을 한 번 더 누르세요.'; return; }
      }
      const res = await post('requests', data());
      toast(`${res.receipt_no} 등록했습니다`); go(`#/request/${res.id}`);
    } else {
      const res = await post('requests/' + id, { ...data(), reason: reason.value });
      toast(res.resend_needed ? '수정했습니다. 이미 배정된 건이라 목록 재전송이 필요합니다.' : '수정했습니다'); render();
    }
  }, msg);
  const cancel = wrap(async () => {
    const why = prompt('취소 사유를 적어 주세요 (기록은 지워지지 않습니다)');
    if (!why) return;
    const res = await post(`requests/${id}/cancel`, { reason: why });
    toast(res.field_notice_needed ? '취소했습니다. 이미 휴대폰에 전달된 건이니 수거팀에 전화로 알리세요.' : '취소했습니다'); render();
  }, msg);
  const editable = r.status !== 'CANCELLED';

  // 현장 결과
  const st = r.state;
  const field = !isNew && st ? h('div', { class: 'card' }, h('h2', {}, '현장 결과'),
    h('div', { class: 'row' }, statusChip(r.status), chip('연락: ' + CONTACT[st.contact]), st.outcome ? chip('저장된 결과: ' + (STATUS[st.outcome] || st.outcome)) : null),
    h('table', {}, h('thead', {}, h('tr', {}, ...['품목', '구분', '원접수', '현장 확인', '실제 수거', '남은 대상', '사유'].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...st.lines.map((l) => h('tr', {}, h('td', {}, `${l.name} ${l.spec}`), h('td', {}, l.kind === 'FIELD' ? chip('현장 추가', 'warn') : '원접수'),
        h('td', {}, l.qty_received), h('td', {}, l.qty_confirmed), h('td', {}, l.qty_collected), h('td', {}, l.remaining ? chip(`${l.remaining}${l.unit_label}`, 'warn') : '0'), h('td', {}, REASON[l.reason] || ''))))),
    h('dl', { class: 'kv' }, h('dt', {}, '접수 당시 금액'), h('dd', {}, won(st.fee.received)), h('dt', {}, '현장 추가분'), h('dd', {}, won(st.fee.field_added)),
      h('dt', {}, '접수 대비 증감'), h('dd', {}, (st.fee.diff > 0 ? '+' : '') + won(st.fee.diff)), h('dt', {}, '금액 확인 대기'), h('dd', {}, st.fee.pending_count + '건'),
      h('dt', {}, '최종 금액'), h('dd', {}, st.fee.final == null ? '미확정' : won(st.fee.final))),
    h('p', { class: 'muted' }, '금액은 현장 확인 수량 기준이며 수납 완료를 뜻하지 않습니다. 일부 수거로 금액을 자동 감액하지 않습니다.'),
    r.photos && r.photos.length ? h('div', { class: 'thumbs' }, ...r.photos.map((p) => p.purged_at ? chip(`보관 기간 만료로 정리됨 (${p.purged_at.slice(0, 10)})`) : p.recovery_status === 'RECOVERED' ? photoImg(p.photo_id) : chip(`사진 미회수 (${p.phase === 'BEFORE' ? '전' : '후'})`, 'warn'))) : null) : null;

  return h('div', {},
    h('div', { class: 'row', style: 'justify-content:space-between' }, h('h1', {}, isNew ? '전화 접수 등록' : `접수 ${r.receipt_no}`), btn('← 목록', () => go('#/requests'))),
    r.status === 'CANCELLED' ? h('div', { class: 'notice' }, '취소된 접수입니다. 사유: ' + (r.cancel_reason || '-')) : null,
    h('div', { class: 'grid2' },
      h('div', {},
        h('div', { class: 'card' }, h('h2', {}, '신청자'),
          h('div', { class: 'grid2' }, lab('이름', F.name), lab('연락처', F.phone)), lab('배출 주소', F.address), lab('상세 배출 위치', F.spot_note), lab('요청 메모', F.memo), dupBox),
        h('div', { class: 'card' }, h('h2', {}, '일정·우선순위'),
          h('div', { class: 'grid3' }, lab('수거 예정일', F.planned_date), lab('방문 약속 시작', F.appt_start), lab('방문 약속 끝', F.appt_end)),
          h('div', { class: 'row' }, h('label', { class: 'row' }, F.must_today, '오늘 반드시 포함'), h('label', { class: 'row' }, F.urgent, '긴급'), lab('품목 우선순위', F.priority)),
          h('p', { class: 'muted' }, '오전에 먼저 가야 하면 방문 약속 시간을 넣으세요. 우선순위는 "포함 여부"에만 영향을 줍니다.'))),
      h('div', {},
        h('div', { class: 'card' }, h('h2', {}, '버릴 물건'), itemsBox, total, editable ? picker : null),
        h('div', { class: 'card' }, h('h2', {}, '배출 위치'), h('p', { class: 'muted' }, '지도를 눌러 실제 배출 위치를 지정합니다. 주소를 외부로 보내지 않기 위해 주소 검색은 쓰지 않습니다.'),
          mapBox, h('div', { class: 'row' }, '위도', F.lat, '경도', F.lon)))),
    field,
    !isNew ? paymentCard(r) : null,
    editable ? h('div', { class: 'card row' }, isNew ? null : reason, btn(isNew ? '접수 등록' : '수정 저장', save, 'primary'), isNew ? null : btn('접수 취소', cancel, 'danger'), msg) : null,
    !isNew && r.versions.length ? h('div', { class: 'card' }, h('h2', {}, '수정 이력'), h('ul', {}, ...r.versions.map((v) => h('li', {}, `버전 ${v.req_ver} → ${v.req_ver + 1} · ${v.at.replace('T', ' ').slice(0, 16)} UTC · ${v.reason}`)))) : null);
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
  const s = (screenDispatch.s ||= { date: boot.today, crews: null, plan: null });
  const date = h('input', { type: 'date', value: s.date });
  const crews = boot.crews.filter((c) => c.active);
  if (!s.crews) s.crews = new Set(crews.map((c) => c.id));
  const crewBox = h('div', { class: 'row' }, ...crews.map((c) => h('label', { class: 'row' }, h('input', { type: 'checkbox', checked: s.crews.has(c.id), onchange: (e) => { e.target.checked ? s.crews.add(c.id) : s.crews.delete(c.id); } }),
    `${c.name} (${c.capacity_m3}m³, ${c.shift_start}~${c.shift_end}${c.device_id ? '' : ', 휴대폰 미연결'})`)));
  const msg = h('p', { class: 'msg' });
  const out = h('div');
  const calc = wrap(async () => {
    s.date = date.value; msg.textContent = '계산 중… (최대 5초)';
    const res = await post('route', { date: s.date, crew_ids: [...s.crews] });
    s.plan = { res, routes: res.routes.map((r) => ({ crew_id: r.crew_id, crew_name: r.crew_name, stops: r.stops.map((x) => ({ ...x, unload_after: x.unload_after ? { ...x.unload_after } : undefined })), end: r.end, unloads: r.unloads })),
      unassigned: res.unassigned.map((u) => ({ ...u })) };
    msg.textContent = ''; draw();
  }, msg);
  const draw = () => {
    liveMaps.forEach((m) => { try { m.remove(); } catch {} }); liveMaps = [];
    const p = s.plan;
    if (!p) { out.replaceChildren(h('p', { class: 'muted' }, '날짜와 팀을 고르고 [노선 계산]을 누르세요. 결과는 추천안이며 순서를 바꿔 확정할 수 있습니다.')); return; }
    const R = p.res.requests;
    const clearUnload = (ri) => { if (p.routes[ri].stops.some((x) => x.unload_after)) { p.routes[ri].stops.forEach((x) => delete x.unload_after); p.routes[ri].unloadStale = true; } };
    const move = (ri, i, d) => { const a = p.routes[ri].stops; const j = i + d; if (j < 0 || j >= a.length) return; [a[i], a[j]] = [a[j], a[i]]; clearUnload(ri); draw(); };
    const toCrew = (ri, i, target) => { clearUnload(ri); if (target !== 'OUT') clearUnload(+target); const [x] = p.routes[ri].stops.splice(i, 1); if (target === 'OUT') p.unassigned.push({ request_id: x.request_id, reason: '담당자가 제외' }); else p.routes[+target].stops.push({ request_id: x.request_id, eta: '' }); draw(); };
    const cols = p.routes.map((rt, ri) => h('div', { class: 'card' }, h('h2', {}, `${rt.crew_name} · ${rt.stops.length}곳`, rt.end ? h('span', { class: 'muted' }, ` 예상 종료 ${rt.end}`) : null, rt.unloads ? chip(`반입지 경유 ${rt.unloads}회`) : null),
      rt.unloadStale ? h('div', { class: 'notice warn small' }, '순서를 바꿔 반입지 경유 위치가 지워졌습니다. 적재가 넘치지 않는지 확인하거나 [노선 계산]을 다시 하세요.') : null,
      ...rt.stops.flatMap((x, i) => [h('div', { class: 'stopline' }, h('span', { class: 's' }, i + 1),
        h('span', { class: 't' }, `${R[x.request_id].name} · ${R[x.request_id].address}`, h('br'), h('span', { class: 'muted' }, `${x.eta ? '도착 ' + x.eta : '시간 재계산 필요'}${x.load_m3 != null ? ` · 누적 적재(추정) ${x.load_m3}m³` : ''}${R[x.request_id].appt[0] ? ` · 약속 ${R[x.request_id].appt[0]}~${R[x.request_id].appt[1] || ''}` : ''}`),
          R[x.request_id].must_today ? chip('오늘 필수', 'warn') : null, x.same_place > 1 ? chip(`같은 장소 ${x.same_place}건`) : null),
        btn('▲', () => move(ri, i, -1), 'small'), btn('▼', () => move(ri, i, 1), 'small'),
        h('select', { class: 'small', onchange: (e) => toCrew(ri, i, e.target.value) }, h('option', { value: '' }, '이동…'), ...p.routes.map((o, oi) => oi !== ri ? h('option', { value: oi }, o.crew_name) : null), h('option', { value: 'OUT' }, '이번 배정에서 빼기'))),
        x.unload_after ? h('div', { class: 'stopline unload' }, h('span', { class: 's' }, '⇩'), h('span', { class: 't' }, `${x.unload_after.label} 하차`, h('br'),
          h('span', { class: 'muted' }, `도착 ${x.unload_after.eta} · 약 ${x.unload_after.volume_m3}m³ 비움 · ${x.unload_after.minutes}분`))) : null])));
    const un = h('div', { class: 'card' }, h('h2', {}, `미배정 ${p.unassigned.length}건`), ...p.unassigned.map((u, i) => h('div', { class: 'stopline' },
      h('span', { class: 't' }, R[u.request_id] ? `${R[u.request_id].name} · ${R[u.request_id].address}` : u.request_id, h('br'), h('span', { class: 'muted' }, u.reason)),
      R[u.request_id] && R[u.request_id].lat == null ? btn('위치 지정', () => go(`#/request/${u.request_id}`), 'small') : null,
      h('select', { class: 'small', onchange: (e) => { if (e.target.value === '') return; const [x] = p.unassigned.splice(i, 1); p.routes[+e.target.value].stops.push({ request_id: x.request_id, eta: '' }); draw(); } },
        h('option', { value: '' }, '팀에 넣기…'), ...p.routes.map((o, oi) => h('option', { value: oi }, o.crew_name))))));
    const mapBox = h('div', { class: 'mapbox', style: 'height:460px' });
    const confirmBtn = btn('배정 확정', wrap(async () => {
      const plan = {}; p.routes.forEach((rt) => { plan[rt.crew_id] = rt.stops.map((x) => ({ request_id: x.request_id, eta: x.eta || '' })); });
      const res = await post('assign', { date: s.date, plan });
      toast(`배정 확정: ${res.map((b) => `${b.count}곳(갱신본 ${b.assign_ver})`).join(', ')}`); s.plan = null; go('#/send');
    }, msg), 'primary');
    out.replaceChildren(h('div', { class: 'notice' }, `이동시간: ${p.res.matrix_note || '-'}`, p.res.unload_site ? ` · 적재가 차면 ${p.res.unload_site.label || '반입지'}에 들러 비움` : ' · 반입지 좌표가 없어 차량 1회 적재 안에서만 계산 (설정에서 지정)', p.res.infeasible ? h('strong', {}, ' · 해를 찾지 못했습니다. 차량·근무시간·필수 포함 조건을 조정하세요.') : null),
      h('div', { class: 'grid2' }, h('div', {}, h('div', { class: 'cols' }, ...cols), un), h('div', {}, h('div', { class: 'card' }, h('h2', {}, '지도'), mapBox,
        h('p', { class: 'muted' }, '선은 방문 순서를 잇는 직선이며 실제 도로가 아닙니다. 핀 위치와 이름은 지도 서버로 보내지 않습니다.')), h('div', { class: 'card row' }, confirmBtn, msg))));
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
          map.addLayer({ id: 'routes', type: 'line', source: 'routes', paint: { 'line-width': 3, 'line-dasharray': [2, 1], 'line-color': ['match', ['get', 'c'], 0, '#1d4ed8', 1, '#b45309', 2, '#7c3aed', '#0e7490'] } });
          map.fitBounds(b, { padding: 50, maxZoom: 16, duration: 0 });
        });
      } catch { mapBox.replaceChildren(h('p', { class: 'pad muted' }, '지도를 불러오지 못했습니다.')); }
    }, 0);
  };
  draw();
  return h('div', {}, h('h1', {}, '배정·노선'),
    h('div', { class: 'card' }, h('div', { class: 'row' }, h('label', { class: 'f' }, '수거일', date), btn('노선 계산', calc, 'primary')), h('h3', {}, '팀'), crewBox,
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
          h('h2', {}, `${b.crew_name} · 갱신본 ${b.assign_ver} · ${b.stops.length}곳 `, b.status === 'ACTIVE' ? chip('현재', 'ok') : chip('이전 버전')),
          b.status === 'ACTIVE' ? btn('QR 표시', wrap(async () => {
            const r = await post('bundles', { batch_id: b.id });
            window.open('/bundle/' + r.bundle_id.slice(0, 8), 'dg-qr', 'width=760,height=900');
            toast(`QR ${r.frames}장 (${r.count}건) 표시`); load();
          }, msg), 'primary', { disabled: !b.device_id }) : null),
        !b.device_id ? h('p', { class: 'err' }, '이 팀에 휴대폰이 연결되어 있지 않습니다 (설정).') : null,
        b.resend_needed && b.status === 'ACTIVE' ? h('div', { class: 'notice' }, '전송 뒤에 수정된 접수가 있습니다. QR을 다시 표시해 새 목록을 전달하세요. 급한 변경은 수거팀에 전화로 먼저 알리세요.') : null,
        h('p', {}, b.stops.map((x) => `${x.seq}. ${x.name}`).join('  ')),
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
    rep, conf, h('p', { class: 'muted' }, '같은 결과를 여러 번 가져와도 한 번만 반영됩니다. 오래된 배정·취소된 접수의 결과는 자동 반영하지 않고 충돌함에 둡니다.'));
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
  const load = wrap(async () => {
    s.date = date.value;
    const d = await api('close?date=' + s.date);
    const note = h('input', { placeholder: '마감 메모', style: 'flex:1' });
    const list = (title, arr, fmt) => h('div', { class: 'card' }, h('h2', {}, `${title} ${arr.length}건`), arr.length ? h('ul', {}, ...arr.map((x) => h('li', {}, fmt(x)))) : h('p', { class: 'muted' }, '없음'));
    body.replaceChildren(
      h('div', { class: 'card' }, h('h2', {}, `${s.date} 접수 ${d.total}건`), h('div', { class: 'row' }, ...Object.entries(d.by_status).map(([k, v]) => { const el = statusChip(k); el.textContent = `${STATUS[k] || k} ${v}`; return el; })),
        h('p', {}, `충돌 미처리 ${d.conflicts}건 · 사진 미회수 ${d.photos_missing}장 · 수신 미확인 전송 ${d.unconfirmed_bundles}건`),
        d.closed ? h('p', {}, chip('마감 기록됨', 'ok'), ` ${d.closed.closed_at.replace('T', ' ').slice(0, 16)} UTC ${d.closed.note || ''}`) : null),
      h('div', { class: 'grid2' },
        list('결과 미회수 (배정했으나 결과 없음)', d.no_result, (x) => `${x.receipt_no} ${x.name}`),
        list('재방문 후보 (일부·미수거·보류)', d.revisit, (x) => `${x.receipt_no} ${x.name} · ${STATUS[x.status]} · 남은 ${x.remaining}${x.next_date ? ` · 재방문 약속 ${x.next_date}${x.appt ? ' ' + x.appt[0] + (x.appt[1] ? '~' + x.appt[1] : '') : ''}` : ''}`),
        list('금액 확인 대기', d.pending_fee, (x) => `${x.receipt_no} ${x.name} · ${x.count}건`),
        list('수납 확인 필요 (미납·부족·과납·금액 미확정)', d.payments, (x) => `${x.receipt_no} ${x.name} · ${PAY[x.status]} · 계산 ${x.calculated == null ? '미확정' : won(x.calculated)} / 납부 ${won(x.paid)}`)),
      unloadCard(d),
      h('div', { class: 'card row' }, note, btn(d.closed ? '마감 다시 기록' : '마감 기록', wrap(async () => { await post('close', { date: s.date, note: note.value }); toast('마감을 기록했습니다'); load(); }), 'primary'),
        h('span', { class: 'muted' }, '미처리 건이 있어도 마감할 수 있으며, 그 목록이 함께 기록되어 다음 날로 이월됩니다.')));
  });
  date.addEventListener('change', load); load();
  return h('div', {}, h('h1', {}, '일일 마감'), h('div', { class: 'card' }, h('label', { class: 'f' }, '수거일', date)), body);
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
  const regImg = h('img', { alt: '기기 등록 QR', style: 'width:260px;image-rendering:pixelated' });
  api('registration-qr').then((r) => { regImg.src = r.image; }).catch(() => {});
  const code = h('textarea', { placeholder: '휴대폰 화면의 기기 코드 (DGD1.…)', rows: 3, style: 'width:100%' });
  const label = h('input', { placeholder: '기기 이름 (예: 1팀 업무폰)' });
  const fp = h('p', { class: 'big' });
  const devs = h('table', {}, h('thead', {}, h('tr', {}, ...['기기', '이름', '상태', '등록', ''].map((t) => h('th', {}, t)))),
    h('tbody', {}, ...boot.devices.map((d) => h('tr', {}, h('td', {}, d.id.slice(0, 8)), h('td', {}, d.label || ''), h('td', {}, d.status === 'ACTIVE' ? chip('사용', 'ok') : chip(d.status === 'LOST' ? '분실' : '중지', 'bad')),
      h('td', {}, d.registered_at.slice(0, 10)), h('td', {}, ...[['ACTIVE', '사용'], ['LOST', '분실 처리'], ['RETIRED', '사용 중지']].filter(([k]) => k !== d.status).map(([k, t]) =>
        btn(t, wrap(async () => { if (k !== 'ACTIVE' && !confirm(`${d.id.slice(0, 8)} 기기를 '${t}'합니다. 이 기기로는 목록을 보낼 수 없고 결과도 받지 않습니다. 계속할까요?`)) return; await post(`devices/${d.id}/status`, { status: k }); boot = null; render(); }, msg), 'small')))))));
  const crewRows = [...boot.crews, { id: '', name: '', device_id: '', capacity_m3: 8, shift_start: '09:00', shift_end: '17:00', active: 1 }].map((c) => {
    const f = { name: h('input', { value: c.name, placeholder: '새 팀 이름' }), device_id: h('select', {}, h('option', { value: '' }, '(없음)'), ...boot.devices.filter((d) => d.status === 'ACTIVE').map((d) => h('option', { value: d.id, selected: d.id === c.device_id }, `${d.id.slice(0, 8)} ${d.label || ''}`))),
      capacity_m3: h('input', { type: 'number', step: '0.5', value: c.capacity_m3, style: 'width:80px' }), shift_start: h('input', { type: 'time', value: c.shift_start }), shift_end: h('input', { type: 'time', value: c.shift_end }), active: h('input', { type: 'checkbox', checked: c.active }) };
    return h('tr', {}, ...Object.values(f).map((x) => h('td', {}, x)), h('td', {}, btn(c.id ? '저장' : '추가', wrap(async () => {
      await post('crews', { id: c.id || null, name: f.name.value, device_id: f.device_id.value, capacity_m3: f.capacity_m3.value, shift_start: f.shift_start.value, shift_end: f.shift_end.value, active: f.active.checked });
      toast('저장했습니다'); boot = null; render();
    }, msg), 'small primary')));
  });
  const st = boot.settings;
  const dlat = h('input', { value: st.depot.lat, style: 'width:120px' }), dlon = h('input', { value: st.depot.lon, style: 'width:120px' }), dlabel = h('input', { value: st.depot.label });
  const osrm = h('input', { value: st.osrm_url, placeholder: 'http://127.0.0.1:5000 (비우면 직선거리 추정)', style: 'width:320px' });
  const sb = h('input', { type: 'number', value: st.service_min_base, style: 'width:70px' }), sp = h('input', { type: 'number', value: st.service_min_per_item, style: 'width:70px' });
  const us = st.unload_site || {};
  const ulat = h('input', { value: us.lat ?? '', placeholder: '위도', style: 'width:120px' }), ulon = h('input', { value: us.lon ?? '', placeholder: '경도', style: 'width:120px' }), ulabel = h('input', { value: us.label || '', placeholder: '반입지 이름' });
  const umin = h('input', { type: 'number', value: st.unload_min, style: 'width:70px' }), umax = h('input', { type: 'number', value: st.max_unloads, min: 0, max: 5, style: 'width:60px' });
  return h('div', {}, h('h1', {}, '설정'),
    h('div', { class: 'grid2' },
      h('div', { class: 'card' }, h('h2', {}, '휴대폰 등록'), h('p', { class: 'muted' }, '1) 휴대폰 앱 [기기 등록]으로 아래 QR을 비춥니다 (공개키만 들어 있음). 2) 휴대폰의 기기 코드를 붙여 넣습니다. 3) 양쪽 6자리 지문이 같은지 확인합니다.'),
        regImg, code, label, btn('기기 등록', wrap(async () => { const r = await post('devices/register', { code: code.value, label: label.value }); fp.textContent = `지문 ${r.fingerprint.slice(0, 3)} ${r.fingerprint.slice(3)}`; toast('등록했습니다. 지문을 휴대폰과 대조하세요.'); boot = null; setTimeout(render, 2500); }, msg), 'primary'), fp),
      h('div', { class: 'card' }, h('h2', {}, '등록된 휴대폰'), devs, h('p', { class: 'muted' }, '분실 시 바로 "분실 처리"하세요. 오프라인 휴대폰의 자료를 원격으로 지울 수는 없지만, 이후 그 기기로는 목록을 보내지 않고 결과도 받지 않습니다.'))),
    h('div', { class: 'card' }, h('h2', {}, '수거팀·차량'), h('table', {}, h('thead', {}, h('tr', {}, ...['팀', '연결 휴대폰', '적재(m³)', '근무 시작', '근무 끝', '사용', ''].map((t) => h('th', {}, t)))), h('tbody', {}, ...crewRows))),
    h('div', { class: 'card' }, h('h2', {}, '노선 계산'), h('div', { class: 'row' }, '차고지 위도', dlat, '경도', dlon, dlabel), h('div', { class: 'row' }, '로컬 OSRM', osrm),
      h('div', { class: 'row' }, '지점당 기본 작업', sb, '분 + 품목당', sp, '분 (추정치)'),
      h('div', { class: 'row' }, '반입지', ulat, ulon, ulabel, '하차', umin, '분 · 차량당 최대', umax, '회'),
      h('p', { class: 'small muted' }, '반입지 좌표를 넣으면 적재가 찰 때 반입지에 들러 비우고 이어서 수거하는 순서를 추천합니다. 비우면 차량 1회 적재 안에서만 계산합니다.'),
      btn('저장', wrap(async () => { await post('settings', { depot: { lat: dlat.value, lon: dlon.value, label: dlabel.value }, osrm_url: osrm.value, service_min_base: sb.value, service_min_per_item: sp.value,
        unload_site: { lat: ulat.value, lon: ulon.value, label: ulabel.value }, unload_min: umin.value, max_unloads: umax.value }); toast('저장했습니다'); boot = null; render(); }, msg), 'primary')),
    volumeCard(),
    backupCard(),
    h('div', { class: 'card' }, h('h2', {}, '수수료표'), h('dl', { class: 'kv' }, h('dt', {}, '적용 표'), h('dd', {}, st.fee_label), h('dt', {}, '버전'), h('dd', {}, st.fee_version),
      h('dt', {}, '자료 확인일'), h('dd', {}, st.fee_checked_on), h('dt', {}, '시행일'), h('dd', {}, st.fee_effective_from || '미확인 (담당자 확인 필요)'))),
    msg);
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
async function render() {
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
      const screens = { home: screenHome, requests: screenRequests, request: () => screenRequest(arg), dispatch: screenDispatch, send: screenSend, results: screenResults, close: screenClose, qrtest: screenQrTest, settings: screenSettings, users: screenUsers, me: screenMe };
      // 역할별 메뉴 (서버도 같은 기준으로 막는다)
      const NAV = [['home', '오늘 현황', 'ADMIN INTAKE FIELD'], ['requests', '접수', 'ADMIN INTAKE FIELD'], ['dispatch', '배정·노선', 'ADMIN INTAKE'], ['send', '목록 전송', 'ADMIN INTAKE'],
        ['results', '결과 가져오기', 'ADMIN INTAKE FIELD'], ['close', '일일 마감', 'ADMIN INTAKE FIELD'], ['qrtest', 'QR 실측', 'ADMIN INTAKE FIELD'], ['settings', '설정', 'ADMIN'], ['users', '사용자·이력', 'ADMIN']];
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
  } catch (e) { view = h('p', { class: 'pad err' }, e.message); }
  if (my === renderSeq) $app.replaceChildren(view);
}
addEventListener('hashchange', render);
render();
