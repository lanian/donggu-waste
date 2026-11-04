// PC 화면: 내 계정·사용자·변경 이력·진단
import { BUILTIN_FEES } from '/web/fees-data.js';
import { ACTION, ENTITY, api, boot, btn, chip, download, go, grid, h, kst, mb, pagehead, post, prefsControls, render, setBoot, toast, wrap } from '../core.js';
import { purgeLogTable } from '../widgets.js';

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
        await post('password', { old: old.value, password: pw.value }); toast('바꿨습니다'); setBoot(null); go('#/requests'); render();
      }, msg), 'primary'), msg),
    h('div', { class: 'card', style: 'max-width:460px' }, h('h2', {}, '화면 설정'), h('p', { class: 'muted small' }, '테마와 글자 크기는 계정마다 저장되어 다른 PC에서 로그인해도 같게 보입니다. 지도와 인쇄물은 항상 밝게 표시합니다.'), prefsControls()));
}

// ------------------------------------------------------------ 사용자·변경 이력 (운영 관리자)
const ROLE = { ADMIN: '운영 관리자', INTAKE: '접수 담당자', FIELD: '수거팀', LEAD: '팀장(검토자)' };
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
    admin ? diagCard() : null,
    purge ? h('div', { class: 'card' }, h('div', { class: 'panel-h' }, h('h2', {}, '개인정보 파기 대장'), h('span', { class: 'muted small' }, `보유기간 ${purge.retention_years}년 · 지금 대상 ${purge.preview.count}건`)),
      purgeLogTable(purge.log), admin ? h('a', { href: '#/settings/privacy', class: 'small' }, '보유기간·파기 설정 →') : null) : null);
}
function diagCard() {
  const body = h('div', {}, h('p', { class: 'muted' }, '불러오는 중…'));
  const load = wrap(async () => {
    const [d, o] = await Promise.all([api('diag'), api('ops-status')]);
    body.replaceChildren(
      h('dl', { class: 'kv compact' }, h('dt', {}, 'DB'), h('dd', {}, `${mb(d.db_bytes)} (WAL ${mb(d.wal_bytes)}, 빈 공간 ${mb(d.free_bytes)}) · 구조 ${d.schema_version} · auto_vacuum ${['NONE', 'FULL', 'INCREMENTAL'][d.auto_vacuum] || d.auto_vacuum}`),
        h('dt', {}, '사진'), h('dd', {}, `${(d.photos.count || 0).toLocaleString()}장 · ${mb(d.photos.bytes)}`),
        d.pre_migration ? [h('dt', {}, '업데이트 사본'), h('dd', {}, d.pre_migration.count ? `${d.pre_migration.count}개 · ${mb(d.pre_migration.bytes)} (state/pre-migration, 개인정보 포함 — 가장 새 1개 외에는 ${d.pre_migration.keep}개·${d.pre_migration.days}일 넘으면 자동 삭제)` : '없음')] : null,
        h('dt', {}, '서버'), h('dd', {}, `로그인 세션 ${d.sessions} · 대기 작업 ${d.jobs_queued} · 예약 확인 ${d.scheduler_tick ? kst(d.scheduler_tick) : '—'}`)),
      grid(['예약 작업', '마지막 실행', '상태'], o.items.map((x) => [x.label, x.last_at ? `${kst(x.last_at)} (${x.age_days}일 전)` : '기록 없음', chip(x.stale ? '확인 필요' : '정상', x.stale ? 'bad' : 'ok')]), { cls: 'ops-tbl' }),
      h('details', { class: 'tableview' }, h('summary', {}, '표별 건수'), grid(['표', '건수'], Object.entries(d.tables).map(([t, n]) => [t, n == null ? '—' : n.toLocaleString()]), { num: [1] })),
      diagHint());
  });
  load();
  return h('div', { class: 'card', id: 'diag' }, h('div', { class: 'panel-h' }, h('h2', {}, '진단'), btn('새로 고침', load, 'small')), body);
}
const diagHint = () => h('p', { class: 'muted small' }, '예약 작업은 서버가 켜져 있는 동안 하루 한 번(02:30 이후) 돕니다. 이틀 넘게 기록이 없으면 서버가 밤에 꺼져 있었는지 확인하세요. 큰 DB를 줄이려면 서버를 끄고 python pc/pc_tool.py vacuum.');
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

export {
  KEY, ROLE, diagCard, diagHint, screenMe, screenUsers, summarizeDetail,
};
