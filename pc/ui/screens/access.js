// PC 화면: 개인정보 접속기록
import { ACCESS, api, boot, btn, chip, download, grid, gridbar, h, kst, pagehead, post, render, runJob, searchbox, toast, wrap } from '../core.js';

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
  const showVerify = (v, kind = '') => vres.replaceChildren(h('div', { class: 'notice ' + (v.ok ? 'info' : 'bad') }, h('strong', {}, `${kind}무결성 검사 ${v.ok ? '정상' : '실패'}`),
    ` · 접속기록 ${v.access.checked}행 ${v.access.ok ? '정상' : `— ${v.access.broken_id}번에서 끊김: ${v.access.why}`} · 변경 이력 ${v.audit.checked}행 ${v.audit.ok ? '정상' : `— ${v.audit.broken_id}번에서 끊김: ${v.audit.why}`}`,
    v.audit.legacy ? ` (해시 사슬 이전 기록 ${v.audit.legacy}행은 검사 대상 아님)` : ''));
  // 빠른 검사: 저장한 검사점 뒤 새 행만(바로). 전체 검사: 처음부터 끝까지(백그라운드 작업, 진행 표시)
  const verify = wrap(async () => { const v = await api('integrity'); showVerify(v, '빠른 '); if (v.incremental || (v.access && v.access.incremental)) toast('빠른 검사: 지난 검사점 뒤 새 기록만 확인했습니다'); });
  const verifyFull = wrap(async () => showVerify(await runJob('integrity/full', {}, '전체 무결성 검사'), '전체 '));
  const done = st.done;
  return h('div', {}, pagehead('개인정보 접속기록', btn('빠른 무결성 검사', verify, 'small', { title: '지난 검사점 뒤에 쌓인 기록만 확인 (몇 초)' }),
    boot.me.role === 'ADMIN' ? btn('전체 무결성 검사', verifyFull, 'small', { title: '처음부터 끝까지 확인 (백그라운드 작업, 기록이 많으면 몇 분)' }) : null, btn('CSV 내보내기', wrap(async () => { await download(`access.csv?${qs()}`, `동구-접속기록-${s.date_from}_${s.date_to}.csv`); toast('내려받았습니다 (내보낸 기록이 이력에 남습니다)'); }), 'small')),
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

export {
  screenAccess,
};
