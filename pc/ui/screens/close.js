// PC 화면: 일일 마감
import { won } from '/web/fees.js';
import { ACTION, ENTITY, IDLE_RESET_MS, PAY, STATUS, afterWork, api, boot, btn, chip, confirmDlg, h, kst, modal, pagehead, post, postOnce, statusChip, toast, workEnd, wrap } from '../core.js';
import { refreshBadges } from './home.js';
import { unloadCard } from './settings.js';

const closeT0 = {}, closeSeen = {};
async function screenClose() {
  const s = (screenClose.s ||= { date: boot.today });
  const date = h('input', { type: 'date', value: s.date });
  const body = h('div', { onclick: () => { closeSeen[date.value] = Date.now(); }, oninput: () => { closeSeen[date.value] = Date.now(); } });
  let lastWant = null, seq = 0;
  const load = wrap(async (force) => {
    const want = date.value;
    if (!closeT0[want] || Date.now() - (closeSeen[want] || 0) > IDLE_RESET_MS) closeT0[want] = Date.now();
    closeSeen[want] = Date.now();
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
        (d.crews_no_result || []).length ? h('div', { class: 'notice bad' }, h('strong', {}, '결과를 가져오지 않은 팀: '), d.crews_no_result.map((c) => `${c.crew}(${kst(c.sent_at)} 전송)`).join(', '),
          afterWork(s.date) ? ' — 수거팀에 [결과 보내기]를 요청하세요. ' : ` — 근무 끝(${workEnd()}) 전이면 아직 수거 중일 수 있습니다. `, h('a', { href: '#/results' }, '결과 가져오기 →')) : null,
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
        // 목록을 보냈는데 결과 파일을 가져오지 않은 팀: 마감을 막지는 않지만 확인창에 먼저 보여 준다
        const nr = d.crews_no_result || [];
        const warnBox = nr.length ? h('div', { class: 'notice bad close-warn' }, h('strong', {}, `결과를 가져오지 않은 팀 ${nr.length}곳`),
          h('ul', {}, ...nr.map((c) => h('li', {}, `${c.crew}: ${kst(c.sent_at)}에 목록을 보냈는데 그 뒤 결과 파일이 없습니다.`))),
          h('p', { class: 'small' }, '수거팀에 [결과 보내기]를 요청해 가져온 뒤 마감하는 것이 좋습니다. 그대로 마감하면 그 팀 접수는 이월됩니다.')) : null;
        if (!(await confirmDlg(last ? '마감 다시 기록' : '마감 기록', `${s.date} 마감을 기록합니다.` + (d.carry.length ? ` 미처리 ${d.carry.length}건의 예정일이 다음 업무일로 옮겨집니다.` : '') + ' 기록은 회차로 쌓이며 팀장 확인을 받아야 합니다.',
          { ok: nr.length ? '그래도 마감 기록' : '마감 기록', more: warnBox ? [warnBox] : [] }))) return;
        const r = await postOnce(`close:${s.date}`, 'close', { date: s.date, note: note.value, close_secs: closeT0[s.date] ? Math.round((Date.now() - closeT0[s.date]) / 1000) : null }); delete closeT0[s.date];
        toast(`마감을 기록했습니다 (${r.seq}회차${r.carried.length ? ` · 이월 ${r.carried.length}건` : ''})`); (r.warnings || []).forEach((w) => toast(w, true)); load(true); refreshBadges(true);
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

export {
  closeSeen, closeT0, screenClose,
};
