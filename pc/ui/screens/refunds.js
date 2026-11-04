// PC 화면: 환불
import { won } from '/web/fees.js';
import { REFUND_CAUSE, REFUND_ST, api, boot, btn, canWork, chip, grid, gridbar, h, kst, modal, pagehead, post, postOnce, promptDlg, render, runJob, searchbox, toast, wrap } from '../core.js';

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
        onOk: () => postOnce(`refund:${c.id}`, 'refunds/request', { case_id: c.id, amount: amt.value, note: note.value }) }))) return;
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
  return h('div', {}, pagehead('환불', role === 'ADMIN' ? btn('환불 전체 점검', wrap(async () => {
      const r = await runJob('refunds/sync', {}, '환불 전체 점검'); toast(`환불 전체 점검: ${r.checked}건 확인 · 새로 생김 ${r.created} · 해소 ${r.closed}`); render();
    }, msg), 'small', { title: '자동 환불 건(납부 뒤 취소·과납)을 전체 접수와 맞춤. 평소에는 쓰기마다 그 접수만, 밤마다 전체' }) : null),
    h('div', { class: 'card searchbox', role: 'group', 'aria-label': '상태' }, h('div', { class: 'cond' }, h('span', { class: 'cond-l' }, '상태'), h('div', { class: 'cond-v wide' }, tabs))),
    gridbar(d.rows.length), msg,
    h('div', { class: 'card tablecard' }, grid(['접수번호', '부과번호', '동', '사유', '금액', '상태', '신청', '결재', '지급', '처리'], d.rows.map((c) => [h('a', { href: `#/request/${c.request_id}` }, c.receipt_no), c.levy_no || '', c.dong || '',
      REFUND_CAUSE[c.cause] || c.cause, won(c.amount), chip(REFUND_ST[c.status] || c.status, stCls(c.status)), c.requested_by ? `${c.requested_by} ${kst(c.requested_at)}` : (c.auto ? '자동 생성' : ''),
      c.approved_by ? `${c.approved_by} ${kst(c.approved_at)}${c.approve_note ? ' · ' + c.approve_note : ''}` : '', c.paid_on ? `${c.paid_on} · ${c.pay_method}${c.account_checked ? ' · 계좌 확인' : ''}${c.account_last4 ? ' ·· ' + c.account_last4 : ''}` : (c.void_reason || ''),
      h('span', { class: 'row' }, ...act(c))]), { num: [4], empty: '해당하는 환불 건이 없습니다.' })),
    d.partial.length ? h('div', { class: 'card' }, h('h2', {}, `일부 수거 차액 환불 후보 ${d.partial.length}건`),
      h('p', { class: 'muted small' }, '일부 수거로 끝나 실제로 실은 수량 기준 부과 금액보다 더 낸 경우입니다. 재방문하지 않기로 했을 때만 신청하세요(자동으로 신청하지 않음).'),
      grid(['접수번호', '부과번호', '동', '부과 금액', '납부', '차액', ''], d.partial.map((x) => [h('a', { href: `#/request/${x.request_id}` }, x.receipt_no), x.levy_no || '', x.dong || '', won(x.levied), won(x.paid), won(x.due),
        work ? btn('차액 환불 신청', wrap(async () => { await postOnce(`refund-partial:${x.request_id}`, 'refunds/request', { request_id: x.request_id, cause: 'PARTIAL', amount: x.due, note: '일부 수거 차액' }); toast('신청했습니다'); render(); }, msg), 'small') : '']), { num: [3, 4, 5] })) : null,
    h('p', { class: 'muted' }, '납부 뒤 취소(환불 필요)·과납은 자동으로 "신청 전" 건이 생깁니다. 접수 담당자가 신청하고, 팀장(검토자)·관리자 중 신청하지 않은 사람이 결재한 뒤, 지급일·방법을 기록합니다. 계좌번호는 저장하지 않습니다.'));
}

// ------------------------------------------------------------ 개인정보 접속기록 (관리자·팀장)

export {
  screenRefunds,
};
