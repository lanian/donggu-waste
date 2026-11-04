// PC 화면: 수수료표 관리
import { BUILTIN_FEES } from '/web/fees-data.js';
import { searchFees } from '/web/fees.js';
import { won } from '/web/fees.js';
import { FEES, api, boot, btn, chip, grid, h, kst, pagehead, post, render, setBoot, toast, wrap } from '../core.js';

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
      : h('div', { class: 'row' }, btn('원문과 대조했고 승인', wrap(async () => { await post(`fee-revisions/${draft.id}/approve`, {}); toast('승인했습니다. 시행일부터 새 접수에 적용됩니다.'); setBoot(null); render(); }, msg), 'primary'),
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

export {
  screenFees,
};
